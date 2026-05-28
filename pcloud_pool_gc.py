#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_pool_gc.py

Pool Garbage Collector für pCloud Backup (POOL-MODE).

FUNKTION:
  1. Scannt alle Snapshots in /_snapshots/
  2. Sammelt alle referenzierten SHA256 aus .meta.json Stubs
  3. Listet alle Files in /_pool/
  4. Löscht unreferenzierte Pool-Files (Garbage Collection)

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf

PERFORMANCE:
  - Parallel-Scan mit ThreadPoolExecutor (8 Workers)
  - Batch-Delete für große Mengen
  - Progress-Tracking für lange Läufe

USAGE:
  python pcloud_pool_gc.py \
    --dest-root /Backup/rtb_1to1 \
    --env-file .env \
    [--dry-run] \
    [--verbose]

ARGUMENTE:
  --dest-root     Remote Root (z.B. /Backup/rtb_1to1)
  --env-file      .env mit PCLOUD_USER + PCLOUD_PASS
  --dry-run       Zeige nur was gelöscht würde (kein echtes Löschen)
  --verbose       Detailliertes Logging
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
    verbose: bool = False
) -> dict:
    """
    Führt Pool Garbage Collection aus.
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root (z.B. /Backup/rtb_1to1)
        dry: Dry-run Mode
        verbose: Verbose Logging
    
    Returns:
        Stats Dict
    """
    t_start = time.time()
    
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    pool_root = f"{dest_root.rstrip('/')}/_pool"
    
    _log("[gc] ===== POOL GARBAGE COLLECTION START =====")
    _log(f"[gc] Snapshots: {snapshots_root}")
    _log(f"[gc] Pool: {pool_root}")
    
    # Stats
    stats = _GCStats()
    ref_set = _RefSet()
    
    # PHASE 1: Scan alle Snapshots und sammle referenzierte SHA256
    _log("[gc] PHASE 1: Scanning snapshots for referenced SHA256...")
    t_scan_start = time.time()
    
    try:
        result = pc._rest_get(cfg, "listfolder", {"path": snapshots_root})
        metadata = result.get("metadata", {})
        contents = metadata.get("contents", [])
        snapshots = [c for c in contents if c.get("isfolder")]
        
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
    
    scan_duration = time.time() - t_scan_start
    scan_stats = stats.get_stats()
    
    _log(f"[gc] PHASE 1 DONE: {scan_stats['snapshots_scanned']} snapshots, "
         f"{scan_stats['stubs_scanned']} stubs, {len(ref_set)} unique SHA256 ({scan_duration:.1f}s)")
    
    # PHASE 2: Liste alle Pool-Files und prüfe Referenzen
    _log("[gc] PHASE 2: Listing pool files...")
    t_list_start = time.time()
    
    pool_files_to_delete = []
    
    try:
        # Liste alle Prefix-Ordner (00-ff)
        result = pc._rest_get(cfg, "listfolder", {"path": pool_root})
        metadata = result.get("metadata", {})
        prefix_folders = [c for c in metadata.get("contents", []) if c.get("isfolder")]
        
        _log(f"[gc] Found {len(prefix_folders)} pool prefix folders")
        
        # Für jeden Prefix-Ordner: Liste Files
        for prefix_folder in prefix_folders:
            prefix_path = prefix_folder.get("path")
            
            try:
                result = pc._rest_get(cfg, "listfolder", {"path": prefix_path})
                metadata = result.get("metadata", {})
                pool_files = [c for c in metadata.get("contents", []) if not c.get("isfolder")]
                
                for pool_file in pool_files:
                    pool_file_name = pool_file.get("name")
                    pool_file_path = pool_file.get("path")
                    pool_file_size = pool_file.get("size", 0)
                    
                    stats.inc_pool_found()
                    
                    # Prüfe ob SHA256 referenziert ist
                    sha256 = pool_file_name.lower()
                    
                    if sha256 in ref_set:
                        # Referenziert → behalten
                        stats.inc_kept()
                    else:
                        # Unreferenziert → löschen
                        pool_files_to_delete.append({
                            "path": pool_file_path,
                            "size": pool_file_size
                        })
            
            except Exception as e:
                _log(f"[gc][WARN] Failed to list {prefix_path}: {e}")
                stats.inc_errors()
    
    except Exception as e:
        _log(f"[gc][ERROR] Failed to list pool root: {e}")
        return {"error": str(e)}
    
    list_duration = time.time() - t_list_start
    list_stats = stats.get_stats()
    
    _log(f"[gc] PHASE 2 DONE: {list_stats['pool_files_found']} pool files found, "
         f"{len(pool_files_to_delete)} to delete, {list_stats['pool_files_kept']} to keep ({list_duration:.1f}s)")
    
    # PHASE 3: Lösche unreferenzierte Pool-Files
    if pool_files_to_delete:
        _log(f"[gc] PHASE 3: Deleting {len(pool_files_to_delete)} unreferenced pool files...")
        t_delete_start = time.time()
        
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
    _log(f"[gc] Duration: {duration:.1f}s")
    _log(f"[gc] Snapshots scanned: {final_stats['snapshots_scanned']}")
    _log(f"[gc] Stubs scanned: {final_stats['stubs_scanned']}")
    _log(f"[gc] Unique SHA256: {len(ref_set)}")
    _log(f"[gc] Pool files found: {final_stats['pool_files_found']}")
    _log(f"[gc] Pool files kept: {final_stats['pool_files_kept']}")
    _log(f"[gc] Pool files deleted: {final_stats['pool_files_deleted']}")
    _log(f"[gc] Space freed: {final_stats['bytes_freed'] / (1024**3):.2f} GB")
    _log(f"[gc] Errors: {final_stats['errors']}")
    
    return {
        "duration": duration,
        "snapshots_scanned": final_stats['snapshots_scanned'],
        "stubs_scanned": final_stats['stubs_scanned'],
        "unique_refs": len(ref_set),
        "pool_files_found": final_stats['pool_files_found'],
        "pool_files_kept": final_stats['pool_files_kept'],
        "pool_files_deleted": final_stats['pool_files_deleted'],
        "bytes_freed": final_stats['bytes_freed'],
        "errors": final_stats['errors']
    }


# ---- CLI ----
def main():
    parser = argparse.ArgumentParser(
        description="pCloud Pool Garbage Collector (POOL-MODE)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
BEISPIEL:
  python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env --dry-run
  python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env --verbose

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf

CRON BEISPIEL (wöchentlich, Sonntag 3 Uhr):
  0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env >> /var/log/backup/pool_gc.log 2>&1
"""
    )
    
    parser.add_argument("--dest-root", required=True,
                        help="Remote Root (z.B. /Backup/rtb_1to1)")
    parser.add_argument("--env-file", default=".env",
                        help=".env Datei mit PCLOUD_USER + PCLOUD_PASS (default: .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry-run: Zeige nur was gelöscht würde")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose Logging")
    
    args = parser.parse_args()
    
    # Config laden
    cfg = pc.load_config_from_env(args.env_file)
    
    if not cfg:
        _log("[ERROR] Konnte pCloud Config nicht laden!")
        sys.exit(1)
    
    # Run GC
    result = run_pool_gc(
        cfg,
        args.dest_root,
        dry=args.dry_run,
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
