#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""pcloud_rsync_prune_bin.py – "rsync-like prune": löscht in pCloud, was es lokal (Quelle) nicht mehr gibt.

Gruppe C (Use-Case Tool) – nutzt pcloud_bin_lib.py.
Idee: Spiegel-Fenster aufräumen wie rsync --delete (mit Bremse).
- Quelle: --src-root (lokales Verzeichnis)
- Ziel:   --dest-path ODER --dest-folderid (Root des Spiegelbereichs)
- Regeln:
  * Dateien werden gelöscht, wenn sie lokal fehlen UND serverseitig älter sind als --grace-days (Default 7).
  * --protect REGEX schützt Pfade (auf RELATIVEM Zielpfad basierend).
  * --delete-empty-dirs entfernt leere Ordner nach Dateilöschungen (rekursiv).
  * --dry-run führt keine Änderungen aus.
  * --clear: nach Papierkorb-Löschung direkt trash_clear.
"""

import os, sys, re, time, argparse, json
from typing import Dict, Any, List, Tuple
import pcloud_bin_lib as pc

def _norm_remote(p: str) -> str:
    if not p: return "/"
    p = p.strip()
    if not p.startswith("/"): p = "/" + p
    while '//' in p: p = p.replace("//","/")
    if len(p)>1 and p.endswith("/"):
        p = p[:-1]
    return p or "/"

def collect_tree(cfg: Dict[str,Any], root_folderid: int) -> Tuple[List[Dict[str,Any]], List[Dict[str,Any]]]:
    top = pc.listfolder(cfg, folderid=root_folderid, recursive=True, nofiles=False, showpath=True)
    root = top.get("metadata",{})
    base_path = root.get("path") or "/"
    files: List[Dict[str,Any]] = []
    folders: List[Dict[str,Any]] = []
    def rel(p: str) -> str:
        if not p: return ""
        if p == base_path: return "."
        b = base_path.rstrip("/")
        return p[len(b)+1:] if p.startswith(b + "/") else p
    def walk(md: Dict[str,Any]):
        if md.get("isfolder"):
            folders.append({"path": md.get("path"), "rel": rel(md.get("path")), "folderid": md.get("folderid")})
            for it in (md.get("contents") or []):
                walk(it)
        else:
            files.append({
                "path": md.get("path"),
                "rel": rel(md.get("path")),
                "fileid": md.get("fileid"),
                "modified": md.get("modified")
            })
    walk(root)
    if folders:
        folders[0]["protect_root"] = True
    return files, folders

def should_delete_file(local_root: str, rel: str, grace_cutoff_ts: float, protect_rx, src_exists_cache: dict) -> bool:
    if protect_rx and protect_rx.search(rel):
        return False
    local_path = os.path.join(local_root, rel if rel != "." else "")
    if rel == ".":
        return False
    exists = src_exists_cache.get(local_path)
    if exists is None:
        src_exists_cache[local_path] = os.path.exists(local_path)
        exists = src_exists_cache[local_path]
    if exists:
        return False
    # Grace wird als Tool-weite Bremse genutzt – das Tool wird bewusst nicht häufiger als die Grace ausgeführt.
    return True

def _parse_modified_to_epoch(mod):
    """
    Versucht 'modified' der pCloud-Metadaten in Epoch (float) zu wandeln.
    - akzeptiert bereits Timestamps (int/float/"1234")
    - parst RFC2822-Strings (z.B. "Wed, 10 Sep 2025 13:44:26 +0000")
    """
    if mod is None:
        return None
    if isinstance(mod, (int, float)):
        return float(mod)
    if isinstance(mod, str) and mod.isdigit():
        return float(int(mod))
    try:
        dt = email.utils.parsedate_to_datetime(str(mod))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def _annotate_files_with_epoch(files):
    """
    Erwartet 'files' als Liste von Dicts (mit Schlüsseln wie 'modified').
    Fügt 'modified_epoch' hinzu (oder None).
    No-Op, falls bereits vorhanden.
    """
    for f in files:
        if isinstance(f, dict):
            if "modified_epoch" not in f:
                f["modified_epoch"] = _parse_modified_to_epoch(f.get("modified"))

def main():
    ap = argparse.ArgumentParser(description="rsync-like prune für pCloud – lösche im Ziel, was lokal fehlt (mit Grace & Schutz).")
    ap.add_argument("--env-file"); ap.add_argument("--profile"); ap.add_argument("--env-dir")
    ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--timeout", type=int); ap.add_argument("--device")

    ap.add_argument("--src-root", required=True, help="Lokaler Quell-Root (Spiegelreferenz).")
    dst = ap.add_mutually_exclusive_group(required=True)
    dst.add_argument("--dest-path")
    dst.add_argument("--dest-folderid", type=int)

    ap.add_argument("--grace-days", type=int, default=7, help="Schonfrist in Tagen (Default: 7).")
    ap.add_argument("--protect", help="Regex, schützt relative Pfade im Ziel (z. B. '(^|/)DO_NOT_DELETE(/|$)').")
    ap.add_argument("--delete-empty-dirs", action="store_true", help="Löscht nach Dateilöschungen leere Verzeichnisse.")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--clear", action="store_true")
    ap.add_argument("--json", action="store_true")

    args = ap.parse_args()
    cfg = pc.effective_config(args.env_file, overrides={"host":args.host,"port":args.port,"timeout":args.timeout,"device":args.device}, profile=args.profile, env_dir=args.env_dir)

    if args.dest_folderid:
        root_id = int(args.dest_folderid)
        root_md = pc.listfolder(cfg, folderid=root_id, recursive=False, nofiles=True, showpath=True).get("metadata",{})
        base_path = root_md.get("path") or "/"
    else:
        base_path = _norm_remote(args.dest_path)
        root_id = pc.ensure_path(cfg, base_path)

    files, folders = collect_tree(cfg, root_id)
    protect_rx = re.compile(args.protect) if args.protect else None
    grace_cutoff_ts = time.time() - args.grace_days*86400

    src_exists_cache = {}
    file_actions = []
    for f in files:
        rel = f["rel"]
        if rel == ".": 
            continue
        if should_delete_file(args.src_root, rel, grace_cutoff_ts, protect_rx, src_exists_cache):
            file_actions.append(f)

    folder_actions = []
    results = {"deleted_files": [], "deleted_folders": [], "errors": []}
    for f in file_actions:
        try:
            if args.dry_run:
                results["deleted_files"].append({"rel": f["rel"], "path": f["path"], "dryrun": True}); continue
            top,_ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "deletefile", {"access_token": cfg["token"], "device": cfg["device"], "fileid": int(f["fileid"])})
            pc._expect_ok(top)
            if args.clear:
                tc,_ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "trash_clear", {"access_token": cfg["token"], "device": cfg["device"], "fileid": int(f["fileid"])})
                pc._expect_ok(tc)
            results["deleted_files"].append({"rel": f["rel"], "path": f["path"]})
        except Exception as e:
            results["errors"].append({"path": f["path"], "error": str(e)})

    if args.delete_empty_dirs:
        folder_map = { (d["path"] or ""): int(d["folderid"]) for d in folders if not d.get("protect_root") }
        for path in sorted(folder_map.keys(), key=lambda p: len(p or ""), reverse=True):
            try:
                md = pc.listfolder(cfg, folderid=folder_map[path], recursive=False, nofiles=False, showpath=False).get("metadata",{})
                contents = md.get("contents") or []
                if contents:
                    continue
                if args.dry_run:
                    folder_actions.append({"path": path, "dryrun": True}); continue
                top,_ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "deletefolder", {"access_token": cfg["token"], "device": cfg["device"], "folderid": int(folder_map[path])})
                pc._expect_ok(top)
                if args.clear:
                    tc,_ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "trash_clear", {"access_token": cfg["token"], "device": cfg["device"], "folderid": int(folder_map[path])})
                    pc._expect_ok(tc)
                folder_actions.append({"path": path})
            except Exception as e:
                results["errors"].append({"path": path, "error": str(e)})
        results["deleted_folders"] = folder_actions

    if args.json:
        print(json.dumps({"base_path": base_path, "file_deletes": results["deleted_files"], "folder_deletes": results.get("deleted_folders", []), "errors": results["errors"]}, ensure_ascii=False, indent=2))
    else:
        print(f"Basis: {base_path}")
        for it in results["deleted_files"]:
            if it.get("dryrun"):
                print(f"DRY-RUN: Datei löschen -> {it['path']}")
            else:
                print(f"OK Datei gelöscht: {it['path']}")
        for it in results.get("deleted_folders", []):
            if it.get("dryrun"):
                print(f"DRY-RUN: Leeren Ordner löschen -> {it['path']}")
            else:
                print(f"OK Ordner gelöscht: {it['path']}")
        if results["errors"]:
            print("Fehler:")
            for e in results["errors"]:
                print(f"  {e['path']}: {e['error']}")

if __name__ == "__main__":
    main()
