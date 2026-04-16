#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_repair_index.py — Index-Reparatur nach Delta-Check.

Nimmt einen Delta-Report (JSON) von pcloud_quick_delta.py und bereinigt
den content_index.json von Phantom-Einträgen (Holder ohne reale Datei).

Workflow:
  1. Delta-Report einlesen (missing_anchors)
  2. Remote content_index.json laden
  3. Für jeden missing_anchor: Holder-Eintrag entfernen
  4. Bereinigten Index lokal speichern unter /srv/pcloud-temp/pcloud_index_{snapshot}.json
  5. Beim nächsten Upload: Resume überspringt echte Dateien, lädt fehlende nach

Benötigt: pcloud_bin_lib.py
"""

from __future__ import annotations
import os, sys, json, argparse, tempfile
from typing import Dict, List, Any

try:
    import pcloud_bin_lib as pc
except Exception:
    print("Fehler: pcloud_bin_lib nicht gefunden", file=sys.stderr)
    sys.exit(2)


def load_delta_report(path: str) -> dict:
    """Lädt den JSON-Report von pcloud_quick_delta.py."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Delta-Report nicht lesbar: {e}", file=sys.stderr)
        sys.exit(2)


def load_remote_index(cfg: dict, snaps_root: str) -> dict:
    """Lädt content_index.json von pCloud."""
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = json.loads(txt or '{"version":1,"items":{}}')
        if not isinstance(j, dict):
            j = {"version": 1, "items": {}}
    except Exception as e:
        print(f"[ERROR] Remote-Index nicht ladbar: {e}", file=sys.stderr)
        sys.exit(2)
    
    if "items" not in j or not isinstance(j["items"], dict):
        j["items"] = {}
    if "version" not in j:
        j["version"] = 1
    
    return j


def repair_index(index: dict, missing_anchors: List[dict], snaps_root: str, *, cleanup_all: bool = False) -> Dict[str, Any]:
    """
    Entfernt Holder-Einträge für missing_anchors.
    
    Args:
        cleanup_all: Wenn True, entfernt auch korrupte String-Holder bei existierenden Anchors
    
    Returns: Stats dict mit removed_holders, cleaned_nodes, removed_nodes, invalid_holders_*
    """
    items = index.get("items", {})
    
    # Build lookup: anchor_path -> (sha256, holder_info)
    missing_lookup: Dict[str, dict] = {}
    for m in missing_anchors:
        anchor = m.get("anchor_path")
        if anchor:
            missing_lookup[anchor] = m
    
    removed_holders = 0
    cleaned_nodes = 0
    removed_nodes = 0
    invalid_holders_missing = 0  # String-Holder bei Missing-Anchors (immer entfernt)
    invalid_holders_other = 0     # String-Holder bei existierenden Anchors (nur mit --cleanup-all)
    
    verbose = os.environ.get("PCLOUD_VERBOSE") == "1"
    debug_samples = []  # Erste 3 Samples für Debugging
    
    # Iterate over all index nodes
    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        
        anchor_path = node.get("anchor_path")
        holders = node.get("holders", [])
        
        # Check if this node's anchor is missing
        is_missing_anchor = anchor_path and anchor_path in missing_lookup
        
        # Skip Nodes ohne Holders (Optimierung)
        if not holders:
            continue
        
        # === Schema-Check für ALLE Nodes (nicht nur Missing-Anchors) ===
        new_holders = []
        for h in holders:
            # Schema-Validierung: String-Holder erkennen und behandeln
            if not isinstance(h, dict):
                if is_missing_anchor:
                    # Bei Missing-Anchor: IMMER entfernen
                    invalid_holders_missing += 1
                    if verbose:
                        print(f"[warn] Korrupter Holder entfernt (Missing-Anchor): {repr(h)[:60]}")
                        print(f"       SHA256: {sha[:16]}... Anchor: {anchor_path}")
                    continue
                else:
                    # Bei existierendem Anchor: nur mit --cleanup-all entfernen
                    invalid_holders_other += 1
                    
                    # Debug: Erste 3 Samples sammeln
                    if len(debug_samples) < 3:
                        debug_samples.append({
                            "sha": sha[:16],
                            "anchor": anchor_path or "(none)",
                            "holder_type": type(h).__name__,
                            "holder_content": repr(h)[:100]
                        })
                    
                    if cleanup_all:
                        if verbose:
                            print(f"[info] Korrupter Holder entfernt (--cleanup-all): {repr(h)[:60]}")
                            print(f"       SHA256: {sha[:16]}...")
                        continue
                    else:
                        # Behalten, nur zählen (für Reporting)
                        new_holders.append(h)
                        continue
            
            # Ab hier: h ist garantiert ein Dict
            
            # Bei Missing-Anchor: prüfe ob dieser Holder auf den Missing-Anchor zeigt
            if is_missing_anchor:
                h_snap = h.get("snapshot")
                h_rel = h.get("relpath")
                
                # Reconstruct the holder's path (identical to upload logic)
                # Upload does: anchor_path = f"{snapshots_root}/{snapshot}/{relpath}"
                if h_snap and h_rel:
                    holder_path = f"{snaps_root}/{h_snap}/{h_rel}"
                    
                    # If this holder path matches the missing anchor, remove it
                    if holder_path == anchor_path:
                        removed_holders += 1
                        continue
            
            # Holder behalten
            new_holders.append(h)
        
        # Update holders wenn sich was geändert hat
        if len(new_holders) < len(holders):
            node["holders"] = new_holders
            cleaned_nodes += 1
        
        # CRITICAL: If the anchor_path itself is missing, remove it immediately
        # (independent of how many holders remain)
        if is_missing_anchor:
            if "anchor_path" in node:
                del node["anchor_path"]
            if "fileid" in node:
                del node["fileid"]
            if "pcloud_hash" in node:
                del node["pcloud_hash"]
        
        # If no holders left AND no anchor, remove node completely
        if not new_holders and not node.get("anchor_path"):
            del items[sha]
            removed_nodes += 1
    
    return {
        "removed_holders": removed_holders,
        "cleaned_nodes": cleaned_nodes,
        "removed_nodes": removed_nodes,
        "invalid_holders_missing": invalid_holders_missing,
        "invalid_holders_other": invalid_holders_other,
        "debug_samples": debug_samples,
    }


def save_local_index(index: dict, snapshot_name: str, output_path: str | None = None) -> str:
    """
    Speichert den bereinigten Index lokal.
    
    Falls output_path nicht angegeben: /srv/pcloud-temp/pcloud_index_{snapshot}.json
    """
    if output_path:
        out = output_path
    else:
        tmp_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
        out = os.path.join(tmp_dir, f"pcloud_index_{snapshot_name}.json")
    
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    
    return out


def main():
    ap = argparse.ArgumentParser(
        description="pcloud_repair_index — Bereinigt Index von Phantom-Einträgen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  1. Erzeuge Delta-Report:
     python pcloud_quick_delta.py --dest-root /backup-nas --json-out /srv/pcloud-temp/delta.json
  
  2. Index reparieren:
     python pcloud_repair_index.py --delta-report /srv/pcloud-temp/delta.json --dest-root /backup-nas
  
  3. Upload starten (nutzt automatisch den reparierten lokalen Index):
     python pcloud_push_json_manifest_to_pcloud.py ...

Schema-Validierung:
  Das Tool prüft automatisch alle Holder-Einträge auf Schema-Korrektheit (Dict vs. String).
  
  - Bei Missing-Anchors: Korrupte String-Holder werden IMMER entfernt
  - Bei existierenden Anchors: String-Holder werden nur gemeldet
    → Verwende --cleanup-all zum Entfernen aller korrupten Holder
""")
    
    ap.add_argument("--delta-report", required=True,
                    help="JSON-Report von pcloud_quick_delta.py")
    ap.add_argument("--dest-root", required=True,
                    help="pCloud-Basispfad (z.B. /backup-nas)")
    ap.add_argument("--snapshot",
                    help="Snapshot-Name (wird aus Delta-Report extrahiert, falls nicht angegeben)")
    ap.add_argument("--env-file",
                    help="Pfad zur .env-Datei (optional)")
    ap.add_argument("--profile",
                    help="pCloud-Profil (optional)")
    ap.add_argument("--output",
                    help="Ausgabepfad für reparierten Index (default: /srv/pcloud-temp/pcloud_index_{snapshot}.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur Report, keine Änderungen")
    ap.add_argument("--cleanup-all", action="store_true",
                    help="Entferne auch korrupte Holder außerhalb von Missing-Anchors (String statt Dict)")
    
    args = ap.parse_args()
    
    cfg = pc.effective_config(env_file=args.env_file, profile=args.profile)
    dest_root = pc._norm_remote_path(args.dest_root)
    snaps_root = f"{dest_root.rstrip('/')}/_snapshots"
    
    print(f"=== pcloud_repair_index ===")
    print(f"Delta-Report: {args.delta_report}")
    print(f"Destination: {snaps_root}")
    print()
    
    # 1. Delta-Report laden
    print("[phase 1] Lade Delta-Report...")
    report = load_delta_report(args.delta_report)
    missing_anchors = report.get("missing_anchors", [])
    
    if not missing_anchors:
        print("[info] Keine missing_anchors im Report → nichts zu tun")
        return
    
    print(f"[phase 1] {len(missing_anchors)} fehlende Anchors gefunden")
    
    # Snapshot-Name aus Report extrahieren (falls nicht als Arg gegeben)
    snapshot_name = args.snapshot
    if not snapshot_name:
        # Versuche aus dem ersten missing_anchor den Snapshot zu extrahieren
        first_anchor = missing_anchors[0].get("anchor_path", "")
        parts = first_anchor.split("/")
        try:
            snap_idx = parts.index("_snapshots") + 1
            snapshot_name = parts[snap_idx]
            print(f"[info] Snapshot-Name aus Report extrahiert: {snapshot_name}")
        except (ValueError, IndexError):
            print("[ERROR] Snapshot-Name konnte nicht ermittelt werden. Bitte --snapshot angeben.", file=sys.stderr)
            sys.exit(2)
    
    # 2. Remote-Index laden
    print()
    print("[phase 2] Lade Remote content_index.json...")
    index = load_remote_index(cfg, snaps_root)
    n_nodes = len(index.get("items", {}))
    print(f"[phase 2] Index geladen: {n_nodes} Nodes")
    
    # 3. Reparatur
    print()
    print("[phase 3] Repariere Index...")
    if args.dry_run:
        print("[dry-run] Simulation — keine Änderungen")
        # Kopie für Dry-Run
        import copy
        index_copy = copy.deepcopy(index)
        stats = repair_index(index_copy, missing_anchors, snaps_root, cleanup_all=args.cleanup_all)
    else:
        stats = repair_index(index, missing_anchors, snaps_root, cleanup_all=args.cleanup_all)
    
    print(f"[phase 3] Holders entfernt:     {stats['removed_holders']}")
    print(f"[phase 3] Nodes bereinigt:      {stats['cleaned_nodes']}")
    print(f"[phase 3] Nodes komplett gelöscht: {stats['removed_nodes']}")
    
    # Schema-Validierung Report
    invalid_missing = stats.get('invalid_holders_missing', 0)
    invalid_other = stats.get('invalid_holders_other', 0)
    if invalid_missing > 0:
        print(f"[phase 3] Korrupte Holder entfernt (Missing-Anchors): {invalid_missing}")
    if invalid_other > 0:
        if args.cleanup_all:
            print(f"[phase 3] Korrupte Holder entfernt (--cleanup-all): {invalid_other}")
        else:
            print(f"[phase 3] ⚠ Korrupte Holder gefunden: {invalid_other} (String statt Dict)")
            print(f"[phase 3]   → Verwende --cleanup-all zum Entfernen")
            
            # Debug-Samples anzeigen
            debug_samples = stats.get('debug_samples', [])
            if debug_samples:
                print(f"\n[debug] Beispiele für korrupte Holder:")
                for i, sample in enumerate(debug_samples, 1):
                    print(f"  Sample {i}:")
                    print(f"    SHA: {sample['sha']}...")
                    print(f"    Anchor: {sample['anchor']}")
                    print(f"    Holder-Type: {sample['holder_type']}")
                    print(f"    Holder-Content: {sample['holder_content']}")
    
    # 4. Lokal speichern
    if not args.dry_run:
        print()
        print("[phase 4] Speichere reparierten Index lokal...")
        out_path = save_local_index(index, snapshot_name, args.output)
        print(f"[phase 4] Index gespeichert: {out_path}")
        
        n_nodes_after = len(index.get("items", {}))
        print()
        print(f"[summary] Nodes vorher: {n_nodes}")
        print(f"[summary] Nodes nachher: {n_nodes_after}")
        print(f"[summary] Delta: {n_nodes - n_nodes_after} Nodes entfernt")
        print()
        print(f"✓ Reparatur abgeschlossen.")
        print(f"  Beim nächsten Upload wird dieser Index verwendet:")
        print(f"    {out_path}")
        print()
        print(f"  Upload-Befehl:")
        print(f"    /opt/apps/pcloud-tools/venv-.../bin/python \\")
        print(f"      /opt/apps/pcloud-tools/main/pcloud_push_json_manifest_to_pcloud.py \\")
        print(f"      --manifest /srv/pcloud-temp/manifest.json \\")
        print(f"      --dest-root {dest_root} \\")
        print(f"      --snapshot-mode 1to1 \\")
        print(f"      --env-file /opt/apps/pcloud-tools/main/.env")
    else:
        print()
        print("[dry-run] Keine Änderungen vorgenommen")


# ============================================================================
# TIME-TRAVEL ARCHIVE: Index-Rekonstruktion für alle Snapshots
# ============================================================================

def repair_string_holders_to_dict(index: dict, snapshot_manifests: List[str]) -> Dict[str, Any]:
    """
    Repariert korrupte String-Holder im Master-Index.
    
    String-Holder entstehen durch Bug in pcloud_push_json_manifest_to_pcloud.py:
      holders.append(snapshot_name)  # ← String statt {"snapshot": ..., "relpath": ...}
    
    Strategie: Nutze die lokalen Manifeste um den korrekten relpath zu rekonstruieren.
    
    Returns: Stats dict mit repair_stats
    """
    items = index.get("items", {})
    
    # Lade alle Manifeste (für Lookup sha256 → relpath per snapshot)
    manifest_lookup = {}  # {snapshot_name: {sha256: relpath}}
    
    print("[repair] Lade Manifeste für Holder-Reparatur...")
    for snap_name in snapshot_manifests:
        manifest_path = f"/srv/pcloud-archive/manifests/{snap_name}.json"
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
                items_dict = manifest.get("items", {})
                manifest_lookup[snap_name] = items_dict
                print(f"  ✓ {snap_name}: {len(items_dict)} Dateien")
        except FileNotFoundError:
            print(f"  ⚠ Manifest nicht gefunden: {snap_name}")
            continue
        except Exception as e:
            print(f"  ✗ Fehler beim Laden von {snap_name}: {e}")
            continue
    
    repaired_count = 0
    unrepaired_count = 0
    removed_count = 0
    
    # Iterate über alle Nodes
    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        
        holders = node.get("holders", [])
        new_holders = []
        
        for h in holders:
            # String-Holder erkannt?
            if isinstance(h, str):
                snapshot_name = h
                
                # Versuche relpath aus Manifest zu rekonstruieren
                if snapshot_name in manifest_lookup:
                    manifest_items = manifest_lookup[snapshot_name]
                    
                    # Suche nach SHA256 in diesem Manifest
                    if sha in manifest_items:
                        relpath = manifest_items[sha]
                        
                        # Repariere: String → Dict
                        repaired_holder = {
                            "snapshot": snapshot_name,
                            "relpath": relpath
                        }
                        new_holders.append(repaired_holder)
                        repaired_count += 1
                    else:
                        # SHA256 nicht in Manifest gefunden → entfernen
                        print(f"  ⚠ SHA {sha[:16]}... nicht in Manifest {snapshot_name} → Holder entfernt")
                        removed_count += 1
                else:
                    # Manifest nicht verfügbar → Holder entfernen
                    print(f"  ⚠ Manifest {snapshot_name} nicht verfügbar → Holder entfernt")
                    removed_count += 1
            
            elif isinstance(h, dict):
                # Korrekt formatierter Holder
                new_holders.append(h)
            else:
                # Unbekanntes Format
                print(f"  ✗ Unbekanntes Holder-Format: {type(h)} → entfernt")
                removed_count += 1
        
        # Update holders
        node["holders"] = new_holders
        
        # Wenn keine Holder mehr → Node löschen
        if not new_holders:
            del items[sha]
    
    return {
        "repaired": repaired_count,
        "removed": removed_count,
        "unrepaired": unrepaired_count
    }


def rebuild_index_from_manifests(snapshot_manifests: List[str], snapshots_root: str, 
                                   manifest_dir: str) -> dict:
    """
    Baut Index komplett neu aus Manifesten auf (wenn kein Remote-Index existiert).
    
    Achtung: fileid und pcloud_hash fehlen (werden beim Upload ergänzt).
    
    Returns: Neuer Index mit {"version": 1, "items": {sha256: node}}
    """
    new_index = {"version": 1, "items": {}}
    
    print("[rebuild] Baue Index von Grund auf aus Manifesten...")
    
    # Lade alle Manifeste
    for snap_name in snapshot_manifests:
        manifest_path = os.path.join(manifest_dir, f"{snap_name}.json")
        
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
                items_dict = manifest.get("items", {})
                
                print(f"  [{snap_name}] {len(items_dict)} Dateien")
                
                # Für jede Datei im Manifest
                for sha, relpath in items_dict.items():
                    holder = {
                        "snapshot": snap_name,
                        "relpath": relpath
                    }
                    
                    # Node existiert schon? (Deduplizierung über Snapshots)
                    if sha in new_index["items"]:
                        # Holder hinzufügen
                        new_index["items"][sha]["holders"].append(holder)
                    else:
                        # Neuer Node
                        anchor_path = f"{snapshots_root}/{snap_name}/{relpath}"
                        new_index["items"][sha] = {
                            "anchor_path": anchor_path,
                            "holders": [holder]
                            # fileid und pcloud_hash fehlen → werden beim Upload ergänzt
                        }
        
        except FileNotFoundError:
            print(f"  ⚠ Manifest nicht gefunden: {snap_name}")
            continue
        except Exception as e:
            print(f"  ✗ Fehler beim Laden von {snap_name}: {e}")
            continue
    
    total_files = len(new_index["items"])
    print(f"[rebuild] ✓ Index erstellt: {total_files} Dateien")
    
    return new_index


def generate_timetravel_archive(cfg: dict, master_index: dict, snapshots_root: str, 
                                  snapshot_order: List[str], archive_dir: str,
                                  *, dry_run: bool = False) -> None:
    """
    Erzeugt Time-Travel Archive: Für jeden Snapshot den Index-Stand zu diesem Zeitpunkt.
    
    Workflow:
      1. Snapshot 1: Nur Dateien aus Snapshot 1
      2. Snapshot 2: Kumuliert Snapshot 1 + 2
      3. Snapshot N: Kumuliert Snapshot 1..N
    
    Jeder Archive-Index enthält:
      - Nur Nodes deren ältester Holder <= current_snapshot
      - Nur Holders die <= current_snapshot sind
      - anchor_path zeigt auf den ältesten Holder
    
    Speichert:
      - Lokal: {archive_dir}/{snapshot}_index.json
      - Remote: {snapshots_root}/_index/archive/{snapshot}_index.json
    """
    items = master_index.get("items", {})
    
    print()
    print(f"[timetravel] Generiere Archive für {len(snapshot_order)} Snapshots...")
    print(f"[timetravel] Archive-Verzeichnis: {archive_dir}")
    print()
    
    for i, current_snap in enumerate(snapshot_order, 1):
        print(f"[{i}/{len(snapshot_order)}] {current_snap}...", end=" ", flush=True)
        
        # Erstelle Index für diesen Zeitpunkt
        snap_index = {"version": 1, "items": {}}
        
        for sha, node in items.items():
            if not isinstance(node, dict):
                continue
            
            holders = node.get("holders", [])
            
            # Filter: Nur Holder <= current_snap
            valid_holders = []
            for h in holders:
                if not isinstance(h, dict):
                    continue
                    
                h_snap = h.get("snapshot")
                if h_snap and h_snap <= current_snap:
                    valid_holders.append(h)
            
            # Wenn keine gültigen Holder → skip node
            if not valid_holders:
                continue
            
            # Sortiere Holder chronologisch (ältester zuerst)
            valid_holders.sort(key=lambda x: x["snapshot"])
            
            # Anchor ist der älteste Holder
            oldest = valid_holders[0]
            anchor_path = f"{snapshots_root}/{oldest['snapshot']}/{oldest['relpath']}"
            
            # Erstelle Node für diesen Stand
            snap_node = {
                "anchor_path": anchor_path,
                "holders": valid_holders
            }
            
            # fileid und pcloud_hash optional (fehlen bei Rebuild)
            if node.get("fileid"):
                snap_node["fileid"] = node["fileid"]
            if node.get("pcloud_hash"):
                snap_node["pcloud_hash"] = node["pcloud_hash"]
            
            snap_index["items"][sha] = snap_node
        
        n_files = len(snap_index["items"])
        print(f"{n_files} Dateien", end="")
        
        # Speichern (lokal + remote)
        if not dry_run:
            # 1. Lokal
            local_path = os.path.join(archive_dir, f"{current_snap}_index.json")
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            
            with open(local_path, 'w', encoding='utf-8') as f:
                json.dump(snap_index, f, indent=2, ensure_ascii=False)
            
            # 2. Remote
            remote_path = f"{snapshots_root}/_index/archive/{current_snap}_index.json"
            try:
                pc.write_json_at_path(cfg, remote_path, snap_index)
                print(f" → ✓")
            except Exception as e:
                print(f" → ✗ Remote-Upload fehlgeschlagen: {e}")
        else:
            print(f" → [dry-run]")
    
    print()
    print(f"[timetravel] ✓ Archive generiert")


def rebuild_complete_index(args):
    """
    Hauptfunktion: Rekonstruiere kompletten Index aus Manifesten und erzeuge Archive.
    
    Workflow:
      1. Remote Master-Index laden (mit maxbytes=None für große Files)
      2. String-Holder reparieren (mit Manifesten)
      3. Time-Travel Archive generieren (für jeden Snapshot)
      4. Master-Index remote+lokal speichern
      5. Archive remote+lokal speichern
    
    Nutzen:
      - Beim Löschen von Snapshots: Passenden Archive-Index als Master kopieren
      - Historie: Alle Index-Stände bleiben erhalten
      - Debugging: Lokale Kopien aller Index-Versionen
    """
    cfg = pc.effective_config(env_file=args.env_file, profile=args.profile)
    dest_root = pc._norm_remote_path(args.dest_root)
    snaps_root = f"{dest_root.rstrip('/')}/_snapshots"
    
    # Archive-Verzeichnis (lokal)
    archive_dir = args.archive_dir or "/srv/pcloud-archive/indexes"
    os.makedirs(archive_dir, exist_ok=True)
    
    # Manifest-Verzeichnis
    manifest_dir = args.manifest_dir or "/srv/pcloud-archive/manifests"
    
    print("=" * 70)
    print("INDEX-REKONSTRUKTION: Time-Travel Archive")
    print("=" * 70)
    print(f"Destination: {snaps_root}")
    print(f"Archive-Dir: {archive_dir}")
    print(f"Manifest-Dir: {manifest_dir}")
    print()
    
    # === PHASE 1: Remote Master-Index laden (oder neu erstellen) ===
    print("[phase 1] Lade Remote Master-Index...")
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    
    try:
        # WICHTIG: maxbytes=None für große Index-Files (> 1 MB)
        master_index = pc.read_json_at_path(cfg, idx_path, maxbytes=None)
        print(f"[phase 1] ✓ Existierender Index geladen: {len(master_index.get('items', {}))} Nodes")
        index_mode = "repair"
    except Exception as e:
        error_str = str(e)
        # Prüfe ob File nicht existiert (result 2002 oder 2009)
        if "2002" in error_str or "2009" in error_str or "does not exist" in error_str.lower():
            print(f"[phase 1] ℹ Index existiert noch nicht → wird neu erstellt")
            master_index = {"version": 1, "items": {}}
            index_mode = "rebuild"
        else:
            # Anderer Fehler (z.B. Netzwerk)
            print(f"[phase 1] ✗ Fehler beim Laden: {e}")
            sys.exit(2)
    
    # === PHASE 2: Snapshot-Reihenfolge ermitteln ===
    print()
    print("[phase 2] Ermittle Snapshot-Chronologie...")
    
    if not os.path.isdir(manifest_dir):
        print(f"[phase 2] ✗ Manifest-Verzeichnis nicht gefunden: {manifest_dir}")
        sys.exit(2)
    
    # Alle Manifeste auflisten (chronologisch sortiert)
    manifest_files = sorted([
        f.replace(".json", "") 
        for f in os.listdir(manifest_dir) 
        if f.endswith(".json")
    ])
    
    if not manifest_files:
        print(f"[phase 2] ✗ Keine Manifeste gefunden in {manifest_dir}")
        sys.exit(2)
    
    print(f"[phase 2] ✓ {len(manifest_files)} Snapshots gefunden")
    print(f"[phase 2]   Ältester: {manifest_files[0]}")
    print(f"[phase 2]   Neuester: {manifest_files[-1]}")
    
    # === PHASE 3: Index aufbauen/reparieren ===
    print()
    if index_mode == "rebuild":
        print("[phase 3] Baue Index von Grund auf aus Manifesten...")
        master_index = rebuild_index_from_manifests(
            snapshot_manifests=manifest_files,
            snapshots_root=snaps_root,
            manifest_dir=manifest_dir
        )
        print(f"[phase 3] ✓ Index erstellt: {len(master_index.get('items', {}))} Dateien")
        print(f"[phase 3]   ⚠ Hinweis: fileid/pcloud_hash fehlen (werden beim Upload ergänzt)")
    
    else:  # repair mode
        print("[phase 3] Repariere String-Holder...")
        repair_stats = repair_string_holders_to_dict(master_index, manifest_files)
        print(f"[phase 3] ✓ Reparatur abgeschlossen")
        print(f"[phase 3]   Repariert: {repair_stats['repaired']} Holder")
        print(f"[phase 3]   Entfernt:  {repair_stats['removed']} Holder")
    
    # === PHASE 4: Time-Travel Archive generieren ===
    print()
    print("[phase 4] Generiere Time-Travel Archive...")
    
    generate_timetravel_archive(
        cfg=cfg,
        master_index=master_index,
        snapshots_root=snaps_root,
        snapshot_order=manifest_files,
        archive_dir=archive_dir,
        dry_run=args.dry_run
    )
    
    # === PHASE 5: Master-Index speichern ===
    if not args.dry_run:
        print()
        print("[phase 5] Speichere reparierten Master-Index...")
        
        # 1. Lokal
        local_master = os.path.join(archive_dir, "content_index_master.json")
        with open(local_master, 'w', encoding='utf-8') as f:
            json.dump(master_index, f, indent=2, ensure_ascii=False)
        print(f"[phase 5]   Lokal: {local_master}")
        
        # 2. Remote
        try:
            pc.write_json_at_path(cfg, idx_path, master_index)
            print(f"[phase 5]   Remote: {idx_path}")
            print(f"[phase 5] ✓ Master-Index aktualisiert")
        except Exception as e:
            print(f"[phase 5] ✗ Remote-Upload fehlgeschlagen: {e}")
            sys.exit(2)
    else:
        print()
        print("[phase 5] [dry-run] Master-Index nicht gespeichert")
    
    # === SUMMARY ===
    print()
    print("=" * 70)
    print("✓ INDEX-REKONSTRUKTION ABGESCHLOSSEN")
    print("=" * 70)
    print(f"Master-Index: {len(master_index.get('items', {}))} Dateien")
    print(f"Archive: {len(manifest_files)} Snapshots")
    print()
    print("Nutzung beim Löschen von Snapshots:")
    print("  1. Letzten gültigen Snapshot ermitteln (z.B. 'snapshot_5')")
    print("  2. Archive-Index kopieren:")
    print(f"     cp {archive_dir}/snapshot_5_index.json \\")
    print(f"        {archive_dir}/content_index_master.json")
    print("  3. Remote hochladen:")
    print("     python pcloud_repair_index.py --upload-master ...")
    print()


def main_rebuild():
    """Entry point für --rebuild-from-manifests."""
    ap = argparse.ArgumentParser(
        description="Index-Rekonstruktion: Time-Travel Archive aus Manifesten",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Dieser Modus rekonstruiert den kompletten Index aus lokalen Manifesten und
erzeugt Time-Travel Archive für jeden Snapshot.

Workflow:
  1. Remote Master-Index laden und String-Holder reparieren
  2. Für jeden Snapshot: Index-Stand zu diesem Zeitpunkt generieren
  3. Archive lokal und remote speichern
  4. Master-Index aktualisieren

Nutzen:
  - Beim Löschen von Snapshots: Passenden Archive-Index kopieren
  - Historie: Alle Index-Stände bleiben verfügbar
  - Debugging: Lokale Kopien aller Versionen

Beispiel:
  python pcloud_repair_index.py --rebuild-from-manifests \\
    --dest-root /backup-nas \\
    --manifest-dir /srv/pcloud-archive/manifests \\
    --archive-dir /srv/pcloud-archive/indexes
""")
    
    ap.add_argument("--rebuild-from-manifests", action="store_true",
                    help="Aktiviert Time-Travel Archive-Modus (wird automatisch erkannt)")
    ap.add_argument("--dest-root", required=True,
                    help="pCloud-Basispfad (z.B. /backup-nas)")
    ap.add_argument("--manifest-dir",
                    help="Verzeichnis mit Manifest-JSONs (default: /srv/pcloud-archive/manifests)")
    ap.add_argument("--archive-dir",
                    help="Lokales Archiv-Verzeichnis (default: /srv/pcloud-archive/indexes)")
    ap.add_argument("--env-file",
                    help="Pfad zur .env-Datei (optional)")
    ap.add_argument("--profile",
                    help="pCloud-Profil (optional)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur Report, keine Änderungen")
    
    args = ap.parse_args()
    rebuild_complete_index(args)


if __name__ == "__main__":
    # Check welcher Modus (vor ArgumentParser um Konflikte zu vermeiden)
    if "--rebuild-from-manifests" in sys.argv:
        main_rebuild()
    else:
        main()
