#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Remote-Groessen auf pCloud (Pool-Mode).

pCloud hat KEINEN API-Endpoint „Ordnergroesse“. Optionen:

  1. userinfo          — Kontingent gesamt (1 REST-Call)
  2. content_index.json — logische Snapshot-Groesse aus pool_refs (1 Download)
  3. listfolder(recursive) — physische Bytes durch Summe aller file.size

Pool-Mode: Unter _snapshots/<name>/ liegen nur Stubs (.meta.json), nicht die
Dateidaten. Die liegen dedupliziert in _pool/. Ein Snapshot „wie auf der NAS“
ist daher die LOGISCHE Groesse (~150 GB), nicht die Stub-Ordnergroesse.

Beispiele (pi-nas):
  python scripts/utilities/remote_pool_sizing.py --env-file .env
  python scripts/utilities/remote_pool_sizing.py --env-file .env \\
      --snap 2026-06-24-040035 --snap 2026-06-24-120041
  python scripts/utilities/remote_pool_sizing.py --env-file .env --walk-pool --walk-snapshots
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Set, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)
import pcloud_bin_lib as pc  # noqa: E402


def _fmt_gb(n: int) -> str:
    if n <= 0:
        return "0.00"
    return f"{n / (1024 ** 3):.2f}"


def _fmt_n(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _walk_files(node: dict) -> Iterator[dict]:
    for c in node.get("contents", []) or []:
        if c.get("isfolder"):
            yield from _walk_files(c)
        else:
            yield c


def _sum_tree(node: dict) -> Tuple[int, int, int]:
    """bytes, files, dirs unterhalb node (inkl. Kinder)."""
    files_b = 0
    n_files = 0
    n_dirs = 0
    if node.get("isfolder"):
        n_dirs += 1
    for c in node.get("contents", []) or []:
        if c.get("isfolder"):
            cb, cf, cd = _sum_tree(c)
            files_b += cb
            n_files += cf
            n_dirs += cd
        else:
            n_files += 1
            files_b += int(c.get("size") or 0)
    return files_b, n_files, n_dirs


def _load_index(cfg: dict, idx_path: str) -> dict:
    txt = pc.get_textfile(cfg, path=idx_path, maxbytes=None)
    return json.loads(txt)


def _snapshot_logical_bytes(pool_refs: dict, snap: str) -> Tuple[int, int, int]:
    """(logical_bytes, file_entries, unique_shas) fuer einen Snapshot."""
    logical = 0
    entries = 0
    shas: Set[str] = set()
    for sha, entry in pool_refs.items():
        if not isinstance(entry, dict):
            continue
        snaps = entry.get("snapshots") or {}
        if not isinstance(snaps, dict):
            continue
        paths = snaps.get(snap)
        if not paths:
            continue
        if not isinstance(paths, list):
            paths = [paths]
        size = int(entry.get("size") or 0)
        logical += size * len(paths)
        entries += len(paths)
        shas.add(sha)
    return logical, entries, len(shas)


def _list_remote_snaps(cfg: dict, snaps_root: str) -> List[str]:
    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
    md = top.get("metadata") or {}
    names = []
    for c in md.get("contents", []) or []:
        if c.get("isfolder") and c.get("name") not in ("_index",):
            names.append(c["name"])
    return sorted(names)


def _userinfo_quota(cfg: dict) -> Tuple[int, int]:
    top = pc._rest_get(cfg, "userinfo", {"getauth": 1})
    info = (top.get("userinfo") or {})
    return int(info.get("usedquota") or 0), int(info.get("quota") or 0)


def _walk_remote(cfg: dict, path: str, label: str) -> Tuple[int, int, int, float]:
    print(f"[walk] {label}: listfolder(recursive) {path} ...", flush=True)
    t0 = time.time()
    top = pc.call_with_backoff(
        pc.listfolder, cfg, path=path, recursive=True, nofiles=False, showpath=False,
    ) or {}
    md = top.get("metadata") or {}
    nbytes, n_files, n_dirs = _sum_tree(md)
    dt = time.time() - t0
    print(
        f"       → {_fmt_gb(nbytes)} GB  ({_fmt_n(n_files)} Dateien, "
        f"{_fmt_n(n_dirs)} Ordner) in {dt:.1f}s",
        flush=True,
    )
    return nbytes, n_files, n_dirs, dt


def main() -> int:
    ap = argparse.ArgumentParser(description="pCloud Remote-Groessen (Pool-Mode)")
    ap.add_argument("--env-file", default=f"{MAIN_DIR}/.env")
    ap.add_argument("--dest-root", help="default: PCLOUD_DEST aus .env")
    ap.add_argument("--snap", action="append", help="Snapshot(s); default: letzte 2 remote")
    ap.add_argument("--walk-pool", action="store_true", help="Physisches _pool/ per API summieren")
    ap.add_argument("--walk-snapshots", action="store_true", help="Gesamtes _snapshots/ summieren (Stubs)")
    ap.add_argument("--walk-root", action="store_true", help="Gesamtes dest-root (langsam)")
    ap.add_argument("--no-quota", action="store_true", help="userinfo ueberspringen")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(
        args.dest_root or os.environ.get("PCLOUD_DEST", "/Backup/rtb_pool"),
    ).rstrip("/")
    pool_root = f"{dest}/_pool"
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

    print(f"dest-root: {dest}")
    print()

    if not args.no_quota:
        try:
            used, quota = _userinfo_quota(cfg)
            pct = f"{100.0 * used / quota:.1f}%" if quota else "—"
            print(f"Konto (userinfo):  {_fmt_gb(used)} / {_fmt_gb(quota)} GB genutzt ({pct})")
            print("  → gesamtes pCloud-Konto, nicht nur rtb_pool")
            print()

    print(f"Lade {idx_path} ...", flush=True)
    t0 = time.time()
    try:
        index = _load_index(cfg, idx_path)
    except Exception as e:
        print(f"FEHLER content_index: {e}", file=sys.stderr)
        return 1
    pool_refs = index.get("pool_refs") or {}
    print(
        f"  → {len(pool_refs)} pool_refs in {time.time() - t0:.1f}s "
        f"(Index v{index.get('version', '?')})",
        flush=True,
    )
    print()

    if args.snap:
        snaps = args.snap
    else:
        remote = _list_remote_snaps(cfg, snaps_root)
        snaps = remote[-2:] if len(remote) >= 2 else remote
        print(f"Remote-Snapshots: {len(remote)} (zeige letzte {len(snaps)})")
        print()

    if not snaps:
        print("Keine Snapshots.")
        return 1

    print(f"{'SNAPSHOT':<22} {'LOGICAL_GB':>11} {'FILES':>10} {'UNIQ_SHA':>10}  (aus Index)")
    print("-" * 58)
    for snap in snaps:
        logical, entries, uniq = _snapshot_logical_bytes(pool_refs, snap)
        print(
            f"{snap:<22} {_fmt_gb(logical):>11} {_fmt_n(entries):>10} "
            f"{_fmt_n(uniq):>10}",
        )
    print()
    print("Hinweis: LOGICAL = wiederherstellbare Dateigroesse (Hardlinks zaehlen mehrfach).")
    print("         Remote-Ordner _snapshots/<name>/ ist nur Stubs (typisch wenige MB).")
    print()

    if args.walk_pool or args.walk_snapshots or args.walk_root:
        print("--- Physische Groessen (API listfolder recursive) ---")
        if args.walk_root:
            _walk_remote(cfg, dest, "dest-root gesamt")
        else:
            if args.walk_pool:
                _walk_remote(cfg, pool_root, "_pool (deduplizierte Bloecke)")
            if args.walk_snapshots:
                _walk_remote(cfg, snaps_root, "_snapshots (alle Stubs)")
        print()
        print("Physisches rtb_pool ≈ _pool + _snapshots (+ manifests/index falls mitgezaehlt).")
        print("Zwei Snapshots teilen sich _pool-Objekte — nicht 2× LOGICAL summieren!")

    elif not (args.walk_pool or args.walk_snapshots or args.walk_root):
        print("Optional (langsamer, echte Bytes auf pCloud):")
        print(f"  --walk-pool          summiert {pool_root}")
        print(f"  --walk-snapshots     summiert {snaps_root}")
        print(f"  --walk-root          summiert ganz {dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
