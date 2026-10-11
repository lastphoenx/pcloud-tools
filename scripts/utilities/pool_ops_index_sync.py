#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ops-Index (pool_index_gc.sqlite3) an Remote content_index_master.json anbinden.

Nur Download + Streaming-Import — kein Pool-GC, kein Orphan-Scan.
Gleicher Kern wie pcloud_pool_gc.py Phase 1 (open_synced_db).

Exit: 0 OK, 2 Fehler (ijson fehlt, API, …).
"""
from __future__ import annotations

import argparse
import os
import sys

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc  # noqa: E402
import pool_gc_index as gci  # noqa: E402
import pcloud_pool_gc as pgc  # noqa: E402
import pool_index_db as pidb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ops-SQLite aus Remote-Master synchronisieren (ohne GC)",
    )
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument(
        "--repair-writes",
        action="store_true",
        help="upload_pending / reapply_purge ausführen (wie scharfer GC)",
    )
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file, env_dir=args.env_dir)
    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env_vars = pgc._load_env_file(env_path)
    dest = (env_vars.get("PCLOUD_DEST") or "").rstrip("/")
    if not dest:
        print("PCLOUD_DEST fehlt (.env)", file=sys.stderr)
        return 2
    snaps_root = f"{dest}/_snapshots"
    ops_path = pidb.default_ops_db_path(env_vars)

    print(f"=== pool_ops_index_sync ===\nOps-DB: {ops_path}")

    try:
        db = gci.open_synced_db(
            cfg,
            snaps_root,
            env_vars,
            log=print,
            repair_writes=bool(args.repair_writes),
        )
        n_snaps = len(db.snapshot_names())
        n_shas = db.count_shas()
        db.close()
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    print(f"Fertig: {n_snaps} snapshots, {n_shas} shas in Ops-DB")
    print("Nächster Schritt: scripts/pool_consistency_check.py --env-file .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
