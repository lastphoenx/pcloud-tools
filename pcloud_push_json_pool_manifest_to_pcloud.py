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
import os, sys, json, argparse, time, datetime, hashlib, gc
import concurrent.futures
import threading
from typing import Dict, Any, Optional, Tuple
from enum import Enum


# ---- Logging mit Timestamp (RTB-Stil) ----
def _ascii_safe(text: str) -> str:
    """Normalisiert Umlaute/Sonderzeichen fuer robuste CLI-Logs auf nicht-UTF8-Terminals."""
    if not isinstance(text, str):
        text = str(text)
    tr = str.maketrans({
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
        "ß": "ss",
        "→": "->", "✓": "[ok]", "⚠️": "[warn]", "❌": "[err]",
    })
    text = text.translate(tr)
    return text.encode("ascii", errors="replace").decode("ascii")


def _log(msg: str, *, file=sys.stderr) -> None:
    """Log-Ausgabe mit Timestamp (robust gegen Encoding-Probleme)"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Auto-Detection: Prüfe ob Terminal UTF-8 unterstützt
    use_ascii = os.environ.get("PCLOUD_ASCII_LOGS", "auto")
    if use_ascii == "auto":
        try:
            # Prüfe Encoding des Ziel-Streams (case-insensitive)
            encoding = (getattr(file, 'encoding', '') or '').upper().replace('-', '').replace('_', '')
            use_ascii = encoding not in ('UTF8', 'UTF')
        except:
            use_ascii = True  # Safe Fallback
    else:
        use_ascii = (use_ascii == "1")
    
    # ASCII-Safe Transformation wenn nötig
    if use_ascii:
        msg = _ascii_safe(msg)
    
    # Robuste Ausgabe mit Fallback
    try:
        print(f"{ts} {msg}", file=file, flush=True)
    except UnicodeEncodeError:
        # Fallback: Ersetze problematische Zeichen
        safe_msg = _ascii_safe(msg)
        print(f"{ts} {safe_msg}", file=file, flush=True)


class DryRunSampler:
    """Hilfsklasse um Dry-Run Logs bei >100k Files zu drosseln"""
    def __init__(self, limit: int = 5):
        self.limit = limit
        self.counts = {}

    def log(self, category: str, msg: str):
        count = self.counts.get(category, 0)
        self.counts[category] = count + 1
        
        if count < self.limit:
            print(f"[dry] {msg}")
        elif count == self.limit:
            print(f"[dry] ... {category}: weitere Ausgaben unterdrueckt ...")

_dry_sampler = DryRunSampler(limit=5)


# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
    import pcloud_path_compat as ppc
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

# --- Chunked Upload Configuration --- (1:1 aus Legacy, beim Ausbau verloren gegangen)
RESUME_THRESHOLD_BYTES = int(os.environ.get("PCLOUD_RESUME_THRESHOLD_GB", "5")) * 1024**3  # Default: 5 GB
RESUME_CHUNK_SIZE = int(os.environ.get("PCLOUD_RESUME_CHUNK_MB", "128")) * 1024**2  # Default: 128 MB

# --- Parallel Upload Configuration ---
SMALL_FILE_THRESHOLD_BYTES = int(os.environ.get("PCLOUD_SMALL_FILE_THRESHOLD_MB", "50")) * 1024**2  # Default: 50 MB
PARALLEL_UPLOAD_THREADS = int(os.environ.get("PCLOUD_UPLOAD_THREADS", "4"))  # Default: 4 threads
PARALLEL_CLEANUP_THREADS = int(os.environ.get(
    "PCLOUD_DELTA_CLEANUP_THREADS",
    os.environ.get("PCLOUD_UPLOAD_THREADS", "4"),
))
DELTA_CLEANUP_PROGRESS_EVERY = max(1, int(os.environ.get("PCLOUD_DELTA_CLEANUP_PROGRESS_EVERY", "500")))

# --- fileid-Cache Telemetrie (1:1 aus Legacy, beim Ausbau verloren gegangen) ---
fid_lookups = 0          # Anzahl _fid_for Aufrufe
fid_cache_hits = 0       # Treffer im Cache
fid_rest_ms = 0.0        # aufsummierte Zeit in pc.resolve_fileid_cached

# --- Globale Metrik-Zaehler (1:1 aus Legacy; MET_POOL_REUSED ist pool-spezifisch) ---
MET_UPLOADED_FILES = 0
MET_POOL_REUSED    = 0   # Pool-spezifisch: Datei lag bereits im _pool (Dedup-Treffer)
MET_RESUMED_FILES  = 0
MET_STUBS_WRITTEN  = 0
MET_PROMOTED       = 0
MET_REMOVED_NODES  = 0
MET_API_RETRIES    = int(os.environ.get("PCLOUD_API_RETRIES", "0"))  # optional Zaehler aus Lib/Wrapper

# --- Global Metrics Lock (Thread-Safety) ---
_metrics_lock = threading.Lock()


def _get_resume_state_dir() -> str:
    """
    Ermittelt State-Verzeichnis für Resume-Uploads (analog zu poc_chunked_resume.py).
    
    Priorität:
    1. ENV: PCLOUD_RESUME_DIR
    2. $PCLOUD_ARCHIVE_DIR/resume/ (production, default /srv/pcloud-archive/resume)
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
    
    production_dir = os.path.join(
        os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "resume"
    )
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
    Chunked Upload mit automatischem Resume — delegiert an pcloud_bin_lib.
    """
    import re

    snapshot_name = None
    match = re.search(r'/_snapshots/([^/]+)/', remote_path)
    if match:
        snapshot_name = match.group(1)

    state_key = hashlib.sha256(remote_path.encode()).hexdigest()[:16]

    try:
        return pc.upload_local_file_resumable(
            cfg,
            local_path,
            remote_path=remote_path,
            state_key=state_key,
            log_prefix="[chunked]",
            log=_log,
            snapshot_name=snapshot_name,
            dry=dry,
        )
    except Exception:
        raise


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

def _snap_names(entry) -> set:
    """Snapshot-Namen aus einem pool_refs-Eintrag - tolerant gegen ALLE Formate:
      - bare list (sehr alt):            ["snapA", ...]
      - dict mit snapshots=list (v1):    {"snapshots": ["snapA", ...]}
      - dict mit snapshots=map (v2):     {"snapshots": {"snapA": [relpaths], ...}}
    """
    if isinstance(entry, dict):
        s = entry.get("snapshots")
        if isinstance(s, dict):
            return set(s.keys())
        if isinstance(s, list):
            return set(s)
        return set()
    if isinstance(entry, list):
        return set(entry)
    return set()


def _register_snap(pool_refs: dict, sha: str, snap: str, relpath: str = "",
                   *, fileid=None, hash=None, size=None) -> None:
    """Registriert (snap, relpath) fuer eine sha im v2-Format
    (pool_refs[sha].snapshots = {snap: [relpaths]}) und konvertiert dabei aeltere
    Formate (bare list / snapshots=list) transparent nach v2. Coords (fileid/hash/size)
    werden nur gesetzt, wenn noch nicht vorhanden - echte Werte werden nie ueberschrieben.
    Zentraler Ersatz fuer alle frueheren inline-Registrierungen (kein rohes .append mehr)."""
    sha = (sha or "").lower()
    if not sha:
        return
    entry = pool_refs.get(sha)
    if not isinstance(entry, dict):
        old = entry if isinstance(entry, list) else []
        entry = {"fileid": fileid, "hash": hash, "size": size, "snapshots": {}}
        for n in old:
            entry["snapshots"][n] = []
        pool_refs[sha] = entry
    s = entry.get("snapshots")
    if isinstance(s, list):
        entry["snapshots"] = {n: [] for n in s}
        s = entry["snapshots"]
    elif not isinstance(s, dict):
        entry["snapshots"] = {}
        s = entry["snapshots"]
    rels = s.setdefault(snap, [])
    if relpath and relpath not in rels:
        rels.append(relpath)
    if fileid is not None and not entry.get("fileid"):
        entry["fileid"] = fileid
    if hash is not None and not entry.get("hash"):
        entry["hash"] = hash
    if size is not None and not entry.get("size"):
        entry["size"] = size


def filter_index_for_snapshot(index: dict, snapshot_name: str) -> dict:
    """Snapshot-isolierte pool_refs fuer Recovery (ohne kumulativen Ballast)."""
    all_refs = index.get("pool_refs") or {}
    filtered = {}
    for sha, entry in all_refs.items():
        if not isinstance(entry, dict):
            continue
        snaps_map = entry.get("snapshots")
        if not isinstance(snaps_map, dict) or snapshot_name not in snaps_map:
            continue
        filtered[sha] = {
            "fileid": entry.get("fileid"),
            "hash": entry.get("hash"),
            "size": entry.get("size"),
            "snapshots": {snapshot_name: snaps_map[snapshot_name]},
        }
    return {"version": 2, "pool_refs": filtered}


def _backup_remote_master_index_before_write(cfg: dict, snapshots_root: str) -> None:
    """Eine Prev-Kopie des Masters (_index/archive/content_index_prev.json), rotierend."""
    if os.environ.get("PCLOUD_INDEX_MASTER_BACKUP", "1") == "0":
        return
    root = snapshots_root.rstrip("/")
    idx_path = f"{root}/_index/content_index.json"
    prev_path = f"{root}/_index/archive/content_index_prev.json"
    if not pc.stat_file_safe(cfg, path=idx_path):
        return
    try:
        pc.ensure_parent_dirs(cfg, prev_path)
        pc.copyfile(cfg, from_path=idx_path, to_path=prev_path)
    except Exception as e:
        _log(f"[index][warn] Master-Prev-Backup fehlgeschlagen: {e}")


def archive_snapshot_index_remote(
    cfg: dict, snapshots_root: str, index: dict, snapshot_name: str, *, dry: bool = False,
) -> None:
    """Gefiltertes Snapshot-Archiv unter _index/archive/<snap>_index.json."""
    root = snapshots_root.rstrip("/")
    archive_path = f"{root}/_index/archive/{snapshot_name}_index.json"
    snap_idx = filter_index_for_snapshot(index, snapshot_name)
    n = len(snap_idx.get("pool_refs") or {})
    if dry:
        _log(f"[dry] archive index: {archive_path} ({n} pool_refs)")
        return
    pc.ensure_parent_dirs(cfg, archive_path)
    archive_dir = os.path.dirname(archive_path)
    fid = pc.stat_folderid_fast(cfg, archive_dir) or pc.ensure_path(cfg, archive_dir)
    pc.write_json_to_folderid(
        cfg, folderid=int(fid), filename=os.path.basename(archive_path),
        obj=snap_idx, minify=False,
    )
    _log(f"[index] ✓ Snapshot-Index archiviert: {archive_path} ({n} pool_refs)")


def save_content_index(cfg: dict, snapshots_root: str, index: dict, *, dry: bool=False) -> None:
    """
    content_index.json persistieren:
    1. Lokal auf SSD (staging + master) — kanonische Kopie, Resume-Quelle
    2. Resumable Chunk-Upload nach pCloud (kein json.dumps-RAM-Spike)
  """
    idx_dir  = f"{snapshots_root.rstrip('/')}/_index"
    idx_name = "content_index.json"

    index["version"] = 2
    if isinstance(index.get("items"), dict) and not index["items"]:
        index.pop("items", None)

    n_refs = len(index.get("pool_refs") or {})

    if dry:
        print(f"[dry] write index: {idx_dir}/{idx_name} (pool_refs={n_refs})")
        return

    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    staging_path = os.path.join(archive_dir, "indexes", "staging", "content_index_pending.json")
    master_path = os.path.join(archive_dir, "indexes", "content_index_master.json")

    _log(f"[index] Schreibe lokal ({n_refs} pool_refs)...")
    t_local = time.time()
    save_content_index_local(staging_path, index)
    save_content_index_local(master_path, index)
    file_size = os.path.getsize(staging_path)
    _log(
        f"[index] ✓ Lokal {pc._format_byte_size(file_size)} in {time.time() - t_local:.1f}s "
        f"-> {staging_path}"
    )
    _log(f"[index] ✓ Master -> {master_path}")

    _backup_remote_master_index_before_write(cfg, snapshots_root)

    fid = pc.stat_folderid_fast(cfg, idx_dir)
    if not fid:
        fid = pc.ensure_path(cfg, idx_dir)

    remote_path = f"{idx_dir}/{idx_name}"
    log_every = int(os.environ.get("PCLOUD_INDEX_UPLOAD_LOG_EVERY_CHUNKS", "1"))
    verify = os.environ.get("PCLOUD_INDEX_UPLOAD_VERIFY", "1") != "0"

    _log(f"[index] Upload nach pCloud: {remote_path}")
    t_up = time.time()
    pc.upload_local_file_resumable(
        cfg,
        staging_path,
        folderid=int(fid),
        filename=idx_name,
        remote_path=remote_path,
        state_key="content_index_json",
        log_prefix="[index-upload]",
        log=_log,
        log_every_chunks=log_every,
        verify_sha256=verify,
    )
    _log(f"[index] ✓ Remote content_index.json ({time.time() - t_up:.1f}s)")


def _release_post_validation_memory() -> None:
    """Speicher nach Validation freigeben (OOM-Schutz vor Index-Upload)."""
    gc.collect()


def _set_upload_complete_marker(
    cfg: dict, dest_snapshot_dir: str, marker_data: dict, *, dry: bool = False
) -> None:
    """Setzt .upload_complete — vor Index-Upload, damit Snapshot bei OOM als fertig gilt."""
    if dry:
        return
    marker_fid = pc.stat_folderid_fast(cfg, dest_snapshot_dir)
    if not marker_fid:
        marker_fid = pc.ensure_path(cfg, dest_snapshot_dir)
    pc.write_json_to_folderid(
        cfg, folderid=int(marker_fid), filename=".upload_complete", obj=marker_data, minify=True
    )
    _log(f"[info] .upload_complete gesetzt: {marker_data.get('snapshot')}")


def _finalize_after_validation_delta(
    cfg: dict,
    snapshots_root: str,
    index: dict,
    snapshot_name: str,
    dest_snapshot_dir: str,
    marker_data: dict,
    *,
    dry: bool = False,
) -> None:
    """
    Finalisierung nach Validation (Delta-Mode):
    1. RAM freigeben
    2. .upload_complete (klein, zuerst)
    3. Index lokal auf SSD + resumable Upload
    4. Snapshot-Index archivieren
    """
    if dry:
        return
    _release_post_validation_memory()
    _set_upload_complete_marker(cfg, dest_snapshot_dir, marker_data, dry=dry)
    save_content_index(cfg, snapshots_root, index, dry=False)
    _log("[delta-mode] ✓ content_index.json gespeichert (nach Validation)")
    try:
        archive_snapshot_index_remote(cfg, snapshots_root, index, snapshot_name, dry=False)
    except Exception as e:
        _log(f"[index][warn] Remote-Archivierung fehlgeschlagen: {e}")


def _upload_complete_matches_snapshot(cfg: dict, marker_path: str, snapshot_name: str) -> bool:
    """True nur wenn .upload_complete existiert und snapshot-Feld passt."""
    return pc.upload_complete_matches_snapshot(cfg, marker_path, snapshot_name)


def _purge_snapshot_refs_from_index(index: dict, snapshot_name: str) -> int:
    """Entfernt snapshot_name aus allen pool_refs-Eintraegen. Gibt Anzahl bereinigter SHAs zurueck."""
    pool_refs = index.get("pool_refs", {})
    if not isinstance(pool_refs, dict):
        return 0
    purged = 0
    for _entry in pool_refs.values():
        if not isinstance(_entry, dict):
            continue
        _snaps = _entry.get("snapshots")
        if isinstance(_snaps, dict) and snapshot_name in _snaps:
            del _snaps[snapshot_name]
            purged += 1
        elif isinstance(_snaps, list) and snapshot_name in _snaps:
            _snaps.remove(snapshot_name)
            purged += 1
    return purged


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

    _fid_progress_every = max(1, int(os.environ.get("PCLOUD_STUB_FID_PROGRESS_EVERY", "100") or "100"))
    _fid_heartbeat_sec = max(15, int(os.environ.get("PCLOUD_STUB_FID_HEARTBEAT_SEC", "60") or "60"))
    _parents_failed = 0
    _stubs_skipped_no_fid = 0
    _t_fid_resolve = time.time()
    _last_fid_log_time = _t_fid_resolve
    _fid_lock = threading.Lock()

    def _resolve_parent_fid(parent_path: str, normalized: str) -> Optional[int]:
        """ensure_path mit Backoff; 2004-Fallback via stat_folderid_fast."""
        try:
            fid = pc.call_with_backoff(
                pc.ensure_path, cfg, path=parent_path, attempts=5, max_sleep=30.0)
            return int(fid)
        except Exception as e:
            if "2004" not in str(e):
                raise
            fid = pc.call_with_backoff(
                pc.stat_folderid_fast, cfg, parent_path, attempts=5, max_sleep=30.0)
            if fid:
                return int(fid)
            raise RuntimeError(f"2004 but folderid not resolvable: {e}") from e

    parents_to_resolve: list[str] = []
    for parent in by_parent.keys():
        if dry:
            parent_fids[parent] = 0
            continue

        normalized_parent = pc._norm_remote_path(parent)

        if not _use_legacy_mode and normalized_parent in folder_cache:
            parent_fids[parent] = folder_cache[normalized_parent]
            _cache_hits += 1
        else:
            parents_to_resolve.append(parent)

    def _log_fid_progress(done: int, *, force: bool = False) -> None:
        nonlocal _last_fid_log_time
        now = time.time()
        if not (
            force
            or done == _total_parents
            or done % _fid_progress_every == 0
            or (now - _last_fid_log_time) >= _fid_heartbeat_sec
        ):
            return
        _last_fid_log_time = now
        _log(
            f"[stubs] Parent-FIDs: {done}/{_total_parents} "
            f"({_cache_hits} cache, {_cache_misses} neu, {_parents_failed} fehl) "
            f"{now - _t_fid_resolve:.0f}s"
        )

    if _cache_hits:
        _log(
            f"[stubs] Parent-FIDs: {_cache_hits}/{_total_parents} aus Cache, "
            f"{len(parents_to_resolve)} verbleibend (API)..."
        )
        _log_fid_progress(_cache_hits, force=True)

    if parents_to_resolve and not dry:
        fid_threads = max(
            1,
            int(
                os.environ.get(
                    "PCLOUD_STUB_FID_THREADS",
                    os.environ.get("PCLOUD_API_META_CONCURRENCY", "6"),
                )
                or "6"
            ),
        )
        _log(
            f"[stubs] {len(parents_to_resolve)} Parent(s) parallel auflösen "
            f"({fid_threads} Threads, nach Pfadtiefe)..."
        )

        def _parent_depth(parent_path: str) -> int:
            return pc._norm_remote_path(parent_path).count("/")

        by_depth: dict[int, list[str]] = {}
        for parent in parents_to_resolve:
            by_depth.setdefault(_parent_depth(parent), []).append(parent)

        def _resolve_one(parent: str) -> tuple[str, Optional[int], Optional[BaseException]]:
            try:
                normalized_parent = pc._norm_remote_path(parent)
                with _fid_lock:
                    cached = folder_cache.get(normalized_parent)
                if cached:
                    return parent, int(cached), None
                fid = _resolve_parent_fid(parent, normalized_parent)
                return parent, fid, None
            except BaseException as e:
                return parent, None, e

        for depth in sorted(by_depth.keys()):
            batch = by_depth[depth]
            _log(
                f"[stubs] Parent-FIDs Tiefe {depth}: {len(batch)} Ordner "
                f"({fid_threads} Threads)..."
            )

            def _apply_parent_result(parent: str, fid: Optional[int], err: Optional[BaseException]) -> None:
                nonlocal _parents_failed, _cache_misses, _api_calls, _stubs_skipped_no_fid
                with _fid_lock:
                    if err is not None:
                        _parents_failed += 1
                        n_skip = len(by_parent[parent])
                        _stubs_skipped_no_fid += n_skip
                        _log(
                            f"[warn] cannot resolve/ensure folderid for {parent}: {err} "
                            f"({n_skip} Stub(s) übersprungen)"
                        )
                    else:
                        parent_fids[parent] = int(fid)
                        folder_cache[pc._norm_remote_path(parent)] = int(fid)
                        _cache_misses += 1
                        _api_calls += 1
                    done = _cache_hits + _cache_misses + _parents_failed
                _log_fid_progress(done)

            if fid_threads > 1 and len(batch) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=fid_threads) as ex:
                    futs = [ex.submit(_resolve_one, p) for p in batch]
                    for fut in concurrent.futures.as_completed(futs):
                        parent, fid, err = fut.result()
                        _apply_parent_result(parent, fid, err)
            else:
                for parent in batch:
                    parent, fid, err = _resolve_one(parent)
                    _apply_parent_result(parent, fid, err)

    # Performance-Report
    if not dry:
        _speedup = (_total_parents / _api_calls) if _api_calls > 0 else 0
        _log(f"[stubs] ✓ Parent-FIDs aufgelöst: {_cache_hits} Cache-Hits, {_cache_misses} neu angelegt")
        if _parents_failed:
            _log(
                f"[stubs][WARN] {_parents_failed} Parent(s) fehlgeschlagen, "
                f"{_stubs_skipped_no_fid} Stub(s) ohne FolderID übersprungen"
            )
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
                _dry_sampler.log("stub", f"stub write: {parent}/{name}\n{txt}")
            else:
                _dry_sampler.log("stub", f"stub write: {parent}/{name}")
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
    Berechnet Pool-Pfad aus SHA256 (RELATIV zu dest_root, ohne führenden Slash!).
    
    Args:
        sha256: SHA256-Hash (64 chars hex)
    
    Returns:
        Pool-Pfad: _pool/XX/[full_sha256]
    """
    if not sha256 or len(sha256) < 2:
        raise ValueError(f"Invalid SHA256: {sha256}")
    
    prefix = sha256[:2].lower()
    return f"_pool/{prefix}/{sha256.lower()}"


def _pool_object_present(cfg: dict, pool_root: str, sha: str, pool_refs: dict) -> bool:
    """
    Prueft ob ein Pool-Objekt fuer sha256 existiert.
    1) Pfad-Stat (_pool/XX/sha)
    2) Fallback: pool_refs[sha].fileid (wie tamper-detect / pool_verify)
    """
    sha = (sha or "").lower()
    if len(sha) != 64:
        return False
    pool_path = f"{pool_root.rstrip('/')}/{sha[:2]}/{sha}"
    st = pc.call_with_backoff(
        pc.stat_file_safe, cfg, path=pool_path, attempts=4, max_sleep=30.0
    )
    if st and st.get("fileid"):
        return True
    ref = (pool_refs or {}).get(sha)
    if ref is None:
        ref = next((v for k, v in pool_refs.items() if k.lower() == sha), None)
    ref = ref or {}
    fid = ref.get("fileid")
    if fid:
        st2 = pc.call_with_backoff(
            pc.stat_file_safe, cfg, fileid=int(fid), attempts=4, max_sleep=30.0
        )
        if st2 and st2.get("fileid"):
            return True
    return False


def _manifest_relpaths_for_sha(manifest: dict, sha: str) -> list[str]:
    sha = (sha or "").lower()
    out = []
    for it in manifest.get("items", []):
        if it.get("type") != "file":
            continue
        if (it.get("sha256") or "").lower() == sha:
            rp = it.get("relpath")
            if rp:
                out.append(rp)
    return out


def _backfill_missing_pool_shas(
    cfg: dict,
    dest_root: str,
    pool_root: str,
    manifest: dict,
    missing_shas: set,
    *,
    dry: bool = False,
) -> tuple[int, list[str]]:
    """
    Laedt fehlende Pool-Objekte aus Manifest-source_path nach (z.B. nach GC-Luecken).
    """
    if not missing_shas:
        return 0, []
    filled = 0
    errors: list[str] = []
    by_sha: dict[str, dict] = {}
    for it in manifest.get("items", []):
        if it.get("type") != "file":
            continue
        sha = (it.get("sha256") or "").lower()
        if sha in missing_shas and it.get("source_path"):
            by_sha[sha] = it

    for sha in sorted(missing_shas):
        it = by_sha.get(sha)
        relpaths = _manifest_relpaths_for_sha(manifest, sha)
        rp = relpaths[0] if relpaths else "?"
        if not it:
            errors.append(f"  - Kein Manifest-Eintrag: {sha[:16]}... ({rp})")
            continue
        src = it.get("source_path")
        if not src or not os.path.isfile(src):
            errors.append(f"  - Quelle fehlt: {rp} ({src})")
            continue
        pool_path_abs = f"{dest_root.rstrip('/')}/{_get_pool_path(sha)}"
        parent_abs = os.path.dirname(pool_path_abs.rstrip("/"))
        if parent_abs:
            pc.ensure_path_cached(cfg, parent_abs)
        try:
            if _pool_object_present(cfg, pool_root, sha, {}):
                filled += 1
                continue
            if dry:
                filled += 1
                continue
            _log(f"[validate][backfill] Nachladen: {rp} -> {pool_path_abs}")
            _upload_file_smart(cfg, src, pool_path_abs, dry=False)
            if _pool_object_present(cfg, pool_root, sha, {}):
                filled += 1
                _log(f"[validate][backfill] ✓ {rp}")
            else:
                errors.append(f"  - Backfill ohne Pool-Treffer: {rp}")
        except Exception as e:
            errors.append(f"  - Backfill {rp}: {e}")
    return filled, errors


def _validate_batch_size() -> int:
    """
    Batch-Groesse fuer RAM-begrenzte Validation (Pool + Stubs).

    Ziel: ~10-25 Fortschritts-Zeilen pro Phase bei pi-nas (~80-110k Items).
    Faustformel: PCLOUD_VALIDATE_BATCH_SIZE ≈ item_count / 15
    """
    try:
        return max(500, int(os.environ.get("PCLOUD_VALIDATE_BATCH_SIZE", "5000")))
    except (TypeError, ValueError):
        return 5000


def _validate_threads() -> int:
    """Parallele stat()-Calls pro Batch (API-bound, nicht RAM-bound)."""
    for key in ("PCLOUD_VALIDATE_THREADS", "PCLOUD_FOLDER_THREADS"):
        raw = os.environ.get(key)
        if raw is not None:
            try:
                return max(1, min(32, int(raw)))
            except (TypeError, ValueError):
                pass
    return 8


def _validate_fail_limit() -> int:
    try:
        return max(1, int(os.environ.get("PCLOUD_VALIDATE_FAIL_LIMIT", "50")))
    except (TypeError, ValueError):
        return 50


def _run_validate_batches(
    items: list,
    check_fn,
    *,
    label: str,
    unit: str = "items",
) -> list:
    """check_fn(item)->fehler|None in Batches; RAM O(batch_size), kein listfolder-Baum."""
    batch_size = _validate_batch_size()
    threads = _validate_threads()
    fail_limit = _validate_fail_limit()
    total = len(items)
    if total == 0:
        return []

    num_batches = (total + batch_size - 1) // batch_size
    failures: list = []
    done = 0
    t_all = time.time()

    _log(
        f"[validate-{label}] Start: {total} {unit}, "
        f"batch={batch_size}, threads={threads}, ~{num_batches} batches"
    )

    for bi in range(num_batches):
        batch = items[bi * batch_size : (bi + 1) * batch_size]
        t0 = time.time()
        batch_fail: list = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            futs = [ex.submit(check_fn, it) for it in batch]
            for fut in concurrent.futures.as_completed(futs):
                err = fut.result()
                if err is not None:
                    batch_fail.append(err)

        failures.extend(batch_fail)
        done += len(batch)
        pct = done / total * 100.0
        dur = time.time() - t0
        _log(
            f"[validate-{label}] Batch {bi + 1}/{num_batches}: "
            f"{len(batch) - len(batch_fail)}/{len(batch)} OK "
            f"({done}/{total}, {pct:.1f}%) {dur:.1f}s"
        )

        if len(failures) >= fail_limit:
            _log(f"[validate-{label}] Abbruch: >={fail_limit} Fehler")
            break
        if bi % 4 == 3:
            gc.collect()

    _log(
        f"[validate-{label}] Fertig: {total - len(failures)}/{total} OK "
        f"({time.time() - t_all:.1f}s)"
    )
    return failures


def _validate_pool_shas_batched(
    cfg: dict,
    pool_root: str,
    manifest_sha256s: set[str],
    pool_refs: dict,
) -> list[str]:
    def _check_sha(sha: str) -> Optional[str]:
        try:
            if _pool_object_present(cfg, pool_root, sha, pool_refs):
                return None
            return sha
        except Exception as e:
            return f"{sha}: {e}"

    return _run_validate_batches(
        sorted(manifest_sha256s), _check_sha, label="pool", unit="SHA256"
    )


def _validate_stubs_batched(
    cfg: dict,
    snapshot_dir: str,
    manifest_items: list[dict],
) -> list[str]:
    snap_base = snapshot_dir.rstrip("/")
    items = [it for it in manifest_items if it.get("relpath")]

    def _check_stub(item: dict) -> Optional[str]:
        relpath = item.get("relpath")
        if not relpath:
            return None
        stub_path = f"{snap_base}/{relpath}.meta.json"
        try:
            if pc.call_with_backoff(pc.stat_file_safe, cfg, path=stub_path, attempts=4, max_sleep=30.0):
                return None
            norm_path = ppc.normalize_path_segments(stub_path)
            if norm_path != stub_path and pc.call_with_backoff(
                pc.stat_file_safe, cfg, path=norm_path, attempts=4, max_sleep=30.0
            ):
                return None
            return relpath
        except Exception as e:
            return f"{relpath}: {e}"

    return _run_validate_batches(items, _check_stub, label="stubs", unit="Stubs")


def validate_pool_snapshot(cfg: dict, snapshot_dir: str, pool_root: str, manifest: dict, index: dict, *, dry: bool = False) -> tuple[bool, list[str]]:
    """
    Post-Upload Konsistenz-Check (RAM-begrenzt, volle Abdeckung).

    Pool + Stubs: stat in Batches (kein listfolder-Gesamtbaum).
    Index: pool_refs-Konsistenz (CPU-only).
    """
    _log(f"[validate] Starte Integritaets-Check (batch) fuer {snapshot_dir}...")
    errors = []
    snapshot_name = manifest.get("snapshot", "?")

    if dry:
        _log("[validate] (dry-run) Simuliere Integritaets-Check...")
        _log(f"[validate] Manifest: {len(manifest.get('items',[]))} Files")
        _log(f"[validate] ✓ Pool: Alle SHA256s vorhanden (simuliert)")
        _log(f"[validate] ✓ Index: Alle SHA256s korrekt in pool_refs (simuliert)")
        _log(f"[validate] ✓✓✓ Snapshot vollständig konsistent (simuliert)")
        return (True, [])

    manifest_items = [item for item in manifest.get("items", []) if item.get("type") == "file"]
    manifest_sha256s = {
        (item.get("sha256") or "").lower()
        for item in manifest_items
        if item.get("sha256")
    }

    total_files = len(manifest_items)
    total_unique_sha256s = len(manifest_sha256s)

    _log(f"[validate] Manifest: {total_files} Files, {total_unique_sha256s} unique SHA256s")

    pool_refs = index.get("pool_refs", {})

    missing_in_pool = _validate_pool_shas_batched(
        cfg, pool_root, manifest_sha256s, pool_refs
    )

    if missing_in_pool and not dry:
        _repair_max = int(os.environ.get("PCLOUD_VALIDATE_POOL_BACKFILL_MAX", "50"))
        if 0 < len(missing_in_pool) <= _repair_max:
            dest_root = pool_root.rsplit("/_pool", 1)[0] if "/_pool" in pool_root else pool_root
            _log(f"[validate] Pool-Backfill: {len(missing_in_pool)} fehlende SHA(s)...")
            filled, _bf_errs = _backfill_missing_pool_shas(
                cfg, dest_root, pool_root, manifest, set(missing_in_pool), dry=dry)
            if filled:
                pool_refs = index.get("pool_refs", {})
                missing_in_pool = _validate_pool_shas_batched(
                    cfg, pool_root, set(missing_in_pool), pool_refs
                )
                if not missing_in_pool:
                    _log(f"[validate] ✓ Pool-Backfill erfolgreich ({filled} nachgeladen)")

    if missing_in_pool:
        errors.append(f"Pool: {len(missing_in_pool)} SHA256s fehlen")
        for sha in missing_in_pool[:5]:
            rels = _manifest_relpaths_for_sha(manifest, sha)
            hint = rels[0] if rels else "?"
            errors.append(f"  - Pool-File fehlt: {sha} ({hint})")
        if len(missing_in_pool) > 5:
            errors.append(f"  ... und {len(missing_in_pool) - 5} weitere")
    else:
        _log(f"[validate] ✓ Pool: Alle {total_unique_sha256s} SHA256s vorhanden")

    _log(f"[validate] Prüfe Index-Konsistenz (pool_refs)...")
    missing_in_index = 0
    wrong_snapshot = 0

    for sha in manifest_sha256s:
        snapshots_for_sha = _snap_names(pool_refs.get(sha))
        if not snapshots_for_sha:
            missing_in_index += 1
        elif snapshot_name not in snapshots_for_sha:
            wrong_snapshot += 1

    if missing_in_index > 0:
        errors.append(f"Index: {missing_in_index} SHA256s fehlen in pool_refs")
    if wrong_snapshot > 0:
        errors.append(f"Index: {wrong_snapshot} SHA256s haben falschen Snapshot")

    if missing_in_index == 0 and wrong_snapshot == 0:
        _log(f"[validate] ✓ Index: Alle {total_unique_sha256s} SHA256s korrekt in pool_refs")

    if os.environ.get("PCLOUD_VALIDATE_STUB_FULL", "1") == "0":
        _log("[validate] Stub-Check übersprungen (PCLOUD_VALIDATE_STUB_FULL=0)")
    elif total_files > 0:
        try:
            missing_stubs = _validate_stubs_batched(cfg, snapshot_dir, manifest_items)
            if missing_stubs:
                errors.append(f"Stubs fehlen: {len(missing_stubs)}")
                for rp in missing_stubs[:10]:
                    errors.append(f"  - Stub fehlt: {rp}")
                if len(missing_stubs) > 10:
                    errors.append(f"  ... und {len(missing_stubs) - 10} weitere")
            else:
                _log(f"[validate] ✓ Stubs: Alle {total_files} vorhanden (100% Coverage)")
        except Exception as e:
            _log(f"[validate][WARN] Stub-Batch-Check fehlgeschlagen: {e} - uebersprungen")

    gc.collect()

    if errors:
        _log(f"[validate] ❌ {len(errors)} Fehler gefunden!")
        return (False, errors)
    _log(f"[validate] ✓✓✓ Snapshot vollständig konsistent (100% Pool-Coverage, {total_unique_sha256s} SHA256s)")
    return (True, [])


# ==============================================================================
# LOCK-FILE MANAGEMENT (Race-Condition Protection)
# ==============================================================================

def create_gc_lock(cfg: dict, dest_root: str, snapshot_name: str, *, dry: bool = False) -> None:
    """
    Erstellt GC-Lock-File um parallele GC-Läufe während Backup zu verhindern.
    
    Lock-File: {dest_root}/.gc_lock
    Format: JSON mit Metadaten (pid, host, started_at, snapshot)
    
    Dieses Lock verhindert dass GC Files löscht die gerade hochgeladen werden!
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root (z.B. /Backup/rtb_1to1)
        snapshot_name: Snapshot-Name (für Debugging)
        dry: Dry-run Mode
    """
    if dry:
        _log("[dry] create_gc_lock: Skip (dry-run)")
        return
    
    import socket
    
    lock_path = f"{dest_root.rstrip('/')}/.gc_lock"
    lock_data = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": time.time(),
        "snapshot": snapshot_name,
        "task": "push_pool_manifest"
    }
    
    try:
        pc.write_json_at_path(cfg, lock_path, lock_data)
        _log(f"[gc-lock] ✓ Lock erstellt: {lock_path}")
    except Exception as e:
        _log(f"[gc-lock][WARN] Konnte Lock nicht erstellen: {e}")


def remove_gc_lock(cfg: dict, dest_root: str, *, dry: bool = False) -> None:
    """
    Entfernt GC-Lock-File nach erfolgreichem Backup.
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root
        dry: Dry-run Mode
    """
    if dry:
        _log("[dry] remove_gc_lock: Skip (dry-run)")
        return
    
    lock_path = f"{dest_root.rstrip('/')}/.gc_lock"
    
    try:
        # Prüfe ob Lock existiert
        lock_stat = pc.stat_file_safe(cfg, path=lock_path)
        
        if lock_stat:
            pc.delete_file(cfg, path=lock_path)
            _log(f"[gc-lock] ✓ Lock entfernt: {lock_path}")
        else:
            _log(f"[gc-lock] Lock existiert nicht (bereits entfernt?)")
    
    except Exception as e:
        _log(f"[gc-lock][WARN] Konnte Lock nicht entfernen: {e}")


# ==============================================================================
# SOURCE-INTEGRITY VALIDATION (Pre-Upload Check)
# ==============================================================================

def validate_source_integrity(manifest: dict, *, deep_check: bool = False) -> tuple[bool, list]:
    """
    PRE-UPLOAD CHECK: Validiert dass alle Files aus Manifest in Source existieren.
    
    KRITISCH: Verhindert dass Manifest auf gelöschte/verschobene Source-Files zeigt!
    
    Checks:
    1. File existiert noch? (os.path.exists)
    2. Optional: Hash stimmt überein? (--deep-check, langsam!)
    
    Args:
        manifest: Manifest Dict
        deep_check: Hash-Verifikation aktivieren (langsam!)
    
    Returns:
        (is_valid, errors)
    """
    _log("[source-integrity] Pre-Upload Source-Check...")
    t_start = time.time()
    errors = []
    
    manifest_files = [it for it in manifest.get("items", []) if it.get("type") == "file"]
    total_files = len(manifest_files)
    
    if total_files == 0:
        _log("[source-integrity] ✓ Keine Files zu prüfen")
        return (True, [])
    
    _log(f"[source-integrity] Prüfe {total_files} Files...")
    
    checked = 0
    missing = 0
    hash_mismatch = 0
    
    for item in manifest_files:
        source_path = item.get("source_path")
        expected_sha256 = item.get("sha256")
        relpath = item.get("relpath", "?")
        
        if not source_path:
            errors.append(f"Manifest-Fehler: Kein source_path für {relpath}")
            continue
        
        # Check 1: File existiert?
        if not os.path.exists(source_path):
            missing += 1
            errors.append(f"Source-File fehlt: {relpath} ({source_path})")
            continue
        
        # Check 2: Optional Deep-Check (Hash-Verifikation)
        if deep_check and expected_sha256:
            import hashlib
            
            try:
                with open(source_path, "rb") as f:
                    actual_sha256 = hashlib.sha256(f.read()).hexdigest().lower()
                
                if actual_sha256 != expected_sha256.lower():
                    hash_mismatch += 1
                    errors.append(f"Source-File geändert: {relpath} (Hash-Mismatch!)")
                    errors.append(f"  Expected: {expected_sha256}")
                    errors.append(f"  Actual:   {actual_sha256}")
            
            except Exception as e:
                errors.append(f"Hash-Check fehlgeschlagen für {relpath}: {e}")
        
        checked += 1
    
    duration = time.time() - t_start
    
    # Result
    if errors:
        _log(f"[source-integrity] ❌ {len(errors)} Fehler gefunden!")
        _log(f"[source-integrity]    Missing: {missing}, Hash-Mismatches: {hash_mismatch}")
        
        for err in errors[:10]:  # Erste 10 zeigen
            _log(f"[source-integrity]   {err}")
        if len(errors) > 10:
            _log(f"[source-integrity]   ... und {len(errors)-10} weitere")
        
        return (False, errors)
    else:
        _log(f"[source-integrity] ✓✓✓ Alle {checked} Files vorhanden ({duration:.1f}s)")
        if deep_check:
            _log(f"[source-integrity] ✓ Deep-Check: Alle Hashes korrekt")
        return (True, [])


# ==============================================================================
# SCOUT & TURBO-DELTA-MODE (Best Match Scout Konzept)
# ==============================================================================

def scout_best_pool_basis(cfg: dict, manifest: dict, archive_dir: str, snapshots_root: str) -> tuple[str | None, float]:
    """
    Findet den effizientesten REMOTE vorhandenen Basis-Snapshot via Jaccard-Ähnlichkeit.

    WICHTIG: Kandidaten sind ausschliesslich Snapshots, die REMOTE unter
    <snapshots_root> tatsaechlich existieren UND fuer die lokal ein Manifest
    vorliegt (Letzteres wird fuer den Diff zwingend benoetigt). Dadurch ist der
    gewaehlte Basis-Snapshot garantiert remote klonbar (copyfolder) UND lokal
    diffbar - der frueher moegliche Fall "Scout waehlt lokal, remote nicht
    vorhanden -> copyfolder API 2005 -> Endlos-Fallback" kann nicht mehr auftreten.

    Strategie:
    - Kandidaten = remote_snapshots ∩ lokale Manifeste (ohne aktuellen, ohne _index).
    - Vergleiche relpath+sha256-Mengen (Jaccard).
    - Early Exit: Bei >95% Match sofort waehlen.

    Returns:
        (snapshot_name, similarity) oder (None, 0.0)
    """
    t_start = time.time()
    verbose = os.environ.get("PCLOUD_VERBOSE") == "1"

    # 1. Welche Snapshots existieren REMOTE? (DAS ist die massgebliche Quelle!)
    remote_snaps = list_remote_snapshot_names(cfg, snapshots_root)
    current_name = manifest.get("snapshot")
    remote_snaps.discard(current_name)

    if not remote_snaps:
        _log(f"[scout] Keine Remote-Snapshots unter {snapshots_root} vorhanden")
        return None, 0.0

    # 2. Aktuelle Dateimenge (relpath → sha256)
    current_files = {
        it.get("relpath"): it.get("sha256")
        for it in manifest.get("items", [])
        if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
    }
    if not current_files:
        _log("[scout] Keine Files im aktuellen Manifest")
        return None, 0.0

    manifests_path = os.path.join(archive_dir, "manifests")
    best_snap = None
    best_score = 0.0

    # Neueste zuerst (Namensformat YYYY-mm-dd-HHMMSS sortiert chronologisch)
    candidates = sorted(remote_snaps, reverse=True)
    _log(f"[scout] Prüfe {len(candidates)} Remote-Snapshots...")

    for snap_name in candidates:
        # Diff braucht ein lokales Manifest des Kandidaten - sonst nicht nutzbar
        basis_manifest_path = os.path.join(manifests_path, f"{snap_name}.json")
        if not os.path.exists(basis_manifest_path):
            if verbose:
                _log(f"[scout]   {snap_name}: remote vorhanden, aber kein lokales Manifest → übersprungen")
            continue

        try:
            with open(basis_manifest_path, "r", encoding="utf-8") as f:
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

            if verbose:
                _log(f"[scout]   {snap_name}: {matches}/{len(current_files)} ({score*100:.1f}%)")

            if score > best_score:
                best_score = score
                best_snap = snap_name

            # Early Exit bei >95%
            if best_score > 0.95:
                break

        except Exception as e:
            if verbose:
                _log(f"[scout]   {snap_name}: Fehler beim Laden: {e}")
            continue

    elapsed = time.time() - t_start
    if best_snap:
        _log(f"[scout] ✓ Best Match (remote): {best_snap} (Similarity: {best_score*100:.1f}%) in {elapsed:.1f}s")
    else:
        _log(f"[scout] Kein geeigneter Remote-Basis-Snapshot gefunden in {elapsed:.1f}s")

    return best_snap, best_score


def _snapshot_stub_relpaths(cfg: dict, snapshot_dir: str) -> set:
    """Relpaths aller .meta.json-Stubs unter snapshot_dir (nach Server-Klon)."""
    snapshot_dir = pc._norm_remote_path(snapshot_dir).rstrip("/")
    result = pc.call_with_backoff(
        pc.listfolder, cfg, path=snapshot_dir, recursive=True, nofiles=False,
    ) or {}
    relpaths: set = set()
    suffix = ".meta.json"

    def _walk(node: dict, cur_path: str) -> None:
        for child in node.get("contents", []) or []:
            name = child.get("name", "")
            child_path = f"{cur_path}/{name}"
            if child.get("isfolder"):
                _walk(child, child_path)
            elif name.endswith(suffix):
                rel = child_path[len(snapshot_dir) + 1 : -len(suffix)]
                if rel:
                    relpaths.add(rel)

    _walk(result.get("metadata", {}) or {}, snapshot_dir)
    return relpaths


def _delta_phase3_cleanup(
    cfg: dict,
    *,
    dest_snapshot_dir: str,
    paths_to_remove: set,
    current_paths: set,
) -> tuple[int, int]:
    """
    Phase 3: veraltete Remote-Stubs entfernen (parallel + Fortschritts-Log).

    Returns:
        (folders_removed, stubs_deleted)
    """
    import concurrent.futures

    t0 = time.time()

    kept_dirs: set[str] = set()
    for rp in current_paths:
        d = os.path.dirname(rp)
        while d:
            kept_dirs.add(d)
            d = os.path.dirname(d)

    dead_top_dirs: set[str] = set()
    single_stubs: list[str] = []
    for rp in paths_to_remove:
        parent = os.path.dirname(rp)
        if parent and parent not in kept_dirs:
            top = parent
            while True:
                gp = os.path.dirname(top)
                if gp and gp not in kept_dirs:
                    top = gp
                else:
                    break
            dead_top_dirs.add(top)
        else:
            single_stubs.append(rp)

    dead_dirs = sorted(dead_top_dirs)
    threads = max(1, PARALLEL_CLEANUP_THREADS)
    progress_every = DELTA_CLEANUP_PROGRESS_EVERY
    total_ops = len(dead_dirs) + len(single_stubs)

    _log(
        f"[delta-mode] Phase 3: Plan — {len(dead_dirs)} Ordner rekursiv, "
        f"{len(single_stubs)} Einzel-Stubs ({total_ops} Ops, Threads={threads})"
    )

    folders_removed = 0
    stubs_deleted = 0
    errors = 0
    done_ops = 0
    last_pct = -1
    lock = threading.Lock()

    def _progress_tick() -> None:
        nonlocal done_ops, last_pct
        with lock:
            done_ops += 1
            if total_ops <= 0:
                return
            pct = int((done_ops / total_ops) * 100)
            show = (
                done_ops % progress_every == 0
                or done_ops == total_ops
                or (pct % 10 == 0 and pct != last_pct)
            )
            if not show:
                return
            last_pct = pct
            elapsed = time.time() - t0
            rate = done_ops / elapsed if elapsed > 0 else 0.0
            remaining_s = (total_ops - done_ops) / rate if rate > 0 else 0.0
            eta_str = (
                f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s / 60)}min"
            )
            _log(
                f"[delta-mode] Phase 3: {done_ops}/{total_ops} ({pct}%) | "
                f"dirs={folders_removed} stubs={stubs_deleted} errors={errors} | "
                f"{eta_str} verbleibend"
            )

    def _delete_dead_dir(rel_dir: str) -> None:
        nonlocal folders_removed, errors
        try:
            pc.deletefolder_recursive(cfg, path=f"{dest_snapshot_dir}/{rel_dir}")
            with lock:
                folders_removed += 1
        except Exception as e:
            with lock:
                errors += 1
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                _log(f"[warn] Konnte toten Ordner nicht löschen: {rel_dir}: {e}")
        finally:
            _progress_tick()

    def _delete_single_stub(rp: str) -> None:
        nonlocal stubs_deleted, errors
        stub_path = f"{dest_snapshot_dir}/{rp}.meta.json"
        try:
            stub_md = pc.stat_file_safe(cfg, path=stub_path)
            if stub_md and stub_md.get("fileid"):
                pc.delete_file(cfg, fileid=int(stub_md["fileid"]))
                with lock:
                    stubs_deleted += 1
        except Exception as e:
            with lock:
                errors += 1
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                _log(f"[warn] Konnte Stub nicht löschen: {stub_path}: {e}")
        finally:
            _progress_tick()

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        if dead_dirs:
            _log(f"[delta-mode] Phase 3: Lösche {len(dead_dirs)} tote Ordner (rekursiv)...")
            list(ex.map(_delete_dead_dir, dead_dirs))
        if single_stubs:
            _log(f"[delta-mode] Phase 3: Lösche {len(single_stubs)} Einzel-Stubs...")
            list(ex.map(_delete_single_stub, single_stubs))

    return folders_removed, stubs_deleted


def push_pool_delta_mode(cfg: dict, manifest: dict, dest_root: str, basis_snapshot_name: str,
                         *, dry: bool = False, verbose: bool = False) -> dict:
    """
    Turbo-Delta-Mode: Synchronisiert neuen Snapshot basierend auf Klon eines alten.
    
    Workflow:
    1. copyfolder(basis_snapshot) → neuer Snapshot (5 Sek statt 73 Min!)
    2. Diff berechnen (Added, Changed, Removed)
    3. Bereinigung: Remote-Stubs ohne Pendant im aktuellen Manifest entfernen
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
    
    # Timeout-Protection: copyfolder ist ein Socket-Read-Timeout pro Antwort-Byte.
    # Bei 80k Stubs + 70k Ordnern dauert der Server-seitige Copy ~600s (beobachtet
    # 2026-06-04). PCLOUD_COPYFOLDER_TIMEOUT hat immer Vorrang (mindestens dieser Wert),
    # auch wenn PCLOUD_TIMEOUT höher als 60 aber unter copyfolder liegt (z.B. 120).
    current_timeout = int(cfg.get("timeout", 30))
    copyfolder_timeout = int(os.environ.get("PCLOUD_COPYFOLDER_TIMEOUT", "700"))
    if current_timeout < copyfolder_timeout:
        cfg["timeout"] = copyfolder_timeout
        _log(f"[delta-mode] Timeout erhöht: {current_timeout}s → {cfg['timeout']}s (copyfolder)")
    else:
        _log(f"[delta-mode] Timeout beibehalten: {current_timeout}s")

    # Marker
    marker_started = f"{dest_snapshot_dir}/.upload_started"
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    
    # Ziel-Status prüfen.
    # - vollständig (.upload_complete vorhanden) -> nichts zu tun
    # - existiert, aber unvollständig            -> abgebrochener Lauf: komplett
    #   verwerfen und sauber neu aufsetzen. Resume/Reconcile ist NICHT zuverlässig:
    #   der content_index wird erst am Ende (aus dem RAM) geschrieben, und listfolder
    #   liefert noch keine SHA256 zum Abgleich. Daher: fresh start via copyfolder.
    if not dry:
        existing_fid = pc.stat_folderid_fast(cfg, dest_snapshot_dir)
        if existing_fid:
            if _upload_complete_matches_snapshot(cfg, marker_complete, snapshot_name):
                _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
                return {"uploaded": 0, "stubs": 0, "resumed": False, "mode": "delta"}
            if pc.stat_file_safe(cfg, path=marker_complete):
                _log(f"[delta-mode] .upload_complete vorhanden, aber snapshot-Feld passt nicht "
                     f"(erwartet {snapshot_name}) → verwerfe und starte sauber neu")
            else:
                _log(f"[delta-mode] Ziel existiert, aber unvollständig (kein .upload_complete) → verwerfe und starte sauber neu")
            pc.deletefolder_recursive_wait(
                cfg,
                path=dest_snapshot_dir,
                log=_log,
            )

            # STAMMDATEN BEREINIGEN: snapshot_name aus pool_refs entfernen.
            # Der Snapshot-Ordner inkl. aller Stubs wurde soeben geloescht. Ohne
            # Bereinigung wuerden pool_refs[sha].snapshots[snapshot_name] noch
            # existieren -> Phase 4 ueberspringt alle added-Files als "bereits fertig"
            # -> 0 Stubs geschrieben. Indexstand muss dem Remote-Zustand entsprechen.
            try:
                _wi_idx = load_content_index(cfg, snapshots_root)
                _wi_n = _purge_snapshot_refs_from_index(_wi_idx, snapshot_name)
                if _wi_n > 0:
                    save_content_index(cfg, snapshots_root, _wi_idx, dry=False)
                    _log(f"[delta-mode] Stammdaten bereinigt: {snapshot_name} aus {_wi_n} pool_refs-Eintraegen entfernt")
            except Exception as e:
                _log(f"[delta-mode][warn] Stammdaten-Bereinigung fehlgeschlagen: {e}")

            # Lokalen Index-Checkpoint dieses Snapshots verwerfen
            try:
                import tempfile as _tf
                _idx_dir = os.getenv("PCLOUD_TEMP_DIR", _tf.gettempdir())
                _idx_path = os.path.join(_idx_dir, f"pcloud_pool_index_{snapshot_name}.json")
                if os.path.exists(_idx_path):
                    os.remove(_idx_path)
            except Exception:
                pass

    # === PHASE 1: SERVER-SIDE COPY (INSTANT STRUKTUR!) ===
    _log(f"[delta-mode] Phase 1: Klone Basis-Snapshot...")
    t_copy_start = time.time()
    
    if not dry:
        copy_attempts = max(1, int(os.environ.get("PCLOUD_COPYFOLDER_ATTEMPTS", "3")))
        copy_err: Exception | None = None
        for copy_try in range(1, copy_attempts + 1):
            try:
                # WICHTIG: copycontentonly=True kopiert die KINDER von from_path direkt
                # nach to_folderid. to_folderid MUSS daher der Snapshot-Ordner SELBST
                # sein - nicht der _snapshots-Parent.
                dest_fid = pc.ensure_path(cfg, dest_snapshot_dir)
                pc.copyfolder(
                    cfg,
                    from_path=basis_snapshot_dir,
                    to_folderid=dest_fid,
                    copycontentonly=True,
                )
                copy_duration = time.time() - t_copy_start
                _log(f"[delta-mode] ✓ Struktur geklont in {copy_duration:.1f}s")
                copy_err = None
                break
            except Exception as e:
                copy_err = e
                _log(
                    f"[delta-mode][ERROR] copyfolder Versuch {copy_try}/{copy_attempts}: {e}"
                )
                if copy_try < copy_attempts:
                    time.sleep(min(30.0 * copy_try, 90.0))

        if copy_err is not None:
            raise RuntimeError(
                f"[delta-mode] copyfolder fehlgeschlagen nach {copy_attempts} Versuch(en) "
                f"(kein Full-Pool-Fallback): {copy_err}"
            ) from copy_err

        # Vom Basis mitkopierte Status-Marker entfernen. Sonst gilt der frisch
        # geklonte Snapshot faelschlich als 'bereits vollstaendig' (Complete-Marker
        # des Basis) bzw. traegt einen fremden Started-Marker.
        for _marker in (marker_complete, marker_started):
            try:
                _mmd = pc.stat_file_safe(cfg, path=_marker)
                if _mmd and _mmd.get("fileid"):
                    pc.delete_file(cfg, fileid=int(_mmd["fileid"]))
            except Exception as _me:
                if verbose:
                    _log(f"[delta-mode][warn] Marker-Cleanup ({_marker}): {_me}")
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
        raise RuntimeError(
            f"[delta-mode] Basis-Manifest fehlt – Abbruch (kein Full-Pool-Fallback): {e}"
        ) from e
    
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
    
    # === PHASE 3: BEREINIGUNG (Remote-Stubs ohne Pendant im aktuellen Manifest) ===
    paths_to_remove = set(deleted_paths)
    if not dry:
        t_stublist = time.time()
        remote_stub_relpaths = _snapshot_stub_relpaths(cfg, dest_snapshot_dir)
        clone_orphans = remote_stub_relpaths - current_paths
        if clone_orphans:
            n_from_diff = len(deleted_paths)
            n_clone_only = len(clone_orphans - deleted_paths)
            paths_to_remove |= clone_orphans
            _log(
                f"[delta-mode] Klon-Abgleich: {len(clone_orphans)} Remote-Stubs nicht im "
                f"aktuellen Manifest ({n_from_diff} aus Diff, {n_clone_only} historischer Überhang) "
                f"in {time.time() - t_stublist:.1f}s"
            )

    if paths_to_remove and not dry:
        _log(f"[delta-mode] Phase 3: Entferne {len(paths_to_remove)} veraltete Einträge...")
        t_cleanup_start = time.time()
        folders_removed, deleted_count = _delta_phase3_cleanup(
            cfg,
            dest_snapshot_dir=dest_snapshot_dir,
            paths_to_remove=paths_to_remove,
            current_paths=current_paths,
        )
        cleanup_duration = time.time() - t_cleanup_start
        _log(
            f"[delta-mode] ✓ Bereinigt: {folders_removed} tote Ordner rekursiv, "
            f"{deleted_count} Einzel-Stubs in {cleanup_duration:.1f}s"
        )
    elif paths_to_remove:
        _log(f"[dry] Würde {len(paths_to_remove)} veraltete Einträge entfernen (tote Ordner rekursiv + Einzel-Stubs)")
    
    # === PHASE 4: UPDATE (Neue/Geänderte Files verarbeiten) ===
    tasks = list(added_paths | changed_paths)
    
    if tasks:
        _log(f"[delta-mode] Phase 4: Verarbeite {len(tasks)} neue/geänderte Files...")
        
        # Index laden
        import tempfile
        _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
        _local_index_path = os.path.join(_local_index_dir, f"pcloud_pool_index_{snapshot_name}.json")
        os.makedirs(_local_index_dir, exist_ok=True)
        
        t_idx = time.time()
        if os.path.exists(_local_index_path):
            index = load_content_index_local(_local_index_path)
            _idx_src = "lokal"
        else:
            index = load_content_index(cfg, snapshots_root)
            _idx_src = "remote"
        
        pool_refs = index.setdefault("pool_refs", {})
        _log(
            f"[delta-mode] Phase 4: Index geladen ({len(pool_refs)} pool_refs, {_idx_src}) "
            f"in {time.time() - t_idx:.1f}s"
        )

        # Nach manuellem Remote-Delete oder fehlgeschlagenem Lauf haengen pool_refs-Eintraege
        # oft noch im Index, obwohl Stubs fehlen -> Phase 4 wuerde sonst 0 Stubs schreiben.
        _purged_refs = _purge_snapshot_refs_from_index(index, snapshot_name)
        if _purged_refs > 0 and not dry:
            _log(f"[delta-mode] Phase 4: pool_refs bereinigt ({_purged_refs} Eintraege fuer {snapshot_name})")
            save_content_index_local(_local_index_path, index)
            save_content_index(cfg, snapshots_root, index, dry=False)
        
        # Stats
        uploaded = 0
        reused = 0
        stubs = 0
        upload_ms = 0.0
        write_ms = 0.0
        stubs_to_write = []
        failed = []  # relpaths, deren Originaldatei NICHT in den Pool geladen werden konnte
        _state_lock = threading.Lock()
        _total_tasks = len(tasks)
        _done_tasks = 0
        _last_progress_pct = 0
        _phase4_start = time.time()
        _progress_every = int(os.environ.get("PCLOUD_DELTA_PROGRESS_EVERY", "100"))

        def _phase4_progress_tick() -> None:
            nonlocal _done_tasks, _last_progress_pct
            with _state_lock:
                _done_tasks += 1
                current_pct = int((_done_tasks / _total_tasks) * 100) if _total_tasks else 100
                show_progress = (
                    _done_tasks % _progress_every == 0
                    or _done_tasks == _total_tasks
                    or (current_pct % 10 == 0 and current_pct != _last_progress_pct)
                )
                if not show_progress:
                    return
                _last_progress_pct = current_pct
                elapsed = time.time() - _phase4_start
                if _done_tasks > 0 and elapsed > 0:
                    rate = _done_tasks / elapsed
                    remaining_s = (_total_tasks - _done_tasks) / rate if rate > 0 else 0
                else:
                    remaining_s = 0
                eta_str = (
                    f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s / 60)}min"
                )
                _log(
                    f"[delta-mode] Phase 4: {_done_tasks}/{_total_tasks} ({current_pct}%) | "
                    f"uploaded={uploaded} reused={reused} failed={len(failed)} | {eta_str} verbleibend"
                )
        
        # Upload-Funktion (wie in push_pool_mode)
        def _upload_to_pool(abs_src: str, sha256: str) -> tuple:
            nonlocal upload_ms
            pool_path_rel = _get_pool_path(sha256)
            # Absolute Pfadangabe für Log-Ausgabe und pCloud-Operationen
            pool_path_abs = f"{dest_root.rstrip('/')}/{pool_path_rel}"
            
            parent_abs = os.path.dirname(pool_path_abs.rstrip("/"))
            if parent_abs:
                # cached: Pool ist zweistufig (_pool/XX), 256 feste Ordner aus Phase 0
                # -> nach dem 1. Mal pro Prefix kein API-Call mehr (statt 1x pro File)
                pc.ensure_path_cached(cfg, parent_abs)

            if dry:
                _dry_sampler.log("upload", f"upload pool: {pool_path_abs}  <- {abs_src}")
                return (None, None)

            # Check ob bereits existiert
            try:
                existing_stat = pc.stat_file_safe(cfg, path=pool_path_abs)
                if existing_stat:
                    pool_fileid = existing_stat.get("fileid")
                    pcloud_hash = existing_stat.get("hash")
                    if pool_fileid:
                        try:
                            with _metrics_lock:
                                globals()["MET_POOL_REUSED"] += 1
                        except Exception:
                            pass
                        return (pool_fileid, pcloud_hash)
            except Exception:
                pass
            
            t0 = time.time()
            res = _upload_file_smart(cfg, abs_src, pool_path_abs, dry=dry)
            with _state_lock:
                upload_ms += (time.time() - t0) * 1000.0
            # Sichtbar machen, dass die Originaldatei real in den Pool geschrieben wurde
            # (nur echte Uploads; Dedup-Treffer kehren oben frueher zurueck).
            _log(f"[pool] ✓ Original in Pool geladen: {pool_path_rel}  <- {abs_src}")
            
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
            pool_path_rel = _get_pool_path(sha256)
            pool_path_abs = f"{dest_root.rstrip('/')}/{pool_path_rel}"
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
                "pool_path": pool_path_abs,
                "pool_fileid": pool_fileid,
                "snapshot": snapshot_name,
            }
            if not dry:
                stubs_to_write.append((meta_path, payload))
            stubs += 1
        
        def _process_file(relpath: str) -> None:
            nonlocal uploaded, reused
            
            try:
                file_item = current_files[relpath]
                abs_src = file_item.get("source_path", "")
                sha256 = (file_item.get("sha256") or "").lower()
                
                if not abs_src or not sha256:
                    # Manifest-Defekt: ohne Quelle/SHA kann die Datei nicht in den Pool ->
                    # als Fehler werten, damit der Snapshot nicht still unvollstaendig wird.
                    with _state_lock:
                        failed.append(relpath)
                    _log(f"[ERROR] {relpath}: kein source_path/sha256 im Manifest")
                    return

                # Resume: Remote-Stub muss existieren (Index allein reicht nicht).
                # Geaenderte Files (changed_paths) immer neu schreiben — geklonter Stub ist veraltet.
                if relpath not in changed_paths and not dry:
                    _stub_remote = f"{dest_snapshot_dir}/{relpath}.meta.json"
                    try:
                        _stub_md = pc.stat_file_safe(cfg, path=_stub_remote)
                        if _stub_md and _stub_md.get("fileid"):
                            with _state_lock:
                                reused += 1
                            return
                    except Exception:
                        pass
                
                # Upload zu Pool
                try:
                    pool_fileid, pcloud_hash = _upload_to_pool(abs_src, sha256)
                    
                    with _state_lock:
                        _register_snap(pool_refs, sha256, snapshot_name, relpath,
                                       fileid=pool_fileid, hash=pcloud_hash,
                                       size=file_item.get("size"))
                        uploaded += 1
                    
                    _queue_stub(relpath, file_item, pool_fileid, pcloud_hash, sha256)
                    
                except Exception as e:
                    _log(f"[ERROR] {relpath}: Upload fehlgeschlagen: {e}")
                    with _state_lock:
                        failed.append(relpath)
            finally:
                _phase4_progress_tick()
        
        # Parallel verarbeiten
        threads = PARALLEL_UPLOAD_THREADS
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(_process_file, tasks))

        # Fail-fast (Reihenfolge!): konnte eine benoetigte Originaldatei NICHT in den
        # Pool geladen werden, darf der Snapshot NICHT finalisiert werden - KEINE Stubs
        # schreiben, KEINEN Index speichern, KEINEN Complete-Marker setzen. Sonst
        # entstuende ein Stub/Index-Eintrag ohne zugehoeriges Pool-Objekt (genau der
        # inkonsistente Zustand, den sonst erst die End-Validation faengt). Das gc-lock
        # raeumt der aeussere finally-Block beim Propagieren der Exception.
        if not dry and failed:
            _log(f"[delta-mode][ERROR] {len(failed)} Datei(en) konnten NICHT in den Pool geladen werden - Snapshot wird NICHT finalisiert:")
            for _fp in failed[:10]:
                _log(f"[delta-mode][ERROR]   - {_fp}")
            if len(failed) > 10:
                _log(f"[delta-mode][ERROR]   ... und {len(failed)-10} weitere")
            raise RuntimeError(f"Pool-Upload fehlgeschlagen fuer {len(failed)} Datei(en) - Snapshot nicht finalisiert")

        _log(f"[delta-mode] ✓ Files verarbeitet: {uploaded} neue, {reused} wiederverwendet")
        
        # Stubs schreiben
        if stubs_to_write and not dry:
            _log(f"[delta-mode] Schreibe {len(stubs_to_write)} Stubs...")
            t0 = time.time()
            _batch_write_stubs(cfg, stubs_to_write, dry=False)
            write_ms = (time.time() - t0) * 1000.0
        
        # Auch UNVERAENDERTE (geklonte) Files fuer diesen Snapshot registrieren,
        # sonst fehlt der Snapshot in pool_refs[sha].snapshots -> Validation/GC falsch.
        if not dry:
            for _it in current_files.values():
                _sha = _it.get("sha256")
                if not _sha:
                    continue
                _register_snap(pool_refs, _sha, snapshot_name, _it.get("relpath", ""),
                               size=_it.get("size"))

        # Index erst NACH erfolgreicher Validation persistieren (siehe unten).
        # Vorher nur im RAM: sonst steht der Snapshot in pool_refs obwohl kein
        # .upload_complete gesetzt wurde -> Retry ueberspringt Stub-Writes.
    else:
        _log("[delta-mode] Keine Änderungen - Snapshot identisch mit Basis")
        uploaded = 0
        reused = 0
        stubs = 0
        upload_ms = 0.0
        write_ms = 0.0
        # Index trotzdem laden + neuen (geklonten) Snapshot fuer ALLE SHAs registrieren
        # (sonst UnboundLocalError bei Validation + fehlende pool_refs-Eintraege).
        import tempfile
        _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
        _local_index_path = os.path.join(_local_index_dir, f"pcloud_pool_index_{snapshot_name}.json")
        os.makedirs(_local_index_dir, exist_ok=True)
        if os.path.exists(_local_index_path):
            index = load_content_index_local(_local_index_path)
        else:
            index = load_content_index(cfg, snapshots_root)
        pool_refs = index.setdefault("pool_refs", {})
        if not dry:
            _reg = 0
            for _it in current_files.values():
                _sha = _it.get("sha256")
                if not _sha:
                    continue
                if snapshot_name not in _snap_names(pool_refs.get(_sha)):
                    _reg += 1
                _register_snap(pool_refs, _sha, snapshot_name, _it.get("relpath", ""),
                               size=_it.get("size"))
            _log(f"[delta-mode] {_reg} Snapshot-Referenzen im Index ergaenzt (geklonter Snapshot)")
            # Remote/Local-Index: erst nach Validation (siehe unten)

    # === POST-UPLOAD VALIDATION (wie Full-Pool-Mode!) ===
    marker_data = {
        "snapshot": snapshot_name,
        "completed_at": time.time(),
        "uploaded": uploaded,
        "stubs": stubs,
        "reused": reused,
        "duration": time.time() - t_start,
        "mode": "delta",
        "basis": basis_snapshot_name,
    }

    validation_enabled = os.environ.get("PCLOUD_VALIDATE_UPLOAD", "1") != "0"
    if validation_enabled and not dry:
        _log("[delta-mode] Starte Post-Upload Validation...")
        is_valid, validation_errors = validate_pool_snapshot(cfg, dest_snapshot_dir, pool_root, manifest, index, dry=dry)
        
        if not is_valid:
            _log(f"[delta-mode][ERROR] Validation fehlgeschlagen: {len(validation_errors)} Fehler!")
            _log("[delta-mode][ERROR] Complete-Marker wird NICHT gesetzt (inkonsistenter Snapshot)")
            for err in validation_errors[:5]:  # Erste 5 Fehler zeigen
                _log(f"[delta-mode][ERROR]   {err}")
            # Index-Checkpoint verwerfen (duerfte ohne .upload_complete nie persistiert sein)
            try:
                import tempfile as _tf
                _fail_idx = os.path.join(os.getenv("PCLOUD_TEMP_DIR", _tf.gettempdir()),
                                         f"pcloud_pool_index_{snapshot_name}.json")
                if os.path.exists(_fail_idx):
                    os.remove(_fail_idx)
            except Exception:
                pass
            raise RuntimeError(f"Snapshot-Validation fehlgeschlagen: {len(validation_errors)} Fehler gefunden")
        else:
            _log("[delta-mode] ✓ Validation erfolgreich - Snapshot ist konsistent")
            _finalize_after_validation_delta(
                cfg, snapshots_root, index, snapshot_name, dest_snapshot_dir, marker_data, dry=dry
            )
    elif not validation_enabled:
        _log("[delta-mode] Validation übersprungen (PCLOUD_VALIDATE_UPLOAD=0)")
        _finalize_after_validation_delta(
            cfg, snapshots_root, index, snapshot_name, dest_snapshot_dir, marker_data, dry=dry
        )

    # === REMOTE INDEX-ARCHIVE: in _finalize_after_validation_delta ===

    # === METRIK-GLOBALS synchronisieren ===
    # Der Delta-Pfad fuehrt eigene lokale Zaehler; ohne diesen Sync zeigte die globale
    # [metrics]-Summary uploaded_files=0 trotz erfolgtem Pool-Upload. (MET_POOL_REUSED
    # wird bereits am Dedup-Treffer in _upload_to_pool hochgezaehlt.)
    try:
        with _metrics_lock:
            globals()["MET_UPLOADED_FILES"] += uploaded
            globals()["MET_STUBS_WRITTEN"] += stubs
    except Exception:
        pass

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


def push_pool_mode(cfg: dict, manifest: dict, dest_root: str, *, dry: bool = False, verbose: bool = False, use_scout: bool = True) -> dict:
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
    
    # ============================================================================
    # === PRE-FLIGHT CHECKS (Source-Integrity + GC-Lock) ===
    # ============================================================================
    
    # === 1. SOURCE-INTEGRITY-CHECK (Pre-Upload) ===
    deep_check = os.environ.get("PCLOUD_SOURCE_DEEP_CHECK") == "1"
    
    is_valid, integrity_errors = validate_source_integrity(manifest, deep_check=deep_check)
    
    if not is_valid:
        _log(f"[ERROR] Source-Integrity-Check fehlgeschlagen! ({len(integrity_errors)} Fehler)")
        _log(f"[ERROR] Backup abgebrochen (Source inkonsistent)")
        return {
            "error": "source_integrity_failed",
            "errors": integrity_errors,
            "uploaded": 0,
            "stubs": 0
        }
    
    # === 2. GC-LOCK erstellen (Race-Protection) ===
    try:
        create_gc_lock(cfg, dest_root, snapshot_name, dry=dry)
    except Exception as e:
        _log(f"[WARN] Konnte GC-Lock nicht erstellen: {e} - fahre trotzdem fort")
    
    # Cleanup-Helper für Lock-Removal
    def _cleanup_lock():
        """Entfernt Lock auch bei Fehler"""
        try:
            remove_gc_lock(cfg, dest_root, dry=dry)
        except Exception as e:
            _log(f"[WARN] Lock-Cleanup fehlgeschlagen: {e}")
    
    try:
        # === Ab hier: Eigentlicher Upload-Code (Lock aktiv!) ===
        
        # === SCOUT: Best-Match Basis-Snapshot finden ===
        scout_enabled = os.environ.get("PCLOUD_SCOUT_ENABLED", "1") != "0"
        scout_threshold = float(os.environ.get("PCLOUD_SCOUT_THRESHOLD", "0.70"))
        
        if scout_enabled and use_scout:
            _log("[pool-mode] Scout: Suche besten Basis-Snapshot (remote)...")
            basis_snapshot, similarity = scout_best_pool_basis(cfg, manifest, archive_dir, snapshots_root)

            if basis_snapshot and similarity >= scout_threshold:
                _log(f"[pool-mode] ✓ Scout Match: {basis_snapshot} ({similarity*100:.1f}%)")
                _log(f"[pool-mode] → Nutze Turbo-Delta-Mode!")

                # Delegation an push_pool_delta_mode
                # (Lock wird vom finally-Block entfernt)
                return push_pool_delta_mode(
                    cfg, manifest, dest_root, basis_snapshot,
                    dry=dry, verbose=verbose
                )
            else:
                if basis_snapshot:
                    _log(f"[pool-mode] Scout Best: {basis_snapshot} ({similarity*100:.1f}%) - unter Schwelle ({scout_threshold*100:.0f}%)")
                else:
                    _log(f"[pool-mode] Scout: Kein geeigneter Remote-Basis gefunden")
                _log(f"[pool-mode] → Full-Pool-Mode")
        elif scout_enabled and not use_scout:
            _log("[pool-mode] Scout übersprungen (Fallback nach Delta-Fehler) → Full-Pool-Mode")
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
        
        # Prüfen ob Upload bereits abgeschlossen (snapshot-Feld muss passen!)
        # Zuerst Ordner-Existenz — stat auf Marker ohne Parent wirft API 2002 (Full-Pool-Crash).
        incomplete_upload = False
        if pc.stat_folderid_fast(cfg, dest_snapshot_dir):
            if _upload_complete_matches_snapshot(cfg, marker_complete, snapshot_name):
                _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
                return {"uploaded": 0, "stubs": 0, "resumed": False}
            if pc.stat_file_safe(cfg, path=marker_complete):
                _log(
                    f"[warn] .upload_complete vorhanden, aber snapshot-Feld passt nicht "
                    f"(erwartet {snapshot_name}) — behandle als unvollständig"
                )
                incomplete_upload = True
            elif pc.stat_file_safe(cfg, path=marker_started):
                incomplete_upload = True
                _log(f"[warn] Unvollständiger Upload erkannt für {snapshot_name} - starte neu")
        
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
            # cached: spart bei der grossen Mehrheit der Aufrufe (Pool-Parent _pool/XX,
            # bereits angelegte Snapshot-Ordner) den API-Roundtrip nach dem 1. Mal.
            pc.call_with_backoff(pc.ensure_path_cached, cfg, path)
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
            # Rekursives listfolder über kompletten Pool, mit Retry (transiente API-Fehler)
            result = pc.call_with_backoff(pc.listfolder, cfg, path=pool_root,
                                          recursive=True, nofiles=False, attempts=4, max_sleep=30.0)
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
            if pc.is_transient_api_error(e):
                raise RuntimeError(
                    f"[preflight] Pool-Scan fehlgeschlagen (API-Verbindung) – "
                    f"Abbruch statt Full-Upload-Erzwingung: {e}"
                ) from e
            # Nicht-transiente Fehler (z.B. Ordner fehlt): alle Files als neu planen;
            # _upload_to_pool dedupliziert pro File via stat.
            _log(f"[preflight][WARN] Pool-Scan nach Retries fehlgeschlagen: {e}")
            _log(f"[preflight][WARN] → Erzwinge Full-Upload (alle als neu); stat dedupliziert pro File.")
            physical_pool_sha256s = set()
        
        # 3. Index-basierte SHA256s (für Vergleich & Index-Reparatur-Erkennung)
        index_pool_sha256s = set(pool_refs.keys())
        _log(f"[preflight] Index: {len(index_pool_sha256s)} SHA256s registriert")
        
        # 4. Delta-Liste: SHA256s die PHYSISCH Upload benötigen (nicht mehr Index-basiert!)
        delta_sha256s = set(manifest_sha256_to_item.keys()) - physical_pool_sha256s
        
        # 5. Reused-Liste: SHA256s PHYSISCH im Pool (für diesen Snapshot aber neu)
        # Prüfe ob bereits für DIESEN Snapshot registriert
        already_in_snapshot = set()
        for sha in manifest_sha256_to_item.keys():
            if snapshot_name in _snap_names(pool_refs.get(sha)):
                already_in_snapshot.add(sha)
        
        # Echte Reused: Physisch vorhanden, aber nicht für diesen Snapshot
        reused_sha256s = (set(manifest_sha256_to_item.keys()) & physical_pool_sha256s) - already_in_snapshot
        
        # 6. Index-Reparatur-Kandidaten: Physisch vorhanden, aber nicht im Index (oder unvollständig)
        # Wir betrachten auch Files als reparaturbedürftig, die zwar im Index sind, aber keine fileid haben.
        needs_index_update = set()
        for sha in reused_sha256s:
            entry = pool_refs.get(sha)
            if not entry or (isinstance(entry, dict) and not entry.get("fileid")):
                needs_index_update.add(sha)
        
        if needs_index_update:
            _log(f"[preflight] ⚠️ Index-Reparatur nötig: {len(needs_index_update)} Files im Pool benötigen Metadaten-Erfassung")
            _log(f"[preflight]    → Erfasse fileids via stat_file...")
            for sha in needs_index_update:
                if dry: continue
                try:
                    p_path = f"{dest_root.rstrip('/')}/{_get_pool_path(sha)}"
                    md = pc.stat_file_safe(cfg, path=p_path)
                    if md and md.get("fileid"):
                        fid = md["fileid"]
                        phash = md.get("hash")
                        size = md.get("size")
                        
                        # Coords nachtragen, Snapshots-Map (inkl. relpaths) ERHALTEN
                        entry = pool_refs.get(sha)
                        if not isinstance(entry, dict):
                            entry = {"fileid": None, "hash": None, "size": None,
                                     "snapshots": {n: [] for n in _snap_names(entry)}}
                            pool_refs[sha] = entry
                        elif not isinstance(entry.get("snapshots"), dict):
                            entry["snapshots"] = {n: [] for n in _snap_names(entry)}
                        entry["fileid"] = fid
                        entry["hash"] = phash
                        entry["size"] = size
                        index_changed = True
                except Exception:
                    pass
        
        preflight_duration = time.time() - t_preflight_start
        _log(f"[preflight] Delta: {len(delta_sha256s)} benötigen Upload ({len(delta_sha256s)*100/len(manifest_sha256_to_item):.1f}%)")
        _log(f"[preflight] Reused: {len(reused_sha256s)} aus Pool wiederverwendet ({len(reused_sha256s)*100/len(manifest_sha256_to_item):.1f}%)")
        _log(f"[preflight] Skipped: {len(already_in_snapshot)} bereits für Snapshot registriert")
        _log(f"[preflight] Abgeschlossen in {preflight_duration:.2f}s")
        
        # 5. File-Liste filtern: Nur noch Delta-Files verarbeiten
        delta_items = [it for it in manifest_files if it.get("sha256") in delta_sha256s]
        reused_items = [it for it in manifest_files if it.get("sha256") in reused_sha256s]
        skipped_items = [it for it in manifest_files if it.get("sha256") in already_in_snapshot]
        
        _log(f"[preflight] Upload-Plan: {len(delta_items)} Files uploaden, {len(reused_items)} reused, {len(skipped_items)} skipped")

        # --- PHASE 2: UPLOAD (nur Delta!) ---
        _log(f"[pool-mode] Phase 2: Upload {len(delta_items)} Delta-Files...")
        
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
            
            pool_path_rel = _get_pool_path(sha256)
            # Absolute Pfadangabe für Log-Ausgabe und pCloud-Operationen
            pool_path_abs = f"{dest_root.rstrip('/')}/{pool_path_rel}"
            
            parent_abs = os.path.dirname(pool_path_abs.rstrip("/"))
            if parent_abs:
                _ensure(parent_abs)
            
            if dry:
                _dry_sampler.log("upload", f"upload pool: {pool_path_abs}  <- {abs_src}")
                return (None, None)
            
            # Check ob bereits existiert (Dedupe!)
            try:
                existing_stat = pc.stat_file_safe(cfg, path=pool_path_abs)
                if existing_stat:
                    pool_fileid = existing_stat.get("fileid")
                    pcloud_hash = existing_stat.get("hash")
                    if pool_fileid:
                        if os.environ.get("PCLOUD_VERBOSE") == "1":
                            _log(f"[pool] ✓ EXISTS: {pool_path_abs} (fileid={pool_fileid})")
                        try:
                            with _metrics_lock:
                                globals()["MET_POOL_REUSED"] += 1
                        except Exception:
                            pass
                        return (pool_fileid, pcloud_hash)
            except Exception:
                pass
            
            # Progress-Hinweis für große Dateien
            file_size = os.path.getsize(abs_src)
            if file_size > 100 * 1024**2:  # > 100MB
                _log(f"[upload] Starte Upload: {sha256[:16]}... ({file_size/1024**2:.1f} MB)")
            
            t0 = time.time()
            res = _upload_file_smart(cfg, abs_src, pool_path_abs, dry=dry)
            elapsed_ms = (time.time() - t0) * 1000.0
            
            _log(f"[pool] upload: {abs_src} → {pool_path_abs}")
            
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
                    stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=pool_path_abs) or {}
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
            
            pool_path_rel = _get_pool_path(sha256)
            pool_path_abs = f"{dest_root.rstrip('/')}/{pool_path_rel}"
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
                "pool_path": pool_path_abs,
                "pool_fileid": pool_fileid,
                "snapshot": snapshot_name,
            }
            if dry:
                _dry_sampler.log("stub", f"write stub: {meta_path}")
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
        # === PHASE 1: FOLDER CREATION ===
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

        if not dry:
            from collections import defaultdict

            # --- Checkpoint: Resume nach Abbruch ohne teuren listfolder ---
            # Datei: {archive_dir}/state/folder_checkpoint_{snapshot_name}.json
            # Beim ersten Lauf: listfolder → missing_folders berechnen → Checkpoint speichern.
            # Beim Resume: Checkpoint laden → listfolder überspringen → ab created_count weitermachen.
            _state_dir = os.path.join(archive_dir, "state")
            _cp_path = os.path.join(_state_dir, f"folder_checkpoint_{snapshot_name}.json")
            _checkpoint = None
            if os.path.exists(_cp_path):
                try:
                    with open(_cp_path, encoding="utf-8") as _f:
                        _cp = json.load(_f)
                    if _cp.get("snapshot") == snapshot_name and _cp.get("status") == "in_progress":
                        _checkpoint = _cp
                        _log(f"[folders] Resume-Checkpoint: {_cp['created_count']}/{_cp['total_folders']} bereits erstellt")
                except Exception as _e:
                    _log(f"[folders] Checkpoint lesen fehlgeschlagen: {_e} – starte neu")

            if _checkpoint:
                # Resume: gespeicherte (depth-geordnete) Ordnerliste verwenden
                _flat_ordered = _checkpoint["flat_ordered"]
                _resume_from  = _checkpoint["created_count"]
                total_folders = _checkpoint["total_folders"]
            else:
                # Erster Lauf: Remote-Zustand via listfolder ermitteln
                remote_folders = set()
                try:
                    result = pc.call_with_backoff(
                        pc.listfolder, cfg, path=dest_snapshot_dir, recursive=True, nofiles=True)
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

                # Tiefengeordnete, deterministisch sortierte Liste fehlender Ordner
                missing_folders = manifest_folders - remote_folders
                _by_depth_init = defaultdict(list)
                for _r in missing_folders:
                    _by_depth_init[_r.count("/")].append(_r)
                _flat_ordered = []
                for _d in sorted(_by_depth_init.keys()):
                    _flat_ordered.extend(sorted(_by_depth_init[_d]))
                _resume_from  = 0
                total_folders = len(_flat_ordered)

                # Checkpoint initial speichern (enthält flat_ordered für möglichen Resume)
                if _flat_ordered:
                    os.makedirs(_state_dir, exist_ok=True)
                    try:
                        with open(_cp_path, "w", encoding="utf-8") as _f:
                            json.dump({
                                "snapshot":      snapshot_name,
                                "total_folders": total_folders,
                                "flat_ordered":  _flat_ordered,
                                "created_count": 0,
                                "status":        "in_progress",
                            }, _f, ensure_ascii=False)
                        _log(f"[folders] Checkpoint angelegt: {_cp_path}")
                    except Exception as _e:
                        _log(f"[folders] Checkpoint schreiben fehlgeschlagen: {_e}")

            # Nur die noch ausstehenden Ordner verarbeiten
            _pending = _flat_ordered[_resume_from:]
            if _pending:
                _log(f"[pool-mode] Lege {len(_pending)} Ordner an (von {total_folders} gesamt"
                     + (f", {_resume_from} bereits erledigt" if _resume_from else "") + ")")

                folders_by_depth = defaultdict(list)
                for reldir in _pending:
                    folders_by_depth[reldir.count("/")].append(reldir)

                threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
                _folders_created = _resume_from
                _folders_lock = threading.Lock()
                _last_progress_pct = 0
                _folders_start_time = time.time()

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
                                elapsed = time.time() - _folders_start_time
                                done_this_run = _folders_created - _resume_from
                                if done_this_run > 0 and elapsed > 0:
                                    rate = done_this_run / elapsed
                                    remaining_s = (total_folders - _folders_created) / rate if rate > 0 else 0
                                else:
                                    remaining_s = 0
                                eta_str = f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s/60)}min"
                                _log(f"[folders] {_folders_created}/{total_folders} ({current_pct}%) | {eta_str} verbleibend")
                            # Checkpoint alle 1000 Ordner
                            if _folders_created % 1000 == 0:
                                try:
                                    with open(_cp_path, "w", encoding="utf-8") as _cf:
                                        json.dump({
                                            "snapshot":      snapshot_name,
                                            "total_folders": total_folders,
                                            "flat_ordered":  _flat_ordered,
                                            "created_count": _folders_created,
                                            "status":        "in_progress",
                                        }, _cf, ensure_ascii=False)
                                    _log(f"[folders] Checkpoint: {_folders_created}/{total_folders}")
                                except Exception as _ce:
                                    _log(f"[folders][warn] Checkpoint-Update fehlgeschlagen: {_ce}")
                        return True
                    except Exception as e:
                        print(f"[warn] Ordner-Anlage fehlgeschlagen fuer {reldir}: {e}", file=sys.stderr)
                        return False

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

            # Checkpoint löschen (Phase 1 erfolgreich abgeschlossen)
            if os.path.exists(_cp_path):
                try:
                    os.remove(_cp_path)
                    _log(f"[folders] Checkpoint geloescht: {_cp_path}")
                except Exception as _e:
                    _log(f"[folders][warn] Checkpoint loeschen fehlgeschlagen: {_e}")
        
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
            nonlocal uploaded, resumed, stubs, index_changed, _done_items, _done_size, _t_last_progress
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
            
            # Hardlink-Fast-Path: inode bereits verarbeitet -> Pool-Objekt existiert sicher.
            # fileid/hash aus dem Index nehmen, Stub schreiben, return -> spart den stat-Call.
            # Bei unvollstaendigem Index-Eintrag faellt es auf den normalen Upload-Pfad zurueck
            # (korrekt, nur 1 stat teurer) - reine Optimierung, keine Verhaltensaenderung am Ergebnis.
            _hl_fileid = None
            _hl_hash = None
            with _state_lock:
                if ino_key in seen_inodes:
                    _entry = pool_refs.get(sha)
                    if isinstance(_entry, dict) and _entry.get("fileid"):
                        _hl_fileid = _entry.get("fileid")
                        _hl_hash = _entry.get("hash")
                        if snapshot_name not in _snap_names(_entry):
                            index_changed = True
                        _register_snap(pool_refs, sha, snapshot_name, relpath)
                        resumed += 1
            if _hl_fileid:
                try:
                    with _metrics_lock:
                        globals()["MET_POOL_REUSED"] += 1
                except Exception:
                    pass
                _queue_stub(relpath, it, _hl_fileid, _hl_hash, sha)
                return
            
            # Upload zu Pool
            try:
                pool_fileid, pcloud_hash = _upload_to_pool(src_abs, sha)
                
                # Update Pool-Refs (thread-safe) - v2 (snapshots=Map snap->[relpaths])
                with _state_lock:
                    _register_snap(pool_refs, sha, snapshot_name, relpath,
                                   fileid=pool_fileid, hash=pcloud_hash, size=it.get("size"))
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
            
            # Pool-Path und FileID holen - NEU: Erst Index, dann API
            pool_fileid = None
            pcloud_hash = None
            
            entry = pool_refs.get(sha)
            if isinstance(entry, dict):
                pool_fileid = entry.get("fileid")
                pcloud_hash = entry.get("hash")
            
            if not pool_fileid:
                # Fallback: API fragen (und Index reparieren)
                pool_path_rel = _get_pool_path(sha)
                pool_path_abs = f"{dest_root.rstrip('/')}/{pool_path_rel}"
                
                try:
                    pool_md = pc.stat_file_safe(cfg, path=pool_path_abs)
                    if pool_md:
                        pool_fileid = pool_md.get("fileid")
                        pcloud_hash = pool_md.get("hash")
                except Exception:
                    pass
            
            if not pool_fileid:
                _log(f"[ERROR] Reused-File {relpath}: Pool-Metadaten nicht findbar (SHA={sha[:16]}...)")
                return

            # Update Pool-Refs (Snapshots registrieren) - v2 (snapshots=Map snap->[relpaths])
            with _state_lock:
                _register_snap(pool_refs, sha, snapshot_name, relpath,
                               fileid=pool_fileid, hash=pcloud_hash, size=it.get("size"))
                index_changed = True
            
            # Stub queuen
            _queue_stub(relpath, it, pool_fileid, pcloud_hash, sha)
        
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
                
                # Remote archivieren: gefilterter Snapshot-Index (nicht voller Master-Klon)
                try:
                    archive_snapshot_index_remote(cfg, snapshots_root, index, snapshot_name, dry=False)
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
        
    finally:
        # === CLEANUP: GC-Lock IMMER entfernen (auch bei Fehler!) ===
        _cleanup_lock()


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
    # --- Neu: Encoding rekonfigurieren ---
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    # --- Ende Neu ---

    ap = argparse.ArgumentParser(description="Pusht ein JSON-Manifest nach pCloud (Object-Store, 1:1-Snapshot oder POOL-Modus).")
    ap.add_argument("--manifest", required=True, help="Pfad zur Manifest-JSON (schema=2 oder schema=4 für Pool)")
    ap.add_argument("--dest-root", required=True, help="Remote-Wurzel, z.B. /Backup/pcloud-snapshots")
    ap.add_argument("--snapshot-name", help="Überschreibe Snapshot-Name aus Manifest (optional, für Testing)")
    ap.add_argument("--snapshot-mode", choices=["pool"], default="pool",
                    help="Upload-Strategie: nur noch 'pool' (deduplizierter Pool + Stub-Snapshots).")
    ap.add_argument("--retention-sync", action="store_true",
                    help="Nach dem Upload: Pool-Retention (entfernte Snapshots loeschen, Platz via Pool-GC).")
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
    
    # === Snapshot-Name Override (optional für Testing) ===
    if args.snapshot_name:
        manifest["snapshot"] = args.snapshot_name
        _log(f"[cli] Snapshot-Name ueberschrieben: {args.snapshot_name}")

    dest_root = pc._norm_remote_path(args.dest_root)
    schema = int(manifest.get("schema", 0) or 0)
    _log(f"[cli] snapshot-mode={args.snapshot_mode} dry-run={bool(args.dry_run)} schema={schema}")

    # POOL-ONLY: 1to1/objects sind abgeschaltet (kein Zurueck). Nur noch Pool-Methode.
    if args.snapshot_mode != "pool":
        print(f"[cli][FATAL] snapshot-mode='{args.snapshot_mode}' wird nicht mehr unterstuetzt "
              f"- nur noch 'pool'.", file=sys.stderr)
        sys.exit(2)

    push_pool_mode(cfg, manifest, dest_root, dry=bool(args.dry_run))

    # Manifest archivieren (mode-unabhaengig, NUR nach erfolgreichem Upload - push_pool_mode
    # wirft sonst). 1:1 wie Legacy (pcloud_push_json_manifest_to_pcloud.py:2370). Ohne diesen
    # Schritt verliert die Pipeline ihr Gedaechtnis: der Wrapper loescht das Temp-Manifest
    # danach, und Scout/Smart-Mode-Referenz des naechsten Laufs braucht manifests/<snap>.json.
    if not args.dry_run:
        try:
            import shutil
            _snap = manifest.get("snapshot") or "SNAPSHOT"
            _man_archive_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "manifests")
            os.makedirs(_man_archive_dir, exist_ok=True)
            _man_archive_path = os.path.join(_man_archive_dir, f"{_snap}.json")
            shutil.copy2(args.manifest, _man_archive_path)
            _log(f"[archive] Manifest archiviert: {_man_archive_path}")
        except Exception as e:
            _log(f"[archive][warn] Manifest-Archivierung fehlgeschlagen: {e}")

    # Optional: Retention NACH Upload (Pool). Nicht kritisch, darf Upload nicht blockieren.
    if args.retention_sync:
        print("")
        print("="*80)
        print("RETENTION-POOL Phase gestartet")
        print("="*80)
        local_snaps = list_local_snapshot_names(manifest.get("root", "/"))
        print(f"Lokale Snapshots: {len(local_snaps)}")
        try:
            t_retention = time.time()
            retention_pool_mode(cfg, dest_root, local_snaps=local_snaps, dry=bool(args.dry_run))
            print("="*80)
            print(f"RETENTION-POOL abgeschlossen ({time.time()-t_retention:.1f}s)")
            print("="*80)
            print("")
        except Exception as _ret_exc:
            _log(f"[retention-pool] WARNING: retention_pool_mode fehlgeschlagen: {_ret_exc}")

    # --- metrics summary (einheitlich, greppbar) ---
    try:
        print(f"[metrics] uploaded_files={MET_UPLOADED_FILES} pool_reused={MET_POOL_REUSED} "
              f"resumed_files={MET_RESUMED_FILES} "
              f"stubs_written={MET_STUBS_WRITTEN} promoted={MET_PROMOTED} removed_nodes={MET_REMOVED_NODES} "
              f"fid_cache_hits={fid_cache_hits} fid_lookups={fid_lookups} fid_rest_ms={int(fid_rest_ms)} "
              f"api_retries={pc.get_api_retry_count()}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
