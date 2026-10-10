#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fehlende Pool-Blobs aus pool_integrity_run-JSON (manifest_vs_pool).

Liest missing_shas pro Snapshot, mappt per ijson Manifest → relpath/size,
prüft ob Datei noch unter <rtb-root>/<snapshot>/<relpath> liegt (Nachladen möglich?).
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import sys
from typing import Any, Dict, List, Set, Tuple

MAIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_pool_gc as pgc  # noqa: E402


def _load_env(path: str) -> dict:
    return pgc._load_env_file(path)


def _snapshot_from_report(data: dict, path: str) -> str:
    if data.get("snapshot"):
        return str(data["snapshot"])
    base = os.path.basename(path)
    if base.startswith("chk_") and base.endswith(".json"):
        return base[4:-5]
    return ""


def _missing_shas_from_report(data: dict, snapshot: str) -> List[str]:
    mvp = data.get("manifest_vs_pool") or {}
    per = (mvp.get("per_snapshot") or {}).get(snapshot) or {}
    shas = list(per.get("missing_shas") or [])
    if shas:
        return [str(s).lower() for s in shas]
    cnt = int(per.get("missing_count") or 0)
    if cnt <= 0:
        return []
    partial = per.get("missing_from_pool") or []
    return [str(s).lower() for s in partial]


def _stream_manifest_meta(
    manifest_path: str,
    want: Set[str],
) -> Dict[str, List[Tuple[str, int]]]:
    """sha -> [(relpath, size), ...]"""
    import ijson  # type: ignore

    out: Dict[str, List[Tuple[str, int]]] = {}
    with open(manifest_path, "rb") as f:
        for it in ijson.items(f, "items.item"):
            if not isinstance(it, dict) or it.get("type") != "file":
                continue
            sha = str(it.get("sha256") or "").lower()
            if sha not in want:
                continue
            rel = str(it.get("path") or it.get("relpath") or "").lstrip("/")
            size = int(it.get("size") or 0)
            out.setdefault(sha, []).append((rel, size))
    return out


def build_rows(
    report_paths: List[str],
    manifests_dir: str,
    rtb_root: str,
) -> Tuple[List[dict], dict]:
    rows: List[dict] = []
    summary: Dict[str, Any] = {
        "reports": len(report_paths),
        "snapshots_with_gaps": 0,
        "blob_rows": 0,
        "rtb_present": 0,
        "appledouble": 0,
        "total_bytes": 0,
    }

    for rpath in report_paths:
        with open(rpath, encoding="utf-8") as f:
            data = json.load(f)
        snap = _snapshot_from_report(data, rpath)
        if not snap:
            continue
        shas = _missing_shas_from_report(data, snap)
        if not shas:
            continue
        summary["snapshots_with_gaps"] += 1
        want = set(shas)
        mpath = os.path.join(manifests_dir, f"{snap}.json")
        meta: Dict[str, List[Tuple[str, int]]] = {}
        if os.path.isfile(mpath):
            meta = _stream_manifest_meta(mpath, want)
        for sha in shas:
            paths = meta.get(sha) or [("", 0)]
            for relpath, size in paths:
                rtb_file = (
                    os.path.join(rtb_root, snap, relpath)
                    if relpath
                    else ""
                )
                rtb_ok = bool(rtb_file and os.path.isfile(rtb_file))
                if rtb_ok:
                    summary["rtb_present"] += 1
                base = os.path.basename(relpath)
                if base.startswith("._"):
                    summary["appledouble"] += 1
                summary["total_bytes"] += size
                rows.append(
                    {
                        "snapshot": snap,
                        "sha256": sha,
                        "relpath": relpath,
                        "size": size,
                        "rtb_path": rtb_file,
                        "rtb_present": "yes" if rtb_ok else "no",
                        "report_json": rpath,
                    }
                )
    summary["blob_rows"] = len(rows)
    return rows, summary


def _print_rollup(rows: List[dict]) -> None:
    by_snap = collections.Counter(r["snapshot"] for r in rows)
    ext = collections.Counter(
        os.path.splitext(r["relpath"])[1].lower() or "(keine)" for r in rows
    )
    top_dirs = collections.Counter()
    for r in rows:
        parts = (r["relpath"] or "").split("/")
        top_dirs[parts[0] if parts and parts[0] else "(root)"] += 1
    print("pro Snapshot:", dict(by_snap.most_common()))
    print("Top-Endungen:", ext.most_common(10))
    print("Top-Ordner:", top_dirs.most_common(10))
    rtb_yes = sum(1 for r in rows if r["rtb_present"] == "yes")
    print(f"RTB noch vorhanden: {rtb_yes}/{len(rows)} Zeilen")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fehlende Pool-Blobs aus Integrity-JSON")
    ap.add_argument(
        "--reports",
        nargs="+",
        default=[],
        help="pool_integrity_run JSON (Glob ok)",
    )
    ap.add_argument("--glob", default="/tmp/chk_*.json", help="wenn --reports leer")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--env-dir", default=".")
    ap.add_argument("--manifests-dir", default=None)
    ap.add_argument("--rtb-root", default=None)
    ap.add_argument("--csv", help="CSV-Ausgabe")
    args = ap.parse_args()

    paths: List[str] = []
    for p in args.reports:
        paths.extend(glob.glob(p))
    if not paths:
        paths = sorted(glob.glob(args.glob))
    if not paths:
        print("Keine Report-JSONs gefunden.", file=sys.stderr)
        return 2

    env_path = args.env_file
    if not os.path.isabs(env_path):
        env_path = os.path.join(args.env_dir, env_path)
    env = _load_env(env_path)
    archive = env.get("PCLOUD_ARCHIVE_DIR") or os.environ.get(
        "PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive",
    )
    manifests_dir = args.manifests_dir or os.path.join(archive, "manifests")
    rtb_root = (
        args.rtb_root
        or env.get("RTB")
        or os.environ.get("RTB", "/mnt/backup/rtb_nas")
    ).rstrip("/")

    rows, summary = build_rows(paths, manifests_dir, rtb_root)
    print(json.dumps({**summary, "rtb_root": rtb_root}, indent=2))
    _print_rollup(rows)

    if args.csv:
        fields = [
            "snapshot", "sha256", "relpath", "size",
            "rtb_present", "rtb_path", "report_json",
        ]
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV: {args.csv} ({len(rows)} Zeilen)", file=sys.stderr)

    return 0 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
