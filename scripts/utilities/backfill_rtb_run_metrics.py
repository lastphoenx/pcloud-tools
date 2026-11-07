#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill backup_runs RTB/PBS columns from snapshot .rtb_backup_summary.json files."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def load_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
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


def mysql_exec(env: dict[str, str], sql: str) -> None:
    pw = env.get("PCLOUD_DB_PASS", "")
    if not pw:
        sys.exit("PCLOUD_DB_PASS missing in env")
    cmd = [
        "mysql",
        "-h", env.get("PCLOUD_DB_HOST", "localhost"),
        "-P", env.get("PCLOUD_DB_PORT", "3306"),
        "-u", env.get("PCLOUD_DB_USER", "pcloud_backup"),
        f"-D{env.get('PCLOUD_DB_NAME', 'pcloud_backup')}",
        "-e", sql,
    ]
    subprocess.run(cmd, env={**os.environ, "MYSQL_PWD": pw}, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--rtb-root",
        default=os.environ.get("RTB_SNAPSHOT_DIR", "/mnt/backup/rtb_nas"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    env = load_env(args.env_file)
    root = Path(args.rtb_root)

    rows = subprocess.run(
        [
            "mysql",
            "-h", env.get("PCLOUD_DB_HOST", "localhost"),
            "-P", env.get("PCLOUD_DB_PORT", "3306"),
            "-u", env.get("PCLOUD_DB_USER", "pcloud_backup"),
            f"-D{env.get('PCLOUD_DB_NAME', 'pcloud_backup')}",
            "-N", "-B",
            "-e",
            "SELECT snapshot_name, run_id FROM backup_runs "
            "WHERE status='SUCCESS' AND rtb_bytes_transferred=0 "
            "ORDER BY started_at DESC LIMIT 200;",
        ],
        env={**os.environ, "MYSQL_PWD": env.get("PCLOUD_DB_PASS", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    if rows.returncode != 0:
        print(rows.stderr, file=sys.stderr)
        return 1

    updated = 0
    for line in rows.stdout.splitlines():
        if not line.strip():
            continue
        snap, run_id = line.split("\t", 1)
        summary = root / snap / ".rtb_backup_summary.json"
        if not summary.is_file():
            continue
        with summary.open(encoding="utf-8") as f:
            s = json.load(f)
        rtb_b = int(s.get("bytes_transferred", 0) or 0)
        rtb_f = int(s.get("files_transferred", 0) or 0)
        rtb_u = int(s.get("units_changed", 0) or 0)
        pbs_b = int(s.get("pbs_bytes_transferred", 0) or 0)
        sql = (
            f"UPDATE backup_runs SET rtb_bytes_transferred={rtb_b}, "
            f"rtb_files_transferred={rtb_f}, rtb_units_changed={rtb_u}, "
            f"pbs_bytes_transferred={pbs_b} WHERE run_id='{run_id}';"
        )
        if args.dry_run:
            print(f"DRY {snap}: rtb={rtb_b} pbs={pbs_b}")
        else:
            mysql_exec(env, sql)
            print(f"OK {snap}: rtb={rtb_b} pbs={pbs_b}")
        updated += 1

    print(f"Done. touched={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
