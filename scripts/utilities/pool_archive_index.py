#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_archive_index.py - erzeugt die per-Snapshot Index-Archivkopie nachtraeglich.

Hintergrund: der Delta-Pfad hat frueher die Snapshot-isolierte Index-Kopie unter
_snapshots/_index/archive/<snap>_index.json nicht angelegt (nur der Full-Pool-Pfad
tat das). Fuer kuenftige Snapshots ist das gefixt; fuer bereits geladene Snapshots
holt dieses Tool die Kopie nach - ohne Re-Run/Klon.

Es kopiert server-seitig die aktuelle Master content_index.json nach
  _snapshots/_index/archive/<snap>_index.json
fuer jeden angegebenen Snapshot (identisches Muster wie im Push-Tool).

WICHTIG (Ehrlichkeit): die Kopie enthaelt den AKTUELLEN Master-Stand, nicht den
Stand zum Finalize-Zeitpunkt des Snapshots. Der Master ist kumulativ/append-only
und autoritativ; jeder pool_refs-Eintrag traegt seine eigene snapshots-Liste. Fuer
Recovery ist das ausreichend. Ein echter Finalize-Zeitpunkt-Stand entstuende nur
durch einen Re-Run des jeweiligen Snapshots.

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \
  python pool_archive_index.py --env-file .env --dest-root /Backup/rtb_pool \
         --snapshot 2026-05-01-103649 2026-05-14-120009

Exit 0 = alle Kopien ok, sonst 1.
"""
from __future__ import annotations
import os, sys, argparse

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc


def main():
    ap = argparse.ArgumentParser(description="Per-Snapshot Index-Archivkopie nachziehen (read-mostly).")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--snapshot", nargs="+", required=True,
                    help="Ein oder mehrere Snapshot-Namen")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(args.dest_root).rstrip("/")
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

    # Sicherstellen, dass der Master ueberhaupt existiert
    if not pc.stat_file_safe(cfg, path=idx_path):
        print(f"[FAIL] Master-Index nicht gefunden: {idx_path}")
        sys.exit(1)
    print(f"[ok]   Master-Index: {idx_path}")

    problems = 0
    for snap in args.snapshot:
        archive_path = f"{snaps_root}/_index/archive/{snap}_index.json"
        try:
            pc.ensure_parent_dirs(cfg, archive_path)
            pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
            print(f"[ok]   archiviert: {archive_path}")
        except Exception as e:
            problems += 1
            print(f"[FAIL] {snap}: {e}")

    print("=" * 60)
    print("RESULT:", "ALLE KOPIEN OK" if problems == 0 else f"{problems} PROBLEM(E)")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
