#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_archive_index.py - erzeugt gefilterte per-Snapshot Index-Archive nachtraeglich.

Erzeugt fuer jeden Snapshot eine GEFILTERTE v2-Archivdatei unter
  _snapshots/_index/archive/<snap>_index.json

Gefiltert bedeutet: nur die pool_refs-Eintraege, deren snapshots-Map diesen
Snapshot enthaelt, und nur der relpath-Eintrag fuer diesen Snapshot.
Damit ist jede Archivdatei eigenstaendig und fuer den Recovery dieses Snapshots
ausreichend (sha -> pool_path + relpaths), ohne kumulativen Ballast der anderen
Snapshots. Aus allen Archivdateien laesst sich der Master-Index vollstaendig
rekonstruieren.

Die fehlenden Archive fuer 04-27, 05-01, 05-14 wurden nicht erstellt, weil
der Archive-Schritt erst in Audit #2 eingebaut wurde und diese Snapshots vorher
hochgeladen wurden. 05-15 hat bereits ein korrektes v2-Archiv.

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \
  python pool_archive_index.py --env-file .env --dest-root /Backup/rtb_pool \
         --snapshot 2026-04-27-173201 2026-05-01-103649 2026-05-14-120009

Exit 0 = alle Archive ok, sonst 1.
"""
from __future__ import annotations
import os, sys, json, argparse

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc


def main():
    ap = argparse.ArgumentParser(description="Gefilterte per-Snapshot v2-Archive nachziehen.")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--snapshot", nargs="+", required=True,
                    help="Ein oder mehrere Snapshot-Namen")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(args.dest_root).rstrip("/")
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

    # Master laden
    try:
        master = json.loads(pc.get_textfile(cfg, path=idx_path))
        all_refs = master.get("pool_refs") or {}
        print(f"[ok]   Master geladen: version={master.get('version')} "
              f"pool_refs={len(all_refs)}")
    except Exception as e:
        print(f"[FAIL] Master-Index nicht ladbar: {e}")
        sys.exit(1)

    problems = 0
    for snap in args.snapshot:
        # Gefiltert: nur SHAs die diesen Snapshot referenzieren, nur dessen relpaths
        filtered = {}
        for sha, entry in all_refs.items():
            if not isinstance(entry, dict):
                continue
            snaps_map = entry.get("snapshots")
            if not isinstance(snaps_map, dict) or snap not in snaps_map:
                continue
            filtered[sha] = {
                "fileid": entry.get("fileid"),
                "hash":   entry.get("hash"),
                "size":   entry.get("size"),
                "snapshots": {snap: snaps_map[snap]}
            }

        snap_idx = {"version": 2, "pool_refs": filtered}
        archive_path = f"{snaps_root}/_index/archive/{snap}_index.json"
        try:
            # Zielordner sicherstellen, dann schreiben
            archive_dir = os.path.dirname(archive_path)
            fid = pc.stat_folderid_fast(cfg, archive_dir)
            if not fid:
                fid = pc.ensure_path(cfg, archive_dir)
            fname = os.path.basename(archive_path)
            pc.write_json_to_folderid(cfg, folderid=int(fid), filename=fname,
                                      obj=snap_idx, minify=False)
            print(f"[ok]   {snap}: {len(filtered)} SHAs -> {archive_path}")
        except Exception as e:
            problems += 1
            print(f"[FAIL] {snap}: {e}")

    print("=" * 60)
    print("RESULT:", "ALLE ARCHIVE OK" if problems == 0 else f"{problems} PROBLEM(E)")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
