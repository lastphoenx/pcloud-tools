#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Snapshot-Groessen ohne du (Manifest + DB). Schnell auf RTB/Hardlink-Baeumen.

du -sh ueber /mnt/backup/rtb_nas/* laeuft Stunden (Millionen Hardlinks, mergerfs, D-State).
Dieses Script nutzt archivierte Manifeste und backup_runs.bytes_uploaded.

Beispiele:
  python scripts/utilities/snapshot_sizing.py --env-file .env
  python scripts/utilities/snapshot_sizing.py --env-file .env --last 10
  python scripts/utilities/snapshot_sizing.py --rtb-root /mnt/backup/rtb_nas --snap 2026-06-23-200041
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)


def _load_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _mysql(env: Dict[str, str], sql: str) -> List[Tuple[str, ...]]:
    pw = env.get("PCLOUD_DB_PASS", "")
    if not pw:
        return []
    proc = subprocess.run(
        [
            "mysql", "-h", env.get("PCLOUD_DB_HOST", "localhost"),
            "-P", env.get("PCLOUD_DB_PORT", "3306"),
            "-u", env.get("PCLOUD_DB_USER", "pcloud_backup"),
            f"-D{env.get('PCLOUD_DB_NAME', 'pcloud_backup')}",
            "-N", "-B", "-e", sql,
        ],
        env={**os.environ, "MYSQL_PWD": pw},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return []
    rows = []
    for line in proc.stdout.strip().splitlines():
        if line:
            rows.append(tuple(line.split("\t")))
    return rows


def _fmt_gb(n: int) -> str:
    if n <= 0:
        return "0"
    return f"{n / (1024 ** 3):.2f}"


def _manifest_stats(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"files": 0, "logical_bytes": 0, "unique_shas": 0, "missing": True}
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"files": 0, "logical_bytes": 0, "unique_shas": 0, "missing": True, "corrupt": True}
    files = 0
    logical = 0
    shas: set[str] = set()
    for it in m.get("items", []) or []:
        if it.get("type") != "file":
            continue
        files += 1
        logical += int(it.get("size") or 0)
        sha = (it.get("sha256") or "").lower()
        if sha:
            shas.add(sha)
    return {
        "files": files,
        "logical_bytes": logical,
        "unique_shas": len(shas),
        "missing": False,
    }


def _list_rtb_snaps(rtb_root: str) -> List[str]:
    if not os.path.isdir(rtb_root):
        return []
    out = []
    for name in os.listdir(rtb_root):
        if name in ("latest", ".DS_Store"):
            continue
        p = os.path.join(rtb_root, name)
        if os.path.isdir(p) and name[:4].isdigit():
            out.append(name)
    return sorted(out)


def _upload_stats(env: Dict[str, str]) -> Dict[str, Tuple[int, int]]:
    """snapshot -> (bytes_uploaded, files_uploaded) letzter SUCCESS."""
    rows = _mysql(
        env,
        """
        SELECT snapshot_name, bytes_uploaded, files_uploaded
        FROM backup_runs
        WHERE status = 'SUCCESS' AND bytes_uploaded IS NOT NULL
        ORDER BY finished_at DESC;
        """,
    )
    out: Dict[str, Tuple[int, int]] = {}
    for parts in rows:
        if len(parts) >= 3 and parts[0] not in out:
            out[parts[0]] = (int(parts[1] or 0), int(parts[2] or 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot sizing via manifests (no du)")
    ap.add_argument("--env-file", default=f"{MAIN_DIR}/.env")
    ap.add_argument("--rtb-root", default="/mnt/backup/rtb_nas")
    ap.add_argument("--manifests-dir", help="default: PCLOUD_ARCHIVE_DIR/manifests")
    ap.add_argument("--last", type=int, default=20, help="Anzahl Snapshots (0=alle)")
    ap.add_argument("--snap", help="Einzelner Snapshot")
    args = ap.parse_args()

    env = _load_env(args.env_file)
    archive = env.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    manifests_dir = args.manifests_dir or os.path.join(archive, "manifests")
    uploads = _upload_stats(env)

    if args.snap:
        snaps = [args.snap]
    else:
        snaps = _list_rtb_snaps(args.rtb_root)
        if args.last > 0:
            snaps = snaps[-args.last :]

    if not snaps:
        print("Keine Snapshots gefunden (latest-Symlink zaehlt nicht).")
        return 1

    print(f"{'SNAPSHOT':<22} {'FILES':>8} {'LOGICAL_GB':>11} {'UNIQ_SHA':>9} "
          f"{'UPLOAD_GB':>10} {'UP_FILES':>9}  MAN")
    print("-" * 85)

    total_logical = 0
    total_upload = 0
    for snap in snaps:
        man_path = os.path.join(manifests_dir, f"{snap}.json")
        st = _manifest_stats(man_path)
        up_b, up_f = uploads.get(snap, (0, 0))
        total_logical += st["logical_bytes"]
        total_upload += up_b
        man_flag = "—" if st.get("missing") else ("!" if st.get("corrupt") else "ok")
        print(
            f"{snap:<22} {st['files']:>8} {_fmt_gb(st['logical_bytes']):>11} "
            f"{st['unique_shas']:>9} {_fmt_gb(up_b):>10} {up_f:>9}  {man_flag}"
        )

    print("-" * 85)
    print(
        f"{'SUMME (angezeigt)':<22} {'':>8} {_fmt_gb(total_logical):>11} "
        f"{'':>9} {_fmt_gb(total_upload):>10}"
    )
    print()
    print("Hinweise:")
    print("  LOGICAL_GB  = Summe Dateigroessen im Manifest (logische Groesse, nicht du)")
    print("  UPLOAD_GB   = Neuer Pool-Traffic bei diesem Upload (dedupliziert, aus DB)")
    print("  RTB physisch ≪ LOGICAL wegen Hardlinks — du nicht verwenden auf rtb_nas")
    print("  Remote Pool-Gesamt: pcloud_pool_gc.py --retention-forecast")
    return 0


if __name__ == "__main__":
    sys.exit(main())
