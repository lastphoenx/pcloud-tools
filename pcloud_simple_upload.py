#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pCloud Simple Upload - Universal Upload Tool

Schweizer-Sackmesser für Ad-hoc-Uploads:
- Einzelne Files oder ganze Ordner rekursiv
- Paralleles Threading (aus .env)
- Automatische Chunk-Strategie (<50MB direkt, ≥50MB chunked)
- Resume-Mechanismus bei Crashes
- SHA256-Verifikation
- Delta-Check nach Upload

Minimale Dependencies:
- Python 3.7+
- pcloud_bin_lib.py (--lib-path)
- .env (ZWINGEND für Token/API-Config)

Usage:
    # Einzelne Datei
    python pcloud_simple_upload.py \\
        --env-file /path/to/.env \\
        --source /local/file.bin \\
        --destination /Backup/test/

    # Ganzer Ordner rekursiv
    python pcloud_simple_upload.py \\
        --env-file /path/to/.env \\
        --source /local/folder/ \\
        --destination /Backup/test/
        
    # Mit custom lib
    python pcloud_simple_upload.py \\
        --env-file /path/to/.env \\
        --lib-path /opt/pcloud/pcloud_bin_lib.py \\
        --source /data/ \\
        --destination /Backup/data/
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ==================== Globals ====================

# Chunk-Konfiguration (konsistent mit pcloud_push_json_manifest_to_pcloud.py)
# Via .env steuerbar: PCLOUD_RESUME_THRESHOLD_GB, PCLOUD_RESUME_CHUNK_MB
CHUNK_SIZE_THRESHOLD = int(os.environ.get("PCLOUD_RESUME_THRESHOLD_GB", "5")) * 1024**3  # Default: 5 GB
CHUNK_SIZE = int(os.environ.get("PCLOUD_RESUME_CHUNK_MB", "128")) * 1024**2            # Default: 128 MB

# Globale Statistiken (thread-safe)
_stats_lock = threading.Lock()
_stats = {
    "files_total": 0,
    "files_uploaded": 0,
    "files_skipped": 0,
    "files_failed": 0,
    "bytes_total": 0,
    "bytes_uploaded": 0,
    "folders_created": 0,
    "start_time": 0,
    "errors": []
}

# ==================== Library Loading ====================

def download_pcloud_lib(target_dir: str) -> str:
    """
    Lädt pcloud_bin_lib.py automatisch von GitHub herunter.
    
    Returns:
        Pfad zur heruntergeladenen Library
    """
    import urllib.request
    
    github_url = "https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_bin_lib.py"
    target_path = os.path.join(target_dir, "pcloud_bin_lib.py")
    
    log(f"Lade pcloud_bin_lib.py herunter von GitHub...", "INFO")
    log(f"  URL: {github_url}", "INFO")
    log(f"  Ziel: {target_path}", "INFO")
    
    try:
        urllib.request.urlretrieve(github_url, target_path)
        log(f"✓ Download erfolgreich!", "OK")
        return target_path
    except Exception as e:
        log(f"✗ Download fehlgeschlagen: {e}", "ERROR")
        log(f"Bitte manuell herunterladen:", "ERROR")
        log(f"  wget {github_url}", "ERROR")
        log(f"  curl -O {github_url}", "ERROR")
        sys.exit(2)


def load_pcloud_lib(lib_path: Optional[str] = None, auto_download: bool = True):
    """
    Lädt pcloud_bin_lib.py mit flexibler Pfad-Auflösung + Auto-Download.
    
    Priorität:
    1. --lib-path Parameter
    2. Gleicher Ordner wie dieses Skript
    3. Parent-Ordner (für scripts/tools/)
    4. /opt/apps/pcloud-tools/main/
    5. AUTO-DOWNLOAD in Script-Dir (falls auto_download=True)
    """
    search_paths = []
    
    if lib_path:
        search_paths.append(lib_path)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    search_paths.extend([
        os.path.join(script_dir, "pcloud_bin_lib.py"),
        os.path.join(script_dir, "..", "pcloud_bin_lib.py"),
        "/opt/apps/pcloud-tools/main/pcloud_bin_lib.py",
    ])
    
    for path in search_paths:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.exists(path):
            # Add directory to sys.path
            lib_dir = os.path.dirname(path)
            if lib_dir not in sys.path:
                sys.path.insert(0, lib_dir)
            
            try:
                import pcloud_bin_lib as pc
                log(f"✓ pcloud_bin_lib geladen: {path}", "OK")
                return pc
            except ImportError as e:
                log(f"✗ Import fehlgeschlagen ({path}): {e}", "WARN")
                continue
    
    # Library nicht gefunden → Auto-Download
    if auto_download:
        log("", "WARN")
        log("⚠ pcloud_bin_lib.py nicht gefunden in:", "WARN")
        for p in search_paths:
            log(f"  - {p}", "WARN")
        log("", "INFO")
        
        # Download ins Script-Verzeichnis
        downloaded_path = download_pcloud_lib(script_dir)
        
        # Erneut versuchen zu laden
        lib_dir = os.path.dirname(downloaded_path)
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        
        try:
            import pcloud_bin_lib as pc
            log(f"✓ pcloud_bin_lib geladen: {downloaded_path}", "OK")
            return pc
        except ImportError as e:
            log(f"✗ Import nach Download fehlgeschlagen: {e}", "ERROR")
            sys.exit(2)
    
    # Kein Auto-Download oder gescheitert
    log("FEHLER: pcloud_bin_lib.py nicht gefunden!", "ERROR")
    log("Durchsuchte Pfade:", "ERROR")
    for p in search_paths:
        log(f"  - {p}", "ERROR")
    log("", "ERROR")
    log("Manueller Download:", "ERROR")
    log("  wget https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_bin_lib.py", "ERROR")
    sys.exit(2)

# ==================== Logging ====================

def log(msg: str, level: str = "INFO"):
    """Thread-safe Logging mit Timestamp"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level:5s}] {msg}", flush=True)

# ==================== State Management ====================

def get_state_dir() -> str:
    """
    State-Verzeichnis mit Fallback-Logik.
    
    Priorität:
    1. /srv/pcloud-archive/resume/ (production)
    2. ~/.pcloud_resume/ (user home)
    3. /tmp/pcloud_resume/ (fallback)
    """
    for state_dir in [
        "/srv/pcloud-archive/resume",
        os.path.expanduser("~/.pcloud_resume"),
        "/tmp/pcloud_resume"
    ]:
        try:
            os.makedirs(state_dir, exist_ok=True)
            # Test write
            test_file = os.path.join(state_dir, ".write_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return state_dir
        except Exception:
            continue
    
    raise RuntimeError("Kein schreibbares State-Verzeichnis gefunden!")


def get_state_file(local_path: str) -> str:
    """State-File-Pfad für eine lokale Datei"""
    state_dir = get_state_dir()
    # Use file path + size + mtime as unique identifier
    stat = os.stat(local_path)
    unique_id = hashlib.md5(
        f"{local_path}:{stat.st_size}:{stat.st_mtime}".encode()
    ).hexdigest()[:16]
    filename = os.path.basename(local_path)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    return os.path.join(state_dir, f"{safe_name}_{unique_id}.state.json")


def load_state(state_file: str) -> Optional[Dict[str, Any]]:
    """State laden"""
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"State laden fehlgeschlagen: {e}", "WARN")
        return None


def save_state(state_file: str, state: Dict[str, Any]):
    """State speichern (atomic write)"""
    temp_file = state_file + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, state_file)


def cleanup_state(state_file: str):
    """State nach erfolgreichem Upload löschen"""
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass

# ==================== Hash Computing ====================

def compute_file_hash(filepath: str, show_progress: bool = False) -> str:
    """SHA256-Hash einer Datei berechnen"""
    h = hashlib.sha256()
    file_size = os.path.getsize(filepath)
    bytes_read = 0
    last_progress = 0
    
    if show_progress and file_size > 100 * 1024**2:  # Nur bei >100MB Progress
        log(f"Berechne SHA256-Hash ({file_size/1024**2:.1f} MB)...", "INFO")
    
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
            bytes_read += len(chunk)
            
            if show_progress and file_size > 100 * 1024**2:
                progress = int((bytes_read / file_size) * 100)
                if progress >= last_progress + 20:
                    log(f"  Hash-Progress: {progress}%", "INFO")
                    last_progress = progress
    
    return h.hexdigest()

# ==================== File Scanner ====================

def scan_source(source_path: str) -> Tuple[List[Dict[str, Any]], int]:
    """
    Scannt Source (File oder Ordner rekursiv).
    
    Returns:
        (files, total_bytes)
        files = [{"local_path": "/foo/bar.txt", "rel_path": "bar.txt", "size": 123}, ...]
    """
    source_path = os.path.abspath(source_path)
    
    if not os.path.exists(source_path):
        log(f"FEHLER: Source nicht gefunden: {source_path}", "ERROR")
        sys.exit(1)
    
    files = []
    total_bytes = 0
    
    if os.path.isfile(source_path):
        # Einzelne Datei
        size = os.path.getsize(source_path)
        files.append({
            "local_path": source_path,
            "rel_path": os.path.basename(source_path),
            "size": size
        })
        total_bytes = size
        log(f"Source: Einzelne Datei ({size/1024**2:.2f} MB)", "INFO")
    
    elif os.path.isdir(source_path):
        # Ordner rekursiv
        log(f"Scanne Ordner rekursiv: {source_path}", "INFO")
        
        for root, dirs, filenames in os.walk(source_path):
            # Filter versteckte Ordner (optional)
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for filename in filenames:
                if filename.startswith('.'):
                    continue  # Skip hidden files
                
                local_path = os.path.join(root, filename)
                rel_path = os.path.relpath(local_path, source_path)
                
                try:
                    size = os.path.getsize(local_path)
                    files.append({
                        "local_path": local_path,
                        "rel_path": rel_path,
                        "size": size
                    })
                    total_bytes += size
                except Exception as e:
                    log(f"✗ Fehler beim Scannen: {rel_path}: {e}", "WARN")
        
        log(f"✓ {len(files)} Dateien gefunden ({total_bytes/1024**2:.2f} MB total)", "OK")
    
    else:
        log(f"FEHLER: Source ist weder File noch Ordner: {source_path}", "ERROR")
        sys.exit(1)
    
    return files, total_bytes

# ==================== Upload Workers ====================

def upload_file_direct(pc, cfg: Dict[str, Any], local_path: str, remote_path: str) -> Dict[str, Any]:
    """
    Direkter Upload für kleine Dateien (<50MB).
    
    Returns:
        {"success": True/False, "fileid": ..., "hash": ..., "error": ...}
    """
    try:
        # Upload: prefer modern helper available in current pcloud_bin_lib
        if hasattr(pc, "upload_file"):
            result = pc.upload_file(cfg, local_path=local_path, remote_path=remote_path)
        else:
            # Legacy fallback for older lib variants
            result = pc.putfile(cfg, path=remote_path, data=local_path)
        
        metadata = result.get("metadata", [])
        if isinstance(metadata, list):
            metadata = metadata[0] if metadata else {}
        
        fileid = metadata.get("fileid")
        pcloud_hash = metadata.get("hash")
        
        # SHA256-Check
        local_hash = compute_file_hash(local_path)
        try:
            checksum_data = pc.checksumfile(cfg, fileid=int(fileid))
            remote_hash = checksum_data.get("sha256", "").lower()
            
            if local_hash.lower() != remote_hash:
                return {
                    "success": False,
                    "error": f"SHA256 Mismatch! Local: {local_hash[:16]}... Remote: {remote_hash[:16]}..."
                }
        except Exception as e:
            log(f"  ⚠ SHA256-Check fehlgeschlagen (Upload aber OK): {e}", "WARN")
        
        return {
            "success": True,
            "fileid": fileid,
            "hash": pcloud_hash,
            "sha256": local_hash
        }
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def upload_file_chunked(pc, cfg: Dict[str, Any], local_path: str, remote_path: str) -> Dict[str, Any]:
    """
    Chunked Upload für große Dateien (≥50MB) mit Resume-Unterstützung.
    
    Returns:
        {"success": True/False, "fileid": ..., "hash": ..., "error": ...}
    """
    state_file = get_state_file(local_path)
    file_size = os.path.getsize(local_path)
    dest_dir = os.path.dirname(remote_path.rstrip("/")) or "/"
    filename = os.path.basename(remote_path)
    
    # Resume-State laden
    state = load_state(state_file)
    uploadid = None
    upload_offset = 0
    
    if state:
        uploadid = state.get("uploadid")
        upload_offset = state.get("offset", 0)
        
        # Server-Status abfragen
        try:
            server_info = pc.upload_info(cfg, uploadid)
            server_offset = server_info.get("size", 0)
            
            if server_offset != upload_offset:
                log(f"  ⚠ Offset-Korrektur: Local {upload_offset} → Server {server_offset}", "WARN")
                upload_offset = server_offset
        except Exception:
            # uploadid abgelaufen
            log(f"  ⚠ uploadid abgelaufen, starte neu", "WARN")
            uploadid = None
            upload_offset = 0
    
    # Upload-Session erstellen (falls nötig)
    if not uploadid:
        try:
            j = pc.upload_create(cfg)
            uploadid = j.get("uploadid")
        except Exception as e:
            return {"success": False, "error": f"upload_create failed: {e}"}
    
    # Chunks hochladen
    try:
        with open(local_path, "rb") as fh:
            fh.seek(upload_offset)
            
            while upload_offset < file_size:
                chunk_data = fh.read(CHUNK_SIZE)
                if not chunk_data:
                    break
                
                # Retry-Logik (wie in pcloud_push_json_manifest_to_pcloud.py)
                max_retries = 12
                for attempt in range(1, max_retries + 1):
                    try:
                        pc.upload_write(cfg, uploadid, upload_offset, chunk_data)
                        break
                    except Exception as e:
                        if attempt >= max_retries:
                            raise
                        log(f"  ⚠ Chunk-Upload Retry {attempt}/{max_retries}: {e}", "WARN")
                        time.sleep(min(2 ** attempt, 30))  # Exponential backoff
                
                upload_offset += len(chunk_data)
                
                # State speichern
                save_state(state_file, {
                    "uploadid": uploadid,
                    "offset": upload_offset,
                    "file_size": file_size,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "updated_at": time.time()
                })
                
                # Progress alle 10%
                progress = int((upload_offset / file_size) * 100)
                if progress % 10 == 0:
                    log(f"  Progress: {progress}% ({upload_offset/1024**2:.1f}/{file_size/1024**2:.1f} MB)", "INFO")
    
    except Exception as e:
        return {"success": False, "error": f"Chunk upload failed: {e}"}
    
    # Finalisieren
    try:
        dest_folderid = pc.ensure_path(cfg, dest_dir)
        j = pc.upload_save(cfg, uploadid, folderid=dest_folderid, name=filename)
        
        metadata = (j.get("metadata") or [{}])[0] if isinstance(j.get("metadata"), list) else j.get("metadata", {})
        fileid = metadata.get("fileid")
        pcloud_hash = metadata.get("hash")
        
        # SHA256-Check
        local_hash = compute_file_hash(local_path, show_progress=True)
        try:
            checksum_data = pc.checksumfile(cfg, fileid=int(fileid))
            remote_hash = checksum_data.get("sha256", "").lower()
            
            if local_hash.lower() != remote_hash:
                return {
                    "success": False,
                    "error": f"SHA256 Mismatch! Local: {local_hash[:16]}... Remote: {remote_hash[:16]}..."
                }
        except Exception as e:
            log(f"  ⚠ SHA256-Check fehlgeschlagen (Upload aber OK): {e}", "WARN")
        
        # State aufräumen
        cleanup_state(state_file)
        
        return {
            "success": True,
            "fileid": fileid,
            "hash": pcloud_hash,
            "sha256": local_hash
        }
    
    except Exception as e:
        return {"success": False, "error": f"upload_save failed: {e}"}


def upload_worker(pc, cfg: Dict[str, Any], file_info: Dict[str, Any], destination_base: str) -> Dict[str, Any]:
    """
    Worker-Funktion für parallele Uploads.
    
    Args:
        file_info: {"local_path": ..., "rel_path": ..., "size": ...}
        destination_base: pCloud-Zielordner (z.B. "/Backup/test/")
    
    Returns:
        {"local_path": ..., "success": ..., "error": ..., "duration": ...}
    """
    local_path = file_info["local_path"]
    rel_path = file_info["rel_path"]
    size = file_info["size"]
    
    # Remote-Pfad konstruieren
    remote_path = os.path.join(destination_base, rel_path).replace("\\", "/")
    
    start_time = time.time()
    
    log(f"⬆ Upload: {rel_path} ({size/1024**2:.2f} MB)", "INFO")
    
    # Strategie wählen
    if size < CHUNK_SIZE_THRESHOLD:
        result = upload_file_direct(pc, cfg, local_path, remote_path)
    else:
        log(f"  → Chunked Upload (Datei ≥{CHUNK_SIZE_THRESHOLD/1024**3:.1f} GB, Chunk-Size: {CHUNK_SIZE/1024**2:.0f} MB)", "INFO")
        result = upload_file_chunked(pc, cfg, local_path, remote_path)
    
    duration = time.time() - start_time
    
    # Statistiken aktualisieren
    with _stats_lock:
        if result["success"]:
            _stats["files_uploaded"] += 1
            _stats["bytes_uploaded"] += size
            log(f"✓ {rel_path} ({duration:.1f}s)", "OK")
        else:
            _stats["files_failed"] += 1
            _stats["errors"].append({
                "file": rel_path,
                "error": result.get("error", "Unknown error")
            })
            log(f"✗ {rel_path}: {result.get('error')}", "ERROR")
    
    return {
        "local_path": local_path,
        "rel_path": rel_path,
        "remote_path": remote_path,
        "success": result["success"],
        "error": result.get("error"),
        "fileid": result.get("fileid"),
        "hash": result.get("hash"),
        "sha256": result.get("sha256"),
        "duration": duration
    }

# ==================== Folder Creation ====================

def create_folders_parallel(pc, cfg: Dict[str, Any], files: List[Dict[str, Any]], 
                            destination_base: str, folder_threads: int):
    """
    Erstellt alle benötigten Zielordner parallel.
    
    Extrahiert alle Ordner-Pfade aus den Files und erstellt sie via ensure_path().
    """
    # Alle Ordner-Pfade sammeln
    folders = set()
    for file_info in files:
        rel_path = file_info["rel_path"]
        rel_dir = os.path.dirname(rel_path)
        
        if rel_dir:
            # Alle Parent-Ordner auch erstellen
            parts = Path(rel_dir).parts
            for i in range(1, len(parts) + 1):
                folder_rel = str(Path(*parts[:i]))
                folder_remote = os.path.join(destination_base, folder_rel).replace("\\", "/")
                folders.add(folder_remote)
    
    if not folders:
        log("✓ Keine Ordner-Struktur nötig (nur Root-Dateien)", "OK")
        return
    
    log(f"Erstelle {len(folders)} Ordner parallel ({folder_threads} Threads)...", "INFO")
    
    def create_folder(folder_path):
        try:
            pc.ensure_path(cfg, folder_path)
            with _stats_lock:
                _stats["folders_created"] += 1
            return {"path": folder_path, "success": True}
        except Exception as e:
            log(f"✗ Ordner-Erstellung fehlgeschlagen: {folder_path}: {e}", "ERROR")
            return {"path": folder_path, "success": False, "error": str(e)}
    
    # Ordner nach Tiefe sortieren (flache zuerst)
    folders_sorted = sorted(folders, key=lambda p: p.count("/"))
    
    with ThreadPoolExecutor(max_workers=folder_threads) as executor:
        list(executor.map(create_folder, folders_sorted))
    
    log(f"✓ {_stats['folders_created']}/{len(folders)} Ordner erstellt", "OK")

# ==================== Delta Check ====================

def delta_check(pc, cfg: Dict[str, Any], files: List[Dict[str, Any]], 
               destination_base: str) -> Dict[str, Any]:
    """
    Delta-Check: Vergleicht lokale Files mit pCloud-Ziel.
    
    Returns:
        {
            "status": "OK" | "INCOMPLETE" | "ERROR",
            "missing": [...],  # Auf pCloud fehlen
            "extra": [...],    # Auf pCloud aber nicht lokal
            "size_mismatch": [...],
            "ok": [...]
        }
    """
    log("", "INFO")
    log("=== Delta-Check: Verifikation ===", "INFO")
    
    # Lokale Files als Dict (rel_path → size)
    local_files = {f["rel_path"]: f["size"] for f in files}
    
    # Nur erwartete Ziele prüfen (kein Vollscan des gesamten Zielordners)
    log(f"Prüfe erwartete Zieldateien unter: {destination_base}", "INFO")
    remote_files = {}

    try:
        base = destination_base.rstrip("/")
        if not base:
            base = "/"

        for rel_path in local_files.keys():
            remote_path = f"{base}/{rel_path.lstrip('/')}" if base != "/" else f"/{rel_path.lstrip('/')}"
            try:
                md = pc.stat_file(cfg, path=remote_path, with_checksum=False)
                remote_files[rel_path] = int(md.get("size", 0))
            except Exception:
                # fehlend => wird unten als missing markiert
                continue

        log(f"✓ {len(remote_files)} erwartete Dateien auf pCloud gefunden", "OK")

    except Exception as e:
        log(f"✗ Delta-Check fehlgeschlagen: {e}", "ERROR")
        return {"status": "ERROR", "error": str(e)}
    
    # Vergleich
    missing = []
    extra = []
    size_mismatch = []
    ok = []
    
    for rel_path, local_size in local_files.items():
        if rel_path not in remote_files:
            missing.append(rel_path)
        elif remote_files[rel_path] != local_size:
            size_mismatch.append({
                "file": rel_path,
                "local_size": local_size,
                "remote_size": remote_files[rel_path]
            })
        else:
            ok.append(rel_path)
    
    # Kein vollständiger Remote-Scan mehr: extra bleibt bewusst leer.
    
    # Report
    log("", "INFO")
    log("Delta-Check Ergebnisse:", "INFO")
    log(f"  ✓ OK: {len(ok)}/{len(local_files)} Dateien", "OK" if len(ok) == len(local_files) else "INFO")
    
    if missing:
        log(f"  ✗ MISSING auf pCloud: {len(missing)} Dateien", "ERROR")
        for f in missing[:10]:
            log(f"    - {f}", "ERROR")
        if len(missing) > 10:
            log(f"    ... und {len(missing)-10} weitere", "ERROR")
    
    if size_mismatch:
        log(f"  ✗ SIZE MISMATCH: {len(size_mismatch)} Dateien", "ERROR")
        for m in size_mismatch[:5]:
            log(f"    - {m['file']}: Local {m['local_size']} != Remote {m['remote_size']}", "ERROR")
    
    if extra:
        log(f"  ⚠ EXTRA auf pCloud: {len(extra)} Dateien (nicht in Source)", "WARN")
    
    status = "OK"
    if missing or size_mismatch:
        status = "INCOMPLETE"
    
    return {
        "status": status,
        "missing": missing,
        "extra": extra,
        "size_mismatch": size_mismatch,
        "ok": ok
    }

# ==================== Main ====================

def main():
    parser = argparse.ArgumentParser(
        description="pCloud Simple Upload - Universal Upload Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  # Einzelne Datei
  python pcloud_simple_upload.py --env-file /opt/apps/pcloud-tools/main/.env \\
      --source /data/backup.tar.gz --destination /Backup/test/

  # Ganzer Ordner rekursiv
  python pcloud_simple_upload.py --env-file /opt/apps/pcloud-tools/main/.env \\
      --source /data/photos/ --destination /Backup/photos/

  # Mit custom lib
  python pcloud_simple_upload.py --env-file .env \\
      --lib-path /opt/pcloud/pcloud_bin_lib.py \\
      --source /data/ --destination /Backup/data/
        """
    )
    
    parser.add_argument("--env-file", required=True, 
                       help="Pfad zur .env-Datei (ZWINGEND für Token/API-Config)")
    parser.add_argument("--source", required=True,
                       help="Lokaler Quell-Pfad (File oder Ordner)")
    parser.add_argument("--destination", required=True,
                       help="pCloud-Zielordner (z.B. /Backup/test/)")
    parser.add_argument("--lib-path", 
                       help="Pfad zu pcloud_bin_lib.py (optional, default: auto-detect)")
    parser.add_argument("--no-auto-download", action="store_true",
                       help="Auto-Download von pcloud_bin_lib.py deaktivieren")
    parser.add_argument("--no-delta-check", action="store_true",
                       help="Delta-Check nach Upload überspringen")
    
    args = parser.parse_args()
    
    # Library laden (mit Auto-Download falls nicht vorhanden)
    pc = load_pcloud_lib(args.lib_path, auto_download=not args.no_auto_download)
    
    # Config laden
    if not os.path.exists(args.env_file):
        log(f"FEHLER: .env nicht gefunden: {args.env_file}", "ERROR")
        sys.exit(1)
    
    cfg = pc.effective_config(env_file=args.env_file)
    
    if not cfg.get("token"):
        log("FEHLER: PCLOUD_TOKEN nicht gesetzt in .env", "ERROR")
        sys.exit(1)
    
    # Threading-Config aus .env
    upload_threads = int(cfg.get("PCLOUD_UPLOAD_THREADS", 4))
    folder_threads = int(cfg.get("PCLOUD_FOLDER_THREADS", 4))
    
    log("", "INFO")
    log("=== pCloud Simple Upload ===", "INFO")
    log(f"Source: {args.source}", "INFO")
    log(f"Destination: {args.destination}", "INFO")
    log(f"Upload Threads: {upload_threads}", "INFO")
    log(f"Folder Threads: {folder_threads}", "INFO")
    log(f"API Host: {cfg.get('host')}:{cfg.get('port')}", "INFO")
    log("", "INFO")
    
    # Source scannen
    _stats["start_time"] = time.time()
    files, total_bytes = scan_source(args.source)
    _stats["files_total"] = len(files)
    _stats["bytes_total"] = total_bytes
    
    if not files:
        log("Keine Dateien zum Uploaden gefunden!", "WARN")
        sys.exit(0)
    
    # Ordner-Struktur erstellen
    create_folders_parallel(pc, cfg, files, args.destination, folder_threads)
    
    # Files hochladen (parallel)
    log("", "INFO")
    log(f"Starte Upload: {len(files)} Dateien mit {upload_threads} Threads...", "INFO")
    
    results = []
    with ThreadPoolExecutor(max_workers=upload_threads) as executor:
        futures = {
            executor.submit(upload_worker, pc, cfg, f, args.destination): f 
            for f in files
        }
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
    
    # Statistiken
    duration = time.time() - _stats["start_time"]
    
    log("", "INFO")
    log("=== Upload abgeschlossen ===", "INFO")
    log(f"Dateien: {_stats['files_uploaded']}/{_stats['files_total']} erfolgreich", 
        "OK" if _stats['files_failed'] == 0 else "WARN")
    log(f"Fehler: {_stats['files_failed']}", "ERROR" if _stats['files_failed'] > 0 else "INFO")
    log(f"Bytes: {_stats['bytes_uploaded']/1024**2:.2f} MB / {_stats['bytes_total']/1024**2:.2f} MB", "INFO")
    log(f"Ordner: {_stats['folders_created']} erstellt", "INFO")
    log(f"Dauer: {duration:.1f}s ({_stats['bytes_uploaded']/duration/1024**2:.2f} MB/s)", "INFO")
    
    if _stats["errors"]:
        log("", "ERROR")
        log("Fehler-Details:", "ERROR")
        for err in _stats["errors"]:
            log(f"  ✗ {err['file']}: {err['error']}", "ERROR")
    
    # Delta-Check
    if not args.no_delta_check and _stats["files_failed"] == 0:
        delta_result = delta_check(pc, cfg, files, args.destination)
        
        if delta_result["status"] == "INCOMPLETE":
            log("", "ERROR")
            log("⚠ WARNUNG: Upload unvollständig! Fehlende oder inkonsistente Dateien erkannt.", "ERROR")
            sys.exit(1)
        elif delta_result["status"] == "ERROR":
            log("", "WARN")
            log("⚠ Delta-Check fehlgeschlagen (Upload aber abgeschlossen)", "WARN")
        else:
            log("", "OK")
            log("✓ Delta-Check OK - Alle Dateien vollständig auf pCloud!", "OK")
    
    log("", "INFO")
    
    # Exit-Code
    sys.exit(0 if _stats["files_failed"] == 0 else 1)


if __name__ == "__main__":
    main()
