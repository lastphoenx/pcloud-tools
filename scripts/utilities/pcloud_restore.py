#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_restore.py – Snapshot-Restore von pCloud

Download von pCloud-Snapshots mit:
- content_index.json Parsing (deduplizierter Index)
- Download echter Dateien von anchor_path (statt Stubs)
- SHA256-Integrity-Check nach Download
- Chunk-basierter Download (RAM-freundlich)
- Ordnerstruktur vom Snapshot beibehalten (relpath)
"""
from __future__ import annotations
import os, sys, json, argparse, hashlib, time, datetime, threading
import concurrent.futures
from typing import Dict, List, Any, Optional

try:
    import pcloud_bin_lib as pc
except ImportError:
    print("[error] pcloud_bin_lib.py nicht gefunden. PYTHONPATH setzen?", file=sys.stderr)
    sys.exit(2)

def log(msg: str, level: str = "info"):
    """Logging mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr if level == "error" else sys.stdout)

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB

# === Parallel Download Configuration ===
SMALL_FILE_THRESHOLD_BYTES = int(os.environ.get("PCLOUD_DOWNLOAD_SMALL_THRESHOLD", str(50 * 1024 * 1024)))  # 50 MB
PARALLEL_DOWNLOAD_THREADS = int(os.environ.get("PCLOUD_DOWNLOAD_THREADS", "16"))


class IndexLoadError(Exception):
    """Fehler beim Laden/Parsen des Content-Index von pCloud."""
    pass

def download_file_with_verify(cfg: Dict, remote_path: str, local_path: str, sha256_expected: Optional[str] = None) -> bool:
    """
    Datei von pCloud streamen + SHA256-Verifikation.
    RAM-schonend: kein vollständiges In-Memory-Puffern.
    """
    try:
        stat = pc.stat_file(cfg, path=remote_path, with_checksum=False) or {}
        file_size = stat.get("size", 0)
        log(f"Download: {remote_path} ({file_size:,} bytes)")

        pc.download_binaryfile_to(
            cfg, path=remote_path,
            local_path=local_path,
            sha256_verify=sha256_expected,
        )
        if sha256_expected:
            log("\u2713 SHA256 OK")
        return True

    except ValueError as e:
        # SHA256-Mismatch (Datei wurde von download_binaryfile_to gelöscht)
        log(str(e), "error")
        return False
    except Exception as e:
        log(f"Download fehlgeschlagen: {e}", "error")
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass
        return False

def download_via_fileid(cfg: Dict, fileid: int, local_path: str, sha256_expected: Optional[str] = None) -> bool:
    """
    Download via FileID — streaming, RAM-schonend.
    """
    try:
        stat = pc.stat_file(cfg, fileid=fileid, with_checksum=False) or {}
        log(f"Download (FileID {fileid}): {stat.get('size', 0):,} bytes")

        pc.download_binaryfile_to(
            cfg, fileid=fileid,
            local_path=local_path,
            sha256_verify=sha256_expected,
        )
        if sha256_expected:
            log("\u2713 SHA256 OK")
        return True

    except ValueError as e:
        log(str(e), "error")
        return False
    except Exception as e:
        log(f"Download (FileID) fehlgeschlagen: {e}", "error")
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass
        return False

def verify_files(out_dir: str, items: List[Dict]) -> Dict[str, int]:
    """
    SHA256-Verifikation der heruntergladenen Dateien
    
    Returns:
        {"verified": count, "mismatches": count, "errors": count}
    """
    log("Starte SHA256-Verifikation...")
    stats = {"verified": 0, "mismatches": 0, "errors": 0}
    
    for item in items:
        relpath = item.get("relpath", "?")
        sha256 = item.get("sha256")
        local_file = os.path.join(out_dir, relpath)
        
        if not sha256:
            continue
        
        if not os.path.exists(local_file):
            log(f"[missing] {relpath}", "warn")
            stats["errors"] += 1
            continue
        
        try:
            hash_obj = hashlib.sha256()
            with open(local_file, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                    hash_obj.update(chunk)

            actual_sha = hash_obj.hexdigest()
            if actual_sha.lower() == sha256.lower():
                stats["verified"] += 1
            else:
                log(f"[mismatch] {relpath}: local={actual_sha} index={sha256}", "error")
                stats["mismatches"] += 1
        
        except Exception as e:
            log(f"[error] {relpath}: {e}", "error")
            stats["errors"] += 1
    
    return stats

class ManifestLoadError(Exception):
    """Fehler beim Laden/Parsen eines lokalen Manifests."""
    pass

def load_index_from_pcloud(cfg: Dict, dest_root: str, snapshot: str) -> List[Dict[str, Any]]:
    """
    Content-Index von pCloud laden und Items für Snapshot extrahieren
    
    Args:
        cfg: pCloud Config
        dest_root: pCloud Basis-Pfad
        snapshot: Snapshot-Name
    
    Returns:
        Liste mit Items (Dateien/Ordner) für diesen Snapshot
    """
    index_path = f"{dest_root}/_snapshots/_index/content_index.json"
    log(f"Lade Content-Index: {index_path}")

    try:
        index = pc.read_json_at_path(cfg, index_path, maxbytes=None)

        if "items" not in index:
            log("Index ungültig (keine 'items')", "error")
            raise IndexLoadError("Index ungültig (keine 'items')")
        
        # Snapshot-Items extrahieren (invert: SHA256 → holders)
        items = []
        for sha256, obj in index["items"].items():
            holders = obj.get("holders", [])
            anchor_path = obj.get("anchor_path")  # echte Datei im Snapshot-Baum
            for holder in holders:
                # Defensive: Handle old bug format where holder is string instead of dict
                if isinstance(holder, str):
                    # Old format: holder is just snapshot name
                    if holder == snapshot:
                        # Use anchor_path as relpath fallback (same location as anchor)
                        relpath = anchor_path.split("/")[-1] if anchor_path else "unknown"
                        items.append({
                            "type": "file",
                            "relpath": relpath,
                            "sha256": sha256,
                            "fileid": obj.get("fileid"),
                            "anchor_path": anchor_path,
                            "size": obj.get("size"),
                        })
                elif isinstance(holder, dict):
                    # New format: holder is dict with snapshot + relpath
                    if holder.get("snapshot") == snapshot:
                        items.append({
                            "type": "file",
                            "relpath": holder.get("relpath"),
                            "sha256": sha256,
                            "fileid": obj.get("fileid"),
                            "anchor_path": anchor_path,
                            "size": obj.get("size"),
                        })
        
        if not items:
            log(f"Snapshot '{snapshot}' nicht gefunden", "error")
            all_snapshots = set()
            for obj in index["items"].values():
                for h in obj.get("holders", []):
                    all_snapshots.add(h.get("snapshot"))
            available = sorted(list(all_snapshots), reverse=True)[:5]
            log(f"Verfügbare: {available}", "error")
            raise IndexLoadError(f"Snapshot '{snapshot}' nicht gefunden. Verfügbare (Top 5): {available}")
        
        log(f"✓ {len(items)} Items für Snapshot {snapshot}")
        return items
    
    except Exception as e:
        log(f"Index-Laden fehlgeschlagen: {e}", "error")
        raise IndexLoadError(f"Index-Laden fehlgeschlagen: {e}")

def load_manifest(manifest_path: str, snapshot_name: str) -> dict:
    """Manifest von lokaler Datei laden und validieren."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError as e:
        raise ManifestLoadError(f"Manifest nicht gefunden: {manifest_path}") from e
    except json.JSONDecodeError as e:
        raise ManifestLoadError(f"Manifest JSON ungültig: {e}") from e

    # Snapshot-Name validieren
    if manifest.get("snapshot") != snapshot_name:
        log(f"Manifest snapshot='{manifest.get('snapshot')}' != requested '{snapshot_name}'", "warn")

    if "items" not in manifest:
        raise ManifestLoadError("Manifest enthält keine 'items' Liste")

    return manifest

def _extract_files_from_listfolder(result: dict, base_path: str = "") -> List[Dict[str, Any]]:
    """
    Extrahiert File-Items aus listfolder-Response (rekursiv).
    
    Args:
        result: listfolder API-Response
        base_path: Basis-Pfad für relpath-Konstruktion
    
    Returns:
        Liste mit File-Items (kompatibel mit Snapshot-Items)
    """
    items = []
    
    def _traverse(obj: dict, parent_path: str = ""):
        if not isinstance(obj, dict):
            return
        
        if obj.get("isfolder"):
            # Folder: traverse children
            folder_name = obj.get("name", "")
            current_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
            for child in obj.get("contents", []):
                _traverse(child, current_path)
        else:
            # File gefunden
            file_name = obj.get("name", "unknown")
            rel_path = f"{parent_path}/{file_name}" if parent_path else file_name
            # Führenden Slash entfernen (Path-Traversal-Schutz)
            rel_path = rel_path.lstrip("/")
            items.append({
                "type": "file",
                "relpath": rel_path,
                "sha256": None,  # Nicht verfügbar bei listfolder
                "fileid": obj.get("fileid"),
                "anchor_path": None,  # Wird via fileid downloaded
                "size": obj.get("size", 0),
            })
    
    metadata = result.get("metadata", {})
    _traverse(metadata, base_path)
    
    return items

def load_direct_folder(cfg: Dict, *, folder_path: str = None, folderid: int = None) -> List[Dict[str, Any]]:
    """
    Lädt Files aus einem pCloud-Ordner (rekursiv) via Pfad oder ID.
    
    Args:
        cfg: pCloud Config
        folder_path: Ordner-Pfad (optional)
        folderid: Ordner-ID (optional)
    
    Returns:
        Liste mit File-Items
    """
    if not folder_path and not folderid:
        raise ValueError("Entweder folder_path oder folderid erforderlich")
    
    log(f"Lade Ordner-Inhalt: {folder_path or f'FolderID={folderid}'} (rekursiv)")
    
    try:
        if folder_path:
            result = pc.listfolder(cfg, path=folder_path, recursive=True)
        else:
            result = pc.listfolder(cfg, folderid=folderid, recursive=True)
        
        # Metadata enthält bereits den Ordner-Namen, base_path="" lassen
        items = _extract_files_from_listfolder(result, base_path="")
        log(f"✓ {len(items)} Dateien gefunden")
        return items
    
    except Exception as e:
        log(f"Ordner-Laden fehlgeschlagen: {e}", "error")
        raise

def load_direct_file(cfg: Dict, *, file_path: str = None, fileid: int = None) -> List[Dict[str, Any]]:
    """
    Lädt ein einzelnes File via Pfad oder ID.
    
    Args:
        cfg: pCloud Config
        file_path: Datei-Pfad (optional)
        fileid: Datei-ID (optional)
    
    Returns:
        Liste mit einem File-Item
    """
    if not file_path and not fileid:
        raise ValueError("Entweder file_path oder fileid erforderlich")
    
    log(f"Lade Datei-Info: {file_path or f'FileID={fileid}'}")
    
    try:
        if file_path:
            stat = pc.stat_file(cfg, path=file_path, with_checksum=False)
            relpath = os.path.basename(file_path)
            anchor = file_path
        else:
            stat = pc.stat_file(cfg, fileid=fileid, with_checksum=False)
            relpath = stat.get("name", f"file_{fileid}")
            anchor = None  # Wird via fileid downloaded
        
        item = {
            "type": "file",
            "relpath": relpath,
            "sha256": None,  # Nicht verfügbar bei stat
            "fileid": stat.get("fileid") or fileid,
            "anchor_path": anchor,
            "size": stat.get("size", 0),
        }
        
        log(f"✓ Datei: {relpath} ({item['size']:,} bytes)")
        return [item]
    
    except Exception as e:
        log(f"Datei-Laden fehlgeschlagen: {e}", "error")
        raise

def main():
    ap = argparse.ArgumentParser(
        description="pCloud Snapshot-Restore (Download echter Dateien vom anchor_path)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  # Verfügbare Snapshots anzeigen
  %(prog)s --manifest pcloud --list-snapshots

  # Plan anzeigen
  %(prog)s --manifest pcloud --snapshot 2025-11-23-082336 --out-dir /tmp/restore

  # Download mit SHA256-Verifikation (PARALLEL)
  %(prog)s --manifest pcloud --snapshot 2025-11-23-082336 --out-dir /srv/pcloud-temp/restore --download --verify

  # === NEU: Direct Download ===
  # Einzelne Datei via Pfad
  %(prog)s --manifest pcloud --file /Backup/rtb_1to1/docs/README.md --out-dir /tmp/restore --download

  # Einzelne Datei via ID
  %(prog)s --manifest pcloud --fileid 123456789 --out-dir /tmp/restore --download

  # Ordner rekursiv via Pfad
  %(prog)s --manifest pcloud --folder /Backup/rtb_1to1/docs --out-dir /tmp/restore --download

  # Ordner rekursiv via ID
  %(prog)s --manifest pcloud --folderid 987654321 --out-dir /tmp/restore --download
        """
    )
    
    ap.add_argument("--manifest", required=True, help="'pcloud' oder lokaler Manifest-Pfad")
    ap.add_argument("--snapshot", help="Snapshot-Name")
    ap.add_argument("--list-snapshots", action="store_true", help="Verfügbare Snapshots aus dem pCloud-Index anzeigen und beenden")
    ap.add_argument("--out-dir", help="Lokales Restore-Ziel (Basis, Snapshot wird als Unterordner angelegt – nur flat-Modus verpflichtend)")

    # === NEU: Direct Download Parameter ===
    ap.add_argument("--folder", help="Ordner-Pfad für direkten Download (rekursiv, überschreibt --snapshot)")
    ap.add_argument("--folderid", type=int, help="Ordner-ID für direkten Download (rekursiv, überschreibt --snapshot)")
    ap.add_argument("--file", help="Datei-Pfad für einzelnen Download (überschreibt --snapshot)")
    ap.add_argument("--fileid", type=int, help="Datei-ID für einzelnen Download (überschreibt --snapshot)")
    
    ap.add_argument("--mode", choices=["flat", "object-store"], default="flat",
                    help="Restore-Modus: 'flat' = direkt in out-dir/snapshot, 'object-store' = lokaler _objects + _snapshots Baum")

    ap.add_argument("--local-objects-root", help="(object-store) Basisverzeichnis für lokalen Object-Store (_objects)")
    ap.add_argument("--local-snapshots-root", help="(object-store) Basisverzeichnis für lokale Snapshot-Bäume (_snapshots)")
    
    ap.add_argument("--dest-root", default="/Backup/rtb_1to1", help="pCloud Basis")
    ap.add_argument("--filter", help="Nur Dateien mit diesem Präfix")
    ap.add_argument("--download", action="store_true", help="Wirklich downloaden (Restore)")
    ap.add_argument("--verify", action="store_true", help="SHA256-Verifikation beim Download")
    ap.add_argument("--verify-only", action="store_true", help="Nur vorhandenen Restore-Baum in --out-dir verifizieren")
    
    # pCloud Config
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--profile")
    ap.add_argument("--env-dir")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--device")
    ap.add_argument("--token")
    
    args = ap.parse_args()

    # --list-snapshots: früh ausführen, kein --snapshot nötig
    if args.list_snapshots:
        cfg = pc.effective_config(
            env_file=args.env_file,
            env_dir=getattr(args, 'env_dir', None),
            profile=args.profile,
            overrides={
                "host": args.host, "port": args.port,
                "timeout": args.timeout, "device": args.device, "token": args.token,
            }
        )
        index_path = f"{args.dest_root}/_snapshots/_index/content_index.json"
        try:
            index = pc.read_json_at_path(cfg, index_path, maxbytes=None)
        except Exception as e:
            log(f"Index laden fehlgeschlagen: {e}", "error")
            return 2
        snapshots: set[str] = set()
        for obj in index.get("items", {}).values():
            for h in obj.get("holders", []):
                if h.get("snapshot"):
                    snapshots.add(h["snapshot"])
        print(f"Verfügbare Snapshots in {args.dest_root}:")
        for s in sorted(snapshots, reverse=True):
            print(f"  {s}")
        return 0

    # === NEU: Direct-Download-Modus (Folder/File via ID oder Pfad) ===
    _direct_mode_params = [args.folder, args.folderid, args.file, args.fileid]
    _direct_mode_active = any(_direct_mode_params)
    
    if _direct_mode_active:
        # Mutual Exclusion prüfen
        if sum([bool(p) for p in _direct_mode_params]) > 1:
            log("Bitte nur EINEN der folgenden Parameter verwenden: --folder, --folderid, --file, --fileid", "error")
            return 2
        
        if args.snapshot:
            log("--snapshot wird ignoriert (Direct-Download-Modus aktiv)", "warn")
        
        # out-dir ist im Direct-Mode verpflichtend
        if not args.out_dir:
            log("--out-dir ist im Direct-Download-Modus erforderlich", "error")
            return 2
        
        log(f"[Direct-Mode] {'Ordner' if (args.folder or args.folderid) else 'Datei'}-Download")
    
    # Snapshot-Mode Validierung (nur wenn nicht Direct-Mode)
    if not _direct_mode_active and not args.snapshot:
        log("--snapshot ist erforderlich (oder --list-snapshots für Übersicht, oder Direct-Download-Parameter)", "error")
        return 2

    # Modus-Konflikte prüfen
    if args.download and args.verify_only:
        log("--download und --verify-only schließen sich aus", "error")
        return 2

    if args.mode == "object-store" and not args.download:
        log("--mode object-store macht nur mit --download Sinn", "error")
        return 2

    if args.mode == "object-store" and (not args.local_objects_root or not args.local_snapshots_root):
        log("--mode object-store benötigt --local-objects-root und --local-snapshots-root", "error")
        return 2

    if args.mode == "flat" and not args.out_dir:
        log("--mode flat benötigt --out-dir", "error")
        return 2
    
    # Config laden
    cfg = pc.effective_config(
        env_file=args.env_file,
        env_dir=args.env_dir,
        profile=args.profile,
        overrides={
            "host": args.host,
            "port": args.port,
            "timeout": args.timeout,
            "device": args.device,
            "token": args.token
        }
    )
    
    # Index / Manifest laden
    try:
        if _direct_mode_active:
            # === NEU: Direct-Download-Modus ===
            if args.folder:
                items = load_direct_folder(cfg, folder_path=args.folder)
            elif args.folderid:
                items = load_direct_folder(cfg, folderid=args.folderid)
            elif args.file:
                items = load_direct_file(cfg, file_path=args.file)
            elif args.fileid:
                items = load_direct_file(cfg, fileid=args.fileid)
        elif args.manifest.lower() == "pcloud":
            items = load_index_from_pcloud(cfg, args.dest_root, args.snapshot)
        else:
            log(f"Lade lokales Manifest: {args.manifest}")
            manifest = load_manifest(args.manifest, args.snapshot)
            items = manifest.get("items", [])
    except (IndexLoadError, ManifestLoadError) as e:
        log(str(e), "error")
        return 2
    except Exception as e:
        log(f"Fehler beim Laden: {e}", "error")
        return 2
    
    # Filtern
    sel = [it for it in items if not args.filter or it.get("relpath", "").startswith(args.filter)]
    
    # Display Info
    if _direct_mode_active:
        mode_desc = "Direct-Download"
        if args.folder:
            mode_desc += f": {args.folder}"
        elif args.folderid:
            mode_desc += f": FolderID={args.folderid}"
        elif args.file:
            mode_desc += f": {args.file}"
        elif args.fileid:
            mode_desc += f": FileID={args.fileid}"
        log(mode_desc)
    else:
        log(f"Snapshot: {args.snapshot} @ {args.dest_root}")
    
    log(f"Items (nach Filter): {len(sel)}")
    
    if not sel:
        log("Keine Items", "warn")
        return 0

    # Verify-only-Modus
    if args.verify_only:
        log("Starte Verify-only (keine Downloads)...")
        # Base-Zielpfad: out_dir/snapshot ODER out_dir (Direct-Mode)
        if _direct_mode_active:
            base_out_dir = args.out_dir
        else:
            base_out_dir = os.path.join(args.out_dir, args.snapshot)
        
        stats = verify_files(base_out_dir, sel)
        log("=" * 60)
        log("Verify-only abgeschlossen:")
        log(f"  ✓ OK:         {stats['verified']}")
        log(f"  ✗ Mismatches: {stats['mismatches']}")
        log(f"  ⚠ Fehler:     {stats['errors']}")
        return 0 if stats["mismatches"] == 0 and stats["errors"] == 0 else 1

    # Plan-Modus (nur anzeigen, was passieren würde)
    if not args.download:
        log("Plan-Modus (keine Downloads, nur Vorschau):")
        for it in sel[:10]:
            relpath = it.get("relpath") or "<unknown>"
            sha_raw = it.get("sha256")
            sha_preview = sha_raw[:8] if isinstance(sha_raw, str) and sha_raw else "?"
            size_str = f", {it['size']:,} B" if it.get("size") is not None else ""
            print(f"  {relpath} [{sha_preview}]{size_str}")
        if len(sel) > 10:
            print(f"  ... ({len(sel) - 10} weitere)")
        known_sizes = [it["size"] for it in sel if it.get("size") is not None]
        if known_sizes:
            total_bytes = sum(known_sizes)
            total_mb = total_bytes / (1024 * 1024)
            log(f"Gesamt: {len(sel)} Dateien, ~{total_mb:.1f} MB ({total_bytes:,} Bytes)")
        else:
            log(f"Gesamt: {len(sel)} Dateien (Größe nicht im Index verfügbar)")
        return 0
    
    # Echtes Restore
    log(f"Starte Download: {len(sel)} Dateien von pCloud...")

    # Flat-Modus: direkt in base_out_dir/snapshot/relpath schreiben (oder Direct-Mode)
    if args.mode == "flat":
        # Base-Zielpfad: out_dir/snapshot ODER out_dir (Direct-Mode)
        if _direct_mode_active:
            base_out_dir = args.out_dir
        else:
            base_out_dir = os.path.join(args.out_dir, args.snapshot)
        
        os.makedirs(base_out_dir, exist_ok=True)

        stats = {"success": 0, "failed": 0, "skipped": 0, "downloaded": 0}
        sha_cache = {}  # Deduplizierung: {sha256 → local_path}
        _state_lock = threading.Lock()  # Thread-Safe Stats
        
        # === Klassifizierung: Small vs Large Files ===
        small_files = [f for f in sel if f.get("size", 0) < SMALL_FILE_THRESHOLD_BYTES]
        large_files = [f for f in sel if f.get("size", 0) >= SMALL_FILE_THRESHOLD_BYTES]
        
        log(f"Klassifizierung: {len(small_files)} kleine (<50MB), {len(large_files)} große Dateien")
        
        # Progress-Tracking
        start_time = time.time()
        total_items = len(sel)
        total_bytes = sum([f.get("size", 0) for f in sel])
        done_items = 0
        done_bytes = 0
        _t_last_progress = start_time
        _PROGRESS_INTERVAL = 5.0  # Sekunden
        
        def _log_progress(force: bool = False):
            """Progress + ETA ausgeben (intervall-basiert)"""
            nonlocal done_items, done_bytes, _t_last_progress
            _now = time.time()
            if not force and (_now - _t_last_progress < _PROGRESS_INTERVAL):
                return
            
            elapsed = _now - start_time
            pct_items = (done_items / total_items * 100) if total_items else 0
            pct_bytes = (done_bytes / total_bytes * 100) if total_bytes else 0
            
            # Timestamp wie in pcloud_push
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if done_bytes > 0 and elapsed > 0:
                speed_mbps = (done_bytes / (1024 * 1024)) / elapsed
                eta_sec = (total_bytes - done_bytes) / (done_bytes / elapsed) if done_bytes > 0 else 0
                eta_str = f"~{int(eta_sec // 60)}min" if eta_sec > 60 else f"~{int(eta_sec)}s"
                print(
                    f"{ts} [restore] {done_items}/{total_items} ({pct_items:.0f}%) | "
                    f"{done_bytes / (1024**3):.2f}/{total_bytes / (1024**3):.2f} GB ({pct_bytes:.0f}%) | "
                    f"downloaded={stats['downloaded']} skipped={stats['skipped']} failed={stats['failed']} | "
                    f"{speed_mbps:.1f} MB/s | {eta_str} verbleibend",
                    flush=True
                )
            else:
                print(f"{ts} [restore] {done_items}/{total_items} ({pct_items:.0f}%)", flush=True)
            
            _t_last_progress = _now
        
        def _process_download_item(item_tuple: tuple) -> dict:
            """
            Thread-Safe Download einer Datei.
            
            Args:
                item_tuple: (idx, item)
            
            Returns:
                {"success": bool, "size": int, "action": str}
            """
            nonlocal stats, sha_cache, done_items, done_bytes
            
            idx, item = item_tuple
            relpath = item.get("relpath", f"?_{idx}")
            sha256 = item.get("sha256")
            fileid = item.get("fileid")
            anchor_path = item.get("anchor_path")
            file_size = item.get("size", 0)
            local_dest = os.path.join(base_out_dir, relpath)
            
            result = {"success": False, "size": file_size, "action": "skipped"}
            
            # Path-Traversal-Guard
            expected_prefix = os.path.join(base_out_dir) + os.sep
            normalized_local_dest = os.path.normpath(local_dest)
            if not normalized_local_dest.startswith(expected_prefix):
                log(f"  [{idx}/{total_items}] ✗ Path-Traversal verhindert: {relpath}", "error")
                with _state_lock:
                    stats["failed"] += 1
                return result
            
            # Deduplizierung: SHA-Cache prüfen
            if sha256 and sha256 in sha_cache:
                cached_src = sha_cache[sha256]
                if cached_src != local_dest and not os.path.exists(local_dest):
                    os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
                    try:
                        os.link(cached_src, local_dest)
                        result["action"] = "hardlink"
                    except OSError:
                        import shutil
                        shutil.copy2(cached_src, local_dest)
                        result["action"] = "copy"
                    result["success"] = True
                    with _state_lock:
                        stats["success"] += 1
                        stats["skipped"] += 1
                        done_items += 1
                        done_bytes += file_size
                    return result
                elif os.path.exists(local_dest):
                    result["success"] = True
                    with _state_lock:
                        stats["skipped"] += 1
                        done_items += 1
                        done_bytes += file_size
                    return result
            
            # Lokale Datei bereits vorhanden? → SHA prüfen (Snapshot-Mode)
            if os.path.exists(local_dest) and sha256 and args.verify:
                try:
                    hash_obj = hashlib.sha256()
                    with open(local_dest, "rb") as f:
                        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                            hash_obj.update(chunk)
                    local_sha = hash_obj.hexdigest()
                    if local_sha.lower() == sha256.lower():
                        result["success"] = True
                        with _state_lock:
                            stats["skipped"] += 1
                            sha_cache[sha256] = local_dest
                            done_items += 1
                            done_bytes += file_size
                        return result
                except Exception:
                    pass  # Falls Prüfung fehlschlägt, neu downloaden
            
            # === NEU: Size-Check Resume (Direct-Mode ohne SHA) ===
            if os.path.exists(local_dest) and not sha256:
                try:
                    local_size = os.path.getsize(local_dest)
                    if local_size == file_size:
                        # Größe stimmt überein → Datei vermutlich komplett
                        result["success"] = True
                        result["action"] = "size-match"
                        with _state_lock:
                            stats["skipped"] += 1
                            done_items += 1
                            done_bytes += file_size
                        return result
                except Exception:
                    pass  # Falls Check fehlschlägt, neu downloaden
            
            # Verzeichnis erstellen
            os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
            
            # Download durchführen
            verify_hash = sha256 if args.verify else None
            
            # Warnung wenn Verifikation gewünscht aber nicht möglich (Direct-Mode)
            if args.verify and not sha256:
                log(f"  [{idx}/{total_items}] ⚠ Verifikation nicht möglich (kein SHA für Live-Datei): {relpath}", "warn")
            
            downloaded = False
            
            if anchor_path:
                downloaded = download_file_with_verify(cfg, anchor_path, local_dest, verify_hash)
            
            if not downloaded and fileid:
                downloaded = download_via_fileid(cfg, fileid, local_dest, verify_hash)
            
            if downloaded:
                result["success"] = True
                result["action"] = "download"
                with _state_lock:
                    if sha256:
                        sha_cache[sha256] = local_dest
                    stats["success"] += 1
                    stats["downloaded"] += 1
                    done_items += 1
                    done_bytes += file_size
            else:
                with _state_lock:
                    stats["failed"] += 1
                    done_items += 1
            
            return result
        
        # === PARALLEL: Kleine Dateien ===
        if small_files:
            log(f"[parallel] Starte Download von {len(small_files)} kleinen Dateien ({PARALLEL_DOWNLOAD_THREADS} Threads)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOAD_THREADS) as executor:
                # Enumerate mit Index
                indexed_small = [(i+1, f) for i, f in enumerate(small_files)]
                futures = [executor.submit(_process_download_item, item) for item in indexed_small]
                
                # Progress-Logging intervall-basiert
                for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                    try:
                        future.result()  # Exception handling
                    except Exception as e:
                        log(f"Download-Fehler: {e}", "error")
                    
                    # Progress-Logging intervall-basiert
                    _log_progress()
            
            log(f"[parallel] {len(small_files)} kleine Dateien abgeschlossen")
        
        # === SEQUENTIAL: Große Dateien ===
        if large_files:
            log(f"[sequential] Starte Download von {len(large_files)} großen Dateien (>= 50MB)...")
            base_idx = len(small_files) + 1
            for i, large_file in enumerate(large_files):
                idx = base_idx + i
                log(f"[{idx}/{total_items}] {large_file.get('relpath')} ({large_file.get('size', 0):,} bytes)")
                try:
                    _process_download_item((idx, large_file))
                except Exception as e:
                    log(f"Download-Fehler: {e}", "error")
                    with _state_lock:
                        stats["failed"] += 1
                
                _log_progress()
        
        # Final Progress
        _log_progress(force=True)

        log("=" * 60)
        log(f"Restore abgeschlossen (flat-Modus):")
        log(f"  ✓ Erfolgreich:    {stats['success']}")
        log(f"  ↓ Downloaded:     {stats['downloaded']}")
        log(f"  ⊘ Dedupliziert:   {stats['skipped']}")
        log(f"  ✗ Fehler:         {stats['failed']}")

        return 0 if stats["failed"] == 0 else 1

    # Object-Store-Modus: _objects + _snapshots mit Hardlinks
    objects_root = os.path.join(args.local_objects_root)
    snaps_root = os.path.join(args.local_snapshots_root)

    os.makedirs(objects_root, exist_ok=True)
    os.makedirs(snaps_root, exist_ok=True)

    stats = {"success": 0, "failed": 0, "skipped": 0, "hardlinks": 0, "objects": 0}
    existing_objects = set()  # merkt sich bereits vorhandene SHA-Objekte in diesem Lauf

    for idx, item in enumerate(sel, 1):
        relpath = item.get("relpath", f"?_{idx}")
        sha256 = item.get("sha256")
        anchor_path = item.get("anchor_path")
        fileid = item.get("fileid")
        snapshot_name = args.snapshot

        if not sha256:
            log(f"[{idx}/{len(sel)}] {relpath}: kein SHA256 im Index, übersprungen", "warn")
            stats["skipped"] += 1
            continue

        log(f"[{idx}/{len(sel)}] {relpath} (SHA={sha256[:8]})")

        # Pfad im Object-Store
        obj_dir = os.path.join(objects_root, sha256[:2])
        obj_path = os.path.join(obj_dir, sha256)

        # Falls Objektdatei noch nicht existiert: aus pCloud holen
        if not os.path.exists(obj_path):
            os.makedirs(obj_dir, exist_ok=True)
            verify_hash = sha256 if args.verify else None

            def _download_object() -> bool:
                if anchor_path:
                    log(f"  → Objekt fehlt lokal, lade nach {obj_path}...")
                    if download_file_with_verify(cfg, anchor_path, obj_path, verify_hash):
                        return True
                    log("  → anchor_path fehlgeschlagen, versuche fileid...", "warn")
                if fileid:
                    return download_via_fileid(cfg, fileid, obj_path, verify_hash)
                log("  ✗ Kein anchor_path und keine fileid vorhanden (Object-Store)", "error")
                return False

            if _download_object():
                stats["objects"] += 1
            else:
                log("  ✗ Download ins Object-Store fehlgeschlagen", "error")
                try:
                    if os.path.exists(obj_path):
                        os.remove(obj_path)
                except Exception:
                    pass
                stats["failed"] += 1
                continue
        else:
            if sha256 not in existing_objects:
                log("  → Objekt bereits im Object-Store vorhanden, verwende es erneut")
                existing_objects.add(sha256)

        # Snapshot-Datei als Hardlink anlegen
        snap_dir = os.path.join(snaps_root, snapshot_name, os.path.dirname(relpath))
        snap_file = os.path.join(snaps_root, snapshot_name, relpath)

        # Path-Traversal-Guard: sicherstellen, dass snap_file unterhalb von snaps_root/snapshot_name liegt
        expected_prefix = os.path.join(snaps_root, snapshot_name) + os.sep
        normalized_snap_file = os.path.normpath(snap_file)
        if not normalized_snap_file.startswith(expected_prefix):
            log(f"  ✗ Ungültiger relpath (Path-Traversal verhindert): {relpath}", "error")
            stats["failed"] += 1
            continue

        os.makedirs(snap_dir or snaps_root, exist_ok=True)

        if os.path.exists(snap_file):
            # Bereits existierende Datei belassen, optional könnte man hier noch SHA prüfen
            log("  → Snapshot-Datei existiert bereits, übersprungen")
            stats["skipped"] += 1
            continue

        try:
            os.link(obj_path, snap_file)
            log("  → Hardlink erstellt")
            stats["hardlinks"] += 1
            stats["success"] += 1
        except OSError as e:
            log(f"  ✗ Hardlink fehlgeschlagen ({e}), versuche Kopie...", "warn")
            try:
                import shutil
                shutil.copy2(obj_path, snap_file)
                log("  → Kopie erstellt (Fallback)")
                stats["success"] += 1
            except Exception as e2:
                log(f"  ✗ Kopie ebenfalls fehlgeschlagen: {e2}", "error")
                stats["failed"] += 1

    log("=" * 60)
    log(f"Restore abgeschlossen (object-store-Modus):")
    log(f"  ✓ Erfolgreich (Snap-Files): {stats['success']}")
    log(f"  ⊘ Übersprungen:            {stats['skipped']}")
    log(f"  ⊕ Neue Objekte:           {stats['objects']}")
    log(f"  ⊕ Hardlinks:              {stats['hardlinks']}")
    log(f"  ✗ Fehler:                 {stats['failed']}")

    return 0 if stats["failed"] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
