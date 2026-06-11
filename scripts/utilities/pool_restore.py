#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_restore.py – Pool-Mode Restore von pCloud

Laedt Dateien aus dem deduplizierten Pool (_pool/XX/sha256) anhand des v2-Index
(pool_refs) oder einzelner Stubs (.meta.json).

Aufloesung:
  relpath + snapshot  →  pool_refs[sha].fileid  →  download_binaryfile_to(fileid=...)
  --relpath (einzeln) →  Stub lesen  →  pool_fileid

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \\
  python pool_restore.py --env-file .env --pool-root /Backup/rtb_pool --list-snapshots

  python pool_restore.py --env-file .env --pool-root /Backup/rtb_pool \\
    --snapshot 2026-05-28-120014 --out-dir /tmp/restore --download --verify

  python pool_restore.py --env-file .env --pool-root /Backup/rtb_pool \\
    --all-versions --relpath "Gemeinsam/Rest/dokument.pdf"

  python pool_restore.py --env-file .env --pool-root /Backup/rtb_pool \\
    --all-versions --relpath "Gemeinsam/Rest/dokument.pdf" \\
    --out-dir /srv/nas/restore --download --verify --only-changed
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc

# Download-Helfer aus Legacy-Restore (gleiches Verzeichnis)
_util_dir = os.path.dirname(os.path.abspath(__file__))
if _util_dir not in sys.path:
    sys.path.insert(0, _util_dir)
from pcloud_restore import (  # noqa: E402
    CHUNK_SIZE,
    PARALLEL_DOWNLOAD_THREADS,
    SMALL_FILE_THRESHOLD_BYTES,
    download_file_with_verify,
    download_via_fileid,
)

SMALL_FILE_THRESHOLD_BYTES = int(
    os.environ.get("PCLOUD_DOWNLOAD_SMALL_THRESHOLD", str(SMALL_FILE_THRESHOLD_BYTES))
)
PARALLEL_DOWNLOAD_THREADS = int(os.environ.get("PCLOUD_DOWNLOAD_THREADS", str(PARALLEL_DOWNLOAD_THREADS)))


class PoolRestoreError(Exception):
    pass


def _log(msg: str, *, level: str = "info") -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stream = sys.stderr if level in ("error", "warn") else sys.stdout
    prefix = f"[{level}] " if level != "info" else ""
    print(f"[{ts}] {prefix}{msg}", file=stream, flush=True)


def resolve_pool_root(args: argparse.Namespace) -> Optional[str]:
    """--pool-root (bevorzugt) oder deprecated --dest-root."""
    if getattr(args, "pool_root", None) and getattr(args, "dest_root", None):
        _log("Sowohl --pool-root als auch --dest-root gesetzt; --pool-root hat Vorrang", level="warn")
    pool_root = getattr(args, "pool_root", None) or getattr(args, "dest_root", None)
    if pool_root and getattr(args, "dest_root", None) and not getattr(args, "pool_root", None):
        _log("--dest-root ist deprecated, bitte --pool-root verwenden", level="warn")
    return pool_root


def _pool_obj_path(dest_root: str, sha256: str) -> str:
    sha = sha256.lower()
    return f"{dest_root.rstrip('/')}/_pool/{sha[:2]}/{sha}"


def _stub_remote_path(dest_root: str, snapshot: str, relpath: str) -> str:
    return f"{dest_root.rstrip('/')}/_snapshots/{snapshot}/{relpath}.meta.json"


def _index_path(dest_root: str) -> str:
    return f"{dest_root.rstrip('/')}/_snapshots/_index/content_index.json"


def load_pool_index(cfg: dict, dest_root: str) -> dict:
    path = _index_path(dest_root)
    _log(f"Lade Pool-Index: {path}")
    try:
        index = pc.read_json_at_path(cfg, path, maxbytes=None)
    except Exception as e:
        raise PoolRestoreError(f"Index laden fehlgeschlagen: {e}") from e
    if int(index.get("version", 0)) < 2 or not index.get("pool_refs"):
        raise PoolRestoreError("Index ist kein v2 Pool-Index (pool_refs fehlt)")
    return index


def list_snapshots_from_index(index: dict) -> List[str]:
    snaps: Set[str] = set()
    for entry in (index.get("pool_refs") or {}).values():
        snaps_map = entry.get("snapshots") or {}
        if isinstance(snaps_map, dict):
            snaps.update(snaps_map.keys())
    return sorted(snaps, reverse=True)


def _norm_relpath_prefix(value: str) -> str:
    return (value or "").lstrip("/")


def _version_local_relpath(relpath: str, snapshot: str) -> str:
    """Ziel unter out-dir/_versions/<relpath>/<snapshot>/<basename>."""
    base = os.path.basename(relpath) or relpath
    return f"_versions/{relpath}/{snapshot}/{base}"


def collect_version_history(
    pool_refs: dict,
    *,
    relpath: str = "",
    filter_prefix: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Alle (snapshot, sha, size) pro relpath aus pool_refs sammeln.
    Rueckgabe: relpath -> Liste chronologisch (Snapshot-Name aufsteigend).
    """
    relpath = _norm_relpath_prefix(relpath)
    filter_prefix = _norm_relpath_prefix(filter_prefix)
    if not relpath and not filter_prefix:
        raise PoolRestoreError("--relpath oder --filter erforderlich fuer --all-versions")

    by_path: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for sha, entry in pool_refs.items():
        if not isinstance(entry, dict):
            continue
        snapshots = entry.get("snapshots") or {}
        if not isinstance(snapshots, dict):
            continue
        fileid = entry.get("fileid")
        size = entry.get("size", 0)
        sha_l = sha.lower()
        for snap, relpaths in snapshots.items():
            if not isinstance(relpaths, list):
                continue
            for rp in relpaths:
                if relpath and rp != relpath:
                    continue
                if filter_prefix and not rp.startswith(filter_prefix):
                    continue
                by_path.setdefault(rp, {})
                prev = by_path[rp].get(snap)
                if prev and prev.get("sha256") != sha_l:
                    _log(
                        f"Mehrere SHA fuer {rp} in Snapshot {snap} — behalte letzten Eintrag",
                        level="warn",
                    )
                by_path[rp][snap] = {
                    "snapshot": snap,
                    "relpath": rp,
                    "sha256": sha_l,
                    "fileid": fileid,
                    "size": size,
                }

    history: Dict[str, List[Dict[str, Any]]] = {}
    for rp, snap_map in by_path.items():
        rows = [snap_map[s] for s in sorted(snap_map.keys())]
        prev_sha: Optional[str] = None
        for row in rows:
            changed = prev_sha is None or row["sha256"] != prev_sha
            row["changed"] = changed
            prev_sha = row["sha256"]
        history[rp] = rows
    return history


def version_items_for_restore(
    history: Dict[str, List[Dict[str, Any]]],
    *,
    only_changed: bool = False,
) -> List[Dict[str, Any]]:
    """Flache Download-Liste aus Versions-Historie (mit local_relpath unter _versions/)."""
    items: List[Dict[str, Any]] = []
    for rp in sorted(history.keys()):
        for row in history[rp]:
            if only_changed and not row.get("changed"):
                continue
            snap = row["snapshot"]
            items.append({
                "type": "file",
                "relpath": rp,
                "snapshot": snap,
                "sha256": row["sha256"],
                "fileid": row.get("fileid"),
                "size": row.get("size", 0),
                "local_relpath": _version_local_relpath(rp, snap),
            })
    return items


def print_version_history(history: Dict[str, List[Dict[str, Any]]]) -> None:
    """Stufe 1: menschenlesbare Timeline aller Snapshots pro Pfad."""
    if not history:
        _log("Keine Versionen gefunden", level="warn")
        return

    total_rows = sum(len(v) for v in history.values())
    distinct_shas = {
        row["sha256"]
        for rows in history.values()
        for row in rows
    }
    _log(
        f"Versions-Historie: {len(history)} Pfad(e), {total_rows} Snapshot-Eintraege, "
        f"{len(distinct_shas)} unterschiedliche SHA"
    )
    print()

    for rp in sorted(history.keys()):
        rows = history[rp]
        path_shas = {r["sha256"] for r in rows}
        print(f"=== {rp} ({len(rows)} Snapshots, {len(path_shas)} distinct SHA) ===")
        print(f"{'Snapshot':<22} {'SHA':<10} {'Size':>12}  {'Status'}")
        print("-" * 60)
        for row in rows:
            sha = (row.get("sha256") or "")[:8]
            size = row.get("size", 0)
            status = "changed" if row.get("changed") else "same"
            print(f"{row['snapshot']:<22} {sha:<10} {size:>12,}  {status}")
        print()


def items_from_index(
    pool_refs: dict,
    snapshot: str,
    *,
    filter_prefix: str = "",
    relpath: str = "",
) -> List[Dict[str, Any]]:
    """Items aus pool_refs fuer einen Snapshot extrahieren."""
    items: List[Dict[str, Any]] = []
    for sha, entry in pool_refs.items():
        if not isinstance(entry, dict):
            continue
        snapshots = entry.get("snapshots") or {}
        if not isinstance(snapshots, dict):
            continue
        relpaths = snapshots.get(snapshot) or []
        fileid = entry.get("fileid")
        size = entry.get("size", 0)
        for rp in relpaths:
            if relpath and rp != relpath:
                continue
            if filter_prefix and not rp.startswith(filter_prefix):
                continue
            items.append({
                "type": "file",
                "relpath": rp,
                "sha256": sha.lower(),
                "fileid": fileid,
                "size": size,
            })
    items.sort(key=lambda x: x.get("relpath", ""))
    return items


def item_from_stub(cfg: dict, dest_root: str, snapshot: str, relpath: str) -> Dict[str, Any]:
    """Einzelnes Item via Stub (.meta.json) aufloesen."""
    stub_path = _stub_remote_path(dest_root, snapshot, relpath)
    _log(f"Lese Stub: {stub_path}")
    try:
        stub = pc.read_json_at_path(cfg, stub_path, maxbytes=None)
    except Exception as e:
        raise PoolRestoreError(f"Stub nicht lesbar: {e}") from e
    if stub.get("type") != "pool_stub":
        raise PoolRestoreError(f"Kein pool_stub: {stub_path}")
    sha = (stub.get("sha256") or "").lower()
    fileid = stub.get("pool_fileid") or stub.get("fileid")
    if not sha:
        raise PoolRestoreError(f"Stub ohne sha256: {stub_path}")
    if not fileid:
        raise PoolRestoreError(f"Stub ohne pool_fileid: {stub_path}")
    return {
        "type": "file",
        "relpath": stub.get("relpath") or relpath,
        "sha256": sha,
        "fileid": fileid,
        "size": stub.get("size", 0),
    }


def snapshot_is_complete(cfg: dict, dest_root: str, snapshot: str) -> bool:
    marker = f"{dest_root.rstrip('/')}/_snapshots/{snapshot}/.upload_complete"
    try:
        pc.stat_file(cfg, path=marker, with_checksum=False)
        return True
    except Exception:
        return False


def _resolve_fileid(cfg: dict, dest_root: str, item: dict) -> Optional[int]:
    fileid = item.get("fileid")
    if fileid:
        return int(fileid)
    sha = item.get("sha256")
    if not sha:
        return None
    try:
        stat = pc.stat_file(cfg, path=_pool_obj_path(dest_root, sha), with_checksum=False) or {}
        fid = stat.get("fileid")
        return int(fid) if fid else None
    except Exception:
        return None


def _download_pool_item(
    cfg: dict,
    dest_root: str,
    item: dict,
    local_dest: str,
    *,
    verify: bool,
) -> bool:
    sha256 = item.get("sha256")
    verify_hash = sha256 if verify else None
    fileid = _resolve_fileid(cfg, dest_root, item)
    if fileid:
        return download_via_fileid(cfg, fileid, local_dest, verify_hash)
    if sha256:
        pool_path = _pool_obj_path(dest_root, sha256)
        return download_file_with_verify(cfg, pool_path, local_dest, verify_hash)
    return False


def run_flat_restore(
    cfg: dict,
    dest_root: str,
    items: List[Dict[str, Any]],
    base_out_dir: str,
    *,
    verify: bool,
) -> int:
    """Paralleler Bulk-Download (flat-Modus)."""
    os.makedirs(base_out_dir, exist_ok=True)
    stats = {"success": 0, "failed": 0, "skipped": 0, "downloaded": 0}
    sha_cache: Dict[str, str] = {}
    state_lock = threading.Lock()

    small_files = [f for f in items if f.get("size", 0) < SMALL_FILE_THRESHOLD_BYTES]
    large_files = [f for f in items if f.get("size", 0) >= SMALL_FILE_THRESHOLD_BYTES]
    total_items = len(items)
    total_bytes = sum(f.get("size", 0) for f in items)
    done_items = 0
    done_bytes = 0
    start_time = time.time()
    t_last_progress = start_time
    progress_interval = 5.0

    _log(
        f"Download: {total_items} Dateien "
        f"({len(small_files)} klein, {len(large_files)} gross), Ziel: {base_out_dir}"
    )

    def log_progress(force: bool = False) -> None:
        nonlocal t_last_progress
        now = time.time()
        if not force and (now - t_last_progress < progress_interval):
            return
        elapsed = now - start_time
        pct = (done_items / total_items * 100) if total_items else 0
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if done_bytes > 0 and elapsed > 0:
            speed = (done_bytes / (1024 * 1024)) / elapsed
            eta = (total_bytes - done_bytes) / (done_bytes / elapsed) if done_bytes else 0
            eta_str = f"~{int(eta // 60)}min" if eta > 60 else f"~{int(eta)}s"
            print(
                f"{ts} [pool-restore] {done_items}/{total_items} ({pct:.0f}%) | "
                f"{done_bytes / (1024**3):.2f}/{total_bytes / (1024**3):.2f} GB | "
                f"dl={stats['downloaded']} skip={stats['skipped']} fail={stats['failed']} | "
                f"{speed:.1f} MB/s | {eta_str}",
                flush=True,
            )
        else:
            print(f"{ts} [pool-restore] {done_items}/{total_items} ({pct:.0f}%)", flush=True)
        t_last_progress = now

    def process_item(item_tuple: tuple) -> None:
        nonlocal done_items, done_bytes
        idx, item = item_tuple
        relpath = item.get("relpath", f"?_{idx}")
        sha256 = item.get("sha256")
        file_size = item.get("size", 0)
        dest_relpath = item.get("local_relpath") or relpath
        local_dest = os.path.join(base_out_dir, dest_relpath)

        expected_prefix = os.path.join(base_out_dir) + os.sep
        if not os.path.normpath(local_dest).startswith(expected_prefix):
            _log(f"Path-Traversal verhindert: {relpath}", level="error")
            with state_lock:
                stats["failed"] += 1
                done_items += 1
            return

        # Hardlink-Dedup bei gleicher SHA
        if sha256 and sha256 in sha_cache:
            cached = sha_cache[sha256]
            if cached != local_dest and not os.path.exists(local_dest):
                os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
                try:
                    os.link(cached, local_dest)
                except OSError:
                    shutil.copy2(cached, local_dest)
                with state_lock:
                    stats["success"] += 1
                    stats["skipped"] += 1
                    done_items += 1
                    done_bytes += file_size
                return
            if os.path.exists(local_dest):
                with state_lock:
                    stats["skipped"] += 1
                    done_items += 1
                    done_bytes += file_size
                return

        # Resume: lokale SHA bereits OK
        if os.path.exists(local_dest) and sha256 and verify:
            try:
                h = hashlib.sha256()
                with open(local_dest, "rb") as f:
                    for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                        h.update(chunk)
                if h.hexdigest().lower() == sha256.lower():
                    with state_lock:
                        stats["skipped"] += 1
                        sha_cache[sha256] = local_dest
                        done_items += 1
                        done_bytes += file_size
                    return
            except Exception:
                pass

        os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
        ok = _download_pool_item(cfg, dest_root, item, local_dest, verify=verify)
        with state_lock:
            if ok:
                stats["success"] += 1
                stats["downloaded"] += 1
                if sha256:
                    sha_cache[sha256] = local_dest
            else:
                stats["failed"] += 1
            done_items += 1
            done_bytes += file_size

    if small_files:
        _log(f"Parallel: {len(small_files)} Dateien ({PARALLEL_DOWNLOAD_THREADS} Threads)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOAD_THREADS) as ex:
            futures = [
                ex.submit(process_item, (i + 1, f))
                for i, f in enumerate(small_files)
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    _log(f"Download-Fehler: {e}", level="error")
                log_progress()

    if large_files:
        _log(f"Sequentiell: {len(large_files)} grosse Dateien (>= {SMALL_FILE_THRESHOLD_BYTES // (1024*1024)} MB)")
        base_idx = len(small_files)
        for i, lf in enumerate(large_files):
            try:
                process_item((base_idx + i + 1, lf))
            except Exception as e:
                _log(f"Download-Fehler: {e}", level="error")
                with state_lock:
                    stats["failed"] += 1
            log_progress()

    log_progress(force=True)
    _log("=" * 60)
    _log(f"Restore abgeschlossen: ok={stats['success']} downloaded={stats['downloaded']} "
         f"skipped={stats['skipped']} failed={stats['failed']}")
    return 0 if stats["failed"] == 0 else 1


def print_plan(items: List[Dict[str, Any]], *, all_versions: bool = False) -> None:
    _log("Plan-Modus (kein Download):")
    for it in items[:15]:
        sha = (it.get("sha256") or "")[:8]
        size = it.get("size", 0)
        snap = it.get("snapshot")
        dest = it.get("local_relpath") or it.get("relpath")
        if all_versions and snap:
            print(f"  [{snap}] {it.get('relpath')} -> {dest} [{sha}] {size:,} B")
        else:
            print(f"  {it.get('relpath')} [{sha}] {size:,} B")
    if len(items) > 15:
        print(f"  ... ({len(items) - 15} weitere)")
    total = sum(it.get("size", 0) for it in items)
    _log(f"Gesamt: {len(items)} Dateien, ~{total / (1024**2):.1f} MB")


def _filter_complete_snapshots(
    cfg: dict,
    dest: str,
    history: Dict[str, List[Dict[str, Any]]],
    *,
    allow_incomplete: bool,
) -> Dict[str, List[Dict[str, Any]]]:
    if allow_incomplete:
        return history
    all_snaps = {row["snapshot"] for rows in history.values() for row in rows}
    complete_snaps = {s for s in all_snaps if snapshot_is_complete(cfg, dest, s)}
    skipped = all_snaps - complete_snaps
    if skipped:
        _log(
            f"Ueberspringe {len(skipped)} unvollstaendige Snapshot(s) ohne .upload_complete: "
            f"{', '.join(sorted(skipped)[:5])}"
            + (" ..." if len(skipped) > 5 else ""),
            level="warn",
        )
    filtered: Dict[str, List[Dict[str, Any]]] = {}
    for rp, rows in history.items():
        kept = [r for r in rows if r["snapshot"] in complete_snaps]
        if kept:
            prev_sha: Optional[str] = None
            for row in kept:
                row["changed"] = prev_sha is None or row["sha256"] != prev_sha
                prev_sha = row["sha256"]
            filtered[rp] = kept
    return filtered


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pool-Mode Restore: Dateien aus _pool via pool_refs oder Stub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool --list-snapshots
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool \\
    --snapshot 2026-05-28-120014 --out-dir /tmp/restore
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool \\
    --snapshot 2026-05-28-120014 --filter "Gemeinsam/Rest/" --out-dir /tmp/restore --download --verify
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool \\
    --snapshot 2026-05-28-120014 --relpath "home/user/datei.txt" \\
    --out-dir /tmp/restore --download --verify
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool \\
    --all-versions --relpath "Gemeinsam/Rest/dokument.pdf"
  %(prog)s --env-file .env --pool-root /Backup/rtb_pool \\
    --all-versions --relpath "Gemeinsam/Rest/dokument.pdf" \\
    --out-dir /srv/nas/restore --download --verify --only-changed
        """,
    )
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--pool-root", help="Remote Pool-Root auf pCloud (Quelle), z.B. /Backup/rtb_pool")
    ap.add_argument("--dest-root", help="(deprecated) Alias fuer --pool-root")
    ap.add_argument("--list-snapshots", action="store_true", help="Verfuegbare Snapshots anzeigen")
    ap.add_argument("--snapshot", help="Snapshot-Name (nicht mit --all-versions)")
    ap.add_argument("--relpath", help="Einzelne Datei (Stub-Weg)")
    ap.add_argument("--filter", help="Nur Relpaths mit diesem Praefix")
    ap.add_argument("--all-versions", action="store_true",
                    help="Alle Snapshot-Versionen fuer --relpath/--filter (Timeline oder Restore)")
    ap.add_argument("--only-changed", action="store_true",
                    help="Bei --all-versions + Download: nur Eintraege mit neuem SHA laden")
    ap.add_argument("--out-dir", help="Lokales Ziel (Unterordner = Snapshot-Name)")
    ap.add_argument("--download", action="store_true", help="Download ausfuehren (sonst Plan)")
    ap.add_argument("--verify", action="store_true", help="SHA256 nach Download pruefen")
    ap.add_argument("--allow-incomplete", action="store_true",
                    help="Snapshot ohne .upload_complete trotzdem erlauben")
    args = ap.parse_args()

    pool_root_raw = resolve_pool_root(args)
    if not pool_root_raw:
        _log("--pool-root erforderlich (--dest-root ist deprecated)", level="error")
        return 2

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(pool_root_raw).rstrip("/")

    if args.list_snapshots:
        try:
            index = load_pool_index(cfg, dest)
        except PoolRestoreError as e:
            _log(str(e), level="error")
            return 2
        snaps = list_snapshots_from_index(index)
        print(f"Snapshots in {dest} ({len(snaps)}):")
        for s in snaps:
            print(f"  {s}")
        return 0

    if args.all_versions:
        if args.snapshot:
            _log("--snapshot wird mit --all-versions ignoriert", level="warn")
        if not args.relpath and not args.filter:
            _log("--all-versions erfordert --relpath oder --filter", level="error")
            return 2
        try:
            index = load_pool_index(cfg, dest)
            pool_refs = index.get("pool_refs") or {}
            history = collect_version_history(
                pool_refs,
                relpath=args.relpath or "",
                filter_prefix=args.filter or "",
            )
            history = _filter_complete_snapshots(
                cfg, dest, history, allow_incomplete=args.allow_incomplete,
            )
            if not history:
                raise PoolRestoreError("Keine Versionen gefunden (nach Filter/Completeness)")
        except PoolRestoreError as e:
            _log(str(e), level="error")
            return 2

        print_version_history(history)

        if not args.download:
            items = version_items_for_restore(history, only_changed=args.only_changed)
            if items:
                print()
                _log(f"Download-Plan ({'nur geaenderte' if args.only_changed else 'alle'}): "
                     f"{len(items)} Datei(en)")
                print_plan(items, all_versions=True)
            return 0

        if not args.out_dir:
            _log("--out-dir erforderlich fuer Download", level="error")
            return 2

        items = version_items_for_restore(history, only_changed=args.only_changed)
        if not items:
            _log("Nichts zu laden (leer oder --only-changed ohne Unterschiede)", level="warn")
            return 0

        _log(f"All-Versions Restore: {len(items)} Datei(en) -> {args.out_dir}/_versions/...")
        return run_flat_restore(cfg, dest, items, args.out_dir, verify=args.verify)

    if not args.snapshot:
        _log("--snapshot erforderlich (oder --list-snapshots / --all-versions)", level="error")
        return 2
    if not args.out_dir:
        _log("--out-dir erforderlich", level="error")
        return 2

    if not args.allow_incomplete and not snapshot_is_complete(cfg, dest, args.snapshot):
        _log(
            f"Snapshot {args.snapshot} hat kein .upload_complete — "
            "moeglicherweise unvollstaendig. --allow-incomplete zum Fortfahren.",
            level="warn",
        )
        return 2

    try:
        if args.relpath:
            items = [item_from_stub(cfg, dest, args.snapshot, args.relpath)]
        else:
            index = load_pool_index(cfg, dest)
            pool_refs = index.get("pool_refs") or {}
            items = items_from_index(
                pool_refs,
                args.snapshot,
                filter_prefix=args.filter or "",
            )
            if not items:
                raise PoolRestoreError(
                    f"Keine Dateien fuer Snapshot '{args.snapshot}'"
                    + (f" mit Filter '{args.filter}'" if args.filter else "")
                )
    except PoolRestoreError as e:
        _log(str(e), level="error")
        return 2

    base_out = os.path.join(args.out_dir, args.snapshot)
    _log(f"Snapshot: {args.snapshot} | Items: {len(items)} | Ziel: {base_out}")

    if not args.download:
        print_plan(items)
        return 0

    return run_flat_restore(cfg, dest, items, base_out, verify=args.verify)


if __name__ == "__main__":
    sys.exit(main())
