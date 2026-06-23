#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pick next snapshot for monthly integrity audit and run pool_integrity_run."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
ENV_FILE = os.environ.get("ENV_FILE", f"{MAIN_DIR}/.env")
sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc  # noqa: E402

_UTIL = os.path.join(MAIN_DIR, "scripts", "utilities")
if _UTIL not in sys.path:
    sys.path.insert(0, _UTIL)


def _load_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _mysql(env: Dict[str, str], sql: str) -> List[str]:
    pw = env.get("PCLOUD_DB_PASS", "")
    if not pw:
        return []
    proc = subprocess.run(
        [
            "mysql", "-h", env.get("PCLOUD_DB_HOST", "localhost"),
            "-P", env.get("PCLOUD_DB_PORT", "3306"),
            "-u", env.get("PCLOUD_DB_USER", "pcloud_backup"),
            f"-D{env.get('PCLOUD_DB_NAME', 'pcloud_backup')}",
            "-N", "-B", "-e", sql,
        ],
        env={**os.environ, "MYSQL_PWD": pw},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.strip().splitlines() if ln]


def _complete_remote_snaps(cfg: dict, dest: str) -> List[str]:
    snaps_root = f"{dest.rstrip('/')}/_snapshots"
    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
    out: List[str] = []
    for c in (top.get("metadata", {}) or {}).get("contents", []) or []:
        if not c.get("isfolder") or c.get("name") == "_index":
            continue
        snap = c.get("name", "")
        marker = f"{snaps_root}/{snap}/.upload_complete"
        try:
            if not pc.stat_file_safe(cfg, path=marker):
                continue
            data = json.loads(pc.get_textfile(cfg, path=marker))
            if str(data.get("snapshot", "")) == str(snap):
                out.append(snap)
        except Exception:
            continue
    return sorted(out)


def _audit_state(env: Dict[str, str]) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """snapshot -> (last_started_at, last_status) for monthly_audit."""
    rows = _mysql(
        env,
        """
        SELECT snapshot_name,
               MAX(started_at),
               SUBSTRING_INDEX(GROUP_CONCAT(status ORDER BY started_at DESC), ',', 1)
        FROM snapshot_integrity_checks
        WHERE check_type = 'monthly_audit'
        GROUP BY snapshot_name;
        """,
    )
    state: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for line in rows:
        parts = line.split("\t")
        if len(parts) >= 3:
            state[parts[0]] = (parts[1], parts[2])
    return state


def _pick_next(remote: List[str], state: Dict[str, Tuple[Optional[str], Optional[str]]]) -> Optional[str]:
    def sort_key(snap: str):
        last_at, last_st = state.get(snap, (None, None))
        never = last_at is None
        failed = last_st == "FAILED"
        stale = False
        if last_at:
            try:
                dt = datetime.strptime(last_at[:19], "%Y-%m-%d %H:%M:%S")
                stale = (datetime.now() - dt).days >= 35
            except ValueError:
                stale = True
        # never first, then failed, then stale/oldest
        return (0 if never else 1, 0 if failed else 1, 0 if stale else 1, last_at or "", snap)

    if not remote:
        return None
    return sorted(remote, key=sort_key)[0]


def main() -> int:
    env = _load_env(ENV_FILE)
    for k, v in os.environ.items():
        if k.startswith("PCLOUD_"):
            env.setdefault(k, v)

    if env.get("PCLOUD_ENABLE_DB", "0") != "1":
        print("[integrity-audit] PCLOUD_ENABLE_DB=0 — Abbruch")
        return 0

    dest = env.get("PCLOUD_DEST", "/Backup/rtb_pool")
    cfg = pc.effective_config(env_file=ENV_FILE)
    remote = _complete_remote_snaps(cfg, dest)
    if not remote:
        print("[integrity-audit] Keine vollstaendigen Remote-Snapshots")
        return 0

    state = _audit_state(env)
    snap = _pick_next(remote, state)
    if not snap:
        print("[integrity-audit] Kein Snapshot gewaehlt")
        return 0

    print(f"[integrity-audit] Pruefe: {snap} ({len(remote)} remote complete)")

    py = (
        "/opt/apps/pcloud-tools/venv/bin/python"
        if os.path.isfile("/opt/apps/pcloud-tools/venv/bin/python")
        else sys.executable
    )
    integrity_run = os.path.join(MAIN_DIR, "scripts", "utilities", "pool_integrity_run.py")
    proc = subprocess.run(
        [
            py, integrity_run,
            "--env-file", ENV_FILE,
            "--pool-root", dest,
            "--snapshot", snap,
            "--check-type", "monthly_audit",
        ],
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
