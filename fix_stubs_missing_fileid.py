#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_stubs_missing_fileid.py

Repariert Stubs (.meta.json), die keine FileID haben:

STANDARD-MODUS:
1. Lädt Master-Index von pCloud (_snapshots/_index/content_index.json)
2. Findet items ohne "fileid" im Index
3. Ermittelt FileID via stat_file() am anchor_path
4. Aktualisiert Index + schreibt alle betroffenen Stubs neu
5. Speichert Index zurück nach pCloud (mit remote Backup)

REWRITE-ALL-MODUS (--rewrite-all):
Nutzen wenn Index FileIDs hat, aber Stubs nicht:
1. Lädt Master-Index von pCloud
2. Nimmt ALLE items (egal ob FileID vorhanden)
3. Schreibt alle Stubs mit FileIDs aus Index neu
4. Speichert Index nur falls neue FileIDs gefetched wurden

Voraussetzungen:
- Master-Index remote vorhanden: <dest-root>/_snapshots/_index/content_index.json
- pcloud_bin_lib.py im selben Verzeichnis
- pCloud credentials (.env oder ENV)

Beispiel (Standard):
    python fix_stubs_missing_fileid.py \\
      --dest-root /Backup/rtb_1to1 \\
      --dry-run

Beispiel (Rewrite-All - Index OK, Stubs kaputt):
    python fix_stubs_missing_fileid.py \\
      --dest-root /Backup/rtb_1to1 \\
      --rewrite-all \\
      --verbose

Beispiel (Index-Check):
    python fix_stubs_missing_fileid.py \\
      --dest-root /Backup/rtb_1to1 \\
      --check-index 20
"""

from __future__ import annotations
import os, sys, json, argparse, time, datetime
from typing import Dict, Any, List, Optional

# ---- Logging ----
def _log(msg: str, *, file=sys.stderr) -> None:
    """Log mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", file=file, flush=True)

# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"FEHLER: pcloud_bin_lib konnte nicht importiert werden: {e}", file=sys.stderr)
    sys.exit(2)

# ---- Globale Statistik ----
stats = {
    "items_total": 0,
    "items_without_fileid": 0,
    "fileids_fetched": 0,
    "fileids_cached": 0,
    "fileids_failed": 0,
    "stubs_rewritten": 0,
    "stubs_failed": 0,
    "index_updated": False,
}


def load_index(cfg: dict, index_path: str) -> dict:
    """Lädt content_index.json von pCloud"""
    _log(f"[index] Lade von pCloud: {index_path}")
    try:
        text = pc.get_textfile(cfg, path=index_path)
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Index konnte nicht geladen werden: {e}")


def save_index(cfg: dict, index: dict, index_path: str, *, dry: bool = False) -> None:
    """Speichert content_index.json nach pCloud (mit Backup)"""
    if dry:
        _log(f"[dry] Index würde gespeichert: {index_path}")
        return
    
    # Backup remote erstellen (aktuellen Index als Backup speichern)
    backup_path = f"{index_path}.backup-{int(time.time())}"
    try:
        _log(f"[index] Erstelle Backup: {backup_path}")
        # Aktuellen Stand von pCloud holen und als Backup speichern
        current_text = pc.get_textfile(cfg, path=index_path)
        current_index = json.loads(current_text)
        pc.write_json_at_path(cfg, path=backup_path, obj=current_index)
    except Exception as e:
        _log(f"[warn] Backup fehlgeschlagen (fahre fort): {e}")
    
    # Speichern nach pCloud
    _log(f"[index] Speichere nach pCloud: {index_path}")
    pc.write_json_at_path(cfg, path=index_path, obj=index)
    
    stats["index_updated"] = True


def fetch_fileid_for_anchor(cfg: dict, anchor_path: str, fid_cache: dict, *, verbose: bool = False) -> Optional[int]:
    """
    Ermittelt FileID für anchor_path via stat_file()
    
    Args:
        cfg: pCloud config
        anchor_path: Remote path zum Anchor
        fid_cache: Cache {anchor_path: fileid}
        verbose: Verbose logging
    
    Returns:
        FileID oder None bei Fehler
    """
    # Cache-Check
    if anchor_path in fid_cache:
        stats["fileids_cached"] += 1
        if verbose:
            _log(f"[fileid] Cache-Hit: {anchor_path} → {fid_cache[anchor_path]}")
        return fid_cache[anchor_path]
    
    # Fetch via stat_file
    try:
        if verbose:
            _log(f"[fileid] Fetch: {anchor_path}")
        
        md = pc.stat_file(cfg, path=anchor_path, with_checksum=False, enrich_path=False)
        
        if not md or md.get("isfolder"):
            _log(f"[warn] Anchor ist Ordner oder nicht gefunden: {anchor_path}")
            stats["fileids_failed"] += 1
            return None
        
        fileid = int(md.get("fileid") or md.get("id") or 0)
        if not fileid:
            _log(f"[warn] FileID nicht im Metadata: {anchor_path}")
            stats["fileids_failed"] += 1
            return None
        
        # Cache
        fid_cache[anchor_path] = fileid
        stats["fileids_fetched"] += 1
        
        if verbose:
            _log(f"[fileid] ✓ {anchor_path} → {fileid}")
        
        return fileid
    
    except Exception as e:
        _log(f"[error] stat_file fehlgeschlagen: {anchor_path}: {e}")
        stats["fileids_failed"] += 1
        return None


def rewrite_stub(cfg: dict, snapshots_root: str, snapshot: str, relpath: str, 
                 sha256: str, anchor_path: str, fileid: int, 
                 *, dry: bool = False, verbose: bool = False) -> bool:
    """
    Schreibt Stub (.meta.json) neu mit FileID
    
    Args:
        cfg: pCloud config
        snapshots_root: Root-Pfad (_snapshots)
        snapshot: Snapshot-Name
        relpath: Relpath der Datei
        sha256: SHA256-Hash
        anchor_path: Anchor-Pfad
        fileid: FileID
        dry: Dry-run Modus
        verbose: Verbose logging
    
    Returns:
        True bei Erfolg, False bei Fehler
    """
    # Pfad konstruieren
    if "/" in relpath:
        stub_dir, base = relpath.rsplit("/", 1)
    else:
        stub_dir, base = "", relpath
    
    parent_dir = f"{snapshots_root.rstrip('/')}/{snapshot}"
    if stub_dir:
        parent_dir = f"{parent_dir}/{stub_dir}"
    
    filename = f"{base}.meta.json"
    meta_path = f"{parent_dir}/{filename}"
    
    if dry:
        print(f"[dry] Stub-Write: {meta_path} → anchor={anchor_path}, fileid={fileid}")
        return True
    
    try:
        # 1) Parent-Folder sicherstellen
        foldid = pc.stat_folderid_fast(cfg, parent_dir)
        if not foldid:
            if verbose:
                _log(f"[stub] ensure parent: {parent_dir}")
            foldid = pc.ensure_path(cfg, parent_dir)
        foldid = int(foldid)
        
        # 2) Bestehendes Stub laden (best effort)
        try:
            old_txt = pc.get_textfile(cfg, path=meta_path)
            payload = json.loads(old_txt)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        
        # 3) Pflichtfelder setzen/aktualisieren
        payload.setdefault("type", "hardlink")
        payload["sha256"] = sha256.lower()
        payload["relpath"] = relpath
        payload["snapshot"] = snapshot
        payload["anchor_path"] = anchor_path
        payload["fileid"] = fileid
        
        # 4) Schreiben
        if verbose:
            _log(f"[stub] Write: {meta_path}")
        
        pc.write_json_to_folderid(cfg, folderid=foldid, filename=filename, obj=payload, minify=True)
        
        stats["stubs_rewritten"] += 1
        return True
    
    except Exception as e:
        _log(f"[error] Stub-Write fehlgeschlagen: {meta_path}: {e}")
        stats["stubs_failed"] += 1
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Repariert Stubs ohne FileID im Master-Index",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    ap.add_argument("--dest-root", required=True, help="pCloud-Zielroot (z.B. /Backup/rtb_1to1)")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht schreiben")
    ap.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    ap.add_argument("--check-index", type=int, metavar="N", help="Zeige erste N items aus Index (Debug)")
    ap.add_argument("--rewrite-all", action="store_true", help="Alle Stubs neu schreiben (nicht nur ohne FileID)")
    args = ap.parse_args()
    
    # Pfade
    snapshots_root = f"{args.dest_root.rstrip('/')}/_snapshots"
    index_path = f"{snapshots_root}/_index/content_index.json"
    
    # Config laden
    cfg = pc.effective_config()
    
    # Index laden (von pCloud)
    try:
        index = load_index(cfg, index_path)
    except Exception as e:
        _log(f"[error] {e}")
        return 1
    items = index.get("items", {})
    
    _log(f"[index] Items im Index: {len(items)}")
    stats["items_total"] = len(items)
    
    # Debug: Index-Check
    if args.check_index:
        _log(f"[debug] Zeige erste {args.check_index} items aus Index:")
        count_with_fid = 0
        count_without_fid = 0
        for i, (sha256, node) in enumerate(items.items(), 1):
            if i > args.check_index:
                break
            has_fid = node.get("fileid") is not None
            if has_fid:
                count_with_fid += 1
            else:
                count_without_fid += 1
            print(f"  [{i}] sha256={sha256[:16]}... fileid={node.get('fileid')} anchor={node.get('anchor_path', 'N/A')[:60]}")
            if not has_fid:
                print(f"       HOLDERS: {len(node.get('holders', []))} stubs")
        _log(f"[debug] Von ersten {args.check_index} items: {count_with_fid} mit FileID, {count_without_fid} ohne FileID")
        
        # Gesamtstatistik
        total_with = sum(1 for n in items.values() if n.get("fileid") is not None)
        total_without = len(items) - total_with
        _log(f"[debug] GESAMT: {total_with} items MIT FileID ({total_with*100//len(items)}%), {total_without} items OHNE FileID ({total_without*100//len(items)}%)")
        return 0
    
    # FileID-Cache (für Anchor-Wiederverwendung)
    fid_cache: Dict[str, int] = {}
    
    # Phase 1: Items sammeln (entweder ohne FileID oder alle falls --rewrite-all)
    _log("[phase1] Sammle items zum Reparieren...")
    items_to_fix: List[Tuple[str, dict]] = []
    
    if args.rewrite_all:
        # ALLE items neu schreiben (Index hat FileIDs, aber Stubs nicht)
        for sha256, node in items.items():
            items_to_fix.append((sha256, node))
        _log(f"[phase1] ✓ --rewrite-all: {len(items_to_fix)} items gefunden")
    else:
        # Nur items ohne FileID im Index
        for sha256, node in items.items():
            if node.get("fileid") is None:
                items_to_fix.append((sha256, node))
        _log(f"[phase1] ✓ {len(items_to_fix)} items ohne FileID im Index gefunden")
    
    stats["items_without_fileid"] = len(items_to_fix)
    
    if not items_to_fix:
        _log("[done] Keine items zu reparieren")
        return 0
    
    # Phase 2: FileIDs sicherstellen (nur falls im Index fehlen)
    _log("[phase2] Prüfe FileIDs...")
    t_start = time.time()
    need_fetch_count = 0
    
    for i, (sha256, node) in enumerate(items_to_fix, 1):
        fileid = node.get("fileid")
        
        # Falls FileID im Index fehlt: fetchen
        if not fileid:
            need_fetch_count += 1
            anchor_path = node.get("anchor_path")
            
            if not anchor_path:
                _log(f"[warn] Item {sha256[:16]}... hat weder FileID noch anchor_path - überspringe")
                continue
            
            # Progress
            if need_fetch_count % 10 == 0 or args.verbose:
                _log(f"[phase2] FileID-Fetch {need_fetch_count}/{len([n for _, n in items_to_fix if not n.get('fileid')])}")
            
            # FileID fetchen
            fileid = fetch_fileid_for_anchor(cfg, anchor_path, fid_cache, verbose=args.verbose)
            
            if fileid:
                # Index aktualisieren
                node["fileid"] = fileid
    
    elapsed = time.time() - t_start
    
    if need_fetch_count > 0:
        _log(f"[phase2] ✓ {stats['fileids_fetched']} neue FileIDs gefetched in {elapsed:.1f}s "
             f"({stats['fileids_cached']} aus Cache, {stats['fileids_failed']} failed)")
    else:
        _log(f"[phase2] ✓ Alle items haben bereits FileID im Index - kein Fetch nötig")
    
    # Phase 3: Stubs neu schreiben
    _log("[phase3] Schreibe Stubs neu...")
    t_start = time.time()
    stub_count = 0
    
    for sha256, node in items_to_fix:
        fileid = node.get("fileid")
        
        if not fileid:
            if args.verbose:
                _log(f"[skip] Item {sha256} hat keine FileID - überspringe Stubs")
            continue
        
        anchor_path = node.get("anchor_path")
        holders = node.get("holders", [])
        
        # Nur Stubs neu schreiben (nicht den Anchor)
        for holder in holders:
            snapshot = holder.get("snapshot")
            relpath = holder.get("relpath")
            
            if not snapshot or not relpath:
                continue
            
            # Stub-Path vs Anchor-Path → nur Stubs neu schreiben
            holder_path = f"{snapshots_root}/{snapshot}/{relpath}"
            if holder_path == anchor_path:
                # Das ist der Anchor, kein Stub
                if args.verbose:
                    _log(f"[skip] Anchor (kein Stub): {holder_path}")
                continue
            
            stub_count += 1
            
            # Progress
            if stub_count % 10 (nur falls FileIDs gefetched wurden)
    if need_fetch_count > 0 and stats['fileids_fetched'] > 0:
        _log("[phase4] Speichere Index (neue FileIDs wurden hinzugefügt)...")
        save_index(cfg, index, index_path, dry=args.dry_run)
    else:
        _log("[phase4] Index-Speicherung übersprungen (keine Änderungen)")
        if args.dry_run:
            stats["index_updated"] = False
            # Stub neu schreiben
            rewrite_stub(
                cfg, snapshots_root, snapshot, relpath, sha256, anchor_path, fileid,
                dry=args.dry_run, verbose=args.verbose
            )
    
    elapsed = time.time() - t_start
    _log(f"[phase3] ✓ {stats['stubs_rewritten']} Stubs neu geschrieben in {elapsed:.1f}s "
         f"({stats['stubs_failed']} failed)")
    
    # Phase 4: Index speichern
    _log("[phase4] Speichere Index...")
    save_index(cfg, index, index_path, dry=args.dry_run)
    
    # Final Report
    print("\n" + "="*60)
    print("REPARATUR ABGESCHLOSSEN")
    print("="*60)
    print(f"Items gesamt:           {stats['items_total']}")
    print(f"Items ohne FileID:      {stats['items_without_fileid']}")
    print(f"FileIDs neu gefetched:  {stats['fileids_fetched']}")
    print(f"FileIDs aus Cache:      {stats['fileids_cached']}")
    print(f"FileIDs fehlgeschlagen: {stats['fileids_failed']}")
    print(f"Stubs neu geschrieben:  {stats['stubs_rewritten']}")
    print(f"Stubs fehlgeschlagen:   {stats['stubs_failed']}")
    print(f"Index aktualisiert:     {'✓' if stats['index_updated'] else '✗ (dry-run)'}")
    print("="*60)
    
    if args.dry_run:
        print("\n⚠ DRY-RUN - Keine Änderungen vorgenommen!")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
