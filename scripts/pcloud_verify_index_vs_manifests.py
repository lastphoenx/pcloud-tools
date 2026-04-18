#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_verify_index_vs_manifests.py — Verifiziert Remote-Index gegen lokale Manifeste.

Prüft die Konsistenz zwischen:
  - Remote content_index.json (auf pCloud)
  - Lokale Manifest-JSONs (Ground Truth vom Backup-Source)

Checks:
  1. Alle Manifest-Dateien im Index vorhanden?
  2. Alle Index-Holder haben entsprechende Manifest-Einträge?
  3. SHA256-Hashes stimmen überein?
  4. Holder-relpaths stimmen mit Manifest überein?

Use Case:
  - Nach Index-Rekonstruktion: Stimmt der neue Index?
  - Nach Snapshot-Löschung: Ist der Index konsistent?
  - Periodische Integritätsprüfung

Benötigt: pcloud_bin_lib.py
"""

from __future__ import annotations
import os, sys, json, argparse, time
from typing import Dict, List, Any, Set

try:
    import pcloud_bin_lib as pc
except Exception:
    print("Fehler: pcloud_bin_lib nicht gefunden", file=sys.stderr)
    sys.exit(2)


def load_manifests(manifest_dir: str) -> Dict[str, Dict[str, List[str]]]:
    """
    Lädt alle Manifeste aus dem Verzeichnis.
    
    Returns:
        Dict[snapshot_name, Dict[sha256, [relpaths]]]
        Note: Liste für Hardlinks/Duplikate (gleicher SHA256, mehrere Pfade)
    """
    manifests: Dict[str, Dict[str, List[str]]] = {}
    
    if not os.path.isdir(manifest_dir):
        print(f"[ERROR] Manifest-Verzeichnis nicht gefunden: {manifest_dir}", file=sys.stderr)
        sys.exit(2)
    
    manifest_files = sorted([f for f in os.listdir(manifest_dir) if f.endswith(".json")])
    
    if not manifest_files:
        print(f"[ERROR] Keine Manifeste gefunden in {manifest_dir}", file=sys.stderr)
        sys.exit(2)
    
    print(f"[manifests] Lade {len(manifest_files)} Manifeste...")
    
    for filename in manifest_files:
        snapshot_name = filename.replace(".json", "")
        manifest_path = os.path.join(manifest_dir, filename)
        
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
                items_list = manifest.get("items", [])
                
                # Manifeste v2/v3: items ist eine Liste von Objekten
                # Baue Lookup-Dict: {sha256: [relpaths]} (Liste für Hardlinks/Duplikate)
                items_dict = {}
                for item in items_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "file":
                        continue
                    
                    sha = item.get("sha256")
                    relpath = item.get("relpath")
                    
                    if sha and relpath:
                        if sha not in items_dict:
                            items_dict[sha] = []
                        items_dict[sha].append(relpath)
                
                manifests[snapshot_name] = items_dict
                print(f"  ✓ {snapshot_name}: {len(items_dict)} Dateien")
        
        except Exception as e:
            print(f"  ✗ Fehler beim Laden von {snapshot_name}: {e}", file=sys.stderr)
            continue
    
    return manifests


def load_remote_index(cfg: dict, snaps_root: str, index_file: str = "content_index.json") -> dict:
    """Lädt Remote-Index (Master oder Archive)."""
    is_archive = index_file != "content_index.json"
    
    if is_archive:
        idx_path = f"{snaps_root.rstrip('/')}/_index/archive/{index_file}"
    else:
        idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    
    try:
        txt = pc.get_textfile(cfg, path=idx_path, maxbytes=None)
        index = json.loads(txt or '{"version":1,"items":{}}')
        if not isinstance(index, dict):
            index = {"version": 1, "items": {}}
        if "items" not in index:
            index["items"] = {}
        return index
    except Exception as e:
        print(f"[ERROR] Index nicht ladbar: {e}", file=sys.stderr)
        sys.exit(2)


def verify_index(index: dict, manifests: Dict[str, Dict[str, List[str]]], 
                 snaps_root: str) -> Dict[str, Any]:
    """
    Vergleicht Index gegen Manifeste.
    
    Note: manifests enthält Listen von relpaths für Hardlinks/Duplikate
    
    Returns:
        Report-Dict mit allen Findings
    """
    items = index.get("items", {})
    
    # Stats
    total_index_nodes = len(items)
    total_manifest_files = sum(len(m) for m in manifests.values())
    
    # Findings
    missing_in_index: List[dict] = []  # Dateien in Manifesten, aber nicht im Index
    extra_in_index: List[dict] = []    # Holder im Index, aber nicht in Manifesten
    sha_mismatch: List[dict] = []      # SHA256 stimmt nicht überein
    relpath_mismatch: List[dict] = []  # relpath stimmt nicht überein
    
    # Build reverse lookup: Was sollte im Index sein?
    expected_files: Dict[str, Set[str]] = {}  # {sha256: {snapshot_names}}
    manifest_lookup = manifests  # Direkt verwenden: {snapshot: {sha256: [relpaths]}}
    
    for snapshot_name, files in manifests.items():
        for sha256 in files.keys():
            if sha256 not in expected_files:
                expected_files[sha256] = set()
            expected_files[sha256].add(snapshot_name)
    
    # === Check 1: Manifest-Dateien im Index? ===
    print("\n[check 1] Prüfe: Alle Manifest-Dateien im Index?")
    for sha256, snapshots_expected in expected_files.items():
        if sha256 not in items:
            # Datei fehlt komplett im Index
            for snap in snapshots_expected:
                relpaths = manifest_lookup[snap][sha256]  # Liste von Pfaden
                for relpath in relpaths:
                    missing_in_index.append({
                        "sha256": sha256,
                        "snapshot": snap,
                        "relpath": relpath,
                        "reason": "SHA256 nicht im Index"
                    })
    
    print(f"[check 1] Fehlende Dateien: {len(missing_in_index)}")
    
    # === Check 2: Index-Holder in Manifesten? + relpath korrekt? ===
    print("\n[check 2] Prüfe: Alle Index-Holder in Manifesten? relpath korrekt?")
    
    for sha256, node in items.items():
        if not isinstance(node, dict):
            continue
        
        holders = node.get("holders", [])
        
        for holder in holders:
            if not isinstance(holder, dict):
                # Korrupter String-Holder
                extra_in_index.append({
                    "sha256": sha256,
                    "holder": repr(holder),
                    "reason": "Korruptes Holder-Format (String statt Dict)"
                })
                continue
            
            snap = holder.get("snapshot")
            relpath = holder.get("relpath")
            
            if not snap or not relpath:
                extra_in_index.append({
                    "sha256": sha256,
                    "holder": holder,
                    "reason": "Holder ohne snapshot/relpath"
                })
                continue
            
            # Prüfe ob Snapshot existiert
            if snap not in manifest_lookup:
                extra_in_index.append({
                    "sha256": sha256,
                    "snapshot": snap,
                    "relpath": relpath,
                    "reason": f"Snapshot {snap} existiert nicht in Manifesten"
                })
                continue
            
            # Prüfe ob SHA256 in diesem Snapshot existiert
            manifest_files = manifest_lookup[snap]
            
            if sha256 not in manifest_files:
                extra_in_index.append({
                    "sha256": sha256,
                    "snapshot": snap,
                    "relpath": relpath,
                    "reason": f"SHA256 nicht in Manifest {snap}"
                })
                continue
            
            # Prüfe relpath (relpath muss in der Liste der gültigen Pfade sein)
            expected_relpaths = manifest_files[sha256]  # Liste bei Hardlinks/Duplikaten
            if relpath not in expected_relpaths:
                relpath_mismatch.append({
                    "sha256": sha256,
                    "snapshot": snap,
                    "index_relpath": relpath,
                    "manifest_relpaths": expected_relpaths  # Alle gültigen Pfade
                })
    
    print(f"[check 2] Extra Holder (nicht in Manifesten): {len(extra_in_index)}")
    print(f"[check 2] relpath-Abweichungen: {len(relpath_mismatch)}")
    
    return {
        "total_index_nodes": total_index_nodes,
        "total_manifest_files": total_manifest_files,
        "total_manifest_snaps": len(manifests),
        "missing_in_index": missing_in_index,
        "extra_in_index": extra_in_index,
        "relpath_mismatch": relpath_mismatch,
    }


def print_report(report: Dict[str, Any]):
    """Gibt Report formatiert aus."""
    print("\n" + "=" * 70)
    print("=== VERIFIKATIONS-ERGEBNIS ===")
    print("=" * 70)
    
    print(f"\n  Index-Nodes:             {report['total_index_nodes']}")
    print(f"  Manifest-Dateien:        {report['total_manifest_files']}")
    print(f"  Manifest-Snapshots:      {report['total_manifest_snaps']}")
    
    missing = report["missing_in_index"]
    extra = report["extra_in_index"]
    relpath_mm = report["relpath_mismatch"]
    
    total_issues = len(missing) + len(extra) + len(relpath_mm)
    
    print(f"\n  Fehlende Dateien (Manifest → Index): {len(missing)}")
    if missing:
        for m in missing[:10]:
            print(f"    [FEHLT] {m['snapshot']}/{m['relpath']}")
            print(f"            SHA256: {m['sha256'][:16]}... | {m['reason']}")
        if len(missing) > 10:
            print(f"    ... und {len(missing)-10} weitere")
    
    print(f"\n  Extra Holder (Index → Manifest):     {len(extra)}")
    if extra:
        for e in extra[:10]:
            snap = e.get('snapshot', '?')
            relpath = e.get('relpath', '?')
            print(f"    [EXTRA] {snap}/{relpath}")
            print(f"            SHA256: {e['sha256'][:16]}... | {e['reason']}")
        if len(extra) > 10:
            print(f"    ... und {len(extra)-10} weitere")
    
    print(f"\n  relpath-Abweichungen:                {len(relpath_mm)}")
    if relpath_mm:
        for r in relpath_mm[:10]:
            print(f"    [RELPATH] {r['snapshot']}")
            print(f"              Index:    {r['index_relpath']}")
            manifest_paths = r.get('manifest_relpaths', r.get('manifest_relpath'))
            if isinstance(manifest_paths, list):
                print(f"              Manifest: {manifest_paths[0]}")  # Zeige ersten gültigen Pfad
                if len(manifest_paths) > 1:
                    print(f"              (+ {len(manifest_paths)-1} weitere gültige Pfade)")
            else:
                print(f"              Manifest: {manifest_paths}")
        if len(relpath_mm) > 10:
            print(f"    ... und {len(relpath_mm)-10} weitere")
    
    print("\n" + "=" * 70)
    if total_issues == 0:
        print("  ✓ KEINE ABWEICHUNGEN — Index konsistent mit Manifesten")
    else:
        print(f"  ✗ {total_issues} PROBLEME GEFUNDEN")
    print("=" * 70)


def main():
    ap = argparse.ArgumentParser(
        description="pcloud_verify_index_vs_manifests — Index gegen Manifeste prüfen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Prüft ob der Remote-Index (content_index.json) mit den lokalen Manifesten
übereinstimmt. Dies ist die Ground-Truth-Verifikation.

Checks:
  1. Alle Manifest-Dateien im Index vorhanden?
  2. Alle Index-Holder haben entsprechende Manifest-Einträge?
  3. relpath-Konsistenz

Beispiele:
  # Master-Index prüfen:
  python pcloud_verify_index_vs_manifests.py \\
    --dest-root /Backup/rtb_1to1 \\
    --manifest-dir /srv/pcloud-archive/manifests
  
  # Archive-Index prüfen:
  python pcloud_verify_index_vs_manifests.py \\
    --dest-root /Backup/rtb_1to1 \\
    --index-file 2026-04-10-075334_index.json \\
    --manifest-dir /srv/pcloud-archive/manifests
  
  # Mit JSON-Report:
  python pcloud_verify_index_vs_manifests.py \\
    --dest-root /Backup/rtb_1to1 \\
    --manifest-dir /srv/pcloud-archive/manifests \\
    --json-out /srv/pcloud-temp/verify-report.json
""")
    
    ap.add_argument("--dest-root", required=True,
                    help="pCloud-Basispfad (z.B. /Backup/rtb_1to1)")
    ap.add_argument("--manifest-dir", required=True,
                    help="Lokales Manifest-Verzeichnis (z.B. /srv/pcloud-archive/manifests)")
    ap.add_argument("--index-file", default="content_index.json",
                    help="Index-Datei (default: content_index.json)")
    ap.add_argument("--env-file",
                    help="Pfad zur .env-Datei (optional)")
    ap.add_argument("--profile",
                    help="pCloud-Profil (optional)")
    ap.add_argument("--json-out",
                    help="Report als JSON speichern")
    
    args = ap.parse_args()
    
    cfg = pc.effective_config(env_file=args.env_file, profile=args.profile)
    dest_root = pc._norm_remote_path(args.dest_root)
    snaps_root = f"{dest_root.rstrip('/')}/_snapshots"
    
    print("=" * 70)
    print("INDEX-VERIFIKATION: Remote-Index vs. Lokale Manifeste")
    print("=" * 70)
    print(f"Destination:   {snaps_root}")
    print(f"Index-Datei:   {args.index_file}")
    print(f"Manifest-Dir:  {args.manifest_dir}")
    print()
    
    t_start = time.time()
    
    # 1) Manifeste laden
    manifests = load_manifests(args.manifest_dir)
    
    # 2) Remote-Index laden
    print()
    print(f"[index] Lade Remote-Index: {args.index_file}...")
    index = load_remote_index(cfg, snaps_root, args.index_file)
    print(f"[index] ✓ Index geladen: {len(index.get('items', {}))} Nodes")
    
    # 3) Verifikation
    print()
    print("[verify] Starte Verifikation...")
    report = verify_index(index, manifests, snaps_root)
    
    # 4) Report ausgeben
    print_report(report)
    
    # 5) Optional: JSON speichern
    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n[output] Report gespeichert: {args.json_out}")
    
    dt = time.time() - t_start
    print(f"\n[timing] Gesamtlaufzeit: {dt:.1f}s")


if __name__ == "__main__":
    main()
