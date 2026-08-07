#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_trash_audit.py — Papierkorb auf pCloud prüfen / Snapshot-Ordner wiederherstellen.

Retention (deletefolderrecursive) verschiebt Snapshots in den Papierkorb — nicht sofort
endgültig gelöscht. trash_list/trash_restore sind REST-only (kein Binary-RPC).

Beispiele (pi-nas):
  cd /opt/apps/pcloud-tools/main
  python scripts/utilities/pcloud_trash_audit.py --env-file .env list
  python scripts/utilities/pcloud_trash_audit.py --env-file .env list --prefix 2026-07
  python scripts/utilities/pcloud_trash_audit.py --env-file .env restore --name 2026-07-02-040018 --dry-run
  python scripts/utilities/pcloud_trash_audit.py --env-file .env restore --folderid 12345678
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Iterator, List, Optional

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)
import pcloud_bin_lib as pc  # noqa: E402

_SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}$")


def _flatten_folders(node: dict) -> Iterator[dict]:
    for item in node.get("contents") or []:
        if item.get("isfolder"):
            yield item
            yield from _flatten_folders(item)


def _find_snapshot_folders(meta: dict, *, prefix: str = "") -> List[dict]:
    hits: List[dict] = []
    for item in _flatten_folders(meta):
        name = item.get("name") or ""
        if not _SNAPSHOT_RE.match(name):
            continue
        if prefix and not name.startswith(prefix):
            continue
        hits.append(item)
    return sorted(hits, key=lambda x: x.get("name") or "")


def cmd_list(cfg: dict, args: argparse.Namespace) -> int:
    top = pc.trash_list(
        cfg,
        folderid=0,
        recursive=bool(args.recursive),
        nofiles=True,
    )
    meta = top.get("metadata") or {}
    hits = _find_snapshot_folders(meta, prefix=args.prefix or "")

    if not hits and not args.recursive:
        top = pc.trash_list(cfg, folderid=0, recursive=True, nofiles=True)
        meta = top.get("metadata") or {}
        hits = _find_snapshot_folders(meta, prefix=args.prefix or "")

    print(f"Papierkorb: {len(hits)} Snapshot-Ordner (Pattern YYYY-MM-DD-HHMMSS)")
    for item in hits:
        print(
            f"  {item.get('name')}  "
            f"fid={item.get('folderid')}  "
            f"origparent={item.get('origparentfolderid')}"
        )
    return 0


def cmd_restore(cfg: dict, args: argparse.Namespace) -> int:
    folderid: Optional[int] = args.folderid
    name = args.name

    if folderid is None and name:
        top = pc.trash_list(cfg, folderid=0, recursive=True, nofiles=True)
        meta = top.get("metadata") or {}
        match = [x for x in _find_snapshot_folders(meta) if x.get("name") == name]
        if not match:
            print(f"[error] Snapshot '{name}' nicht im Papierkorb gefunden.", file=sys.stderr)
            return 1
        if len(match) > 1:
            print(f"[error] Mehrere Treffer für '{name}' — --folderid angeben.", file=sys.stderr)
            return 1
        folderid = int(match[0]["folderid"])
        print(f"Gefunden: {name} folderid={folderid}")

    if folderid is None:
        print("[error] --folderid oder --name angeben.", file=sys.stderr)
        return 2

    plan = pc.trash_restorepath(cfg, folderid=folderid)
    dest = (plan.get("destination") or {})
    restored = (plan.get("metadata") or {})
    print(f"Restore-Plan: {restored.get('name')} -> {dest.get('path') or dest.get('name')}")

    if args.dry_run:
        print("[dry-run] Kein Restore ausgeführt.")
        return 0

    pc.trash_restore(cfg, folderid=folderid)
    print("[ok] Wiederhergestellt. Bitte .upload_complete im Snapshot prüfen.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="pCloud Papierkorb: Snapshots listen / restore.")
    ap.add_argument("--env-file", required=True, help="Pfad zur .env (PCLOUD_TOKEN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Snapshot-Ordner im Papierkorb anzeigen")
    p_list.add_argument("--prefix", default="", help="Namens-Prefix, z.B. 2026-07")
    p_list.add_argument(
        "--recursive",
        action="store_true",
        help="Sofort rekursiv listen (langsamer, sonst Auto-Fallback)",
    )

    p_restore = sub.add_parser("restore", help="Snapshot-Ordner aus Papierkorb wiederherstellen")
    p_restore.add_argument("--folderid", type=int, help="folderid aus list")
    p_restore.add_argument("--name", help="Snapshot-Name, z.B. 2026-07-02-040018")
    p_restore.add_argument("--dry-run", action="store_true", help="Nur trash_restorepath anzeigen")

    args = ap.parse_args()
    cfg = pc.effective_config(env_file=args.env_file)
    pc.preflight_or_raise(cfg)

    if args.cmd == "list":
        return cmd_list(cfg, args)
    if args.cmd == "restore":
        return cmd_restore(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
