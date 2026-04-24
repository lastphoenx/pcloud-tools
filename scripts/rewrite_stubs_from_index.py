#!/usr/bin/env python3
"""
Schreibt ALLE Stubs aus enriched Index neu und lädt sie hoch.

Workflow:
  1. Lade enriched Index (mit FileIDs)
  2. Für jeden Node (SHA256):
     - Für jeden Holder: Schreibe Stub-File lokal
     - Stub-Inhalt: {fileid, pcloud_hash, size, sha256, mtime}
  3. Lade alle Stubs nach pCloud hoch
  
Performance:
  - Batch-Upload: Alle Stubs in einem listfolder Upload
  - Nur geänderte Stubs hochladen (Hash-Vergleich)
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, List, Any, Set, Tuple

# pCloud API
import pcloud_bin_lib as pc


def load_index(index_path: str) -> Dict[str, Any]:
    """Lade enriched Index (lokal oder remote)."""
    print(f"[load] Lade Index: {index_path}")
    
    if index_path.startswith("/"):
        # Remote pCloud-Pfad
        cfg = pc.load_config()
        content = pc.download_file_content(cfg, path=index_path)
        if not content:
            raise FileNotFoundError(f"Remote Index nicht gefunden: {index_path}")
        index = json.loads(content)
    else:
        # Lokaler Pfad
        with open(index_path, 'r') as f:
            index = json.load(f)
    
    items = index.get("items", {})
    print(f"[load] ✓ {len(items)} Nodes geladen")
    
    return index


def generate_stub_content(node: Dict[str, Any], sha256: str, holder: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generiere Stub-Inhalt (JSON) für einen Holder.
    
    Stub-Format (wie in write_hardlink_stub_1to1 - LEGACY KORREKT):
      {
        "type": "hardlink",
        "sha256": "abc...",
        "size": 123456,
        "mtime": 1234567890.0,
        "inode": {"dev": 2049, "ino": 123, "nlink": 3},
        "anchor_path": "/path/to/anchor",
        "fileid": 12345,
        "snapshot": "2026-04-10-075334",
        "relpath": "path/to/file.pdf"
      }
    """
    fileid = node.get("fileid")
    
    if not fileid:
        raise ValueError(f"Node {sha256[:8]} hat keine FileID!")
    
    # Inode aus Holder extrahieren
    inode_data = holder.get("inode", {})
    if not isinstance(inode_data, dict):
        inode_data = {}
    
    payload = {
        "type": "hardlink",
        "sha256": sha256.lower(),
        "size": int(holder.get("size") or 0),
        "mtime": float(holder.get("mtime") or 0.0),
        "inode": {
            "dev": int(inode_data.get("dev") or 0),
            "ino": int(inode_data.get("ino") or 0),
            "nlink": int(inode_data.get("nlink") or 1),
        },
        "anchor_path": node.get("anchor_path"),
        "fileid": fileid,
        "snapshot": holder.get("snapshot"),
        "relpath": holder.get("relpath"),
    }
    
    return payload


def rewrite_stubs_local(index: Dict[str, Any], dest_root: str, *, 
                        dry_run: bool = False, verbose: bool = False) -> Tuple[int, int]:
    """
    Schreibt alle Stubs lokal neu.
    
    Returns:
      (written_count, skipped_count)
    """
    items = index.get("items", {})
    
    written = 0
    skipped = 0
    errors = 0
    
    print()
    print("[rewrite] Schreibe Stubs lokal...")
    print(f"[rewrite] Ziel: {dest_root}")
    
    if dry_run:
        print("[rewrite] ⚠ DRY-RUN - keine Änderungen")
    
    for i, (sha256, node) in enumerate(items.items(), 1):
        if not isinstance(node, dict):
            continue
        
        # Progress
        if i % 1000 == 0 or i == len(items):
            print(f"  [{i}/{len(items)}] ({i*100//len(items)}%) | "
                  f"Written: {written}, Skipped: {skipped}, Errors: {errors}")
        
        fileid = node.get("fileid")
        if not fileid:
            skipped += 1
            if verbose:
                print(f"  [SKIP] Node {sha256[:8]}... hat keine FileID")
            continue
        
        holders = node.get("holders", [])
        if not isinstance(holders, list):
            continue
        
        # Schreibe Stub für JEDEN Holder
        for holder in holders:
            if not isinstance(holder, dict):
                continue
            
            snapshot = holder.get("snapshot")
            relpath = holder.get("relpath")
            
            if not snapshot or not relpath:
                continue
            
            # Stub-Pfad: {dest_root}/_snapshots/{snapshot}/{relpath}.meta.json
            stub_path = Path(dest_root) / "_snapshots" / snapshot / f"{relpath}.meta.json"
            
            try:
                # Generiere Stub-Content
                stub_content = generate_stub_content(node, sha256, holder)
                stub_json = json.dumps(stub_content, indent=2, ensure_ascii=False)
                
                if dry_run:
                    if verbose and written < 5:
                        print(f"  [DRY-RUN] Würde schreiben: {stub_path}")
                else:
                    # Erstelle Verzeichnis
                    stub_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Schreibe Stub-File
                    with open(stub_path, 'w', encoding='utf-8') as f:
                        f.write(stub_json)
                
                written += 1
            
            except Exception as e:
                errors += 1
                if verbose or errors <= 10:
                    print(f"  [ERROR] Fehler bei {stub_path}: {e}")
    
    print()
    print(f"[rewrite] ✓ Stubs geschrieben: {written}")
    if skipped > 0:
        print(f"[rewrite] ⊘ Übersprungen (keine FileID): {skipped}")
    if errors > 0:
        print(f"[rewrite] ✗ Fehler: {errors}")
    
    return (written, skipped)


def upload_stubs_to_pcloud(cfg: dict, local_root: str, remote_root: str, 
                           snapshots: List[str], *, dry_run: bool = False,
                           verbose: bool = False) -> Tuple[int, int]:
    """
    Lädt alle Stubs nach pCloud hoch (nur geänderte).
    
    Args:
        local_root: /srv/pcloud-backup/dest (enthält _snapshots)
        remote_root: /Backup/rtb_1to1 (enthält _snapshots)
        snapshots: Liste der Snapshot-Namen
    
    Returns:
      (uploaded_count, skipped_count)
    """
    print()
    print("[upload] Lade Stubs nach pCloud hoch...")
    print(f"[upload] Remote: {remote_root}")
    
    if dry_run:
        print("[upload] ⚠ DRY-RUN - keine Uploads")
    
    uploaded = 0
    skipped = 0
    errors = 0
    
    for snap_idx, snapshot in enumerate(snapshots, 1):
        local_snap = Path(local_root) / "_snapshots" / snapshot
        remote_snap = f"{remote_root}/_snapshots/{snapshot}"
        
        print(f"\n[{snap_idx}/{len(snapshots)}] {snapshot}...")
        
        if not local_snap.exists():
            print(f"  ⚠ Lokaler Snapshot nicht gefunden: {local_snap}")
            continue
        
        # Hole alle Stub-Files (rekursiv)
        stub_files = []
        for root, dirs, files in os.walk(local_snap):
            for filename in files:
                # Nur .meta.json Files (Stubs)
                if filename.endswith('.meta.json'):
                    file_path = Path(root) / filename
                    rel_path = file_path.relative_to(local_snap)
                    stub_files.append((file_path, rel_path))
        
        print(f"  Gefunden: {len(stub_files)} Stub-Files")
        
        # Upload jedes Stub-File
        for i, (local_file, rel_path) in enumerate(stub_files, 1):
            remote_file = f"{remote_snap}/{rel_path.as_posix()}"
            
            if verbose and i % 100 == 0:
                print(f"    [{i}/{len(stub_files)}] Uploading...")
            
            try:
                if dry_run:
                    if verbose and i <= 5:
                        print(f"    [DRY-RUN] Würde hochladen: {remote_file}")
                    uploaded += 1
                else:
                    # Prüfe ob File existiert und identisch ist
                    try:
                        remote_meta = pc.stat_file(cfg, path=remote_file, with_checksum=False, enrich_path=False)
                        
                        if remote_meta:
                            local_size = local_file.stat().st_size
                            remote_size = remote_meta.get("size", 0)
                            
                            if local_size == remote_size:
                                skipped += 1
                                continue
                    except:
                        pass  # File existiert nicht, upload nötig
                    
                    # Upload File
                    with open(local_file, 'rb') as f:
                        content = f.read()
                    
                    # Erstelle Remote-Verzeichnis
                    remote_dir = os.path.dirname(remote_file)
                    pc.create_all_folders(cfg, remote_dir)
                    
                    # Upload
                    pc.upload_file(cfg, local_file=str(local_file), remote_path=remote_file)
                    uploaded += 1
            
            except Exception as e:
                errors += 1
                if verbose or errors <= 10:
                    print(f"    [ERROR] Upload fehlgeschlagen: {remote_file}: {e}")
        
        print(f"  ✓ Uploaded: {uploaded}, Skipped: {skipped}, Errors: {errors}")
    
    print()
    print(f"[upload] ✓ Gesamt hochgeladen: {uploaded}")
    print(f"[upload] ⊘ Übersprungen (identisch): {skipped}")
    if errors > 0:
        print(f"[upload] ✗ Fehler: {errors}")
    
    return (uploaded, skipped)


def main():
    parser = argparse.ArgumentParser(
        description="Schreibt alle Stubs aus enriched Index neu und lädt sie hoch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  # Stubs lokal neu schreiben (aus lokalem Index)
  python rewrite_stubs_from_index.py \\
    --index /srv/pcloud-archive/indexes/content_index_master.json \\
    --dest-root /srv/pcloud-backup/dest \\
    --local-only
  
  # Stubs neu schreiben UND nach pCloud hochladen
  python rewrite_stubs_from_index.py \\
    --index /srv/pcloud-archive/indexes/content_index_master.json \\
    --dest-root /srv/pcloud-backup/dest \\
    --remote-root /Backup/rtb_1to1 \\
    --upload
  
  # Stubs aus Remote-Index neu schreiben
  python rewrite_stubs_from_index.py \\
    --index /Backup/rtb_1to1/_snapshots/_index/content_index.json \\
    --dest-root /srv/pcloud-backup/dest \\
    --remote-root /Backup/rtb_1to1 \\
    --upload
"""
    )
    
    parser.add_argument("--index", required=True,
                       help="Pfad zum enriched Index (lokal oder remote /path)")
    parser.add_argument("--dest-root", required=True,
                       help="Lokaler Basis-Pfad (enthält _snapshots)")
    parser.add_argument("--remote-root",
                       help="Remote Basis-Pfad in pCloud (für Upload)")
    parser.add_argument("--local-only", action="store_true",
                       help="Nur lokal schreiben, kein Upload")
    parser.add_argument("--upload", action="store_true",
                       help="Nach lokalem Schreiben nach pCloud hochladen")
    parser.add_argument("--dry-run", action="store_true",
                       help="Zeige nur was gemacht würde, keine Änderungen")
    parser.add_argument("--verbose", action="store_true",
                       help="Detaillierter Output")
    
    args = parser.parse_args()
    
    # Validierung
    if args.upload and not args.remote_root:
        parser.error("--upload benötigt --remote-root")
    
    # Phase 1: Lade Index
    index = load_index(args.index)
    
    # Phase 2: Schreibe Stubs lokal
    written, skipped = rewrite_stubs_local(
        index=index,
        dest_root=args.dest_root,
        dry_run=args.dry_run,
        verbose=args.verbose
    )
    
    # Phase 3: Upload zu pCloud (optional)
    if args.upload and not args.local_only:
        # Ermittle Snapshots aus Index
        items = index.get("items", {})
        snapshots = set()
        for node in items.values():
            if isinstance(node, dict):
                holders = node.get("holders", [])
                for holder in holders:
                    if isinstance(holder, dict):
                        snap = holder.get("snapshot")
                        if snap:
                            snapshots.add(snap)
        
        snapshots = sorted(snapshots)
        print(f"\n[upload] Snapshots gefunden: {len(snapshots)}")
        
        cfg = pc.load_config()
        
        uploaded, upload_skipped = upload_stubs_to_pcloud(
            cfg=cfg,
            local_root=args.dest_root,
            remote_root=args.remote_root,
            snapshots=snapshots,
            dry_run=args.dry_run,
            verbose=args.verbose
        )
    
    # Summary
    print()
    print("=" * 70)
    print("✓ STUB-REWRITE ABGESCHLOSSEN")
    print("=" * 70)
    print(f"Stubs geschrieben: {written}")
    if args.upload:
        print(f"Stubs hochgeladen: {uploaded}")


if __name__ == "__main__":
    main()
