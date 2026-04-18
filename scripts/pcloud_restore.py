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
import os, sys, json, argparse, hashlib, time, datetime
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


class IndexLoadError(Exception):
    """Fehler beim Laden/Parsen des Content-Index von pCloud."""
    pass

def download_file_with_verify(cfg: Dict, remote_path: str, local_path: str, sha256_expected: Optional[str] = None) -> bool:
    """
    Datei von pCloud downloaden mit SHA256-Verifikation und Chunk-Verarbeitung
    
    Args:
        cfg: pCloud Config
        remote_path: Pfad auf pCloud
        local_path: Lokaler Ziel-Dateipfad
        sha256_expected: SHA256 zur Verifikation
    
    Returns:
        True wenn erfolgreich
    """
    try:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

        stat = pc.stat_file(cfg, path=remote_path, with_checksum=False) or {}
        file_size = stat.get("size", 0)

        log(f"Download: {remote_path} ({file_size} bytes)")

        # Binär-Download (korrekt für alle Dateitypen: Text, Fotos, Archive, ...)
        content_bytes = pc.get_binaryfile(cfg, path=remote_path)

        with open(local_path, "wb") as f:
            f.write(content_bytes)

        # SHA256-Verifikation
        if sha256_expected:
            actual_sha = hashlib.sha256(content_bytes).hexdigest()
            if actual_sha.lower() != sha256_expected.lower():
                log(f"SHA256 MISMATCH: expected {sha256_expected}, got {actual_sha}", "error")
                os.remove(local_path)
                return False
            log(f"\u2713 SHA256 OK")

        return True
    
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
    Download via FileID (Binary API)
    """
    try:
        stat = pc.stat_file(cfg, fileid=fileid, with_checksum=False) or {}
        file_size = stat.get("size", 0)
        log(f"Download (FileID {fileid}): {file_size} bytes")

        content_bytes = pc.get_binaryfile(cfg, fileid=fileid)

        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content_bytes)

        if sha256_expected:
            actual_sha = hashlib.sha256(content_bytes).hexdigest()
            if actual_sha.lower() != sha256_expected.lower():
                log(f"SHA256 MISMATCH", "error")
                os.remove(local_path)
                return False

        return True

    except Exception as e:
        log(f"Download (FileID) fehlgeschlagen: {e}", "error")
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
                if holder.get("snapshot") == snapshot:
                    items.append({
                        "type": "file",
                        "relpath": holder.get("relpath"),
                        "sha256": sha256,
                        "fileid": obj.get("fileid"),
                        "anchor_path": anchor_path,
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

  # Download mit SHA256-Verifikation
  %(prog)s --manifest pcloud --snapshot 2025-11-23-082336 --out-dir /srv/pcloud-temp/restore --download --verify
        """
    )
    
    ap.add_argument("--manifest", required=True, help="'pcloud' oder lokaler Manifest-Pfad")
    ap.add_argument("--snapshot", help="Snapshot-Name")
    ap.add_argument("--list-snapshots", action="store_true", help="Verfügbare Snapshots aus dem pCloud-Index anzeigen und beenden")
    ap.add_argument("--out-dir", help="Lokales Restore-Ziel (Basis, Snapshot wird als Unterordner angelegt – nur flat-Modus verpflichtend)")

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

    if not args.snapshot:
        log("--snapshot ist erforderlich (oder --list-snapshots für Übersicht)", "error")
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
        if args.manifest.lower() == "pcloud":
            items = load_index_from_pcloud(cfg, args.dest_root, args.snapshot)
        else:
            log(f"Lade lokales Manifest: {args.manifest}")
            manifest = load_manifest(args.manifest, args.snapshot)
            items = manifest.get("items", [])
    except (IndexLoadError, ManifestLoadError) as e:
        log(str(e), "error")
        return 2
    
    # Filtern
    sel = [it for it in items if not args.filter or it.get("relpath", "").startswith(args.filter)]
    
    log(f"Snapshot: {args.snapshot} @ {args.dest_root}")
    log(f"Items (nach Filter): {len(sel)}")
    
    if not sel:
        log("Keine Items", "warn")
        return 0
    
    # Basis-Zielpfad: out_dir/snapshot (nur im flat-Modus)
    base_out_dir = os.path.join(args.out_dir, args.snapshot) if args.out_dir else None

    # Verify-only-Modus
    if args.verify_only:
        log("Starte Verify-only (keine Downloads)...")
        if not base_out_dir:
            log("--verify-only setzt --out-dir im flat-Modus voraus", "error")
            return 2
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
            print(f"  {it.get('relpath')} [{it.get('sha256', '?')[:8]}]")
        if len(sel) > 10:
            print(f"  ... ({len(sel) - 10} weitere)")
        return 0
    
    # Echtes Restore
    log(f"Starte Download: {len(sel)} Dateien von pCloud...")

    # Flat-Modus: direkt in base_out_dir/snapshot/relpath schreiben
    if args.mode == "flat":
        os.makedirs(base_out_dir, exist_ok=True)

        stats = {"success": 0, "failed": 0, "skipped": 0}
        sha_cache = {}  # Deduplizierung: {sha256 → local_path}

        for idx, item in enumerate(sel, 1):
            relpath = item.get("relpath", f"?_{idx}")
            sha256 = item.get("sha256")
            fileid = item.get("fileid")
            anchor_path = item.get("anchor_path")
            local_dest = os.path.join(base_out_dir, relpath)

            log(f"[{idx}/{len(sel)}] {relpath}")

            # Path-Traversal-Guard: sicherstellen, dass local_dest unterhalb von base_out_dir liegt
            expected_prefix_flat = os.path.join(base_out_dir) + os.sep
            normalized_local_dest = os.path.normpath(local_dest)
            if not normalized_local_dest.startswith(expected_prefix_flat):
                log(f"  ✗ Ungültiger relpath (Path-Traversal verhindert): {relpath}", "error")
                stats["failed"] += 1
                continue

            # Deduplizierung innerhalb eines Laufs (SHA-Caching)
            # Beide Dateien müssen lokal existieren, auch wenn sie denselben Inhalt haben!
            if sha256 and sha256 in sha_cache:
                cached_src = sha_cache[sha256]
                if cached_src != local_dest and not os.path.exists(local_dest):
                    log("  \u2192 Cache Hit (SHA256): erstelle Hardlink/Kopie")
                    os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)
                    try:
                        os.link(cached_src, local_dest)
                    except OSError:
                        import shutil
                        shutil.copy2(cached_src, local_dest)
                    stats["success"] += 1
                else:
                    log("  \u2192 Cache Hit (SHA256, Ziel bereits vorhanden)")
                    stats["skipped"] += 1
                continue

            # Bereits vorhandene lokale Datei prüfen und ggf. überspringen
            if os.path.exists(local_dest) and sha256 and args.verify:
                try:
                    hash_obj = hashlib.sha256()
                    with open(local_dest, "rb") as f:
                        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                            hash_obj.update(chunk)
                    local_sha = hash_obj.hexdigest()
                    if local_sha.lower() == sha256.lower():
                        log("  → OK (übersprungen, lokale Datei mit korrektem SHA vorhanden)")
                        stats["skipped"] += 1
                        # SHA-Caching aktualisieren
                        sha_cache[sha256] = local_dest
                        continue
                    else:
                        log("  → Lokale Datei existiert, aber SHA stimmt nicht, lade neu...", "warn")
                except Exception as e:
                    log(f"  → Lokale Datei konnte nicht geprüft werden, lade neu... ({e})", "warn")

            # Verzeichnis erstellen
            os.makedirs(os.path.dirname(local_dest) or ".", exist_ok=True)

            # Download von anchor_path (echte Datei, nicht Stub!)
            if anchor_path:
                verify_hash = sha256 if args.verify else None
                if download_file_with_verify(cfg, anchor_path, local_dest, verify_hash):
                    if sha256:
                        sha_cache[sha256] = local_dest
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
            else:
                log(f"  ✗ Kein anchor_path vorhanden", "error")
                stats["failed"] += 1

        log("=" * 60)
        log(f"Restore abgeschlossen (flat-Modus):")
        log(f"  ✓ Erfolgreich:  {stats['success']}")
        log(f"  ⊘ Dedupliziert: {stats['skipped']}")
        log(f"  ✗ Fehler:       {stats['failed']}")

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
            if not anchor_path:
                log("  ✗ Kein anchor_path vorhanden (Object-Store)", "error")
                stats["failed"] += 1
                continue

            log(f"  → Objekt fehlt lokal, lade nach {obj_path}...")
            verify_hash = sha256 if args.verify else None
            if download_file_with_verify(cfg, anchor_path, obj_path, verify_hash):
                stats["objects"] += 1
            else:
                log("  ✗ Download ins Object-Store fehlgeschlagen", "error")
                # Sicherheitsmaßnahme: defekte Datei entfernen
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
