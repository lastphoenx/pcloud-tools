#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lesender Report: Index-SHAs (live Snapshots) ohne Eintrag im Pool-Listing.

Ordnet fehlende SHAs Snapshots/Relpaths zu (Ops-DB) und optional pCloud-stat pro SHA.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pcloud_bin_lib as pc
import pool_gc_index as gci
import pool_index_db as pidb
import pcloud_pool_gc as pgc


def _load_env(path: str) -> dict:
    return pgc._load_env_file(path)


def _collect_pool_sha_set(cfg: dict, pool_root: str, *, quiet: bool) -> Set[str]:
    out: Set[str] = set()
    for pf in pgc._iter_pool_files_by_prefix(cfg, pool_root, quiet=quiet):
        name = str(pf.get("name") or "").lower()
        if name:
            out.add(name)
    return out


def _live_referenced_shas(
    cfg: dict,
    snaps_root: str,
    env_vars: dict,
    live: Set[str],
) -> List[str]:
    ref_lookup = gci.referenced_shas_for_gc(
        cfg, snaps_root, env_vars, live, log=lambda _m: None,
    )
    try:
        return sorted(
            sha
            for sha in ref_lookup.iter_shas()
        )
    finally:
        ref_lookup.close()


def _refs_for_shas(
    ops_db_path: str,
    live: Set[str],
    shas: List[str],
) -> List[Tuple[str, str, str, Optional[int], Optional[int]]]:
    """Rows: sha, snapshot, relpath, fileid, size."""
    if not shas or not live:
        return []
    uri = f"file:{os.path.abspath(ops_db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        snap_ph = ",".join("?" * len(live))
        sha_ph = ",".join("?" * len(shas))
        sql = f"""
            SELECT s.sha, n.name, r.relpath, s.fileid, s.size
            FROM shas s
            JOIN snap_refs r ON r.sha_id = s.id
            JOIN snapshots n ON n.id = r.snap_id
            WHERE n.name IN ({snap_ph})
              AND s.sha IN ({sha_ph})
            ORDER BY s.sha, n.name, r.relpath
        """
        params: List[Any] = sorted(live) + shas
        return [
            (
                str(row[0]).lower(),
                str(row[1]),
                str(row[2] if row[2] is not None else ""),
                row[3],
                row[4],
            )
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.close()


def run_gap_report(
    cfg: dict,
    env_vars: dict,
    *,
    verify_stat: bool = False,
    quiet_pool: bool = True,
    sha_limit: Optional[int] = None,
) -> Dict[str, Any]:
    dest = (env_vars.get("PCLOUD_DEST") or os.environ.get("PCLOUD_DEST") or "").rstrip("/")
    if not dest:
        raise SystemExit("PCLOUD_DEST fehlt (.env)")
    snaps_root = f"{dest}/_snapshots"
    pool_root = f"{dest}/_pool"
    ops_db = pidb.default_ops_db_path(env_vars)

    t0 = time.time()
    live = pgc._list_remote_snapshot_names(cfg, snaps_root)
    pool_shas = _collect_pool_sha_set(cfg, pool_root, quiet=quiet_pool)
    indexed = _live_referenced_shas(cfg, snaps_root, env_vars, live)
    gap_shas = [s for s in indexed if s not in pool_shas]
    if sha_limit is not None:
        gap_shas = gap_shas[: max(0, sha_limit)]

    refs = _refs_for_shas(ops_db, live, gap_shas)
    stat_by_sha: Dict[str, str] = {}
    if verify_stat:
        for sha in gap_shas:
            path = pc.pool_file_remote_path(pool_root, sha)
            try:
                stat_by_sha[sha] = pc.classify_remote_file_stat(cfg, path=path)
            except Exception as e:
                stat_by_sha[sha] = f"error:{type(e).__name__}"

    rows: List[dict] = []
    if refs:
        for sha, snap, relpath, fileid, size in refs:
            rows.append(
                {
                    "sha256": sha,
                    "snapshot": snap,
                    "relpath": relpath,
                    "fileid": fileid,
                    "index_size": size,
                    "in_pool_listing": "no",
                    "remote_stat": stat_by_sha.get(sha, ""),
                }
            )
    else:
        for sha in gap_shas:
            rows.append(
                {
                    "sha256": sha,
                    "snapshot": "",
                    "relpath": "",
                    "fileid": "",
                    "index_size": "",
                    "in_pool_listing": "no",
                    "remote_stat": stat_by_sha.get(sha, ""),
                }
            )

    present_despite_gap = sum(1 for v in stat_by_sha.values() if v == "present")
    return {
        "live_snapshots": len(live),
        "pool_files_listed": len(pool_shas),
        "index_shas_live": len(indexed),
        "gap_sha_count": len(gap_shas),
        "gap_rows": len(rows),
        "verify_stat": verify_stat,
        "remote_present_despite_listing_gap": present_despite_gap,
        "duration_sec": round(time.time() - t0, 2),
        "ops_db": ops_db,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Pool-Gap-Report (read-only)")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument("--csv", help="CSV-Ausgabedatei (eine Zeile pro snap_ref)")
    ap.add_argument(
        "--verify-stat",
        action="store_true",
        help="pCloud stat pro fehlender SHA (langsam, unterscheidet Listing vs. echt fehlend)",
    )
    ap.add_argument("--verbose-pool", action="store_true", help="Pool-Präfix-Log wie GC")
    ap.add_argument("--limit", type=int, default=0, help="Max. Anzahl Gap-SHAs (0=alle)")
    args = ap.parse_args()

    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env_vars = _load_env(env_path)
    cfg = pc.effective_config(env_file=args.env_file, env_dir=args.env_dir)

    limit = args.limit if args.limit > 0 else None
    report = run_gap_report(
        cfg,
        env_vars,
        verify_stat=args.verify_stat,
        quiet_pool=not args.verbose_pool,
        sha_limit=limit,
    )
    summary = {k: v for k, v in report.items() if k != "rows"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.csv:
        fieldnames = [
            "sha256",
            "snapshot",
            "relpath",
            "fileid",
            "index_size",
            "in_pool_listing",
            "remote_stat",
        ]
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or ".", exist_ok=True)
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(report["rows"])
        print(f"CSV: {args.csv} ({len(report['rows'])} Zeilen)", file=sys.stderr)

    return 2 if report["gap_sha_count"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
