#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_sync_all_snapshots.py – Orchestriert vollständigen Snapshot-Sync:
- ermittelt Delta (neue/gelöschte RTB-Snapshots)
- pusht neue Snapshots via vorhandener Tools
- führt Retention im 1:1-Modus serverseitig (copyfile + delete) durch
"""

from __future__ import annotations
import os, sys, json, argparse, subprocess
from typing import Any, Dict, List, Set
import pcloud_bin_lib as pc

# ---------------- helpers ----------------

def _snapshots_local(rtb_root: str) -> Set[str]:
    """Alle Snapshot-Namen (Verzeichnisse; 'latest' ausgeschlossen)."""
    out: Set[str] = set()
    for n in os.listdir(rtb_root):
        p = os.path.join(rtb_root, n)
        if n == "latest":  # RTB-Symlink
            continue
        if os.path.isdir(p):
            out.add(n)
    return out

def _snapshots_remote(cfg: dict, dest_root: str) -> Set[str]:
    """Alle Remote-Snapshots unter <dest>/_snapshots (Ordnernamen, _index auslassen)."""
    root = f"{pc._norm_remote_path(dest_root)}/_snapshots"
    out: Set[str] = set()
    try:
        top = pc.listfolder(cfg, path=root, recursive=False, nofiles=True, showpath=False)
        for it in (top.get("metadata", {}) or {}).get("contents", []) or []:
            if it.get("isfolder") and it.get("name") not in ("_index",):
                out.add(it["name"])
    except Exception:
        pass
    return out

def _ensure_parent_dirs(cfg: dict, remote_path: str) -> None:
    parent = os.path.dirname(pc._norm_remote_path(remote_path)) or "/"
    pc.ensure_path(cfg, parent)

def _stat_file_safe(cfg: dict, *, path: str) -> dict|None:
    try:
        return pc.stat_file(cfg, path=pc._norm_remote_path(path), with_checksum=False) or {}
    except Exception:
        return None

# ------- 1:1 Retention: Promotion + Löschen (serverseitig) -------

def _load_remote_index(cfg: dict, dest_root: str) -> dict:
    """content_index.json lesen oder leeren Index liefern."""
    idx_path = f"{pc._norm_remote_path(dest_root)}/_snapshots/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = json.loads(txt)
        if "items" not in j: j["items"] = {}
        return j
    except Exception:
        return {"version": 1, "items": {}}

def _save_remote_index(cfg: dict, dest_root: str, idx: dict, dry: bool) -> None:
    idx_path = f"{pc._norm_remote_path(dest_root)}/_snapshots/_index/content_index.json"
    txt = json.dumps(idx, ensure_ascii=False, indent=2)
    if dry:
        print(f"[dry] write {idx_path} (items={len(idx.get('items',{}))})")
        return
    _ensure_parent_dirs(cfg, idx_path)
    pc.put_textfile(cfg, path=idx_path, text=txt)

def retention_sync_1to1(cfg: dict, dest_root: str, *, local_snaps: Set[str], dry: bool=False) -> None:
    """
    Entfernt entfernte Snapshots, die lokal fehlen, mit Promotion:
    - Für jeden SHA-Eintrag: wenn Anchor in zu löschendem Snapshot liegt, auf verbleibenden Holder promoten (copyfile)
    - Danach Snapshot-Ordner deletefolderrecursive
    """
    snapshots_root = f"{pc._norm_remote_path(dest_root)}/_snapshots"
    remote_snaps = _snapshots_remote(cfg, dest_root)
    to_delete = sorted(s for s in remote_snaps if s not in local_snaps)
    if not to_delete:
        return

    idx = _load_remote_index(cfg, dest_root)
    items = idx.get("items", {})

    for sdel in to_delete:
        del_prefix = f"{snapshots_root}/{sdel}/"
        # alle SHA-Knoten prüfen
        for sha, node in list(items.items()):
            anchor = node.get("anchor_path") or ""
            if not anchor.startswith(del_prefix):
                continue

            holders = [h for h in (node.get("holders") or []) if h.get("snapshot") != sdel]
            if not holders:
                # keine weitere Referenz -> Eintrag entfernen (Datei verschwindet mit Snapshot)
                del items[sha]
                continue

            # neuen Anchor wählen (jüngster Holder)
            new_holder = max(holders, key=lambda h: h.get("snapshot") or "")
            new_path = f"{snapshots_root}/{new_holder['snapshot']}/{new_holder['relpath']}"

            if dry:
                print(f"[dry] promote {sha}: {anchor} -> {new_path}")
                node["anchor_path"] = new_path
            else:
                # fileid der Quelle -> copyfile
                src_fid = node.get("fileid")
                if not src_fid:
                    m = _stat_file_safe(cfg, path=anchor)
                    src_fid = m.get("fileid") if m else None
                if not src_fid:
                    raise RuntimeError(f"promotion needs fileid for {anchor}")
                _ensure_parent_dirs(cfg, new_path)
                pc.copyfile(cfg, src_fileid=int(src_fid), dest_path=new_path)
                m2 = _stat_file_safe(cfg, path=new_path)
                node["fileid"] = m2.get("fileid") if m2 else src_fid
                node["anchor_path"] = new_path

            node["holders"] = holders

        # Snapshot-Ordner löschen
        rmpath = f"{snapshots_root}/{sdel}"
        if dry:
            print(f"[dry] delete remote snapshot: {rmpath}")
        else:
            pc.deletefolder_recursive(cfg, path=rmpath)

    _save_remote_index(cfg, dest_root, idx, dry=dry)

# ---------------- orchestrator ----------------

def run_manifest(script: str, snap_root: str, out_json: str, *, hash_alg: str,
                 follow_symlinks: bool, follow_hardlinks: bool,
                 store_hardlink_target: bool, store_symlink_target: bool) -> None:
    cmd = [
        sys.executable, script,
        "--root", snap_root,
        "--out", out_json,
        "--hash", hash_alg,
    ]
    if follow_symlinks:
        cmd.append("--follow-symlinks")
    if not follow_hardlinks:
        cmd.append("--no-follow-hardlinks")
    if store_hardlink_target:
        cmd.append("--store-hardlink-target")
    if store_symlink_target:
        cmd.append("--store-symlink-target")
    subprocess.check_call(cmd)

def run_push(script: str, manifest_json: str, dest_root: str, *, env_file: str|None,
             snapshot_mode: str, dry: bool) -> None:
    cmd = [
        sys.executable, script,
        "--manifest", manifest_json,
        "--dest-root", dest_root,
        "--snapshot-mode", snapshot_mode,
    ]
    if env_file:
        cmd += ["--env-file", env_file]
    if dry:
        cmd.append("--dry-run")
    subprocess.check_call(cmd)

# ---------------- CLI ----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Sync ALL RTB snapshots to pCloud (delta-aware).")
    ap.add_argument("--rtb-root", required=True, help="Lokales RTB-Wurzelverzeichnis (enthält Snapshot-Ordner + latest)")
    ap.add_argument("--dest-root", required=True, help="Remote Wurzel in pCloud (z.B. /Backup/pcloud-snapshots)")
    ap.add_argument("--snapshot-mode", choices=["objects","1to1"], default="objects")
    ap.add_argument("--dry-run", action="store_true")

    # Pfade zu bestehenden Tools
    ap.add_argument("--manifest-script", default="pcloud_json_manifest.py")
    ap.add_argument("--push-script", default="pcloud_push_json_manifest_to_pcloud.py")

    # Manifest-Optionen (Defaults wie bisher)
    ap.add_argument("--hash", default="sha256")
    ap.add_argument("--follow-symlinks", action="store_true")
    ap.add_argument("--no-follow-hardlinks", action="store_true")
    ap.add_argument("--store-hardlink-target", action="store_true")
    ap.add_argument("--store-symlink-target", action="store_true")

    # pCloud Config/ENV
    ap.add_argument("--env-file"); ap.add_argument("--profile"); ap.add_argument("--env-dir")
    ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--timeout", type=int)
    ap.add_argument("--device"); ap.add_argument("--token")
    args = ap.parse_args()

    # Config ziehen (nur für Retention/Remote-Listing nötig)
    cfg = pc.effective_config(
        env_file=args.env_file,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token},
        profile=args.profile,
        env_dir=args.env_dir
    )

    rtb_root = os.path.abspath(args.rtb_root)
    dest_root = pc._norm_remote_path(args.dest_root)

    local = _snapshots_local(rtb_root)
    remote = _snapshots_remote(cfg, dest_root)

    to_add = sorted(local - remote)
    to_del = sorted(remote - local)

    print(f"[plan] local={len(local)} remote={len(remote)} add={to_add} del={to_del}")

    # 1) 1:1-Retention vorab (Promotion + Delete), objects-Modus braucht das nicht
    if args.snapshot_mode == "1to1" and to_del:
        print("[retention] 1to1: promotion+delete for removed snapshots...")
        retention_sync_1to1(cfg, dest_root, local_snaps=local, dry=bool(args.dry_run))

    # 2) Neue Snapshots hochladen (je Snapshot: Manifest -> Push)
    for snap in to_add:
        snap_root = os.path.join(rtb_root, snap)
        out_json = f"/tmp/{snap}.manifest.json"
        print(f"[new] {snap}: build manifest -> {out_json}")
        run_manifest(args.manifest_script, snap_root, out_json,
                     hash_alg=args.hash,
                     follow_symlinks=bool(args.follow_symlinks),
                     follow_hardlinks=not args.no_follow_hardlinks,
                     store_hardlink_target=bool(args.store_hardlink_target),
                     store_symlink_target=bool(args.store_symlink_target))
        print(f"[push] {snap} -> {dest_root} (mode={args.snapshot_mode})")
        run_push(args.push_script, out_json, dest_root,
                 env_file=args.env_file,
                 snapshot_mode=args.snapshot_mode,
                 dry=bool(args.dry_run))

    # 3) objects-Modus: entfernte Snapshots schlicht löschen (Objekt-Store bleibt dedupliziert)
    if args.snapshot_mode == "objects" and to_del:
        for snap in to_del:
            rmpath = f"{dest_root}/_snapshots/{snap}"
            if args.dry_run:
                print(f"[dry] delete remote snapshot dir: {rmpath}")
            else:
                try:
                    pc.deletefolder_recursive(cfg, path=rmpath)
                except Exception as e:
                    print(f"[warn] delete failed {rmpath}: {e}", file=sys.stderr)

    print("done.")

if __name__ == "__main__":
    main()
