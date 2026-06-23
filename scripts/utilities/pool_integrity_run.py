#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_integrity_run.py — Integritaetscheck pro Snapshot mit DB-Tracking.

Wrappt pool_verify_backup.run_verify(), schreibt Ergebnis nach MariaDB
(snapshot_integrity_checks) und JSON unter PCLOUD_ARCHIVE_DIR/integrity/.

Aufruf:
  python pool_integrity_run.py \\
    --env-file .env --pool-root /Backup/rtb_pool \\
    --snapshot 2026-06-23-130737 --check-type post_upload

  # Monatlicher Audit (ein Snapshot):
  python pool_integrity_run.py ... --check-type monthly_audit --snapshot SNAP
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc

# pool_verify_backup im gleichen Verzeichnis
_UTIL_DIR = os.path.dirname(os.path.abspath(__file__))
if _UTIL_DIR not in sys.path:
    sys.path.insert(0, _UTIL_DIR)
from pool_verify_backup import run_verify  # noqa: E402


def _load_env_file(path: str) -> Dict[str, str]:
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


def _sql_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("'", "''")


def _mysql(env: Dict[str, str], sql: str) -> tuple[int, str, str]:
    db_pass = env.get("PCLOUD_DB_PASS") or os.environ.get("PCLOUD_DB_PASS", "")
    if not db_pass:
        return 1, "", "PCLOUD_DB_PASS not set"
    host = env.get("PCLOUD_DB_HOST", "localhost")
    port = env.get("PCLOUD_DB_PORT", "3306")
    user = env.get("PCLOUD_DB_USER", "pcloud_backup")
    db = env.get("PCLOUD_DB_NAME", "pcloud_backup")
    proc = subprocess.run(
        ["mysql", "-h", host, "-P", port, "-u", user, f"-D{db}", "-e", sql],
        env={**os.environ, "MYSQL_PWD": db_pass},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _db_enabled(env: Dict[str, str]) -> bool:
    if env.get("PCLOUD_ENABLE_DB", "0") != "1":
        return False
    rc, _, _ = _mysql(env, "SELECT 1 FROM snapshot_integrity_checks LIMIT 0;")
    return rc == 0


def _db_insert_running(
    env: Dict[str, str],
    check_id: str,
    snapshot: str,
    check_type: str,
    backup_run_id: Optional[str],
) -> bool:
    br = f"'{_sql_escape(backup_run_id)}'" if backup_run_id else "NULL"
    sql = (
        f"INSERT INTO snapshot_integrity_checks "
        f"(check_id, snapshot_name, check_type, status, started_at, backup_run_id) "
        f"VALUES ('{_sql_escape(check_id)}', '{_sql_escape(snapshot)}', "
        f"'{_sql_escape(check_type)}', 'RUNNING', NOW(), {br});"
    )
    rc, _, err = _mysql(env, sql)
    if rc != 0:
        print(f"[warn] DB insert failed: {err.strip()}", file=sys.stderr)
    return rc == 0


def _db_finish(
    env: Dict[str, str],
    check_id: str,
    status: str,
    issues: int,
    report_path: str,
    error_summary: Optional[str],
    duration_sec: float,
) -> None:
    summary_sql = (
        f"'{_sql_escape(error_summary[:2000])}'" if error_summary else "NULL"
    )
    report_sql = f"'{_sql_escape(report_path)}'" if report_path else "NULL"
    sql = (
        f"UPDATE snapshot_integrity_checks SET "
        f"status='{status}', finished_at=NOW(), "
        f"duration_sec={int(round(duration_sec))}, "
        f"issues_count={int(issues)}, "
        f"report_path={report_sql}, "
        f"error_summary={summary_sql} "
        f"WHERE check_id='{_sql_escape(check_id)}';"
    )
    rc, _, err = _mysql(env, sql)
    if rc != 0:
        print(f"[warn] DB update failed: {err.strip()}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-snapshot integrity check + DB tracking")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--pool-root", help="Remote pool root (z.B. /Backup/rtb_pool)")
    ap.add_argument("--dest-root", help="Deprecated alias for --pool-root")
    ap.add_argument("--snapshot", required=True, help="Snapshot name to verify")
    ap.add_argument(
        "--check-type",
        choices=("post_upload", "monthly_audit", "manual"),
        default="manual",
    )
    ap.add_argument("--backup-run-id", help="Link to backup_runs.run_id (post_upload)")
    ap.add_argument("--stub-sample", type=int, default=0)
    ap.add_argument("--json-out", help="JSON report path (default: archive/integrity/)")
    ap.add_argument("--no-db", action="store_true", help="Skip MariaDB writes")
    args = ap.parse_args()

    pool_root = args.pool_root or args.dest_root
    if not pool_root:
        print("[FAIL] --pool-root required", file=sys.stderr)
        return 2

    env = _load_env_file(args.env_file)
    for k, v in os.environ.items():
        if k.startswith("PCLOUD_"):
            env.setdefault(k, v)

    archive = env.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    manifests_dir = os.path.join(archive, "manifests")
    integrity_dir = os.path.join(archive, "integrity")
    os.makedirs(integrity_dir, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.json_out or os.path.join(
        integrity_dir,
        f"{args.snapshot}_{args.check_type}_{ts}.json",
    )

    check_id = str(uuid.uuid4())
    use_db = not args.no_db and _db_enabled(env)

    print(f"=== pool_integrity_run ===")
    print(f"Snapshot:   {args.snapshot}")
    print(f"Check type: {args.check_type}")
    print(f"Check ID:   {check_id}")
    print()

    if use_db:
        _db_insert_running(env, check_id, args.snapshot, args.check_type, args.backup_run_id)

    cfg = pc.effective_config(env_file=args.env_file)
    t0 = time.time()
    try:
        result: Dict[str, Any] = run_verify(
            cfg,
            pool_root_raw=pool_root,
            manifests_dir=manifests_dir,
            snapshot_filter=[args.snapshot],
            stub_sample=args.stub_sample,
            verbose=True,
        )
    except Exception as e:
        result = {
            "ok": False,
            "issues": 1,
            "error": str(e),
            "error_summary": str(e),
            "duration_sec": round(time.time() - t0, 2),
        }
        print(f"[FAIL] verify exception: {e}", file=sys.stderr)

    result["check_id"] = check_id
    result["check_type"] = args.check_type
    result["snapshot"] = args.snapshot
    result["timestamp_iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n[report] {report_path}")

    ok = bool(result.get("ok"))
    issues = int(result.get("issues") or 0)
    summary = result.get("error_summary") or result.get("error")
    duration = float(result.get("duration_sec") or (time.time() - t0))

    if use_db:
        _db_finish(
            env,
            check_id,
            "OK" if ok else "FAILED",
            issues,
            report_path,
            str(summary) if summary else None,
            duration,
        )

    print("=" * 60)
    print("RESULT:", "OK" if ok else f"FAILED ({issues} issues)")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
