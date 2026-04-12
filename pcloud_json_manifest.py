#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_json_manifest.py – erzeugt ein lokales Snapshot-Manifest (Schema v2).

Features
- Verzeichnisbaum unter --root erfassen (dirs, files, symlinks)
- Pro Item: snapshot, relpath, type, size/mtime (bei file), sha256 (optional), ext, inode(dev,ino,nlink)
- Optionen für Hash, Hardlink-/Symlink-Handhabung

Beispiel
  SNAP=$(readlink -f /mnt/backup/rtb_nas/latest)
  python pcloud_json_manifest.py \
    --root "$SNAP" \
    --out /srv/pcloud-temp/snap.json \
    --hash sha256 \
    --no-follow-hardlinks \
    --store-hardlink-target \
    --store-symlink-target \
    --follow-symlinks
"""

from __future__ import annotations
import os, sys, json, argparse, hashlib, time
from typing import Dict, Any, List, Tuple, Optional

# ---------------- util ----------------

def sha256_file(p: str, buf: int = int(os.environ.get("MANIFEST_HASH_BUFSIZE", 4*1024*1024))) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------- walker ----------------

def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}"
        b /= 1024

def walk(root: str,
         snapshot: str,
         *,
         hash_algo: Optional[str],          # "sha256" oder None
         follow_symlinks: bool,
         follow_hardlinks: bool,
         store_hardlink_target: bool,
         store_symlink_target: bool,
         progress_interval: float = 30.0) -> List[Dict[str, Any]]:

    items: List[Dict[str, Any]] = []
    base = os.path.abspath(root)

    # Für optionale Hardlink-Zielverfolgung: erste Sicht pro (dev,ino)
    first_seen: dict[tuple[int,int], str] = {}

    # Für Fortschritts-Reporting: Gesamtgröße vorab ermitteln
    total_bytes = 0
    total_files = 0
    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        for name in files:
            ab = os.path.join(cur, name)
            if not os.path.islink(ab) and os.path.isfile(ab):
                try:
                    total_bytes += os.path.getsize(ab)
                    total_files += 1
                except OSError:
                    pass
    print(f"[manifest] Starte: {total_files} Dateien, {_fmt_bytes(total_bytes)}", file=sys.stderr)

    done_files = 0
    done_bytes = 0
    t_start = time.monotonic()
    t_last_progress = t_start

    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        rel_cur = os.path.relpath(cur, base).replace("\\", "/")
        if rel_cur == ".": rel_cur = ""

        # DIR
        items.append({
            "snapshot": snapshot,
            "relpath": rel_cur,
            "type": "dir",
        })

        # FILES
        for name in files:
            ab = os.path.join(cur, name)
            rel = (os.path.join(rel_cur, name) if rel_cur else name).replace("\\", "/")

            try:
                st = os.lstat(ab)  # lstat! (Symlink-Metadaten)
            except FileNotFoundError:
                # Zwischenzeitlich verschwunden – überspringen
                continue

            # Symlink?
            if os.path.islink(ab):
                entry: Dict[str, Any] = {
                    "snapshot": snapshot,
                    "relpath": rel,
                    "type": "symlink",
                    "lmode": oct(st.st_mode),
                }
                if store_symlink_target:
                    try:
                        entry["target"] = os.readlink(ab)
                    except OSError as e:
                        entry["target_error"] = str(e)
                items.append(entry)
                continue

            # Nur reguläre Dateien erfassen (keine Sockets/Devices/…)
            if not os.path.isfile(ab):
                continue

            # Inode/Hardlink-Infos
            dev = int(st.st_dev); ino = int(st.st_ino); nlink = int(st.st_nlink)
            inode_obj = {"dev": dev, "ino": ino, "nlink": nlink}

            # Extension bestimmen
            _, ext = os.path.splitext(rel)
            ext = ext if ext else None

            # Hash optional
            file_hash = None
            if hash_algo == "sha256":
                try:
                    file_hash = sha256_file(ab)
                except Exception as e:
                    print(f"[warn] hash fail: {ab}: {e}", file=sys.stderr)

            entry: Dict[str, Any] = {
                "snapshot": snapshot,
                "type": "file",
                "relpath": rel,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
                "source_path": os.path.abspath(ab),
                "ext": ext,
                "inode": inode_obj,
            }
            if file_hash:
                entry["sha256"] = file_hash

            done_files += 1
            done_bytes += int(st.st_size)

            # Fortschritt alle progress_interval Sekunden
            now = time.monotonic()
            if now - t_last_progress >= progress_interval:
                elapsed = now - t_start
                pct_files = done_files / total_files * 100 if total_files else 0
                pct_bytes = done_bytes / total_bytes * 100 if total_bytes else 0
                eta_s = (elapsed / done_bytes * (total_bytes - done_bytes)) if done_bytes else 0
                eta_str = f"~{int(eta_s/60)}min" if eta_s > 60 else f"~{int(eta_s)}s"
                print(
                    f"[manifest] {done_files}/{total_files} Dateien ({pct_files:.0f}%) | "
                    f"{_fmt_bytes(done_bytes)} / {_fmt_bytes(total_bytes)} ({pct_bytes:.0f}%) | "
                    f"{eta_str} verbleibend",
                    file=sys.stderr, flush=True
                )
                t_last_progress = now

            # Hardlink-Ziel optional festhalten
            if store_hardlink_target and nlink > 1:
                key = (dev, ino)
                if key in first_seen:
                    entry["hardlink_of"] = first_seen[key]  # relpath der ersten Sicht
                else:
                    first_seen[key] = rel
                    entry["hardlink_master"] = True

            items.append(entry)

    return items

# ---------------- main ----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Lokales Snapshot-Manifest erzeugen (Schema v2).")

    ap.add_argument("--root", required=True, help="Lokales Quellverzeichnis (z. B. ein RTB-Snapshot)")
    ap.add_argument("--snapshot", help="Snapshot-Name (Default: YYYYmmdd-HHMMSS)")
    ap.add_argument("--out", help="Manifest-Zieldatei (JSON). Default: stdout")

    # Verhalten
    ap.add_argument("--hash", choices=["sha256", "none"], default="sha256", help="Datei-Hash aufnehmen (Default: sha256)")
    ap.add_argument("--follow-symlinks", action="store_true", help="Symlinks als Dateien traversieren (Default: nein)")
    ap.add_argument("--no-follow-hardlinks", dest="follow_hardlinks", action="store_false",
                    help="Hardlinks NICHT zusammenführen (nur Info, Default: folgen=True)")
    ap.set_defaults(follow_hardlinks=True)
    ap.add_argument("--store-hardlink-target", action="store_true",
                    help="relpath des ersten Auftretens (dev,ino) mitschreiben")
    ap.add_argument("--store-symlink-target", action="store_true",
                    help="Symlink-Ziel (readlink) mitschreiben")

    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"root not found: {root}", file=sys.stderr)
        sys.exit(2)

    snap = args.snapshot or time.strftime("%Y%m%d-%H%M%S")
    hash_algo = None if args.hash == "none" else args.hash

    payload = {
        "schema": 2,
        "snapshot": snap,
        "root": root,
        "created": int(time.time()),
        "hash": (hash_algo or "none"),
        "follow_symlinks": bool(args.follow_symlinks),
        "follow_hardlinks": bool(args.follow_hardlinks),
        "store_hardlink_target": bool(args.store_hardlink_target),
        "store_symlink_target": bool(args.store_symlink_target),
        "items": walk(
            root,
            snap,
            hash_algo=hash_algo,
            follow_symlinks=bool(args.follow_symlinks),
            follow_hardlinks=bool(args.follow_hardlinks),
            store_hardlink_target=bool(args.store_hardlink_target),
            store_symlink_target=bool(args.store_symlink_target),
        )
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()

    print(f"Manifest OK: snapshot={snap} items={len(payload['items'])}", file=sys.stderr)

if __name__ == "__main__":
    main()
