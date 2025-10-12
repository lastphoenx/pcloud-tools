#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ew_restore_plan.py – Dateien aus Manifest (per Object-Store) lokal wiederherstellen.
"""
from __future__ import annotations
import os, sys, json, argparse, urllib.parse, urllib.request
import pcloud_bin_lib as pc

def rest_getfilelink_by_path(cfg: dict, path: str, forcedownload: int = 1) -> str:
    base = "https://api.pcloud.com/getfilelink"
    q = urllib.parse.urlencode({
        "access_token": cfg["token"],
        "path": pc._norm_remote_path(path),
        "forcedownload": forcedownload
    })
    with urllib.request.urlopen(base + "?" + q, timeout=int(cfg["timeout"])) as r:
        data = json.loads(r.read().decode("utf-8"))
    if int(data.get("result", 1)) != 0:
        raise RuntimeError(f"getfilelink failed: {data}")
    hosts = data.get("hosts") or []
    p = data.get("path")
    if not hosts or not p:
        raise RuntimeError("getfilelink: incomplete response.")
    return f"https://{hosts[0]}{p}"

def download_to(url: str, dst: str, timeout: int = 60) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dst, "wb") as f:
        f.write(r.read())

def main():
    ap = argparse.ArgumentParser(description="Restore-Plan bauen oder herunterladen.")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest-root", required=True, help="pCloud Basis, z.B. /Backup")
    ap.add_argument("--objects-dir", default="_objects")
    ap.add_argument("--snapshots-dir", default="_snapshots")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--filter", help="nur diese relpath (Präfix-Match) wiederherstellen")
    ap.add_argument("--out-dir", required=True, help="lokales Restore-Ziel")
    ap.add_argument("--download", action="store_true", help="statt Plan nur: wirklich downloaden")
    ap.add_argument("--env-file"); ap.add_argument("--profile"); ap.add_argument("--env-dir")
    ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--timeout", type=int)
    ap.add_argument("--device"); ap.add_argument("--token")
    args = ap.parse_args()

    cfg = pc.effective_config(
        env_file=args.env_file, env_dir=args.env_dir, profile=args.profile,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token}
    )

    manifest = json.load(open(args.manifest, "r", encoding="utf-8"))
    if manifest["snapshot"] != args.snapshot:
        print(f"[warn] manifest snapshot={manifest['snapshot']} != requested {args.snapshot}", file=sys.stderr)

    sel = []
    for it in manifest["items"]:
        if it["type"] not in ("file","symlink"):
            continue
        if args.filter and not it["relpath"].startswith(args.filter):
            continue
        sel.append(it)

    print(f"restore items: {len(sel)}")

    objects_root = f"{pc._norm_remote_path(args.dest_root)}/{args.objects_dir}".replace("//","/")

    for it in sel:
        rel = it["relpath"]
        out = os.path.join(args.out_dir, rel)
        if it["type"] == "symlink":
            tgt = it["target"]
            print(f"[symlink] {out} -> {tgt}")
            if args.download:
                os.makedirs(os.path.dirname(out), exist_ok=True)
                try:
                    if os.path.lexists(out): os.remove(out)
                except Exception: pass
                try:
                    os.symlink(tgt, out)
                except Exception as e:
                    with open(out + ".symlink.txt", "w", encoding="utf-8") as f:
                        f.write(tgt + "\n")
            continue

        sha = it["sha256"]
        obj_path = f"{objects_root}/{sha[:2]}/{sha}"
        if not args.download:
            print(f"[plan] {obj_path}  ->  {out}")
        else:
            url = rest_getfilelink_by_path(cfg, obj_path, forcedownload=1)
            print(f"[get] {url} -> {out}")
            download_to(url, out, timeout=int(cfg["timeout"]))

    print("Done.")

if __name__ == "__main__":
    main()
