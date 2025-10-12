#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ew_push_to_pcloud.py – Manifest in pCloud hochschieben (objects + snapshot-stubs).
"""
from __future__ import annotations
import os, sys, json, argparse, tempfile
from typing import Dict, Any
import pcloud_bin_lib as pc

def _ensure_path(cfg: dict, path: str) -> int:
    return pc.ensure_path(cfg, path)

def _exists_remote_file(cfg: dict, path: str) -> bool:
    try:
        md = pc.stat_file(cfg, path=path, with_checksum=False)
        return bool(md and not md.get("isfolder"))
    except Exception:
        return False

def _upload_text(cfg: dict, dest_folderid: int, filename: str, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        pc.upload_streaming(cfg, tmp_path, dest_folderid=dest_folderid, filename=filename)
    finally:
        try: os.remove(tmp_path)
        except: pass

def main():
    ap = argparse.ArgumentParser(description="Push Manifest nach pCloud")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--dest-root", required=True, help="z.B. /Backup")
    ap.add_argument("--objects-dir", default="_objects")
    ap.add_argument("--snapshots-dir", default="_snapshots")
    ap.add_argument("--dry-run", action="store_true")
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
    snap = manifest["snapshot"]
    dest_root = pc._norm_remote_path(args.dest_root)
    objects_root = f"{dest_root}/{args.objects_dir}".replace("//","/")
    snaps_root   = f"{dest_root}/{args.snapshots_dir}".replace("//","/")
    snap_root    = f"{snaps_root}/{snap}".replace("//","/")

    if not args.dry_run:
        _ensure_path(cfg, objects_root)
        _ensure_path(cfg, snap_root)
    print(f"[plan] objects={objects_root} snapshot={snap_root}")

    shas = [it["sha256"] for it in manifest["items"] if it.get("type")=="file"]
    uniq = sorted(set(shas))
    uploaded = 0; skipped = 0
    for sha in uniq:
        sub = sha[:2]
        obj_path = f"{objects_root}/{sub}/{sha}".replace("//","/")
        if _exists_remote_file(cfg, obj_path):
            skipped += 1
            continue
        any_item = next(it for it in manifest["items"] if it.get("type")=="file" and it["sha256"]==sha)
        src = any_item["source_path"]
        if args.dry_run:
            print(f"[dry] upload object: {obj_path}  <- {src}")
        else:
            fid = _ensure_path(cfg, f"{objects_root}/{sub}")
            pc.upload_streaming(cfg, src, dest_folderid=fid, filename=sha)
        uploaded += 1

    print(f"objects: uploaded={uploaded} skipped={skipped}")

    stubs = 0
    for it in manifest["items"]:
        rel = it["relpath"]
        snap_dir = os.path.dirname(rel)
        target_dir = snap_root if not snap_dir else f"{snap_root}/{snap_dir}".replace("//","/")
        if not args.dry_run:
            _ensure_path(cfg, target_dir)

        if it["type"] == "dir":
            continue

        if it["type"] == "file":
            sha = it["sha256"]; size = it["size"]; mtime = it.get("mtime")
            obj_path = f"{objects_root}/{sha[:2]}/{sha}"
            stub = {
                "type": "link",
                "sha256": sha,
                "size": size,
                "mtime": mtime,
                "object_path": obj_path
            }
            fname = os.path.basename(rel) + ".meta.json"
            if args.dry_run:
                print(f"[dry] stub: {target_dir}/{fname} -> {obj_path}")
            else:
                fid = pc.ensure_path(cfg, target_dir)
                _upload_text(cfg, fid, fname, json.dumps(stub, ensure_ascii=False, indent=2))
            stubs += 1
        elif it["type"] == "symlink":
            stub = {
                "type": "symlink",
                "target": it["target"],
                "lmode": it.get("lmode")
            }
            fname = os.path.basename(rel) + ".symlink.json"
            if args.dry_run:
                print(f"[dry] stub: {target_dir}/{fname} -> (symlink:{it['target']})")
            else:
                fid = pc.ensure_path(cfg, target_dir)
                _upload_text(cfg, fid, fname, json.dumps(stub, ensure_ascii=False, indent=2))
            stubs += 1

    print(f"stubs: {stubs} (snapshot={snap})")

if __name__ == "__main__":
    main()
