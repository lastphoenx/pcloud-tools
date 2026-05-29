#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_push_json_pool_manifest_to_pcloud.py

=== POOL-MODE VERSION ===
Lädt ein lokales Manifest (v4) nach pCloud mit POOL-basierter Architektur.

POOL-ARCHITEKTUR:
  /_pool/XX/[sha256]           ← Echte Dateien (dedupliziert nach SHA256)
  /_snapshots/SNAPSHOT/...     ← Nur .meta.json Stubs (lesbare Ordnerstruktur)

Vorteile:
- ✓ Quota-effizient: Nur 1× pro SHA256 (nicht 20× bei 20 Snapshots)
- ✓ Retention einfach: deletefolderrecursive(snapshot) - fertig!
- ✓ Lesbar: Jeder Snapshot behält Original-Ordnerstruktur
- ✓ Restore schnell: pool_fileid in Stub → direkter Download

STUB-FORMAT (pool_stub):
{
  "type": "pool_stub",
  "sha256": "abc123...",
  "pcloud_hash": "...",         # Für API checksumfile (Speed-Verify)
  "size": 12345678,
  "mtime": 1717000000.0,
  "relpath": "home/user/file.txt",
  "pool_path": "/_pool/ab/abc123...",
  "pool_fileid": 87654321,
  "snapshot": "2026-05-28-120014"
}

BETRIEBSART: --snapshot-mode pool
- Alle Files → /_pool/ (dedupliziert)
- Alle Snapshots → Stubs (lesbare Struktur)
- Content-Index → Pool-Referenzen tracken
- Retention → Ordner löschen + Pool-GC

Erwartetes Manifest (schema=4):
{
  "schema": 4,
  "mode": "pool_full" oder "pool_smart",
  "snapshot": "YYYY-mm-dd-HHMMSS",
  "root": "/abs/pfad/zum/snapshot",
  "hash": "sha256",
  "items": [...]
}

Benötigt: pcloud_bin_lib.py im selben Verzeichnis oder PYTHONPATH.
"""

from __future__ import annotations
import os, sys, json, argparse, time, datetime
import concurrent.futures
import threading
from typing import Dict, Any, Optional, Tuple
from enum import Enum


# ---- Logging mit Timestamp (RTB-Stil) ----
def _log(msg: str, *, file=sys.stderr) -> None:
    """Log-Ausgabe mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", file=file, flush=True)


# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"Fehler: pcloud_bin_lib konnte nicht importiert werden: {e}", file=sys.stderr)
    sys.exit(2)


# --- Schwellenwerte für Smart-Strategie-Auswahl ---
# MATCH_THRESHOLD (X): Ab welcher Übereinstimmung lohnt sich Klonen/Template?
_MATCH_THRESHOLD = float(os.environ.get("PCLOUD_SMART_MATCH_THRESHOLD", "0.85"))
# STUB_THRESHOLD (Y): Mindest-Stub-Ratio in Basis für TURBO (Quota-Schutz)
_STUB_THRESHOLD  = float(os.environ.get("PCLOUD_SMART_STUB_THRESHOLD", "0.50"))
# TEMPLATE_THRESHOLD (Z): Mindest-Übereinstimmung mit Template für TEMPLATE-DELTA-SAFE
_TEMPLATE_THRESHOLD = float(os.environ.get("PCLOUD_SMART_TEMPLATE_THRESHOLD", "0.70"))

# Smart-Strategy 2.0 (absolute Delta-Metriken + harte Sicherheitsgates)
_SMART_STUB_TRANSFORM_THRESHOLD = float(os.environ.get("PCLOUD_SMART_STUB_TRANSFORM_THRESHOLD", "0.80"))
_SMART_SAVED_CALLS_MIN = int(os.environ.get("PCLOUD_SMART_SAVED_CALLS_MIN", "1000"))
_SMART_TEMPLATE_STRONG_THRESHOLD = float(os.environ.get("PCLOUD_SMART_TEMPLATE_STRONG_THRESHOLD", "0.90"))

_FOLDER_TEMPLATE_DIRNAME = "_folder_template"


def _get_manifest_paths(manifest: dict) -> set[str]:
    """Extrahiert alle Datei-Pfade aus dem Manifest."""
    return {it["relpath"] for it in manifest.get("items", []) if it.get("type") == "file" and "relpath" in it}

def _get_manifest_folders(manifest: dict) -> set[str]:
    """Extrahiert alle Ordner-Pfade aus dem Manifest."""
    return {it["relpath"] for it in manifest.get("items", []) if it.get("type") == "dir" and "relpath" in it}

def _get_snapshot_paths(index: dict, snapshot_name: str) -> set[str]:
    """Extrahiert alle Datei-Pfade eines Snapshots aus dem Master-Index."""
    items = index.get("items") or {}
    paths = set()
    for sha, node in items.items():
        # Anchor check
        ap = node.get("anchor_path") or ""
        if "/_snapshots/" in ap:
            try:
                parts = ap.split("/_snapshots/")
                if len(parts) > 1:
                    subparts = parts[1].split("/", 1)
                    if subparts[0] == snapshot_name and len(subparts) > 1:
                        paths.add(subparts[1])
            except (IndexError, AttributeError):
                pass
        
        # Holders check
        holders = node.get("holders") or []
        for h in holders:
            if h.get("snapshot") == snapshot_name:
                rp = h.get("relpath")
                if rp:
                    paths.add(rp)
    return paths


def push_1to1_smart_controller(cfg, manifest, dest_root, *, dry=False, verbose=False, manifest_path=None):
    """
    Zentraler Strategy-Controller – EINZIGER Entscheidungspunkt für Strategie.
    Entscheidet EINMAL, ruft dann Executor mit klarem Modus auf.
    """
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    
    _log(f"[smart-controller] Analysiere Strategie für {snapshot_name}...")
    
    # Einheitliche Metriken (unified source) mit Fallback bei API-Problemen
    try:
        metrics = _get_sync_metrics_unified(cfg, manifest, dest_root, archive_dir)
    except Exception as e:
        _log(f"[smart-controller] WARN: Metriken konnten nicht berechnet werden ({e}). Nutze SAFE-MODE.")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="safe-mode")

    # Single Source of Truth: zentrale Klasse entscheidet deterministisch
    controller = SmartStrategyController()
    strategy = controller.decide(metrics)
    controller.log_decision(strategy, metrics, snapshot_name)

    if strategy == SyncStrategy.TURBO_MODE:
        try:
            return push_1to1_delta_mode(
                cfg,
                manifest,
                dest_root,
                dry=dry,
                verbose=verbose,
                manifest_path=manifest_path,
                basis_snapshot=metrics.get("basis_snapshot"),
            )
        except Exception as e:
            _log(f"[fallback] TURBO failed: {e} -> switching to SAFE-MODE")
            return push_1to1_mode(
                cfg,
                manifest,
                dest_root,
                dry=dry,
                verbose=verbose,
                manifest_path=manifest_path,
                strategy_mode=SyncStrategy.SAFE_MODE.value,
            )

    return push_1to1_mode(
        cfg,
        manifest,
        dest_root,
        dry=dry,
        verbose=verbose,
        manifest_path=manifest_path,
        strategy_mode=strategy.value,
    )


# Performance-Messung
fid_cache = {}
fid_lookups = 0          # Anzahl _fid_for Aufrufe
fid_cache_hits = 0       # Treffer im Cache
fid_rest_ms = 0.0        # aufsummierte Zeit in pc.resolve_fileid_cached
t_phase_start = time.time()

# --- shared fileid cache for this process ---
_fid_cache_shared: dict = {}

# --- Metrics (Prometheus-freundlich) ---
MET_UPLOADED_FILES = 0
MET_RESUMED_FILES  = 0
MET_STUBS_WRITTEN  = 0
MET_PROMOTED       = 0
MET_REMOVED_NODES  = 0
MET_API_RETRIES    = int(os.environ.get("PCLOUD_API_RETRIES", "0"))  # optional Zähler aus Lib/Wrapper

# --- Chunked Upload Configuration ---
RESUME_THRESHOLD_BYTES = int(os.environ.get("PCLOUD_RESUME_THRESHOLD_GB", "5")) * 1024**3  # Default: 5 GB
RESUME_CHUNK_SIZE = int(os.environ.get("PCLOUD_RESUME_CHUNK_MB", "128")) * 1024**2  # Default: 128 MB

# --- Parallel Upload Configuration ---
SMALL_FILE_THRESHOLD_BYTES = int(os.environ.get("PCLOUD_SMALL_FILE_THRESHOLD_MB", "50")) * 1024**2  # Default: 50 MB
PARALLEL_UPLOAD_THREADS = int(os.environ.get("PCLOUD_UPLOAD_THREADS", "4"))  # Default: 4 threads

# --- Global Metrics Lock (Thread-Safety) ---
_metrics_lock = threading.Lock()

# ----------------- Utilities -----------------

def _ensure_parent(cfg, remote_path: str, *, dry: bool = False) -> None:
    """
    Stellt sicher, dass alle Elternordner für `remote_path` existieren.
    Delegiert vollständig an pcloud_bin_lib.ensure_parent_dirs(...).
    """
    if dry:
        return
    pc.ensure_parent_dirs(cfg, remote_path)


def _get_resume_state_dir() -> str:
    """
    Ermittelt State-Verzeichnis für Resume-Uploads (analog zu poc_chunked_resume.py).
    
    Priorität:
    1. ENV: PCLOUD_RESUME_DIR
    2. /srv/pcloud-archive/resume/ (production)
    3. ~/.pcloud_resume/ (user home)
    4. /tmp/pcloud_resume/ (fallback)
    """
    if "PCLOUD_RESUME_DIR" in os.environ:
        state_dir = os.environ["PCLOUD_RESUME_DIR"]
        try:
            os.makedirs(state_dir, exist_ok=True)
            return state_dir
        except Exception:
            pass
    
    production_dir = "/srv/pcloud-archive/resume"
    if os.path.exists("/srv"):
        try:
            os.makedirs(production_dir, exist_ok=True)
            test_file = os.path.join(production_dir, ".write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return production_dir
        except Exception:
            pass
    
    home_dir = os.path.expanduser("~/.pcloud_resume")
    try:
        os.makedirs(home_dir, exist_ok=True)
        return home_dir
    except Exception:
        pass
    
    tmp_dir = "/tmp/pcloud_resume"
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir


def _cleanup_orphaned_resume_states(state_dir: str, *, max_age_days: int = 7, verbose: bool = False) -> int:
    """
    Bereinigt verwaiste/alte Resume-State-Files.
    
    Löscht:
    - Files älter als max_age_days (Standard: 7 Tage)
    - Error-Status Files älter als 24 Stunden
    - Files mit korruptem JSON
    
    Returns:
        Anzahl gelöschter Files
    """
    if not os.path.exists(state_dir):
        return 0
    
    deleted = 0
    now = time.time()
    max_age_seconds = max_age_days * 86400
    error_max_age = 86400  # 24 Stunden
    
    try:
        for filename in os.listdir(state_dir):
            if not filename.endswith(".state.json"):
                continue
            
            filepath = os.path.join(state_dir, filename)
            
            try:
                # File-Age prüfen
                mtime = os.path.getmtime(filepath)
                age = now - mtime
                
                # Alte Files löschen
                if age > max_age_seconds:
                    if verbose:
                        _log(f"[cleanup] Lösche altes State-File ({age/86400:.1f} Tage alt): {filename}")
                    os.remove(filepath)
                    deleted += 1
                    continue
                
                # State laden und prüfen
                try:
                    with open(filepath, "r") as f:
                        state = json.load(f)
                    
                    # Error-Status Files nach 24h löschen
                    if state.get("status") == "error" and age > error_max_age:
                        if verbose:
                            _log(f"[cleanup] Lösche Error-State ({age/3600:.1f}h alt): {filename}")
                        os.remove(filepath)
                        deleted += 1
                        continue
                    
                except json.JSONDecodeError:
                    # Korruptes JSON löschen
                    if verbose:
                        _log(f"[cleanup] Lösche korruptes State-File: {filename}")
                    os.remove(filepath)
                    deleted += 1
                    continue
                
            except Exception as e:
                if verbose:
                    _log(f"[cleanup] Fehler bei {filename}: {e}")
                continue
    
    except Exception as e:
        _log(f"[cleanup] Cleanup-Fehler: {e}")
    
    if deleted > 0 and verbose:
        _log(f"[cleanup] {deleted} verwaiste State-Files gelöscht")
    
    return deleted


def _upload_file_resumable(cfg: dict, local_path: str, remote_path: str,
                          *, dry: bool = False) -> dict:
    """
    Chunked Upload mit automatischem Resume (basierend auf poc_chunked_resume.py).
    
    Features:
    - State-Persistenz nach jedem Chunk
    - upload_info() Server-Sync vor Resume
    - Automatische Offset-Korrektur
    - SHA256-Verifikation nach Upload
    - Snapshot-Validierung (State gehört zu aktuellem Snapshot)
    - Automatisches Cleanup ungültiger/alter State-Files
    
    Returns:
        Dict mit 'metadata' (kompatibel mit pc.upload_file)
    """
    import hashlib
    import re
    
    if dry:
        _log(f"[dry] chunked upload: {remote_path} <- {local_path}")
        return {"metadata": {"fileid": None, "hash": None, "size": os.path.getsize(local_path)}}
    
    state_dir = _get_resume_state_dir()
    file_size = os.path.getsize(local_path)
    
    # Einmalige Cleanup-Routine (pro Prozess)
    global _resume_cleanup_done
    try:
        _resume_cleanup_done
    except NameError:
        _resume_cleanup_done = False
    
    if not _resume_cleanup_done and os.environ.get("PCLOUD_RESUME_CLEANUP", "1") != "0":
        max_age = int(os.environ.get("PCLOUD_RESUME_CLEANUP_DAYS", "7"))
        verbose = os.environ.get("PCLOUD_VERBOSE") == "1"
        _cleanup_orphaned_resume_states(state_dir, max_age_days=max_age, verbose=verbose)
        _resume_cleanup_done = True
    
    # Snapshot-Name aus remote_path extrahieren (wichtig für Validierung!)
    # Format: /_snapshots/<snapshot_name>/...
    snapshot_name = None
    match = re.search(r'/_snapshots/([^/]+)/', remote_path)
    if match:
        snapshot_name = match.group(1)
    
    # State-File basierend auf remote_path (eindeutig!)
    state_key = hashlib.sha256(remote_path.encode()).hexdigest()[:16]
    filename_base = os.path.basename(local_path)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename_base)
    state_file = os.path.join(state_dir, f"{safe_name}_{state_key}.state.json")
    
    # State laden (falls vorhanden) - VOR SHA256-Berechnung!
    uploadid = None
    upload_offset = 0
    chunks_uploaded = 0
    file_hash = None
    is_resume = False
    
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            
            # Hash aus State übernehmen (falls vorhanden)
            file_hash = state.get("file_hash")
            
            # Validierung 1: Pfad & Größe müssen matchen
            if state.get("remote_path") != remote_path:
                _log(f"[chunked] State ungültig (remote_path geändert) - lösche State")
                os.remove(state_file)
                file_hash = None
            elif state.get("file_size") != file_size:
                _log(f"[chunked] State ungültig (file_size geändert) - lösche State")
                os.remove(state_file)
                file_hash = None
            # Validierung 2: Snapshot-Name muss matchen (wenn vorhanden)
            elif snapshot_name and state.get("snapshot_name") != snapshot_name:
                _log(f"[chunked] State ungültig (Snapshot geändert: {state.get('snapshot_name')} -> {snapshot_name}) - lösche State")
                os.remove(state_file)
                file_hash = None
            else:
                # State ist valide - versuche Resume
                uploadid = state.get("uploadid")
                upload_offset = state.get("offset", 0)
                chunks_uploaded = state.get("chunks_uploaded", 0)
                is_resume = True
                
                _log(f"[chunked] Lade State: {os.path.basename(state_file)}")
                _log(f"[chunked] Resume @ {upload_offset:,} Bytes ({upload_offset/file_size*100:.1f}%)")
                
                # Metrik hochzählen
                try:
                    with _metrics_lock:
                        globals()["MET_RESUMED_FILES"] += 1
                except Exception:
                    pass
                
                # Server-Sync via upload_info()
                try:
                    server_info = pc.upload_info(cfg, uploadid)
                    server_offset = server_info.get("size", 0)
                    
                    if server_offset != upload_offset:
                        _log(f"[chunked] Offset-Korrektur: Lokal={upload_offset:,} Server={server_offset:,}")
                        upload_offset = server_offset
                        chunks_uploaded = server_offset // RESUME_CHUNK_SIZE
                except Exception as e:
                    _log(f"[chunked] upload_info fehlgeschlagen: {e} - lösche State und starte neu")
                    # Wichtig: State-File löschen, nicht überschreiben!
                    try:
                        os.remove(state_file)
                    except Exception:
                        pass
                    uploadid = None
                    upload_offset = 0
                    chunks_uploaded = 0
                    is_resume = False
                    file_hash = None  # SHA256 neu berechnen
        except Exception as e:
            _log(f"[chunked] State-Load fehlgeschlagen: {e} - starte neu")
            try:
                os.remove(state_file)
            except Exception:
                pass
            uploadid = None
            file_hash = None
    
    # FIX 3: SHA256 nur berechnen wenn nicht aus State geladen
    if not file_hash:
        _log(f"[chunked] Berechne SHA256 für {filename_base} ({file_size/1024**3:.2f} GB)...")
        h = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024**2), b""):
                h.update(chunk)
        file_hash = h.hexdigest()
    
    # Upload-Session erstellen (falls nötig)
    if not uploadid:
        _log(f"[chunked] Erstelle Upload-Session...")
        resp = pc.upload_create(cfg)
        uploadid = resp.get("uploadid")
        _log(f"[chunked] uploadid: {uploadid}")
    
    # Chunks hochladen
    _log(f"[chunked] Starte Upload: {file_size/1024**3:.2f} GB @ {RESUME_CHUNK_SIZE/1024**2:.0f} MB Chunks")
    
    with open(local_path, "rb") as fh:
        fh.seek(upload_offset)
        chunk_number = chunks_uploaded
        
        while upload_offset < file_size:
            chunk_data = fh.read(RESUME_CHUNK_SIZE)
            if not chunk_data:
                break
            
            chunk_number += 1
            
            # FIX 2: Retry-Logik für Chunk-Upload (transiente Netzwerkfehler)
            chunk_uploaded = False
            for retry in range(1, 6):  # 5 Versuche pro Chunk
                try:
                    pc.upload_write(cfg, uploadid, upload_offset, chunk_data)
                    chunk_uploaded = True
                    break
                except Exception as e:
                    if retry == 5:
                        _log(f"[chunked] Chunk {chunk_number} fehlgeschlagen nach 5 Versuchen: {e}")
                        # State speichern vor Exit
                        with open(state_file, "w") as f:
                            json.dump({
                                "uploadid": uploadid,
                                "offset": upload_offset,
                                "chunks_uploaded": chunk_number - 1,
                                "file_hash": file_hash,
                                "file_size": file_size,
                                "remote_path": remote_path,
                                "snapshot_name": snapshot_name,
                                "status": "error",
                                "error": str(e),
                                "updated_at": time.time()
                            }, f)
                        raise
                    else:
                        wait_time = 2 ** retry  # Exponential backoff: 2, 4, 8, 16 Sekunden
                        _log(f"[chunked] Chunk {chunk_number} Retry {retry}/5 nach {wait_time}s: {e}")
                        time.sleep(wait_time)
            
            if not chunk_uploaded:
                raise RuntimeError(f"Chunk {chunk_number} konnte nicht hochgeladen werden")
            
            upload_offset += len(chunk_data)
            
            # State speichern nach jedem Chunk
            with open(state_file, "w") as f:
                json.dump({
                    "uploadid": uploadid,
                    "offset": upload_offset,
                    "chunks_uploaded": chunk_number,
                    "file_hash": file_hash,
                    "file_size": file_size,
                    "remote_path": remote_path,
                    "snapshot_name": snapshot_name,
                    "status": "in_progress",
                    "updated_at": time.time()
                }, f)
            
            # Progress Log (alle 10 Chunks)
            if chunk_number % 10 == 0:
                progress_pct = upload_offset / file_size * 100
                _log(f"[chunked] Progress: {upload_offset:,}/{file_size:,} Bytes ({progress_pct:.1f}%)")
    
    # Finalisierung mit Retry-Logik
    _log(f"[chunked] Finalisiere Upload...")
    dest_dir = os.path.dirname(remote_path.rstrip("/")) or "/"
    dest_filename = os.path.basename(remote_path)
    dest_folderid = pc.ensure_path(cfg, dest_dir)
    
    # FIX 2: Retry-Logik auch für upload_save (kritisch!)
    result = None
    for retry in range(1, 6):  # 5 Versuche für Finalisierung
        try:
            result = pc.upload_save(cfg, uploadid, folderid=dest_folderid, name=dest_filename)
            break
        except Exception as e:
            if retry == 5:
                _log(f"[chunked] upload_save fehlgeschlagen nach 5 Versuchen: {e}")
                raise
            else:
                wait_time = 2 ** retry
                _log(f"[chunked] upload_save Retry {retry}/5 nach {wait_time}s: {e}")
                time.sleep(wait_time)
    
    if not result:
        raise RuntimeError("upload_save konnte nicht abgeschlossen werden")
    
    metadata = result.get("metadata", {})
    if isinstance(metadata, list):
        metadata = metadata[0] if metadata else {}
    
    _log(f"[chunked] Upload abgeschlossen: FileID={metadata.get('fileid')}")
    
    # SHA256-Verifikation
    remote_fileid = metadata.get('fileid')
    if remote_fileid:
        try:
            checksum_data = pc.checksumfile(cfg, fileid=int(remote_fileid))
            remote_sha256 = checksum_data.get("sha256", "").lower()
            
            if file_hash.lower() == remote_sha256:
                _log(f"[chunked] ✓ SHA256 verifiziert")
            else:
                _log(f"[chunked] ✗ SHA256 MISMATCH! Lokal={file_hash[:16]}... Remote={remote_sha256[:16]}...")
        except Exception as e:
            _log(f"[chunked] SHA256-Verifikation fehlgeschlagen: {e}")
    
    # State aufräumen
    try:
        os.remove(state_file)
    except Exception:
        pass
    
    return result


def _upload_file_smart(cfg: dict, local_path: str, remote_path: str,
                      *, dry: bool = False) -> dict:
    """
    Smart Upload: Standard-Upload für kleine Files, Chunked-Resume für große Files.
    
    Args:
        cfg: pCloud Config
        local_path: Lokale Quelldatei
        remote_path: Ziel in pCloud (voller Pfad inkl. Dateiname)
        dry: Dry-run Mode
    
    Returns:
        Upload-Response mit 'metadata' Dict
    """
    file_size = os.path.getsize(local_path)
    
    # Große Dateien: Chunked Upload mit Resume
    if file_size > RESUME_THRESHOLD_BYTES:
        return _upload_file_resumable(cfg, local_path, remote_path, dry=dry)
    
    # Kleine Dateien: Standard-Upload (schneller!)
    return pc.call_with_backoff(pc.upload_file, cfg,
                                local_path=local_path,
                                remote_path=remote_path,
                                attempts=12, max_sleep=60.0)


def stat_file_safe(cfg: dict, *, path: Optional[str]=None, fileid: Optional[int]=None) -> Optional[dict]:
    """Stat-Datei; gibt None bei 'not found' zurück (anstatt Exception)."""
    try:
        if path is not None:
            md = pc.stat_file(cfg, path=pc._norm_remote_path(path), with_checksum=False, enrich_path=True)
        else:
            md = pc.stat_file(cfg, fileid=int(fileid), with_checksum=False, enrich_path=True)
        if not md or md.get("isfolder"):
            return None
        return md
    except Exception:
        return None
def ensure_parent_dirs(cfg: dict, remote_path: str, *, dry: bool=False) -> None:
    """Sorgt dafür, dass alle Ordner bis zum parent von remote_path existieren."""
    p = pc._norm_remote_path(remote_path)
    parent = p.rsplit("/", 1)[0] or "/"
    if dry:
        return
    pc.ensure_path(cfg, parent)

def upload_json_stub(cfg: dict, remote_path: str, payload: dict, *, dry: bool=False) -> None:
    if dry:
        target = payload.get("object_path") or payload.get("anchor_path") or payload.get("sha256")
        print(f"[dry] stub: {remote_path} -> {target}")
        return
    pc.ensure_parent_dirs(cfg, remote_path)
    pc.write_json_at_path(cfg, remote_path, payload)

def _bytes_to_tempfile(b: bytes) -> str:
    import tempfile
    fd, p = tempfile.mkstemp(prefix="pcloud_stub_", suffix=".json")
    with os.fdopen(fd, "wb") as f:
        f.write(b)
    return p

def object_path_for(objects_root: str, sha256: str, ext: Optional[str], layout: str="two-level") -> str:
    """Pfad im Object-Store. layout='two-level' legt /_objects/xx/sha.ext an."""
    sha = (sha256 or "").lower()
    if not sha or len(sha) < 2:
        sub = "zz"
    else:
        sub = sha[:2]
    e = (ext or "").lstrip(".")
    tail = sha if not e else (sha + "." + e)
    return f"{objects_root.rstrip('/')}/{sub}/{tail}"

def snapshot_path_for(snapshots_root: str, snapshot: str, relpath: str) -> str:
    return f"{snapshots_root.rstrip('/')}/{snapshot}/{relpath}".replace("//", "/")

def stub_path_for(snapshots_root: str, snapshot: str, relpath: str) -> str:
    return f"{snapshots_root.rstrip('/')}/{snapshot}/{relpath}.meta.json".replace("//", "/")

def key_from_inode(item: dict) -> Optional[str]:
    """Erzeugt einen Key für Hardlink-Gruppierung; None wenn kein inode."""
    ino = (item.get("inode") or {})
    dev = ino.get("dev"); n = ino.get("ino")
    if dev is None or n is None:
        return None
    return f"{dev}:{n}"

def _compute_snapshot_stub_ratio(index: dict, snapshot_name: str) -> tuple:
    """
    Analysiert den lokalen Master-Index und berechnet die Stub-Ratio
    für einen gegebenen Snapshot – OHNE API-Calls, rein lokal, O(n).

    Ein Node "gehört" zu snapshot_name wenn:
      a) anchor_path den Snapshot-Namen enthält (→ echte Datei / Anchor)
      b) Ein Holder-Eintrag mit snapshot == snapshot_name existiert (→ Stub)

    Returns: (total, stubs, stub_ratio)
      total      = Anzahl Dateien, die in diesem Snapshot existieren
      stubs      = Davon Stubs (d.h. Holder, aber NICHT Anchor)
      stub_ratio = stubs / total (0.0 bis 1.0)
    """
    items = (index.get("items") or {})
    total = 0
    stub_count = 0

    for sha, node in items.items():
        anchor_path = node.get("anchor_path") or ""

        # Snapshot-Name aus anchor_path extrahieren:
        # Format: /.../_snapshots/YYYY-MM-DD-HHMMSS/relpath
        # → Segment nach "_snapshots/" ist der Snapshot-Name
        anchor_snap = ""
        if "/_snapshots/" in anchor_path:
            try:
                anchor_snap = anchor_path.split("/_snapshots/")[1].split("/")[0]
            except (IndexError, AttributeError):
                anchor_snap = ""

        # Prüfe ob Node in diesem Snapshot vorkommt (als Anchor ODER Holder)
        is_anchor = (anchor_snap == snapshot_name)
        is_holder = any(
            isinstance(h, dict) and h.get("snapshot") == snapshot_name
            for h in (node.get("holders") or [])
        )

        if is_anchor or is_holder:
            total += 1
            if not is_anchor:  # Holder aber kein Anchor → Stub
                stub_count += 1

    ratio = stub_count / total if total > 0 else 0.0
    return total, stub_count, ratio


def save_content_index_local(local_path: str, index: dict) -> None:
    """Speichert den Index lokal als JSON."""
    import tempfile
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    # Atomic write via tempfile
    dir_path = os.path.dirname(local_path)
    with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False, suffix='.tmp') as f:
        json.dump(index, f, separators=(',', ':'))
        temp_path = f.name
    os.replace(temp_path, local_path)
    # Ensure readable by all (fix for simulator and other utilities)
    os.chmod(local_path, 0o644)

def load_content_index_local(local_path: str) -> dict:
    """Lädt den Index lokal, falls vorhanden."""
    try:
        with open(local_path, 'r') as f:
            j = json.load(f)
        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j
    except FileNotFoundError:
        return {"version": 1, "items": {}}
    except Exception:
        return {"version": 1, "items": {}}

def load_content_index(cfg: dict, snapshots_root: str) -> dict:
    """
    Lädt _snapshots/_index/content_index.json robust.
    - Wenn Datei fehlt/kaputt: leeren Index zurückgeben.
    - Ein 'result'≠0 im JSON gilt als API-Fehler (dann leerer Index).
    - Fehlt 'result' völlig (Normalfall bei echter Index-Datei) → OK.
    """
    idx_path = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = json.loads(txt)

        # Nur als API-Fehler werten, wenn 'result' vorhanden *und* != 0
        if isinstance(j, dict) and "result" in j and j.get("result") != 0:
            return {"version": 1, "items": {}}

        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j
    except Exception:
        return {"version": 1, "items": {}}

def save_content_index(cfg: dict, snapshots_root: str, index: dict, *, dry: bool=False) -> None:
    """
    content_index.json effizient schreiben:
    - ohne erneutes ensure()
    - minified JSON
    """
    idx_dir  = f"{snapshots_root.rstrip('/')}/_index"
    idx_name = "content_index.json"

    if dry:
        print(f"[dry] write index: {idx_dir}/{idx_name} (items={len(index.get('items',{}))})")
        return

    # Ordner muss existieren (wurde vorher per Batch-Ensure angelegt)
    fid = pc.stat_folderid_fast(cfg, idx_dir)
    if not fid:
        # sehr selten: Fallback (legt an und holt folderid)
        fid = pc.ensure_path(cfg, idx_dir)

    # Pretty-Print via ENV steuerbar
    pretty = os.environ.get("PCLOUD_PRETTY_JSON", "0") == "1"
    pc.write_json_to_folderid(cfg, folderid=int(fid), filename=idx_name, obj=index, minify=(not pretty))

def list_remote_snapshot_names(cfg: dict, snapshots_root: str) -> set[str]:
    """Liest die Ordnernamen unter <snapshots_root> (außer '_index')."""
    out: set[str] = set()
    try:
        top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False)
        for it in (top.get("metadata", {}) or {}).get("contents", []) or []:
            if it.get("isfolder") and it.get("name") and it.get("name") != "_index":
                out.add(it["name"])
    except Exception:
        pass
    return out

def list_local_snapshot_names(manifest_root: str) -> set[str]:
    """Liest Geschwister-Ordner des gegebenen Snapshot-Roots (RTB-Stil)."""
    base = os.path.dirname(os.path.abspath(manifest_root))  # parent von ".../<snapshot>"
    names = set()
    try:
        for n in os.listdir(base):
            p = os.path.join(base, n)
            if os.path.isdir(p) and n not in ("latest",):
                names.add(n)
    except Exception:
        pass
    return names


# ============================================================================
# SMART STRATEGY CONTROLLER
# ============================================================================

class SyncStrategy(Enum):
    SAFE_MODE = "SAFE-MODE"
    TURBO_MODE = "TURBO-MODE"
    TEMPLATE_DELTA_SAFE = "TEMPLATE-DELTA-SAFE"


class SmartStrategyController:
    """
    Deterministische Wahl der Sync-Strategie basierend auf Metriken.
    Priorisiert Quota-Sicherheit und Performance.
    """

    def __init__(self):
        # Default Schwellenwerte (über ENV steuerbar)
        self.x_match_ratio = _MATCH_THRESHOLD
        self.y_stub_ratio = _STUB_THRESHOLD
        self.z_template_match = _TEMPLATE_THRESHOLD
        self.stub_transform_ratio = _SMART_STUB_TRANSFORM_THRESHOLD
        self.saved_calls_min = _SMART_SAVED_CALLS_MIN
        self.template_strong_match = _SMART_TEMPLATE_STRONG_THRESHOLD
        self.last_reason = ""

    def decide(self, metrics: dict) -> SyncStrategy:
        """
        Entscheidungslogik gemäß Fachspezifikation:
        1. Quota-Schutz (Stub-Ratio) ist Bedingung für Turbo.
        2. Template ist kontrollierte mittlere Stufe.
        3. Safe-Mode ist immer verfügbarer Fallback.
        """
        source_count = metrics.get("source_snapshots", 0)
        stub_ratio = metrics.get("stub_ratio", 0.0)
        template_match = metrics.get("template_match", 0.0)
        template_exists = metrics.get("template_exists", False)
        saved_calls = int(metrics.get("saved_calls", 0))
        cleanup_calls = int(metrics.get("cleanup_calls", 0))

        # Fall 1: Nur ein Snapshot -> SAFE-MODE
        if source_count <= 1:
            self.last_reason = "initial_upload"
            return SyncStrategy.SAFE_MODE

        # Fall 2: Harter Quota-Schutz + Transformations-Run
        if stub_ratio < self.stub_transform_ratio:
            self.last_reason = "transformation_to_stubs"
            return SyncStrategy.SAFE_MODE

        # Fall 3: Effizienz-Gate via absolute Call-Last
        if saved_calls > cleanup_calls and saved_calls >= self.saved_calls_min:
            self.last_reason = "api_efficiency_gain"
            return SyncStrategy.TURBO_MODE

        # Fall 4: Template als kontrollierter Mittelweg
        if template_exists and template_match >= self.template_strong_match:
            self.last_reason = "template_fallback"
            return SyncStrategy.TEMPLATE_DELTA_SAFE

        # Fall 5: Fallback
        self.last_reason = "default_safe"
        return SyncStrategy.SAFE_MODE

    def log_decision(self, strategy: SyncStrategy, metrics: dict, snapshot_name: str):
        """Schreibt eine maschinenlesbare Log-Zeile für Auditing."""
        m = metrics
        log_line = (
            f"[decision] mode={strategy.value} | "
            f"reason={self.last_reason} | "
            f"src_count={m.get('source_snapshots')} | "
            f"match={m.get('match_ratio'):.3f} (target>={self.x_match_ratio}) | "
            f"stub={m.get('stub_ratio'):.3f} (transform_if<{self.stub_transform_ratio}) | "
            f"tmpl_match={m.get('template_match'):.3f} (target>={self.template_strong_match}) | "
            f"match_count={m.get('match_count', 0)} | "
            f"saved_calls={m.get('saved_calls', 0)} | "
            f"cleanup_calls={m.get('cleanup_calls', 0)} | "
            f"upload_calls={m.get('upload_calls', 0)} | "
            f"upload_bytes={m.get('upload_bytes', 0)} | "
            f"identical={m.get('identical_count', 0)} | "
            f"new={m.get('new_count', 0)} | "
            f"changed={m.get('changed_count', 0)} | "
            f"deleted={m.get('deleted_count', 0)} | "
            f"tmpl_exists={m.get('template_exists')} | "
            f"basis={m.get('basis_snapshot')}"
        )
        _log(log_line)


# ============================================================================
# FOLDER TEMPLATE MANAGEMENT
# ============================================================================


def _load_template_manifest(archive_dir: str) -> Optional[dict]:
    """
    Lädt das lokale Template-Manifest (gespeichert nach Upload).
    Gibt None zurück wenn nicht vorhanden oder ungültig.
    
    Returns:
        dict mit {"folders": [...], "template_path": "...", ...} oder None
    """
    path = os.path.join(archive_dir, "folder_template_manifest.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Validierung: muss folders-Liste haben
        if not isinstance(data.get("folders"), list):
            return None
        return data
    except Exception:
        return None


def _save_template_manifest(archive_dir: str, template_path: str, folders: set, source_snapshot: str) -> None:
    """
    Aktualisiert das lokale Template-Manifest nach einem Upload.
    
    Args:
        archive_dir: /srv/pcloud-archive oder via PCLOUD_ARCHIVE_DIR
        template_path: /Backup/rtb_1to1/_folder_template
        folders: Set der Ordner-relpaths (was im Template IST)
        source_snapshot: Snapshot-Name (z.B. "2026-04-27-173201")
    """
    path = os.path.join(archive_dir, "folder_template_manifest.json")
    os.makedirs(archive_dir, exist_ok=True)
    
    data = {
        "template_path": template_path,
        "source_snapshot": source_snapshot,
        "updated_at": time.time(),
        "folder_count": len(folders),
        "folders": sorted(folders),
    }
    
    try:
        import tempfile
        dir_path = os.path.dirname(path)
        with tempfile.NamedTemporaryFile(mode="w", dir=dir_path, delete=False, suffix=".tmp", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            tmp = f.name
        os.replace(tmp, path)
    except Exception as e:
        _log(f"[template] Manifest-Speicherung fehlgeschlagen: {e}")


def _get_sync_metrics(cfg: dict, manifest: dict, dest_root: str, archive_dir: str) -> dict:
    """Berechnet Kennzahlen für die Strategiewahl."""
    metrics = {
        "source_snapshots": 0,
        "match_ratio": 0.0,
        "stub_ratio": 0.0,
        "template_match": 0.0,
        "template_exists": False,
        "basis_snapshot": None
    }

    # 1. Source Snapshots zählen (Lokal im Archiv)
    if os.path.exists(archive_dir):
        manifests = [f for f in os.listdir(archive_dir) if f.endswith(".json")]
        metrics["source_snapshots"] = len(manifests)
        
        # Basis-Manifest ermitteln (letztes vor dem aktuellen)
        current_name = manifest.get("snapshot")
        other_manifests = sorted([m.replace(".json", "") for m in manifests if m.replace(".json", "") != current_name])
        if other_manifests:
            basis_name = other_manifests[-1]
            metrics["basis_snapshot"] = basis_name
            
            # Basis laden und Match/Stub Ratio berechnen
            try:
                with open(f"{archive_dir}/{basis_name}.json", "r") as f:
                    basis_manifest = json.load(f)
                
                # Sets von (relpath, size, mtime) für Match-Check
                curr_items = { (it.get("relpath"), it.get("size"), it.get("mtime")) 
                              for it in manifest.get("items", []) if it.get("type") == "file" }
                basis_items = { (it.get("relpath"), it.get("size"), it.get("mtime")) 
                               for it in basis_manifest.get("items", []) if it.get("type") == "file" }
                
                if curr_items:
                    intersection = curr_items & basis_items
                    metrics["match_ratio"] = len(intersection) / len(curr_items)
                    
                    # Stub-Ratio: wie viele curr_items existieren (nach relpath) in basis_items?
                    basis_paths = { it.get("relpath") for it in basis_manifest.get("items", []) if it.get("type") == "file" }
                    curr_paths = { it.get("relpath") for it in manifest.get("items", []) if it.get("type") == "file" }
                    stub_matches = curr_paths & basis_paths
                    metrics["stub_ratio"] = len(stub_matches) / len(curr_paths)
            except Exception:
                pass

    # 2. Template Check (Remote auf pCloud)
    template_path = f"{dest_root.rstrip('/')}/{_FOLDER_TEMPLATE_DIRNAME}"
    try:
        template_md = pc.stat_file(cfg, path=template_path, with_checksum=False)
        if template_md and template_md.get("isfolder"):
            metrics["template_exists"] = True
            
            # Template Match (aus lokalem Cache-Manifest)
            cached_tmpl = _load_template_manifest(archive_dir)
            if cached_tmpl:
                tmpl_folders = set(cached_tmpl.get("folders", []))
                curr_folders = { it.get("relpath").rstrip("/") for it in manifest.get("items", []) 
                                if it.get("type") == "dir" and it.get("relpath") }
                if curr_folders:
                    shared = tmpl_folders & curr_folders
                    metrics["template_match"] = len(shared) / len(curr_folders)
    except Exception:
        pass

    return metrics


def _get_sync_metrics_unified(cfg: dict, manifest: dict, dest_root: str, archive_dir: str) -> dict:
    """
    Einheitliche Kennzahlenberechnung für alle Entscheidungen.
    IMMER aus derselben Quelle (Index + Manifest-Archive).
    """
    metrics = {
        "source_snapshots": 0,
        "match_ratio": 0.0,
        "stub_ratio": 0.0,  # ECHT aus Index, nicht Pfad-Proxy
        "template_match": 0.0,
        "template_exists": False,
        "basis_snapshot": None,
        "total_files": 0,
        "basis_total_files": 0,
        "match_count": 0,
        "identical_count": 0,
        "new_count": 0,
        "changed_count": 0,
        "deleted_count": 0,
        "saved_calls": 0,
        "cleanup_calls": 0,
        "upload_calls": 0,
        "upload_bytes": 0,
    }

    # Aktuelle Dateimenge aus Manifest (fixer Ausgangswert)
    current_files = {
        it.get("relpath"): it
        for it in (manifest.get("items") or [])
        if it.get("type") == "file" and it.get("relpath")
    }
    metrics["total_files"] = len(current_files)

    # 1. Source-Snapshots aus dem RICHTIGEN Pfad
    manifests_dir = os.path.join(archive_dir, "manifests")
    if os.path.exists(manifests_dir):
        # Zähle nur Snapshots mit abgelautenem .json
        manifests = [f for f in os.listdir(manifests_dir) if f.endswith(".json")]
        metrics["source_snapshots"] = len(manifests)
        
        # Basis: neuestes Manifest OHNE aktuellen Snapshot
        current_name = manifest.get("snapshot")
        valid_bases = sorted([m.replace(".json", "") 
                             for m in manifests 
                             if m.replace(".json", "") != current_name])
        
        if valid_bases:
            basis_name = valid_bases[-1]
            metrics["basis_snapshot"] = basis_name

            # 2a. Delta-Zahlen aus Manifest-Diff-Logik (identical/new/changed/deleted)
            basis_manifest_path = os.path.join(manifests_dir, f"{basis_name}.json")
            try:
                with open(basis_manifest_path, "r", encoding="utf-8") as f:
                    basis_manifest = json.load(f)

                basis_files = {
                    it.get("relpath"): it
                    for it in (basis_manifest.get("items") or [])
                    if it.get("type") == "file" and it.get("relpath")
                }

                metrics["basis_total_files"] = len(basis_files)

                current_paths = set(current_files.keys())
                basis_paths = set(basis_files.keys())

                new_paths = current_paths - basis_paths
                deleted_paths = basis_paths - current_paths
                common_paths = current_paths & basis_paths

                identical_count = 0
                changed_count = 0

                for relpath in common_paths:
                    curr_item = current_files[relpath]
                    base_item = basis_files[relpath]
                    curr_sha = (curr_item.get("sha256") or "").lower()
                    base_sha = (base_item.get("sha256") or "").lower()
                    curr_mtime = curr_item.get("mtime")
                    base_mtime = base_item.get("mtime")
                    if curr_sha == base_sha and curr_mtime == base_mtime:
                        identical_count += 1
                    else:
                        changed_count += 1

                new_count = len(new_paths)
                deleted_count = len(deleted_paths)

                metrics["identical_count"] = identical_count
                metrics["match_count"] = identical_count
                metrics["new_count"] = new_count
                metrics["changed_count"] = changed_count
                metrics["deleted_count"] = deleted_count

                # Smart-Strategy 2.0 Kernmetriken
                metrics["saved_calls"] = identical_count
                metrics["cleanup_calls"] = deleted_count + changed_count
                metrics["upload_calls"] = new_count + changed_count

                changed_paths = {
                    relpath
                    for relpath in common_paths
                    if (
                        (current_files[relpath].get("sha256") or "").lower()
                        != (basis_files[relpath].get("sha256") or "").lower()
                        or current_files[relpath].get("mtime") != basis_files[relpath].get("mtime")
                    )
                }
                upload_bytes = 0
                for relpath in (new_paths | changed_paths):
                    sz = current_files.get(relpath, {}).get("size")
                    if isinstance(sz, (int, float)):
                        upload_bytes += int(sz)
                metrics["upload_bytes"] = upload_bytes

                if metrics["total_files"] > 0:
                    metrics["match_ratio"] = identical_count / metrics["total_files"]
            except Exception as e:
                _log(f"[metrics] Warnung: Konnte Basis-Manifest nicht auswerten: {e}")
            
            # 2b. Stub-Ratio aus UNIFIED SOURCE (Remote Index via load_content_index)
            # NICHT aus lokalem Manifest, sondern aus echter pCloud-Index
            try:
                snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
                index = load_content_index(cfg, snapshots_root)  # Authoritative Quelle
                
                # Stub-Ratio EXAKT wie in delta-copy (via _compute_snapshot_stub_ratio)
                _, _, stub_ratio = _compute_snapshot_stub_ratio(index, basis_name)
                metrics["stub_ratio"] = stub_ratio
            except Exception as e:
                _log(f"[metrics] Warnung: Konnte Index nicht laden: {e}")
                metrics["stub_ratio"] = 0.0
    
    # 3. Template Check (wie gehabt, aber konsistent)
    template_path = f"{dest_root.rstrip('/')}/{_FOLDER_TEMPLATE_DIRNAME}"
    try:
        pc.stat_file(cfg, path=template_path, with_checksum=False)
        metrics["template_exists"] = True
        
        # Template Match aus Live-Zustand
        template_folders = _list_remote_folders_from_template(cfg, template_path)
        manifest_folders = _get_manifest_folders(manifest)
        if manifest_folders:
            metrics["template_match"] = len(template_folders & manifest_folders) / len(manifest_folders)
    except Exception:
        pass

    return metrics


def _list_remote_folders_from_template(cfg: dict, template_path: str) -> set:
    """
    Lädt AKTUELLEN Ordner-Zustand des Templates von pCloud (IST-Zustand).
    
    WICHTIG: pCloud ist Single Source of Truth - nicht das lokale Manifest!
    Das lokale Manifest wird nur zum Speichern verwendet, nicht zum Diff.
    
    Args:
        cfg: pCloud Config
        template_path: /Backup/rtb_1to1/_folder_template
        
    Returns:
        Set von Ordner-relpaths (was WIRKLICH im Template ist)
        Leeres Set wenn Template nicht existiert
    """
    try:
        result = pc.call_with_backoff(
            pc.listfolder, cfg,
            path=template_path,
            recursive=True,
            nofiles=True
        )
    except Exception as e:
        if "2005" in str(e) or "not found" in str(e).lower():
            return set()  # Template existiert noch nicht
        raise

    folders = set()

    def _collect(obj: dict, parent: str = "") -> None:
        for child in (obj.get("contents") or []):
            if not child.get("isfolder"):
                continue
            name = child.get("name", "")
            relpath = f"{parent}/{name}" if parent else name
            folders.add(relpath)
            _collect(child, relpath)

    _collect(result.get("metadata") or {})
    return folders


def finalize_index_fileids(cfg, snapshots_root):
    """
    Lädt <snapshots_root>/_index/content_index.json und füllt fehlende fileids
    (für Nodes mit anchor_path) via REST /stat nach. Schreibt nur bei Änderungen.
    Return: Anzahl reparierter Einträge.
    """
    start = time.time()

    idx_path = f"{pc._norm_remote_path(snapshots_root).rstrip('/')}/_index/content_index.json"
    try:
        index = json.loads(pc.get_textfile(cfg, path=idx_path))
    except Exception:
        return 0
    if not isinstance(index, dict):
        return 0

    items = index.get("items", {})
    if not isinstance(items, dict) or not items:
        return 0

    repaired = 0
    changed  = False

    # Gemeinsamer Cache mit dem Modul-Cache teilen:
    global _fid_cache_shared
    cache = _fid_cache_shared

    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        if (node.get("fileid") in (None, "")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=cache)
            if fid:
                node["fileid"] = fid
                repaired += 1
                changed = True

    if changed:
        try:
            pc.put_textfile(cfg, path=idx_path, text=json.dumps(index, ensure_ascii=False, indent=2))
        except Exception:
            pc.write_json_at_path(cfg, path=idx_path, obj=index)

    if os.environ.get("PCLOUD_TIMING") == "1":
        print(f"[timing] finalize_index_fileids: fixed={repaired}, total={time.time()-start:.2f}s")

    return repaired

def _batch_ensure_paths(cfg: dict, paths: list[str], *, dry: bool = False) -> None:
    """
    Batch-Version von ensure_parent_dirs für mehrere Pfade.
    Nutzt createfolderrecursive (ein Call pro Parent-Kette).
    """
    if not paths:
        return

    # eindeutige Parents sammeln
    parents = { os.path.dirname(p.rstrip("/")) for p in paths if p }
    # stabile Reihenfolge (kann helfen beim Debug)
    parents = sorted(parents)

    for parent in parents:
        try:
            pc.ensure_path(cfg, parent, dry=dry)
        except Exception:
            # nicht hart abbrechen – idempotent, nächste versuchen
            continue

def _build_folder_cache_from_tree(cfg: dict, root_path: str) -> dict[str, int]:
    """
    Lädt Ordner-Struktur via listfolder (recursive=True, nofiles=True)
    und baut eine Map: {normalized_path: folderid}
    
    Performance:
      - 1× listfolder (recursive) API-Call
      - Statt N× ensure_path/stat für Parent-FolderID-Lookups
      - Typisch: 1 Call statt 1,000+ Calls (999x Reduktion)
    
    Returns:
        dict mapping normalized paths to folderids
        
    Example:
        cache = _build_folder_cache_from_tree(cfg, "/My Cloud/_snapshots/2026-04-17-120000")
        # cache = {"/My Cloud/_snapshots/2026-04-17-120000": 12345,
        #          "/My Cloud/_snapshots/2026-04-17-120000/dir1": 12346, ...}
    """
    try:
        result = pc.listfolder(cfg, path=root_path, recursive=True, nofiles=True)
    except Exception as e:
        # Root existiert noch nicht (erstes Upload) oder Fehler → leere Map
        if "2005" in str(e) or "not found" in str(e).lower():
            return {}
        # Bei anderen Fehlern auch leere Map (defensiv, kein Abbruch)
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[warn] listfolder für Folder-Cache fehlgeschlagen: {e}")
        return {}
    
    cache = {}
    
    def _traverse(node, parent_path=""):
        """Rekursiv alle Ordner aus dem Tree extrahieren"""
        if not isinstance(node, dict):
            return
        
        # Nur Ordner interessieren uns
        if not node.get("isfolder"):
            return
        
        folder_name = node.get("name", "")
        folderid = node.get("folderid")
        
        # Pfad konstruieren
        if parent_path:
            full_path = f"{parent_path}/{folder_name}"
        else:
            # Root-Node: verwende den übergebenen Pfad
            full_path = root_path
        
        # Normalisieren (wichtig für Map-Lookup!)
        normalized = pc._norm_remote_path(full_path)
        
        if folderid:
            cache[normalized] = int(folderid)
        
        # Rekursiv in Kinder eintauchen
        for child in node.get("contents") or []:
            _traverse(child, full_path)
    
    # Start mit metadata (Root-Ordner)
    metadata = result.get("metadata")
    if metadata:
        _traverse(metadata, parent_path="")
    
    return cache

def _batch_write_stubs(cfg: dict, stubs: list[tuple[str, dict]], *, dry: bool = False) -> None:
    """
    Schreibt gesammelte Stubs (.meta.json) in ihre Zielordner (parent folderid + filename).
    'stubs' ist eine Liste von Tuples: (remote_stub_path, payload_dict)
    Pretty-Print via ENV: PCLOUD_PRETTY_JSON=1
    Erweitert Payload um menschenlesbare Felder: format_version, kind, holder_type, mtime_iso
    """
    import datetime

    if not stubs:
        return

    pretty = os.environ.get("PCLOUD_PRETTY_JSON", "0") == "1"
    
    # Progress-Tracking für Stub-Writing (thread-safe)
    _stubs_written = 0
    _stubs_failed = 0
    _stubs_lock = threading.Lock()
    _progress_interval = int(os.environ.get("PCLOUD_STUB_PROGRESS_INTERVAL", "500"))
    _last_progress_pct = 0

    # 1) nach Parent gruppieren
    by_parent: dict[str, list[tuple[str, dict]]] = {}
    for stub_path, payload in stubs:
        parent = os.path.dirname(stub_path.rstrip("/"))
        name = os.path.basename(stub_path)
        by_parent.setdefault(parent, []).append((name, payload))

    # 2) parent-fids auflösen (optimiert: listfolder + selective ensure)
    parent_fids: dict[str, int] = {}
    _total_parents = len(by_parent)
    _cache_hits = 0
    _cache_misses = 0
    _api_calls = 0
    
    # 2a) Batch-Lookup: Lade existierende Ordner-Struktur (1 API-Call)
    #     Extrahiere Snapshot-Root aus erstem Parent-Pfad
    if not dry and by_parent:
        # Snapshot-Root ermitteln (z.B. /My Cloud/_snapshots/2026-04-17-120000)
        first_parent = next(iter(by_parent.keys()))
        # Format: /.../snapshots_root/snapshot_name/... → extrahiere bis snapshot_name
        parts = first_parent.split("/")
        snapshot_root = None
        # Finde _snapshots Index
        try:
            snapshots_idx = parts.index("_snapshots")
            # snapshot_root = alles bis einschließlich snapshot_name (snapshots_idx + 2)
            if len(parts) > snapshots_idx + 1:
                snapshot_root = "/".join(parts[:snapshots_idx + 2])
            else:
                # Zu flache Struktur (kein snapshot_name nach _snapshots)
                _log(f"[stubs][WARN] Snapshot-Root-Extraktion fehlgeschlagen (zu flach): {first_parent}")
                snapshot_root = None
        except (ValueError, IndexError):
            # _snapshots nicht im Pfad gefunden (unerwartete Struktur)
            _log(f"[stubs][WARN] '_snapshots' nicht im Pfad gefunden: {first_parent}")
            snapshot_root = None
        
        # Cache-Build nur wenn snapshot_root valide ist
        if snapshot_root:
            _log(f"[stubs] Lade Ordner-Struktur via listfolder: {snapshot_root}")
            t_cache_start = time.time()
            folder_cache = _build_folder_cache_from_tree(cfg, snapshot_root)
            t_cache_ms = (time.time() - t_cache_start) * 1000.0
            _api_calls += 1  # Ein listfolder-Call
            if folder_cache:
                _log(f"[stubs] ✓ Folder-Cache geladen: {len(folder_cache)} Ordner in {t_cache_ms:.0f}ms")
            else:
                _log(f"[stubs][WARN] Folder-Cache leer nach listfolder (Snapshot existiert noch nicht?)")
        else:
            # Snapshot-Root ungültig → Skip Cache-Build (Legacy-Mode wird unten aktiviert)
            _log(f"[stubs][WARN] Überspringe Cache-Build (ungültige snapshot_root)")
            folder_cache = {}
    else:
        folder_cache = {}
    
    # 2b) Fallback-Detection: Wenn Cache leer ABER viele Parents → Legacy-Mode
    _use_legacy_mode = False
    if not dry and not folder_cache and _total_parents > 10:
        _log(f"[stubs][WARN] Folder-Cache leer ({len(folder_cache)} Einträge) trotz {_total_parents} Parents")
        _log(f"[stubs][WARN] → Fallback zu Legacy-Mode (sequential ensure_path)")
        _log(f"[stubs][WARN] → Erwartet: ~{int(_total_parents * 0.5 / 60)}min statt <5s")
        _use_legacy_mode = True
    
    # 2c) Parent-FIDs: Cache-Lookup (optimiert) oder Legacy-Mode (sequential)
    if _use_legacy_mode:
        _log(f"[stubs] Löse {_total_parents} Parent-FolderIDs auf (Legacy-Modus: sequential ensure_path)...")
    else:
        _log(f"[stubs] Löse {_total_parents} Parent-FolderIDs auf (Cache-Optimiert: {len(folder_cache)} gecacht)...")
    
    for parent in by_parent.keys():
        if dry:
            # im Dry-Run keine REST-Lookups; fiktive fid
            parent_fids[parent] = 0
            continue
        
        # Normalisieren für Cache-Lookup
        normalized_parent = pc._norm_remote_path(parent)
        
        # Cache-Lookup (O(1))
        if normalized_parent in folder_cache:
            parent_fids[parent] = folder_cache[normalized_parent]
            _cache_hits += 1
        else:
            # Cache-Miss: Ordner existiert noch nicht → anlegen
            try:
                fid = pc.ensure_path(cfg, path=parent)
                parent_fids[parent] = int(fid)
                folder_cache[normalized_parent] = int(fid)  # Cache updaten
                _cache_misses += 1
                _api_calls += 1  # Zähle ensure_path als API-Call
            except Exception as e:
                # Bei 2004 (Already exists): FolderID via stat nachziehen
                if "2004" in str(e):
                    try:
                        fid = pc.stat_folderid_fast(cfg, parent)
                        if fid:
                            parent_fids[parent] = int(fid)
                            folder_cache[normalized_parent] = int(fid)
                            _cache_misses += 1
                            _api_calls += 1
                            if os.environ.get("PCLOUD_VERBOSE") == "1":
                                _log(f"[info] Folder {parent} existiert bereits (2004) - FolderID via stat geholt: {fid}")
                        else:
                            _log(f"[warn] Folder {parent} existiert (2004), aber FolderID nicht auflösbar - Stubs werden übersprungen")
                    except Exception as e2:
                        _log(f"[warn] cannot resolve folderid for {parent}: {e} (fallback failed: {e2})")
                else:
                    _log(f"[warn] cannot resolve/ensure folderid for {parent}: {e}")
    
    # Performance-Report
    if not dry:
        _speedup = (_total_parents / _api_calls) if _api_calls > 0 else 0
        _log(f"[stubs] ✓ Parent-FIDs aufgelöst: {_cache_hits} Cache-Hits, {_cache_misses} neu angelegt")
        _log(f"[stubs] ✓ API-Calls: {_api_calls} (statt {_total_parents}) → {_speedup:.0f}x Reduktion")

    # 3) Schreibjobs bauen (nur Parents mit bekannter fid) + Payload anreichern
    tasks: list[tuple[str, str, dict]] = []
    for parent, items in by_parent.items():
        if parent not in parent_fids:
            continue
        for name, payload in items:
            # Payload "menschenfreundlich" erweitern (restore bleibt kompatibel)
            if "format_version" not in payload:
                payload["format_version"] = 1
            if "kind" not in payload:
                payload["kind"] = "stub"
            if "holder_type" not in payload and payload.get("type") == "hardlink":
                payload["holder_type"] = "hardlink"
            
            # mtime_iso hinzufügen falls mtime vorhanden
            mtime = payload.get("mtime")
            if mtime and "mtime_iso" not in payload:
                try:
                    payload["mtime_iso"] = datetime.datetime.fromtimestamp(float(mtime), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass
            
            tasks.append((parent, name, payload))

    if not tasks:
        return

    threads = int(os.environ.get("PCLOUD_STUB_THREADS", "4") or "4")
    total_tasks = len(tasks)
    
    # Start-Meldung
    _log(f"[stubs] Starte Batch-Write: {total_tasks} Stubs mit {threads} Threads...")

    def _upload_one(args: tuple[str, str, dict]):
        nonlocal _stubs_written, _stubs_failed, _last_progress_pct
        parent, name, payload = args
        
        if dry:
            # Pretty-Print auch im Dry-Run für Debug
            if pretty:
                txt = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                print(f"[dry] stub write: {parent}/{name}\n{txt}")
            else:
                print(f"[dry] stub write: {parent}/{name}")
            return True
        
        # Retry-Logik für robuste Stub-Writes (Timeout-Protection)
        try:
            ret = pc.call_with_backoff(
                pc.write_json_to_folderid,
                cfg,
                folderid=parent_fids[parent],
                filename=name,
                obj=payload,
                minify=(not pretty),
                attempts=5,
                max_sleep=30.0
            )
        except Exception as e:
            with _stubs_lock:
                _stubs_failed += 1
            _log(f"[warn] Stub-Write fehlgeschlagen ({_stubs_failed}): {parent}/{name}: {e}")
            return False
        
        # --- metriken: nur bei erfolgreichem write inkrementieren (thread-safe)
        try:
            if ret:
                with _metrics_lock:
                    globals()["MET_STUBS_WRITTEN"] += 1
        except Exception:
            pass
        
        # Progress-Tracking (thread-safe)
        with _stubs_lock:
            _stubs_written += 1
            current_pct = int((_stubs_written / total_tasks) * 100)
            
            # Alle _progress_interval Stubs ODER bei Prozent-Änderung (10%, 20%, ...)
            show_progress = (
                _stubs_written % _progress_interval == 0 or 
                _stubs_written == total_tasks or
                (current_pct % 10 == 0 and current_pct != _last_progress_pct)
            )
            
            if show_progress:
                _last_progress_pct = current_pct
                eta_per_stub = 0.5  # Schätzung: ~0.5s pro Stub
                remaining = (total_tasks - _stubs_written) * eta_per_stub / threads
                eta_str = f"~{int(remaining/60)}min" if remaining > 60 else f"~{int(remaining)}s"
                _log(f"[stubs] {_stubs_written}/{total_tasks} ({current_pct}%) | {eta_str} verbleibend")
        
        return ret

    if threads > 1 and len(tasks) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(_upload_one, tasks))
    else:
        for t in tasks:
            _upload_one(t)
    
    # Abschluss-Meldung mit Fehler-Statistik
    if _stubs_failed > 0:
        _log(f"[warn] {_stubs_failed} Stubs fehlgeschlagen (von {total_tasks})")
    _log(f"[stubs] ✓ {_stubs_written}/{total_tasks} Stubs erfolgreich ({(_stubs_written/total_tasks*100):.1f}%)")

# ----------------- Haupt-Logik -----------------

def push_objects_mode(cfg: dict, manifest: dict, dest_root: str, *, dry: bool, objects_layout: str="two-level") -> None:
    """Hash-Object-Store + Stubs in Snapshot."""
    objects_root   = f"{dest_root.rstrip('/')}/_objects"
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    snapshot       = manifest["snapshot"]
    items          = manifest.get("items") or []

    uploaded = 0; skipped = 0; stubs = 0

    _log(f"[plan] objects={objects_root} snapshot={snapshots_root}/{snapshot}")

    # === Parallel Upload für Objects-Mode ===
    _state_lock = threading.Lock()
    
    # 1) echte Objekte sicherstellen (mit Parallelisierung)
    _file_items = [it for it in items if it.get("type") == "file"]
    
    def _upload_object(it: dict) -> None:
        """Upload ein Objekt (thread-safe)"""
        nonlocal uploaded, skipped
        
        sha = it.get("sha256")
        ext = (it.get("ext") or "").lstrip(".")
        if not sha:
            print(f"[warn] file ohne sha256: {it.get('relpath')}", file=sys.stderr)
            return

        obj_path = object_path_for(objects_root, sha, ext, layout=objects_layout)
        
        # stat_file_safe ist thread-safe (nur lesen)
        md = stat_file_safe(cfg, path=obj_path)
        
        if md:
            with _state_lock:
                skipped += 1
        else:
            if dry:
                print(f"[dry] upload object: {obj_path}  <- {it.get('source_path')}")
            else:
                ensure_parent_dirs(cfg, obj_path, dry=False)
                _upload_file_smart(cfg, it["source_path"], obj_path, dry=dry)
            
            with _state_lock:
                uploaded += 1
    
    # Files klassifizieren (small vs large)
    _small_objects = [f for f in _file_items if (f.get("size") or 0) < SMALL_FILE_THRESHOLD_BYTES]
    _large_objects = [f for f in _file_items if (f.get("size") or 0) >= SMALL_FILE_THRESHOLD_BYTES]
    
    if _small_objects and _large_objects:
        _log(f"[objects] {len(_small_objects)} kleine Objekte parallel, {len(_large_objects)} große sequentiell")
    elif _small_objects:
        _log(f"[objects] {len(_small_objects)} kleine Objekte parallel")
    else:
        _log(f"[objects] {len(_large_objects)} große Objekte sequentiell")
    
    # Kleine Objekte parallel
    if _small_objects and PARALLEL_UPLOAD_THREADS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_UPLOAD_THREADS) as ex:
            list(ex.map(_upload_object, _small_objects))
    else:
        for obj in _small_objects:
            _upload_object(obj)
    
    # Große Objekte sequentiell
    for obj in _large_objects:
        _upload_object(obj)
    # === Ende Parallel Upload (Objects-Mode) ===

    print(f"objects: uploaded={uploaded} skipped={skipped}")
    
    # Update global metrics (thread-safe)
    try:
        with _metrics_lock:
            globals()["MET_UPLOADED_FILES"] += uploaded
    except Exception:
        pass

    # 2) Snapshot-Stubs erzeugen
    for it in items:
        if it.get("type") != "file": continue
        sha = it.get("sha256")
        ext = (it.get("ext") or "").lstrip(".")
        obj_path = object_path_for(objects_root, sha, ext, layout=objects_layout)
        stub_remote = stub_path_for(snapshots_root, snapshot, it["relpath"])
        payload = {
            "type": "link",
            "sha256": sha,
            "size": it.get("size"),
            "mtime": it.get("mtime"),
            "object_path": obj_path,
            "ext": ext or None,
            "inode": it.get("inode"),
            "snapshot": snapshot,
            "relpath": it.get("relpath"),
        }
        upload_json_stub(cfg, stub_remote, payload, dry=dry)
        stubs += 1

    print(f"stubs: {stubs} (snapshot={snapshot})")
    
    # Update global metrics (thread-safe)
    try:
        with _metrics_lock:
            globals()["MET_STUBS_WRITTEN"] += stubs
    except Exception:
        pass


def ensure_snapshots_layout(cfg: dict, dest_root: str, *, dry: bool = False) -> None:
    """
    Stellt sicher, dass <dest_root>/_snapshots und _snapshots/_index existieren
    und dass eine leere Index-Datei angelegt werden kann.
    """
    snapshots_root = f"{pc._norm_remote_path(dest_root).rstrip('/')}/_snapshots"
    index_dir = f"{snapshots_root}/_index"
    if dry:
        print(f"[dry] ensure: {snapshots_root}")
        print(f"[dry] ensure: {index_dir}")
        return
    pc.ensure_path(cfg, snapshots_root)
    pc.ensure_path(cfg, index_dir)

def push_1to1_mode(cfg, manifest, dest_root, *, dry=False, verbose=False, manifest_path=None, 
                   strategy_mode="SAFE-MODE"):
    """
    1:1-Modus mit Resume-Unterstützung.
    Entscheidung zwischen TURBO/TEMPLATE/SAFE erfolgt EXTERN in push_1to1_smart_controller.
    
    Args:
        strategy_mode: "SAFE-MODE", "TEMPLATE-DELTA-SAFE", oder "TURBO-MODE" (wird von Controller gesetzt)
    """
    t_phase_start = time.time()
    ensure_ms = 0.0
    upload_ms = 0.0
    write_ms  = 0.0

    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    
    # Kein Decision-Block mehr hier!
    # Nur noch Execution basierend auf strategy_mode
    force_safe = (strategy_mode == "SAFE-MODE")
    template_force_active = (strategy_mode == "TEMPLATE-DELTA-SAFE")
    
    # === Timeout-Protection für Mass-Uploads ===
    # Ensure minimum timeout (kritisch bei 19k+ Stub-Writes)
    if "timeout" not in cfg or cfg.get("timeout", 0) < 30:
        cfg["timeout"] = int(os.environ.get("PCLOUD_TIMEOUT", "60"))
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[config] Timeout auf {cfg['timeout']}s gesetzt (Mass-Upload-Protection)")


    # === NEU: Upload-Status-Marker ===
    marker_started = f"{dest_snapshot_dir}/.upload_started"
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    
    # Prüfen ob unvollständiger Upload existiert
    incomplete_upload = False
    try:
        pc.stat_file(cfg, path=marker_started, with_checksum=False)
        # Started-Marker existiert
        try:
            pc.stat_file(cfg, path=marker_complete, with_checksum=False)
            # Complete-Marker auch da → Upload war erfolgreich
            _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
            return {"uploaded": 0, "stubs": 0, "resumed": False}
        except:
            # Nur Started, kein Complete → unvollständig!
            incomplete_upload = True
            _log(f"[warn] Unvollständiger Upload erkannt für {snapshot_name} - starte neu")
    except:
        # Kein Started-Marker → frischer Upload
        pass
    
    # Bei unvollständigem Upload: Index-Driven Skip (keine Löschung)
    if incomplete_upload:
        _log(f"[resume] Setze Upload fort für {snapshot_name} (bereits verarbeitete Dateien werden übersprungen)")
    # === ENDE NEU ===

    _log(f"[plan] 1to1 snapshot={dest_snapshot_dir}")

    # === NEU: Started-Marker setzen ===
    if not dry:
        try:
            pc.call_with_backoff(pc.ensure_path, cfg, dest_snapshot_dir)
            pc.call_with_backoff(pc.put_textfile, cfg, path=marker_started,
                          text=json.dumps({
                              "snapshot": snapshot_name,
                              "started_at": time.time(),
                              "host": os.uname().nodename
                          }))
            _log(f"[info] Upload-Started-Marker gesetzt: {marker_started}")
        except Exception as e:
            _log(f"[warn] Konnte Started-Marker nicht setzen: {e}")
    # === ENDE NEU ===

    # Lock für shared state (muss VOR Hilfsfunktionen definiert werden!)
    _state_lock = threading.Lock()

    # --- kleine Helfer ---
    def _ensure(path: str) -> None:
        nonlocal ensure_ms
        if not path:
            return
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] ensure: {path}")
            return
        t0 = time.time()
        pc.call_with_backoff(pc.ensure_path, cfg, path)
        with _state_lock:
            ensure_ms += (time.time() - t0) * 1000.0

    def _delete_if_exists(path: str) -> None:
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] delete-if-exists: {path}")
            return
        try:
            md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=path) or {}
            fid = md.get("fileid")
            if fid:
                pc.delete_file(cfg, fileid=int(fid))
        except Exception:
            pass

    # Lokaler Index-Cache-Pfad (nur während Upload, wird am Ende hochgeladen)
    import tempfile
    _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
    _local_index_path = os.path.join(_local_index_dir, f"pcloud_index_{snapshot_name}.json")
    os.makedirs(_local_index_dir, exist_ok=True)

    # Index laden: erst lokal (falls vorhanden), sonst von pCloud
    if os.path.exists(_local_index_path):
        _log(f"[resume] Lade lokalen Index: {_local_index_path}")
        index = load_content_index_local(_local_index_path)
    else:
        index = load_content_index(cfg, snapshots_root)
    items = index.setdefault("items", {})

    # Anchor-Cache aufbauen
    known_anchors = {}
    for sha, node in items.items():
        ap = node.get("anchor_path")
        fid = node.get("fileid")
        if ap and fid:
            known_anchors[sha] = (ap, fid)
    
    if known_anchors and os.environ.get("PCLOUD_VERBOSE") == "1":
        print(f"[prefetch] {len(known_anchors)} bekannte Anchors gecacht")

    # Hilfstabellen
    seen_inodes: dict[tuple[int,int], str] = {}
    uploaded = 0
    resumed = 0   # Bereits im Index für diesen Snapshot
    stubs = 0
    index_changed = False
    stubs_to_write: list[tuple[str, dict]] = []

    # --- Upload-Hilfsroutine ---
    def _upload_real_file(abs_src: str, dst_path: str) -> tuple:
        """Returns (fileid, pcloud_hash)"""
        nonlocal upload_ms
        parent = os.path.dirname(dst_path.rstrip("/"))
        if parent:
            _ensure(parent)
        if dry:
            print(f"[dry] upload 1to1: {dst_path}  <- {abs_src}")
            return (None, None)

        # Progress-Hinweis für große Dateien
        file_size = os.path.getsize(abs_src)
        if file_size > 100 * 1024**2:  # > 100MB
            print(f"[upload] Starte Upload: {os.path.basename(dst_path)} ({file_size/1024**2:.1f} MB)", flush=True)

        t0 = time.time()
        res = _upload_file_smart(cfg, abs_src, dst_path, dry=dry)
        elapsed_ms = (time.time() - t0) * 1000.0
        
        # Thread-safe metrics update
        with _state_lock:
            upload_ms += elapsed_ms
        
        # Global metrics (thread-safe)
        try:
            with _metrics_lock:
                globals()["MET_UPLOADED_FILES"] += 1
        except Exception:
            pass

        # fileid + hash aus der Upload-Antwort
        try:
            md = (res or {}).get("metadata") or {}
            fileid = md.get("fileid")
            pcloud_hash = md.get("hash")  # pCloud's hash field
        except Exception:
            fileid = None
            pcloud_hash = None

        # Optional: Eager-FileID via stat, falls Upload keine liefert
        if (not fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
            try:
                stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=dst_path) or {}
                if not fileid:
                    fileid = stat_md.get("fileid")
                if not pcloud_hash:
                    pcloud_hash = stat_md.get("hash")
            except Exception:
                pass

        return (fileid, pcloud_hash)

    # --- Stub sammeln ---
    def _queue_stub(relpath: str, file_item: dict, node: dict) -> None:
        nonlocal stubs, index_changed

        eager = os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0"
        if eager and (not node.get("fileid")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=_fid_cache_shared)
            if fid:
                node["fileid"] = fid
                index_changed = True

        meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
        payload = {
            "type": "hardlink",
            "sha256": file_item.get("sha256"),
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "snapshot": snapshot_name,
            "relpath": relpath,
            "anchor_path": node.get("anchor_path"),
            "fileid": node.get("fileid") if node.get("fileid") is not None else None,
            "inode": file_item.get("inode"),
        }
        if dry:
            print(f"[dry] write stub: {meta_path}")
        else:
            stubs_to_write.append((meta_path, payload))
        stubs += 1

    # --- Hauptschleife: Items des Manifests ---
    _all_items = [it for it in (manifest.get("items") or []) if it.get("type") == "file"]
    _total_items = len(_all_items)
    _total_size = sum(it.get("size") or 0 for it in _all_items)
    _done_items = 0
    _done_size = 0
    _t_loop_start = time.time()
    _t_last_progress = _t_loop_start
    _PROGRESS_INTERVAL = float(os.environ.get("PCLOUD_PROGRESS_INTERVAL", "30"))
    _SAVE_INTERVAL = int(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL", "100"))
    _SAVE_INTERVAL_TIME = float(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL_TIME", "300"))  # 5min
    _last_saved_count = 0
    _t_last_index_save = time.time()
    _log(f"[push] Starte Upload: {_total_items} Dateien, {_total_size/1024**3:.2f} GB")

    # === Folder-Template-basierte Ordner-Anlage ===
    # Manifest-Ordner sammeln (was sein SOLLTE)
    manifest_folders = set()
    for it in manifest.get("items") or []:
        if it.get("type") == "dir":
            relpath = it.get("relpath", "").rstrip("/")
            if relpath:  # Filter leere Strings (Root-Verzeichnis)
                manifest_folders.add(relpath)
    
    # Template-Pfade
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    template_path = f"{dest_root.rstrip('/')}/{_FOLDER_TEMPLATE_DIRNAME}"
    
    # Prüfe ob Template existiert (via stat)
    template_exists = False
    try:
        template_md = pc.stat_file(cfg, path=template_path, with_checksum=False)
        template_exists = bool(template_md and template_md.get("isfolder"))
    except Exception:
        pass
    
    # Entscheidung: Template nutzen oder Einzeln anlegen?
    template_used = False
    
    if template_exists and template_force_active:
        # === Template-basierte Anlage (SCHNELL!) ===
        # 1. IST-Zustand von pCloud laden (pCloud = Single Source of Truth!)
        _log(f"[template] Lade aktuellen Template-Zustand von pCloud...")
        t_load = time.time()
        template_folders = _list_remote_folders_from_template(cfg, template_path)
        _log(f"[template] Template hat {len(template_folders)} Ordner ({time.time()-t_load:.1f}s)")
        
        if template_folders:
            # 2. Diff berechnen (lokal, OHNE API-Call!)
            to_add = manifest_folders - template_folders       # Neue Ordner
            to_delete = template_folders - manifest_folders    # Überflüssige
            shared = manifest_folders & template_folders       # Identisch
            
            _log(f"[template] Überlapp: {len(shared)}/{len(manifest_folders)} ({len(shared)/len(manifest_folders)*100:.0f}%)")
            _log(f"[template] Delta: +{len(to_add)} neue, -{len(to_delete)} überflüssige")
            
            # 3. Template kopieren (1 API-Call statt N!)
            _log(f"[template] Kopiere Template → {snapshot_name} ...")
            try:
                if not dry:
                    dest_snapshot_fid = pc.call_with_backoff(pc.ensure_path, cfg, dest_snapshot_dir)
                    pc.call_with_backoff(pc.copyfolder, cfg, from_path=template_path, to_folderid=dest_snapshot_fid, noover=True, copycontentonly=True)
                _log(f"[template] ✓ Template kopiert (~2-5s statt ~5min)")
                template_used = True
                
                # 4. Überflüssige Ordner löschen (tiefste zuerst)
                if to_delete:
                    _log(f"[template] Lösche {len(to_delete)} überflüssige Ordner...")
                    deleted = 0
                    for relpath in sorted(to_delete, key=lambda p: -p.count("/")):
                        try:
                            if not dry:
                                pc.call_with_backoff(pc.delete_folder, cfg, path=f"{dest_snapshot_dir}/{relpath}", recursive=False)
                            deleted += 1
                        except Exception as e:
                            _log(f"[warn] Konnte {relpath} nicht löschen: {e}")
                    _log(f"[template] ✓ {deleted} überflüssige Ordner gelöscht")
                
                # 5. Fehlende Ordner anlegen (parallel)
                if to_add:
                    from collections import defaultdict
                    _log(f"[template] Lege {len(to_add)} fehlende Ordner an...")
                    
                    folders_by_depth = defaultdict(list)
                    for reldir in to_add:
                        folders_by_depth[reldir.count("/")].append(reldir)
                    
                    threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
                    created = 0
                    for depth in sorted(folders_by_depth.keys()):
                        batch = folders_by_depth[depth]
                        if threads > 1 and len(batch) > 1 and not dry:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                                results = list(ex.map(
                                    lambda p: pc.call_with_backoff(pc.ensure_path, cfg, f"{dest_snapshot_dir}/{p}"),
                                    batch
                                ))
                                created += len([r for r in results if r])
                        else:
                            for reldir in batch:
                                if not dry:
                                    pc.call_with_backoff(pc.ensure_path, cfg, f"{dest_snapshot_dir}/{reldir}")
                                created += 1
                    _log(f"[template] ✓ {created} neue Ordner angelegt")
                
                # 6. Template aktualisieren (wenn Struktur sich änderte)
                if (to_add or to_delete) and not dry:
                    _log("[template] Aktualisiere Template mit neuer Struktur...")
                    try:
                        pc.call_with_backoff(pc.delete_folder, cfg, path=template_path, recursive=True)
                        template_fid = pc.call_with_backoff(pc.ensure_path, cfg, template_path)
                        pc.call_with_backoff(pc.copyfolder, cfg, from_path=dest_snapshot_dir, to_folderid=template_fid, noover=True, copycontentonly=True)
                        _save_template_manifest(archive_dir, template_path, manifest_folders, snapshot_name)
                        _log("[template] ✓ Template aktualisiert")
                    except Exception as e:
                        _log(f"[warn] Template-Update fehlgeschlagen: {e}")
                
            except Exception as e:
                _log(f"[warn] Template-Nutzung fehlgeschlagen: {e} – Fallback zu Einzeln-Anlage")
                template_used = False
    
    # Fallback: Ordner einzeln anlegen (wenn kein Template oder Template-Fehler)
    if not template_used:
        # Remote-Ordner sammeln (was IST bereits da)
        remote_folders = set()
        try:
            _log(f"[plan] Lade Remote-Ordnerstruktur: {dest_snapshot_dir}")
            result = pc.call_with_backoff(pc.listfolder, cfg, path=dest_snapshot_dir, recursive=True, nofiles=True)
            def _collect_folders(obj, parent_path=""):
                if isinstance(obj, dict) and obj.get("isfolder"):
                    folder_name = obj.get("name", "")
                    folder_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                    remote_folders.add(folder_path)
                    for child in obj.get("contents") or []:
                        _collect_folders(child, folder_path)
            metadata = result.get("metadata") or {}
            for child in metadata.get("contents") or []:
                _collect_folders(child, "")
            _log(f"[plan] {len(remote_folders)} Remote-Ordner gefunden")
        except Exception as e:
            if "2005" in str(e) or "not found" in str(e).lower():
                _log(f"[plan] Snapshot-Ordner existiert noch nicht (erstes Upload)")
            else:
                _log(f"[warn] listfolder fehlgeschlagen: {e}")
        
        # Differenz berechnen
        missing_folders = manifest_folders - remote_folders
        
        if missing_folders:
            from collections import defaultdict
            _log(f"[plan] Lege {len(missing_folders)} fehlende Ordner an (von {len(manifest_folders)} gesamt)")
            
            folders_by_depth = defaultdict(list)
            for reldir in missing_folders:
                folders_by_depth[reldir.count("/")].append(reldir)
            
            max_depth = max(folders_by_depth.keys()) if folders_by_depth else 0
            threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
            
            _folders_created = 0
            _folders_lock = threading.Lock()
            _last_progress_pct = 0
            _folders_start_time = time.time()
            total_folders = len(missing_folders)
            
            def _create_folder(reldir: str) -> bool:
                nonlocal _folders_created, _last_progress_pct
                try:
                    _ensure(f"{dest_snapshot_dir}/{reldir}")
                    with _folders_lock:
                        _folders_created += 1
                        current_pct = int((_folders_created / total_folders) * 100)
                        show_progress = (
                            _folders_created % 100 == 0 or
                            _folders_created == total_folders or
                            (current_pct % 10 == 0 and current_pct != _last_progress_pct)
                        )
                        if show_progress:
                            _last_progress_pct = current_pct
                            # Dynamische ETA: Echte Geschwindigkeit statt hardcoded 0.05s!
                            elapsed = time.time() - _folders_start_time
                            if _folders_created > 0 and elapsed > 0:
                                rate = _folders_created / elapsed  # Ordner pro Sekunde
                                remaining_s = (total_folders - _folders_created) / rate if rate > 0 else 0
                            else:
                                remaining_s = 0
                            eta_str = f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s/60)}min"
                            _log(f"[folders] {_folders_created}/{total_folders} ({current_pct}%) | {eta_str} verbleibend")
                    return True
                except Exception as e:
                    print(f"[warn] Ordner-Anlage fehlgeschlagen für {reldir}: {e}", file=sys.stderr)
                    return False
            
            _log(f"[folders] {max_depth + 1} Ebenen, {threads} Threads pro Ebene")
            for depth in sorted(folders_by_depth.keys()):
                folders_at_depth = folders_by_depth[depth]
                if threads > 1 and len(folders_at_depth) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                        list(ex.map(_create_folder, folders_at_depth))
                else:
                    for folder in folders_at_depth:
                        _create_folder(folder)
            
            _log(f"[folders] ✓ {total_folders} Ordner erfolgreich angelegt")
            
            # Initial-Template erstellen (Ordner sind noch leer!)
            if not template_exists and len(manifest_folders) > 50 and not dry:
                _log("[template] Erstelle initiales Template (Ordner sind noch leer)...")
                try:
                    template_fid = pc.call_with_backoff(pc.ensure_path, cfg, template_path)
                    pc.call_with_backoff(pc.copyfolder, cfg, from_path=dest_snapshot_dir, to_folderid=template_fid, noover=True, copycontentonly=True)
                    _save_template_manifest(archive_dir, template_path, manifest_folders, snapshot_name)
                    _log("[template] ✓ Template erstellt für zukünftige Snapshots")
                except Exception as e:
                    _log(f"[warn] Template-Erstellung fehlgeschlagen: {e}")
        else:
            _log(f"[plan] Alle {len(manifest_folders)} Ordner existieren bereits")
    # === Ende Folder-Template-basierte Ordner-Anlage ===

    # === Parallel Upload für kleine Dateien ===
    # (_state_lock bereits oben definiert - wird hier wiederverwendet)
    
    # File-Processing-Funktion (thread-safe)
    def _process_file_item(it: dict) -> None:
        """Verarbeitet ein File-Item (thread-safe für parallele Ausführung)"""
        nonlocal uploaded, resumed, stubs, index_changed, _done_items, _done_size, _t_last_progress
        nonlocal _last_saved_count, _t_last_index_save, upload_ms

        # Progress-Tracking (ohne Lock für Performance, nicht kritisch)
        with _state_lock:
            _done_items += 1
            _done_size += it.get("size") or 0
            _now = time.time()
            if _now - _t_last_progress >= _PROGRESS_INTERVAL:
                _elapsed = _now - _t_loop_start
                _pct = _done_items / _total_items * 100 if _total_items else 0
                _pct_b = _done_size / _total_size * 100 if _total_size else 0
                _eta = (_elapsed / _done_size * (_total_size - _done_size)) if _done_size else 0
                _eta_str = f"~{int(_eta/60)}min" if _eta > 60 else f"~{int(_eta)}s"
                _log(
                    f"[push] {_done_items}/{_total_items} ({_pct:.0f}%) | "
                    f"{_done_size/1024**3:.2f}/{_total_size/1024**3:.2f} GB ({_pct_b:.0f}%) | "
                    f"new_anchors={uploaded} reused={resumed} stubs_queued={stubs} | {_eta_str} verbleibend"
                )
                _t_last_progress = _now

        relpath = it.get("relpath") or ""
        src_abs = it.get("source_path") or ""
        sha = it.get("sha256") or ""
        inode = it.get("inode") or {}
        dev = int(inode.get("dev") or 0)
        ino = int(inode.get("ino") or 0)
        ino_key = (dev, ino)

        dst_path = f"{dest_snapshot_dir}/{relpath}"

        # Index-Zugriff (thread-safe)
        with _state_lock:
            node = items.setdefault(sha, {"holders": []})
            
            # === Index-Driven Skip: Prüfen ob bereits im Index für diesen Snapshot ===
            already_in_snapshot = any(
                h.get("snapshot") == snapshot_name and h.get("relpath") == relpath
                for h in node.get("holders", [])
            )
            if already_in_snapshot:
                # Bereits verarbeitet → skip
                seen_inodes[ino_key] = relpath
                resumed += 1
                return
            
            # Content-SHA auch als Feld im Node mitführen
            if sha and node.get("sha256") != sha:
                node["sha256"] = sha
                index_changed = True
            
            # Anchor-Info aus Cache
            if sha in known_anchors:
                anchor_path, anchor_fid = known_anchors[sha]
                if not node.get("anchor_path"):
                    node["anchor_path"] = anchor_path
                if not node.get("fileid"):
                    node["fileid"] = anchor_fid
            else:
                anchor_path = node.get("anchor_path") or ""
            
            is_anchor_here = (anchor_path == dst_path)

            # Hardlink-Check
            if ino_key in seen_inodes:
                if not is_anchor_here:
                    _queue_stub(relpath, it, node)
                else:
                    _delete_if_exists(f"{dst_path}.meta.json")
                return

        # Upload (außerhalb Lock für Parallelität!)
        fid = None
        pcloud_hash = None
        if not anchor_path:
            fid, pcloud_hash = _upload_real_file(src_abs, dst_path)
            
            # Index-Updates (thread-safe)
            with _state_lock:
                if node.get("anchor_path") != dst_path:
                    node["anchor_path"] = dst_path
                    index_changed = True
                if fid and node.get("fileid") != fid:
                    node["fileid"] = fid
                    index_changed = True
                if pcloud_hash and node.get("pcloud_hash") != pcloud_hash:
                    node["pcloud_hash"] = pcloud_hash
                    index_changed = True
                uploaded += 1
            
            _delete_if_exists(f"{dst_path}.meta.json")
        else:
            with _state_lock:
                if is_anchor_here:
                    resumed += 1
                    _delete_if_exists(f"{dst_path}.meta.json")
                else:
                    _queue_stub(relpath, it, node)

        # Holder registrieren (thread-safe)
        with _state_lock:
            h = {"snapshot": snapshot_name, "relpath": relpath}
            if h not in node["holders"]:
                node["holders"].append(h)
                index_changed = True
            seen_inodes[ino_key] = relpath

            # Periodisches Index-Save (thread-safe)
            _now_save = time.time()
            _count_trigger = _SAVE_INTERVAL > 0 and (uploaded + resumed + stubs) >= _last_saved_count + _SAVE_INTERVAL
            _time_trigger = _SAVE_INTERVAL_TIME > 0 and (_now_save - _t_last_index_save) >= _SAVE_INTERVAL_TIME
            if not dry and (_count_trigger or _time_trigger):
                save_content_index_local(_local_index_path, index)
                _last_saved_count = uploaded + resumed + stubs
                _t_last_index_save = _now_save
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    _reason = "count" if _count_trigger else "time"
                    print(f"[index] Lokal gespeichert ({_reason}) nach {uploaded + resumed + stubs} Dateien")

    # Files klassifizieren (nur echte Files, keine Dirs)
    _file_items = [it for it in (manifest.get("items") or []) if it.get("type") == "file"]
    _small_files = [f for f in _file_items if (f.get("size") or 0) < SMALL_FILE_THRESHOLD_BYTES]
    _large_files = [f for f in _file_items if (f.get("size") or 0) >= SMALL_FILE_THRESHOLD_BYTES]
    
    if _small_files and _large_files:
        _log(f"[parallel] {len(_small_files)} kleine Dateien (< {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) parallel, "
             f"{len(_large_files)} große Dateien sequentiell")
    elif _small_files:
        _log(f"[parallel] {len(_small_files)} kleine Dateien (< {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) parallel")
    else:
        _log(f"[parallel] {len(_large_files)} große Dateien (>= {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) sequentiell")

    # Kleine Dateien parallel hochladen
    if _small_files and PARALLEL_UPLOAD_THREADS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_UPLOAD_THREADS) as ex:
            list(ex.map(_process_file_item, _small_files))
    else:
        for f in _small_files:
            _process_file_item(f)

    # Große Dateien sequentiell (volle Bandbreite pro File)
    for f in _large_files:
        _process_file_item(f)
    # === Ende Parallel Upload ===


    # --- Batch: Stubs & Index schreiben (einmaliges Ensure + Writes) ---
    if not dry and stubs_to_write:
        _log(f"[push] ✓ Loop abgeschlossen. Bereite Stub-Batch vor ({len(stubs_to_write)} Stubs)...")
        t0 = time.time()
        _batch_write_stubs(cfg, stubs_to_write, dry=False)  # sorgt intern für 1x Parent-Ensure
        write_ms += (time.time() - t0) * 1000.0


    # Index schreiben (lokal → pCloud → lokal löschen)
    if dry:
        print(f"[dry] write index: {snapshots_root}/_index/content_index.json (items={len(items)})")
    else:
        # Finaler lokaler Save (falls noch Änderungen seit letztem periodischen Save)
        if index_changed:
            save_content_index_local(_local_index_path, index)
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[index] Finaler lokaler Save vor Upload")
        
        # Index hochladen nach pCloud
        if os.path.exists(_local_index_path):
            t0 = time.time()
            save_content_index(cfg, snapshots_root, index, dry=False)
            dt_ms = (time.time() - t0) * 1000.0
            write_ms += dt_ms 
            print(f"[timing] index_write_ms={int(dt_ms)}")
            
            # Remote archivieren (Paranoia-Modus: Snapshot-isolierter Index für Recovery)
            try:
                idx_path = f"{snapshots_root}/_index/content_index.json"
                archive_path = f"{snapshots_root}/_index/archive/{snapshot_name}_index.json"
                pc.ensure_parent_dirs(cfg, archive_path)
                pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
                _log(f"[index] ✓ Content-Index remote archiviert: {archive_path}")
            except Exception as e:
                _log(f"[index][warn] Remote-Archivierung fehlgeschlagen: {e}")
            
            # Master-Index aktualisieren (alle Snapshots zusammen)
            try:
                master_index_path = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "indexes", "content_index_master.json")
                os.makedirs(os.path.dirname(master_index_path), exist_ok=True)
                save_content_index_local(master_index_path, index)
                _log(f"[index] ✓ Master-Index aktualisiert: {master_index_path}")
            except Exception as e:
                _log(f"[index][warn] Master-Index-Update fehlgeschlagen: {e}")
            
            # Lokale Index-Datei löschen
            try:
                os.remove(_local_index_path)
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    print(f"[index] Lokale Kopie gelöscht: {_local_index_path}")
            except Exception as e:
                print(f"[warn] Konnte lokale Index-Datei nicht löschen: {e}")
            
            # Manifest archivieren (falls Pfad gegeben und Upload erfolgreich)
            if manifest_path and not dry:
                try:
                    archive_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "manifests")
                    os.makedirs(archive_dir, exist_ok=True)
                    archive_path = os.path.join(archive_dir, f"{snapshot_name}.json")
                    
                    import shutil
                    shutil.copy2(manifest_path, archive_path)
                    _log(f"[archive] Manifest archiviert: {archive_path}")
                    
                    # Optional: Index auch archivieren
                    if os.environ.get("PCLOUD_ARCHIVE_INDEX") == "1":
                        index_archive_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "indexes")
                        os.makedirs(index_archive_dir, exist_ok=True)
                        index_src = os.path.join(_local_index_dir, f"pcloud_index_{snapshot_name}.json")
                        if os.path.exists(index_src):
                            index_archive_path = os.path.join(index_archive_dir, f"{snapshot_name}_index.json")
                            shutil.copy2(index_src, index_archive_path)
                            _log(f"[archive] Index archiviert: {index_archive_path}")
                except Exception as e:
                    print(f"[warn] Manifest-Archivierung fehlgeschlagen: {e}")
        else:
            print("[info] index unchanged (no write)")

    # FINALIZE
    if not dry:
        do_finalize = (os.environ.get("PCLOUD_SKIP_FINALIZE") in (None, "", "0"))
        if do_finalize and (uploaded > 0 or stubs > 0 or index_changed):
            try:
                finalize_index_fileids(cfg, snapshots_root)
            except Exception:
                pass

    # === NEU: Complete-Marker setzen ===
    if not dry:
        try:
            pc.put_textfile(cfg, path=marker_complete,
                          text=json.dumps({
                              "snapshot": snapshot_name,
                              "completed_at": time.time(),
                              "uploaded": uploaded,
                              "resumed": resumed,
                              "stubs": stubs
                          }))
            _log(f"[success] Upload-Complete-Marker gesetzt: {marker_complete}")
        except Exception as e:
            _log(f"[ERROR] Konnte Complete-Marker nicht setzen: {e}")
            raise  # CRITICAL: Ohne Marker ist Upload unvollständig!
    # === ENDE NEU ===

    if os.environ.get("PCLOUD_TIMING") == "1":
        total_ms = (time.time() - t_phase_start) * 1000.0
        print(f"[timing] push_1to1: total={total_ms/1000:.2f}s, ensure={ensure_ms:.0f}ms, upload={upload_ms:.0f}ms, writes={write_ms:.0f}ms")

    # Update global metrics (thread-safe)
    try:
        with _metrics_lock:
            globals()["MET_RESUMED_FILES"] += resumed
    except Exception:
        pass

    print(f"1to1: uploaded={uploaded} resumed={resumed} stubs={stubs} (snapshot={snapshot_name})")
    return {"uploaded": uploaded, "resumed": resumed, "stubs": stubs}

def retention_sync_1to1(cfg, dest_root, *, local_snaps=None, dry=False, rewrite_stubs=True):
    """
    Retention/Prune für den 1:1-Modus, index-zentriert.

    Ablauf:
      - Remote-Snapshots unter <dest>/_snapshots mit lokalen (local_snaps) vergleichen.
      - Für jeden entfernten Remote-Snapshot:
          • Holders für gelöschte Snaps entfernen.
          • Liegt Anchor im gelöschten Snap:
              - Gibt es verbleibende Holder -> Anchor serverseitig in Pfad des jüngsten Holders moven,
                Index aktualisieren, Ziel-Stub entfernen, übrige Holder -> Stub (optional).
              - Keine Holder mehr -> Node entfernen.
          • Snapshot-Ordner löschen nur, wenn keine Blocker (z. B. fehlende fileid / Move-Fehler).
      - Index zuletzt schreiben (write-last), aber NUR wenn keine Blocker auftraten.
      - WICHTIG: am Anchor-Pfad gibt es KEINEN Stub; stale Stubs dort werden gelöscht.
    """
    # Timing / Metriken für Stub- und Index-Writes
    ret_stub_ms = 0.0
    ret_index_write_ms = 0.0
    ret_stub_writes = 0
    ret_index_changed = False

    # --- Hilfsfunktionen -----------------------------------------------------

    def _list_remote_snapshots(snapshots_root: str) -> list[str]:
        try:
            # REST API statt Binary (zuverlässig, kein Socket-Blocking)
            top = pc._rest_get(cfg, "listfolder", {"path": snapshots_root, "nofiles": 1}) or {}
            contents = (top.get("metadata") or {}).get("contents") or []
            return sorted(c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index")
        except Exception:
            return []

    def _stat_fileid_safe(path: str):
        try:
            # REST API statt Binary (zuverlässig, kein Socket-Blocking)
            md = pc._rest_get(cfg, "stat", {"path": path}) or {}
            return (md.get("metadata") or {}).get("fileid")
        except Exception:
            return None

    def _load_index(snapshots_root: str) -> dict:
        idx_path = f"{snapshots_root}/_index/content_index.json"
        try:
            # REST getfilelink statt Binary get_textfile (bereits REST in get_textfile, aber direkt ist klarer)
            txt = pc.get_textfile(cfg, path=idx_path)
            j = json.loads(txt)
            if not isinstance(j, dict):
                j = {"version": 1, "items": {}}
        except Exception:
            j = {"version": 1, "items": {}}
        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j

    def _save_index(snapshots_root: str, idx: dict, simulate: bool):
        nonlocal ret_index_write_ms
        if simulate:
            print(f"[dry] save index: items={len(idx.get('items', {}))}")
        else:
            t0 = time.time()
            save_content_index(cfg, snapshots_root, idx, dry=False)
            dt = (time.time() - t0) * 1000.0
            ret_index_write_ms += dt
            if os.environ.get("PCLOUD_TIMING") == "1":
                print(f"[timing] retention_index_write_ms={int(dt)}")

    def _rewrite_stub(snapshots_root: str, snapshot: str, relpath: str, sha: str, new_anchor_path: str, fileid) -> None:
        """
        Stub-JSON effizient neu schreiben:
          - Parent-Folder per folderid (stat_folderid_fast/ensure_path)
          - Schreiben via write_json_to_folderid(..., minify=True)
          - Vorhandenes Stub-JSON (falls vorhanden) übernehmen/aktualisieren
        """
        nonlocal ret_stub_ms, ret_stub_writes
        
        # relpath in (Unter)ordner + Basisdatei splitten
        if "/" in relpath:
            stub_dir, base = relpath.rsplit("/", 1)
        else:
            stub_dir, base = "", relpath

        parent_dir = f"{snapshots_root.rstrip('/')}/{snapshot}"
        if stub_dir:
            parent_dir = f"{parent_dir}/{stub_dir}"
        filename = f"{base}.meta.json"
        meta_path = f"{parent_dir}/{filename}"

        if dry:
            print(f"[dry] rewrite stub: {meta_path} -> anchor={new_anchor_path}")
            return

        # 1) Parent-FolderID besorgen (ohne per-File ensure)
        fid = pc.stat_folderid_fast(cfg, parent_dir)
        if not fid:
            fid = pc.ensure_path(cfg, parent_dir)
        fid = int(fid)

        # 2) Vorhandenes Stub-JSON (best effort) laden
        try:
            old_txt = pc.get_textfile(cfg, path=meta_path)
            payload = json.loads(old_txt)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        # 3) Pflichtfelder setzen/aktualisieren
        payload.setdefault("type", "hardlink")
        payload["sha256"] = sha
        payload["relpath"] = relpath
        payload["snapshot"] = snapshot
        payload["anchor_path"] = new_anchor_path
        payload["fileid"] = fileid if fileid is not None else None

        # 4) Schreiben per folderid (minified)
        t0 = time.time()
        pc.write_json_to_folderid(cfg, folderid=fid, filename=filename, obj=payload, minify=True)
        dt = (time.time() - t0) * 1000.0
        ret_stub_ms += dt
        ret_stub_writes += 1

        if os.environ.get("PCLOUD_TIMING") == "1":
            print(f"[timing] retention_stub_write_ms={int(dt)} file={meta_path}")

    def _delete_file_if_exists(path: str) -> None:
        "Best-effort: löscht Datei (z. B. stale Stub) am Pfad, wenn vorhanden."
        if dry:
            print(f"[dry] delete-if-exists: {path}")
            return
        try:
            fid = _stat_fileid_safe(path)
            if fid:
                pc.delete_file(cfg, fileid=int(fid))
        except Exception:
            pass

    # --- Setup & Daten holen -------------------------------------------------

    ensure_snapshots_layout(cfg, dest_root, dry=dry)
    snapshots_root = f"{pc._norm_remote_path(dest_root).rstrip('/')}/_snapshots"

    print("[retention] === RETENTION START ===")
    print(f"[retention] API-Call: listfolder({snapshots_root})")
    t_list = time.time()
    remote_snaps = set(_list_remote_snapshots(snapshots_root))
    print(f"[retention] listfolder fertig ({int((time.time()-t_list)*1000)}ms) → {len(remote_snaps)} remote Snapshots")
    
    local_snaps = set(local_snaps or [])
    to_delete = sorted(s for s in remote_snaps if s not in local_snaps)
    keep_snaps = remote_snaps & local_snaps

    print(f"[retention] Remote: {len(remote_snaps)} | Lokal: {len(local_snaps)} | Zu löschen: {len(to_delete)}")
    if to_delete:
        print(f"[retention] Snapshots die gelöscht werden: {', '.join(to_delete)}")

    if not to_delete:
        print("[retention] Nichts zu löschen - SKIP")
        return

    print(f"[retention] API-Call: Lade Content-Index (193+ MB JSON)...")
    t_idx = time.time()
    idx = _load_index(snapshots_root)
    print(f"[retention] Content-Index geladen ({int((time.time()-t_idx)*1000)}ms) → {len(idx.get('items', {}))} Nodes")
    items = idx.setdefault("items", {})

    promoted = 0
    removed_nodes = 0
    any_blockers = False

    print(f"[retention] === ANALYSE START: {len(to_delete)} Snapshots werden verarbeitet ===")

    # --- Hauptlogik pro zu löschendem Snapshot ------------------------------

    for sdel in to_delete:
        print(f"[retention] Verarbeite Snapshot: {sdel}")
        del_prefix = f"{snapshots_root}/{sdel}/"
        snapshot_blockers = False  # Pro-Snapshot Blocker-Flag
        
        nodes_checked = 0
        nodes_with_anchor_in_deleted = 0
        nodes_with_holders_in_deleted = 0

        for sha, node in list(items.items()):
            nodes_checked += 1
            if nodes_checked % 5000 == 0:
                print(f"[retention]   Progress: {nodes_checked}/{len(items)} Nodes geprüft...")
            
            if not isinstance(node, dict):
                continue

            holders = list(node.get("holders") or [])
            anchor = node.get("anchor_path") or ""
            anchor_in_deleted = anchor.startswith(del_prefix)
            
            if anchor_in_deleted:
                nodes_with_anchor_in_deleted += 1

            # Invariante: am Anchor-Pfad KEIN Stub (.meta.json) → best-effort Cleanup
            if anchor:
                _delete_file_if_exists(f"{anchor}.meta.json")

            # (A) Node ohne Holder, Anchor im gelöschten Snapshot -> Node weg
            if not holders and anchor_in_deleted:
                if dry:
                    print(f"[dry] drop node (no holders, anchor in {sdel}): {sha[:8]}…")
                else:
                    del items[sha]
                    removed_nodes += 1
                    ret_index_changed = True
                continue

            # (B) Holder splitten in keep/drop und im Node setzen
            keep_holders = [h for h in holders if h.get("snapshot") in keep_snaps]
            drop_holders = [h for h in holders if h.get("snapshot") in to_delete]
            if drop_holders or anchor_in_deleted:
                node["holders"] = keep_holders
                ret_index_changed = True
                nodes_with_holders_in_deleted += 1

            # keine Keeper?
            if not keep_holders:
                if anchor_in_deleted:
                    if dry:
                        print(f"[dry] drop node (no keepers, anchor in {sdel}): {sha[:8]}…")
                    else:
                        del items[sha]
                        removed_nodes += 1
                continue

            # (C) Anchor liegt im gelöschten Snapshot -> Promotion (MOVE)
            if anchor_in_deleted:
                new_holder = max(keep_holders, key=lambda h: h.get("snapshot") or "")
                new_path = f"{snapshots_root}/{new_holder['snapshot']}/{new_holder['relpath']}"

                # No-Op-Guard
                if anchor == new_path:
                    print(f"[retention]   Anchor bereits am richtigen Ort: {new_path}")
                    node["anchor_path"] = new_path
                    # am Anchor KEIN Stub: ggf. stale Stub löschen
                    _delete_file_if_exists(f"{new_path}.meta.json")
                    # optional: Stubs der übrigen Holder neu schreiben
                    if rewrite_stubs:
                        for h in keep_holders:
                            if h is new_holder or (h["snapshot"] == new_holder["snapshot"] and h["relpath"] == new_holder["relpath"]):
                                _delete_file_if_exists(f"{snapshots_root}/{h['snapshot']}/{h['relpath']}.meta.json")
                                continue
                            _rewrite_stub(snapshots_root, h["snapshot"], h["relpath"], sha, node["anchor_path"], node.get("fileid"))
                    continue

                if dry:
                    print(f"[dry] promote (move) {sha[:8]}… {anchor} -> {new_path}")
                    node["anchor_path"] = new_path
                    promoted += 1
                else:
                    print(f"[retention]   API-Call: move fileid (Anchor-Promotion) {sha[:8]}... → {new_holder['snapshot']}/{new_holder['relpath']}")
                    fid = node.get("fileid") or _stat_fileid_safe(anchor)
                    if not fid:
                        print(f"[warn] retention: fehlende fileid für Anchor {anchor}; Snapshot {sdel} wird NICHT gelöscht.", file=sys.stderr)
                        snapshot_blockers = True
                        any_blockers = True  # === NEU ===
                        continue

                    pc.ensure_parent_dirs(cfg, new_path)
                    # am Ziel darf kein Stub bleiben
                    _delete_file_if_exists(f"{new_path}.meta.json")

                    try:
                        t_move = time.time()
                        pc.move(cfg, from_fileid=int(fid), to_path=new_path)
                        print(f"[retention]   move fertig ({int((time.time()-t_move)*1000)}ms)")
                    except Exception as e:
                        print(f"[warn] retention: move failed for fileid={fid} -> {new_path}: {e}", file=sys.stderr)
                        snapshot_blockers = True
                        any_blockers = True  # === NEU ===
                        continue

                    node["anchor_path"] = new_path
                    node["fileid"] = int(fid)
                    promoted += 1
                    ret_index_changed = True

                # übrige Holder: Stubs neu schreiben (Ziel-Holder auslassen)
                if rewrite_stubs:
                    for h in keep_holders:
                        if h is new_holder or (h["snapshot"] == new_holder["snapshot"] and h["relpath"] == new_holder["relpath"]):
                            _delete_file_if_exists(f"{snapshots_root}/{h['snapshot']}/{h['relpath']}.meta.json")
                            continue
                        _rewrite_stub(snapshots_root, h["snapshot"], h["relpath"], sha, node["anchor_path"], node.get("fileid"))

        # Snapshot nur löschen, wenn keine Blocker auftraten
        rmpath = f"{snapshots_root}/{sdel}"
        
        print(f"[retention] Snapshot {sdel} Analyse fertig:")
        print(f"[retention]   - Nodes mit Anchor in {sdel}: {nodes_with_anchor_in_deleted}")
        print(f"[retention]   - Nodes mit Holders in {sdel}: {nodes_with_holders_in_deleted}")
        print(f"[retention]   - Blocker aufgetreten: {snapshot_blockers}")
        
        if snapshot_blockers:
            print(f"[warn] retention: Snapshot {sdel} bleibt bestehen (Blocker vorhanden).")
            continue

        if dry:
            print(f"[dry] delete snapshot dir: {rmpath}")
            print(f"[dry] delete manifest: /srv/pcloud-archive/manifests/{sdel}.json")
        else:
            print(f"[retention] API-Call: deletefolderrecursive({rmpath})")
            t_del = time.time()
            pc.call_with_backoff(pc.delete_folder, cfg, path=rmpath, recursive=True)
            print(f"[retention] Snapshot-Ordner gelöscht ({int((time.time()-t_del)*1000)}ms)")
            
            # Paritäts-Cleanup: Manifest löschen wenn Remote-Snapshot gelöscht wird
            manifest_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "manifests")
            manifest_file = os.path.join(manifest_dir, f"{sdel}.json")
            if os.path.exists(manifest_file):
                try:
                    os.remove(manifest_file)
                    print(f"[retention] Lokales Manifest gelöscht: {sdel}.json")
                except Exception as e:
                    print(f"[warn] Konnte Manifest nicht löschen: {manifest_file} ({e})", file=sys.stderr)

    # === NEU: Index nur schreiben wenn KEINE Blocker ===
    if any_blockers:
        print(f"[warn] retention: Index NICHT geschrieben wegen Blocker(n) in einem oder mehreren Snapshots")
    else:
        if ret_index_changed:
            print(f"[retention] API-Call: Content-Index schreiben (write_json_to_folderid)...")
            t_idx_save = time.time()
            _save_index(snapshots_root, idx, simulate=dry)
            print(f"[retention] Content-Index geschrieben ({int((time.time()-t_idx_save)*1000)}ms)")
        else:
            print("[retention] Keine Index-Änderungen - kein Schreiben notwendig")
    # === ENDE NEU ===

    print(f"[retention] === RETENTION ABGESCHLOSSEN ===")
    print(f"[retention] Statistik: promoted={promoted}, removed_nodes={removed_nodes}")
    
    if os.environ.get("PCLOUD_TIMING") == "1":
        print(f"[timing] retention: stubs_ms={int(ret_stub_ms)} index_ms={int(ret_index_write_ms)} stubs_written={ret_stub_writes}")

    msg = f"[retention] promoted={promoted} removed_nodes={removed_nodes}"
    print(msg if not dry else "[dry] " + msg[1:])
    # Metrics (thread-safe)
    try:
        with _metrics_lock:
            globals()["MET_PROMOTED"] += int(promoted)
            globals()["MET_REMOVED_NODES"] += int(removed_nodes)
    except Exception:
        pass


# ----------------- Delta-Copy Mode (PoC) -----------------

def push_1to1_delta_mode(cfg, manifest, dest_root, *, dry=False, verbose=False, manifest_path=None, basis_snapshot=None):
    """
    Delta-Copy Mode: Server-seitiges Klonen + Selective Update
    
    Workflow:
      1. Finde letzten vollständigen Snapshot (via content_index.json)
      2. copyfolder() - Server-seitiges Klonen (2-5s statt 3.5h)
      3. Manifest-Diff berechnen (10s)
      4. DELETE-Loop: deleted + changed Dateien löschen
      5. WRITE-Loop: new + changed Dateien hochladen/stubben
      6. Content-Index aktualisieren
      
    Performance:
      - Typisch: 60x-210x schneller bei minimalen Änderungen
      - 100k Dateien, 1 Änderung: 3.5h → <2min
      
    Fallback:
      - Falls kein Basis-Snapshot existiert: Wechsel zu push_1to1_mode()
    """
    t_start = time.time()
    
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    
    _log(f"[delta-copy] Start: {snapshot_name}")
    _log(f"[delta-copy] Ziel: {dest_snapshot_dir}")
    
    # === Config: Timeout Protection (copyfolder kann bei 20k+ Dateien lange dauern) ===
    # Delta-Copy Meta-Operationen brauchen ~60-120s, Standard-Timeout (30s) ist zu kurz
    current_timeout = int(cfg.get("timeout", 30))
    if current_timeout <= 60:  # Erhöhe nur bei Standard/niedrigen Werten
        cfg["timeout"] = 300  # 5 Minuten Puffer (Test: 67s bei ~20k Dateien)
        _log(f"[delta-copy] Timeout erhöht: {current_timeout}s → {cfg['timeout']}s (Meta-Operationen)")
    else:
        _log(f"[delta-copy] Timeout beibehalten: {current_timeout}s")
    
    # === Schritt 1: Finde Basis-Snapshot ===
    _log(f"[delta-copy][1/6] Suche letzten vollständigen Snapshot...")
    t_find_start = time.time()
    
    try:
        index = load_content_index(cfg, snapshots_root)
    except Exception as e:
        _log(f"[delta-copy][FALLBACK] Konnte content_index.json nicht laden: {e}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    # Finde letzten Snapshot mit .upload_complete Marker (falls nicht übergeben)
    if basis_snapshot is None:
        remote_snapshots = list_remote_snapshot_names(cfg, snapshots_root)
        
        # Sortiere absteigend (neueste zuerst)
        sorted_snapshots = sorted(remote_snapshots, reverse=True)
        
        for candidate in sorted_snapshots:
            if candidate == snapshot_name:
                continue  # Überspringe den neuen Snapshot selbst
            
            # Prüfe ob .upload_complete existiert
            marker_complete = f"{snapshots_root}/{candidate}/.upload_complete"
            try:
                pc.stat_file(cfg, path=marker_complete, with_checksum=False)
                basis_snapshot = candidate
                _log(f"[delta-copy][1/6] Basis gefunden: {basis_snapshot}")
                break
            except Exception:
                # Kein Complete-Marker → überspringe
                if verbose:
                    _log(f"[delta-copy][1/6] Überspringe {candidate} (kein Complete-Marker)")
                continue
    else:
        _log(f"[delta-copy][1/6] Basis via Controller übergeben: {basis_snapshot}")
    
    if not basis_snapshot:
        _log(f"[delta-copy][FALLBACK] Kein vollständiger Basis-Snapshot gefunden")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    t_find_ms = (time.time() - t_find_start) * 1000.0
    _log(f"[delta-copy][1/6] ✓ Basis: {basis_snapshot} ({t_find_ms:.0f}ms)")

    # === Schritt 1.5: Stub-Ratio-Check ===
    # copyfolder lohnt sich nur, wenn der Basis-Snapshot bereits überwiegend
    # aus Stubs besteht. Andernfalls würde copyfolder echte Dateien duplizieren
    # (doppelte Quota), statt nur leichte Stubs zu klonen.
    #
    # Threshold via ENV konfigurierbar:
    #   PCLOUD_COPYFOLDER_MIN_STUB_RATIO  (default 0.5 = 50% Stubs nötig)
    #   PCLOUD_COPYFOLDER_MIN_FILES       (default 100, vermeidet False-Positives bei kleinen Snapshots)
    _min_stub_ratio = float(os.environ.get("PCLOUD_COPYFOLDER_MIN_STUB_RATIO", "0.5"))
    _min_files      = int(os.environ.get("PCLOUD_COPYFOLDER_MIN_FILES", "100"))

    _basis_total, _basis_stubs, _basis_ratio = _compute_snapshot_stub_ratio(index, basis_snapshot)
    _log(f"[delta-copy][1.5/6] Basis-Analyse '{basis_snapshot}': "
         f"{_basis_total} Dateien, {_basis_stubs} Stubs ({_basis_ratio:.1%}) "
         f"[threshold: >={_min_stub_ratio:.0%} bei >={_min_files} Dateien]")

    if _basis_total < _min_files or _basis_ratio < _min_stub_ratio:
        _log(f"[delta-copy][SAFE-MODE] Basis hat zu wenig Stubs "
             f"({_basis_ratio:.1%} < {_min_stub_ratio:.0%} oder "
             f"{_basis_total} < {_min_files} Dateien)")
        _log(f"[delta-copy][SAFE-MODE] Baue Snapshot mit frischer Stub-Struktur auf "
             f"(einmalige Transformation, danach TURBO-MODE aktiv)")
        return push_1to1_mode(cfg, manifest, dest_root,
                              dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")

    _log(f"[delta-copy][TURBO-MODE] Stub-Ratio OK ({_basis_ratio:.1%}) – nutze copyfolder + Delta")

    # === Schritt 2: copyfolder() - Server-seitiges Klonen ===
    _log(f"[delta-copy][2/6] Starte Server-Side Copy: {basis_snapshot} → {snapshot_name}")
    t_copy_start = time.time()
    
    basis_path = f"{snapshots_root}/{basis_snapshot}"
    
    # KRITISCH: Zielordner VORHER anlegen (copycontentonly erwartet existierenden Container)
    if not dry:
        try:
            pc.ensure_path(cfg, snapshots_root)  # Parent sicherstellen
            dest_snapshot_fid = pc.ensure_path(cfg, dest_snapshot_dir)  # Zielordner anlegen!
            _log(f"[delta-copy][2/6] ✓ Zielordner angelegt: {dest_snapshot_dir}")
        except Exception as e:
            _log(f"[delta-copy][ERROR] Konnte Zielordner nicht anlegen: {e}")
            return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    if dry:
        _log(f"[dry] copyfolder (contentonly): {basis_path} → {dest_snapshot_dir}")
    else:
        try:
            # copyfolder mit copycontentonly=True
            # Kopiert NUR den INHALT von basis_snapshot in den neuen Ordner
            result = pc.call_with_backoff(pc.copyfolder, cfg, 
                                          from_path=basis_path, 
                                          to_folderid=dest_snapshot_fid, 
                                          copycontentonly=True)
            
            if verbose:
                _log(f"[delta-copy][2/6] copyfolder result: {json.dumps(result, indent=2)}")
            
            # CRITICAL: Warte bis Ordner wirklich existiert (pCloud async)
            # copyfolder() returned sofort, aber Ordner braucht Zeit bis sichtbar
            _log(f"[delta-copy][2/6] Warte auf Ordner-Sichtbarkeit...")
            max_wait_sec = 30
            poll_interval = 0.5
            elapsed = 0.0
            folder_exists = False
            
            while elapsed < max_wait_sec:
                try:
                    # Prüfe ob Ordner existiert
                    pc.stat_file(cfg, path=dest_snapshot_dir, with_checksum=False)
                    folder_exists = True
                    _log(f"[delta-copy][2/6] ✓ Ordner sichtbar nach {elapsed:.1f}s")
                    break
                except Exception:
                    # Noch nicht sichtbar, warte
                    time.sleep(poll_interval)
                    elapsed += poll_interval
            
            if not folder_exists:
                raise Exception(f"Ordner {dest_snapshot_dir} nach {max_wait_sec}s immer noch nicht sichtbar")
        
        except Exception as e:
            _log(f"[delta-copy][ERROR] copyfolder fehlgeschlagen: {e}")
            _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
            # Cleanup: Versuche geklonten Snapshot zu löschen
            try:
                pc.call_with_backoff(pc.delete_folder, cfg, path=dest_snapshot_dir, recursive=True)
            except Exception:
                pass
                return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    # Timing für copyfolder-Operation
    t_copy_ms = (time.time() - t_copy_start) * 1000.0
    _log(f"[delta-copy][2/6] copyfolder abgeschlossen: {t_copy_ms:.1f}ms")
    
    # === KRITISCH: Lösche alte Marker (wurden via copyfolder mitkopiert!) ===
    # Problem: copyfolder kopiert .upload_complete/.upload_started vom Basis-Snapshot
    # → Würde Gap-Detection täuschen (denkt Upload komplett, obwohl Delta fehlt)
    # → Lösung: .upload_complete löschen, .upload_started NEU schreiben (Resume-Konsistenz!)
    if not dry:
        # NUR .upload_complete löschen (alter Marker vom Basis-Snapshot)
        marker_complete_old = f"{dest_snapshot_dir}/.upload_complete"
        try:
            pc.deletefile(cfg, path=marker_complete_old)
            _log(f"[delta-copy][2/6] ✓ Gelöscht (mitkopiert): {marker_complete_old}")
        except Exception:
            pass  # Marker existierte evtl. nicht
        
        # .upload_started NEU schreiben mit aktuellen Metadaten
        marker_started_new = f"{dest_snapshot_dir}/.upload_started"
        try:
            pc.put_textfile(cfg, path=marker_started_new, text=json.dumps({
                "snapshot": snapshot_name,
                "started_at": time.time(),
                "mode": "delta-copy",
                "basis_snapshot": basis_snapshot,
                "status": "delta_upload_in_progress"
            }))
            _log(f"[delta-copy][2/6] ✓ Started-Marker aktualisiert: {marker_started_new}")
        except Exception as e:
            _log(f"[delta-copy][WARN] Konnte Started-Marker nicht setzen: {e}")
    
    # === Schritt 3: Manifest-Diff berechnen ===
    _log(f"[delta-copy][3/6] Berechne Manifest-Diff...")
    t_diff_start = time.time()
    
    # Finde Basis-Manifest (lokal im Archive)
    archive_base = os.getenv("PCLOUD_MANIFEST_ARCHIVE", "/srv/pcloud-archive")
    basis_manifest_path = f"{archive_base}/manifests/{basis_snapshot}.json"
    
    if not os.path.exists(basis_manifest_path):
        _log(f"[delta-copy][ERROR] Basis-Manifest nicht gefunden: {basis_manifest_path}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        # Cleanup
        if not dry:
            try:
                pc.call_with_backoff(pc.delete_folder, cfg, path=dest_snapshot_dir, recursive=True)
            except Exception:
                pass
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    # Import pcloud_manifest_diff
    try:
        import pcloud_manifest_diff
    except Exception as e:
        _log(f"[delta-copy][ERROR] Konnte pcloud_manifest_diff nicht importieren: {e}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    
    # Schreibe current manifest temporär (falls noch nicht gespeichert)
    import tempfile
    temp_current = None
    if not manifest_path or not os.path.exists(manifest_path):
        fd, temp_current = tempfile.mkstemp(suffix=".json", prefix="manifest_current_")
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        current_manifest_path = temp_current
    else:
        current_manifest_path = manifest_path
    
    try:
        diff = pcloud_manifest_diff.compare_manifests(current_manifest_path, basis_manifest_path)
    except Exception as e:
        _log(f"[delta-copy][ERROR] Manifest-Diff fehlgeschlagen: {e}")
        if temp_current:
            os.unlink(temp_current)
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path, strategy_mode="SAFE-MODE")
    finally:
        if temp_current and os.path.exists(temp_current):
            os.unlink(temp_current)
    
    t_diff_ms = (time.time() - t_diff_start) * 1000.0
    
    stats = diff["stats"]
    _log(f"[delta-copy][3/6] ✓ Manifest-Diff berechnet ({t_diff_ms:.0f}ms)")
    _log(f"[delta-copy][3/6]   Identisch: {stats['identical_count']}")
    _log(f"[delta-copy][3/6]   Neu:       {stats['new_count']}")
    _log(f"[delta-copy][3/6]   Geändert:  {stats['changed_count']}")
    _log(f"[delta-copy][3/6]   Gelöscht:  {stats['deleted_count']}")
    
    # === Schritt 4: DELETE-Loop (Parallel) ===
    _log(f"[delta-copy][4/6] Lösche geänderte und gelöschte Dateien...")
    t_delete_start = time.time()
    
    delete_count = 0
    delete_items = diff["deleted"] + diff["changed"]
    
    def _delete_file_and_stub(item):
        """Helper: Löscht Datei + Stub (parallel-safe)"""
        nonlocal delete_count
        relpath = item.get("relpath")
        if not relpath:
            return
        
        file_path = f"{dest_snapshot_dir}/{relpath}"
        stub_path = f"{file_path}.meta.json"
        
        if dry:
            _log(f"[dry] delete: {file_path}")
            _log(f"[dry] delete stub: {stub_path}")
            return
        
        # Datei löschen
        try:
            pc.delete_file(cfg, path=file_path)
            with _metrics_lock:
                delete_count += 1
        except Exception as e:
            if "2005" not in str(e) and "not found" not in str(e).lower():
                _log(f"[delta-copy][4/6][warn] Konnte {file_path} nicht löschen: {e}")
        
        # Stub löschen (best effort)
        try:
            pc.delete_file(cfg, path=stub_path)
        except Exception:
            pass  # Stub existiert nicht → okay
    
    # Parallel-Delete (analog zu Parallel-Upload)
    if delete_items and not dry:
        max_workers = min(PARALLEL_UPLOAD_THREADS, 8)  # Max 8 parallel Deletes
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(_delete_file_and_stub, delete_items))
    elif dry:
        for item in delete_items:
            _delete_file_and_stub(item)
    
    t_delete_ms = (time.time() - t_delete_start) * 1000.0
    _log(f"[delta-copy][4/6] ✓ {delete_count} Dateien gelöscht ({t_delete_ms:.0f}ms)")
    
    # === Schritt 5: WRITE-Loop (new + changed) ===
    _log(f"[delta-copy][5/6] Schreibe neue und geänderte Dateien...")
    t_write_start = time.time()
    
    write_items = diff["new"] + diff["changed"]
    uploaded = 0
    stubs = 0
    
    # Hilfstabellen (ähnlich wie push_1to1_mode)
    seen_inodes: dict[tuple[int,int], str] = {}
    items_dict = index.setdefault("items", {})
    known_anchors = {}
    
    for sha, node in items_dict.items():
        ap = node.get("anchor_path")
        fid = node.get("fileid")
        if ap and fid:
            known_anchors[sha] = (ap, fid)
    
    # Sortiere nach Extension für bessere Fehlerdiagnose
    write_items.sort(key=lambda x: (x.get("ext") or "", x.get("relpath") or ""))
    
    index_changed = False
    stubs_to_write = []
    
    def _ensure(path: str) -> None:
        if not path or dry:
            return
        pc.call_with_backoff(pc.ensure_path, cfg, path)
    
    def _upload_real_file(abs_src: str, dst_path: str) -> tuple:
        """Returns (fileid, pcloud_hash)"""
        parent = os.path.dirname(dst_path.rstrip("/"))
        if parent:
            _ensure(parent)
        if dry:
            _log(f"[dry] upload: {dst_path} <- {abs_src}")
            return (None, None)
        
        res = _upload_file_smart(cfg, abs_src, dst_path, dry=dry)
        
        try:
            md = (res or {}).get("metadata") or {}
            fileid = md.get("fileid")
            pcloud_hash = md.get("hash")
        except Exception:
            fileid = None
            pcloud_hash = None
        
        # Eager FileID
        if (not fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
            try:
                stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=dst_path) or {}
                if not fileid:
                    fileid = stat_md.get("fileid")
                if not pcloud_hash:
                    pcloud_hash = stat_md.get("hash")
            except Exception:
                pass
        
        return (fileid, pcloud_hash)
    
    def _queue_stub(relpath: str, file_item: dict, node: dict) -> None:
        nonlocal stubs, index_changed
        
        eager = os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0"
        if eager and (not node.get("fileid")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=_fid_cache_shared)
            if fid:
                node["fileid"] = fid
                index_changed = True
        
        meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
        payload = {
            "type": "hardlink",
            "sha256": file_item.get("sha256"),
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "snapshot": snapshot_name,
            "relpath": relpath,
            "anchor_path": node.get("anchor_path"),
            "fileid": node.get("fileid") if node.get("fileid") is not None else None,
            "inode": file_item.get("inode"),
        }
        if dry:
            _log(f"[dry] write stub: {meta_path}")
        else:
            stubs_to_write.append((meta_path, payload))
        stubs += 1
    
    # === Parallel Upload für kleine Dateien (Delta-Mode) ===
    _state_lock = threading.Lock()
    
    def _process_write_item(file_item: dict) -> None:
        """Verarbeitet ein write-item (thread-safe)"""
        nonlocal uploaded, stubs, index_changed
        
        relpath = file_item.get("relpath")
        if not relpath:
            return
        
        sha = (file_item.get("sha256") or "").lower()
        abs_src = file_item.get("source_path")
        ext = file_item.get("ext", "")
        
        if not sha or not abs_src:
            _log(f"[delta-copy][5/6][warn] Überspringe {relpath} (kein SHA256 oder source_path)")
            return
        
        # Hardlink-Dedupe (thread-safe)
        ino_data = file_item.get("inode")
        key = None
        if ino_data and isinstance(ino_data, dict):
            dev = ino_data.get("dev")
            ino = ino_data.get("ino")
            if dev and ino:
                key = (dev, ino)
        
        with _state_lock:
            if key and key in seen_inodes:
                # Hardlink zu bereits verarbeitetem File
                _queue_stub(relpath, file_item, items_dict.get(sha, {}))
                return
            
            # Prüfe ob SHA256 bereits im Index existiert
            node = items_dict.get(sha)
        
        if node:
            # Hash existiert bereits → Stub schreiben
            with _state_lock:
                _queue_stub(relpath, file_item, node)
                if key:
                    seen_inodes[key] = sha
        else:
            # Neue Datei → Upload + Anchor registrieren (außerhalb Lock!)
            dst_path = f"{dest_snapshot_dir}/{relpath}"
            fileid, pcloud_hash = _upload_real_file(abs_src, dst_path)
            
            # Index-Updates (thread-safe)
            with _state_lock:
                uploaded += 1
                
                # Index-Eintrag erstellen
                node = {
                    "anchor_path": dst_path,
                    "anchor_snapshot": snapshot_name,
                    "holders": [{"snapshot": snapshot_name, "relpath": relpath}],
                    "ext": ext,
                }
                if fileid:
                    node["fileid"] = fileid
                if pcloud_hash:
                    node["pcloud_hash"] = pcloud_hash
                
                items_dict[sha] = node
                index_changed = True
                
                if key:
                    seen_inodes[key] = sha
    
    # Files klassifizieren (small vs large)
    _small_writes = [f for f in write_items if (f.get("size") or 0) < SMALL_FILE_THRESHOLD_BYTES]
    _large_writes = [f for f in write_items if (f.get("size") or 0) >= SMALL_FILE_THRESHOLD_BYTES]
    
    if _small_writes and _large_writes:
        _log(f"[delta-copy][5/6] {len(_small_writes)} kleine Dateien parallel, {len(_large_writes)} große sequentiell")
    elif _small_writes:
        _log(f"[delta-copy][5/6] {len(_small_writes)} kleine Dateien parallel")
    else:
        _log(f"[delta-copy][5/6] {len(_large_writes)} große Dateien sequentiell")
    
    # Kleine Dateien parallel
    if _small_writes and PARALLEL_UPLOAD_THREADS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_UPLOAD_THREADS) as ex:
            list(ex.map(_process_write_item, _small_writes))
    else:
        for f in _small_writes:
            _process_write_item(f)
    
    # Große Dateien sequentiell
    for f in _large_writes:
        _process_write_item(f)
    # === Ende Parallel Upload (Delta-Mode) ===
    
    # Stubs schreiben (Batch)
    # Note: MET_STUBS_WRITTEN wird in _batch_write_stubs() hochgezählt
    if stubs_to_write and not dry:
        _log(f"[delta-copy][5/6] Schreibe {len(stubs_to_write)} Stubs...")
        _batch_write_stubs(cfg, stubs_to_write, dry=dry)
    
    t_write_ms = (time.time() - t_write_start) * 1000.0
    _log(f"[delta-copy][5/6] ✓ WRITE abgeschlossen: {uploaded} uploads, {stubs} stubs ({t_write_ms:.0f}ms)")
    
    # === Schritt 6: Content-Index aktualisieren ===
    _log(f"[delta-copy][6/6] Aktualisiere Content-Index...")
    t_index_start = time.time()
    
    # Holders aktualisieren: Snapshot zu allen verwendeten Hashes hinzufügen
    # WICHTIG: Iteriere über items_dict (enthält auch neue Nodes aus Phase 5!)
    for file_item in (manifest.get("items") or []):
        if file_item.get("type") != "file":
            continue
        
        sha = (file_item.get("sha256") or "").lower()
        if not sha:
            continue
        
        node = items_dict.get(sha)
        if not node:
            # Sollte nicht vorkommen (Phase 5 hätte Node erstellt)
            _log(f"[delta-copy][6/6][ERROR] SHA256 {sha[:16]}... nicht im Index!")
            continue
        
        holders = node.setdefault("holders", [])
        relpath = file_item.get("relpath") or ""
        
        # Holder mit vollständigen Metadaten (wie in Manifesten)
        holder_entry = {
            "snapshot": snapshot_name,
            "relpath": relpath,
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "inode": file_item.get("inode"),  # {"dev": ..., "ino": ..., "nlink": ...}
            "ext": file_item.get("ext"),
        }
        
        # Check if this exact holder already exists (robust gegen String-Leichen)
        holder_exists = any(
            isinstance(h, dict) and h.get("snapshot") == snapshot_name and h.get("relpath") == relpath
            for h in holders
        )
        
        if not holder_exists:
            holders.append(holder_entry)
            index_changed = True
    
    # Index speichern (remote + lokal)
    if index_changed:
        save_content_index(cfg, snapshots_root, index, dry=dry)
        _log(f"[delta-copy][6/6] ✓ Content-Index remote gespeichert")
        
        # Lokal archivieren (Snapshot-spezifisch)
        if not dry:
            archive_index_path = f"{archive_base}/indexes/content_index_{snapshot_name}.json"
            os.makedirs(os.path.dirname(archive_index_path), exist_ok=True)
            save_content_index_local(archive_index_path, index)
            _log(f"[delta-copy][6/6] ✓ Content-Index lokal archiviert: {archive_index_path}")
        
        # Master-Index aktualisieren (alle Snapshots zusammen)
        if not dry:
            master_index_path = f"{archive_base}/indexes/content_index_master.json"
            save_content_index_local(master_index_path, index)
            _log(f"[delta-copy][6/6] ✓ Master-Index aktualisiert: {master_index_path}")
        
        # Remote archivieren (Paranoia-Modus: Snapshot-isolierter Index für Recovery)
        if not dry:
            idx_path = f"{snapshots_root}/_index/content_index.json"
            archive_path = f"{snapshots_root}/_index/archive/{snapshot_name}_index.json"
            try:
                pc.ensure_parent_dirs(cfg, archive_path)
                pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
                _log(f"[delta-copy][6/6] ✓ Content-Index remote archiviert: {archive_path}")
            except Exception as e:
                _log(f"[delta-copy][6/6][warn] Remote-Archivierung fehlgeschlagen: {e}")
    else:
        _log(f"[delta-copy][6/6] Content-Index unverändert")
    
    t_index_ms = (time.time() - t_index_start) * 1000.0
    
    # === Upload-Complete Marker setzen ===
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    if not dry:
        try:
            pc.put_textfile(cfg, path=marker_complete, text=json.dumps({
                "snapshot": snapshot_name,
                "completed_at": time.time(),
                "mode": "delta-copy",
                "basis_snapshot": basis_snapshot,
            }))
            _log(f"[delta-copy] ✓ Complete-Marker gesetzt: {marker_complete}")
        except Exception as e:
            _log(f"[delta-copy][ERROR] Konnte Complete-Marker nicht setzen: {e}")
            raise  # CRITICAL: Ohne Marker ist Upload unvollständig!
    
    # === Manifest archivieren (lokal + remote) ===
    if manifest_path and not dry:
        archive_dest = f"{archive_base}/manifests/{snapshot_name}.json"
        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        
        try:
            import shutil
            shutil.copy2(manifest_path, archive_dest)
            _log(f"[delta-copy] Manifest lokal archiviert: {archive_dest}")
        except Exception as e:
            _log(f"[delta-copy][warn] Konnte Manifest lokal nicht archivieren: {e}")
    
    # Remote-Manifest Upload (für Restore ohne lokales Archiv!)
    if manifest and not dry:
        remote_manifest_path = f"{dest_snapshot_dir}/_manifest.json"
        try:
            pc.put_textfile(cfg, path=remote_manifest_path, text=json.dumps(manifest, indent=2))
            _log(f"[delta-copy] ✓ Manifest remote gespeichert: {remote_manifest_path}")
        except Exception as e:
            _log(f"[delta-copy][WARN] Remote-Manifest Upload fehlgeschlagen: {e}")
    
    # === Summary ===
    t_total_ms = (time.time() - t_start) * 1000.0
    
    _log(f"[delta-copy] ✓ ABGESCHLOSSEN ({t_total_ms/1000:.1f}s total)")
    _log(f"[delta-copy]   Server-Copy: {t_copy_ms:.0f}ms")
    _log(f"[delta-copy]   Manifest-Diff: {t_diff_ms:.0f}ms")
    _log(f"[delta-copy]   DELETE: {delete_count} Dateien, {t_delete_ms:.0f}ms")
    _log(f"[delta-copy]   WRITE: {uploaded} uploads, {stubs} stubs, {t_write_ms:.0f}ms")
    _log(f"[delta-copy]   Index: {t_index_ms:.0f}ms")
    
    # Metrics (thread-safe)
    try:
        with _metrics_lock:
            globals()["MET_UPLOADED_FILES"] += uploaded
    except Exception:
        pass
    
    return {
        "uploaded": uploaded,
        "stubs": stubs,
        "deleted": delete_count,
        "mode": "delta-copy",
        "basis_snapshot": basis_snapshot,
        "timings": {
            "total_ms": t_total_ms,
            "find_ms": t_find_ms,
            "copy_ms": t_copy_ms,
            "diff_ms": t_diff_ms,
            "delete_ms": t_delete_ms,
            "write_ms": t_write_ms,
            "index_ms": t_index_ms,
        }
    }


# ============================================================================
# POOL-MODE FUNCTIONS (NEW)
# ============================================================================

# Thread-safe Stats für Pool-Mode
class _PoolStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.uploaded = 0
        self.stubs = 0
        self.skipped = 0
        self.errors = 0
        self.processed = 0
    
    def inc_uploaded(self):
        with self.lock:
            self.uploaded += 1
    
    def inc_stubs(self):
        with self.lock:
            self.stubs += 1
    
    def inc_skipped(self):
        with self.lock:
            self.skipped += 1
    
    def inc_errors(self):
        with self.lock:
            self.errors += 1
    
    def inc_processed(self):
        with self.lock:
            self.processed += 1
    
    def get_stats(self) -> dict:
        with self.lock:
            return {
                "uploaded": self.uploaded,
                "stubs": self.stubs,
                "skipped": self.skipped,
                "errors": self.errors,
                "processed": self.processed,
            }


def _get_pool_path(sha256: str) -> str:
    """
    Berechnet Pool-Pfad aus SHA256.
    
    Args:
        sha256: SHA256-Hash (64 chars hex)
    
    Returns:
        Pool-Pfad: /_pool/XX/[full_sha256]
    """
    if not sha256 or len(sha256) < 2:
        raise ValueError(f"Invalid SHA256: {sha256}")
    
    prefix = sha256[:2].lower()
    return f"/_pool/{prefix}/{sha256.lower()}"


def _upload_to_pool(cfg: dict, local_path: str, sha256: str, *, dry: bool = False) -> Tuple[Optional[int], Optional[str]]:
    """
    Upload File in Pool (dedupliziert).
    Nutzt _upload_file_smart() für Robustheit (Retry, Resume, Timeouts).
    
    Args:
        cfg: pCloud Config
        local_path: Lokaler File-Pfad
        sha256: SHA256 Hash des Files
        dry: Dry-run Mode
    
    Returns:
        (pool_fileid, pcloud_hash) oder (None, None) bei dry-run
    """
    pool_path = _get_pool_path(sha256)
    
    if dry:
        _log(f"[dry] upload_to_pool: {local_path} → {pool_path}")
        return (None, None)
    
    # Check ob File bereits im Pool existiert (mit Safe-Wrapper)
    existing_stat = stat_file_safe(cfg, path=pool_path)
    if existing_stat:
        pool_fileid = existing_stat.get("fileid")
        pcloud_hash = existing_stat.get("hash")
        
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[pool] ✓ EXISTS: {pool_path} (fileid={pool_fileid})")
        
        return (pool_fileid, pcloud_hash)
    
    # Upload mit _upload_file_smart (Retry + Resume für große Files)
    _log(f"[pool] upload: {local_path} → {pool_path}")
    
    res = _upload_file_smart(cfg, local_path, pool_path, dry=dry)
    
    # fileid + hash aus Upload-Antwort extrahieren
    # ROBUST: pCloud API kann verschiedene Response-Formate liefern!
    pool_fileid = None
    pcloud_hash = None
    
    if res and isinstance(res, dict):
        # Standard-Fall: metadata ist ein Dict
        md = res.get("metadata")
        
        # Fall 1: metadata ist Liste (wenn Ordner erstellt wurden)
        if isinstance(md, list) and len(md) > 0:
            # Letztes Element ist die hochgeladene Datei
            md = md[-1] if isinstance(md[-1], dict) else {}
        
        # Fall 2: metadata ist Dict (Normalfall)
        elif not isinstance(md, dict):
            md = {}
        
        # Extrahiere fileid + hash
        if md:
            pool_fileid = md.get("fileid")
            pcloud_hash = md.get("hash")
    
    # EAGER FILEID FALLBACK (wie im Original - aber robuster!)
    # Wenn Upload-Response unvollständig war, hole via stat
    if (not pool_fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
        try:
            stat_md = pc.call_with_backoff(stat_file_safe, cfg, path=pool_path) or {}
            if not pool_fileid:
                pool_fileid = stat_md.get("fileid")
            if not pcloud_hash:
                pcloud_hash = stat_md.get("hash")
            
            if pool_fileid and os.environ.get("PCLOUD_VERBOSE") == "1":
                _log(f"[pool] ✓ FileID via EAGER fallback: {pool_fileid}")
        except Exception as e:
            _log(f"[pool][warn] EAGER stat failed for {pool_path}: {e}")
    
    # Finale Validierung
    if not pool_fileid:
        _log(f"[pool][ERROR] Upload lieferte keine FileID: {pool_path}")
        # Nicht aufgeben - versuche nochmal via stat (ohne EAGER check)
        try:
            stat_md = stat_file_safe(cfg, path=pool_path)
            if stat_md:
                pool_fileid = stat_md.get("fileid")
                pcloud_hash = stat_md.get("hash")
                _log(f"[pool] ✓ FileID via finaler stat-check: {pool_fileid}")
        except Exception as e:
            _log(f"[pool][ERROR] Finale FileID-Ermittlung fehlgeschlagen: {e}")
    
    if os.environ.get("PCLOUD_VERBOSE") == "1":
        _log(f"[pool] ✓ UPLOADED: {pool_path} (fileid={pool_fileid}, hash={pcloud_hash})")
    
    return (pool_fileid, pcloud_hash)


def _write_pool_stub(cfg: dict, snapshot_dir: str, relpath: str, file_item: dict, pool_fileid: int, pcloud_hash: str, *, dry: bool = False) -> None:
    """
    Schreibt Pool-Stub (.meta.json) in Snapshot-Ordner.
    Nutzt REST API write_json_to_folderid() für Robustheit (kein Binary-Blocking).
    
    Args:
        cfg: pCloud Config
        snapshot_dir: Remote Snapshot-Ordner (z.B. /_snapshots/2026-05-28-120014)
        relpath: Relative Pfad im Snapshot (z.B. home/user/file.txt)
        file_item: Manifest File-Item mit sha256, size, mtime, etc.
        pool_fileid: Pool-File ID
        pcloud_hash: pCloud Hash
        dry: Dry-run Mode
    """
    sha256 = file_item.get("sha256", "").lower()
    size = file_item.get("size", 0)
    mtime = file_item.get("mtime", 0.0)
    snapshot_name = file_item.get("snapshot", "?")
    
    pool_path = _get_pool_path(sha256)
    
    # Stub-Pfad: snapshot_dir/relpath.meta.json
    if "/" in relpath:
        stub_dir, base = relpath.rsplit("/", 1)
        parent_dir = f"{snapshot_dir}/{stub_dir}"
    else:
        base = relpath
        parent_dir = snapshot_dir
    
    stub_filename = f"{base}.meta.json"
    stub_path = f"{parent_dir}/{stub_filename}"
    
    if dry:
        _log(f"[dry] write_pool_stub: {stub_path}")
        return
    
    # Stub-Payload (wie im Original: format_version, kind, holder_type)
    stub_payload = {
        "format_version": 1,
        "kind": "stub",
        "type": "pool_stub",
        "holder_type": "pool",
        "sha256": sha256,
        "pcloud_hash": pcloud_hash or "",
        "size": size,
        "mtime": mtime,
        "relpath": relpath,
        "pool_path": pool_path,
        "pool_fileid": pool_fileid,
        "snapshot": snapshot_name,
    }
    
    # Parent-Ordner sicherstellen (gibt folderid zurück)
    parent_fid = pc.ensure_path(cfg, parent_dir)
    
    # Stub schreiben via REST (robust, kein Binary-Blocking!)
    pc.write_json_to_folderid(cfg, folderid=parent_fid, filename=stub_filename, obj=stub_payload, minify=True)
    
    if os.environ.get("PCLOUD_VERBOSE") == "1":
        _log(f"[stub] ✓ {stub_path}")


def _process_pool_item(
    cfg: dict,
    file_item: dict,
    seen_inodes: dict,
    dry: bool = False
) -> Optional[tuple]:
    """
    Verarbeitet ein File-Item: Upload in Pool, RETURN Stub-Info (schreibt NICHT!).
    
    Returns:
        (relpath, file_item, pool_fileid, pcloud_hash, is_hardlink_stub) oder None bei Fehler
    """
    relpath = file_item.get("relpath")
    sha256 = file_item.get("sha256", "").lower()
    abs_src = file_item.get("source_path")
    file_size = file_item.get("size", 0)
    
    # Validierung mit detailliertem Error-Report
    if not relpath:
        _log(f"[pool-worker][ERROR] File-Item fehlt 'relpath': {file_item}")
        return None
    
    if not sha256:
        _log(f"[pool-worker][ERROR] {relpath}: Fehlt SHA256 (corrupt manifest?)")
        return None
    
    if not abs_src:
        _log(f"[pool-worker][ERROR] {relpath}: Fehlt 'source_path' (manifest bug?)")
        return None
    
    if not os.path.exists(abs_src):
        _log(f"[pool-worker][ERROR] {relpath}: Source file nicht gefunden: {abs_src}")
        return None
    
    if not os.path.isfile(abs_src):
        _log(f"[pool-worker][ERROR] {relpath}: Source ist kein File: {abs_src}")
        return None
    
    # Hardlink-Check (lokale Dedupe)
    ino_data = file_item.get("inode")
    cached_result = None
    
    if ino_data and isinstance(ino_data, dict):
        dev = ino_data.get("dev")
        ino = ino_data.get("ino")
        key = (dev, ino) if (dev and ino) else None
        
        if key:
            # Thread-safe Lookup
            cached_result = seen_inodes.get(key)
    
    if cached_result:
        # Hardlink zu bereits verarbeitetem File → Return Cached Info
        cached_sha, cached_fileid, cached_hash = cached_result
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[pool-worker] ✓ HARDLINK: {relpath} → cached fileid={cached_fileid}")
        return (relpath, file_item, cached_fileid, cached_hash, True)  # True = ist Hardlink-Stub
    
    # Pool-Upload (Check ob existiert oder Upload)
    try:
        pool_fileid, pcloud_hash = _upload_to_pool(cfg, abs_src, sha256, dry=dry)
        
        # Validiere Upload-Ergebnis
        if not pool_fileid:
            _log(f"[pool-worker][ERROR] {relpath}: Upload lieferte keine FileID (size={file_size}, sha256={sha256[:8]}...)")
            _log(f"[pool-worker][ERROR] → Source: {abs_src}")
            return None
        
    except FileNotFoundError as e:
        _log(f"[pool-worker][ERROR] {relpath}: File verschwand während Upload: {e}")
        return None
    except PermissionError as e:
        _log(f"[pool-worker][ERROR] {relpath}: Keine Leserechte: {e}")
        return None
    except IOError as e:
        _log(f"[pool-worker][ERROR] {relpath}: I/O-Fehler beim Lesen: {e}")
        return None
    except Exception as e:
        _log(f"[pool-worker][ERROR] {relpath}: Upload fehlgeschlagen (size={file_size}, sha256={sha256[:8]}...)")
        _log(f"[pool-worker][ERROR] → Exception: {type(e).__name__}: {e}")
        _log(f"[pool-worker][ERROR] → Source: {abs_src}")
        return None
    
    # Hardlink-Tracking (wird von Hauptthread gemacht nach Return!)
    # seen_inodes[key] = (sha256, pool_fileid, pcloud_hash)
    
    return (relpath, file_item, pool_fileid, pcloud_hash, False)  # False = neuer Upload


def validate_pool_snapshot(cfg: dict, snapshot_dir: str, pool_root: str, manifest: dict, index: dict, *, dry: bool = False) -> tuple[bool, list[str]]:
    """
    Post-Upload Konsistenz-Check für Pool-Mode Snapshots (NEUE IMPLEMENTATION).
    
    Strategie (ULTRA-EFFIZIENT):
    1. Pool-Full-Check: listfolder(/_pool) → ALLE SHA256s in ~2-5s (1 API-Call!)
    2. Set-Diff: manifest_sha256s - pool_sha256s = missing_files
    3. Pool-Refs-Check: Snapshot in index["pool_refs"] für alle SHA256s?
    4. Optional: Stub-Stichprobe (konfigurierbar via PCLOUD_VALIDATE_STUB_SAMPLE)
    
    Warum besser als alte Stichproben-Methode?
    - Alte Methode: 100× stat_file() = ~10s, nur 0.1% Coverage
    - Neue Methode: 1× listfolder() = ~2-5s, 100% Coverage!
    
    Args:
        cfg: pCloud Config
        snapshot_dir: Remote Snapshot-Pfad (z.B. /_snapshots/2026-05-28-120014)
        pool_root: Pool-Root (z.B. /_pool)
        manifest: Manifest Dict
        index: Content-Index Dict
        dry: Dry-Run Mode
    
    Returns:
        (is_valid, errors) - True wenn alles ok, sonst Liste mit Fehlern
    """
    _log(f"[validate] Starte Full-Integritäts-Check für {snapshot_dir}...")
    errors = []
    snapshot_name = manifest.get("snapshot", "?")
    
    if dry:
        _log("[validate] Überspringe Validation (dry-run)")
        return (True, [])
    
    # === 1. MANIFEST-SHA256s sammeln ===
    manifest_items = [item for item in manifest.get("items", []) if item.get("type") == "file"]
    manifest_sha256s = {
        (item.get("sha256") or "").lower()
        for item in manifest_items
        if item.get("sha256")
    }
    
    total_files = len(manifest_items)
    total_unique_sha256s = len(manifest_sha256s)
    
    _log(f"[validate] Manifest: {total_files} Files, {total_unique_sha256s} unique SHA256s")
    
    # === 2. POOL-SHA256s via listfolder (FULL-CHECK in 1 API-Call!) ===
    _log(f"[validate] Lade Pool-Struktur via listfolder({pool_root})...")
    t_pool_start = time.time()
    
    try:
        # Rekursives listfolder über kompletten Pool
        result = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
        
        # SHA256s aus Pool-Pfaden extrahieren
        pool_sha256s = set()
        
        def _extract_sha256s_from_tree(obj, parent_path=""):
            """Rekursiv SHA256s aus listfolder-Tree extrahieren"""
            if isinstance(obj, dict):
                # File gefunden
                if not obj.get("isfolder") and obj.get("name"):
                    filename = obj.get("name")
                    # Pool-Files sind benannt als: SHA256 (z.B. "abc123def456...")
                    # Validiere: muss 64 Hex-Zeichen sein
                    if len(filename) == 64 and all(c in "0123456789abcdef" for c in filename):
                        pool_sha256s.add(filename.lower())
                
                # Ordner: Rekursiv in Contents
                for child in obj.get("contents", []):
                    _extract_sha256s_from_tree(child, parent_path)
        
        metadata = result.get("metadata", {})
        _extract_sha256s_from_tree(metadata)
        
        pool_duration = time.time() - t_pool_start
        _log(f"[validate] Pool: {len(pool_sha256s)} SHA256s gefunden in {pool_duration:.2f}s")
        
    except Exception as e:
        errors.append(f"Pool-listfolder fehlgeschlagen: {e}")
        _log(f"[validate][ERROR] Konnte Pool nicht laden: {e}")
        return (False, errors)
    
    # === 3. DELTA-CHECK: Fehlen Files im Pool? ===
    missing_in_pool = manifest_sha256s - pool_sha256s
    
    if missing_in_pool:
        errors.append(f"Pool: {len(missing_in_pool)} SHA256s fehlen")
        for sha in list(missing_in_pool)[:10]:  # Erste 10 zeigen
            errors.append(f"  - Pool-File fehlt: {sha[:16]}...")
        if len(missing_in_pool) > 10:
            errors.append(f"  ... und {len(missing_in_pool)-10} weitere")
    else:
        _log(f"[validate] ✓ Pool: Alle {total_unique_sha256s} SHA256s vorhanden")
    
    # === 4. POOL-REFS-CHECK (Index-Konsistenz) ===
    _log(f"[validate] Prüfe Index-Konsistenz (pool_refs)...")
    pool_refs = index.get("pool_refs", {})
    missing_in_index = 0
    wrong_snapshot = 0
    
    for sha in manifest_sha256s:
        snapshots_for_sha = pool_refs.get(sha, [])
        
        if not snapshots_for_sha:
            # SHA256 fehlt komplett im Index
            missing_in_index += 1
            if len(errors) < 20:  # Limit error details
                errors.append(f"Index: SHA256 {sha[:16]}... nicht in pool_refs")
        elif snapshot_name not in snapshots_for_sha:
            # SHA256 im Index, aber Snapshot fehlt in der Liste
            wrong_snapshot += 1
            if len(errors) < 20:
                errors.append(f"Index: SHA256 {sha[:16]}... fehlt Snapshot {snapshot_name} (hat: {snapshots_for_sha})")
    
    if missing_in_index > 0:
        errors.append(f"Index: {missing_in_index} SHA256s komplett fehlen in pool_refs")
    if wrong_snapshot > 0:
        errors.append(f"Index: {wrong_snapshot} SHA256s haben falschen Snapshot in pool_refs")
    
    if missing_in_index == 0 and wrong_snapshot == 0:
        _log(f"[validate] ✓ Index: Alle {total_unique_sha256s} SHA256s korrekt in pool_refs")
    
    # === 5. OPTIONAL: STUB-STICHPROBE ===
    stub_sample_size = int(os.environ.get("PCLOUD_VALIDATE_STUB_SAMPLE", "100"))
    
    if stub_sample_size > 0 and total_files > 0:
        import random
        sample_size = min(stub_sample_size, total_files)
        sample_items = random.sample(manifest_items, sample_size) if total_files > sample_size else manifest_items
        
        _log(f"[validate] Prüfe {sample_size} Stub-Files (Stichprobe)...")
        checked_stubs = 0
        
        for item in sample_items:
            relpath = item.get("relpath")
            if not relpath:
                continue
            
            stub_path = f"{snapshot_dir}/{relpath}.meta.json"
            try:
                stub_stat = pc.stat_file_safe(cfg, path=stub_path)
                if not stub_stat:
                    errors.append(f"Stub fehlt: {relpath}")
                else:
                    checked_stubs += 1
            except Exception as e:
                errors.append(f"Stub-Check fehlgeschlagen für {relpath}: {e}")
        
        _log(f"[validate] Stubs: {checked_stubs}/{sample_size} ok")
    
    # === RESULT ===
    if errors:
        _log(f"[validate] ❌ {len(errors)} Fehler gefunden!")
        for err in errors[:15]:  # Erste 15 zeigen
            _log(f"[validate]   {err}")
        if len(errors) > 15:
            _log(f"[validate]   ... und {len(errors)-15} weitere")
        return (False, errors)
    else:
        _log(f"[validate] ✓✓✓ Snapshot vollständig konsistent (100% Pool-Coverage, {total_unique_sha256s} SHA256s)")
        return (True, [])


# ==============================================================================
# SCOUT & TURBO-DELTA-MODE (Best Match Scout Konzept)
# ==============================================================================

def scout_best_pool_basis(cfg: dict, manifest: dict, archive_dir: str) -> tuple[str | None, float]:
    """
    Findet den effizientesten Basis-Snapshot via Jaccard-Ähnlichkeit.
    
    Strategie:
    - Vergleiche relpath-Mengen der Manifeste.
    - Performance-Limit: Scanne nur die letzten 10 Snapshots.
    - Early Exit: Bei >95% Match sofort wählen.
    
    Args:
        cfg: pCloud Config (wird nicht verwendet, für Signatur-Kompatibilität)
        manifest: Aktuelles Manifest
        archive_dir: Pfad zum Manifest-Archiv
    
    Returns:
        (snapshot_name, similarity) oder (None, 0.0)
    """
    t_start = time.time()
    manifests_path = os.path.join(archive_dir, "manifests")
    
    if not os.path.isdir(manifests_path):
        _log("[scout] Kein Manifest-Archiv gefunden")
        return None, 0.0
    
    # Aktuelle Dateimenge (relpath → sha256)
    current_files = {
        it.get("relpath"): it.get("sha256")
        for it in manifest.get("items", [])
        if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
    }
    
    if not current_files:
        _log("[scout] Kein Files im aktuellen Manifest")
        return None, 0.0
    
    current_name = manifest.get("snapshot")
    best_snap = None
    best_score = 0.0
    
    # Neueste zuerst prüfen
    archived_files = sorted(
        [f for f in os.listdir(manifests_path) if f.endswith(".json")],
        reverse=True
    )
    
    _log(f"[scout] Prüfe {min(len(archived_files), 10)} Snapshots...")
    
    for filename in archived_files[:10]:
        snap_name = filename.replace(".json", "")
        if snap_name == current_name:
            continue
        
        try:
            with open(os.path.join(manifests_path, filename), "r", encoding="utf-8") as f:
                arch_manifest = json.load(f)
            
            # Basis-Dateien (relpath → sha256)
            basis_files = {
                it.get("relpath"): it.get("sha256")
                for it in arch_manifest.get("items", [])
                if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
            }
            
            if not basis_files:
                continue
            
            # Jaccard-Similarity (relpath + sha256 match)
            matches = sum(
                1 for relpath, sha in current_files.items()
                if basis_files.get(relpath) == sha
            )
            
            score = matches / len(current_files)
            
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                _log(f"[scout]   {snap_name}: {matches}/{len(current_files)} ({score*100:.1f}%)")
            
            if score > best_score:
                best_score = score
                best_snap = snap_name
            
            # Early Exit bei >95%
            if best_score > 0.95:
                break
                
        except Exception as e:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                _log(f"[scout]   {snap_name}: Fehler beim Laden: {e}")
            continue
    
    elapsed = time.time() - t_start
    
    if best_snap:
        _log(f"[scout] ✓ Best Match: {best_snap} (Similarity: {best_score*100:.1f}%) in {elapsed:.1f}s")
    else:
        _log(f"[scout] Kein geeigneter Basis-Snapshot gefunden in {elapsed:.1f}s")
    
    return best_snap, best_score


def push_pool_delta_mode(cfg: dict, manifest: dict, dest_root: str, basis_snapshot_name: str, 
                         *, dry: bool = False, verbose: bool = False) -> dict:
    """
    Turbo-Delta-Mode: Synchronisiert neuen Snapshot basierend auf Klon eines alten.
    
    Workflow:
    1. copyfolder(basis_snapshot) → neuer Snapshot (5 Sek statt 73 Min!)
    2. Diff berechnen (Added, Changed, Removed)
    3. Bereinigung: Veraltete Stubs entfernen
    4. Update: Neue/Geänderte Stubs parallel verarbeiten
    5. Marker & Manifest finalisieren
    
    Args:
        cfg: pCloud Config
        manifest: Aktuelles Manifest
        dest_root: Remote Root
        basis_snapshot_name: Name des Basis-Snapshots (wird geklont)
        dry: Dry-run Mode
        verbose: Verbose Logging
    
    Returns:
        Stats Dict
    """
    t_start = time.time()
    
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    pool_root = f"{dest_root.rstrip('/')}/_pool"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    basis_snapshot_dir = f"{snapshots_root}/{basis_snapshot_name}"
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    
    _log(f"[delta-mode] Snapshot: {snapshot_name}")
    _log(f"[delta-mode] Basis: {basis_snapshot_name}")
    _log(f"[delta-mode] Pool: {pool_root}")
    
    # Timeout-Protection
    if "timeout" not in cfg or cfg.get("timeout", 0) < 30:
        cfg["timeout"] = int(os.environ.get("PCLOUD_TIMEOUT", "60"))
    
    # Marker
    marker_started = f"{dest_snapshot_dir}/.upload_started"
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    
    # Prüfen ob bereits vollständig
    try:
        pc.stat_file(cfg, path=marker_complete, with_checksum=False)
        _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
        return {"uploaded": 0, "stubs": 0, "resumed": False, "mode": "delta"}
    except:
        pass
    
    # === PHASE 1: SERVER-SIDE COPY (INSTANT STRUKTUR!) ===
    _log(f"[delta-mode] Phase 1: Klone Basis-Snapshot...")
    t_copy_start = time.time()
    
    if not dry:
        try:
            # copyfolder mit toname (Ordner wird umbenannt beim Kopieren)
            snapshots_fid = pc.ensure_path(cfg, snapshots_root)
            pc.copyfolder(cfg, from_path=basis_snapshot_dir, to_folderid=snapshots_fid, 
                         toname=snapshot_name, copycontentonly=True)
            copy_duration = time.time() - t_copy_start
            _log(f"[delta-mode] ✓ Struktur geklont in {copy_duration:.1f}s")
        except Exception as e:
            _log(f"[delta-mode][ERROR] copyfolder fehlgeschlagen: {e}")
            _log("[delta-mode] Fallback zu Full-Pool-Mode...")
            return push_pool_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose)
    else:
        _log(f"[dry] copyfolder({basis_snapshot_dir} → {snapshot_name})")
    
    # Started-Marker setzen
    if not dry:
        try:
            pc.put_textfile(cfg, path=marker_started, text=json.dumps({
                "snapshot": snapshot_name,
                "started_at": time.time(),
                "mode": "delta",
                "basis": basis_snapshot_name,
                "host": os.uname().nodename
            }))
        except Exception as e:
            _log(f"[warn] Konnte Started-Marker nicht setzen: {e}")
    
    # === PHASE 2: MANIFEST-DIFF BERECHNEN ===
    _log("[delta-mode] Phase 2: Berechne Manifest-Diff...")
    t_diff_start = time.time()
    
    # Basis-Manifest laden
    manifests_path = os.path.join(archive_dir, "manifests")
    basis_manifest_path = os.path.join(manifests_path, f"{basis_snapshot_name}.json")
    
    try:
        with open(basis_manifest_path, "r", encoding="utf-8") as f:
            basis_manifest = json.load(f)
    except Exception as e:
        _log(f"[delta-mode][ERROR] Basis-Manifest nicht gefunden: {e}")
        _log("[delta-mode] Fallback zu Full-Pool-Mode...")
        return push_pool_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose)
    
    # File-Maps erstellen
    current_files = {
        it.get("relpath"): it
        for it in manifest.get("items", [])
        if it.get("type") == "file" and it.get("relpath")
    }
    
    basis_files = {
        it.get("relpath"): it
        for it in basis_manifest.get("items", [])
        if it.get("type") == "file" and it.get("relpath")
    }
    
    # Diff berechnen
    current_paths = set(current_files.keys())
    basis_paths = set(basis_files.keys())
    
    added_paths = current_paths - basis_paths
    deleted_paths = basis_paths - current_paths
    common_paths = current_paths & basis_paths
    
    # Changed Files (gleicher Pfad, aber andere SHA256)
    changed_paths = set()
    for relpath in common_paths:
        curr_sha = current_files[relpath].get("sha256", "")
        base_sha = basis_files[relpath].get("sha256", "")
        if curr_sha != base_sha:
            changed_paths.add(relpath)
    
    diff_duration = time.time() - t_diff_start
    _log(f"[delta-mode] Diff: +{len(added_paths)} -{len(deleted_paths)} Δ{len(changed_paths)} (={len(common_paths)-len(changed_paths)} unverändert) in {diff_duration:.1f}s")
    
    # === PHASE 3: BEREINIGUNG (Veraltete Stubs löschen) ===
    if deleted_paths and not dry:
        _log(f"[delta-mode] Phase 3: Lösche {len(deleted_paths)} veraltete Stubs...")
        t_cleanup_start = time.time()
        deleted_count = 0
        
        for relpath in deleted_paths:
            stub_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
            try:
                # Stub-File-ID holen und löschen
                stub_md = pc.stat_file_safe(cfg, path=stub_path)
                if stub_md:
                    fid = stub_md.get("fileid")
                    if fid:
                        pc.delete_file(cfg, fileid=int(fid))
                        deleted_count += 1
            except Exception as e:
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    _log(f"[warn] Konnte Stub nicht löschen: {stub_path}: {e}")
        
        cleanup_duration = time.time() - t_cleanup_start
        _log(f"[delta-mode] ✓ {deleted_count} Stubs gelöscht in {cleanup_duration:.1f}s")
    elif deleted_paths:
        _log(f"[dry] Würde {len(deleted_paths)} Stubs löschen")
    
    # === PHASE 4: UPDATE (Neue/Geänderte Files verarbeiten) ===
    tasks = list(added_paths | changed_paths)
    
    if tasks:
        _log(f"[delta-mode] Phase 4: Verarbeite {len(tasks)} neue/geänderte Files...")
        
        # Index laden
        import tempfile
        _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
        _local_index_path = os.path.join(_local_index_dir, f"pcloud_pool_index_{snapshot_name}.json")
        os.makedirs(_local_index_dir, exist_ok=True)
        
        if os.path.exists(_local_index_path):
            index = load_content_index_local(_local_index_path)
        else:
            index = load_content_index(cfg, snapshots_root)
        
        pool_refs = index.setdefault("pool_refs", {})
        
        # Stats
        uploaded = 0
        reused = 0
        stubs = 0
        upload_ms = 0.0
        write_ms = 0.0
        stubs_to_write = []
        _state_lock = threading.Lock()
        
        # Upload-Funktion (wie in push_pool_mode)
        def _upload_to_pool(abs_src: str, sha256: str) -> tuple:
            nonlocal upload_ms
            pool_path = _get_pool_path(sha256)
            parent = os.path.dirname(pool_path.rstrip("/"))
            if parent:
                pc.ensure_path(cfg, parent)
            
            if dry:
                print(f"[dry] upload pool: {pool_path}  <- {abs_src}")
                return (None, None)
            
            # Check ob bereits existiert
            try:
                existing_stat = pc.stat_file_safe(cfg, path=pool_path)
                if existing_stat:
                    pool_fileid = existing_stat.get("fileid")
                    pcloud_hash = existing_stat.get("hash")
                    if pool_fileid:
                        return (pool_fileid, pcloud_hash)
            except Exception:
                pass
            
            t0 = time.time()
            res = _upload_file_smart(cfg, abs_src, pool_path, dry=dry)
            with _state_lock:
                upload_ms += (time.time() - t0) * 1000.0
            
            try:
                md = (res or {}).get("metadata") or {}
                if isinstance(md, list) and len(md) > 0:
                    md = md[-1]
                elif not isinstance(md, dict):
                    md = {}
                pool_fileid = md.get("fileid")
                pcloud_hash = md.get("hash")
            except Exception:
                pool_fileid = None
                pcloud_hash = None
            
            return (pool_fileid, pcloud_hash)
        
        def _queue_stub(relpath: str, file_item: dict, pool_fileid: int, pcloud_hash: int, sha256: str) -> None:
            nonlocal stubs
            pool_path = _get_pool_path(sha256)
            meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
            payload = {
                "format_version": 1,
                "kind": "stub",
                "type": "pool_stub",
                "holder_type": "pool",
                "sha256": sha256,
                "pcloud_hash": pcloud_hash or "",
                "size": file_item.get("size"),
                "mtime": file_item.get("mtime"),
                "relpath": relpath,
                "pool_path": pool_path,
                "pool_fileid": pool_fileid,
                "snapshot": snapshot_name,
            }
            if not dry:
                stubs_to_write.append((meta_path, payload))
            stubs += 1
        
        def _process_file(relpath: str) -> None:
            nonlocal uploaded, reused
            
            file_item = current_files[relpath]
            abs_src = file_item.get("source_path", "")
            sha256 = file_item.get("sha256", "")
            
            if not abs_src or not sha256:
                return
            
            # Prüfen ob bereits im Pool (anderer Snapshot)
            with _state_lock:
                if sha256 not in pool_refs:
                    pool_refs[sha256] = []
                
                already_in_snapshot = snapshot_name in pool_refs.get(sha256, [])
                if already_in_snapshot:
                    reused += 1
                    return
            
            # Upload zu Pool
            try:
                pool_fileid, pcloud_hash = _upload_to_pool(abs_src, sha256)
                
                with _state_lock:
                    if snapshot_name not in pool_refs[sha256]:
                        pool_refs[sha256].append(snapshot_name)
                    uploaded += 1
                
                _queue_stub(relpath, file_item, pool_fileid, pcloud_hash, sha256)
                
            except Exception as e:
                _log(f"[ERROR] {relpath}: Upload fehlgeschlagen: {e}")
        
        # Parallel verarbeiten
        threads = int(os.environ.get("PCLOUD_PARALLEL_UPLOAD_THREADS", "4"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(_process_file, tasks))
        
        _log(f"[delta-mode] ✓ Files verarbeitet: {uploaded} neue, {reused} wiederverwendet")
        
        # Stubs schreiben
        if stubs_to_write and not dry:
            _log(f"[delta-mode] Schreibe {len(stubs_to_write)} Stubs...")
            t0 = time.time()
            _batch_write_stubs(cfg, stubs_to_write, dry=False)
            write_ms = (time.time() - t0) * 1000.0
        
        # Index speichern
        if not dry:
            save_content_index_local(_local_index_path, index)
            save_content_index(cfg, snapshots_root, index, dry=False)
    else:
        _log("[delta-mode] Keine Änderungen - Snapshot identisch mit Basis")
        uploaded = 0
        reused = 0
        stubs = 0
        upload_ms = 0.0
        write_ms = 0.0
    
    # === COMPLETE-MARKER SETZEN ===
    if not dry:
        try:
            marker_data = {
                "snapshot": snapshot_name,
                "completed_at": time.time(),
                "uploaded": uploaded,
                "stubs": stubs,
                "reused": reused,
                "duration": time.time() - t_start,
                "mode": "delta",
                "basis": basis_snapshot_name
            }
            marker_fid = pc.stat_folderid_fast(cfg, dest_snapshot_dir)
            if not marker_fid:
                marker_fid = pc.ensure_path(cfg, dest_snapshot_dir)
            pc.write_json_to_folderid(cfg, folderid=int(marker_fid), 
                                     filename=".upload_complete", obj=marker_data, minify=True)
        except Exception as e:
            _log(f"[warn] Konnte Complete-Marker nicht setzen: {e}")
    
    total_duration = time.time() - t_start
    _log(f"[delta-mode] ✓ Abgeschlossen: {uploaded} neue, {reused} wiederverwendet, {stubs} stubs ({total_duration:.1f}s)")
    _log(f"[timing] upload_ms={int(upload_ms)} write_ms={int(write_ms)}")
    
    return {
        "uploaded": uploaded,
        "reused": reused,
        "stubs": stubs,
        "duration": total_duration,
        "upload_ms": upload_ms,
        "write_ms": write_ms,
        "mode": "delta",
        "basis": basis_snapshot_name
    }


def push_pool_mode(cfg: dict, manifest: dict, dest_root: str, *, dry: bool = False, verbose: bool = False) -> dict:
    """
    POOL-MODE Upload - 1:1 KOPIERT vom Original 1to1_mode, nur Upload-Target angepasst!
    
    Architektur:
      - Files → /_pool/XX/[sha256] (dedupliziert)
      - Snapshots → Nur .meta.json Stubs (lesbare Ordnerstruktur)
      - Content-Index → Pool-Referenzen tracken
    
    Args:
        cfg: pCloud Config
        manifest: Manifest Dict (schema=4)
        dest_root: Remote Root (z.B. /Backup/rtb_1to1)
        dry: Dry-run Mode
        verbose: Verbose Logging
    
    Returns:
        Stats Dict
    """
    t_phase_start = time.time()
    
    # === METRICS (wie Original 1to1!) ===
    ensure_ms = 0.0
    upload_ms = 0.0
    write_ms  = 0.0
    
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    pool_root = f"{dest_root.rstrip('/')}/_pool"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    
    _log(f"[pool-mode] Snapshot: {snapshot_name}")
    _log(f"[pool-mode] Pool: {pool_root}")
    _log(f"[pool-mode] Snapshot-Dir: {dest_snapshot_dir}")
    
    # === SCOUT: Best-Match Basis-Snapshot finden ===
    scout_enabled = os.environ.get("PCLOUD_SCOUT_ENABLED", "1") != "0"
    scout_threshold = float(os.environ.get("PCLOUD_SCOUT_THRESHOLD", "0.70"))
    
    if scout_enabled:
        _log("[pool-mode] Scout: Suche besten Basis-Snapshot...")
        basis_snapshot, similarity = scout_best_pool_basis(cfg, manifest, archive_dir)
        
        if basis_snapshot and similarity >= scout_threshold:
            _log(f"[pool-mode] ✓ Scout Match: {basis_snapshot} ({similarity*100:.1f}%)")
            _log(f"[pool-mode] → Nutze Turbo-Delta-Mode!")
            
            # Delegation an push_pool_delta_mode
            return push_pool_delta_mode(
                cfg, manifest, dest_root, basis_snapshot,
                dry=dry, verbose=verbose
            )
        else:
            if basis_snapshot:
                _log(f"[pool-mode] Scout Best: {basis_snapshot} ({similarity*100:.1f}%) - unter Schwelle ({scout_threshold*100:.0f}%)")
            else:
                _log(f"[pool-mode] Scout: Kein Basis gefunden")
            _log(f"[pool-mode] → Fallback zu Full-Pool-Mode")
    else:
        _log("[pool-mode] Scout deaktiviert (PCLOUD_SCOUT_ENABLED=0)")
    
    # === Timeout-Protection (wie Original!) ===
    if "timeout" not in cfg or cfg.get("timeout", 0) < 30:
        cfg["timeout"] = int(os.environ.get("PCLOUD_TIMEOUT", "60"))
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[config] Timeout auf {cfg['timeout']}s gesetzt (Mass-Upload-Protection)")
    
    # === UPLOAD-STATUS-MARKER (wie Original!) ===
    marker_started = f"{dest_snapshot_dir}/.upload_started"
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    
    # Prüfen ob unvollständiger Upload existiert
    incomplete_upload = False
    try:
        pc.stat_file(cfg, path=marker_started, with_checksum=False)
        # Started-Marker existiert
        try:
            pc.stat_file(cfg, path=marker_complete, with_checksum=False)
            # Complete-Marker auch da → Upload war erfolgreich
            _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
            return {"uploaded": 0, "stubs": 0, "resumed": False}
        except:
            # Nur Started, kein Complete → unvollständig!
            incomplete_upload = True
            _log(f"[warn] Unvollständiger Upload erkannt für {snapshot_name} - starte neu")
    except:
        # Kein Started-Marker → frischer Upload
        pass
    
    # Bei unvollständigem Upload: Index-Driven Skip (keine Löschung)
    if incomplete_upload:
        _log(f"[resume] Setze Upload fort für {snapshot_name} (bereits verarbeitete Dateien werden übersprungen)")
    
    _log(f"[plan] pool snapshot={dest_snapshot_dir}")
    
    # === Started-Marker setzen (wie Original!) ===
    if not dry:
        try:
            pc.call_with_backoff(pc.ensure_path, cfg, dest_snapshot_dir)
            pc.call_with_backoff(pc.put_textfile, cfg, path=marker_started,
                          text=json.dumps({
                              "snapshot": snapshot_name,
                              "started_at": time.time(),
                              "mode": "pool",
                              "host": os.uname().nodename
                          }))
            _log(f"[info] Upload-Started-Marker gesetzt: {marker_started}")
        except Exception as e:
            _log(f"[warn] Konnte Started-Marker nicht setzen: {e}")
    
    # === STATE-LOCK (wie Original!) ===
    _state_lock = threading.Lock()
    
    # === HILFSFUNKTIONEN (1:1 vom Original!) ===
    def _ensure(path: str) -> None:
        nonlocal ensure_ms
        if not path:
            return
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] ensure: {path}")
            return
        t0 = time.time()
        pc.call_with_backoff(pc.ensure_path, cfg, path)
        with _state_lock:
            ensure_ms += (time.time() - t0) * 1000.0

    def _delete_if_exists(path: str) -> None:
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] delete-if-exists: {path}")
            return
        try:
            md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=path) or {}
            fid = md.get("fileid")
            if fid:
                pc.delete_file(cfg, fileid=int(fid))
        except Exception:
            pass
    
    # === LOKALER INDEX-CACHE (wie Original!) ===
    import tempfile
    _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
    _local_index_path = os.path.join(_local_index_dir, f"pcloud_pool_index_{snapshot_name}.json")
    os.makedirs(_local_index_dir, exist_ok=True)
    
    # Index laden: erst lokal (falls vorhanden), sonst von pCloud
    if os.path.exists(_local_index_path):
        _log(f"[resume] Lade lokalen Index: {_local_index_path}")
        index = load_content_index_local(_local_index_path)
    else:
        index = load_content_index(cfg, snapshots_root)
    items = index.setdefault("items", {})
    
    # Pool-Refs-Struktur (für Pool-Mode)
    pool_refs = index.setdefault("pool_refs", {})  # SHA256 → [snapshot1, snapshot2, ...]
    
    # ============================================================================
    # === PREFLIGHT: DELTA-BERECHNUNG (VOR Upload!) ===
    # ============================================================================
    _log("[pool-mode] Preflight: Berechne Upload-Delta...")
    t_preflight_start = time.time()
    
    # 1. Manifest-SHA256s sammeln
    manifest_files = [it for it in (manifest.get("items") or []) if it.get("type") == "file"]
    manifest_sha256_to_item = {
        it.get("sha256"): it 
        for it in manifest_files 
        if it.get("sha256")
    }
    
    _log(f"[preflight] Manifest: {len(manifest_files)} Files, {len(manifest_sha256_to_item)} unique SHA256s")
    
    # 2. PHYSISCHE Pool-SHA256s via listfolder (NEU: wie Validation-Audit!)
    _log(f"[preflight] Scanne Pool-Struktur via listfolder({pool_root})...")
    t_pool_scan = time.time()
    
    try:
        # Rekursives listfolder über kompletten Pool (1 API-Call!)
        result = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
        physical_pool_sha256s = set()
        
        def _extract_pool_sha256s(obj):
            """Rekursiv SHA256s aus listfolder-Tree extrahieren"""
            if isinstance(obj, dict):
                # File gefunden
                if not obj.get("isfolder") and obj.get("name"):
                    filename = obj.get("name")
                    # Pool-Files: 64 Hex-Zeichen (SHA256)
                    if len(filename) == 64 and all(c in "0123456789abcdef" for c in filename):
                        physical_pool_sha256s.add(filename.lower())
                
                # Ordner: Rekursiv durchlaufen
                for child in obj.get("contents", []):
                    _extract_pool_sha256s(child)
        
        metadata = result.get("metadata", {})
        _extract_pool_sha256s(metadata)
        
        pool_scan_duration = time.time() - t_pool_scan
        _log(f"[preflight] Pool-Scan: {len(physical_pool_sha256s)} SHA256s gefunden in {pool_scan_duration:.2f}s")
        
    except Exception as e:
        _log(f"[preflight][WARN] Pool-Scan fehlgeschlagen: {e}, falle zurück auf Index-basiert")
        physical_pool_sha256s = set(pool_refs.keys())  # Fallback auf Index bei Fehler
    
    # 3. Index-basierte SHA256s (für Vergleich & Index-Reparatur-Erkennung)
    index_pool_sha256s = set(pool_refs.keys())
    _log(f"[preflight] Index: {len(index_pool_sha256s)} SHA256s registriert")
    
    # 4. Delta-Liste: SHA256s die PHYSISCH Upload benötigen (nicht mehr Index-basiert!)
    delta_sha256s = set(manifest_sha256_to_item.keys()) - physical_pool_sha256s
    
    # 5. Reused-Liste: SHA256s PHYSISCH im Pool (für diesen Snapshot aber neu)
    # Prüfe ob bereits für DIESEN Snapshot registriert
    already_in_snapshot = {
        sha for sha in manifest_sha256_to_item.keys()
        if snapshot_name in pool_refs.get(sha, [])
    }
    
    # Echte Reused: Physisch vorhanden, aber nicht für diesen Snapshot
    reused_sha256s = (set(manifest_sha256_to_item.keys()) & physical_pool_sha256s) - already_in_snapshot
    
    # 6. Index-Reparatur-Kandidaten: Physisch vorhanden, aber nicht im Index
    needs_index_update = reused_sha256s - index_pool_sha256s
    
    if needs_index_update:
        _log(f"[preflight] ⚠️ Index-Reparatur nötig: {len(needs_index_update)} Files physisch vorhanden, aber nicht im Index")
        _log(f"[preflight]    → Diese werden automatisch in pool_refs aufgenommen")
    
    preflight_duration = time.time() - t_preflight_start
    _log(f"[preflight] Delta: {len(delta_sha256s)} benötigen Upload ({len(delta_sha256s)*100/len(manifest_sha256_to_item):.1f}%)")
    _log(f"[preflight] Reused: {len(reused_sha256s)} aus Pool wiederverwendet ({len(reused_sha256s)*100/len(manifest_sha256_to_item):.1f}%)")
    _log(f"[preflight] Skipped: {len(already_in_snapshot)} bereits für Snapshot registriert")
    _log(f"[preflight] Abgeschlossen in {preflight_duration:.2f}s")
    
    # 5. File-Liste filtern: Nur noch Delta-Files verarbeiten
    # Wichtig: Pro SHA256 können mehrere Files existieren (Hardlinks!)
    delta_items = [it for it in manifest_files if it.get("sha256") in delta_sha256s]
    reused_items = [it for it in manifest_files if it.get("sha256") in reused_sha256s]
    skipped_items = [it for it in manifest_files if it.get("sha256") in already_in_snapshot]
    
    _log(f"[preflight] Upload-Plan: {len(delta_items)} Files uploaden, {len(reused_items)} reused, {len(skipped_items)} skipped")
    
    # Hilfstabellen
    seen_inodes: dict[tuple[int,int], str] = {}
    uploaded = 0
    resumed = len(reused_items)  # Bereits VORAB gezählt (aus Pool wiederverwendet)
    stubs = 0
    index_changed = False
    stubs_to_write: list[tuple[str, dict]] = []
    
    # === UPLOAD-FUNKTION (Pool-spezifisch, aber wie Original _upload_real_file!) ===
    def _upload_to_pool(abs_src: str, sha256: str) -> tuple:
        """Upload zu Pool, Returns (pool_fileid, pcloud_hash) - 1:1 wie Original _upload_real_file"""
        nonlocal upload_ms
        
        pool_path = _get_pool_path(sha256)
        parent = os.path.dirname(pool_path.rstrip("/"))
        if parent:
            _ensure(parent)
        
        if dry:
            print(f"[dry] upload pool: {pool_path}  <- {abs_src}")
            return (None, None)
        
        # Check ob bereits existiert (Dedupe!)
        try:
            existing_stat = pc.stat_file_safe(cfg, path=pool_path)
            if existing_stat:
                pool_fileid = existing_stat.get("fileid")
                pcloud_hash = existing_stat.get("hash")
                if pool_fileid:
                    if os.environ.get("PCLOUD_VERBOSE") == "1":
                        print(f"[pool] ✓ EXISTS: {sha256[:8]}... (fileid={pool_fileid})")
                    return (pool_fileid, pcloud_hash)
        except Exception:
            pass
        
        # Progress-Hinweis für große Dateien (wie Original!)
        file_size = os.path.getsize(abs_src)
        if file_size > 100 * 1024**2:  # > 100MB
            print(f"[upload] Starte Upload: {sha256[:16]}... ({file_size/1024**2:.1f} MB)", flush=True)
        
        t0 = time.time()
        res = _upload_file_smart(cfg, abs_src, pool_path, dry=dry)
        elapsed_ms = (time.time() - t0) * 1000.0
        
        # Thread-safe metrics update (wie Original!)
        with _state_lock:
            upload_ms += elapsed_ms
        
        # Global metrics (thread-safe, wie Original!)
        try:
            with _metrics_lock:
                globals()["MET_UPLOADED_FILES"] += 1
        except Exception:
            pass
        
        # fileid + hash aus der Upload-Antwort (1:1 wie Original!)
        try:
            md = (res or {}).get("metadata") or {}
            # Defensive: md kann Liste sein wenn Ordner erstellt wurden
            if isinstance(md, list) and len(md) > 0:
                md = md[-1]  # Letztes Element ist File
            elif not isinstance(md, dict):
                md = {}
            pool_fileid = md.get("fileid")
            pcloud_hash = md.get("hash")
        except Exception:
            pool_fileid = None
            pcloud_hash = None
        
        # Optional: Eager-FileID via stat (wie Original!)
        if (not pool_fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
            try:
                stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=pool_path) or {}
                if not pool_fileid:
                    pool_fileid = stat_md.get("fileid")
                if not pcloud_hash:
                    pcloud_hash = stat_md.get("hash")
            except Exception:
                pass
        
        return (pool_fileid, pcloud_hash)
    
    # === STUB-QUEUE-FUNKTION (1:1 vom Original, nur Payload angepasst!) ===
    def _queue_stub(relpath: str, file_item: dict, pool_fileid: int, pcloud_hash: int, sha256: str) -> None:
        nonlocal stubs, index_changed
        
        pool_path = _get_pool_path(sha256)
        meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
        payload = {
            "format_version": 1,
            "kind": "stub",
            "type": "pool_stub",
            "holder_type": "pool",
            "sha256": sha256,
            "pcloud_hash": pcloud_hash or "",
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "relpath": relpath,
            "pool_path": pool_path,
            "pool_fileid": pool_fileid,
            "snapshot": snapshot_name,
        }
        if dry:
            print(f"[dry] write stub: {meta_path}")
        else:
            stubs_to_write.append((meta_path, payload))
        stubs += 1
    
    # ============================================================================
    # === PHASE 0: POOL-ORDNERSTRUKTUR ERSTELLEN (256 Ordner: 00-FF) ===
    # ============================================================================
    _log("[pool-mode] Phase 0: Erstelle Pool-Ordnerstruktur (00-FF)...")
    t_pool_folders_start = time.time()
    
    if not dry:
        # 256 Pool-Ordner generieren (00-FF, lowercase wie _get_pool_path)
        pool_folders_needed = [f"{pool_root}/{hex(i)[2:].zfill(2)}" for i in range(256)]
        
        # Prüfe welche bereits existieren (um unnötige API-Calls zu sparen)
        existing_pool_folders = set()
        try:
            result = pc.call_with_backoff(pc.listfolder, cfg, path=pool_root, recursive=False, nofiles=True)
            metadata = result.get("metadata", {})
            contents = metadata.get("contents", [])
            existing_pool_folders = {c["name"] for c in contents if c.get("isfolder")}
            _log(f"[pool-mode] Pool hat bereits {len(existing_pool_folders)}/256 Ordner")
        except Exception as e:
            if "2005" not in str(e) and "not found" not in str(e).lower():
                _log(f"[warn] listfolder Pool fehlgeschlagen: {e}")
        
        # Nur fehlende Ordner erstellen
        missing_pool_folders = [p for p in pool_folders_needed if p.split("/")[-1] not in existing_pool_folders]
        
        if missing_pool_folders:
            _log(f"[pool-mode] Erstelle {len(missing_pool_folders)} fehlende Pool-Ordner...")
            
            # Parallel mit Workers erstellen (wie bei Snapshot-Ordnern)
            pool_threads = int(os.environ.get("PCLOUD_POOL_FOLDER_THREADS", "8"))
            created_count = 0
            
            def _create_pool_folder(path: str) -> bool:
                try:
                    pc.call_with_backoff(pc.ensure_path, cfg, path)
                    return True
                except Exception as e:
                    _log(f"[warn] Pool-Ordner {path} konnte nicht erstellt werden: {e}")
                    return False
            
            # Batch-Erstellung mit ThreadPool
            with concurrent.futures.ThreadPoolExecutor(max_workers=pool_threads) as ex:
                results = list(ex.map(_create_pool_folder, missing_pool_folders))
                created_count = sum(1 for r in results if r)
            
            _log(f"[pool-mode] ✓ {created_count}/{len(missing_pool_folders)} Pool-Ordner erstellt")
        else:
            _log("[pool-mode] ✓ Alle 256 Pool-Ordner existieren bereits")
    elif dry:
        _log("[dry] Pool-Ordnerstruktur würde erstellt (00-FF)")
    
    pool_folders_duration = time.time() - t_pool_folders_start
    _log(f"[pool-mode] Phase 0 abgeschlossen: Pool-Struktur ({pool_folders_duration:.1f}s)")
    
    # === PROGRESS-TRACKING VARIABLEN (1:1 wie Original!) ===
    _all_items = [it for it in (manifest.get("items") or []) if it.get("type") == "file"]
    _total_items = len(_all_items)
    _total_size = sum(it.get("size") or 0 for it in _all_items)
    _done_items = 0
    _done_size = 0
    _t_loop_start = time.time()
    _t_last_progress = _t_loop_start
    _PROGRESS_INTERVAL = float(os.environ.get("PCLOUD_PROGRESS_INTERVAL", "30"))
    _SAVE_INTERVAL = int(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL", "100"))
    _SAVE_INTERVAL_TIME = float(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL_TIME", "300"))  # 5min
    _last_saved_count = 0
    _t_last_index_save = time.time()
    _log(f"[push] Starte Upload: {_total_items} Dateien, {_total_size/1024**3:.2f} GB")
    
    # ============================================================================
    # === PHASE 1: FOLDER CREATION (1:1 wie Original mit Template!) ===
    # ============================================================================
    _log("[pool-mode] Phase 1: Ordnerstruktur anlegen...")
    t_folder_start = time.time()
    
    # Manifest-Ordner sammeln (was sein SOLLTE)
    manifest_items = manifest.get("items", [])
    manifest_folders = set()
    for it in manifest_items:
        if it.get("type") == "dir":
            relpath = it.get("relpath", "").rstrip("/")
            if relpath:  # Filter leere Strings (Root-Verzeichnis)
                manifest_folders.add(relpath)
    
    _log(f"[pool-mode] Manifest hat {len(manifest_folders)} Ordner")
    
    # Template-Pfade
    _FOLDER_TEMPLATE_DIRNAME = "_folder_template"
    template_path = f"{dest_root.rstrip('/')}/{_FOLDER_TEMPLATE_DIRNAME}"
    template_force_active = os.environ.get("PCLOUD_FOLDER_TEMPLATE_FORCE", "0") == "1"
    
    # Prüfe ob Template existiert
    template_exists = False
    if not dry:
        try:
            template_md = pc.stat_file(cfg, path=template_path, with_checksum=False)
            template_exists = bool(template_md and template_md.get("isfolder"))
        except Exception:
            pass
    
    # Entscheidung: Template nutzen oder Einzeln anlegen? (1:1 Original Zeile 2008)
    template_used = False
    
    if template_exists and template_force_active and not dry:
        # === Template-basierte Anlage (SCHNELL!) ===
        _log(f"[pool-mode] Nutze Template: {template_path}")
        try:
            # 1. Template-Ordner laden
            _log("[pool-mode] Lade Template-Struktur...")
            template_folders = set()
            try:
                result = pc.call_with_backoff(pc.listfolder, cfg, path=template_path, recursive=True, nofiles=True)
                def _collect_folders(obj, parent_path=""):
                    if isinstance(obj, dict) and obj.get("isfolder"):
                        folder_name = obj.get("name", "")
                        folder_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                        template_folders.add(folder_path)
                        for child in obj.get("contents") or []:
                            _collect_folders(child, folder_path)
                metadata = result.get("metadata") or {}
                for child in metadata.get("contents") or []:
                    _collect_folders(child, "")
                _log(f"[pool-mode] Template hat {len(template_folders)} Ordner")
            except Exception as e:
                _log(f"[warn] Template-Laden fehlgeschlagen: {e}")
                raise
            
            # 2. Diff berechnen
            to_add = manifest_folders - template_folders
            to_delete = template_folders - manifest_folders
            shared = manifest_folders & template_folders
            
            overlap_pct = len(shared)/len(manifest_folders)*100 if manifest_folders else 0
            _log(f"[pool-mode] Überlapp: {len(shared)}/{len(manifest_folders)} ({overlap_pct:.0f}%)")
            _log(f"[pool-mode] Delta: +{len(to_add)} neue, -{len(to_delete)} überflüssige")
            
            # 3. Template kopieren (1 API-Call!)
            _log(f"[pool-mode] Kopiere Template → {snapshot_name}...")
            dest_snapshot_fid = pc.call_with_backoff(pc.ensure_path, cfg, dest_snapshot_dir)
            pc.call_with_backoff(pc.copyfolder, cfg, from_path=template_path, to_folderid=dest_snapshot_fid, noover=True, copycontentonly=True)
            _log("[pool-mode] ✓ Template kopiert (~2-5s statt ~5min)")
            template_used = True
            
            # 4. Überflüssige Ordner löschen (tiefste zuerst)
            if to_delete:
                _log(f"[pool-mode] Lösche {len(to_delete)} überflüssige Ordner...")
                deleted = 0
                for relpath in sorted(to_delete, key=lambda p: -p.count("/")):
                    try:
                        pc.call_with_backoff(pc.delete_folder, cfg, path=f"{dest_snapshot_dir}/{relpath}", recursive=False)
                        deleted += 1
                    except Exception as e:
                        _log(f"[warn] Konnte {relpath} nicht löschen: {e}")
                _log(f"[pool-mode] ✓ {deleted} überflüssige Ordner gelöscht")
            
            # 5. Fehlende Ordner anlegen (parallel mit PCLOUD_FOLDER_THREADS)
            if to_add:
                from collections import defaultdict
                _log(f"[pool-mode] Lege {len(to_add)} fehlende Ordner an...")
                
                folders_by_depth = defaultdict(list)
                for reldir in to_add:
                    folders_by_depth[reldir.count("/")].append(reldir)
                
                threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
                created = 0
                created_lock = threading.Lock()
                total = len(to_add)
                t_add_start = time.time()
                
                def _create_template_folder(reldir: str) -> bool:
                    """Erstellt Ordner mit Progress-Logging"""
                    nonlocal created
                    try:
                        _ensure(f"{dest_snapshot_dir}/{reldir}")
                        with created_lock:
                            created += 1
                            # Progress alle 100 oder am Ende
                            if created % 100 == 0 or created == total:
                                elapsed = time.time() - t_add_start
                                rate = created / elapsed if elapsed > 0 else 0
                                pct = int((created / total) * 100)
                                _log(f"[pool-mode] Template-Delta: {created}/{total} ({pct}%) | Rate: {rate:.1f}/s")
                        return True
                    except Exception as e:
                        _log(f"[warn] Ordner {reldir} konnte nicht erstellt werden: {e}")
                        return False
                
                for depth in sorted(folders_by_depth.keys()):
                    batch = folders_by_depth[depth]
                    if threads > 1 and len(batch) > 1:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                            list(ex.map(_create_template_folder, batch))
                    else:
                        for reldir in batch:
                            _create_template_folder(reldir)
                
                _log(f"[pool-mode] ✓ {created} neue Ordner angelegt")
            
            # 6. Template aktualisieren (wenn Struktur sich änderte)
            if to_add or to_delete:
                _log("[pool-mode] Aktualisiere Template mit neuer Struktur...")
                try:
                    pc.call_with_backoff(pc.delete_folder, cfg, path=template_path, recursive=True)
                    template_fid = pc.call_with_backoff(pc.ensure_path, cfg, template_path)
                    pc.call_with_backoff(pc.copyfolder, cfg, from_path=dest_snapshot_dir, to_folderid=template_fid, noover=True, copycontentonly=True)
                    _log("[pool-mode] ✓ Template aktualisiert")
                except Exception as e:
                    _log(f"[warn] Template-Update fehlgeschlagen: {e}")
        
        except Exception as e:
            _log(f"[warn] Template-Nutzung fehlgeschlagen: {e} – Fallback zu Einzeln-Anlage")
            template_used = False
    
    # Fallback: Ordner einzeln anlegen (wenn kein Template oder Template-Fehler)
    if not template_used and not dry:
        _log("[pool-mode] Lege Ordner einzeln an (kein Template)...")
        
        # Remote-Ordner sammeln (was IST bereits da)
        remote_folders = set()
        try:
            result = pc.call_with_backoff(pc.listfolder, cfg, path=dest_snapshot_dir, recursive=True, nofiles=True)
            def _collect_folders(obj, parent_path=""):
                if isinstance(obj, dict) and obj.get("isfolder"):
                    folder_name = obj.get("name", "")
                    folder_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                    remote_folders.add(folder_path)
                    for child in obj.get("contents") or []:
                        _collect_folders(child, folder_path)
            metadata = result.get("metadata") or {}
            for child in metadata.get("contents") or []:
                _collect_folders(child, "")
            _log(f"[pool-mode] {len(remote_folders)} Remote-Ordner gefunden")
        except Exception as e:
            if "2005" in str(e) or "not found" in str(e).lower():
                _log("[pool-mode] Snapshot-Ordner existiert noch nicht (erstes Upload)")
            else:
                _log(f"[warn] listfolder fehlgeschlagen: {e}")
        
        # Differenz berechnen
        missing_folders = manifest_folders - remote_folders
        
        if missing_folders:
            from collections import defaultdict
            _log(f"[pool-mode] Lege {len(missing_folders)} fehlende Ordner an (von {len(manifest_folders)} gesamt)")
            
            folders_by_depth = defaultdict(list)
            for reldir in missing_folders:
                folders_by_depth[reldir.count("/")].append(reldir)
            
            threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
            _folders_created = 0
            _folders_lock = threading.Lock()
            _last_progress_pct = 0
            _folders_start_time = time.time()
            total_folders = len(missing_folders)
            
            def _create_folder(reldir: str) -> bool:
                """1:1 vom Original - Zeile 2132-2159"""
                nonlocal _folders_created, _last_progress_pct
                try:
                    _ensure(f"{dest_snapshot_dir}/{reldir}")
                    with _folders_lock:
                        _folders_created += 1
                        current_pct = int((_folders_created / total_folders) * 100)
                        show_progress = (
                            _folders_created % 100 == 0 or
                            _folders_created == total_folders or
                            (current_pct % 10 == 0 and current_pct != _last_progress_pct)
                        )
                        if show_progress:
                            _last_progress_pct = current_pct
                            # Dynamische ETA: Echte Geschwindigkeit statt hardcoded 0.05s!
                            elapsed = time.time() - _folders_start_time
                            if _folders_created > 0 and elapsed > 0:
                                rate = _folders_created / elapsed  # Ordner pro Sekunde
                                remaining_s = (total_folders - _folders_created) / rate if rate > 0 else 0
                            else:
                                remaining_s = 0
                            eta_str = f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s/60)}min"
                            _log(f"[folders] {_folders_created}/{total_folders} ({current_pct}%) | {eta_str} verbleibend")
                    return True
                except Exception as e:
                    print(f"[warn] Ordner-Anlage fehlgeschlagen für {reldir}: {e}", file=sys.stderr)
                    return False
            
            # Ordner nach Tiefe erstellen (Parents zuerst) - 1:1 Original Zeile 2160-2175
            _log(f"[folders] {len(folders_by_depth)} Ebenen, {threads} Threads pro Ebene")
            for depth in sorted(folders_by_depth.keys()):
                folders_at_depth = folders_by_depth[depth]
                if threads > 1 and len(folders_at_depth) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                        list(ex.map(_create_folder, folders_at_depth))
                else:
                    for folder in folders_at_depth:
                        _create_folder(folder)
            
            _log(f"[folders] ✓ {total_folders} Ordner erfolgreich angelegt")
        else:
            _log("[pool-mode] Alle Ordner existieren bereits")
    
    folder_duration = time.time() - t_folder_start
    _log(f"[pool-mode] Phase 1 abgeschlossen: Ordnerstruktur ({folder_duration:.1f}s)")
    
    # ============================================================================
    # === PHASE 2: FILE UPLOAD (1:1 wie Original mit Index-Driven Skip!) ===
    # ============================================================================
    
    # === FILE-PROCESSING-FUNKTION (1:1 vom Original, nur Pool-Upload!) ===
    def _process_file_item(it: dict) -> None:
        """
        Verarbeitet ein Delta-File (benötigt Upload).
        Preflight hat bereits gefiltert: Nur Files die nicht im Pool sind!
        """
        nonlocal uploaded, stubs, index_changed, _done_items, _done_size, _t_last_progress
        nonlocal _last_saved_count, _t_last_index_save, upload_ms
        
        # Progress-Tracking
        with _state_lock:
            _done_items += 1
            _done_size += it.get("size") or 0
            _now = time.time()
            if _now - _t_last_progress >= _PROGRESS_INTERVAL:
                _elapsed = _now - _t_loop_start
                _pct = _done_items / _total_items * 100 if _total_items else 0
                _pct_b = _done_size / _total_size * 100 if _total_size else 0
                _eta = (_elapsed / _done_size * (_total_size - _done_size)) if _done_size else 0
                _eta_str = f"~{int(_eta/60)}min" if _eta > 60 else f"~{int(_eta)}s"
                _log(
                    f"[push] {_done_items}/{_total_items} ({_pct:.0f}%) | "
                    f"{_done_size/1024**3:.2f}/{_total_size/1024**3:.2f} GB ({_pct_b:.0f}%) | "
                    f"new_anchors={uploaded} reused={resumed} stubs_queued={stubs} | {_eta_str} verbleibend"
                )
                _t_last_progress = _now
        
        relpath = it.get("relpath") or ""
        src_abs = it.get("source_path") or ""
        sha = it.get("sha256") or ""
        inode = it.get("inode") or {}
        dev = int(inode.get("dev") or 0)
        ino = int(inode.get("ino") or 0)
        ino_key = (dev, ino)
        
        # Hardlink-Check
        with _state_lock:
            if ino_key in seen_inodes:
                # Hardlink zu bereits verarbeitetem File → nutze gecachte Info
                first_relpath = seen_inodes[ino_key]
                # Pool-FileID aus erstem Upload holen (aus stubs_to_write oder Index)
                # Für jetzt: Upload-Skip (wird unten gefixt wenn nötig)
                pass
        
        # Upload zu Pool
        try:
            pool_fileid, pcloud_hash = _upload_to_pool(src_abs, sha)
            
            # Update Pool-Refs (thread-safe)
            with _state_lock:
                if sha not in pool_refs:
                    pool_refs[sha] = []
                if snapshot_name not in pool_refs[sha]:
                    pool_refs[sha].append(snapshot_name)
                    index_changed = True
                uploaded += 1
            
            # Stub queuen
            _queue_stub(relpath, it, pool_fileid, pcloud_hash, sha)
            
        except FileNotFoundError as e:
            _log(f"[ERROR] {relpath}: File verschwand während Upload: {e}")
            return
        except PermissionError as e:
            _log(f"[ERROR] {relpath}: Keine Leserechte: {e}")
            return
        except Exception as e:
            _log(f"[ERROR] {relpath}: Upload fehlgeschlagen: {type(e).__name__}: {e}")
            return
        
        # Hardlink-Tracking
        with _state_lock:
            seen_inodes[ino_key] = relpath
            
            # === PERIODISCHES INDEX-SAVE (1:1 wie Original!) ===
            _now_save = time.time()
            _count_trigger = _SAVE_INTERVAL > 0 and (uploaded + resumed + stubs) >= _last_saved_count + _SAVE_INTERVAL
            _time_trigger = _SAVE_INTERVAL_TIME > 0 and (_now_save - _t_last_index_save) >= _SAVE_INTERVAL_TIME
            if not dry and (_count_trigger or _time_trigger):
                save_content_index_local(_local_index_path, index)
                _last_saved_count = uploaded + resumed + stubs
                _t_last_index_save = _now_save
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    _reason = "count" if _count_trigger else "time"
                    print(f"[index] Lokal gespeichert ({_reason}) nach {uploaded + resumed + stubs} Dateien")
    
    def _process_reused_file(it: dict) -> None:
        """
        Verarbeitet ein Reused-File (bereits im Pool, braucht nur Stub).
        Kein Upload, nur Pool-FileID holen und Stub erstellen.
        """
        nonlocal stubs, index_changed
        
        relpath = it.get("relpath") or ""
        sha = it.get("sha256") or ""
        
        if not sha:
            return
        
        # Pool-Path und FileID holen
        pool_path = _get_pool_path(sha)
        
        try:
            # FileID aus Pool holen (kein Upload!)
            pool_md = pc.stat_file_safe(cfg, path=pool_path)
            if not pool_md:
                _log(f"[ERROR] Reused-File {relpath}: Pool-File nicht gefunden: {pool_path}")
                return
            
            pool_fileid = pool_md.get("fileid")
            pcloud_hash = pool_md.get("hash")
            
            # Update Pool-Refs
            with _state_lock:
                if sha not in pool_refs:
                    pool_refs[sha] = []
                if snapshot_name not in pool_refs[sha]:
                    pool_refs[sha].append(snapshot_name)
                    index_changed = True
            
            # Stub queuen
            _queue_stub(relpath, it, pool_fileid, pcloud_hash, sha)
            
        except Exception as e:
            _log(f"[ERROR] Reused-File {relpath}: Konnte Pool-FileID nicht holen: {e}")
    
    # === FILES KLASSIFIZIEREN (nur Delta-Files für Upload!) ===
    _log(f"[pool-mode] Phase 2: Upload {len(delta_items)} Delta-Files...")
    _small_delta = [f for f in delta_items if (f.get("size") or 0) < SMALL_FILE_THRESHOLD_BYTES]
    _large_delta = [f for f in delta_items if (f.get("size") or 0) >= SMALL_FILE_THRESHOLD_BYTES]
    
    if _small_delta and _large_delta:
        _log(f"[parallel] {len(_small_delta)} kleine Dateien (< {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) parallel, "
             f"{len(_large_delta)} große Dateien sequentiell")
    elif _small_delta:
        _log(f"[parallel] {len(_small_delta)} kleine Dateien (< {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) parallel")
    elif _large_delta:
        _log(f"[parallel] {len(_large_delta)} große Dateien (>= {SMALL_FILE_THRESHOLD_BYTES/1024**2:.0f} MB) sequentiell")
    
    # === KLEINE DELTA-DATEIEN PARALLEL ===
    if _small_delta and PARALLEL_UPLOAD_THREADS > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_UPLOAD_THREADS) as ex:
            list(ex.map(_process_file_item, _small_delta))
    else:
        for f in _small_delta:
            _process_file_item(f)
    
    # === GROSSE DELTA-DATEIEN SEQUENTIELL ===
    for f in _large_delta:
        _process_file_item(f)
    
    # === REUSED-FILES VERARBEITEN (nur Stubs, kein Upload!) ===
    if reused_items:
        _log(f"[pool-mode] Phase 2b: Erstelle {len(reused_items)} Stubs für reused Files...")
        
        # Reused-Files können alle parallel (kein Upload, nur stat_file)
        reused_threads = int(os.environ.get("PCLOUD_REUSED_THREADS", "8"))
        if reused_threads > 1 and len(reused_items) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=reused_threads) as ex:
                list(ex.map(_process_reused_file, reused_items))
        else:
            for f in reused_items:
                _process_reused_file(f)
    
    # ============================================================================
    # === PHASE 3: BATCH-WRITE STUBS ===
    # ============================================================================
    if not dry and stubs_to_write:
        _log(f"[push] ✓ Loop abgeschlossen. Bereite Stub-Batch vor ({len(stubs_to_write)} Stubs)...")
        t0 = time.time()
        _batch_write_stubs(cfg, stubs_to_write, dry=False)
        write_ms += (time.time() - t0) * 1000.0
    
    # ============================================================================
    # === INDEX SCHREIBEN (1:1 wie Original!) ===
    # ============================================================================
    if dry:
        print(f"[dry] write index: {snapshots_root}/_index/content_index.json (pool_refs={len(pool_refs)})")
    else:
        # Finaler lokaler Save (falls noch Änderungen seit letztem periodischen Save)
        if index_changed:
            save_content_index_local(_local_index_path, index)
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[index] Finaler lokaler Save vor Upload")
        
        # Index hochladen nach pCloud
        if os.path.exists(_local_index_path):
            t0 = time.time()
            save_content_index(cfg, snapshots_root, index, dry=False)
            dt_ms = (time.time() - t0) * 1000.0
            write_ms += dt_ms
            print(f"[timing] index_write_ms={int(dt_ms)}")
            
            # Remote archivieren (Paranoia-Modus: Snapshot-isolierter Index für Recovery)
            try:
                idx_path = f"{snapshots_root}/_index/content_index.json"
                archive_path = f"{snapshots_root}/_index/archive/{snapshot_name}_index.json"
                pc.ensure_parent_dirs(cfg, archive_path)
                pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
                _log(f"[index] ✓ Content-Index remote archiviert: {archive_path}")
            except Exception as e:
                _log(f"[index][warn] Remote-Archivierung fehlgeschlagen: {e}")
            
            # Master-Index aktualisieren (alle Snapshots zusammen)
            try:
                master_index_path = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "indexes", "content_index_master.json")
                os.makedirs(os.path.dirname(master_index_path), exist_ok=True)
                save_content_index_local(master_index_path, index)
                _log(f"[index] ✓ Master-Index aktualisiert: {master_index_path}")
            except Exception as e:
                _log(f"[index][warn] Master-Index-Update fehlgeschlagen: {e}")
            
            # Lokale Index-Datei löschen
            try:
                os.remove(_local_index_path)
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    print(f"[index] Lokale Kopie gelöscht: {_local_index_path}")
            except Exception as e:
                print(f"[warn] Konnte lokale Index-Datei nicht löschen: {e}")
    
    # === POST-UPLOAD VALIDATION (User-Request: Konsistenz-Check VOR Complete-Marker!) ===
    validation_enabled = os.environ.get("PCLOUD_VALIDATE_UPLOAD", "1") != "0"
    if validation_enabled and not dry:
        _log("[pool-mode] Starte Post-Upload Validation...")
        is_valid, validation_errors = validate_pool_snapshot(cfg, dest_snapshot_dir, pool_root, manifest, index, dry=dry)
        
        if not is_valid:
            _log(f"[pool-mode][ERROR] Validation fehlgeschlagen: {len(validation_errors)} Fehler!")
            _log("[pool-mode][ERROR] Complete-Marker wird NICHT gesetzt (inkonsistenter Snapshot)")
            for err in validation_errors[:5]:  # Erste 5 Fehler zeigen
                _log(f"[pool-mode][ERROR]   {err}")
            
            # KRITISCH: Upload ist fehlerhaft, kein Complete-Marker!
            raise RuntimeError(f"Snapshot-Validation fehlgeschlagen: {len(validation_errors)} Fehler gefunden")
        else:
            _log("[pool-mode] ✓ Validation erfolgreich - Snapshot ist konsistent")
    elif not validation_enabled:
        _log("[pool-mode] Validation übersprungen (PCLOUD_VALIDATE_UPLOAD=0)")
    
    # === COMPLETE-MARKER SETZEN (wie Original!) ===
    if not dry:
        try:
            marker_data = {
                "snapshot": snapshot_name,
                "completed_at": time.time(),
                "uploaded": uploaded,
                "stubs": stubs,
                "resumed": resumed,
                "duration": time.time() - t_phase_start,
                "mode": "pool"
            }
            marker_fid = pc.stat_folderid_fast(cfg, dest_snapshot_dir)
            if not marker_fid:
                marker_fid = pc.ensure_path(cfg, dest_snapshot_dir)
            pc.write_json_to_folderid(cfg, folderid=int(marker_fid), filename=".upload_complete", obj=marker_data, minify=True)
            _log(f"[info] Upload-Complete-Marker gesetzt: {marker_complete}")
        except Exception as e:
            _log(f"[warn] Konnte Complete-Marker nicht setzen: {e}")
    
    # === TIMING-STATS (wie Original!) ===
    total_duration = time.time() - t_phase_start
    _log(f"[pool-mode] Upload abgeschlossen: {uploaded} new anchors, {resumed} reused anchors, {stubs} stubs queued ({total_duration:.1f}s)")
    _log(f"[timing] upload_ms={int(upload_ms)} write_ms={int(write_ms)} ensure_ms={int(ensure_ms)}")
    
    return {
        "uploaded": uploaded,
        "resumed": resumed,
        "stubs": stubs,
        "duration": total_duration,
        "upload_ms": upload_ms,
        "write_ms": write_ms,
        "ensure_ms": ensure_ms
    }


def retention_pool_mode(cfg: dict, dest_root: str, *, local_snaps: Optional[list] = None, dry: bool = False) -> None:
    """
    POOL-MODE Retention (vereinfacht!).
    
    Löscht Snapshot-Ordner die nicht mehr lokal existieren.
    Pool-Garbage-Collection erfolgt separat (async).
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root
        local_snaps: Liste lokaler Snapshot-Namen
        dry: Dry-run Mode
    """
    _log("[retention-pool] Start")
    
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    
    # Remote Snapshots auflisten
    try:
        result = pc._rest_get(cfg, "listfolder", {"path": snapshots_root})
        metadata = result.get("metadata", {})
        contents = metadata.get("contents", [])
        remote_snaps = {c["name"] for c in contents if c.get("isfolder")}
        
        _log(f"[retention-pool] Remote: {len(remote_snaps)} Snapshots")
    except Exception as e:
        _log(f"[retention-pool] ERROR listing remote snapshots: {e}")
        return
    
    # Welche löschen?
    local_set = set(local_snaps or [])
    to_delete = remote_snaps - local_set
    
    _log(f"[retention-pool] Lokal: {len(local_set)} | Zu löschen: {len(to_delete)}")
    
    if not to_delete:
        _log("[retention-pool] Nichts zu löschen")
        return
    
    # Snapshots löschen
    for snap in sorted(to_delete):
        snap_path = f"{snapshots_root}/{snap}"
        
        if dry:
            _log(f"[dry] retention-pool: deletefolderrecursive({snap_path})")
            continue
        
        try:
            _log(f"[retention-pool] Lösche: {snap_path}")
            pc.delete_folder(cfg, path=snap_path, recursive=True)
            _log(f"[retention-pool] ✓ Gelöscht: {snap}")
        except Exception as e:
            _log(f"[retention-pool] ERROR deleting {snap}: {e}")
    
    _log(f"[retention-pool] ✓ FERTIG: {len(to_delete)} Snapshots gelöscht")
    _log("[retention-pool] HINWEIS: Pool-Garbage-Collection separat ausführen!")


# ----------------- CLI -----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Pusht ein JSON-Manifest nach pCloud (Object-Store, 1:1-Snapshot oder POOL-Modus).")
    ap.add_argument("--manifest", required=True, help="Pfad zur Manifest-JSON (schema=2 oder schema=4 für Pool)")
    ap.add_argument("--dest-root", required=True, help="Remote-Wurzel, z.B. /Backup/pcloud-snapshots")
    ap.add_argument("--snapshot-mode", choices=["objects","1to1","pool"], default="objects",
                    help="Upload-Strategie: objects (Hash-Object-Store + Stubs), 1to1 (Materialisieren + Stubs), oder pool (Pool-basiert mit Stubs)")
    ap.add_argument("--use-delta-copy", action="store_true",
                    help="Delta-Copy-Modus: Server-seitiges Klonen + selective Updates (nur mit --snapshot-mode 1to1). "
                         "Erfordert vorherigen vollständigen Snapshot. Fallback zu Full-Mode wenn kein Basis existiert.")
    ap.add_argument("--objects-layout", choices=["two-level"], default="two-level",
                    help="Layout für Object-Store (aktuell nur two-level).")
    ap.add_argument("--retention-sync", action="store_true",
                    help="Nach dem Upload: entfernte Snapshots, die lokal fehlen, sauber promoten/löschen (relevant für --snapshot-mode 1to1 oder pool).")
    ap.add_argument("--dry-run", action="store_true")


    # pCloud Config
    ap.add_argument("--env-file")
    ap.add_argument("--profile")
    ap.add_argument("--env-dir")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--device")
    ap.add_argument("--token")

    args = ap.parse_args()

    # Config
    cfg = pc.effective_config(
        env_file=args.env_file,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token},
        profile=args.profile,
        env_dir=args.env_dir
    )

    # --- Neu: Plausibilisierung & Preflight ---
    # Zielpfad normieren (führt führenden "/")
    args.dest_root = pc._norm_remote_path(args.dest_root)
    # ENV-File rein informativ: effective_config hat bereits geprüft, ob Token existiert
    try:
        pc.preflight_or_raise(cfg)   # → raise bei Auth/Quota/API down
    except Exception as e:
        print(f"[preflight][FAIL] {e}", file=sys.stderr)
        sys.exit(12)
    # --- Ende Neu ---

    # Manifest lesen
    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if int(manifest.get("schema", 0)) < 2:
        print("Manifest schema>=2 erwartet (mit inode/ext/sha256).", file=sys.stderr)
        sys.exit(2)

    dest_root = pc._norm_remote_path(args.dest_root)

    # Upload ZUERST (Retention kommt ans Ende, nach Upload)
    if args.snapshot_mode == "objects":
        push_objects_mode(cfg, manifest, dest_root, dry=bool(args.dry_run), objects_layout=args.objects_layout)
    elif args.snapshot_mode == "pool":
        # POOL-MODE (NEU)
        push_pool_mode(cfg, manifest, dest_root, dry=bool(args.dry_run))
    else:
        # 1to1-Mode
        # Smart Strategy Selection, aber mit Legacy-Kompatibilität für --use-delta-copy
        if args.use_delta_copy:
            _log("[legacy] --use-delta-copy Flag erkannt; erzwinge TURBO-MODE (deprecated, nutze Smart-Controller stattdessen)")
            # Direkt zu Delta-Mode, ohne Metriken zu prüfen
            push_1to1_delta_mode(cfg, manifest, dest_root, dry=bool(args.dry_run), manifest_path=args.manifest)
        else:
            # Standard: Smart-Entscheidung
            push_1to1_smart_controller(cfg, manifest, dest_root, dry=bool(args.dry_run), manifest_path=args.manifest)

    # Optional: Retention-Sync NACH Upload (nicht kritisch, darf Upload nicht blockieren)
    if args.retention_sync:
        print("")
        print("="*80)
        print("RETENTION-SYNC Phase gestartet")
        print("="*80)
        
        if args.snapshot_mode == "pool":
            # POOL-MODE Retention (vereinfacht)
            local_snaps = list_local_snapshot_names(manifest["root"])
            print(f"Lokale Snapshots: {len(local_snaps)}")
            try:
                t_retention = time.time()
                retention_pool_mode(cfg, dest_root, local_snaps=local_snaps, dry=bool(args.dry_run))
                retention_duration = time.time() - t_retention
                print("="*80)
                print(f"RETENTION-POOL abgeschlossen ({retention_duration:.1f}s)")
                print("="*80)
                print("")
            except Exception as _ret_exc:
                _log(f"[retention-pool] WARNING: retention_pool_mode fehlgeschlagen (nicht kritisch): {_ret_exc}")
                print("="*80)
                print(f"RETENTION-POOL FEHLER: {_ret_exc}")
                print("="*80)
                print("")
        
        elif args.snapshot_mode == "1to1":
            # 1to1-Mode Retention (legacy)
            local_snaps = list_local_snapshot_names(manifest["root"])
            print(f"Lokale Snapshots: {len(local_snaps)}")
            try:
                t_retention = time.time()
                retention_sync_1to1(cfg, dest_root, local_snaps=local_snaps, dry=bool(args.dry_run))
                retention_duration = time.time() - t_retention
                print("="*80)
                print(f"RETENTION-SYNC abgeschlossen ({retention_duration:.1f}s)")
                print("="*80)
                print("")
            except Exception as _ret_exc:
                _log(f"[retention] WARNING: retention_sync_1to1 fehlgeschlagen (nicht kritisch): {_ret_exc}")
                print("="*80)
                print(f"RETENTION-SYNC FEHLER: {_ret_exc}")
            print("="*80)
            print("")
    else:
        if args.snapshot_mode == "1to1":
            print("")
            print("="*80)
            print("RETENTION-SYNC übersprungen (--retention-sync nicht gesetzt)")
            print("="*80)
            print("")

    # --- metrics summary (einheitlich, greppbar) ---
    try:
        print(f"[metrics] uploaded_files={MET_UPLOADED_FILES} resumed_files={MET_RESUMED_FILES} "
              f"stubs_written={MET_STUBS_WRITTEN} promoted={MET_PROMOTED} removed_nodes={MET_REMOVED_NODES} "
              f"fid_cache_hits={fid_cache_hits} fid_lookups={fid_lookups} fid_rest_ms={int(fid_rest_ms)} "
              f"api_retries={MET_API_RETRIES}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
