#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_delete_bin.py – gezieltes Löschen auf pCloud (Binary API)

Features
- Dateien löschen per: --file-id | --file-path
- Ordner löschen per:  --folder-id | --folder-path
- Schutz: --recursive nur mit Bestätigung (--confirm-name); Root "/" wird abgelehnt
- --dry-run zeigt nur an
- Optional: --clear leert den Papierkorb danach (Trash-Clear)

Voraussetzung:
- pcloud_bin_lib.py im Python-Pfad
"""
import argparse, sys
from typing import Optional, Dict, Any
import pcloud_bin_lib as pc

def _norm_remote_path(p: str) -> str:
    if not p:
        return "/"
    p = p.strip()
    if not p.startswith("/"):
        p = "/" + p
    p = p.replace("//","/")
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p or "/"

def _delete_file(cfg: Dict[str,Any], *, fileid: Optional[int]=None, path: Optional[str]=None):
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if fileid is not None:
        params["fileid"] = int(fileid)
    elif path is not None:
        params["path"] = _norm_remote_path(path)
    else:
        raise ValueError("deletefile: fileid oder path angeben.")
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "deletefile", params)
    pc._expect_ok(top)
    return top

def _delete_folder(cfg: Dict[str,Any], *, folderid: Optional[int]=None, path: Optional[str]=None, recursive: bool=False):
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if folderid is not None:
        params["folderid"] = int(folderid)
    elif path is not None:
        p = _norm_remote_path(path)
        if p == "/":
            raise RuntimeError("Sicherheitsbremse: Root '/' kann nicht gelöscht werden.")
        params["path"] = p
    else:
        raise ValueError("deletefolder: folderid oder path angeben.")
    if recursive:
        params["recursive"] = 1
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "deletefolder", params)
    pc._expect_ok(top)
    return top

def _trash_clear(cfg: Dict[str,Any]):
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "trash_clear", params)
    pc._expect_ok(top)
    return top

def main():
    ap = argparse.ArgumentParser(description="Gezieltes Löschen (Datei/Ordner) auf pCloud.")
    ap.add_argument("--env-file", help=".env Pfad (optional).")
    ap.add_argument("--profile", help="Profilname (lädt profiles/<name>.env).")
    ap.add_argument("--env-dir", help="Basisordner für .env/profiles.")

    # Datei-Target
    g1 = ap.add_mutually_exclusive_group()
    g1.add_argument("--file-id", type=int, help="fileid der zu löschenden Datei")
    g1.add_argument("--file-path", help="Pfad der zu löschenden Datei (z. B. /Backup/foo.txt)")

    # Ordner-Target
    g2 = ap.add_mutually_exclusive_group()
    g2.add_argument("--folder-id", type=int, help="folderid des zu löschenden Ordners")
    g2.add_argument("--folder-path", help="Pfad des zu löschenden Ordners")

    ap.add_argument("--recursive", action="store_true", help="Ordner rekursiv löschen (benötigt --confirm-name)")
    ap.add_argument("--confirm-name", help="Bestätigung des Ordnernamens für --recursive (z. B. 'tmp')")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht löschen")
    ap.add_argument("--clear", action="store_true", help="Papierkorb nach dem Löschen leeren")

    # Overrides
    ap.add_argument("--host", help="Override Host")
    ap.add_argument("--port", type=int, help="Override Port")
    ap.add_argument("--timeout", type=int, help="Override Timeout (Sek.)")
    ap.add_argument("--device", help="Override Device")
    ap.add_argument("--token", help="Override Token")

    args = ap.parse_args()

    # Mindestens ein Target?
    if not any([args.file_id, args.file_path, args.folder_id, args.folder_path]):
        print("Fehler: Bitte --file-id / --file-path oder --folder-id / --folder-path angeben.", file=sys.stderr)
        sys.exit(2)

    cfg = pc.effective_config(
        env_file=args.env_file,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token},
        profile=args.profile,
        env_dir=args.env_dir
    )

    # Datei löschen
    if args.file_id or args.file_path:
        label = f"fileid={args.file_id}" if args.file_id else _norm_remote_path(args.file_path)
        print(f"[Plan] Datei löschen: {label}")
        if args.dry_run:
            print("[dry-run] deletefile wird NICHT ausgeführt.")
        else:
            _delete_file(cfg, fileid=args.file_id, path=args.file_path)
            print("[OK] Datei gelöscht.")

    # Ordner löschen
    if args.folder_id or args.folder_path:
        p = _norm_remote_path(args.folder_path) if args.folder_path else None
        # Root-Schutz
        if (p == "/") or (args.folder_id == 0):
            print("Sicherheitsbremse: Root '/' kann nicht gelöscht werden.", file=sys.stderr)
            sys.exit(2)
        # Recursive-Schutz
        if args.recursive:
            # Bestätigung nötig
            name_expected = (p.rsplit("/",1)[-1] if p else str(args.folder_id))
            if not args.confirm_name or args.confirm_name != name_expected:
                print(f"Sicherheitsbremse: --recursive benötigt --confirm-name {name_expected}", file=sys.stderr)
                sys.exit(2)
        label = f"folderid={args.folder_id}" if args.folder_id else p
        print(f"[Plan] Ordner löschen: {label}  (recursive={bool(args.recursive)})")
        if args.dry_run:
            print("[dry-run] deletefolder wird NICHT ausgeführt.")
        else:
            _delete_folder(cfg, folderid=args.folder_id, path=args.folder_path, recursive=args.recursive)
            print("[OK] Ordner gelöscht.")

    # Trash leeren
    if args.clear:
        if args.dry_run:
            print("[dry-run] trash_clear wird NICHT ausgeführt.")
        else:
            _trash_clear(cfg)
            print("[OK] Papierkorb geleert.")

if __name__ == "__main__":
    main()
