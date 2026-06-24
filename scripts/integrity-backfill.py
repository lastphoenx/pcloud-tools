#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill Pool-Integritaet: Post-Upload (Spalte 1) und/oder Monthly-Audit (Spalte 2).

Spalte 1 (Dashboard Post-Upload): check_type=manual -> backup_runs.integrity_*
Spalte 2 (Dashboard Audit):       check_type=monthly_audit -> snapshot_integrity_checks

Beispiele:
  # Nur anzeigen was fehlt:
  python scripts/integrity-backfill.py --env-file .env --dry-run

  # Post-Upload fuer alle ohne Eintrag (~1-2 min/Snapshot):
  python scripts/integrity-backfill.py --env-file .env --post-upload

  # Monthly-Audit einmalig fuer alle (~1-2 min/Snapshot, Pi nicht belasten):
  python scripts/integrity-backfill.py --env-file .env --audit --max 5

  # Beides, max 3 Snapshots diesmal:
  python scripts/integrity-backfill.py --env-file .env --post-upload --audit --max 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Set, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc  # noqa: E402

ENV_FILE = os.environ.get("ENV_FILE", f"{MAIN_DIR}/.env")


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


def _audit_state(env: Dict[str, str]) -> Dict[str, Tuple[str | None, str | None]]:
    rows = _mysql_lines(
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
    state: Dict[str, Tuple[str | None, str | None]] = {}
    for line in rows:
        parts = line.split("\t")
        if len(parts) >= 3:
            state[parts[0]] = (parts[1], parts[2])
    return state


def _mysql_lines(env: Dict[str, str], sql: str) -> List[str]:
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


def _post_upload_done(env: Dict[str, str]) -> Set[str]:
    rows = _mysql_lines(
        env,
        """
        SELECT DISTINCT snapshot_name FROM backup_runs
        WHERE integrity_status IS NOT NULL;
        """,
    )
    return set(rows)


def _run_integrity(env: Dict[str, str], snap: str, check_type: str) -> int:
    dest = env.get("PCLOUD_DEST", "/Backup/rtb_pool")
    py = (
        "/opt/apps/pcloud-tools/venv/bin/python"
        if os.path.isfile("/opt/apps/pcloud-tools/venv/bin/python")
        else sys.executable
    )
    script = os.path.join(MAIN_DIR, "scripts", "utilities", "pool_integrity_run.py")
    return subprocess.run(
        [
            py, script,
            "--env-file", env.get("_env_file", ENV_FILE),
            "--pool-root", dest,
            "--snapshot", snap,
            "--check-type", check_type,
        ],
        check=False,
    ).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill pool integrity DB columns")
    ap.add_argument("--env-file", default=ENV_FILE)
    ap.add_argument("--post-upload", action="store_true", help="Spalte 1: manual -> backup_runs")
    ap.add_argument("--audit", action="store_true", help="Spalte 2: monthly_audit -> snapshot_integrity_checks")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=0, help="Max Snapshots pro Modus (0=alle)")
    ap.add_argument("--oldest-first", action="store_true", help="Aelteste zuerst (default: neueste)")
    args = ap.parse_args()

    if not args.post_upload and not args.audit and not args.dry_run:
        ap.error("Mindestens --post-upload, --audit oder --dry-run angeben")

    env = _load_env(args.env_file)
    env["_env_file"] = args.env_file
    if env.get("PCLOUD_ENABLE_DB", "0") != "1":
        print("PCLOUD_ENABLE_DB=0 — Abbruch")
        return 1

    dest = env.get("PCLOUD_DEST", "/Backup/rtb_pool")
    cfg = pc.effective_config(env_file=args.env_file)
    remote = _complete_remote_snaps(cfg, dest)
    if not remote:
        print("Keine vollstaendigen Remote-Snapshots")
        return 0

    order = sorted(remote) if args.oldest_first else sorted(remote, reverse=True)
    pu_done = _post_upload_done(env)
    audit_state = _audit_state(env)
    audit_done = {s for s, (at, st) in audit_state.items() if at and st == "OK"}

    need_pu = [s for s in order if s not in pu_done]
    need_audit = [s for s in order if s not in audit_done]

    print(f"Remote complete: {len(remote)}")
    print(f"Post-Upload fehlend: {len(need_pu)}")
    print(f"Monthly-Audit fehlend (nie OK): {len(need_audit)}")

    if args.dry_run:
        if need_pu:
            print("\nPost-Upload Backlog (erste 10):")
            for s in need_pu[:10]:
                print(f"  {s}")
        if need_audit:
            print("\nAudit Backlog (erste 10):")
            for s in need_audit[:10]:
                print(f"  {s}")
        est = (len(need_pu) if args.post_upload else 0) + (len(need_audit) if args.audit else 0)
        if not args.post_upload and not args.audit:
            est = len(need_pu) + len(need_audit)
        print(f"\nGeschaetzt ~{est * 2} min bei sequentiellen Laeufen")
        return 0

    errors = 0
    if args.post_upload:
        todo = need_pu[: args.max] if args.max > 0 else need_pu
        print(f"\n=== Post-Upload Backfill ({len(todo)} Snapshot(s)) ===")
        for i, snap in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {snap}")
            if _run_integrity(env, snap, "manual") != 0:
                errors += 1

    if args.audit:
        todo = need_audit[: args.max] if args.max > 0 else need_audit
        print(f"\n=== Monthly-Audit Backfill ({len(todo)} Snapshot(s)) ===")
        for i, snap in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {snap}")
            if _run_integrity(env, snap, "monthly_audit") != 0:
                errors += 1

    print(f"\nFertig. Fehler: {errors}")
    print("Dashboard: sudo scripts/generate_reports.sh && sudo systemctl restart monitoring-dashboard.service")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
