#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_gc_index.py — Ein Index-Motor für GC, Retention, delete-snapshots, forecast.

Arbeitsspeicher: Remote-Master → Disk (streaming download), pool_refs → SQLite
(streaming import), Purge/Abfragen per SQL, Export → Master-JSON (streaming write),
Upload resumable von Disk. Kein volles pool_refs-Dict im RAM.

Gleiche Purge-/Referenz-Logik für dry-run und scharf (dry = kein Upload/Löschung).
Dry-run delete-snapshots: nur bestehende Ops-DB. Lesen/Schreiben sonst: ein Sync-Pfad
(Remote-SHA-Check, kein digest-Marathon).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, Iterable, Optional, Set

import pcloud_bin_lib as pc

import pool_index_db as pidb
from pool_index_db import _hash_file_sha256

LogFn = Callable[[str], None]


def _env_get(env_vars: Optional[dict], key: str, default: str = "") -> str:
    if env_vars and key in env_vars:
        return env_vars[key]
    return os.environ.get(key, default)


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


def _ops_db_matches_remote_sha(db: pidb.PoolIndexDB, remote_sha: Optional[str]) -> bool:
    if db.get_meta("upload_pending") == "1":
        return False
    if db.count_shas() == 0:
        return False
    stored = (db.get_meta("master_sha256") or "").strip().lower()
    return bool(remote_sha and stored and remote_sha == stored)


def _abort_stale_pending_upload(
    db: pidb.PoolIndexDB,
    *,
    log: LogFn,
    remote_sha: str,
    stored_base: str,
) -> None:
    """Remote-Index hat sich geändert — kein Upload des lokalen Pending-Masters."""
    pending_snaps = (db.get_meta("upload_pending_snaps") or "").strip()
    log(
        "[gc-engine][ERROR] upload_pending, aber Remote-Index ist neuer "
        f"(remote {remote_sha[:16]}… ≠ Basis {stored_base[:16]}…) — "
        "kein Upload (würde Backup-Index überschreiben). "
        "Sync von Remote, Purge ggf. erneut."
    )
    db.set_meta("upload_pending", "0", commit=False)
    db.set_meta("master_pending_sha256", "", commit=False)
    db.set_meta("upload_pending_snaps", "", commit=False)
    if pending_snaps:
        db.set_meta("reapply_purge_after_sync", pending_snaps, commit=False)
    db.conn.commit()


def _log_deferred_index_repairs(env_vars: Optional[dict], *, log: LogFn) -> None:
    db_path = pidb.default_ops_db_path(env_vars)
    if not os.path.isfile(db_path):
        return
    db = pidb.open_db(db_path, create=False)
    try:
        pending = db.get_meta("upload_pending") == "1"
        reapply = (db.get_meta("reapply_purge_after_sync") or "").strip()
        if pending or reapply:
            log(
                "[gc-engine][info] Ausstehende Index-Reparatur "
                f"(upload_pending={pending}, reapply={bool(reapply)}) — "
                "nur Lesen; scharfer delete-snapshots/Retention/GC-Sync führt Repair aus"
            )
    finally:
        db.close()


def _reapply_purge_after_remote_drift(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    repair_writes: bool = True,
) -> None:
    db_path = pidb.default_ops_db_path(env_vars)
    if not os.path.isfile(db_path):
        return
    db = pidb.open_db(db_path, create=False)
    try:
        raw = (db.get_meta("reapply_purge_after_sync") or "").strip()
    finally:
        db.close()
    if not raw:
        return
    if not repair_writes:
        _log_deferred_index_repairs(env_vars, log=log)
        return
    snaps = {s.strip() for s in raw.split(",") if s.strip()}
    if not snaps:
        return
    log(
        f"[gc-engine] Purge nach Remote-Drift erneut ({len(snaps)} Snapshot(s)) …"
    )
    apply_index_purge_for_deleted_snaps(
        cfg, snapshots_root, env_vars, snaps, dry=False, log=log,
    )
    db2 = pidb.open_db(db_path, create=False)
    try:
        db2.set_meta("reapply_purge_after_sync", "", commit=True)
    finally:
        db2.close()


def maybe_flush_pending_master_upload(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    repair_writes: bool = True,
) -> bool:
    """
    Nach fehlgeschlagenem Master-Upload: lokaler Master/Ops-DB sind neu,
    Remote noch alt — upload_pending=1 bis Upload gelingt.
    """
    db_path = pidb.default_ops_db_path(env_vars)
    if not os.path.isfile(db_path):
        return False
    db = pidb.open_db(db_path, create=False)
    try:
        if db.get_meta("upload_pending") != "1":
            return False
        if not repair_writes:
            _log_deferred_index_repairs(env_vars, log=log)
            return False
        master_path, _ = master_paths(env_vars)
        if not os.path.isfile(master_path):
            log(
                "[gc-engine][ERROR] upload_pending gesetzt, Master-Datei fehlt — "
                f"erwartet: {master_path}"
            )
            return False
        pending_sha = (db.get_meta("master_pending_sha256") or "").strip().lower()
        stored_base = (db.get_meta("master_sha256") or "").strip().lower()
        remote_now = remote_master_sha256(cfg, snapshots_root, log=log)
        if remote_now and pending_sha and remote_now == pending_sha:
            log(
                "[gc-engine] Remote-Index entspricht pending SHA — "
                "Upload bereits erfolgt, Pending aufgeräumt"
            )
            db.record_master_meta(master_path, known_sha256=pending_sha, commit=False)
            db.set_meta("upload_pending", "0", commit=False)
            db.set_meta("master_pending_sha256", "", commit=False)
            db.set_meta("upload_pending_snaps", "", commit=False)
            db.conn.commit()
            return True
        if stored_base and remote_now and remote_now != stored_base:
            _abort_stale_pending_upload(
                db, log=log, remote_sha=remote_now, stored_base=stored_base,
            )
            return False
        n_refs = db.count_shas()
        log(
            f"[gc-engine] Ausstehender Master-Upload (upload_pending) — "
            f"Retry ({n_refs} pool_refs) …"
        )
        _upload_master_resumable(cfg, snapshots_root, master_path, n_refs, log=log)
        file_sha = pending_sha
        if not file_sha:
            file_sha = _hash_file_sha256(master_path)
        db.record_master_meta(master_path, known_sha256=file_sha, commit=False)
        db.set_meta("upload_pending", "0", commit=False)
        db.set_meta("master_pending_sha256", "", commit=False)
        db.set_meta("upload_pending_snaps", "", commit=False)
        db.conn.commit()
        log("[gc-engine] Ausstehender Master-Upload abgeschlossen")
        return True
    except Exception as e:
        log(
            f"[gc-engine][ERROR] Master-Upload (Retry) fehlgeschlagen: {e} — "
            "upload_pending bleibt gesetzt"
        )
        raise
    finally:
        db.close()


def _open_ops_db_if_current(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    allow_without_remote_sha: bool = False,
) -> Optional[pidb.PoolIndexDB]:
    """
    Ops-DB öffnen wenn Remote-SHA zum letzten Import passt (kein Download/Import).
    """
    db_path = pidb.default_ops_db_path(env_vars)
    if not os.path.isfile(db_path):
        return None
    db = pidb.open_db(db_path, create=False)
    n = db.count_shas()
    if n == 0:
        db.close()
        return None
    remote_sha = remote_master_sha256(cfg, snapshots_root, log=log)
    if _ops_db_matches_remote_sha(db, remote_sha):
        log(
            "[gc-engine] Ops-DB aktuell (Remote-SHA) — Download/Re-Import übersprungen"
        )
        master_path, _ = master_paths(env_vars)
        if os.path.isfile(master_path) and remote_sha:
            db.refresh_master_metadata(master_path, known_sha256=remote_sha)
        return db
    if allow_without_remote_sha and not remote_sha:
        log(
            f"[gc-engine][warn] Remote-SHA unbekannt — Ops-DB für Lesen ({n} SHAs)"
        )
        return db
    db.close()
    return None


def _ops_db_can_skip_import_after_download(
    db: pidb.PoolIndexDB,
    master_path: str,
    remote_sha: Optional[str],
) -> bool:
    if db.count_shas() == 0:
        return False
    if _ops_db_matches_remote_sha(db, remote_sha):
        return True
    if db.master_fingerprint_matches(master_path) is True:
        return True
    return False


def download_remote_master(
    cfg: dict,
    snapshots_root: str,
    master_path: str,
    *,
    log: LogFn,
) -> str:
    """RAM-schonend: getfilelink + Chunk-Write auf Disk. Returns: SHA256 der Datei."""
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
    file_sha = pc.download_binaryfile_to(cfg, path=remote, local_path=tmp).strip().lower()
    os.replace(tmp, master_path)
    log(
        f"[gc-engine] Download fertig ({time.time() - t0:.1f}s, "
        f"{os.path.getsize(master_path)} bytes, sha256={file_sha[:16]}…)"
    )
    return file_sha


def open_ops_db_for_queries(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    repair_writes: bool = False,
) -> pidb.PoolIndexDB:
    """Forecast / GC Phase 1 — gleicher Fast-Path wie scharf, nur SQL-Lesen."""
    maybe_flush_pending_master_upload(
        cfg, snapshots_root, env_vars, log=log, repair_writes=repair_writes,
    )
    log_path = pidb.default_ops_db_path(env_vars)
    log(f"[gc-engine] Ops-SQLite (Abfragen): {log_path}")
    current = _open_ops_db_if_current(
        cfg, snapshots_root, env_vars, log=log, allow_without_remote_sha=True,
    )
    if current is not None:
        return current
    log("[gc-engine] Ops-DB fehlt oder Remote geändert — Sync …")
    return open_synced_db(
        cfg, snapshots_root, env_vars, log=log, repair_writes=repair_writes,
    )


def open_synced_db(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    repair_writes: bool = True,
) -> pidb.PoolIndexDB:
    """Ops-DB zum Remote-Stand (Download/Import nur wenn nötig)."""
    maybe_flush_pending_master_upload(
        cfg, snapshots_root, env_vars, log=log, repair_writes=repair_writes,
    )
    db_path = pidb.default_ops_db_path(env_vars)
    log(f"[gc-engine] Ops-SQLite (Sync): {db_path}")

    current = _open_ops_db_if_current(
        cfg, snapshots_root, env_vars, log=log, allow_without_remote_sha=False,
    )
    if current is not None:
        return current

    master_path, _ = master_paths(env_vars)
    db = pidb.open_db(db_path, create=True)
    remote_sha_hint = remote_master_sha256(cfg, snapshots_root, log=log)
    stored = (db.get_meta("master_sha256") or "").strip().lower()

    if remote_sha_hint and stored and remote_sha_hint != stored and db.count_shas() > 0:
        log("[gc-engine] Remote-Index geändert seit letztem Ops-Import — Download …")
    elif not stored or db.count_shas() == 0:
        log("[gc-engine] Ops-Index leer oder ohne Fingerprint — Download …")
    elif not remote_sha_hint:
        log("[gc-engine] Remote-SHA unbekannt — Download …")
    else:
        log("[gc-engine] Ops-DB nicht mehr aktuell — Download …")

    file_sha = download_remote_master(cfg, snapshots_root, master_path, log=log)
    if remote_sha_hint and file_sha != remote_sha_hint:
        log(
            "[gc-engine][warn] Remote-Index während Download geändert "
            "(checksumfile ≠ Stream-SHA) — nutze Datei-SHA"
        )

    if _ops_db_can_skip_import_after_download(db, master_path, file_sha):
        log("[gc-engine] SQLite aktuell — Re-Import übersprungen")
        db.refresh_master_metadata(master_path, known_sha256=file_sha)
        _reapply_purge_after_remote_drift(
            cfg, snapshots_root, env_vars, log=log, repair_writes=repair_writes,
        )
        return db

    log("[gc-engine] Streaming-Import Master → SQLite …")
    db.import_from_json_streaming(master_path, log=log, known_sha256=file_sha)
    _reapply_purge_after_remote_drift(
        cfg, snapshots_root, env_vars, log=log, repair_writes=repair_writes,
    )
    return db


def retention_index_metrics(
    db: pidb.PoolIndexDB,
    remote_snaps: Set[str],
    snaps_to_remove: Set[str],
) -> tuple[int, int, int, int]:
    """Counts + geschätzte Orphan-Bytes per SQL (kein millionenfaches SHA-Set)."""
    n_now = db.count_referenced_shas_for_snapshots(remote_snaps)
    keep = remote_snaps - snaps_to_remove
    n_after = db.count_referenced_shas_for_snapshots(keep)
    n_orphan = db.count_orphan_shas_if_snapshots_removed(remote_snaps, snaps_to_remove)
    orphan_bytes = db.sum_orphan_bytes_if_snapshots_removed(remote_snaps, snaps_to_remove)
    return n_now, n_after, n_orphan, orphan_bytes


def purge_snapshots_in_db(
    db: pidb.PoolIndexDB,
    snap_names: Iterable[str],
    *,
    log: LogFn,
    commit: bool = True,
) -> Dict[str, int]:
    removed_refs = 0
    for snap in sorted({s.strip() for s in snap_names if s and s.strip()}):
        n = db.purge_snapshot(snap, commit=commit)
        if n:
            log(f"[gc-engine] purge {snap}: {n} snap_refs")
        removed_refs += n
    return {"removed_snap_refs": removed_refs}


def export_master_from_db(
    db: pidb.PoolIndexDB,
    env_vars: Optional[dict],
    *,
    log: LogFn,
    replace_master: bool = True,
) -> Dict[str, object]:
    master_path, staging_path = master_paths(env_vars)
    os.makedirs(os.path.dirname(staging_path), exist_ok=True)
    os.makedirs(os.path.dirname(master_path), exist_ok=True)
    exp = db.export_content_index_json(staging_path, record_export_meta=False)
    file_sha = str(exp.get("file_sha256") or "")
    if replace_master:
        os.replace(staging_path, master_path)
    n_refs = int(exp.get("shas") or 0)
    log(
        f"[gc-engine] Export lokal: {master_path} "
        f"({n_refs} pool_refs, {exp.get('bytes', 0)} bytes, {exp.get('seconds', 0):.1f}s)"
    )
    exp["master_path"] = master_path
    exp["staging_path"] = staging_path
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
    local_size = os.path.getsize(local_path)
    gc_min_ratio = float(os.environ.get("PCLOUD_GC_INDEX_UPLOAD_MIN_SIZE_RATIO", "0"))
    if gc_min_ratio > 0:
        log(
            "[gc-engine][warn] PCLOUD_GC_INDEX_UPLOAD_MIN_SIZE_RATIO>0 — nach Purge kann "
            "Upload blockieren (upload_pending); bei Bedarf auf 0 setzen"
        )
    pc.guard_index_upload_size(
        cfg, remote_path, local_size, min_size_ratio=gc_min_ratio,
    )
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
    """Dry-run: nur COUNT — kein DELETE (purge_snapshot commit() bricht SAVEPOINT)."""
    db_path = pidb.default_ops_db_path(env_vars)
    log(f"[gc-engine] Dry-run: Ops-DB nur lesen (kein Remote-Download): {db_path}")
    if not os.path.isfile(db_path):
        log(
            "[gc-engine][ERROR] Ops-DB fehlt — einmaliger Sync nötig (scharf oder Forecast/GC)."
        )
        return {"removed_snap_refs": 0}
    db = pidb.open_db(db_path, create=False)
    try:
        n_shas = db.count_shas()
        if n_shas == 0:
            log("[gc-engine][ERROR] Ops-DB leer — kein Import überspringen möglich")
            return {"removed_snap_refs": 0}
        log(f"[gc-engine] Ops-DB bereit ({n_shas} SHAs) — Purge simulieren (COUNT)")
        removed_refs = 0
        for snap in sorted({s.strip() for s in deleted_snaps if s and s.strip()}):
            n = db.snap_ref_count(snap)
            if n:
                log(f"[dry] würde purge {snap}: {n} snap_refs")
            removed_refs += n
        if removed_refs:
            log(
                f"[dry] Index-Purge simuliert: {removed_refs} snap-refs; "
                f"würde exportieren (~{n_shas} pool_refs) + Upload"
            )
        else:
            log(
                f"[dry] Index-Purge simuliert: 0 snap-refs — "
                "kein Export/Upload nötig"
            )
        return {"removed_snap_refs": removed_refs}
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
    db = open_synced_db(
        cfg, snapshots_root, env_vars, log=log, repair_writes=True,
    )
    c = db.conn
    master_path = ""
    file_sha = ""
    n_refs = 0
    try:
        c.execute("BEGIN IMMEDIATE")
        stats = purge_snapshots_in_db(db, deleted_snaps, log=log, commit=False)
        if stats["removed_snap_refs"] == 0:
            c.rollback()
            log(
                "[gc-engine] Purge ohne snap-refs — Export/Upload übersprungen"
            )
            return stats
        snap_csv = ",".join(sorted({s.strip() for s in deleted_snaps if s.strip()}))
        db.set_meta("upload_pending_snaps", snap_csv, commit=False)
        exp = export_master_from_db(db, env_vars, log=log, replace_master=False)
        staging_path = str(exp["staging_path"])
        master_path = str(exp["master_path"])
        file_sha = str(exp.get("file_sha256") or "")
        n_refs = int(exp.get("shas") or 0)
        os.replace(staging_path, master_path)
        db.set_meta("upload_pending", "1", commit=False)
        if file_sha:
            db.set_meta("master_pending_sha256", file_sha, commit=False)
        c.commit()
    except Exception:
        c.rollback()
        db.close()
        raise

    log("[gc-engine] Ops-DB committed (upload_pending) — Master-Upload ohne Write-Lock")
    try:
        try:
            _upload_master_resumable(cfg, snapshots_root, master_path, n_refs, log=log)
        except Exception:
            log(
                "[gc-engine][ERROR] Upload fehlgeschlagen — lokaler Master/Ops-DB "
                "bereinigt, Remote veraltet; nächster Lauf versucht Upload erneut "
                "(upload_pending=1)."
            )
            raise
        db.record_master_meta(master_path, known_sha256=file_sha or None, commit=False)
        db.set_meta("upload_pending", "0", commit=False)
        db.set_meta("master_pending_sha256", "", commit=False)
        db.set_meta("upload_pending_snaps", "", commit=False)
        c.commit()
        return stats
    finally:
        db.close()


class RemoteSnapReferencedShaLookup:
    """Membership-Tests per SQL — kein millionenfaches SHA-Set im RAM."""

    def __init__(self, db: pidb.PoolIndexDB, remote_snaps: Set[str]):
        self._db = db
        self._remote = remote_snaps
        self._len = db.count_referenced_shas_for_snapshots(remote_snaps)

    def __contains__(self, sha: object) -> bool:
        return self._db.sha_has_remote_snap_ref(str(sha), self._remote)

    def __len__(self) -> int:
        return self._len

    def iter_shas(self, *, batch_size: int = 8192):
        yield from self._db.iter_referenced_shas_for_snapshots(
            self._remote, batch_size=batch_size,
        )

    def close(self) -> None:
        self._db.close()


def referenced_shas_for_gc(
    cfg: dict,
    snapshots_root: str,
    env_vars: Optional[dict],
    remote_snaps: Set[str],
    *,
    log: LogFn,
) -> RemoteSnapReferencedShaLookup:
    db = open_ops_db_for_queries(cfg, snapshots_root, env_vars, log=log)
    return RemoteSnapReferencedShaLookup(db, remote_snaps)
