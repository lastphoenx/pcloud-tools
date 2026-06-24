#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_integrity_run.py — Integritaetscheck pro Snapshot mit DB-Tracking.

Speichert:
  post_upload / manual  -> backup_runs.integrity_* (am Upload-Lauf, kein Log-Muell)
  monthly_audit         -> snapshot_integrity_checks (1 Zeile pro Snapshot, UPSERT)

JSON-Reports bleiben unter PCLOUD_ARCHIVE_DIR/integrity/ (Dateisystem, nicht DB-Historie).
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

_UTIL_DIR = os.path.dirname(os.path.abspath(__file__))
if _UTIL_DIR not in sys.path:
    sys.path.insert(0, _UTIL_DIR)
from pool_verify_backup import PoolRemoteCache, run_verify  # noqa: E402


def run_integrity_for_snapshot(
    *,
    env: Dict[str, str],
    cfg: dict,
    pool_root: str,
    snapshot: str,
    check_type: str,
    backup_run_id: Optional[str] = None,
    stub_sample: int = 0,
    remote_cache: Optional[PoolRemoteCache] = None,
    verbose: bool = True,
    no_db: bool = False,
    json_out: Optional[str] = None,
) -> tuple[bool, Dict[str, Any]]:
    """Einzel-Snapshot-Integrity inkl. Report + DB. Fuer Batch mit remote_cache."""
    archive = env.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    manifests_dir = os.path.join(archive, "manifests")
    integrity_dir = os.path.join(archive, "integrity")
    os.makedirs(integrity_dir, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = json_out or os.path.join(
        integrity_dir, f"{snapshot}_{check_type}_{ts}.json"
    )

    use_db = not no_db and _db_enabled(env)

    if verbose:
        print("=== pool_integrity_run ===")
        print(f"Snapshot:   {snapshot}")
        print(f"Check type: {check_type}")
        print()

    t0 = time.time()
    try:
        result: Dict[str, Any] = run_verify(
            cfg,
            pool_root_raw=pool_root,
            manifests_dir=manifests_dir,
            snapshot_filter=[snapshot],
            stub_sample=stub_sample,
            verbose=verbose,
            remote_cache=remote_cache,
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

    result["check_type"] = check_type
    result["snapshot"] = snapshot
    result["timestamp_iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    if verbose:
        print(f"\n[report] {report_path}")

    ok = bool(result.get("ok"))
    issues = int(result.get("issues") or 0)
    summary = result.get("error_summary") or result.get("error")
    duration = float(result.get("duration_sec") or (time.time() - t0))
    db_status = "OK" if ok else "FAILED"

    if use_db:
        if check_type == "monthly_audit":
            _upsert_monthly_audit(
                env, snapshot, db_status, issues, report_path,
                str(summary) if summary else None, duration,
            )
        else:
            _save_to_backup_run(
                env, snapshot, backup_run_id, db_status, issues, report_path,
            )

    if verbose:
        print("=" * 60)
        print("RESULT:", "OK" if ok else f"FAILED ({issues} issues)")
        print("=" * 60)

    return ok, result


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
    rc, _, _ = _mysql(env, "SELECT integrity_status FROM backup_runs LIMIT 0;")
    return rc == 0


def _save_to_backup_run(
    env: Dict[str, str],
    snapshot: str,
    backup_run_id: Optional[str],
    status: str,
    issues: int,
    report_path: str,
) -> None:
    st = "OK" if status == "OK" else "FAILED"
    rp = f"'{_sql_escape(report_path)}'" if report_path else "NULL"
    if backup_run_id:
        where = f"run_id = '{_sql_escape(backup_run_id)}'"
    else:
        # Letzter Lauf je Snapshot (SUCCESS bevorzugt, sonst neuester RUNNING/FAILED)
        where = f"""run_id = (
            SELECT run_id FROM (
                SELECT run_id FROM backup_runs
                WHERE snapshot_name = '{_sql_escape(snapshot)}'
                ORDER BY
                  CASE status WHEN 'SUCCESS' THEN 0 WHEN 'RUNNING' THEN 1 ELSE 2 END,
                  started_at DESC
                LIMIT 1
            ) _r
        )"""
    sql = f"""
        UPDATE backup_runs SET
            integrity_status = '{st}',
            integrity_issues_count = {int(issues)},
            integrity_checked_at = NOW(),
            integrity_report_path = {rp}
        WHERE {where};
    """
    rc, _, err = _mysql(env, sql)
    if rc != 0:
        print(f"[warn] backup_runs integrity update failed: {err.strip()}", file=sys.stderr)
    else:
        print(f"[db] backup_runs.integrity_status={st} ({snapshot})")


def _upsert_monthly_audit(
    env: Dict[str, str],
    snapshot: str,
    status: str,
    issues: int,
    report_path: str,
    error_summary: Optional[str],
    duration_sec: float,
) -> None:
    check_id = str(uuid.uuid4())
    st = "OK" if status == "OK" else "FAILED"
    summary_sql = f"'{_sql_escape((error_summary or '')[:2000])}'" if error_summary else "NULL"
    rp = f"'{_sql_escape(report_path)}'" if report_path else "NULL"
    sql = f"""
        INSERT INTO snapshot_integrity_checks (
            check_id, snapshot_name, check_type, status,
            started_at, finished_at, duration_sec,
            issues_count, report_path, error_summary
        ) VALUES (
            '{_sql_escape(check_id)}', '{_sql_escape(snapshot)}', 'monthly_audit', '{st}',
            NOW(), NOW(), {int(round(duration_sec))},
            {int(issues)}, {rp}, {summary_sql}
        )
        ON DUPLICATE KEY UPDATE
            check_id = VALUES(check_id),
            status = VALUES(status),
            started_at = NOW(),
            finished_at = NOW(),
            duration_sec = VALUES(duration_sec),
            issues_count = VALUES(issues_count),
            report_path = VALUES(report_path),
            error_summary = VALUES(error_summary);
    """
    rc, _, err = _mysql(env, sql)
    if rc != 0:
        print(f"[warn] monthly_audit upsert failed: {err.strip()}", file=sys.stderr)
    else:
        print(f"[db] monthly_audit={st} ({snapshot})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-snapshot integrity check + DB tracking")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--pool-root", help="Remote pool root")
    ap.add_argument("--dest-root", help="Deprecated alias for --pool-root")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument(
        "--check-type",
        choices=("post_upload", "monthly_audit", "manual"),
        default="manual",
    )
    ap.add_argument("--backup-run-id", help="backup_runs.run_id (post_upload)")
    ap.add_argument("--stub-sample", type=int, default=0)
    ap.add_argument("--json-out", help="JSON report path")
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    pool_root = args.pool_root or args.dest_root
    if not pool_root:
        print("[FAIL] --pool-root required", file=sys.stderr)
        return 2

    env = _load_env_file(args.env_file)
    for k, v in os.environ.items():
        if k.startswith("PCLOUD_"):
            env.setdefault(k, v)

    use_db = not args.no_db and _db_enabled(env)

    cfg = pc.effective_config(env_file=args.env_file)
    ok, _ = run_integrity_for_snapshot(
        env=env,
        cfg=cfg,
        pool_root=pool_root,
        snapshot=args.snapshot,
        check_type=args.check_type,
        backup_run_id=args.backup_run_id,
        stub_sample=args.stub_sample,
        no_db=not use_db,
        json_out=args.json_out,
        verbose=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
