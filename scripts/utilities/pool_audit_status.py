#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_audit_status.py — Schneller Status-Report: RTB vs. Manifeste vs. pCloud vs. MariaDB

Kein rekursives find — nur direkte Verzeichnis-Listen und gezielte pCloud-API-Calls.

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \\
  python scripts/utilities/pool_audit_status.py \\
    --env-file .env --pool-root /Backup/rtb_pool

  Manifest-Pfad kommt aus .env (PCLOUD_ARCHIVE_DIR, Default /srv/pcloud-archive).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc

_SNAP_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}-\d{6}$")
_JSON_MODE = False


def _log(msg: str) -> None:
    if _JSON_MODE:
        return
    print(msg, flush=True)


def _matrix_hint(in_rtb: bool, in_man: bool, in_remote: bool, is_complete: bool) -> str:
    """Kurzkommentar pro Matrix-Zeile (Spalte Hinweis)."""
    if in_rtb and not is_complete:
        if in_remote:
            return "Catch-up: Remote unvollständig (kein .upload_complete)"
        return "Catch-up: noch nicht auf pCloud"
    if not in_rtb and in_remote and is_complete and in_man:
        return "RTB-Retention; pCloud ok"
    if not in_rtb and in_remote and is_complete and not in_man:
        return "Complete; Manifest fehlt lokal"
    if not in_rtb and in_remote and not is_complete:
        return "Zombie auf pCloud → pool_gc --delete-snapshots SNAP"
    if in_rtb and is_complete and not in_man:
        return "Complete; Manifest archivieren"
    if not in_remote and is_complete:
        return "prüfen: complete ohne Remote-Ordner?"
    return "prüfen"


def _list_rtb_snapshots(rtb_root: str) -> List[str]:
    if not os.path.isdir(rtb_root):
        return []
    return sorted(
        name for name in os.listdir(rtb_root)
        if _SNAP_RE.match(name) and os.path.isdir(os.path.join(rtb_root, name))
    )


def _list_manifest_snapshots(manifests_dir: str) -> Tuple[List[str], List[str]]:
    """Returns (valid_snap_names, problems)."""
    if not os.path.isdir(manifests_dir):
        return [], [f"Verzeichnis fehlt: {manifests_dir}"]
    snaps: List[str] = []
    problems: List[str] = []
    for name in sorted(os.listdir(manifests_dir)):
        if not name.endswith(".json"):
            continue
        snap = name[:-5]
        path = os.path.join(manifests_dir, name)
        try:
            size = os.path.getsize(path)
            if size == 0:
                problems.append(f"leer: {path}")
                continue
            with open(path, "rb") as f:
                if not f.read(1):
                    problems.append(f"leer: {path}")
                    continue
        except OSError as e:
            problems.append(f"nicht lesbar: {path} ({e})")
            continue
        snaps.append(snap)
    return snaps, problems


def _list_remote_snapshot_folders(cfg: dict, snaps_root: str) -> List[str]:
    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
    return sorted(
        c["name"]
        for c in (top.get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
        and _SNAP_RE.match(c.get("name", ""))
    )


def _has_upload_complete(cfg: dict, snaps_root: str, snap: str) -> bool:
    return bool(pc.stat_file_safe(cfg, path=f"{snaps_root}/{snap}/.upload_complete"))


def _check_complete_parallel(
    cfg: dict, snaps_root: str, snaps: List[str], workers: int = 8
) -> Dict[str, bool]:
    result: Dict[str, bool] = {}

    def _one(snap: str) -> Tuple[str, bool]:
        return snap, _has_upload_complete(cfg, snaps_root, snap)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for snap, ok in ex.map(_one, snaps):
            result[snap] = ok
    return result


def _storage_line(path: str) -> str:
    """Kurze df-Zeile fuer einen Pfad (Filesystem + Mount)."""
    try:
        proc = subprocess.run(
            ["df", "-h", path, "--output=source,fstype,size,used,avail,pcent,target"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip() and not ln.startswith("Filesystem")]
        return lines[0] if lines else "?"
    except Exception:
        return "?"


def _warn_nas_duplicate(manifests_dir: str) -> None:
    """Warnt wenn /srv/nas/pcloud-archive ein separater Baum ist (nicht Pipeline-Pfad)."""
    nas_manifests = "/srv/nas/pcloud-archive/manifests"
    if not os.path.isdir(nas_manifests):
        return
    try:
        if os.path.samefile(manifests_dir, nas_manifests):
            return
    except OSError:
        pass
    _log(f"[warn] {nas_manifests} existiert separat (mergerfs) — "
         f"Pipeline nutzt {manifests_dir}")


def _load_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _query_db_failed(env: Dict[str, str]) -> List[Dict[str, str]]:
    """Letzter FAILED-Status pro Snapshot aus MariaDB (optional)."""
    db_pass = env.get("PCLOUD_DB_PASS") or os.environ.get("PCLOUD_DB_PASS", "")
    if not db_pass:
        return []
    host = env.get("PCLOUD_DB_HOST", "localhost")
    port = env.get("PCLOUD_DB_PORT", "3306")
    user = env.get("PCLOUD_DB_USER", "pcloud_backup")
    db = env.get("PCLOUD_DB_NAME", "pcloud_backup")
    sql = """
        SELECT br.snapshot_name, br.status, br.started_at, br.error_message
        FROM backup_runs br
        INNER JOIN (
            SELECT snapshot_name, MAX(started_at) AS max_started
            FROM backup_runs GROUP BY snapshot_name
        ) latest ON br.snapshot_name = latest.snapshot_name
               AND br.started_at = latest.max_started
        WHERE br.status = 'FAILED'
        ORDER BY br.snapshot_name;
    """
    try:
        proc = subprocess.run(
            [
                "mysql", "-h", host, "-P", port, "-u", user, f"-D{db}",
                "-N", "-B", "-e", sql,
            ],
            env={**os.environ, "MYSQL_PWD": db_pass},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            _log(f"[warn] MariaDB nicht abfragbar: {proc.stderr.strip()}")
            return []
        rows = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                rows.append({
                    "snapshot_name": parts[0],
                    "status": parts[1],
                    "started_at": parts[2],
                    "error_message": parts[3],
                })
        return rows
    except Exception as e:
        _log(f"[warn] MariaDB-Abfrage fehlgeschlagen: {e}")
        return []


def _query_db_running(env: Dict[str, str]) -> List[Dict[str, str]]:
    """Offene RUNNING-Zeilen (Zombies, wenn die Pipeline nicht laeuft)."""
    db_pass = env.get("PCLOUD_DB_PASS") or os.environ.get("PCLOUD_DB_PASS", "")
    if not db_pass:
        return []
    host = env.get("PCLOUD_DB_HOST", "localhost")
    port = env.get("PCLOUD_DB_PORT", "3306")
    user = env.get("PCLOUD_DB_USER", "pcloud_backup")
    db = env.get("PCLOUD_DB_NAME", "pcloud_backup")
    sql = """
        SELECT run_id, snapshot_name, started_at
        FROM backup_runs
        WHERE status = 'RUNNING'
        ORDER BY started_at;
    """
    try:
        proc = subprocess.run(
            [
                "mysql", "-h", host, "-P", port, "-u", user, f"-D{db}",
                "-N", "-B", "-e", sql,
            ],
            env={**os.environ, "MYSQL_PWD": db_pass},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return []
        rows = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({
                    "run_id": parts[0],
                    "snapshot_name": parts[1],
                    "started_at": parts[2],
                })
        return rows
    except Exception:
        return []


def _query_db_summary(env: Dict[str, str]) -> Optional[Dict[str, int]]:
    db_pass = env.get("PCLOUD_DB_PASS") or os.environ.get("PCLOUD_DB_PASS", "")
    if not db_pass:
        return None
    host = env.get("PCLOUD_DB_HOST", "localhost")
    port = env.get("PCLOUD_DB_PORT", "3306")
    user = env.get("PCLOUD_DB_USER", "pcloud_backup")
    db = env.get("PCLOUD_DB_NAME", "pcloud_backup")
    sql = "SELECT status, COUNT(*) FROM backup_runs GROUP BY status;"
    try:
        proc = subprocess.run(
            ["mysql", "-h", host, "-P", port, "-u", user, f"-D{db}", "-N", "-B", "-e", sql],
            env={**os.environ, "MYSQL_PWD": db_pass},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return dict(line.split("\t", 1) for line in proc.stdout.strip().splitlines() if "\t" in line)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Pool-Backup Status: RTB, Manifeste, pCloud, DB")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--pool-root", help="Remote Pool-Root, z.B. /Backup/rtb_pool")
    ap.add_argument("--dest-root", help="(deprecated) Alias fuer --pool-root")
    ap.add_argument("--rtb-root", default=None)
    ap.add_argument("--manifests-dir", default=None,
                    help="Default: <PCLOUD_ARCHIVE_DIR aus .env>/manifests")
    ap.add_argument("--skip-db", action="store_true", help="MariaDB nicht abfragen")
    ap.add_argument("--json", action="store_true",
                    help="Nur kompaktes JSON (Health-Check / Dashboard)")
    ap.add_argument("--workers", type=int, default=8, help="Parallele .upload_complete Checks")
    args = ap.parse_args()

    global _JSON_MODE
    _JSON_MODE = bool(args.json)

    pool_root_raw = args.pool_root or args.dest_root
    if not pool_root_raw:
        if args.json:
            print(json.dumps({
                "error": "pool-root required",
                "catchup_count": 0,
                "incomplete_count": 0,
                "running_count": 0,
                "catchup": [],
                "incomplete_remote": [],
                "running": [],
            }))
        else:
            _log("[FAIL] --pool-root erforderlich")
        return 2
    if args.dest_root and not args.pool_root:
        _log("[warn] --dest-root ist deprecated, bitte --pool-root verwenden")

    env_vars = _load_env_file(args.env_file)
    archive_dir = (
        env_vars.get("PCLOUD_ARCHIVE_DIR")
        or os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    )
    if not args.rtb_root:
        args.rtb_root = env_vars.get("RTB") or os.environ.get("RTB", "/mnt/backup/rtb_nas")
    if not args.manifests_dir:
        args.manifests_dir = os.path.join(archive_dir, "manifests")
    temp_dir = env_vars.get("PCLOUD_TEMP_DIR") or os.environ.get("PCLOUD_TEMP_DIR", "/srv/pcloud-temp")

    t0 = time.time()
    cfg = pc.effective_config(env_file=args.env_file)
    dest = pc._norm_remote_path(pool_root_raw).rstrip("/")
    snaps_root = f"{dest}/_snapshots"

    _log("=== pool_audit_status ===")
    _log(f"RTB:        {args.rtb_root}")
    _log(f"Manifeste:  {args.manifests_dir}")
    _log(f"Archiv:     {archive_dir}")
    _log(f"Temp:       {temp_dir}")
    _log(f"Pool-Root:  {dest}")
    _log(f"Storage:    archive {_storage_line(archive_dir)}")
    _log(f"Storage:    temp    {_storage_line(temp_dir)}")
    _warn_nas_duplicate(args.manifests_dir)
    _log("")

    # --- Lokale Listen (schnell) ---
    rtb_snaps = _list_rtb_snapshots(args.rtb_root)
    manifest_snaps, manifest_problems = _list_manifest_snapshots(args.manifests_dir)
    rtb_set = set(rtb_snaps)
    manifest_set = set(manifest_snaps)

    _log(f"RTB-Snapshots:      {len(rtb_snaps)}")
    _log(f"Archiv-Manifeste:   {len(manifest_snaps)}")
    if manifest_problems:
        _log(f"Manifest-Probleme:  {len(manifest_problems)}")
        for p in manifest_problems:
            _log(f"  [FAIL] {p}")

    # --- Remote: ein listfolder + parallele complete-Checks ---
    _log("")
    _log("[fetch] Remote-Snapshot-Ordner...")
    remote_folders = _list_remote_snapshot_folders(cfg, snaps_root)
    remote_set = set(remote_folders)
    _log(f"Remote-Ordner:      {len(remote_folders)}")

    _log("[fetch] .upload_complete pruefen (parallel)...")
    complete_map = _check_complete_parallel(cfg, snaps_root, remote_folders, args.workers)
    complete_set = {s for s, ok in complete_map.items() if ok}
    incomplete_remote = sorted(remote_set - complete_set)

    _log(f"Remote complete:    {len(complete_set)}")
    _log(f"Remote incomplete:  {len(incomplete_remote)}")

    # --- Kreuzvergleiche ---
    all_snaps = sorted(rtb_set | manifest_set | remote_set)
    _log("")
    _log("--- Snapshot-Matrix (nur Abweichungen) ---")
    _log("Legende: RTB=lokal | Man=Manifest archiviert | Pcl=Ordner auf pCloud | Cmp=.upload_complete")
    _log(f"{'Snapshot':<22} {'RTB':^5} {'Man':^5} {'Pcl':^5} {'Cmp':^5}  Hinweis")
    issues = 0
    ok_hidden = 0
    for snap in all_snaps:
        in_rtb = snap in rtb_set
        in_man = snap in manifest_set
        in_remote = snap in remote_set
        is_complete = snap in complete_set
        if in_rtb and in_man and is_complete:
            ok_hidden += 1
            continue
        issues += 1
        flags = (
            ("x" if in_rtb else "-"),
            ("x" if in_man else "-"),
            ("x" if in_remote else "-"),
            ("x" if is_complete else "-"),
        )
        hint = _matrix_hint(in_rtb, in_man, in_remote, is_complete)
        _log(f"{snap:<22} {flags[0]:^5} {flags[1]:^5} {flags[2]:^5} {flags[3]:^5}  {hint}")
    if ok_hidden:
        _log(f"({ok_hidden} Snapshot(s) mit RTB+Man+Cmp — alles ok, nicht gezeigt)")

    catchup = sorted(rtb_set - complete_set)
    manifest_no_complete = sorted(manifest_set - complete_set)
    complete_no_manifest = sorted(complete_set - manifest_set)
    remote_no_rtb = sorted(complete_set - rtb_set)
    running_rows: List[Dict[str, str]] = []
    if not args.skip_db:
        running_rows = _query_db_running(env_vars)

    if args.json:
        payload = {
            "catchup": catchup,
            "catchup_count": len(catchup),
            "incomplete_remote": incomplete_remote,
            "incomplete_count": len(incomplete_remote),
            "rtb_count": len(rtb_snaps),
            "remote_complete_count": len(complete_set),
            "running": running_rows,
            "running_count": len(running_rows),
        }
        print(json.dumps(payload, ensure_ascii=True), flush=True)
        return 0

    _log("")
    _log("--- Zusammenfassung ---")
    if not catchup:
        _log("✓ Alle RTB-Snapshots haben .upload_complete auf pCloud")
    else:
        _log(f"✗ Catch-up noetig ({len(catchup)}): {', '.join(catchup)}")

    if manifest_no_complete:
        _log(f"✗ Manifest aber NICHT complete ({len(manifest_no_complete)}): "
             f"{', '.join(manifest_no_complete)}")

    if complete_no_manifest:
        _log(f"⚠ Complete aber KEIN Manifest ({len(complete_no_manifest)}): "
             f"{', '.join(complete_no_manifest)}")

    if remote_no_rtb:
        _log(f"ℹ Complete auf pCloud, RTB lokal geloescht ({len(remote_no_rtb)}): "
             f"{', '.join(remote_no_rtb)}")

    if incomplete_remote:
        _log(f"✗ Remote-Ordner ohne .upload_complete ({len(incomplete_remote)}): "
             f"{', '.join(incomplete_remote)}")

    # --- MariaDB ---
    if not args.skip_db:
        _log("")
        _log("--- MariaDB (letzter Lauf pro Snapshot = FAILED) ---")
        db_summary = _query_db_summary(env_vars)
        if db_summary:
            _log(f"backup_runs: {db_summary}")
        failed_latest = _query_db_failed(env_vars)
        if not failed_latest and db_summary is None:
            _log("(MariaDB nicht verfuegbar oder PCLOUD_DB_PASS fehlt)")
        elif not failed_latest:
            _log("✓ Kein Snapshot mit letztem Lauf = FAILED")
        else:
            for row in failed_latest:
                snap = row["snapshot_name"]
                now_ok = snap in complete_set
                status = "→ inzwischen COMPLETE" if now_ok else "→ noch OFFEN"
                _log(f"  {snap}: {row['error_message']} ({row['started_at']}) {status}")

    _log("")
    _log(f"Fertig in {time.time() - t0:.1f}s")

    if manifest_problems or catchup or manifest_no_complete or incomplete_remote:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
