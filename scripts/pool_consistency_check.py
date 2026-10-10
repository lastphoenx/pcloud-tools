#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lesende Konsistenzprüfung vor GC / nach Backup.

Exit: 0 OK, 1 Warnung, 2 kritisch.
JSONL (eine Zeile pro Lauf): PCLOUD_POOL_CONSISTENCY_JSONL (Default /var/log/backup/pool_consistency.jsonl)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Optional, Set

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc  # noqa: E402
import pool_consistency_lib as pcl  # noqa: E402
import pool_gc_manifest_guard as mg  # noqa: E402
import pool_index_db as pidb  # noqa: E402
import pcloud_pool_gc as pgc  # noqa: E402


def _read_ops_pending(ops_db: str) -> Dict[str, Any]:
    if not os.path.isfile(ops_db):
        return {"upload_pending": False, "reapply_purge": ""}
    uri = f"file:{os.path.abspath(ops_db)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        return {
            "upload_pending": rows.get("upload_pending") == "1",
            "reapply_purge": (rows.get("reapply_purge_after_sync") or "").strip(),
        }
    finally:
        conn.close()


def _manifest_snapshot_names(manifests_dir: str) -> Set[str]:
    if not os.path.isdir(manifests_dir):
        return set()
    return {
        f[:-5]
        for f in os.listdir(manifests_dir)
        if f.endswith(".json") and f[:-5]
    }


def _last_live_count(jsonl_path: str) -> Optional[int]:
    if not os.path.isfile(jsonl_path):
        return None
    last: Optional[int] = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if "live_count" in rec:
                        last = int(rec["live_count"])
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
    except OSError:
        return None
    return last


def run_check(
    cfg: dict,
    env_vars: dict,
    *,
    manifests_dir: str,
    pool_check: bool = False,
    jsonl_path: str,
) -> Dict[str, Any]:
    dest = (env_vars.get("PCLOUD_DEST") or os.environ.get("PCLOUD_DEST") or "").rstrip("/")
    if not dest:
        raise SystemExit("PCLOUD_DEST fehlt (.env)")
    snaps_root = f"{dest}/_snapshots"
    pool_root = f"{dest}/_pool"

    live = pgc._list_remote_snapshot_names(cfg, snaps_root)
    ops_db = pidb.default_ops_db_path(env_vars)
    backup_db = env_vars.get("PCLOUD_POOL_INDEX_DB_PATH") or pidb.default_db_path()
    ops_names = mg.snapshot_names_in_ops_db(ops_db) if os.path.isfile(ops_db) else set()
    _missing_ops_row, zero_ops_refs = (
        mg.classify_live_ops_coverage(ops_db, live)
        if os.path.isfile(ops_db)
        else (sorted(live), [])
    )
    backup_names = (
        mg.snapshot_names_in_ops_db(backup_db) if os.path.isfile(backup_db) else set()
    )
    man_names = _manifest_snapshot_names(manifests_dir)
    pending = _read_ops_pending(ops_db)
    last_live = _last_live_count(jsonl_path)

    pool_gaps: Optional[int] = None
    if pool_check:
        import pool_gc_index as gci  # noqa: E402

        ref_lookup = gci.referenced_shas_for_gc(
            cfg, snaps_root, env_vars, live, log=lambda _m: None,
        )
        try:
            pool_shas = set()
            for pf in pgc._iter_pool_files_by_prefix(cfg, pool_root, quiet=True):
                pool_shas.add(str(pf.get("name", "")).lower())
            missing = 0
            for sha in ref_lookup.iter_shas():
                if sha not in pool_shas:
                    missing += 1
            pool_gaps = missing
        finally:
            ref_lookup.close()

    verdict = pcl.evaluate_pool_consistency(
        live=live,
        ops_db_names=ops_names,
        backup_db_names=backup_names,
        manifest_names=man_names,
        last_live_count=last_live,
        upload_pending=pending["upload_pending"],
        reapply_purge=pending["reapply_purge"],
        pool_gaps=pool_gaps,
        live_ops_zero_refs=zero_ops_refs,
    )
    verdict["live_zero_ops_refs"] = zero_ops_refs
    verdict["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    verdict["ops_db"] = ops_db
    verdict["backup_db"] = backup_db
    verdict["manifests_dir"] = manifests_dir
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description="Pool/Snapshot Konsistenz (read-only)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument(
        "--manifests-dir",
        default=None,
        help="Default: $PCLOUD_ARCHIVE_DIR/manifests",
    )
    ap.add_argument(
        "--pool",
        action="store_true",
        help="Zähle Index-SHAs ohne Pool-Datei (langsam, API)",
    )
    ap.add_argument(
        "--jsonl",
        default=os.environ.get(
            "PCLOUD_POOL_CONSISTENCY_JSONL",
            "/var/log/backup/pool_consistency.jsonl",
        ),
    )
    ap.add_argument("--no-jsonl", action="store_true")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file, env_dir=args.env_dir)
    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env_vars = pgc._load_env_file(env_path)
    archive = env_vars.get("PCLOUD_ARCHIVE_DIR") or os.environ.get(
        "PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive",
    )
    manifests_dir = args.manifests_dir or os.path.join(archive, "manifests")

    result = run_check(
        cfg,
        env_vars,
        manifests_dir=manifests_dir,
        pool_check=args.pool,
        jsonl_path=args.jsonl,
    )

    line = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    print(line)
    if not args.no_jsonl:
        try:
            os.makedirs(os.path.dirname(args.jsonl) or ".", exist_ok=True)
            with open(args.jsonl, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            print(f"[warn] JSONL nicht schreibbar: {e}", file=sys.stderr)

    level = int(result.get("level", pcl.LEVEL_CRITICAL))
    if result.get("critical_reasons"):
        for r in result["critical_reasons"]:
            print(f"[CRITICAL] {r}", file=sys.stderr)
        if result.get("live_missing_ops_db"):
            print(
                "  live∉ops: " + ", ".join(result["live_missing_ops_db"][:12]),
                file=sys.stderr,
            )
    if result.get("warn_reasons"):
        for r in result["warn_reasons"]:
            print(f"[WARN] {r}", file=sys.stderr)

    return level


if __name__ == "__main__":
    sys.exit(main())
