#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pool-Blobs aus lokalem RTB nachladen (SHA256-Verify vor Upload).

Typisch nach missing_blobs_report.py: eindeutige SHAs mit in_local_rtb=yes,
Quelldatei unter --rtb-snapshot (Default: neuester RTB mit PBS-Inhalt).

Beispiel:
  set -a && . ./.env && set +a
  python scripts/utilities/pool_recover_from_rtb.py \\
    --csv /srv/pcloud-archive/missing_blobs_2026-10-10.csv \\
    --rtb-snapshot 2026-08-06-172826 --dry-run
  python scripts/utilities/pool_recover_from_rtb.py ...   # scharf
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
from typing import Dict, List, Tuple

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc
import pcloud_pool_gc as pgc
from pcloud_push_json_pool_manifest_to_pcloud import (
    _get_pool_path,
    _pool_object_present,
    _upload_file_smart,
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _load_tasks(
    csv_path: str,
    *,
    rtb_snapshot: str,
) -> Tuple[Dict[str, str], int]:
    """sha -> relpath (eine Quelle pro SHA)."""
    by_sha: Dict[str, Tuple[str, str]] = {}
    skipped = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("in_local_rtb") or "").strip().lower() not in ("yes", "1", "true"):
                skipped += 1
                continue
            sha = (row.get("sha256") or "").strip().lower()
            rel = (row.get("relpath") or "").strip().lstrip("/")
            snap = (row.get("snapshot") or "").strip()
            if len(sha) != 64 or not rel:
                skipped += 1
                continue
            prev = by_sha.get(sha)
            if prev is None:
                by_sha[sha] = (rel, snap)
            elif snap == rtb_snapshot and prev[1] != rtb_snapshot:
                by_sha[sha] = (rel, snap)
    return {s: t[0] for s, t in by_sha.items()}, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="missing_blobs_report CSV")
    ap.add_argument("--rtb-snapshot", default="2026-08-06-172826")
    ap.add_argument("--rtb", default=None, help="RTB root (default: RTB aus .env)")
    ap.add_argument("--dest-root", default=None, help="default: PCLOUD_DEST aus .env")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max SHAs (Test)")
    args = ap.parse_args()

    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env = pgc._load_env_file(env_path)
    dest_root = (args.dest_root or env.get("PCLOUD_DEST") or "").strip()
    if not dest_root:
        print("PCLOUD_DEST fehlt (--dest-root oder .env)", file=sys.stderr)
        return 2
    rtb_root = (args.rtb or env.get("RTB") or os.environ.get("RTB", "/mnt/backup/rtb_nas")).rstrip("/")
    pool_root = f"{dest_root.rstrip('/')}/_pool"

    cfg = pc.effective_config(env_file=env_path)
    try:
        pc.preflight_or_raise(cfg)
    except Exception as e:
        print(f"[preflight][FAIL] {e}", file=sys.stderr)
        return 12

    tasks, skipped_rows = _load_tasks(args.csv, rtb_snapshot=args.rtb_snapshot)
    shas = sorted(tasks.keys())
    if args.limit and args.limit > 0:
        shas = shas[: args.limit]

    stats = {
        "unique_shas": len(tasks),
        "process": len(shas),
        "skipped_csv_rows": skipped_rows,
        "already_in_pool": 0,
        "would_upload": 0,
        "uploaded": 0,
        "hash_mismatch": 0,
        "missing_local": 0,
        "errors": 0,
    }
    mismatches: List[str] = []

    print(
        f"[recover] SHAs={len(shas)} (von {stats['unique_shas']} eindeutig), "
        f"rtb={rtb_root}/{args.rtb_snapshot}, dry_run={args.dry_run}",
        flush=True,
    )

    for i, sha in enumerate(shas, 1):
        rel = tasks[sha]
        local = os.path.join(rtb_root, args.rtb_snapshot, rel)
        if _pool_object_present(cfg, pool_root, sha, {}):
            stats["already_in_pool"] += 1
            continue
        if not os.path.isfile(local):
            stats["missing_local"] += 1
            if stats["missing_local"] <= 5:
                print(f"[missing] {sha[:16]}... {rel}", file=sys.stderr)
            continue
        try:
            digest = _sha256_file(local)
        except OSError as e:
            stats["errors"] += 1
            print(f"[error] read {rel}: {e}", file=sys.stderr)
            continue
        if digest != sha:
            stats["hash_mismatch"] += 1
            mismatches.append(f"{sha} {rel} (file={digest[:16]}...)")
            continue
        pool_path_abs = f"{dest_root.rstrip('/')}/{_get_pool_path(sha)}"
        parent_abs = os.path.dirname(pool_path_abs.rstrip("/"))
        if parent_abs:
            pc.ensure_path_cached(cfg, parent_abs)
        if args.dry_run:
            stats["would_upload"] += 1
            if i % 500 == 0 or i == len(shas):
                print(f"[dry] {i}/{len(shas)} would_upload={stats['would_upload']}", flush=True)
            continue
        try:
            _upload_file_smart(cfg, local, pool_path_abs, dry=False)
            if _pool_object_present(cfg, pool_root, sha, {}):
                stats["uploaded"] += 1
            else:
                stats["errors"] += 1
                print(f"[error] upload ohne Pool-Treffer: {rel}", file=sys.stderr)
        except Exception as e:
            stats["errors"] += 1
            print(f"[error] upload {rel}: {e}", file=sys.stderr)
        if i % 100 == 0 or i == len(shas):
            print(
                f"[progress] {i}/{len(shas)} uploaded={stats['uploaded']} "
                f"present={stats['already_in_pool']} mismatch={stats['hash_mismatch']}",
                flush=True,
            )

    print(f"[recover] done: {stats}", flush=True)
    if mismatches:
        print("[recover] hash_mismatch (max 10):", file=sys.stderr)
        for line in mismatches[:10]:
            print(f"  {line}", file=sys.stderr)
    if stats["hash_mismatch"]:
        return 3
    if stats["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
