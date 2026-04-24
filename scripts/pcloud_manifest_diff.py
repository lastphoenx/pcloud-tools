#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_manifest_diff.py — Manifest-Vergleich für Delta-Copy

Vergleicht zwei Manifeste (aktuell vs. Referenz) und erzeugt strukturierten Diff.

Usage:
    python pcloud_manifest_diff.py \
        --current /path/to/new_manifest.json \
        --reference /path/to/old_manifest.json \
        --out /tmp/diff.json

Output:
    {
        "identical": [items],     # Pfad, Hash, Mtime gleich
        "new": [items],           # In current, nicht in reference
        "changed": [items],       # Pfad gleich, aber Hash/Mtime anders
        "deleted": [items],       # In reference, nicht in current
        "new_dirs": [relpaths],   # Neue Ordner
        "deleted_dirs": [relpaths], # Gelöschte Ordner
        "stats": {...}
    }
"""

import json
import argparse
import sys
from typing import Dict, List, Any


def compare_manifests(current_path: str, reference_path: str) -> Dict[str, Any]:
    """
    Vergleicht zwei Manifest-Dateien und erzeugt strukturierten Diff.
    
    Kategorisierung:
        - identical: Pfad, SHA256, mtime gleich → nichts tun
        - new: Pfad existiert nur in current → Upload/Stub
        - changed: Pfad existiert, aber SHA256 oder mtime anders → DELETE + WRITE
        - deleted: Pfad existiert nur in reference → DELETE
    
    Args:
        current_path: Pfad zum neuen Manifest
        reference_path: Pfad zum Referenz-Manifest
    
    Returns:
        Diff-Dictionary mit kategorisierten Items
    """
    # Manifeste laden
    with open(current_path, 'r', encoding='utf-8') as f:
        current = json.load(f)
    
    with open(reference_path, 'r', encoding='utf-8') as f:
        reference = json.load(f)
    
    # Indizes aufbauen (relpath → item)
    current_files = {
        it["relpath"]: it 
        for it in current.get("items", []) 
        if it.get("type") == "file"
    }
    
    reference_files = {
        it["relpath"]: it 
        for it in reference.get("items", []) 
        if it.get("type") == "file"
    }
    
    current_dirs = {
        it["relpath"] 
        for it in current.get("items", []) 
        if it.get("type") == "dir" and it.get("relpath")  # Filter Root ("")
    }
    
    reference_dirs = {
        it["relpath"] 
        for it in reference.get("items", []) 
        if it.get("type") == "dir" and it.get("relpath")
    }
    
    # Kategorisierung
    identical = []
    new = []
    changed = []
    deleted = []
    
    current_paths = set(current_files.keys())
    reference_paths = set(reference_files.keys())
    
    # 1. Neue Dateien (nur in current)
    new_paths = current_paths - reference_paths
    for relpath in new_paths:
        new.append(current_files[relpath])
    
    # 2. Gelöschte Dateien (nur in reference)
    deleted_paths = reference_paths - current_paths
    for relpath in deleted_paths:
        deleted.append(reference_files[relpath])
    
    # 3. Existierende Dateien: Identisch vs. Geändert
    common_paths = current_paths & reference_paths
    for relpath in common_paths:
        curr_item = current_files[relpath]
        ref_item = reference_files[relpath]
        
        curr_sha = (curr_item.get("sha256") or "").lower()
        ref_sha = (ref_item.get("sha256") or "").lower()
        curr_mtime = curr_item.get("mtime")
        ref_mtime = ref_item.get("mtime")
        
        # Identisch: SHA256 UND mtime gleich
        if curr_sha == ref_sha and curr_mtime == ref_mtime:
            identical.append(curr_item)
        else:
            changed.append(curr_item)
    
    # 4. Ordner-Diff
    new_dirs = sorted(current_dirs - reference_dirs)
    deleted_dirs = sorted(reference_dirs - current_dirs)
    
    # Statistiken
    stats = {
        "current_snapshot": current.get("snapshot", "unknown"),
        "reference_snapshot": reference.get("snapshot", "unknown"),
        "current_files": len(current_files),
        "reference_files": len(reference_files),
        "identical_count": len(identical),
        "new_count": len(new),
        "changed_count": len(changed),
        "deleted_count": len(deleted),
        "new_dirs_count": len(new_dirs),
        "deleted_dirs_count": len(deleted_dirs),
    }
    
    return {
        "identical": identical,
        "new": new,
        "changed": changed,
        "deleted": deleted,
        "new_dirs": new_dirs,
        "deleted_dirs": deleted_dirs,
        "stats": stats,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Manifest-Vergleich für Delta-Copy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "--current", 
        required=True,
        help="Pfad zum aktuellen/neuen Manifest (z.B. 2026-04-16.json)"
    )
    
    parser.add_argument(
        "--reference", 
        required=True,
        help="Pfad zum Referenz-Manifest (z.B. 2026-04-15.json)"
    )
    
    parser.add_argument(
        "--out",
        help="Ausgabedatei für Diff (JSON). Default: stdout"
    )
    
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Nur Statistiken ausgeben (keine vollständigen Item-Listen)"
    )
    
    args = parser.parse_args()
    
    # Diff berechnen
    try:
        diff = compare_manifests(args.current, args.reference)
    except FileNotFoundError as e:
        print(f"Error: Manifest nicht gefunden: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Ungültiges JSON: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Ausgabe
    stats = diff["stats"]
    
    print(f"Manifest-Diff: {stats['reference_snapshot']} → {stats['current_snapshot']}")
    print(f"{'='*60}")
    print(f"  Identical:     {stats['identical_count']:>6} (keine Aktion)")
    print(f"  New:           {stats['new_count']:>6} (Upload/Stub)")
    print(f"  Changed:       {stats['changed_count']:>6} (DELETE + WRITE)")
    print(f"  Deleted:       {stats['deleted_count']:>6} (DELETE)")
    print(f"{'='*60}")
    print(f"  New Dirs:      {stats['new_dirs_count']:>6}")
    print(f"  Deleted Dirs:  {stats['deleted_dirs_count']:>6}")
    print(f"{'='*60}")
    print(f"  TOTAL Aktionen: {stats['new_count'] + stats['changed_count'] + stats['deleted_count']:>6} API-Calls")
    print()
    
    # Vollständiger Diff speichern (falls --out)
    if args.out:
        output = diff if not args.stats_only else {"stats": stats}
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"✓ Diff gespeichert: {args.out}")
    elif not args.stats_only:
        # stdout-Ausgabe
        json.dump(diff, sys.stdout, indent=2, ensure_ascii=False)
        print()


if __name__ == "__main__":
    main()
