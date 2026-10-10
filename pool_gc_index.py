#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_gc_index.py — Ein Index-Motor für GC, Retention, delete-snapshots, forecast.

Arbeitsspeicher: Remote-Master → Disk (streaming download), pool_refs → SQLite
(streaming import), Purge/Abfragen per SQL, Export → Master-JSON (streaming write),
Upload resumable von Disk. Kein volles pool_refs-Dict im RAM.

Gleiche Purge-/Referenz-Logik für dry-run und scharf (dry = kein Upload, DB bleibt
nach Forecast unverändert via separaten Sync nur lesen).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, Iterable, Optional, Set

import pcloud_bin_lib as pc

import pool_index_db as pidb

LogFn = Callable[[str], None]


def _env_get(env_vars: Optional[dict], key: str, default: str = "") -> str:
    if env_vars and key in env_vars:
        return env_vars[key]
    return os.environ.get(key, default)


def gc_engine_enabled(env_vars: Optional[dict] = None) -> bool:
    """Default an — PCLOUD_GC_USE_INDEX_DB=0 erzwingt Legacy (nicht empfohlen)."""
    return _env_get(env_vars, "PCLOUD_GC_USE_INDEX_DB", "1") != "0"


def master_paths(env_vars: Optional[dict] = None) -> tuple[str, str]:
    archive = _env_get(env_vars, "PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    master = _env_get(env_vars, "PCLOUD_POOL_INDEX_MASTER", "") or os.path.join(
        archive, "indexes", "content_index_master.json"
    )
    staging = os.path.join(archive, "indexes", "staging", "content_index_pending.json")
    return master, staging


def remote_index_path(snapshots_root: str) -> str:
    return f"{snapshots_root.rstrip('/')}/_index/content_index.json"


def remote_master_sha256(cfg: dict, snapshots_root: str, *, log: LogFn) -> Optional[str]:
    """pCloud checksumfile — kein 2-GB-Download nur für Vergleich."""
    path = remote_index_path(snapshots_root)
    try:
        cs = pc.checksumfile(cfg, path=path)
        h = (cs.get("sha256") or "").strip().lower()
        return h or None
    except Exception as e:
        log(f"[gc-engine][warn] Remote checksumfile ({path}): {e}")
        return None


def _ops_db_can_skip_import(
    db: pidb.PoolIndexDB,
    master_path: str,
    remote_sha: Optional[str],
) -> bool:
    if db.count_shas() == 0:
        return False
    stored = (db.get_meta("master_sha256") or "").strip().lower()
    if stored and remote_sha and remote_sha == stored:
        return True
    if db.master_fingerprint_matches(master_path) is True:
        return True
    # Kein master_file_sha256_matches — liest 2 GB JSON erneut (Pi-minuten, kein Mehrwert).
    return False


def download_remote_master(
    cfg: dict,
    snapshots_root: str,
    master_path: str,
    *,
    log: LogFn,
) -> None:
    """RAM-schonend: getfilelink + Chunk-Write auf Disk."""
    remote = remote_index_path(snapshots_root)
    os.makedirs(os.path.dirname(os.path.abspath(master_path)) or ".", exist_ok=True)
    tmp = master_path + ".downloading"
    if os.path.isfile(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    log(f"[gc-engine] Lade Remote-Index → {master_path}")
    t0 = time.time()
    pc.download_binaryfile_to(cfg, path=remote, local_path=tmp)
    os.replace(tmp, master_path)
    log(f"[gc-engine] Download fertig ({time.time() - t0:.1f}s, {os.path.getsize(master_path)} bytes)")


def open_synced_db(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
) -> pidb.PoolIndexDB:
    """Remote-Master auf Disk, SQLite spiegeln (streaming import wenn nötig)."""
    master_path, _ = master_paths(env_vars)
    db_path = pidb.default_ops_db_path(env_vars)
    log(f"[gc-engine] Ops-SQLite (getrennt vom Backup-Index): {db_path}")
    db = pidb.open_db(db_path, create=True)

    remote_sha = remote_master_sha256(cfg, snapshots_root, log=log)
    stored = (db.get_meta("master_sha256") or "").strip().lower()
    if remote_sha and stored and remote_sha == stored and db.count_shas() > 0:
        log(
            "[gc-engine] Remote-Index unverändert (SHA256) — "
            "Download und Re-Import übersprungen"
        )
        if os.path.isfile(master_path):
            db.refresh_master_metadata(master_path)
        return db

    if remote_sha and stored and remote_sha != stored:
        log("[gc-engine] Remote-Index geändert seit letztem Ops-Import — Download …")
    elif not stored or db.count_shas() == 0:
        log("[gc-engine] Ops-Index leer oder ohne Fingerprint — Download …")
    else:
        log("[gc-engine] Remote-SHA unbekannt — Download …")

    download_remote_master(cfg, snapshots_root, master_path, log=log)
    remote_sha = remote_sha or remote_master_sha256(cfg, snapshots_root, log=log)

    if _ops_db_can_skip_import(db, master_path, remote_sha):
        log("[gc-engine] SQLite aktuell — Re-Import übersprungen")
        db.refresh_master_metadata(master_path)
        return db

    log("[gc-engine] Streaming-Import Master → SQLite …")
    db.import_from_json_streaming(master_path, log=log)
    return db


def retention_index_metrics(
    db: pidb.PoolIndexDB,
    remote_snaps: Set[str],
    snaps_to_remove: Set[str],
) -> tuple[Set[str], Set[str], Set[str]]:
    """Gleiche Semantik wie _referenced_shas + _shas_orphaned_after_retention."""
    refs_now = db.referenced_shas_for_snapshots(remote_snaps)
    keep = remote_snaps - snaps_to_remove
    refs_after = db.referenced_shas_for_snapshots(keep)
    orphan = db.orphan_shas_if_snapshots_removed(remote_snaps, snaps_to_remove)
    return refs_now, refs_after, orphan


def purge_snapshots_in_db(
    db: pidb.PoolIndexDB,
    snap_names: Iterable[str],
    *,
    log: LogFn,
) -> Dict[str, int]:
    removed_refs = 0
    for snap in sorted({s.strip() for s in snap_names if s and s.strip()}):
        n = db.purge_snapshot(snap)
        if n:
            log(f"[gc-engine] purge {snap}: {n} snap_refs")
        removed_refs += n
    return {"removed_snap_refs": removed_refs}


def publish_master_index(
    cfg: dict,
    snapshots_root: str,
    db: pidb.PoolIndexDB,
    env_vars: Optional[dict],
    *,
    dry: bool,
    log: LogFn,
) -> Dict[str, int]:
    master_path, staging_path = master_paths(env_vars)
    os.makedirs(os.path.dirname(staging_path), exist_ok=True)
    os.makedirs(os.path.dirname(master_path), exist_ok=True)
    exp = db.export_content_index_json(staging_path)
    os.replace(staging_path, master_path)
    db.refresh_master_metadata(master_path)
    n_refs = int(exp.get("shas") or 0)
    log(
        f"[gc-engine] Export lokal: {master_path} "
        f"({n_refs} pool_refs, {exp.get('bytes', 0)} bytes, {exp.get('seconds', 0):.1f}s)"
    )
    if dry:
        log(f"[dry] upload skipped: {remote_index_path(snapshots_root)}")
        return exp
    _upload_master_resumable(cfg, snapshots_root, master_path, n_refs, log=log)
    return exp


def _upload_master_resumable(
    cfg: dict,
    snapshots_root: str,
    local_path: str,
    n_refs: int,
    *,
    log: LogFn,
) -> None:
    idx_dir = f"{snapshots_root.rstrip('/')}/_index"
    idx_name = "content_index.json"
    remote_path = f"{idx_dir}/{idx_name}"
    fid = pc.stat_folderid_fast(cfg, idx_dir)
    if not fid:
        fid = pc.ensure_path(cfg, idx_dir)
    log_every = int(os.environ.get("PCLOUD_INDEX_UPLOAD_LOG_EVERY_CHUNKS", "1"))
    verify = os.environ.get("PCLOUD_INDEX_UPLOAD_VERIFY", "1") != "0"
    log(f"[gc-engine] Upload: {remote_path} (pool_refs={n_refs})")
    t0 = time.time()
    pc.upload_local_file_resumable(
        cfg,
        local_path,
        folderid=int(fid),
        filename=idx_name,
        remote_path=remote_path,
        state_key="content_index_json",
        log_prefix="[gc-engine-upload]",
        log=log,
        log_every_chunks=log_every,
        verify_sha256=verify,
    )
    log(f"[gc-engine] Upload fertig ({time.time() - t0:.1f}s)")


def _dry_simulate_purge_on_ops_db(
    env_vars: Optional[dict],
    deleted_snaps: Set[str],
    *,
    log: LogFn,
) -> Dict[str, int]:
    """Dry-run delete-snapshots: bestehende Ops-DB — kein Download, Import, digest."""
    db_path = pidb.default_ops_db_path(env_vars)
    log(f"[gc-engine] Dry-run: Ops-DB nur lesen (kein Remote-Download): {db_path}")
    if not os.path.isfile(db_path):
        log(
            "[gc-engine][ERROR] Ops-DB fehlt — zuerst einmal Lauf mit neuem Code "
            "(pull) oder Ops-Import; Backup-DB pool_index.sqlite3 wird nicht genutzt."
        )
        return {"removed_snap_refs": 0}
    db = pidb.open_db(db_path, create=False)
    try:
        n_shas = db.count_shas()
        if n_shas == 0:
            log("[gc-engine][ERROR] Ops-DB leer — kein Import überspringen möglich")
            return {"removed_snap_refs": 0}
        log(f"[gc-engine] Ops-DB bereit ({n_shas} SHAs) — Purge simulieren")
        db.conn.execute("SAVEPOINT index_purge")
        stats = purge_snapshots_in_db(db, deleted_snaps, log=log)
        log(
            f"[dry] Index-Purge simuliert: {stats['removed_snap_refs']} snap-refs; "
            f"würde exportieren (~{n_shas} pool_refs), kein Upload"
        )
        db.conn.execute("ROLLBACK TO SAVEPOINT index_purge")
        return stats
    finally:
        db.close()


def apply_index_purge_for_deleted_snaps(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    deleted_snaps: Set[str],
    *,
    dry: bool,
    log: LogFn,
) -> Dict[str, int]:
    """
    Nach Remote-Snapshot-Löschung: SQLite purge + Export (+ Upload wenn nicht dry).
    """
    if not deleted_snaps:
        return {"removed_snap_refs": 0}
    if dry:
        return _dry_simulate_purge_on_ops_db(env_vars, deleted_snaps, log=log)
    db = open_synced_db(cfg, snapshots_root, env_vars, log=log)
    try:
        db.conn.execute("SAVEPOINT index_purge")
        stats = purge_snapshots_in_db(db, deleted_snaps, log=log)
        publish_master_index(cfg, snapshots_root, db, env_vars, dry=False, log=log)
        db.conn.execute("RELEASE SAVEPOINT index_purge")
        return stats
    finally:
        db.close()


def referenced_shas_for_gc(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    remote_snaps: Set[str],
    *,
    log: LogFn,
) -> Set[str]:
    db = open_synced_db(cfg, snapshots_root, env_vars, log=log)
    try:
        return db.referenced_shas_for_snapshots(remote_snaps)
    finally:
        db.close()
