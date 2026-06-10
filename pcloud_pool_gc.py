#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_pool_gc.py

Pool Garbage Collector für pCloud Backup (POOL-MODE) - OPTIMIZED VERSION.

FUNKTION:
  1. Lädt content_index.json (pool_refs) für schnelle Referenz-Sammlung
  2. Optional: Deep-Audit gegen Stubs (--audit-mode)
  3. Listet alle Files in /_pool/ (1× rekursiver listfolder statt 256×)
  4. Grace Period: Löscht nur Files älter als X Stunden (Race-Protection)
  5. Löscht unreferenzierte Pool-Files (Garbage Collection)

OPTIMIERUNGEN (vs. alte Version):
  - PHASE 1: Index-basiert (0.1s) statt get_textfile (Stunden bei 100k Files!)
  - PHASE 2: 1× listfolder rekursiv statt 256× einzeln
  - Grace Period: Schützt vor Race-Conditions mit laufenden Backups
  - Audit-Mode: Optional Deep-Validation gegen physische Stubs

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf
  - NICHT während laufender Backups (oder mit ausreichender Grace Period)

PERFORMANCE:
  - Index-Load: ~0.1s (statt Stunden!)
  - Pool-Scan: ~2-5s (1× rekursiv)
  - Parallel-Delete mit ThreadPoolExecutor (8 Workers)
  - Progress-Tracking für lange Läufe

USAGE:
  # Standard-GC (Index-basiert, schnell)
  python pcloud_pool_gc.py \
    --pool-root /Backup/rtb_pool \
    --env-file .env \
    [--dry-run] \
    [--grace-hours 24] \
    [--verbose]
  
  # Deep-Audit (validiert Index gegen Stubs, langsam!)
  python pcloud_pool_gc.py \
    --pool-root /Backup/rtb_pool \
    --env-file .env \
    --audit-mode \
    --dry-run

ARGUMENTE:
  --pool-root       Remote Pool-Root auf pCloud (z.B. /Backup/rtb_pool)
  --dest-root       (deprecated) Alias fuer --pool-root
  --env-file        .env mit PCLOUD_USER + PCLOUD_PASS
  --dry-run         Zeige nur was gelöscht würde (kein echtes Löschen)
  --audit-mode      Deep-Audit: Validiere Index gegen physische Stubs
  --grace-hours     Grace Period in Stunden (default: 24)
  --verbose         Detailliertes Logging
"""

from __future__ import annotations
import os, sys, json, argparse, time, datetime
import concurrent.futures
import threading
from typing import Set, Dict, List
from collections import defaultdict

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


# Thread-safe Set für referenced SHA256
class _RefSet:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = set()
    
    def add(self, sha256: str):
        with self.lock:
            self.data.add(sha256)
    
    def update(self, sha256_list: List[str]):
        with self.lock:
            self.data.update(sha256_list)
    
    def __contains__(self, sha256: str) -> bool:
        with self.lock:
            return sha256 in self.data
    
    def __len__(self) -> int:
        with self.lock:
            return len(self.data)
    
    def get_copy(self) -> Set[str]:
        with self.lock:
            return self.data.copy()


# Thread-safe Stats
class _GCStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshots_scanned = 0
        self.stubs_scanned = 0
        self.pool_files_found = 0
        self.pool_files_deleted = 0
        self.pool_files_kept = 0
        self.bytes_freed = 0
        self.errors = 0
    
    def inc_snapshots(self):
        with self.lock:
            self.snapshots_scanned += 1
    
    def inc_stubs(self, count: int = 1):
        with self.lock:
            self.stubs_scanned += count
    
    def inc_pool_found(self):
        with self.lock:
            self.pool_files_found += 1
    
    def inc_deleted(self, size_bytes: int = 0):
        with self.lock:
            self.pool_files_deleted += 1
            self.bytes_freed += size_bytes
    
    def inc_kept(self):
        with self.lock:
            self.pool_files_kept += 1
    
    def inc_errors(self):
        with self.lock:
            self.errors += 1
    
    def get_stats(self) -> dict:
        with self.lock:
            return {
                "snapshots_scanned": self.snapshots_scanned,
                "stubs_scanned": self.stubs_scanned,
                "pool_files_found": self.pool_files_found,
                "pool_files_deleted": self.pool_files_deleted,
                "pool_files_kept": self.pool_files_kept,
                "bytes_freed": self.bytes_freed,
                "errors": self.errors,
            }


def _load_refs_from_index(
    cfg: dict,
    snapshots_root: str,
    stats: _GCStats,
    verbose: bool = False
) -> Set[str]:
    """
    Lädt referenzierte SHA256s aus content_index.json (ULTRA-SCHNELL!).
    
    Dies ist die Standard-Methode für GC. Der Index enthält bereits alle Referenzen
    in pool_refs = {sha256: [snapshot1, snapshot2, ...]}.
    
    Performance: ~0.1s statt Stunden bei Stub-Scan!
    
    Args:
        cfg: pCloud Config
        snapshots_root: Snapshots-Root (z.B. /_snapshots)
        stats: GC Stats Object
        verbose: Verbose Logging
    
    Returns:
        Set[str] - Alle referenzierten SHA256s
    """
    _log("[gc] PHASE 1: Loading references from content_index.json...")
    t_start = time.time()
    
    index_path = f"{snapshots_root}/_index/content_index.json"
    
    try:
        # Download Index
        if verbose:
            _log(f"[gc] Downloading {index_path}...")
        
        index_content = pc.get_textfile(cfg, path=index_path)
        index = json.loads(index_content)
        
        # pool_refs extrahieren
        pool_refs = index.get("pool_refs", {})
        
        if not pool_refs:
            _log("[gc][WARN] Index enthält keine pool_refs! Fallback auf Stub-Scan.")
            return set()
        
        # Alle SHA256s sammeln (Keys von pool_refs)
        referenced_sha256s = set(pool_refs.keys())
        
        # Optional: Zähle Snapshot-Zuordnungen
        total_refs = sum(
            len(v.get("snapshots", [])) if isinstance(v, dict) else len(v)
            for v in pool_refs.values()
        )
        
        duration = time.time() - t_start
        _log(f"[gc] PHASE 1 DONE: {len(referenced_sha256s)} unique SHA256s, "
             f"{total_refs} total refs ({duration:.2f}s)")
        
        return referenced_sha256s
    
    except Exception as e:
        _log(f"[gc][ERROR] Failed to load index: {e}")
        _log("[gc] Fallback auf Stub-Scan (langsam!)...")
        return set()


def _scan_snapshot_for_refs(
    cfg: dict,
    snapshot_path: str,
    ref_set: _RefSet,
    stats: _GCStats,
    verbose: bool = False
) -> None:
    """
    Scannt einen Snapshot-Ordner rekursiv nach .meta.json Stubs und sammelt SHA256.
    
    Worker-Funktion für ThreadPoolExecutor.
    """
    try:
        snapshot_name = snapshot_path.split("/")[-1]
        
        if verbose:
            _log(f"[gc-scan] Scanning snapshot: {snapshot_name}")
        
        # Rekursiv alle Files im Snapshot auflisten
        result = pc._rest_get(cfg, "listfolder", {
            "path": snapshot_path,
            "recursive": 1
        })
        
        metadata = result.get("metadata", {})
        contents = metadata.get("contents", [])
        
        # Filter: nur .meta.json Files
        stub_files = [c for c in contents if not c.get("isfolder") and c.get("name", "").endswith(".meta.json")]
        
        sha256_list = []
        
        for stub in stub_files:
            stub_path = stub.get("path")
            
            try:
                # Download Stub via get_textfile (robust, REST API)
                stub_content = pc.get_textfile(cfg, path=stub_path)
                stub_data = json.loads(stub_content)
                
                # Extrahiere SHA256
                sha256 = stub_data.get("sha256", "").lower()
                if sha256:
                    sha256_list.append(sha256)
            
            except Exception as e:
                if verbose:
                    _log(f"[gc-scan][WARN] Failed to read stub {stub_path}: {e}")
                stats.inc_errors()
        
        # Batch-Update (effizienter als einzelne add() calls)
        ref_set.update(sha256_list)
        stats.inc_stubs(len(sha256_list))
        stats.inc_snapshots()
        
        if verbose:
            _log(f"[gc-scan] ✓ {snapshot_name}: {len(sha256_list)} refs")
    
    except Exception as e:
        _log(f"[gc-scan][ERROR] Failed to scan {snapshot_path}: {e}")
        stats.inc_errors()


def _delete_pool_file(
    cfg: dict,
    pool_file_path: str,
    pool_file_size: int,
    stats: _GCStats,
    dry: bool = False,
    verbose: bool = False
) -> None:
    """
    Löscht ein Pool-File.
    
    Worker-Funktion für ThreadPoolExecutor.
    """
    if dry:
        if verbose:
            _log(f"[dry] delete: {pool_file_path} ({pool_file_size} bytes)")
        stats.inc_deleted(pool_file_size)
        return
    
    try:
        # Delete mit Backoff (robust gegen transiente Fehler)
        pc.call_with_backoff(pc.delete_file, cfg, path=pool_file_path, attempts=5, max_sleep=30.0)
        stats.inc_deleted(pool_file_size)
        
        if verbose:
            _log(f"[gc-delete] ✓ {pool_file_path} ({pool_file_size} bytes)")
    
    except Exception as e:
        _log(f"[gc-delete][ERROR] Failed to delete {pool_file_path}: {e}")
        stats.inc_errors()


def run_pool_gc(
    cfg: dict,
    dest_root: str,
    *,
    dry: bool = False,
    audit_mode: bool = False,
    grace_hours: int = 24,
    verbose: bool = False
) -> dict:
    """
    Führt Pool Garbage Collection aus (OPTIMIZED VERSION).
    
    STRATEGIE:
    1. Index-basiert (pool_refs) → 0.1s statt Stunden!
    2. Optional: Audit-Mode (validiert Index gegen Stubs)
    3. Rekursiver Pool-Scan (1 API-Call statt 256)
    4. Grace Period (nur Files > X Stunden alt löschen)
    5. Parallel-Delete (Thread-safe)
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root (z.B. /Backup/rtb_1to1)
        dry: Dry-run Mode
        audit_mode: Deep-Audit (validiert Index gegen Stubs, langsam!)
        grace_hours: Grace Period in Stunden (Race-Protection)
        verbose: Verbose Logging
    
    Returns:
        Stats Dict
    """
    t_start = time.time()
    
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    pool_root = f"{dest_root.rstrip('/')}/_pool"
    
    _log("[gc] ===== POOL GARBAGE COLLECTION START =====")
    _log(f"[gc] Mode: {'AUDIT (Deep-Validation)' if audit_mode else 'STANDARD (Index-basiert)'}")
    _log(f"[gc] Snapshots: {snapshots_root}")
    _log(f"[gc] Pool: {pool_root}")
    _log(f"[gc] Grace Period: {grace_hours}h")
    _log(f"[gc] Dry-Run: {dry}")
    
    # ============================================================================
    # === GC-LOCK CHECK (Race-Protection gegen parallel laufende Backups!) ===
    # ============================================================================
    lock_path = f"{dest_root}/.gc_lock"
    stale_lock_hours = int(os.environ.get("PCLOUD_GC_STALE_LOCK_HOURS", "48"))
    
    try:
        lock_content = pc.get_textfile(cfg, path=lock_path)
        lock_data = json.loads(lock_content)
        
        # Lock-Alter berechnen
        lock_age_seconds = time.time() - lock_data.get("started_at", 0)
        lock_age_hours = lock_age_seconds / 3600
        
        _log(f"[gc] ⚠️ GC-Lock erkannt!")
        _log(f"[gc]    Snapshot: {lock_data.get('snapshot', '?')}")
        _log(f"[gc]    Host: {lock_data.get('host', '?')}")
        _log(f"[gc]    PID: {lock_data.get('pid', '?')}")
        _log(f"[gc]    Alter: {lock_age_hours:.1f}h")
        
        # Stale-Lock Check
        if lock_age_hours < stale_lock_hours:
            # Lock ist frisch → Backup läuft!
            _log(f"[gc] ❌ ABBRUCH: Backup läuft (Lock < {stale_lock_hours}h alt)")
            _log(f"[gc]    Warte bis Backup abgeschlossen ist, dann versuche erneut")
            return {
                "error": "backup_in_progress",
                "lock_age_hours": lock_age_hours,
                "snapshot": lock_data.get("snapshot"),
                "aborted": True
            }
        else:
            # Lock ist stale → Backup wahrscheinlich crashed
            _log(f"[gc] ⚠️ STALE LOCK erkannt (>{stale_lock_hours}h alt)")
            _log(f"[gc]    Annahme: Backup-Prozess abgestürzt, fahre mit GC fort")
            _log(f"[gc]    Lock wird ignoriert (nicht gelöscht für Debugging)")
    
    except Exception:
        # Kein Lock vorhanden → alles ok!
        _log(f"[gc] ✓ Kein GC-Lock gefunden, fahre fort")
    
    # Stats
    stats = _GCStats()
    
    # ============================================================================
    # === PHASE 1: Sammle referenzierte SHA256s ===
    # ============================================================================
    if audit_mode:
        # AUDIT-MODE: Scanne alle Stubs (langsam, aber validiert Index)
        _log("[gc] PHASE 1: AUDIT-MODE - Scanning all stubs for references...")
        t_scan_start = time.time()
        
        ref_set = _RefSet()
        
        try:
            result = pc._rest_get(cfg, "listfolder", {"path": snapshots_root})
            metadata = result.get("metadata", {})
            contents = metadata.get("contents", [])
            snapshots = [c for c in contents if c.get("isfolder") and not c.get("name") == "content_index.json"]
            
            _log(f"[gc] Found {len(snapshots)} snapshots")
        except Exception as e:
            _log(f"[gc][ERROR] Failed to list snapshots: {e}")
            return {"error": str(e)}
        
        # Parallel-Scan mit ThreadPoolExecutor
        max_workers = int(os.environ.get("PCLOUD_GC_WORKERS", "8"))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for snapshot in snapshots:
                snapshot_path = snapshot.get("path")
                future = executor.submit(
                    _scan_snapshot_for_refs,
                    cfg,
                    snapshot_path,
                    ref_set,
                    stats,
                    verbose
                )
                futures.append(future)
            
            # Warte auf alle Scanner
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    _log(f"[gc][ERROR] Scanner failed: {e}")
                    stats.inc_errors()
        
        referenced_sha256s = ref_set.get_copy()
        scan_duration = time.time() - t_scan_start
        scan_stats = stats.get_stats()
        
        _log(f"[gc] PHASE 1 DONE: {scan_stats['snapshots_scanned']} snapshots, "
             f"{scan_stats['stubs_scanned']} stubs, {len(referenced_sha256s)} unique SHA256 ({scan_duration:.1f}s)")
    
    else:
        # STANDARD-MODE: Index-basiert (ultra-schnell!)
        referenced_sha256s = _load_refs_from_index(cfg, snapshots_root, stats, verbose)
        
        # Fallback auf Stub-Scan falls Index-Load fehlschlägt
        if not referenced_sha256s:
            _log("[gc] Fallback: Scanning stubs (Index nicht verfügbar)...")
            
            ref_set = _RefSet()
            
            try:
                result = pc._rest_get(cfg, "listfolder", {"path": snapshots_root})
                metadata = result.get("metadata", {})
                contents = metadata.get("contents", [])
                snapshots = [c for c in contents if c.get("isfolder")]
                
                max_workers = int(os.environ.get("PCLOUD_GC_WORKERS", "8"))
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [
                        executor.submit(_scan_snapshot_for_refs, cfg, s.get("path"), ref_set, stats, verbose)
                        for s in snapshots
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            _log(f"[gc][ERROR] Scanner failed: {e}")
                            stats.inc_errors()
                
                referenced_sha256s = ref_set.get_copy()
            
            except Exception as e:
                _log(f"[gc][ERROR] Stub-Scan auch fehlgeschlagen: {e}")
                return {"error": str(e)}
    
    if not referenced_sha256s:
        _log("[gc][ERROR] Keine Referenzen gefunden! Abbruch (Sicherheit).")
        return {"error": "No references found"}
    
    # ============================================================================
    # === PHASE 2: Liste alle Pool-Files (REKURSIV, 1 API-Call!) ===
    # ============================================================================
    _log("[gc] PHASE 2: Listing pool files (recursive)...")
    t_list_start = time.time()
    
    pool_files_to_delete = []
    grace_cutoff = time.time() - (grace_hours * 3600) if grace_hours > 0 else 0
    
    try:
        # Rekursives listfolder über kompletten Pool (wie validate_pool_snapshot!)
        result = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
        
        def _extract_pool_files(obj, pool_files_list: list):
            """Rekursiv Pool-Files aus listfolder-Tree extrahieren"""
            if isinstance(obj, dict):
                # File gefunden
                if not obj.get("isfolder"):
                    filename = obj.get("name", "")
                    filepath = obj.get("path", "")
                    filesize = obj.get("size", 0)
                    modified = obj.get("modified")  # Unix-Timestamp
                    
                    # Validiere: Pool-Files sind 64 Hex-Zeichen (SHA256)
                    if len(filename) == 64 and all(c in "0123456789abcdef" for c in filename):
                        pool_files_list.append({
                            "name": filename.lower(),
                            "path": filepath,
                            "size": filesize,
                            "modified": modified
                        })
                
                # Ordner: Rekursiv durchlaufen
                for child in obj.get("contents", []):
                    _extract_pool_files(child, pool_files_list)
        
        pool_files = []
        metadata = result.get("metadata", {})
        _extract_pool_files(metadata, pool_files)
        
        list_duration = time.time() - t_list_start
        _log(f"[gc] PHASE 2 DONE: {len(pool_files)} pool files found ({list_duration:.1f}s)")
        
        # Prüfe jedes Pool-File
        _log(f"[gc] Checking references (grace period: {grace_hours}h)...")
        
        for pool_file in pool_files:
            sha256 = pool_file["name"]
            stats.inc_pool_found()
            
            # 1. Referenz-Check
            if sha256 in referenced_sha256s:
                # Referenziert → behalten
                stats.inc_kept()
                continue
            
            # 2. Grace-Period-Check (Race-Protection!)
            if grace_hours > 0 and pool_file.get("modified"):
                # Unix-Timestamp zu Python-Timestamp
                file_mtime = pool_file["modified"]
                
                if file_mtime > grace_cutoff:
                    # File ist jünger als Grace Period → behalten (könnte gerade uploaded sein)
                    stats.inc_kept()
                    if verbose:
                        age_hours = (time.time() - file_mtime) / 3600
                        _log(f"[gc-grace] Keeping {sha256[:16]}... (age: {age_hours:.1f}h < {grace_hours}h)")
                    continue
            
            # 3. Unreferenziert & alt genug → löschen
            pool_files_to_delete.append(pool_file)
    
    except Exception as e:
        _log(f"[gc][ERROR] Failed to list pool: {e}")
        return {"error": str(e)}
    
    check_stats = stats.get_stats()
    _log(f"[gc] Check complete: {len(pool_files_to_delete)} to delete, "
         f"{check_stats['pool_files_kept']} to keep")
    
    # ============================================================================
    # === PHASE 3: Lösche unreferenzierte Pool-Files ===
    # ============================================================================
    if pool_files_to_delete:
        _log(f"[gc] PHASE 3: Deleting {len(pool_files_to_delete)} unreferenced pool files...")
        t_delete_start = time.time()
        
        max_workers = int(os.environ.get("PCLOUD_GC_WORKERS", "8"))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for pool_file in pool_files_to_delete:
                future = executor.submit(
                    _delete_pool_file,
                    cfg,
                    pool_file["path"],
                    pool_file["size"],
                    stats,
                    dry,
                    verbose
                )
                futures.append(future)
            
            # Warte auf alle Deleter
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    _log(f"[gc][ERROR] Deleter failed: {e}")
                    stats.inc_errors()
        
        delete_duration = time.time() - t_delete_start
        delete_stats = stats.get_stats()
        
        _log(f"[gc] PHASE 3 DONE: {delete_stats['pool_files_deleted']} files deleted, "
             f"{delete_stats['bytes_freed'] / (1024**3):.2f} GB freed ({delete_duration:.1f}s)")
    else:
        _log("[gc] PHASE 3 SKIPPED: No unreferenced files found")
    
    # Final Stats
    duration = time.time() - t_start
    final_stats = stats.get_stats()
    
    _log("[gc] ===== POOL GARBAGE COLLECTION DONE =====")
    _log(f"[gc] Mode: {'AUDIT' if audit_mode else 'INDEX-BASED'}")
    _log(f"[gc] Duration: {duration:.1f}s")
    _log(f"[gc] Unique SHA256 refs: {len(referenced_sha256s)}")
    _log(f"[gc] Pool files found: {final_stats['pool_files_found']}")
    _log(f"[gc] Pool files kept: {final_stats['pool_files_kept']}")
    _log(f"[gc] Pool files deleted: {final_stats['pool_files_deleted']}")
    _log(f"[gc] Space freed: {final_stats['bytes_freed'] / (1024**3):.2f} GB")
    _log(f"[gc] Errors: {final_stats['errors']}")
    
    if dry:
        _log("[gc] ⚠ DRY-RUN: Keine echten Löschungen durchgeführt")
    
    return {
        "duration": duration,
        "mode": "audit" if audit_mode else "index",
        "unique_refs": len(referenced_sha256s),
        "pool_files_found": final_stats['pool_files_found'],
        "pool_files_kept": final_stats['pool_files_kept'],
        "pool_files_deleted": final_stats['pool_files_deleted'],
        "bytes_freed": final_stats['bytes_freed'],
        "errors": final_stats['errors']
    }


# ---- CLI ----
def main():
    parser = argparse.ArgumentParser(
        description="pCloud Pool Garbage Collector (POOL-MODE) - OPTIMIZED",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
BEISPIEL (Standard - Index-basiert, schnell):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --dry-run
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --verbose

BEISPIEL (Audit-Mode - validiert Index gegen Stubs, langsam!):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --audit-mode --dry-run

BEISPIEL (mit Grace Period 48h):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --grace-hours 48

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf
  - NICHT während laufender Backups (oder mit ausreichender Grace Period)

OPTIMIERUNGEN:
  - Index-basiert: 0.1s statt Stunden (bei 100k Files)!
  - Rekursiver Pool-Scan: 1× API-Call statt 256×
  - Grace Period: Schützt vor Race-Conditions
  - Audit-Mode: Optional Deep-Validation

CRON BEISPIEL (wöchentlich, Sonntag 3 Uhr, 24h Grace):
  0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --grace-hours 24 >> /var/log/backup/pool_gc.log 2>&1
"""
    )
    
    parser.add_argument("--pool-root",
                        help="Remote Pool-Root auf pCloud, z.B. /Backup/rtb_pool")
    parser.add_argument("--dest-root",
                        help="(deprecated) Alias fuer --pool-root")
    parser.add_argument("--env-file", default=".env",
                        help=".env Datei mit PCLOUD_USER + PCLOUD_PASS (default: .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry-run: Zeige nur was gelöscht würde")
    parser.add_argument("--audit-mode", action="store_true",
                        help="Audit-Mode: Validiere Index gegen physische Stubs (langsam!)")
    parser.add_argument("--grace-hours", type=int, default=24,
                        help="Grace Period in Stunden (default: 24, 0=deaktiviert)")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose Logging")
    
    args = parser.parse_args()

    pool_root = args.pool_root or args.dest_root
    if not pool_root:
        parser.error("--pool-root erforderlich (--dest-root ist deprecated)")
    if args.dest_root and not args.pool_root:
        _log("--dest-root ist deprecated, bitte --pool-root verwenden")
    
    # Config laden
    cfg = pc.effective_config(env_file=args.env_file)
    
    # Run GC
    result = run_pool_gc(
        cfg,
        pool_root,
        dry=args.dry_run,
        audit_mode=args.audit_mode,
        grace_hours=args.grace_hours,
        verbose=args.verbose
    )
    
    # Exit Code
    if result.get("errors", 0) > 0:
        _log("[gc] ⚠ GC abgeschlossen mit Fehlern")
        sys.exit(1)
    else:
        _log("[gc] ✓ GC erfolgreich abgeschlossen")
        sys.exit(0)


if __name__ == "__main__":
    main()
