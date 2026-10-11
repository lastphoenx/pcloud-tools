#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Retention-Modus, Lösch-Grenzen (Abbruch) und Konsistenz-Gate (A3/A5)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

import pool_consistency_lib as pcl

MODE_TIME = "time"
MODE_RTB_MIRROR = "rtb-mirror"
MODE_UNSET = "unset"


def env_get(env_vars: Optional[Dict[str, str]], key: str, default: str = "") -> str:
    if env_vars and key in env_vars:
        return env_vars[key]
    return os.environ.get(key, default)


def time_retention_params(env_vars: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
    days_full = int(env_get(env_vars, "PCLOUD_REMOTE_RETENTION_DAYS_FULL", "0") or 0)
    weekly_weeks = int(env_get(env_vars, "PCLOUD_REMOTE_RETENTION_WEEKLY_WEEKS", "54") or 54)
    return days_full, weekly_weeks


def time_retention_enabled(env_vars: Optional[Dict[str, str]] = None) -> bool:
    return time_retention_params(env_vars)[0] > 0


def resolve_retention_mode(
    env_vars: Optional[Dict[str, str]],
    *,
    allow_rtb_mode: bool,
) -> Tuple[str, Optional[str]]:
    """
    time: PCLOUD_REMOTE_RETENTION_DAYS_FULL > 0
    rtb-mirror: nur mit PCLOUD_RETENTION_MODE=rtb-mirror und --allow-rtb-mode
    unset: sonst (kein stiller Fallback auf rtb-spiegel)
    """
    if time_retention_enabled(env_vars):
        return MODE_TIME, None
    mode_env = (env_get(env_vars, "PCLOUD_RETENTION_MODE", "") or "").strip().lower()
    if mode_env in ("rtb-mirror", "rtb_spiegel", "rtb-spiegel"):
        if not allow_rtb_mode:
            return MODE_RTB_MIRROR, (
                "rtb-spiegel blockiert ohne --allow-rtb-mode "
                "(Zeit-Retention: PCLOUD_REMOTE_RETENTION_DAYS_FULL>0 in --env-file)"
            )
        return MODE_RTB_MIRROR, None
    return MODE_UNSET, (
        "Retention-Modus unklar: PCLOUD_REMOTE_RETENTION_DAYS_FULL=0 — "
        "setze DAYS_FULL>0 für Zeit-Retention oder "
        "PCLOUD_RETENTION_MODE=rtb-mirror nur mit --allow-rtb-mode"
    )


def retention_env_summary(env_vars: Optional[Dict[str, str]]) -> str:
    days_full, weekly_weeks = time_retention_params(env_vars)
    mode = (env_get(env_vars, "PCLOUD_RETENTION_MODE", "") or "").strip() or "(default)"
    max_sn = env_get(env_vars, "PCLOUD_RETENTION_MAX_DELETE_SNAPS", "40")
    max_pct = env_get(env_vars, "PCLOUD_RETENTION_MAX_DELETE_PCT", "30")
    gc_pct = env_get(env_vars, "PCLOUD_GC_MAX_DELETE_PCT", "15")
    catchup = env_get(env_vars, "PCLOUD_CATCHUP_MAX_PER_RUN", "1")
    return (
        f"DAYS_FULL={days_full} WEEKLY_WEEKS={weekly_weeks} "
        f"RETENTION_MODE={mode} MAX_DELETE_SNAPS={max_sn} MAX_DELETE_PCT={max_pct}% "
        f"GC_MAX_DELETE_PCT={gc_pct}% CATCHUP_MAX_PER_RUN={catchup}"
    )


def validate_env_file_exists(env_file: str) -> Optional[str]:
    if not env_file or not os.path.isfile(env_file):
        return f"--env-file nicht lesbar: {env_file}"
    return None


def check_rtb_backup_marker(rtb_root: str) -> Optional[str]:
    """Wie rtb_staged_backup.sh: gültiges RTB nur mit backup.marker."""
    marker = os.path.join(rtb_root, "backup.marker")
    if not os.path.isfile(marker):
        return f"Kein backup.marker unter {rtb_root} — Abbruch (RTB nicht gemountet?)"
    return None


def check_snapshot_delete_allowed(
    n_delete: int,
    n_remote: int,
    env_vars: Optional[Dict[str, str]],
    cli_max_snapshots: Optional[int],
) -> Optional[str]:
    """
    Abbruch wenn Grenze überschritten — nichts wird gelöscht.
    CLI --max-delete-snapshots N = explizite Obergrenze für diesen Lauf.
    """
    if n_delete <= 0:
        return None
    if cli_max_snapshots is not None:
        if n_delete > int(cli_max_snapshots):
            return (
                f"Retention: {n_delete} Snapshots > --max-delete-snapshots={cli_max_snapshots}"
            )
        return None

    max_abs = int(env_get(env_vars, "PCLOUD_RETENTION_MAX_DELETE_SNAPS", "40") or 40)
    max_pct = float(env_get(env_vars, "PCLOUD_RETENTION_MAX_DELETE_PCT", "30") or 30)

    if n_delete > max_abs:
        return (
            f"Retention: {n_delete} Snapshots > PCLOUD_RETENTION_MAX_DELETE_SNAPS={max_abs} "
            f"(bewusst mehr: --max-delete-snapshots N)"
        )
    if n_remote > 0:
        pct = 100.0 * n_delete / n_remote
        if pct > max_pct:
            return (
                f"Retention: {n_delete}/{n_remote} ({pct:.1f}%) > "
                f"PCLOUD_RETENTION_MAX_DELETE_PCT={max_pct}% "
                f"(bewusst mehr: --max-delete-snapshots N)"
            )
    return None


def check_gc_delete_allowed(
    n_candidates: int,
    n_pool_files: int,
    env_vars: Optional[Dict[str, str]],
    cli_max_pct: Optional[float],
) -> Optional[str]:
    """Abbruch wenn Löschkandidaten > erlaubter Anteil am Pool (Default 15 %)."""
    if n_candidates <= 0:
        return None
    if n_pool_files <= 0:
        return None

    if cli_max_pct is not None:
        allowed_pct = float(cli_max_pct)
    else:
        allowed_pct = float(env_get(env_vars, "PCLOUD_GC_MAX_DELETE_PCT", "15") or 15)

    actual_pct = 100.0 * n_candidates / n_pool_files
    if actual_pct > allowed_pct:
        return (
            f"Pool-GC: {n_candidates}/{n_pool_files} ({actual_pct:.1f}%) > "
            f"erlaubt {allowed_pct}% "
            f"(bewusst mehr: --max-delete-pct N)"
        )
    return None


def live_drop_warn_ratio(env_vars: Optional[Dict[str, str]]) -> float:
    """
    Anteil Live-Snapshot-Rückgang vs. letzter JSONL-Zeile, bevor critical.
    Default: PCLOUD_RETENTION_MAX_DELETE_PCT (30) — geplante Monats-Retention nicht als Incident.
    """
    custom = (env_get(env_vars, "PCLOUD_POOL_CONSISTENCY_LIVE_DROP_RATIO", "") or "").strip()
    if custom:
        return float(custom)
    pct = float(env_get(env_vars, "PCLOUD_RETENTION_MAX_DELETE_PCT", "30") or 30)
    return pct / 100.0


def record_consistency_jsonl(main_dir: str, env_file: str) -> None:
    """Nach erfolgreicher Retention: JSONL-Basis aktualisieren (live_count_drop)."""
    script = os.path.join(main_dir, "scripts", "pool_consistency_check.py")
    if not os.path.isfile(script):
        return
    subprocess.run(
        [sys.executable, script, "--env-file", env_file],
        cwd=main_dir,
        capture_output=True,
        text=True,
        check=False,
    )


def parse_consistency_verdict(stdout: str) -> Optional[Dict[str, object]]:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "level" in data:
            return data
    return None


def run_consistency_gate(
    main_dir: str,
    env_file: str,
    *,
    max_allowed_level: int = 1,
) -> Optional[str]:
    """
    pool_consistency_check: Exit 0/1/2 = Level; 3 = Check-Crash; ohne JSON = blockieren.
    """
    script = os.path.join(main_dir, "scripts", "pool_consistency_check.py")
    if not os.path.isfile(script):
        return f"Konsistenzcheck fehlt: {script}"
    proc = subprocess.run(
        [sys.executable, script, "--env-file", env_file, "--no-jsonl"],
        cwd=main_dir,
        capture_output=True,
        text=True,
    )
    verdict = parse_consistency_verdict(proc.stdout or "")
    valid_names = ("ok", "warn", "critical")
    if verdict is None:
        err_tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return (
            f"pool_consistency_check ohne gültiges JSON (exit={proc.returncode}); "
            f"Retention/GC abgebrochen. {err_tail}"
        )
    level_name = str(verdict.get("level_name") or "")
    if level_name == "check_failed" or proc.returncode == pcl.LEVEL_CHECK_FAILED:
        return (
            f"pool_consistency_check fehlgeschlagen: {verdict.get('error', level_name)}"
        )
    if proc.returncode not in (pcl.LEVEL_OK, pcl.LEVEL_WARN, pcl.LEVEL_CRITICAL):
        return (
            f"pool_consistency_check unerwarteter exit={proc.returncode}; "
            f"Retention/GC abgebrochen."
        )
    if level_name and level_name not in valid_names:
        return f"pool_consistency_check unbekanntes level_name={level_name}"
    try:
        level = int(verdict.get("level", pcl.LEVEL_CRITICAL))
    except (TypeError, ValueError):
        return "pool_consistency_check: level im JSON ungültig"
    if level <= max_allowed_level:
        return None
    err_tail = (proc.stderr or "").strip()[-500:]
    return (
        f"pool_consistency_check level={level} (max erlaubt {max_allowed_level}); "
        f"Retention/GC abgebrochen. {err_tail}"
    )
