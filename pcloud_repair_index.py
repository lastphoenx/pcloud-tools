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
  4. Bereinigten Index lokal speichern unter /tmp/pcloud_index_{snapshot}.json
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


def repair_index(index: dict, missing_anchors: List[dict], snaps_root: str) -> Dict[str, Any]:
    """
    Entfernt Holder-Einträge für missing_anchors.
    
    Returns: Stats dict mit removed_holders, cleaned_nodes, removed_nodes
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
    
    # Iterate over all index nodes
    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        
        anchor_path = node.get("anchor_path")
        holders = node.get("holders", [])
        
        # Check if this node's anchor is missing
        if anchor_path and anchor_path in missing_lookup:
            # This anchor is phantom
            # Find and remove holders that reference this anchor
            new_holders = []
            for h in holders:
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
                
                new_holders.append(h)
            
            # Update holders
            if len(new_holders) < len(holders):
                node["holders"] = new_holders
                cleaned_nodes += 1
            
            # If no holders left, remove anchor_path and fileid
            if not new_holders:
                if "anchor_path" in node:
                    del node["anchor_path"]
                if "fileid" in node:
                    del node["fileid"]
                if "pcloud_hash" in node:
                    del node["pcloud_hash"]
                
                # If node is now completely empty (no holders, no anchor), remove it
                if not node.get("holders") and not node.get("anchor_path"):
                    del items[sha]
                    removed_nodes += 1
    
    return {
        "removed_holders": removed_holders,
        "cleaned_nodes": cleaned_nodes,
        "removed_nodes": removed_nodes,
    }


def save_local_index(index: dict, snapshot_name: str, output_path: str | None = None) -> str:
    """
    Speichert den bereinigten Index lokal.
    
    Falls output_path nicht angegeben: /tmp/pcloud_index_{snapshot}.json
    """
    if output_path:
        out = output_path
    else:
        tmp_dir = tempfile.gettempdir()
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
     python pcloud_quick_delta.py --dest-root /backup-nas --json-out /tmp/delta.json
  
  2. Index reparieren:
     python pcloud_repair_index.py --delta-report /tmp/delta.json --dest-root /backup-nas
  
  3. Upload starten (nutzt automatisch den reparierten lokalen Index):
     python pcloud_push_json_manifest_to_pcloud.py ...
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
                    help="Ausgabepfad für reparierten Index (default: /tmp/pcloud_index_{snapshot}.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur Report, keine Änderungen")
    
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
        stats = repair_index(index_copy, missing_anchors, snaps_root)
    else:
        stats = repair_index(index, missing_anchors, snaps_root)
    
    print(f"[phase 3] Holders entfernt:     {stats['removed_holders']}")
    print(f"[phase 3] Nodes bereinigt:      {stats['cleaned_nodes']}")
    print(f"[phase 3] Nodes komplett gelöscht: {stats['removed_nodes']}")
    
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
        print(f"      --manifest /tmp/manifest.json \\")
        print(f"      --dest-root {dest_root} \\")
        print(f"      --snapshot-mode 1to1 \\")
        print(f"      --env-file /opt/apps/pcloud-tools/main/.env")
    else:
        print()
        print("[dry-run] Keine Änderungen vorgenommen")


if __name__ == "__main__":
    main()
