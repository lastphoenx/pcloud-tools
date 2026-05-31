#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_rebuild_index_v2.py - baut den v2-content_index neu (snapshots als Map snap->[relpaths]).

Quelle:
  - relpath->sha pro Snapshot: die LOKALEN Manifeste /srv/pcloud-archive/manifests/<snap>.json
    (autoritativ fuer die Datei->Inhalt-Zuordnung des jeweiligen Snapshots).
  - fileid/hash/size pro sha: der AKTUELLE Remote-Index (pool_refs), da Manifeste keine
    Remote-Koordinaten tragen.

Ergebnis (v2):
  { "version": 2,
    "pool_refs": { "<sha>": { "fileid":.., "hash":.., "size":..,
                              "snapshots": { "<snap>": ["<relpath>", ...], ... } } } }

Default ist READ-MOSTLY: schreibt v2 nur lokal nach --out zur INSPEKTION. Erst mit --upload
wird der Remote-Index ersetzt (vorher Backup nach _index/archive/).

Fehlt fuer einen remote vorhandenen Snapshot das lokale Manifest, bricht das Tool ab und
nennt den Regenerier-Befehl (kein Teil-Rebuild).

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \
  python pool_rebuild_index_v2.py --env-file .env --dest-root /Backup/rtb_pool \
         --out /srv/pcloud-temp/content_index_v2.json
  # nach Inspektion:
  ... --upload
"""
from __future__ import annotations
import os, sys, json, time, argparse

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc


def _register(refs: dict, sha: str, snap: str, relpath: str,
              fileid=None, hsh=None, size=None) -> None:
    e = refs.get(sha)
    if e is None:
        e = {"fileid": fileid, "hash": hsh, "size": size, "snapshots": {}}
        refs[sha] = e
    rels = e["snapshots"].setdefault(snap, [])
    if relpath and relpath not in rels:
        rels.append(relpath)
    if fileid is not None and not e.get("fileid"):
        e["fileid"] = fileid
    if hsh is not None and not e.get("hash"):
        e["hash"] = hsh
    if size is not None and not e.get("size"):
        e["size"] = size


def main() -> int:
    ap = argparse.ArgumentParser(description="v2-content_index neu bauen (snapshots->relpaths).")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--manifests-dir", default="/srv/pcloud-archive/manifests")
    ap.add_argument("--rtb-root", default="/mnt/backup/rtb_nas", help="nur fuer Regenerier-Hinweis")
    ap.add_argument("--out", required=True, help="lokale v2-JSON zur Inspektion")
    ap.add_argument("--upload", action="store_true", help="Remote-Index ersetzen (mit Backup)")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(args.dest_root).rstrip("/")
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

    # 1) aktueller Remote-Index -> coords-Quelle
    cur_refs = {}
    try:
        cur = json.loads(pc.get_textfile(cfg, path=idx_path))
        cur_refs = cur.get("pool_refs") or {}
        print(f"[ok]   aktueller Remote-Index: {len(cur_refs)} pool_refs")
    except Exception as e:
        print(f"[FAIL] aktueller Remote-Index nicht ladbar: {e}")
        return 1

    # 2) remote vorhandene Snapshots (autoritativ fuer 'was ist hochgeladen')
    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True, showpath=False)
    snaps = sorted(
        c.get("name") for c in (top.get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
    )
    print(f"[ok]   Snapshots remote: {len(snaps)} -> {snaps}")

    # 3) Manifeste pruefen - alle muessen lokal vorliegen
    missing = [s for s in snaps if not os.path.exists(os.path.join(args.manifests_dir, f"{s}.json"))]
    if missing:
        print(f"[FAIL] {len(missing)} Snapshot-Manifest(e) fehlen lokal: {missing}")
        print(f"       Regenerieren (re-hasht via ref-manifest nur Geaendertes), z.B.:")
        ref = os.path.join(args.manifests_dir, f"{snaps[0]}.json")
        for s in missing:
            print(f"       python {os.path.join(os.environ.get('MAIN_DIR','.'),'pcloud_json_pool_manifest.py')} "
                  f"--root {args.rtb_root}/{s} --snapshot {s} "
                  f"--out {args.manifests_dir}/{s}.json --hash sha256 --ref-manifest {ref}")
        return 1

    # 4) v2 aufbauen
    refs: dict = {}
    for snap in snaps:
        man_path = os.path.join(args.manifests_dir, f"{snap}.json")
        with open(man_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        n = 0
        for it in m.get("items", []):
            if it.get("type") != "file":
                continue
            rp = it.get("relpath")
            sha = it.get("sha256")
            if not rp or not sha:
                continue
            c = cur_refs.get(sha) or {}
            _register(refs, sha, snap, rp,
                      fileid=c.get("fileid"), hsh=c.get("hash"),
                      size=(it.get("size") if it.get("size") is not None else c.get("size")))
            n += 1
        print(f"[build] {snap}: {n} Dateien")

    # 5) Konsistenz-Checks (nur Bericht, kein Abbruch)
    no_coords = [s for s, e in refs.items() if not e.get("fileid")]
    not_covered = [s for s in cur_refs.keys() if s not in refs]
    total_paths = sum(len(p) for e in refs.values() for p in e["snapshots"].values())
    print(f"[stats] v2: {len(refs)} shas, {total_paths} (snap,relpath)-Eintraege")
    if no_coords:
        print(f"[warn] {len(no_coords)} shas ohne fileid (nicht im aktuellen Remote-Index, z.B. {no_coords[:3]})")
    if not_covered:
        print(f"[warn] {len(not_covered)} shas im Remote-Index, aber in KEINEM Manifest "
              f"(verwaiste Refs? z.B. {not_covered[:3]})")

    v2 = {"version": 2, "pool_refs": refs}

    # 6) lokal schreiben (immer)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(v2, f, ensure_ascii=False)
    print(f"[ok]   v2 lokal geschrieben: {args.out}")

    # 7) optional hochladen (mit Backup)
    if args.upload:
        ts = int(time.time())
        backup_path = f"{snaps_root}/_index/archive/content_index_pre_v2_{ts}.json"
        try:
            pc.ensure_parent_dirs(cfg, backup_path)
            pc.copyfile(cfg, from_path=idx_path, to_path=backup_path)
            print(f"[ok]   Backup des bisherigen Index: {backup_path}")
        except Exception as e:
            print(f"[FAIL] Backup fehlgeschlagen - breche Upload ab: {e}")
            return 1
        # kanonischen v2-Writer des Push-Tools wiederverwenden (version=2, items-Drop)
        from pcloud_push_json_pool_manifest_to_pcloud import save_content_index
        save_content_index(cfg, snaps_root, v2, dry=False)
        print(f"[ok]   Remote-Index ersetzt: {idx_path}")
    else:
        print("[info] Kein --upload: Remote-Index unveraendert. Erst --out pruefen, dann --upload.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
