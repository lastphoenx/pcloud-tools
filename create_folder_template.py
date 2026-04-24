#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
create_folder_template.py — Einmaliges Setup-Script für den Ordner-Template-Cache

Erstellt /Backup/rtb_1to1/_folder_template/ als leeren Ordnerbaum,
indem die Struktur des neuesten (oder angegebenen) Snapshots server-seitig
kopiert wird (copyfolder mit copycontentonly=False, danach alle Dateien löschen).

BESSER: Wir nutzen listfolder(nofiles=1) und legen Ordner direkt an —
kein Datei-Löschaufwand, minimale API-Calls.

Usage:
    python create_folder_template.py [--dest-root /Backup/rtb_1to1] [--from-snapshot 2026-04-10-075334]
    python create_folder_template.py --dry-run
    python create_folder_template.py --update   # Nur fehlende/überflüssige Ordner sync
"""

import os
import sys
import json
import argparse
import concurrent.futures
import time
from collections import defaultdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcloud_bin_lib as pc

TEMPLATE_DIRNAME = "_folder_template"


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def list_remote_folders_from_snapshot(cfg: dict, snapshot_path: str) -> set:
    """
    Listet alle Ordner-relpaths im Snapshot (nofiles=1, recursive).
    Gibt ein Set von relativen Pfaden zurück (ohne führenden Slash).
    """
    folders = set()

    def _collect(obj: dict, parent: str = "") -> None:
        for child in (obj.get("contents") or []):
            if not child.get("isfolder"):
                continue
            name = child.get("name", "")
            relpath = f"{parent}/{name}" if parent else name
            folders.add(relpath)
            _collect(child, relpath)

    try:
        result = pc.call_with_backoff(
            pc.listfolder, cfg,
            path=snapshot_path,
            recursive=True,
            nofiles=True
        )
        _collect(result.get("metadata") or {})
    except Exception as e:
        _log(f"[error] listfolder fehlgeschlagen: {e}")
        sys.exit(1)

    return folders


def list_remote_folders_from_template(cfg: dict, template_path: str) -> set:
    """Wie list_remote_folders_from_snapshot, aber für das Template."""
    try:
        result = pc.call_with_backoff(
            pc.listfolder, cfg,
            path=template_path,
            recursive=True,
            nofiles=True
        )
    except Exception as e:
        if "2005" in str(e) or "not found" in str(e).lower():
            return set()  # Template existiert noch nicht
        raise

    folders = set()

    def _collect(obj: dict, parent: str = "") -> None:
        for child in (obj.get("contents") or []):
            if not child.get("isfolder"):
                continue
            name = child.get("name", "")
            relpath = f"{parent}/{name}" if parent else name
            folders.add(relpath)
            _collect(child, relpath)

    _collect(result.get("metadata") or {})
    return folders


def template_exists(cfg: dict, template_path: str) -> bool:
    """Prüft ob das Template remote existiert."""
    try:
        md = pc.stat_file(cfg, path=template_path, with_checksum=False)
        return bool(md and md.get("isfolder"))
    except Exception:
        return False


def create_folders_parallel(
    cfg: dict,
    template_path: str,
    folders: set,
    threads: int = 8,
    dry: bool = False
) -> int:
    """Legt Ordner parallel an, nach Tiefe sortiert (Parents zuerst)."""
    if not folders:
        return 0

    by_depth = defaultdict(list)
    for f in folders:
        by_depth[f.count("/")].append(f)

    created = 0
    total = len(folders)
    lock = __import__("threading").Lock()

    def _create(relpath: str) -> bool:
        nonlocal created
        full_path = f"{template_path}/{relpath}"
        if dry:
            print(f"  [dry] create: {full_path}")
            with lock:
                created += 1
            return True
        try:
            pc.call_with_backoff(pc.ensure_path, cfg, full_path)
            with lock:
                created += 1
                if created % 100 == 0 or created == total:
                    _log(f"  [{created}/{total}] Ordner angelegt...")
            return True
        except Exception as e:
            _log(f"  [warn] Fehler bei {relpath}: {e}")
            return False

    for depth in sorted(by_depth.keys()):
        batch = by_depth[depth]
        if threads > 1 and len(batch) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                list(ex.map(_create, batch))
        else:
            for f in batch:
                _create(f)

    return created


def delete_folders_sequential(
    cfg: dict,
    template_path: str,
    folders: set,
    dry: bool = False
) -> int:
    """Löscht überflüssige Ordner im Template (tiefste zuerst)."""
    if not folders:
        return 0

    deleted = 0
    # Tiefste zuerst (Kinder vor Eltern)
    for relpath in sorted(folders, key=lambda p: -p.count("/")):
        full_path = f"{template_path}/{relpath}"
        if dry:
            print(f"  [dry] delete: {full_path}")
            deleted += 1
            continue
        try:
            pc.delete_folder(cfg, path=full_path, recursive=False)
            deleted += 1
        except Exception as e:
            _log(f"  [warn] Konnte {relpath} nicht löschen: {e}")

    return deleted


def save_template_manifest(template_path: str, folders: set, snapshot_name: str) -> None:
    """Speichert lokales Manifest des Templates für schnellen Overlap-Check."""
    archive_dir = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    manifest_path = os.path.join(archive_dir, "folder_template_manifest.json")
    os.makedirs(archive_dir, exist_ok=True)
    data = {
        "template_path": template_path,
        "source_snapshot": snapshot_name,
        "updated_at": time.time(),
        "folder_count": len(folders),
        "folders": sorted(folders),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    _log(f"[manifest] Template-Manifest gespeichert: {manifest_path} ({len(folders)} Ordner)")


def main():
    parser = argparse.ArgumentParser(
        description="Einmaliges Setup-Script: Ordner-Template auf pCloud anlegen/aktualisieren",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("--dest-root", default="/Backup/rtb_1to1",
                        help="pCloud Ziel-Root (default: /Backup/rtb_1to1)")
    parser.add_argument("--from-snapshot",
                        help="Quell-Snapshot (default: neuester remote Snapshot)")
    parser.add_argument("--env-file",
                        default=os.path.join(os.path.dirname(__file__), ".env"),
                        help="Pfad zur .env-Datei")
    parser.add_argument("--threads", type=int, default=8,
                        help="Parallele Threads für Ordner-Anlage (default: 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nur anzeigen, nichts ändern")
    parser.add_argument("--update", action="store_true",
                        help="Template aktualisieren (add/delete Delta, kein Rebuild)")
    args = parser.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest_root = pc._norm_remote_path(args.dest_root).rstrip("/")
    snapshots_root = f"{dest_root}/_snapshots"
    template_path = f"{dest_root}/{TEMPLATE_DIRNAME}"

    _log(f"=== Folder-Template Setup ===")
    _log(f"dest_root:     {dest_root}")
    _log(f"template_path: {template_path}")
    _log(f"mode:          {'dry-run' if args.dry_run else ('update' if args.update else 'create/rebuild')}")

    # Quell-Snapshot bestimmen
    source_snapshot = args.from_snapshot
    if not source_snapshot:
        _log(f"[source] Suche neuesten remote Snapshot...")
        try:
            js = pc._rest_get(cfg, "listfolder", {"path": snapshots_root, "nofiles": 1})
            names = [
                c["name"] for c in (js.get("metadata") or {}).get("contents", [])
                if c.get("isfolder") and c.get("name") != "_index"
            ]
            if not names:
                _log(f"[error] Keine Snapshots gefunden unter {snapshots_root}")
                sys.exit(1)
            source_snapshot = sorted(names)[-1]  # neuester
        except Exception as e:
            _log(f"[error] Konnte Snapshots nicht laden: {e}")
            sys.exit(1)

    source_path = f"{snapshots_root}/{source_snapshot}"
    _log(f"[source] Quell-Snapshot: {source_snapshot}")

    # Ordner aus Quell-Snapshot laden
    _log(f"[scan] Lade Ordnerstruktur aus {source_snapshot} (nofiles=1)...")
    t0 = time.time()
    source_folders = list_remote_folders_from_snapshot(cfg, source_path)
    _log(f"[scan] {len(source_folders)} Ordner gefunden ({time.time()-t0:.1f}s)")

    # Template-Status prüfen
    tmpl_exists = template_exists(cfg, template_path)

    if tmpl_exists and args.update:
        # UPDATE: Nur Delta (add/delete)
        _log(f"[update] Template existiert – berechne Delta...")
        t0 = time.time()
        tmpl_folders = list_remote_folders_from_template(cfg, template_path)
        _log(f"[update] Template hat {len(tmpl_folders)} Ordner ({time.time()-t0:.1f}s)")

        to_add = source_folders - tmpl_folders
        to_delete = tmpl_folders - source_folders
        shared = source_folders & tmpl_folders

        _log(f"[update] Identisch: {len(shared)}, Neu: {len(to_add)}, Zu löschen: {len(to_delete)}")

        if to_add:
            _log(f"[update] Lege {len(to_add)} neue Ordner an...")
            create_folders_parallel(cfg, template_path, to_add, threads=args.threads, dry=args.dry_run)

        if to_delete:
            _log(f"[update] Lösche {len(to_delete)} überflüssige Ordner...")
            delete_folders_sequential(cfg, template_path, to_delete, dry=args.dry_run)

    elif not tmpl_exists or not args.update:
        # CREATE / REBUILD
        if tmpl_exists:
            _log(f"[rebuild] Template existiert bereits – rebuild (--update für Delta-Modus)")
        else:
            _log(f"[create] Template existiert noch nicht – erstelle neu...")

        if not args.dry_run:
            pc.call_with_backoff(pc.ensure_path, cfg, template_path)

        _log(f"[create] Lege {len(source_folders)} Ordner an ({args.threads} Threads)...")
        t0 = time.time()
        created = create_folders_parallel(
            cfg, template_path, source_folders,
            threads=args.threads, dry=args.dry_run
        )
        _log(f"[create] ✓ {created} Ordner angelegt ({time.time()-t0:.1f}s)")

    # Lokales Manifest speichern
    if not args.dry_run:
        save_template_manifest(template_path, source_folders, source_snapshot)

    _log(f"[done] Template unter {template_path} bereit")
    _log(f"[done] Verwende '--update' bei zukünftigen Ordner-Änderungen")


if __name__ == "__main__":
    main()
