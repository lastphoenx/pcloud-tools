#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ew_manifest.py – Lokales Snapshot-Manifest erzeugen (Files + Symlinks + Dirs).
"""
from __future__ import annotations
import os, sys, json, argparse, hashlib, time
from typing import Dict, Any, List

def sha256_file(p: str, buf: int=1024*1024) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()

def walk(root: str, follow_symlinks: bool=False) -> List[Dict[str,Any]]:
    items: List[Dict[str,Any]] = []
    base = os.path.abspath(root)
    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        rel_cur = os.path.relpath(cur, base).replace("\\","/")
        if rel_cur == ".": rel_cur = ""
        items.append({"relpath": rel_cur, "type": "dir"})
        for name in files:
            ab = os.path.join(cur, name)
            rel = (os.path.join(rel_cur, name) if rel_cur else name).replace("\\","/")
            st = os.lstat(ab)  # lstat! wir unterscheiden Symlinks
            if os.path.islink(ab):
                items.append({
                    "relpath": rel,
                    "type": "symlink",
                    "target": os.readlink(ab),
                    "lmode": oct(st.st_mode),
                })
                continue
            if not os.path.isfile(ab):
                continue
            try:
                items.append({
                    "relpath": rel,
                    "type": "file",
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "sha256": sha256_file(ab),
                    "source_path": os.path.abspath(ab),
                })
            except Exception as e:
                print(f"[warn] hash fail: {ab}: {e}", file=sys.stderr)
    return items

def main():
    ap = argparse.ArgumentParser(description="Snapshot-Manifest erzeugen.")
    ap.add_argument("--root", required=True, help="Lokales Quellverzeichnis")
    ap.add_argument("--snapshot", help="Snapshot-Name (Default: YYYYmmdd-HHMMSS)")
    ap.add_argument("--out", help="Manifest-Zieldatei (JSON). Default: stdout")
    ap.add_argument("--follow-symlinks", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"root not found: {root}", file=sys.stderr); sys.exit(2)
    snap = args.snapshot or time.strftime("%Y%m%d-%H%M%S")

    payload = {
        "snapshot": snap,
        "root": root,
        "created": int(time.time()),
        "items": walk(root, follow_symlinks=bool(args.follow_symlinks)),
        "schema": 1
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2); print()
    print(f"Manifest OK: snapshot={snap} items={len(payload['items'])}", file=sys.stderr)

if __name__ == "__main__":
    main()
