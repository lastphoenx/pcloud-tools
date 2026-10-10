#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_pool_gc.py

Pool Garbage Collector für pCloud Backup (POOL-MODE) - OPTIMIZED VERSION.

FUNKTION:
  1. Lädt content_index.json (pool_refs) für schnelle Referenz-Sammlung
  2. Optional: Deep-Audit gegen Stubs (--audit-mode)
  3. Listet alle Files in /_pool/ (1× rekursiver listfolder statt 256×)
  4. Grace Period: Löscht nur Files älter als X Stunden (Race-Protection)
  5. Löscht unreferenzierte Pool-Files (Garbage Collection)

OPTIMIERUNGEN (vs. alte Version):
  - PHASE 1: Index-basiert (0.1s) statt get_textfile (Stunden bei 100k Files!)
  - PHASE 2: 1× listfolder rekursiv statt 256× einzeln
  - Grace Period: Schützt vor Race-Conditions mit laufenden Backups
  - Audit-Mode: Optional Deep-Validation gegen physische Stubs

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf
  - NICHT während laufender Backups (oder mit ausreichender Grace Period)

PERFORMANCE:
  - Index-Load: ~0.1s (statt Stunden!)
  - Pool-Scan: ~2-5s (1× rekursiv)
  - Parallel-Delete mit ThreadPoolExecutor (8 Workers)
  - Progress-Tracking für lange Läufe

USAGE:
  # Standard-GC (Index-basiert, schnell)
  python pcloud_pool_gc.py \
    --pool-root /Backup/rtb_pool \
    --env-file .env \
    [--dry-run] \
    [--grace-hours 24] \
    [--verbose]
  
  # Deep-Audit (validiert Index gegen Stubs, langsam!)
  python pcloud_pool_gc.py \
    --pool-root /Backup/rtb_pool \
    --env-file .env \
    --audit-mode \
    --dry-run

ARGUMENTE:
  --pool-root       Remote Pool-Root auf pCloud (z.B. /Backup/rtb_pool)
  --dest-root       (deprecated) Alias fuer --pool-root
  --env-file        .env mit PCLOUD_USER + PCLOUD_PASS
  --dry-run         Zeige nur was gelöscht würde (kein echtes Löschen)
  --audit-mode      Deep-Audit: Validiere Index gegen physische Stubs
  --grace-hours     Grace Period in Stunden (default: 24)
  --verbose         Detailliertes Logging
"""

from __future__ import annotations
import os, sys, json, argparse, time, datetime, re
import threading
from typing import Set, Dict, List, Tuple, Optional
from collections import defaultdict

_SNAP_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}-\d{6}$")
_ARCHIVE_INDEX_RE = re.compile(r"^(20\d{2}-\d{2}-\d{2}-\d{6})_index\.json$")

# ---- Logging mit Timestamp (RTB-Stil) ----
def _log(msg: str, *, file=sys.stderr) -> None:
    """Log-Ausgabe mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", file=file, flush=True)


# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"Fehler: pcloud_bin_lib konnte nicht importiert werden: {e}", file=sys.stderr)
    sys.exit(2)

try:
    import pool_gc_index as gci
except Exception as e:
    gci = None  # type: ignore
    _GCI_IMPORT_ERR = str(e)
else:
    _GCI_IMPORT_ERR = ""

try:
    import pool_gc_manifest_guard as mg
    import pool_index_db as pidb
except Exception as e:
    mg = None  # type: ignore
    pidb = None  # type: ignore
    _MG_IMPORT_ERR = str(e)
else:
    _MG_IMPORT_ERR = ""


# Thread-safe Stats
class _GCStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshots_scanned = 0
        self.stubs_scanned = 0
        self.pool_files_found = 0
        self.pool_files_deleted = 0
        self.pool_files_kept = 0
        self.bytes_freed = 0
        self.errors = 0
    
    def inc_snapshots(self):
        with self.lock:
            self.snapshots_scanned += 1
    
    def inc_stubs(self, count: int = 1):
        with self.lock:
            self.stubs_scanned += count
    
    def inc_pool_found(self):
        with self.lock:
            self.pool_files_found += 1
    
    def inc_deleted(self, size_bytes: int = 0):
        with self.lock:
            self.pool_files_deleted += 1
            self.bytes_freed += size_bytes
    
    def inc_kept(self):
        with self.lock:
            self.pool_files_kept += 1
    
    def inc_errors(self):
        with self.lock:
            self.errors += 1
    
    def get_stats(self) -> dict:
        with self.lock:
            return {
                "snapshots_scanned": self.snapshots_scanned,
                "stubs_scanned": self.stubs_scanned,
                "pool_files_found": self.pool_files_found,
                "pool_files_deleted": self.pool_files_deleted,
                "pool_files_kept": self.pool_files_kept,
                "bytes_freed": self.bytes_freed,
                "errors": self.errors,
            }


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


def _snap_names(entry) -> Set[str]:
    """Snapshot-Namen aus pool_refs-Eintrag (v1/v2 tolerant)."""
    if isinstance(entry, dict):
        s = entry.get("snapshots")
        if isinstance(s, dict):
            return set(s.keys())
        if isinstance(s, list):
            return set(s)
        return set()
    if isinstance(entry, list):
        return set(entry)
    return set()


def _list_local_rtb_snaps(rtb_root: str) -> Set[str]:
    if not os.path.isdir(rtb_root):
        return set()
    return {
        name for name in os.listdir(rtb_root)
        if _SNAP_RE.match(name) and os.path.isdir(os.path.join(rtb_root, name))
    }


def _list_remote_snapshot_names(cfg: dict, snapshots_root: str) -> Set[str]:
    result = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True)
    return {
        c["name"]
        for c in (result.get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
        and _SNAP_RE.match(c.get("name", ""))
    }


def _parse_snapshot_dt(name: str) -> Optional[datetime.datetime]:
    if not _SNAP_RE.match(name):
        return None
    try:
        return datetime.datetime.strptime(name, "%Y-%m-%d-%H%M%S")
    except ValueError:
        return None


def _env_get(env_vars: Optional[Dict[str, str]], key: str, default: str = "") -> str:
    """Wert aus .env-Dict (Cron) oder os.environ (Shell)."""
    if env_vars and key in env_vars:
        return env_vars[key]
    return os.environ.get(key, default)


def _time_retention_params(env_vars: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
    days_full = int(_env_get(env_vars, "PCLOUD_REMOTE_RETENTION_DAYS_FULL", "0") or 0)
    weekly_weeks = int(_env_get(env_vars, "PCLOUD_REMOTE_RETENTION_WEEKLY_WEEKS", "54") or 54)
    return days_full, weekly_weeks


def _time_retention_enabled(env_vars: Optional[Dict[str, str]] = None) -> bool:
    return _time_retention_params(env_vars)[0] > 0


def compute_time_retention_keep(
    remote_snaps: Set[str],
    env_vars: Optional[Dict[str, str]] = None,
) -> Tuple[Set[str], Set[str]]:
    """
    Remote-Retention: alle Snapshots der letzten N Tage + aelter nur 1 pro ISO-Woche,
    maximal W Wochen in der Wochen-Tier.

    Env: PCLOUD_REMOTE_RETENTION_DAYS_FULL, PCLOUD_REMOTE_RETENTION_WEEKLY_WEEKS
    """
    days_full, weekly_weeks = _time_retention_params(env_vars)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days_full)

    keep: Set[str] = set()
    weekly_candidates: List[Tuple[Tuple[int, int], str, datetime.datetime]] = []

    for name in remote_snaps:
        dt = _parse_snapshot_dt(name)
        if dt is None:
            keep.add(name)
            continue
        if dt >= cutoff:
            keep.add(name)
        else:
            wy, wk, _ = dt.isocalendar()
            weekly_candidates.append(((wy, wk), name, dt))

    by_week: Dict[Tuple[int, int], List[Tuple[str, datetime.datetime]]] = defaultdict(list)
    for week_key, name, dt in weekly_candidates:
        by_week[week_key].append((name, dt))

    week_keepers: List[Tuple[datetime.datetime, str]] = []
    for items in by_week.values():
        name, dt = max(items, key=lambda x: x[1])
        week_keepers.append((dt, name))

    week_keepers.sort(key=lambda x: x[0])
    if weekly_weeks > 0 and len(week_keepers) > weekly_weeks:
        week_keepers = week_keepers[-weekly_weeks:]

    for _, name in week_keepers:
        keep.add(name)

    return keep, remote_snaps - keep


def _resolve_retention_deletes(
    remote_snaps: Set[str],
    local_snaps: Set[str],
    env_vars: Optional[Dict[str, str]] = None,
) -> Tuple[Set[str], str]:
    if _time_retention_enabled(env_vars):
        days_full, weekly_weeks = _time_retention_params(env_vars)
        keep, to_delete = compute_time_retention_keep(remote_snaps, env_vars)
        mode = f"zeit:{days_full}d+{weekly_weeks}w (behalten {len(keep)})"
        return to_delete, mode
    to_delete = remote_snaps - local_snaps
    return to_delete, f"rtb-spiegel (remote ohne lokales RTB, del {len(to_delete)})"


def _retention_safety_abort(
    rtb_root: str,
    local_snaps: Set[str],
    remote_snaps: Set[str],
    env_vars: Optional[Dict[str, str]],
) -> Optional[str]:
    """Verhindert Massenloeschung bei ungemountetem/leerem RTB im rtb-spiegel-Modus."""
    if _time_retention_enabled(env_vars):
        return None
    if not os.path.isdir(rtb_root):
        return f"RTB-Pfad nicht erreichbar: {rtb_root}"
    if len(local_snaps) == 0 and len(remote_snaps) > 0:
        return (
            "0 lokale Snapshots bei rtb-spiegel — "
            "vermutlich ungemountetes RTB; wuerde ALLE Remote-Snapshots loeschen"
        )
    return None


def _archive_index_remote_path(snapshots_root: str, snap: str) -> str:
    return f"{snapshots_root.rstrip('/')}/_index/archive/{snap}_index.json"


def _delete_remote_archive_index(
    cfg: dict, snapshots_root: str, snap: str, *, dry: bool = False
) -> bool:
    """Entfernt _index/archive/<snap>_index.json (Recovery-Kopie, kein Master-Index)."""
    path = _archive_index_remote_path(snapshots_root, snap)
    if dry:
        _log(f"[dry] delete archive index: {path}")
        return True
    try:
        md = pc.stat_file_safe(cfg, path=path)
        if not md or not md.get("fileid"):
            return False
        sz = int(md.get("size") or 0)
        pc.delete_file(cfg, fileid=int(md["fileid"]), size_bytes=sz)
        _log(f"[retention] ✓ Archiv-Index entfernt: {snap}_index.json")
        return True
    except Exception as e:
        _log(f"[retention][warn] Archiv-Index nicht gelöscht ({snap}): {e}")
        return False


def _purge_orphan_archive_indexes(
    cfg: dict,
    snapshots_root: str,
    remote_snaps: Set[str],
    *,
    dry: bool = False,
) -> Dict[str, int]:
    """
    Löscht <snap>_index.json unter _index/archive/, wenn kein Remote-Snapshot <snap> existiert.

    Master-Backups (content_index_prev.json, content_index_pre_v2_*) bleiben unangetastet.
    """
    archive_dir = f"{snapshots_root.rstrip('/')}/_index/archive"
    stats = {"listed": 0, "orphans": 0, "deleted": 0, "errors": 0}
    try:
        result = pc.listfolder(cfg, path=archive_dir, recursive=False, nofiles=False)
        contents = (result.get("metadata", {}) or {}).get("contents", []) or []
    except Exception as e:
        _log(f"[retention][warn] archive-Ordner nicht lesbar: {e}")
        return stats

    for it in contents:
        if it.get("isfolder"):
            continue
        name = it.get("name") or ""
        m = _ARCHIVE_INDEX_RE.match(name)
        if not m:
            continue
        stats["listed"] += 1
        snap = m.group(1)
        if snap in remote_snaps:
            continue
        stats["orphans"] += 1
        if dry:
            _log(f"[dry] orphan archive index: {name} (kein Remote-Snapshot)")
            stats["deleted"] += 1
            continue
        try:
            fid = it.get("fileid")
            if not fid:
                stats["errors"] += 1
                continue
            sz = int(it.get("size") or 0)
            pc.delete_file(cfg, fileid=int(fid), size_bytes=sz)
            stats["deleted"] += 1
            _log(f"[retention] ✓ Orphan-Archiv-Index entfernt: {name}")
        except Exception as e:
            stats["errors"] += 1
            _log(f"[retention][warn] Orphan-Archiv-Index {name}: {e}")

    if stats["orphans"]:
        _log(
            f"[retention] Archiv-Indexe: {stats['listed']} Snapshot-Archive, "
            f"{stats['orphans']} ohne Remote-Snapshot, "
            f"{stats['deleted']} gelöscht, {stats['errors']} Fehler"
        )
    elif stats["listed"]:
        _log(f"[retention] Archiv-Indexe: {stats['listed']} Snapshot-Archive, keine Orphans")
    return stats


def _pool_index_db_sync_enabled(env_vars: dict) -> bool:
    if _env_get(env_vars, "PCLOUD_POOL_INDEX_DB", "0") != "1":
        return False
    return _env_get(env_vars, "PCLOUD_POOL_INDEX_DB_SYNC_ON_GC", "1") != "0"


def _sync_pool_index_db_after_master(
    master_path: str,
    env_vars: dict,
    deleted_snaps: Set[str],
    *,
    dry: bool,
) -> None:
    """SQLite (C1) an Master-JSON anbinden nach Index-Schreiben durch GC/Retention."""
    if dry or not _pool_index_db_sync_enabled(env_vars):
        return
    mode = (_env_get(env_vars, "PCLOUD_POOL_INDEX_DB_SYNC_MODE", "auto") or "auto").lower()
    if mode == "skip":
        return
    try:
        import pool_index_db as pidb
    except Exception as e:
        _log(f"[index-db][warn] Sync übersprungen (Import): {e}")
        return

    try:
        db_path = pidb.default_db_path()
        if mode == "import":
            _log("[index-db] Sync: Streaming-Import aus Master (kann Minuten dauern)")
            with pidb.open_db(db_path, create=True) as db:
                db.import_from_json_streaming(master_path, log=_log)
            return

        if not deleted_snaps:
            _log("[index-db] Sync: nichts zu purgen — Master-mtime triggert Auto-Import beim nächsten Delta")
            return

        _log(f"[index-db] Sync: purge-snapshot für {len(deleted_snaps)} Snapshot(s)")
        with pidb.open_db(db_path, create=True) as db:
            total = 0
            for snap in sorted(deleted_snaps):
                n = db.purge_snapshot(snap)
                if n:
                    _log(f"[index-db] purge {snap}: {n} snap_refs")
                    total += n
            if os.path.isfile(master_path):
                db.refresh_master_metadata(master_path)
                digest = db.digest()["sha256"]
                db.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_content_digest', ?)",
                    (digest,),
                )
                db.conn.commit()
        _log(f"[index-db] Sync fertig ({total} snap_refs entfernt)")
    except Exception as e:
        _log(f"[index-db][warn] Sync fehlgeschlagen: {e} — nächster Delta-Lauf reimportiert aus Master")


def _extract_pool_files_from_tree(obj: dict, pool_root: str, out: List[dict]) -> None:
    if not isinstance(obj, dict):
        return
    if not obj.get("isfolder"):
        filename = (obj.get("name") or "").lower()
        if len(filename) == 64 and all(c in "0123456789abcdef" for c in filename):
            out.append({
                "name": filename,
                "path": obj.get("path") or pc.pool_file_remote_path(pool_root, filename),
                "size": obj.get("size", 0),
                "modified": obj.get("modified"),
                "fileid": obj.get("fileid"),
            })
        return
    for child in obj.get("contents", []) or []:
        _extract_pool_files_from_tree(child, pool_root, out)


def _iter_pool_files_by_prefix(cfg: dict, pool_root: str, *, quiet: bool = False):
    """Pool-Dateien pro _pool/XX-Präfix (kein einzelner Riesen-listfolder)."""
    top = pc.listfolder(cfg, path=pool_root, recursive=False, nofiles=True)
    contents = (top.get("metadata", {}) or {}).get("contents", []) or []
    prefixes = sorted(
        (
            c for c in contents
            if c.get("isfolder") and c.get("name") and len(c.get("name", "")) == 2
        ),
        key=lambda c: str(c.get("name") or ""),
    )
    if not prefixes:
        batch: List[dict] = []
        result = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
        _extract_pool_files_from_tree(result.get("metadata", {}) or {}, pool_root, batch)
        for p in batch:
            yield p
        return
    for i, child in enumerate(prefixes, 1):
        label = child.get("name", "")
        sub_path = child.get("path") or f"{pool_root.rstrip('/')}/{label}"
        if not quiet:
            _log(f"[gc] Pool-Präfix {i}/{len(prefixes)}: {label}")
        batch: List[dict] = []
        result = pc.listfolder(cfg, path=sub_path, recursive=True, nofiles=False)
        _extract_pool_files_from_tree(result.get("metadata", {}) or {}, pool_root, batch)
        for p in batch:
            yield p


def _list_pool_files(cfg: dict, pool_root: str) -> List[dict]:
    return list(_iter_pool_files_by_prefix(cfg, pool_root))


def run_retention_forecast(
    cfg: dict,
    dest_root: str,
    rtb_root: str,
    *,
    verbose: bool = False,
    env_file: str = ".env",
) -> dict:
    dest_root = pc._norm_remote_path(dest_root).rstrip("/")
    snapshots_root = f"{dest_root}/_snapshots"
    pool_root = f"{dest_root}/_pool"
    env_vars = _load_env_file(env_file)

    _log("[retention-forecast] ===== START =====")
    local_snaps = _list_local_rtb_snaps(rtb_root)
    remote_snaps = _list_remote_snapshot_names(cfg, snapshots_root)
    abort = _retention_safety_abort(rtb_root, local_snaps, remote_snaps, env_vars)
    if abort:
        _log(f"[retention-forecast][ERROR] Sicherheitsabbruch: {abort}")
        return {"aborted": abort, "local_snaps": len(local_snaps), "remote_snaps": len(remote_snaps)}
    to_delete_set, mode = _resolve_retention_deletes(remote_snaps, local_snaps, env_vars)
    to_delete = sorted(to_delete_set)
    keep_remote = remote_snaps - to_delete_set

    _log(f"[retention-forecast] Modus: {mode}")
    _log(f"[retention-forecast] RTB lokal:     {len(local_snaps)}")
    _log(f"[retention-forecast] Remote:        {len(remote_snaps)}")
    _log(f"[retention-forecast] Behalten:      {len(keep_remote)}")
    _log(f"[retention-forecast] Nachzug (del): {len(to_delete)} Snapshots")

    if to_delete:
        for snap in to_delete[:20]:
            _log(f"  - {snap}")
        if len(to_delete) > 20:
            _log(f"  ... und {len(to_delete) - 20} weitere")
    else:
        _log("[retention-forecast] Nichts zu loeschen")

    db = gci.open_ops_db_for_queries(cfg, snapshots_root, env_vars, log=_log)
    try:
        n_refs_now, n_refs_after, n_orphan, reclaim_bytes = gci.retention_index_metrics(
            db, remote_snaps, set(to_delete),
        )
    finally:
        db.close()

    _log(f"[retention-forecast] Index-SHAs (remote-gebunden): {n_refs_now}")
    _log(f"[retention-forecast] SHAs nach Retention noch referenziert: {n_refs_after}")
    _log(f"[retention-forecast] Pool-GC-Kandidaten (neu): {n_orphan}")

    _log(
        f"[retention-forecast] Pool-Einsparung (Index-Größen, simuliert): "
        f"{reclaim_bytes / (1024**3):.2f} GB ({n_orphan} SHAs)"
    )
    _log(
        "[retention-forecast] Hinweis: Einsparung aus Ops-DB sizes; "
        "physischer Pool-Abgleich nur via Pool-GC"
    )

    _log("[retention-forecast] Empfehlung:")
    if to_delete:
        _log("  1. python pcloud_pool_gc.py --retention-apply --dry-run ...")
        _log("  2. python pcloud_pool_gc.py --retention-apply --run-gc ...")
    else:
        _log("  Retention-Nachzug nicht noetig; periodischer GC reicht.")

    _log("[retention-forecast] ===== DONE =====")
    return {
        "local_snaps": len(local_snaps),
        "remote_snaps": len(remote_snaps),
        "to_delete": to_delete,
        "orphan_shas": n_orphan,
        "reclaim_bytes": reclaim_bytes,
    }


def _remote_snapshot_folder_exists(cfg: dict, snap_path: str) -> bool:
    """True wenn der Snapshot-Ordner auf pCloud noch existiert."""
    try:
        pc.listfolder(cfg, path=snap_path, recursive=False, nofiles=True)
        return True
    except Exception as e:
        err = str(e).lower()
        if "2005" in str(e) or "not found" in err or "does not exist" in err:
            return False
        # Unklarer Fehler — vorsichtshalber als „noch da“ behandeln
        return True


def _wait_until_snapshot_gone(
    cfg: dict,
    snap_path: str,
    snap: str,
    *,
    poll_sec: int,
    timeout_sec: int,
) -> bool:
    """
    pCloud deletefolderrecursive kann sofort OK liefern, Löschung läuft aber asynchron
    (große Stub-Bäume). Polling per listfolder bis Ordner weg oder Timeout.
    """
    t0 = time.time()
    while True:
        elapsed = time.time() - t0
        if not _remote_snapshot_folder_exists(cfg, snap_path):
            _log(f"[retention] ✓ {snap} remote entfernt ({elapsed:.0f}s)")
            return True
        if elapsed >= timeout_sec:
            _log(f"[retention][ERROR] Timeout {timeout_sec}s — {snap} existiert noch")
            return False
        remaining = int(timeout_sec - elapsed)
        _log(
            f"[retention] … {snap} noch auf pCloud, warte {poll_sec}s "
            f"({elapsed:.0f}s vergangen, max {timeout_sec}s)"
        )
        time.sleep(poll_sec)


def run_delete_snapshots(
    cfg: dict,
    dest_root: str,
    snap_names: List[str],
    *,
    dry: bool = False,
    run_gc: bool = False,
    grace_hours: int = 24,
    verbose: bool = False,
    env_file: str = ".env",
) -> dict:
    """
    Gezielt einzelne Remote-Snapshots löschen + pool_refs bereinigen.
    Kein Retention-Modus (zeit/rtb-spiegel) — nur explizit genannte Namen.
    """
    dest_root = pc._norm_remote_path(dest_root).rstrip("/")
    snapshots_root = f"{dest_root}/_snapshots"
    env_vars = _load_env_file(env_file)

    to_delete = sorted({s.strip() for s in snap_names if s and s.strip()})
    _log("[delete-snapshots] ===== START =====")
    if dry:
        _log("[delete-snapshots] DRY-RUN — keine Loeschungen")
    if not to_delete:
        _log("[delete-snapshots] Keine Snapshot-Namen angegeben")
        return {"deleted": 0, "errors": 0, "to_delete": []}

    remote_snaps = _list_remote_snapshot_names(cfg, snapshots_root)
    _log(f"[delete-snapshots] Explizit: {len(to_delete)} | Remote-Ordner: {len(remote_snaps)}")

    poll_sec = int(_env_get(env_vars, "PCLOUD_RETENTION_DELETE_POLL_SEC", "60") or 60)
    timeout_sec = int(_env_get(env_vars, "PCLOUD_RETENTION_DELETE_TIMEOUT_SEC", "1200") or 1200)
    errors = 0
    deleted = 0
    deleted_snaps: Set[str] = set()

    for snap in to_delete:
        snap_path = f"{snapshots_root}/{snap}"
        on_remote = snap in remote_snaps
        if not on_remote:
            _log(f"[delete-snapshots][warn] {snap} — kein Remote-Ordner (Index wird bereinigt)")
            deleted_snaps.add(snap)
            continue
        if dry:
            _log(f"[dry] deletefolderrecursive({snap_path})")
            _delete_remote_archive_index(cfg, snapshots_root, snap, dry=True)
            deleted_snaps.add(snap)
            continue
        _log(f"[delete-snapshots] Loesche ({len(deleted_snaps) + 1}/{len(to_delete)}): {snap_path}")
        try:
            pc.delete_folder(cfg, path=snap_path, recursive=True)
            _log("[delete-snapshots] API deletefolderrecursive angenommen — prüfe Entfernung …")
        except Exception as e:
            _log(f"[delete-snapshots][warn] API delete {snap}: {e} — prüfe per Polling")
        if _wait_until_snapshot_gone(
            cfg, snap_path, snap, poll_sec=poll_sec, timeout_sec=timeout_sec,
        ):
            deleted += 1
            deleted_snaps.add(snap)
            _delete_remote_archive_index(cfg, snapshots_root, snap, dry=False)
        else:
            errors += 1
            _log(f"[delete-snapshots][ERROR] {snap} noch remote — Index nicht bereinigt")

    if deleted_snaps:
        env_vars = _load_env_file(env_file)
        purge_stats = gci.apply_index_purge_for_deleted_snaps(
            cfg, snapshots_root, env_vars, deleted_snaps, dry=dry, log=_log,
        )
        _log(
            f"[delete-snapshots] Index bereinigt: {purge_stats.get('removed_snap_refs', 0)} snap-refs"
        )
        if not dry:
            master_path, _ = gci.master_paths(env_vars)
            _sync_pool_index_db_after_master(
                master_path, env_vars, deleted_snaps, dry=False,
            )
    else:
        _log("[delete-snapshots] Index unveraendert")

    remaining_remote = remote_snaps - deleted_snaps if dry else _list_remote_snapshot_names(cfg, snapshots_root)
    archive_stats = _purge_orphan_archive_indexes(
        cfg, snapshots_root, remaining_remote, dry=dry,
    )

    result = {
        "deleted": deleted,
        "errors": errors,
        "to_delete": to_delete,
        "archive_purge": archive_stats,
    }

    if run_gc and not dry:
        _log("[delete-snapshots] Starte Pool-GC …")
        gc_result = run_pool_gc(
            cfg, dest_root, dry=False, audit_mode=False,
            grace_hours=grace_hours, verbose=verbose, env_file=env_file,
        )
        result["gc"] = gc_result
    elif run_gc and dry:
        _log("[delete-snapshots] --run-gc mit --dry-run: GC separat ausfuehren")

    _log("[delete-snapshots] ===== DONE =====")
    return result


def run_retention_apply(
    cfg: dict,
    dest_root: str,
    rtb_root: str,
    *,
    dry: bool = False,
    run_gc: bool = False,
    grace_hours: int = 24,
    verbose: bool = False,
    env_file: str = ".env",
) -> dict:
    dest_root = pc._norm_remote_path(dest_root).rstrip("/")
    snapshots_root = f"{dest_root}/_snapshots"
    env_vars = _load_env_file(env_file)

    _log("[retention] ===== RETENTION APPLY =====")
    if dry:
        _log("[retention] DRY-RUN — keine Loeschungen")

    local_snaps = _list_local_rtb_snaps(rtb_root)
    remote_snaps = _list_remote_snapshot_names(cfg, snapshots_root)
    abort = _retention_safety_abort(rtb_root, local_snaps, remote_snaps, env_vars)
    if abort:
        _log(f"[retention][ERROR] Sicherheitsabbruch: {abort}")
        return {"deleted": 0, "errors": 1, "to_delete": [], "aborted": abort}
    to_delete_set, mode = _resolve_retention_deletes(remote_snaps, local_snaps, env_vars)
    to_delete = sorted(to_delete_set)

    _log(f"[retention] Modus: {mode}")
    _log(f"[retention] Zu loeschen: {len(to_delete)} Remote-Snapshots")
    poll_sec = int(_env_get(env_vars, "PCLOUD_RETENTION_DELETE_POLL_SEC", "60") or 60)
    timeout_sec = int(_env_get(env_vars, "PCLOUD_RETENTION_DELETE_TIMEOUT_SEC", "1200") or 1200)
    errors = 0
    deleted = 0
    deleted_snaps: Set[str] = set()

    for snap in to_delete:
        snap_path = f"{snapshots_root}/{snap}"
        if dry:
            _log(f"[dry] deletefolderrecursive({snap_path})")
            _delete_remote_archive_index(cfg, snapshots_root, snap, dry=True)
            deleted += 1
            deleted_snaps.add(snap)
            continue
        _log(f"[retention] Loesche Snapshot ({deleted + errors + 1}/{len(to_delete)}): {snap_path}")
        try:
            pc.delete_folder(cfg, path=snap_path, recursive=True)
            _log(f"[retention] API deletefolderrecursive angenommen — prüfe Entfernung …")
        except Exception as e:
            _log(f"[retention][warn] API delete {snap}: {e} — prüfe per Polling ob weg")
        if _wait_until_snapshot_gone(
            cfg, snap_path, snap, poll_sec=poll_sec, timeout_sec=timeout_sec,
        ):
            deleted += 1
            deleted_snaps.add(snap)
            _delete_remote_archive_index(cfg, snapshots_root, snap, dry=False)
        else:
            errors += 1
            _log(f"[retention][ERROR] Abbruch Kette bei {snap} — Index wird nicht bereinigt")
            break

    if deleted_snaps:
        purge_stats = gci.apply_index_purge_for_deleted_snaps(
            cfg, snapshots_root, env_vars, deleted_snaps, dry=dry, log=_log,
        )
        _log(
            f"[retention] Index bereinigt: {purge_stats.get('removed_snap_refs', 0)} snap-refs"
        )
        if not dry:
            master_path, _ = gci.master_paths(env_vars)
            _sync_pool_index_db_after_master(
                master_path, env_vars, deleted_snaps, dry=False,
            )
    else:
        _log("[retention] Nichts zu loeschen — Index unveraendert")

    if dry:
        remaining_remote = remote_snaps - deleted_snaps
    else:
        remaining_remote = _list_remote_snapshot_names(cfg, snapshots_root)
    archive_stats = _purge_orphan_archive_indexes(
        cfg, snapshots_root, remaining_remote, dry=dry,
    )

    result = {
        "deleted": deleted,
        "errors": errors,
        "to_delete": to_delete,
        "archive_purge": archive_stats,
    }

    if run_gc and not dry:
        _log("[retention] Starte Pool-GC nach Retention...")
        gc_result = run_pool_gc(
            cfg, dest_root, dry=False, audit_mode=False,
            grace_hours=grace_hours, verbose=verbose, env_file=env_file,
        )
        result["gc"] = gc_result
    elif run_gc and dry:
        _log("[retention] --run-gc mit --dry-run: GC separat mit --dry-run ausfuehren")

    _log("[retention] ===== DONE =====")
    return result


def _delete_pool_file(
    cfg: dict,
    pool_file_path: str,
    pool_file_size: int,
    stats: _GCStats,
    dry: bool = False,
    verbose: bool = False,
    fileid: Optional[int] = None,
) -> None:
    """
    Löscht ein Pool-File.
    
    Worker-Funktion für ThreadPoolExecutor.
    """
    label = pool_file_path or (f"fileid={fileid}" if fileid else "?")
    if dry:
        _log(f"[dry] delete: {label} ({pool_file_size} bytes)")
        stats.inc_deleted(pool_file_size)
        return
    
    try:
        del_cfg = cfg
        kwargs: dict = {"size_bytes": pool_file_size}
        if fileid:
            kwargs["fileid"] = int(fileid)
        else:
            kwargs["path"] = pool_file_path
        pc.call_with_backoff(pc.delete_file, del_cfg, **kwargs, attempts=5, max_sleep=30.0)
        stats.inc_deleted(pool_file_size)
        
        if verbose:
            _log(f"[gc-delete] ✓ {label} ({pool_file_size} bytes)")
    
    except Exception as e:
        _log(f"[gc-delete][ERROR] Failed to delete {label}: {e}")
        stats.inc_errors()


def run_pool_gc(
    cfg: dict,
    dest_root: str,
    *,
    dry: bool = False,
    audit_mode: bool = False,
    grace_hours: int = 24,
    verbose: bool = False,
    env_file: str = ".env",
) -> dict:
    """
    Führt Pool Garbage Collection aus (OPTIMIZED VERSION).
    
    STRATEGIE:
    1. Index-basiert (pool_refs) → 0.1s statt Stunden!
    2. Optional: Audit-Mode (validiert Index gegen Stubs)
    3. Rekursiver Pool-Scan (1 API-Call statt 256)
    4. Grace Period (nur Files > X Stunden alt löschen)
    5. Parallel-Delete (Thread-safe)
    
    Args:
        cfg: pCloud Config
        dest_root: Remote Root (z.B. /Backup/rtb_1to1)
        dry: Dry-run Mode
        audit_mode: Deep-Audit (validiert Index gegen Stubs, langsam!)
        grace_hours: Grace Period in Stunden (Race-Protection)
        verbose: Verbose Logging
    
    Returns:
        Stats Dict
    """
    t_start = time.time()
    
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    pool_root = f"{dest_root.rstrip('/')}/_pool"
    env_vars = _load_env_file(env_file)
    
    _log("[gc] ===== POOL GARBAGE COLLECTION START =====")
    _log(f"[gc] Mode: {'AUDIT (Deep-Validation)' if audit_mode else 'STANDARD (Index-basiert)'}")
    _log(f"[gc] Snapshots: {snapshots_root}")
    _log(f"[gc] Pool: {pool_root}")
    _log(f"[gc] Grace Period: {grace_hours}h")
    _log(f"[gc] Dry-Run: {dry}")

    if audit_mode:
        _log(
            "[gc][ERROR] --audit-mode ist deaktiviert bis Streaming-Umbau "
            "(kein millionenfaches Referenz-Set im RAM). Standard-GC nutzen."
        )
        return {"error": "audit_mode_disabled", "aborted": True}
    
    # ============================================================================
    # === GC-LOCK CHECK (Race-Protection gegen parallel laufende Backups!) ===
    # ============================================================================
    lock_path = f"{dest_root}/.gc_lock"
    stale_lock_hours = int(os.environ.get("PCLOUD_GC_STALE_LOCK_HOURS", "48"))
    
    try:
        lock_content = pc.get_textfile(cfg, path=lock_path)
        lock_data = json.loads(lock_content)
        
        # Lock-Alter berechnen
        lock_age_seconds = time.time() - lock_data.get("started_at", 0)
        lock_age_hours = lock_age_seconds / 3600
        
        _log(f"[gc] ⚠️ GC-Lock erkannt!")
        _log(f"[gc]    Snapshot: {lock_data.get('snapshot', '?')}")
        _log(f"[gc]    Host: {lock_data.get('host', '?')}")
        _log(f"[gc]    PID: {lock_data.get('pid', '?')}")
        _log(f"[gc]    Alter: {lock_age_hours:.1f}h")
        
        # Stale-Lock Check
        if lock_age_hours < stale_lock_hours:
            # Lock ist frisch → Backup läuft!
            _log(f"[gc] ❌ ABBRUCH: Backup läuft (Lock < {stale_lock_hours}h alt)")
            _log(f"[gc]    Warte bis Backup abgeschlossen ist, dann versuche erneut")
            return {
                "error": "backup_in_progress",
                "lock_age_hours": lock_age_hours,
                "snapshot": lock_data.get("snapshot"),
                "aborted": True
            }
        else:
            # Lock ist stale → Backup wahrscheinlich crashed
            _log(f"[gc] ⚠️ STALE LOCK erkannt (>{stale_lock_hours}h alt)")
            _log(f"[gc]    Annahme: Backup-Prozess abgestürzt, fahre mit GC fort")
            _log(f"[gc]    Lock wird ignoriert (nicht gelöscht für Debugging)")
    
    except Exception:
        # Kein Lock vorhanden → alles ok!
        _log(f"[gc] ✓ Kein GC-Lock gefunden, fahre fort")
    
    # Stats
    stats = _GCStats()
    
    # ============================================================================
    # === PHASE 1: Sammle referenzierte SHA256s ===
    # ============================================================================
    ref_lookup: Optional[gci.RemoteSnapReferencedShaLookup] = None
    referenced_sha256s: object = set()
    manifest_protected_store = None
    manifest_guard_meta: Dict[str, object] = {}
    try:
        remote_snaps = _list_remote_snapshot_names(cfg, snapshots_root)
        _log("[gc] PHASE 1: Index (gc-engine / SQLite, kein Master-Dict im RAM)")
        t_scan_start = time.time()
        ref_lookup = gci.referenced_shas_for_gc(
            cfg, snapshots_root, env_vars, remote_snaps, log=_log,
        )
        referenced_sha256s = ref_lookup
        scan_duration = time.time() - t_scan_start
        _log(
            f"[gc] PHASE 1 DONE: {len(remote_snaps)} remote snapshots, "
            f"{len(ref_lookup)} unique SHA256 ({scan_duration:.1f}s)"
        )

        if mg is not None and pidb is not None:
            ops_db = pidb.default_ops_db_path(env_vars)
            guard = mg.build_gc_manifest_guard(
                remote_snaps, ops_db, env_vars, log=_log,
            )
            manifest_protected_store = guard.protected
            manifest_guard_meta = {
                "manifest_guard_missing_in_ops_db": guard.missing_in_ops_db,
                "manifest_guard_zero_refs_in_ops_db": guard.zero_refs_in_ops_db,
                "manifest_guard_gap_snapshots": guard.manifest_gap_snapshots,
                "manifest_guard_protected_shas": len(guard.protected),
            }
            if guard.abort_reason:
                if manifest_protected_store is not None:
                    manifest_protected_store.close()
                return {
                    "error": guard.abort_reason,
                    "aborted": True,
                    **manifest_guard_meta,
                }
            referenced_sha256s = mg.ManifestProtectedShaLookup(
                ref_lookup, guard.protected,
            )
        elif _MG_IMPORT_ERR:
            _log(f"[gc-guard][ERROR] Manifest-Guard nicht geladen: {_MG_IMPORT_ERR}")
            return {"error": "manifest_guard_import_failed", "aborted": True}

        if len(ref_lookup) == 0:
            _log(
                "[gc][ERROR] Keine Referenzen in Ops-Index — Abbruch (Sicherheit). "
                "Ops-DB leer/inkonsistent oder kein Remote-Sync; kein Stub-Scan-Fallback."
            )
            return {"error": "No references found"}

        # ============================================================================
        # === PHASE 2: Liste alle Pool-Files (REKURSIV, 1 API-Call!) ===
        # ============================================================================
        _log("[gc] PHASE 2: Listing pool files (by _pool/XX prefix)...")
        t_list_start = time.time()

        pool_files_to_delete = []
        grace_cutoff = time.time() - (grace_hours * 3600) if grace_hours > 0 else 0
        pool_files_found = 0

        try:
            _log(f"[gc] Checking references (grace period: {grace_hours}h)...")

            for pool_file in _iter_pool_files_by_prefix(cfg, pool_root):
                pool_files_found += 1
                sha256 = pool_file["name"]
                stats.inc_pool_found()

                if sha256 in referenced_sha256s:
                    stats.inc_kept()
                    continue

                if grace_hours > 0 and pool_file.get("modified"):
                    file_mtime = pc.parse_metadata_modified_ts(pool_file["modified"])
                    if file_mtime is None:
                        stats.inc_kept()
                        if verbose:
                            _log(f"[gc-grace] Keeping {sha256[:16]}... (modified unparseable)")
                        continue
                    if file_mtime > grace_cutoff:
                        stats.inc_kept()
                        if verbose:
                            age_hours = (time.time() - file_mtime) / 3600
                            _log(
                                f"[gc-grace] Keeping {sha256[:16]}... "
                                f"(age: {age_hours:.1f}h < {grace_hours}h)"
                            )
                        continue

                pool_files_to_delete.append(pool_file)

            list_duration = time.time() - t_list_start
            _log(f"[gc] PHASE 2 DONE: {pool_files_found} pool files found ({list_duration:.1f}s)")

        except Exception as e:
            _log(f"[gc][ERROR] Failed to list pool: {e}")
            return {"error": str(e)}

        check_stats = stats.get_stats()
        _log(
            f"[gc] Check complete: {len(pool_files_to_delete)} to delete, "
            f"{check_stats['pool_files_kept']} to keep"
        )

        # ============================================================================
        # === PHASE 3: Lösche unreferenzierte Pool-Files ===
        # ============================================================================
        if pool_files_to_delete:
            _log(f"[gc] PHASE 3: Deleting {len(pool_files_to_delete)} unreferenced pool files...")
            t_delete_start = time.time()

            for pool_file in pool_files_to_delete:
                _delete_pool_file(
                    cfg,
                    pool_file["path"],
                    pool_file["size"],
                    stats,
                    dry,
                    verbose,
                    fileid=pool_file.get("fileid"),
                )

            delete_duration = time.time() - t_delete_start
            delete_stats = stats.get_stats()

            _log(
                f"[gc] PHASE 3 DONE: {delete_stats['pool_files_deleted']} files deleted, "
                f"{delete_stats['bytes_freed'] / (1024**3):.2f} GB freed ({delete_duration:.1f}s)"
            )
        else:
            _log("[gc] PHASE 3 SKIPPED: No unreferenced files found")

        duration = time.time() - t_start
        final_stats = stats.get_stats()

        _log("[gc] ===== POOL GARBAGE COLLECTION DONE =====")
        _log("[gc] Mode: INDEX-BASED")
        _log(f"[gc] Duration: {duration:.1f}s")
        _log(f"[gc] Unique SHA256 refs: {len(referenced_sha256s)}")
        _log(f"[gc] Pool files found: {final_stats['pool_files_found']}")
        _log(f"[gc] Pool files kept: {final_stats['pool_files_kept']}")
        _log(f"[gc] Pool files deleted: {final_stats['pool_files_deleted']}")
        _log(f"[gc] Space freed: {final_stats['bytes_freed'] / (1024**3):.2f} GB")
        _log(f"[gc] Errors: {final_stats['errors']}")

        if dry:
            _log("[gc] ⚠ DRY-RUN: Keine echten Löschungen durchgeführt")

        out = {
            "duration": duration,
            "mode": "index",
            "unique_refs": len(referenced_sha256s),
            "pool_files_found": final_stats["pool_files_found"],
            "pool_files_kept": final_stats["pool_files_kept"],
            "pool_files_deleted": final_stats["pool_files_deleted"],
            "bytes_freed": final_stats["bytes_freed"],
            "errors": final_stats["errors"],
        }
        out.update(manifest_guard_meta)
        return out
    finally:
        if manifest_protected_store is not None:
            manifest_protected_store.close()
        if ref_lookup is not None:
            ref_lookup.close()


# ---- CLI ----
def main():
    parser = argparse.ArgumentParser(
        description="pCloud Pool Garbage Collector (POOL-MODE) - OPTIMIZED",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
BEISPIEL (Standard - Index-basiert, schnell):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --dry-run
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --verbose

BEISPIEL (Audit-Mode - validiert Index gegen Stubs, langsam!):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --audit-mode --dry-run

BEISPIEL (mit Grace Period 48h):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --grace-hours 48

BEISPIEL (Retention — Forecast, read-only):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --retention-forecast --rtb-root /mnt/backup/rtb_nas

BEISPIEL (Retention — scharf + Pool-GC):
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --retention-apply --run-gc --env-file .env
  python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --retention-apply --dry-run

WANN AUSFÜHREN:
  - Nach Retention (wenn Snapshots gelöscht wurden)
  - Periodisch (z.B. wöchentlich via Cron)
  - Manuell bei Platzbedarf
  - NICHT während laufender Backups (oder mit ausreichender Grace Period)

OPTIMIERUNGEN:
  - Index-basiert: 0.1s statt Stunden (bei 100k Files)!
  - Rekursiver Pool-Scan: 1× API-Call statt 256×
  - Grace Period: Schützt vor Race-Conditions
  - Audit-Mode: Optional Deep-Validation

CRON BEISPIEL (wöchentlich, Sonntag 3 Uhr, 24h Grace):
  0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --grace-hours 24 >> /var/log/backup/pool_gc.log 2>&1
"""
    )
    
    parser.add_argument("--pool-root",
                        help="Remote Pool-Root auf pCloud, z.B. /Backup/rtb_pool")
    parser.add_argument("--dest-root",
                        help="(deprecated) Alias fuer --pool-root")
    parser.add_argument("--env-file", default=".env",
                        help=".env Datei mit PCLOUD_USER + PCLOUD_PASS (default: .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Dry-run: Zeige nur was gelöscht würde")
    parser.add_argument("--audit-mode", action="store_true",
                        help="Audit-Mode: Validiere Index gegen physische Stubs (langsam!)")
    parser.add_argument("--grace-hours", type=int, default=24,
                        help="Grace Period in Stunden (default: 24, 0=deaktiviert)")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose Logging")
    parser.add_argument("--retention-forecast", action="store_true",
                        help="Retention-Simulation: RTB vs. Remote, GC-Einsparung schaetzen (read-only)")
    parser.add_argument("--retention-apply", action="store_true",
                        help="Retention scharf: Remote-Snapshots ohne lokales RTB loeschen + Index bereinigen")
    parser.add_argument("--rtb-root", default=None,
                        help="Lokales RTB-Verzeichnis (default: RTB aus .env oder /mnt/backup/rtb_nas)")
    parser.add_argument("--run-gc", action="store_true",
                        help="Nach --retention-apply Pool-GC ausfuehren (ohne --dry-run)")
    parser.add_argument(
        "--delete-snapshots",
        metavar="SNAP,...",
        help="Gezielt Remote-Snapshots löschen (Komma-Liste), Index bereinigen — kein Retention-Modus",
    )
    
    args = parser.parse_args()

    if args.retention_forecast and args.retention_apply:
        parser.error("--retention-forecast und --retention-apply schliessen sich aus")
    if args.delete_snapshots and (args.retention_forecast or args.retention_apply):
        parser.error("--delete-snapshots nicht mit --retention-forecast/--retention-apply kombinieren")

    pool_root = args.pool_root or args.dest_root
    cfg = pc.effective_config(env_file=args.env_file)
    env_vars = _load_env_file(args.env_file)
    if not pool_root:
        pool_root = env_vars.get("PCLOUD_DEST") or os.environ.get("PCLOUD_DEST")
    if not pool_root:
        parser.error(
            "--pool-root erforderlich (oder PCLOUD_DEST in --env-file / Umgebung)"
        )
    if args.dest_root and not args.pool_root:
        _log("--dest-root ist deprecated, bitte --pool-root verwenden")
    rtb_root = args.rtb_root or env_vars.get("RTB") or os.environ.get("RTB", "/mnt/backup/rtb_nas")

    if gci is None:
        _log(
            f"[gc-engine][ERROR] pool_gc_index nicht geladen ({_GCI_IMPORT_ERR}) — "
            "pip install -r requirements.txt (ijson)."
        )
        sys.exit(2)

    if args.retention_forecast:
        run_retention_forecast(
            cfg, pool_root, rtb_root, verbose=args.verbose, env_file=args.env_file,
        )
        sys.exit(0)

    if args.retention_apply:
        result = run_retention_apply(
            cfg, pool_root, rtb_root,
            dry=args.dry_run,
            run_gc=args.run_gc,
            grace_hours=args.grace_hours,
            verbose=args.verbose,
            env_file=args.env_file,
        )
        if result.get("errors", 0) > 0:
            _log("[retention] ⚠ Abgeschlossen mit Fehlern")
            sys.exit(1)
        _log("[retention] ✓ Erfolgreich abgeschlossen")
        sys.exit(0)

    if args.delete_snapshots:
        snap_list = [s.strip() for s in args.delete_snapshots.split(",") if s.strip()]
        result = run_delete_snapshots(
            cfg, pool_root, snap_list,
            dry=args.dry_run,
            run_gc=args.run_gc,
            grace_hours=args.grace_hours,
            verbose=args.verbose,
            env_file=args.env_file,
        )
        if result.get("errors", 0) > 0:
            _log("[delete-snapshots] ⚠ Abgeschlossen mit Fehlern")
            sys.exit(1)
        _log("[delete-snapshots] ✓ Erfolgreich abgeschlossen")
        sys.exit(0)
    
    # Run GC
    result = run_pool_gc(
        cfg,
        pool_root,
        dry=args.dry_run,
        audit_mode=args.audit_mode,
        grace_hours=args.grace_hours,
        verbose=args.verbose,
        env_file=args.env_file,
    )
    
    # Exit Code
    if result.get("error"):
        _log(f"[gc] ⚠ GC abgebrochen: {result['error']}")
        sys.exit(1)
    if result.get("errors", 0) > 0:
        _log("[gc] ⚠ GC abgeschlossen mit Fehlern")
        sys.exit(1)
    elif result.get("aborted"):
        sys.exit(2)
    else:
        _log("[gc] ✓ GC erfolgreich abgeschlossen")
        sys.exit(0)


if __name__ == "__main__":
    main()
