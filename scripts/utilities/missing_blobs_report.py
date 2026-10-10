#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
missing_blobs_report.py — nur lesend, RAM-sparsam (ijson).

Liest pool_integrity_run-Reports (--json-out chk_<snap>.json) und zeigt,
welche Manifest-Dateien eines Snapshots im Pool fehlen, wie gross sie sind und
ob sie im lokalen RTB-Snapshot noch liegen (Pool-Nachladen moeglich).

Beispiel:
  python scripts/utilities/missing_blobs_report.py \\
    2026-07-26-120040 2026-07-31-040049 2026-08-06-172826 \\
    --reports-dir /tmp --csv /tmp/missing_blobs.csv
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import sys
from typing import Dict, Iterable, List, Set, Tuple

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)


def missing_shas_from_report(path: str, snap: str) -> Set[str]:
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)
    per = (rep.get("manifest_vs_pool") or {}).get("per_snapshot") or {}
    block = per.get(snap) or {}
    shas = block.get("missing_shas") or []
    if shas:
        return {str(s).lower() for s in shas}
    if int(block.get("missing_count") or 0) <= 0:
        return set()
    return {str(s).lower() for s in (block.get("missing_from_pool") or [])}


def scan_manifest(manifest_path: str, wanted: Set[str]) -> Iterable[Tuple[str, str, int]]:
    import ijson  # type: ignore

    with open(manifest_path, "rb") as f:
        for it in ijson.items(f, "items.item"):
            if not isinstance(it, dict) or it.get("type") != "file":
                continue
            sha = str(it.get("sha256") or "").lower()
            if sha not in wanted:
                continue
            rel = str(it.get("relpath") or it.get("path") or "").strip().lstrip("/")
            if not rel:
                continue
            yield sha, rel, int(it.get("size") or 0)


def analyze_snapshot(
    snap: str,
    *,
    reports_dir: str,
    manifests_dir: str,
    rtb_root: str,
) -> Tuple[List[Tuple[str, str, str, int, bool]], Dict[str, int]]:
    rep_path = os.path.join(reports_dir, f"chk_{snap}.json")
    if not os.path.isfile(rep_path):
        raise FileNotFoundError(f"Report fehlt: {rep_path}")
    missing = missing_shas_from_report(rep_path, snap)
    mpath = os.path.join(manifests_dir, f"{snap}.json")
    if not os.path.isfile(mpath):
        raise FileNotFoundError(f"Manifest fehlt: {mpath}")

    files = list(scan_manifest(mpath, missing))
    mapped_shas = {s for s, _r, _z in files}
    stats = {
        "missing_shas": len(missing),
        "manifest_files": len(files),
        "shas_without_manifest_path": len(missing - mapped_shas),
        "total_bytes": sum(z for _s, _r, z in files),
        "rtb_present": 0,
    }
    rows: List[Tuple[str, str, str, int, bool]] = []
    for sha, rel, size in files:
        rtb_file = os.path.join(rtb_root, snap, rel)
        ok = os.path.isfile(rtb_file)
        if ok:
            stats["rtb_present"] += 1
        rows.append((snap, sha, rel, size, ok))
    return rows, stats


def _print_snapshot_summary(
    snap: str,
    stats: Dict[str, int],
    rows: List[Tuple[str, str, str, int, bool]],
    rtb_root: str,
) -> None:
    by_top = collections.Counter(
        rel.split("/", 1)[0] for _snap, _sha, rel, _sz, _ok in rows
    )
    by_ext = collections.Counter(
        os.path.splitext(rel)[1].lower() or "(keine)"
        for _snap, _sha, rel, _sz, _ok in rows
    )
    print(f"\n== {snap}: {stats['missing_shas']} SHAs fehlen im Pool, "
          f"{stats['manifest_files']} Manifest-Dateien, {stats['total_bytes']} Bytes")
    if stats["shas_without_manifest_path"]:
        print(f"  [warn] {stats['shas_without_manifest_path']} SHA(s) ohne Pfad im Manifest-Stream")
    print("  nach Ordner :", by_top.most_common(8))
    print("  nach Endung :", by_ext.most_common(8))
    n = stats["manifest_files"]
    print(f"  davon im lokalen RTB vorhanden: {stats['rtb_present']}/{n}  ({rtb_root}/{snap}/...)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "snapshots",
        nargs="*",
        help="Snapshot-Namen (leer = aus chk_*.json in --reports-dir)",
    )
    ap.add_argument("--reports-dir", default="/tmp", help="Verzeichnis mit chk_<snap>.json")
    ap.add_argument("--manifests", default=None, help="Default: $PCLOUD_ARCHIVE_DIR/manifests")
    ap.add_argument("--rtb", default=None, help="Default: $RTB oder /mnt/backup/rtb_nas")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument("--csv", default="/tmp/missing_blobs.csv")
    args = ap.parse_args()

    import pcloud_pool_gc as pgc

    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env = pgc._load_env_file(env_path)
    archive = env.get("PCLOUD_ARCHIVE_DIR") or os.environ.get(
        "PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive",
    )
    manifests_dir = args.manifests or os.path.join(archive, "manifests")
    rtb_root = (args.rtb or env.get("RTB") or os.environ.get("RTB", "/mnt/backup/rtb_nas")).rstrip("/")

    snaps: List[str] = list(args.snapshots)
    if not snaps:
        for p in sorted(glob.glob(os.path.join(args.reports_dir, "chk_*.json"))):
            base = os.path.basename(p)
            snaps.append(base[4:-5])

    if not snaps:
        print("Keine Snapshots (Argumente oder chk_*.json).", file=sys.stderr)
        return 2

    all_rows: List[Tuple[str, str, str, int, bool]] = []
    totals = {"snapshots": 0, "files": 0, "rtb_present": 0, "bytes": 0}

    for snap in snaps:
        try:
            rows, stats = analyze_snapshot(
                snap,
                reports_dir=args.reports_dir,
                manifests_dir=manifests_dir,
                rtb_root=rtb_root,
            )
        except FileNotFoundError as e:
            print(f"[skip] {snap}: {e}", file=sys.stderr)
            continue
        if stats["missing_shas"] == 0:
            print(f"\n== {snap}: keine Pool-Luecken (Manifest vs Pool OK)")
            continue
        totals["snapshots"] += 1
        totals["files"] += stats["manifest_files"]
        totals["rtb_present"] += stats["rtb_present"]
        totals["bytes"] += stats["total_bytes"]
        _print_snapshot_summary(snap, stats, rows, rtb_root)
        all_rows.extend(rows)

    print(
        f"\n== gesamt: {totals['snapshots']} Snapshot(s), "
        f"{totals['files']} Dateien, {totals['bytes']} Bytes, "
        f"RTB {totals['rtb_present']}/{totals['files']}"
    )

    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["snapshot", "sha256", "relpath", "size", "in_local_rtb"])
        for snap, sha, rel, size, ok in all_rows:
            w.writerow([snap, sha, rel, size, "yes" if ok else "no"])
    print(f"Details: {args.csv} ({len(all_rows)} Zeilen)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
