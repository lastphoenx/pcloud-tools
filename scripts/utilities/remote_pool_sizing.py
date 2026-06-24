#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Remote-Groessen auf pCloud (Pool-Mode).

pCloud hat KEINEN API-Endpoint „Ordnergroesse“. Summe file.size aus listfolder.

Pool-Mode:
  _pool/           deduplizierte Bloecke (~150 GB gesamt, geteilt)
  _snapshots/<s>/  nur Stubs (.meta.json), typisch ~100–500 MB pro Snapshot
  content_index    kumulativer Master (waechst, Recovery/GC)

Beispiele (pi-nas):
  python scripts/utilities/remote_pool_sizing.py --env-file .env
  python scripts/utilities/remote_pool_sizing.py --env-file .env --snap 2026-06-24-120041
  python scripts/utilities/remote_pool_sizing.py --env-file .env --walk-pool
  python scripts/utilities/remote_pool_sizing.py --env-file .env --walk-pool --logical
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Set, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)
import pcloud_bin_lib as pc  # noqa: E402


def _fmt_gb(n: int) -> str:
    if n <= 0:
        return "0.00"
    return f"{n / (1024 ** 3):.2f}"


def _fmt_mb(n: int) -> str:
    if n <= 0:
        return "0"
    return f"{n / (1024 ** 2):.0f}"


def _fmt_n(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _sum_tree(node: dict) -> Tuple[int, int, int]:
    """bytes, files, dirs unterhalb node."""
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
    ui = top.get("userinfo") or top
    used = int(ui.get("usedquota") or top.get("usedquota") or 0)
    quota = int(ui.get("quota") or top.get("quota") or 0)
    return used, quota


def _walk_pool_sharded(cfg: dict, pool_root: str) -> Tuple[int, int, float]:
    pool_root = pool_root.rstrip("/")
    print(f"[walk] _pool: summiere file.size in 256 Hex-Shards ...", flush=True)
    t0 = time.time()
    total_b = total_f = 0
    shards = 0
    for i in range(256):
        sub = f"{pool_root}/{i:02x}"
        try:
            top = pc.call_with_backoff(
                pc.listfolder, cfg, path=sub, recursive=True, nofiles=False, showpath=False,
            ) or {}
        except Exception:
            continue
        md = top.get("metadata") or {}
        if not md.get("contents"):
            continue
        shards += 1
        b, f, _ = _sum_tree(md)
        total_b += b
        total_f += f
    dt = time.time() - t0
    print(
        f"       → {_fmt_gb(total_b)} GB ({_fmt_n(total_f)} Pool-Objekte, {shards} Shards) "
        f"in {dt:.1f}s",
        flush=True,
    )
    return total_b, total_f, dt


def _walk_one_snapshot(cfg: dict, snaps_root: str, snap: str) -> Tuple[int, int, float]:
    """Physische Groesse eines Snapshot-Ordners (Stubs + Marker)."""
    path = f"{snaps_root.rstrip('/')}/{snap}"
    t0 = time.time()
    top = pc.call_with_backoff(
        pc.listfolder, cfg, path=path, recursive=True, nofiles=False, showpath=False,
    ) or {}
    b, f, _ = _sum_tree(top.get("metadata") or {})
    return b, f, time.time() - t0


def _walk_snapshots_list(
    cfg: dict, snaps_root: str, snap_names: List[str],
) -> Tuple[int, int, float]:
    """Summiert Stubs pro Snapshot-Ordner (nicht gesamtes _snapshots — vermeidet API 5000)."""
    print(
        f"[walk] Stubs: {len(snap_names)} Snapshot-Ordner einzeln ...",
        flush=True,
    )
    t0 = time.time()
    total_b = total_f = 0
    for snap in snap_names:
        try:
            b, f, _ = _walk_one_snapshot(cfg, snaps_root, snap)
            total_b += b
            total_f += f
        except Exception as e:
            print(f"       ! {snap}: {e}", flush=True)
    dt = time.time() - t0
    print(
        f"       → {_fmt_mb(total_b)} MB Stubs ({_fmt_n(total_f)} Dateien) in {dt:.1f}s",
        flush=True,
    )
    return total_b, total_f, dt


def main() -> int:
    ap = argparse.ArgumentParser(description="pCloud Remote-Groessen (Pool-Mode)")
    ap.add_argument("--env-file", default=f"{MAIN_DIR}/.env")
    ap.add_argument("--dest-root", help="default: PCLOUD_DEST aus .env")
    ap.add_argument("--snap", action="append", help="Snapshot(s); default: letzte --last remote")
    ap.add_argument("--last", type=int, default=2, metavar="N",
                    help="Letzte N Remote-Snapshots messen (default: 2, 0=alle)")
    ap.add_argument("--logical", action="store_true",
                    help="Zusaetzlich logische Groesse aus Index (langsam, ~20s)")
    ap.add_argument("--walk-pool", action="store_true",
                    help="Zusaetzlich physisches _pool/ summieren (~40s)")
    ap.add_argument("--walk-pool-mono", action="store_true", help="_pool/ in einem listfolder")
    ap.add_argument("--walk-snapshots", action="append", nargs="*", metavar="SNAP",
                    help="Stub-Summe mehrerer Ordner; 'all' = alle remote")
    ap.add_argument("--no-quota", action="store_true")
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
            print(f"Konto (userinfo):  {_fmt_gb(used)} / {_fmt_gb(quota)} GB ({pct})")
            print()
        except Exception as e:
            print(f"  (userinfo nicht verfügbar: {e})")
            print()

    remote = _list_remote_snaps(cfg, snaps_root)
    if args.snap:
        snaps = args.snap
    elif args.last == 0:
        snaps = remote
    elif args.last > 0:
        snaps = remote[-args.last:]
    else:
        snaps = remote[-2:] if len(remote) >= 2 else remote

    if not snaps:
        print("Keine Snapshots.")
        return 1

    print(f"Remote-Snapshots gesamt: {len(remote)}  |  messe: {len(snaps)}")
    print()

    # Standard: nur physische Groesse der Snapshot-Ordner (Stubs), kein _pool
    print(f"{'SNAPSHOT':<22} {'REMOTE_MB':>10} {'FILES':>11}")
    print("-" * 46)
    total_stub_b = 0
    for snap in snaps:
        try:
            b, f, dt = _walk_one_snapshot(cfg, snaps_root, snap)
            total_stub_b += b
            print(f"{snap:<22} {_fmt_mb(b):>10} {_fmt_n(f):>11}  ({dt:.1f}s)")
        except Exception as e:
            print(f"{snap:<22} {'ERROR':>10} {'—':>11}  ({e})")
    if len(snaps) > 1:
        print("-" * 46)
        print(f"{'Summe (angezeigt)':<22} {_fmt_mb(total_stub_b):>10}")
    print()
    print("REMOTE_MB = Bytes auf pCloud im Snapshot-Ordner (Stubs, nicht die Pool-Daten).")
    print("Pool gesamt:  --walk-pool   |   Wiederherstellbar:  --logical")
    print()

    pool_refs: dict = {}
    if args.logical or args.walk_pool:
        print(f"Lade {idx_path} ...", flush=True)
        t0 = time.time()
        try:
            index = _load_index(cfg, idx_path)
            pool_refs = index.get("pool_refs") or {}
            print(f"  → {len(pool_refs)} pool_refs in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"  Index nicht ladbar: {e}", flush=True)
        print()

    if args.logical and pool_refs:
        print(f"{'SNAPSHOT':<22} {'LOGICAL_GB':>11} {'FILES':>10}  (Index/Manifest)")
        print("-" * 48)
        for snap in snaps:
            logical, entries, _ = _snapshot_logical_bytes(pool_refs, snap)
            print(f"{snap:<22} {_fmt_gb(logical):>11} {_fmt_n(entries):>10}")
        print("  LOGICAL = wiederherstellbare Groesse, nicht pCloud-Platz pro Snapshot-Ordner.")
        print()

    if args.walk_pool:
        print("--- _pool (dedupliziert, alle Snapshots gemeinsam) ---")
        if args.walk_pool_mono:
            top = pc.call_with_backoff(
                pc.listfolder, cfg, path=pool_root, recursive=True, nofiles=False,
            ) or {}
            b, f, _ = _sum_tree(top.get("metadata") or {})
            print(f"       → {_fmt_gb(b)} GB ({_fmt_n(f)} Objekte)")
        else:
            _walk_pool_sharded(cfg, pool_root)
        print()

    if args.walk_snapshots is not None:
        if args.walk_snapshots == []:
            walk_list = snaps
        elif len(args.walk_snapshots) == 1 and args.walk_snapshots[0] == "all":
            walk_list = remote
        else:
            walk_list = args.walk_snapshots
        print("--- Stub-Summe ---")
        _walk_snapshots_list(cfg, snaps_root, walk_list)
        print()

    if not (args.walk_pool or args.logical or args.walk_snapshots is not None):
        print("Mehr:")
        print("  --snap NAME ...     bestimmte Snapshot(s)")
        print("  --last 1            nur letzter Snapshot")
        print("  --walk-pool         dedupliziertes _pool/ in GB")
        print("  --logical           wiederherstellbare Groesse aus Index")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
