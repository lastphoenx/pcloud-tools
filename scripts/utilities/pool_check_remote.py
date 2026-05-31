#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_check_remote.py - Read-only Konsistenz-Check eines Pool-Backups auf pCloud.

Prueft (nur Lesezugriffe, KEINE Aenderungen):
  1. content_index.json laedt + pool_refs sind enriched (fileid/hash/size)
  2. _pool/ Objektanzahl  und: jede im Index referenzierte SHA physisch im Pool?
  3. (mit --snapshot) .upload_complete vorhanden + Stub-Anzahl

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \
  python pool_check_remote.py --env-file .env --dest-root /Backup/rtb_pool \
         --snapshot 2026-04-27-173201

Exit 0 = alle Checks ok, sonst 1.
"""
from __future__ import annotations
import os, sys, json, argparse

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc


def _walk(node):
    """Yield alle File-Eintraege (rekursiv) aus einem listfolder-metadata-Baum."""
    for c in node.get("contents", []) or []:
        if c.get("isfolder"):
            yield from _walk(c)
        else:
            yield c


def _pool_shas(cfg, pool_root):
    md = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
    shas = set()
    for f in _walk(md.get("metadata", {}) or {}):
        n = (f.get("name") or "").lower()
        if len(n) == 64 and all(ch in "0123456789abcdef" for ch in n):
            shas.add(n)
    return shas


def main():
    ap = argparse.ArgumentParser(description="Read-only Pool-Check (remote).")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--snapshot", help="Snapshot-Name; ohne -> nur Pool/Index global")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(args.dest_root).rstrip("/")
    pool_root = f"{dest}/_pool"
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

    problems = 0
    def ok(m):  print(f"[ok]   {m}")
    def bad(m):
        nonlocal problems; problems += 1; print(f"[FAIL] {m}")

    # 1. Index laden + enriched?
    refs = {}
    try:
        idx = json.loads(pc.get_textfile(cfg, path=idx_path))
        refs = idx.get("pool_refs") or {}
        ok(f"content_index.json geladen ({len(refs)} pool_refs)")
        sk = list(refs.keys())[:50]
        enr = sum(1 for k in sk if isinstance(refs[k], dict) and refs[k].get("fileid"))
        if sk and enr == len(sk):
            ok(f"Index enriched (Stichprobe {enr}/{len(sk)} mit fileid)")
        elif sk:
            bad(f"Index NICHT vollstaendig enriched ({enr}/{len(sk)} mit fileid)")
    except Exception as e:
        bad(f"content_index.json nicht ladbar: {e}")

    # 2. Pool zaehlen + Index-SHAs physisch vorhanden?
    try:
        pool = _pool_shas(cfg, pool_root)
        ok(f"_pool: {len(pool)} Objekte")
        if refs:
            missing = set(refs.keys()) - pool
            if missing:
                bad(f"{len(missing)} Index-SHAs FEHLEN physisch im Pool "
                    f"(z.B. {sorted(missing)[:3]})")
            else:
                ok(f"Alle {len(refs)} Index-SHAs physisch im Pool vorhanden")
    except Exception as e:
        bad(f"Pool-Scan fehlgeschlagen: {e}")

    # 3. Snapshot-spezifisch
    if args.snapshot:
        snap_dir = f"{snaps_root}/{args.snapshot}"
        if pc.stat_file_safe(cfg, path=f"{snap_dir}/.upload_complete"):
            ok(f"{args.snapshot}: .upload_complete vorhanden")
        else:
            bad(f"{args.snapshot}: .upload_complete FEHLT (unvollstaendig)")
        try:
            md = pc.listfolder(cfg, path=snap_dir, recursive=True, nofiles=False)
            stubs = [f for f in _walk(md.get("metadata", {}) or {})
                     if (f.get("name") or "").endswith(".meta.json")]
            ok(f"{args.snapshot}: {len(stubs)} Stubs vorhanden")
        except Exception as e:
            bad(f"{args.snapshot}: Stub-Listing fehlgeschlagen: {e}")

    print("=" * 60)
    print("RESULT:", "ALLE CHECKS OK" if problems == 0 else f"{problems} PROBLEM(E)")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
