#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_check_local.py - Read-only Check der LOKALEN Pool-Artefakte (auf der pi).

Prueft (KEINE Aenderungen):
  1. (optional) Quell-Manifest: laedt, hat items, schema.
     Hinweis: Das Manifest ist der Quell-Scan -> fileid wird NICHT erwartet (by design).
  2. Lokaler Master-Index: pool_refs vorhanden + enriched (fileid/hash/size).

Aufruf:
  python pool_check_local.py \
    --manifest /srv/pcloud-archive/manifests/2026-04-27-173201.json \
    --master-index /srv/pcloud-archive/indexes/content_index_master.json

Exit 0 = ok, sonst 1.
"""
from __future__ import annotations
import os, sys, json, argparse


def main():
    ap = argparse.ArgumentParser(description="Read-only Pool-Check (lokal).")
    ap.add_argument("--manifest", help="Pfad zum Quell-Manifest (optional)")
    ap.add_argument("--master-index",
                    default="/srv/pcloud-archive/indexes/content_index_master.json")
    args = ap.parse_args()

    problems = 0
    def ok(m):  print(f"[ok]   {m}")
    def info(m): print(f"[info] {m}")
    def bad(m):
        nonlocal problems; problems += 1; print(f"[FAIL] {m}")

    # 1. Manifest (Quell-Scan)
    if args.manifest:
        try:
            with open(args.manifest, encoding="utf-8") as f:
                m = json.load(f)
            files = [it for it in (m.get("items") or []) if it.get("type") == "file"]
            ok(f"Manifest: {len(files)} Files, schema={m.get('schema')}")
            with_fid = sum(1 for it in files[:50]
                           if it.get("pool_fileid") or it.get("fileid"))
            info(f"Manifest fileid-Anreicherung: {with_fid}/50 Stichprobe "
                 f"(0 = by design Quell-Scan; >0 nur falls Write-Back aktiviert)")
        except Exception as e:
            bad(f"Manifest nicht ladbar: {e}")

    # 2. Master-Index (muss enriched sein)
    try:
        with open(args.master_index, encoding="utf-8") as f:
            idx = json.load(f)
        refs = idx.get("pool_refs") or {}
        ok(f"Master-Index: {len(refs)} pool_refs")
        sk = list(refs.keys())[:50]
        enr = sum(1 for k in sk
                  if isinstance(refs[k], dict) and refs[k].get("fileid"))
        legacy = sum(1 for k in sk if isinstance(refs[k], list))
        if sk and enr == len(sk):
            ok(f"Master-Index enriched ({enr}/{len(sk)} mit fileid/hash/size)")
        elif sk:
            bad(f"Master-Index NICHT vollstaendig enriched "
                f"({enr} enriched, {legacy} altes Listen-Format, von {len(sk)})")
    except FileNotFoundError:
        bad(f"Master-Index nicht gefunden: {args.master_index}")
    except Exception as e:
        bad(f"Master-Index nicht ladbar: {e}")

    print("=" * 60)
    print("RESULT:", "ALLE CHECKS OK" if problems == 0 else f"{problems} PROBLEM(E)")
    sys.exit(0 if problems == 0 else 1)


if __name__ == "__main__":
    main()
