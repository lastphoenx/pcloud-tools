# Dateiname: pcloud_upload.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_upload.py – Dünner CLI-Wrapper um pcloud_bin_lib (Binary-API).

Zweck:
- Einheitlicher, wartbarer Upload-Einstiegspunkt für Systemd-Services & Cron.
- Nutzt die Library-Funktion `upload_chunked` (keine Duplikation des Protokoll-Codes).
- Kann Ordner rekursiv hochladen (optional), inkl. Zielpfad-Erzeugung.

Beispiele:
  pcloud_upload.py --env-file /opt/entropywatcher/common.env \
      --env-dir /opt/entropywatcher/profiles --profile nas \
      --dest-path /backups/nas borg/archives/2025-10-05-00-00/

  pcloud_upload.py --dest-folderid 1234567 --verify --progress myfile.tar.zst
"""
from __future__ import annotations
import os, sys
from typing import Any, Dict
import argparse
import pcloud_bin_lib as pc

def _progress_fn_factory(name: str):
    def _p(sent: int, total: int):
        pct = (sent/total*100.0) if total else 0.0
        sys.stderr.write(f"\r{name}  {pct:5.1f}%  {sent}/{total} bytes")
        sys.stderr.flush()
    return _p

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Datei(en) zu pCloud hochladen (Binary-API, Lib-Wrapper).", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("paths", nargs="+", help="Dateien (oder Verzeichnisse mit --recursive)")
    ap.add_argument("--env-file", help="Pfad zu .env (Basis)")
    ap.add_argument("--env-dir", help="Zusätzlicher Env-Ordner (Profiles)")
    ap.add_argument("--profile", help="Env-Profilname (lädt <env-dir>/<name>.env)")
    ap.add_argument("--dest-path", help="Zielpfad in pCloud (z.B. /backups/nas)")
    ap.add_argument("--dest-folderid", type=int, help="Alternativ: Zielordner-ID")
    ap.add_argument("--filename", help="Ziel-Dateiname (nur 1 Datei)")
    ap.add_argument("--rename-if-exists", action="store_true", help="Bestehende Dateien umbenennen statt Fehler")
    ap.add_argument("--verify", action="store_true", help="Nach Upload checksumfile prüfen")
    ap.add_argument("--recursive", action="store_true", help="Ordner rekursiv hochladen")
    ap.add_argument("--progress", action="store_true", help="Fortschritt anzeigen")
    ap.add_argument("--chunk-size", type=int, default=8*1024*1024, help="Chunk-Größe in Bytes")
    args = ap.parse_args(argv)

    cfg = pc.build_config(args.env_file, args.env_dir, args.profile)
    cfg["host"] = pc.get_apiserver_nearest(cfg)

    if args.dest_folderid:
        dest_fid = int(args.dest_folderid)
    else:
        if not args.dest_path:
            print("[abbruch] Bitte --dest-path oder --dest-folderid angeben", file=sys.stderr); return 2
        meta = pc.ensure_path(cfg, pc._norm_remote_path(args.dest_path))
        dest_fid = int(meta["folderid"])

    to_send: list[tuple[str,str|None]] = []
    for p in args.paths:
        if os.path.isdir(p):
            if not args.recursive:
                print(f"[abbruch] Ordner ohne --recursive: {p}", file=sys.stderr); return 2
            for root, _, files in os.walk(p):
                for n in files:
                    to_send.append((os.path.join(root,n), None))
        else:
            to_send.append((p, args.filename if len(args.paths)==1 else None))

    for local_path, forced_name in to_send:
        if not os.path.isfile(local_path):
            print(f"[skip] nicht gefunden/keine Datei: {local_path}", file=sys.stderr); continue
        prog = _progress_fn_factory(os.path.basename(local_path)) if args.progress else None
        try:
            top = pc.upload_chunked(cfg, local_path, dest_fid, filename=forced_name, chunk_size=args.chunk_size, progress=prog)
        except pc.PCloudError as e:
            if prog: sys.stderr.write("\n")
            print(f"[fehler] Upload fehlgeschlagen ({e})", file=sys.stderr); return 10
        if prog: sys.stderr.write("\n")
        if args.verify:
            try:
                pc.checksumfile(cfg, fileid=int(top.get("fileid") or 0))
                print(f"Verify OK: {os.path.basename(local_path)}")
            except Exception as e:
                print(f"[warn] verify fehlgeschlagen: {e}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
