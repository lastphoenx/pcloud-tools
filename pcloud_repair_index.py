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
    
    # Iterate over all index nodes
    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        
        anchor_path = node.get("anchor_path")
        holders = node.get("holders", [])
        
        # Check if this node's anchor is missing
        is_missing_anchor = anchor_path and anchor_path in missing_lookup
        
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


if __name__ == "__main__":
    main()
