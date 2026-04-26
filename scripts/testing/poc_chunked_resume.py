#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proof of Concept: Persistent Chunked Upload mit Resume

Test-Ziele:
1. Chunked Upload (upload_create → upload_write → upload_save)
2. State nach jedem Chunk persistent speichern
3. Resume nach Abbruch (uploadid-Gültigkeit testen)
4. Detailliertes Logging aller API-Responses

Test-Modi:
  --mode normal       : Normaler Upload ohne Abbruch
  --mode abort-after  : Abbruch nach N Chunks (--abort-after-chunks N)
  --mode resume       : Resume eines abgebrochenen Uploads
  --mode timeout-test : Upload + lange Pause + Resume (uploadid-Timeout testen)

Verwendung:
  # 1. Normaler Upload (mit absichtlichem Abbruch nach 3 Chunks)
  python poc_chunked_resume.py --file /path/to/bigfile.bin --mode abort-after --abort-after-chunks 3

  # 2. Resume nach Abbruch
  python poc_chunked_resume.py --file /path/to/bigfile.bin --mode resume

  # 3. Timeout-Test (Upload starten, warten, dann resume)
  python poc_chunked_resume.py --file /path/to/bigfile.bin --mode timeout-test --timeout-minutes 10
"""

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import datetime
from typing import Optional, Dict, Any

# pcloud_bin_lib laden
try:
    import pcloud_bin_lib as pc
except ImportError:
    print("FEHLER: pcloud_bin_lib.py nicht gefunden. Skript muss im selben Verzeichnis liegen.", file=sys.stderr)
    sys.exit(2)


# ==================== Konfiguration ====================

# State-Verzeichnis (für persistent state + response logs)
STATE_DIR = os.path.expanduser("~/.pcloud_poc_state")
REMOTE_TEST_DIR = "/Backup/rtb_1to1/testing_purpose"

# Chunk-Größe (klein für schnelle Tests, z.B. 2 MB)
CHUNK_SIZE = int(os.environ.get("POC_CHUNK_SIZE", str(2 * 1024**2)))  # 2 MB default

# ==================== Hilfsfunktionen ====================

def log(msg: str, level: str = "INFO"):
    """Logging mit Timestamp"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} [{level}] {msg}", flush=True)


def compute_file_hash(filepath: str) -> str:
    """SHA256-Hash einer Datei berechnen"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def ensure_state_dir():
    """State-Verzeichnis erstellen"""
    os.makedirs(STATE_DIR, exist_ok=True)


def get_state_file(local_path: str) -> str:
    """State-Datei-Pfad für eine lokale Datei"""
    filename = os.path.basename(local_path)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    return os.path.join(STATE_DIR, f"{safe_name}.state.json")


def get_response_log_file(local_path: str) -> str:
    """Response-Log-Datei-Pfad"""
    filename = os.path.basename(local_path)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)
    return os.path.join(STATE_DIR, f"{safe_name}.responses.jsonl")


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
    import tempfile
    temp_file = state_file + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, state_file)


def log_response(log_file: str, response: Dict[str, Any]):
    """API-Response in JSONL-Log schreiben"""
    entry = {
        "timestamp": time.time(),
        "datetime": datetime.now().isoformat(),
        "response": response
    }
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


def cleanup_state(state_file: str, response_log: str):
    """State und Logs nach erfolgreichem Upload löschen"""
    for f in [state_file, response_log]:
        if os.path.exists(f):
            os.remove(f)
            log(f"Gelöscht: {f}")


# ==================== Upload-Logik ====================

def test_uploadid_valid(cfg: Dict[str, Any], uploadid: int) -> bool:
    """
    Testet ob uploadid noch gültig ist.
    Sendet einen leeren upload_write-Request als Probe.
    """
    log(f"Teste uploadid {uploadid} auf Gültigkeit...", "TEST")
    
    session = pc._get_session()
    base_url = pc._rest_base(cfg)
    
    try:
        # Sende 0-Byte-Test an offset 0
        r = session.post(f"{base_url}/upload_write", params={
            "access_token": cfg["token"],
            "uploadid": uploadid,
            "uploadoffset": 0
        }, data=b"", timeout=(10, 10))
        
        j = r.json()
        
        # Error 1900 = "Upload not found"
        if j.get("result") == 1900:
            log(f"uploadid {uploadid} ist NICHT mehr gültig (Error 1900)", "WARN")
            return False
        
        # Andere Fehler ignorieren wir (wichtig ist nur ob uploadid bekannt ist)
        log(f"uploadid {uploadid} ist GÜLTIG (result={j.get('result')})", "OK")
        return True
        
    except Exception as e:
        log(f"uploadid-Test fehlgeschlagen: {e}", "WARN")
        return False


def chunked_upload_with_resume(cfg: Dict[str, Any], local_path: str, remote_path: str, 
                                mode: str = "normal", abort_after_chunks: int = 0,
                                timeout_minutes: int = 0) -> Dict[str, Any]:
    """
    Chunked Upload mit persistentem State und Resume-Unterstützung.
    
    Args:
        cfg: pCloud-Konfiguration
        local_path: Lokale Datei
        remote_path: Ziel in pCloud
        mode: 'normal', 'abort-after', 'resume', 'timeout-test'
        abort_after_chunks: Bei 'abort-after': Nach N Chunks abbrechen
        timeout_minutes: Bei 'timeout-test': Minuten warten vor Resume
    """
    ensure_state_dir()
    
    state_file = get_state_file(local_path)
    response_log = get_response_log_file(local_path)
    
    file_size = os.path.getsize(local_path)
    file_hash = compute_file_hash(local_path)
    filename = os.path.basename(remote_path)
    
    log(f"=== Chunked Upload PoC ===")
    log(f"Datei: {local_path}")
    log(f"Größe: {file_size:,} Bytes ({file_size/1024**2:.2f} MB)")
    log(f"SHA256: {file_hash}")
    log(f"Chunk-Größe: {CHUNK_SIZE:,} Bytes ({CHUNK_SIZE/1024**2:.2f} MB)")
    log(f"Ziel: {remote_path}")
    log(f"Modus: {mode}")
    log(f"State-File: {state_file}")
    log(f"Response-Log: {response_log}")
    log("")
    
    # === Resume-Logik ===
    uploadid = None
    upload_offset = 0
    chunks_uploaded = 0
    
    if mode == "resume":
        state = load_state(state_file)
        
        if not state:
            log("FEHLER: Kein State-File gefunden für Resume!", "ERROR")
            sys.exit(1)
        
        # Validierung
        if state.get("file_hash") != file_hash:
            log("FEHLER: Datei wurde seit Abbruch geändert (Hash-Mismatch)!", "ERROR")
            log(f"  Erwartet: {state.get('file_hash')}")
            log(f"  Aktuell:  {file_hash}")
            sys.exit(1)
        
        uploadid = state.get("uploadid")
        upload_offset = state.get("offset", 0)
        chunks_uploaded = state.get("chunks_uploaded", 0)
        
        log(f"[RESUME] State geladen:", "INFO")
        log(f"  uploadid: {uploadid}")
        log(f"  offset: {upload_offset:,} Bytes ({upload_offset/file_size*100:.1f}%)")
        log(f"  chunks_uploaded: {chunks_uploaded}")
        log("")
        
        # uploadid-Gültigkeit testen
        if not test_uploadid_valid(cfg, uploadid):
            log("[RESUME] uploadid abgelaufen → FALLBACK: Neuer Upload", "WARN")
            uploadid = None
            upload_offset = 0
            chunks_uploaded = 0
        else:
            log("[RESUME] uploadid gültig → Upload wird fortgesetzt", "OK")
        log("")
    
    # === Upload-Session erstellen (falls nötig) ===
    session = pc._get_session()
    base_url = pc._rest_base(cfg)
    
    if not uploadid:
        log("[INIT] Erstelle neue Upload-Session (upload_create)...", "INFO")
        
        r = session.post(f"{base_url}/upload_create", params={
            "access_token": cfg["token"]
        }, timeout=(60, 30))
        
        j = r.json()
        log_response(response_log, {"step": "upload_create", "response": j})
        
        if j.get("result") != 0:
            log(f"FEHLER: upload_create fehlgeschlagen: {j}", "ERROR")
            sys.exit(1)
        
        uploadid = j.get("uploadid")
        log(f"[INIT] uploadid erhalten: {uploadid}", "OK")
        log("")
    
    # === Chunks hochladen ===
    log("[UPLOAD] Starte Chunk-Upload...", "INFO")
    
    with open(local_path, "rb") as fh:
        fh.seek(upload_offset)  # Spring zu Resume-Position
        
        chunk_number = chunks_uploaded
        
        while upload_offset < file_size:
            chunk_data = fh.read(CHUNK_SIZE)
            if not chunk_data:
                break
            
            chunk_number += 1
            chunk_start_time = time.time()
            
            # === Timeout-Test: Pause nach erstem Chunk ===
            if mode == "timeout-test" and chunk_number == 1 and timeout_minutes > 0:
                log(f"[TIMEOUT-TEST] Warte {timeout_minutes} Minuten...", "TEST")
                log(f"[TIMEOUT-TEST] Drücke Ctrl+C um manuell zu testen, oder warte...", "TEST")
                time.sleep(timeout_minutes * 60)
                log(f"[TIMEOUT-TEST] Warte-Zeit vorbei, setze Upload fort...", "TEST")
            
            # === Chunk hochladen ===
            log(f"[CHUNK {chunk_number}] Lade {len(chunk_data):,} Bytes @ Offset {upload_offset:,}...")
            
            r = session.post(f"{base_url}/upload_write", params={
                "access_token": cfg["token"],
                "uploadid": uploadid,
                "uploadoffset": upload_offset
            }, data=chunk_data, headers={
                "Content-Type": "application/octet-stream",
                "Connection": "keep-alive"
            }, timeout=(60, 120))
            
            j = r.json()
            chunk_duration = time.time() - chunk_start_time
            
            # Response loggen
            log_response(response_log, {
                "step": "upload_write",
                "chunk_number": chunk_number,
                "offset": upload_offset,
                "size": len(chunk_data),
                "duration_s": round(chunk_duration, 2),
                "response": j
            })
            
            if j.get("result") != 0:
                log(f"FEHLER: upload_write fehlgeschlagen: {j}", "ERROR")
                # State trotzdem speichern (für Debugging)
                save_state(state_file, {
                    "uploadid": uploadid,
                    "offset": upload_offset,
                    "chunks_uploaded": chunk_number - 1,
                    "file_hash": file_hash,
                    "file_size": file_size,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "status": "error",
                    "error": j,
                    "updated_at": time.time()
                })
                sys.exit(1)
            
            # Erfolg!
            upload_offset += len(chunk_data)
            progress_pct = upload_offset / file_size * 100
            speed_mbps = (len(chunk_data) / 1024**2) / chunk_duration
            
            log(f"[CHUNK {chunk_number}] ✓ OK (result={j.get('result')}) | "
                f"{upload_offset:,}/{file_size:,} Bytes ({progress_pct:.1f}%) | "
                f"{speed_mbps:.2f} MB/s")
            
            # === State speichern ===
            save_state(state_file, {
                "uploadid": uploadid,
                "offset": upload_offset,
                "chunks_uploaded": chunk_number,
                "file_hash": file_hash,
                "file_size": file_size,
                "local_path": local_path,
                "remote_path": remote_path,
                "status": "in_progress",
                "updated_at": time.time()
            })
            
            # === Abort-Test: Nach N Chunks abbrechen ===
            if mode == "abort-after" and chunk_number >= abort_after_chunks:
                log(f"[ABORT-TEST] Breche nach {chunk_number} Chunks ab (wie gewünscht)", "TEST")
                log(f"[ABORT-TEST] State gespeichert in: {state_file}", "TEST")
                log(f"[ABORT-TEST] Zum Resume: python {sys.argv[0]} --file {local_path} --mode resume", "TEST")
                sys.exit(0)
    
    # === Upload finalisieren ===
    log("")
    log("[FINALIZE] Finalisiere Upload (upload_save)...", "INFO")
    
    # Zielordner + Dateiname
    dest_dir = os.path.dirname(remote_path.rstrip("/")) or "/"
    dest_folderid = pc.ensure_path(cfg, dest_dir)
    
    r = session.post(f"{base_url}/upload_save", params={
        "access_token": cfg["token"],
        "uploadid": uploadid,
        "folderid": dest_folderid,
        "name": filename
    }, timeout=(60, 60))
    
    j = r.json()
    log_response(response_log, {"step": "upload_save", "response": j})
    
    if j.get("result") != 0:
        log(f"FEHLER: upload_save fehlgeschlagen: {j}", "ERROR")
        sys.exit(1)
    
    metadata = (j.get("metadata") or [{}])[0] if isinstance(j.get("metadata"), list) else j.get("metadata", {})
    
    log(f"[FINALIZE] ✓ Upload erfolgreich abgeschlossen!", "OK")
    log(f"  FileID: {metadata.get('fileid')}")
    log(f"  Hash: {metadata.get('hash')}")
    log(f"  Größe: {metadata.get('size'):,} Bytes")
    log("")
    
    # State aufräumen
    cleanup_state(state_file, response_log)
    
    log("=== PoC ABGESCHLOSSEN ===", "OK")
    
    return j


# ==================== Main ====================

def main():
    parser = argparse.ArgumentParser(description="PoC: Persistent Chunked Upload mit Resume")
    parser.add_argument("--file", required=True, help="Lokale Datei zum Hochladen")
    parser.add_argument("--remote-dir", default=REMOTE_TEST_DIR, help=f"Zielordner in pCloud (default: {REMOTE_TEST_DIR})")
    parser.add_argument("--mode", choices=["normal", "abort-after", "resume", "timeout-test"], 
                       default="normal", help="Test-Modus")
    parser.add_argument("--abort-after-chunks", type=int, default=3, 
                       help="Bei 'abort-after': Nach N Chunks abbrechen (default: 3)")
    parser.add_argument("--timeout-minutes", type=int, default=10,
                       help="Bei 'timeout-test': Minuten warten vor Resume (default: 10)")
    parser.add_argument("--env-file", help="Pfad zur .env-Datei (optional, default: auto-detect)")
    
    args = parser.parse_args()
    
    # Datei validieren
    if not os.path.exists(args.file):
        print(f"FEHLER: Datei nicht gefunden: {args.file}", file=sys.stderr)
        sys.exit(1)
    
    # pCloud-Config laden
    cfg = pc.effective_config(env_file=args.env_file)
    
    if not cfg.get("token"):
        print("FEHLER: PCLOUD_TOKEN nicht gesetzt in .env", file=sys.stderr)
        sys.exit(1)
    
    # Remote-Pfad
    filename = os.path.basename(args.file)
    remote_path = f"{args.remote_dir.rstrip('/')}/{filename}"
    
    # Upload starten
    try:
        result = chunked_upload_with_resume(
            cfg=cfg,
            local_path=args.file,
            remote_path=remote_path,
            mode=args.mode,
            abort_after_chunks=args.abort_after_chunks,
            timeout_minutes=args.timeout_minutes
        )
        sys.exit(0)
    except KeyboardInterrupt:
        log("", "")
        log("=== ABBRUCH DURCH BENUTZER (Ctrl+C) ===", "WARN")
        state_file = get_state_file(args.file)
        log(f"State wurde gespeichert in: {state_file}", "INFO")
        log(f"Zum Resume: python {sys.argv[0]} --file {args.file} --mode resume", "INFO")
        sys.exit(130)
    except Exception as e:
        log(f"FEHLER: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
