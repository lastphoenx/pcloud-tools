#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Debug-Skript: Vergleicht Remote-Ordner mit Manifest-Ordnern
"""
import sys, json, argparse
import pcloud_bin_lib as pc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--env-file")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    
    with open(args.manifest, 'r') as f:
        manifest = json.load(f)
    
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(args.dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    
    # 1. Remote-Ordner sammeln
    print(f"[1] Lade Remote-Ordner von: {dest_snapshot_dir}")
    remote_folders = set()
    try:
        result = pc.listfolder(cfg, path=dest_snapshot_dir, recursive=True, nofiles=True)
        
        def _collect_folders(obj, parent_path=""):
            if isinstance(obj, dict) and obj.get("isfolder"):
                folder_name = obj.get("name", "")
                folder_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                remote_folders.add(folder_path)
                for child in obj.get("contents") or []:
                    _collect_folders(child, folder_path)
        
        # Direkt mit contents starten
        metadata = result.get("metadata") or {}
        for child in metadata.get("contents") or []:
            _collect_folders(child, "")
        
        print(f"    → {len(remote_folders)} Remote-Ordner gefunden")
    except Exception as e:
        print(f"    → Fehler: {e}")
    
    # 2. Manifest-Ordner sammeln
    print(f"\n[2] Sammle Manifest-Ordner")
    manifest_folders = set()
    for it in manifest.get("items") or []:
        if it.get("type") == "dir":
            relpath = it.get("relpath", "").rstrip("/")
            if relpath:  # Filter leere Strings (Root-Verzeichnis)
                manifest_folders.add(relpath)
    print(f"    → {len(manifest_folders)} Manifest-Ordner")
    
    # 3. Differenz berechnen
    missing = manifest_folders - remote_folders
    extra = remote_folders - manifest_folders
    
    print(f"\n[3] Analyse:")
    print(f"    Remote: {len(remote_folders)}")
    print(f"    Manifest: {len(manifest_folders)}")
    print(f"    Fehlend (in Manifest, nicht Remote): {len(missing)}")
    print(f"    Extra (Remote, nicht in Manifest): {len(extra)}")
    
    if missing:
        print(f"\n[4] Erste 20 fehlende Ordner:")
        for i, folder in enumerate(sorted(missing)[:20], 1):
            print(f"    {i}. {folder}")
        if len(missing) > 20:
            print(f"    ... und {len(missing) - 20} weitere")
    
    if extra:
        print(f"\n[5] Erste 20 Extra-Ordner (Remote aber nicht in Manifest):")
        for i, folder in enumerate(sorted(extra)[:20], 1):
            print(f"    {i}. {folder}")
        if len(extra) > 20:
            print(f"    ... und {len(extra) - 20} weitere")
    
    # 6. Sample-Vergleich
    print(f"\n[6] Sample-Check (erste 10 Manifest-Ordner):")
    for folder in sorted(manifest_folders)[:10]:
        status = "✓ EXISTS" if folder in remote_folders else "✗ MISSING"
        print(f"    {status}: {folder}")

if __name__ == "__main__":
    main()
