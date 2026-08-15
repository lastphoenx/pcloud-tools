#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_index_db.py — C1 local SQLite working index for pool_refs.

Hybrid: SQLite on SSD is the delta-mode working store. Finalize streams
v2 content_index.json (same schema as today) for the existing chunk upload.
No pcloud_bin_lib import — stdlib only, offline-testable.

Schema (integer IDs, WAL):
  shas(id, sha UNIQUE, fileid, hash, size)
  snapshots(id, name UNIQUE)
  snap_refs(snap_id, sha_id, relpath)  PRIMARY KEY  — relpath '' = sentinel []
  meta(key, value)

Zero-ref SHAs stay in `shas` (GC visibility: export snapshots={}).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
_HASH_INT_MAX = (1 << 63) - 1

LogFn = Callable[[str], None]


def _hash_file_sha256(path: str, block_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(block_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def default_db_path() -> str:
    override = os.environ.get("PCLOUD_POOL_INDEX_DB_PATH")
    if override:
        return override
    archive = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    return os.path.join(archive, "indexes", "pool_index.sqlite3")


def default_master_path() -> str:
    archive = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    return os.path.join(archive, "indexes", "content_index_master.json")


def _hash_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _digest_coord(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _export_hash(value: Optional[str]) -> Any:
    if value is None or value == "":
        return None
    try:
        n = int(value)
        if -_HASH_INT_MAX - 1 <= n <= _HASH_INT_MAX:
            return n
    except (TypeError, ValueError):
        pass
    return value


def _snapshots_map(entry: Any) -> Dict[str, List[str]]:
    """Normalize pool_refs entry to {snap: [relpaths]} (v2 / v1 list / bare list)."""
    if isinstance(entry, list):
        return {str(n): [] for n in entry}
    if not isinstance(entry, dict):
        return {}
    s = entry.get("snapshots")
    if isinstance(s, dict):
        out: Dict[str, List[str]] = {}
        for k, v in s.items():
            if isinstance(v, list):
                out[str(k)] = [str(x) if x is not None else "" for x in v]
            else:
                out[str(k)] = []
        return out
    if isinstance(s, list):
        return {str(n): [] for n in s}
    return {}


def _coords(entry: Any) -> Tuple[Any, Any, Any]:
    if not isinstance(entry, dict):
        return None, None, None
    return entry.get("fileid"), entry.get("hash"), entry.get("size")


def digest_from_json(json_path: str) -> dict:
    """Canonical digest of a v2 (or legacy) content_index JSON file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pool_refs = data.get("pool_refs") or {}
    h = hashlib.sha256()
    n_pairs = 0
    items = []
    for sha_raw, entry in pool_refs.items():
        sha_l = (sha_raw or "").lower()
        if not sha_l:
            continue
        items.append((sha_l, entry))
    items.sort(key=lambda x: x[0])
    for sha_l, entry in items:
        fileid, phash, size = _coords(entry)
        h.update(
            f"{sha_l}|{_digest_coord(fileid)}|{_digest_coord(phash)}|{_digest_coord(size)}\n".encode()
        )
    pair_lines = []
    for sha_l, entry in items:
        for snap, rels in _snapshots_map(entry).items():
            if not rels:
                pair_lines.append((sha_l, snap, ""))
            else:
                for rp in rels:
                    pair_lines.append((sha_l, snap, rp or ""))
    pair_lines.sort()
    for sha_l, snap, rp in pair_lines:
        n_pairs += 1
        h.update(f"{sha_l}|{snap}|{rp}\n".encode())
    return {
        "n_shas": len(items),
        "n_pairs": n_pairs,
        "sha256": h.hexdigest(),
    }


class PoolIndexDB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_schema()

    def _apply_pragmas(self, *, import_fast: bool = False) -> None:
        c = self.conn
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=OFF" if import_fast else "PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA cache_size=-65536")
        c.execute("PRAGMA mmap_size=268435456")
        c.execute("PRAGMA foreign_keys=ON")

    def _init_schema(self) -> None:
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            );
            CREATE TABLE IF NOT EXISTS shas (
              id INTEGER PRIMARY KEY,
              sha TEXT NOT NULL UNIQUE,
              fileid INTEGER,
              hash TEXT,
              size INTEGER
            );
            CREATE TABLE IF NOT EXISTS snapshots (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS snap_refs (
              snap_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
              sha_id INTEGER NOT NULL REFERENCES shas(id) ON DELETE CASCADE,
              relpath TEXT NOT NULL,
              PRIMARY KEY (snap_id, sha_id, relpath)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS idx_snap_refs_sha ON snap_refs(sha_id);
            CREATE INDEX IF NOT EXISTS idx_snap_refs_relpath ON snap_refs(snap_id, relpath);
            """
        )
        cur = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if cur is None:
            c.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('index_version', '2')"
            )
            c.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PoolIndexDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def count_shas(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM shas").fetchone()[0])

    def snapshot_names(self) -> List[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM snapshots ORDER BY name"
            )
        ]

    def snapshot_pair_count(self, snapshot: str) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM snap_refs r
            JOIN snapshots n ON n.id = r.snap_id
            WHERE n.name = ?
            """,
            (snapshot,),
        ).fetchone()
        return int(row[0])

    def _ensure_snapshot(self, name: str) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO snapshots(name) VALUES (?)", (name,)
        )
        row = self.conn.execute(
            "SELECT id FROM snapshots WHERE name=?", (name,)
        ).fetchone()
        return int(row[0])

    def _snapshot_id(self, name: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT id FROM snapshots WHERE name=?", (name,)
        ).fetchone()
        return None if row is None else int(row[0])

    def import_from_json(self, json_path: str, *, log: Optional[LogFn] = None) -> dict:
        t0 = time.time()
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pool_refs = data.get("pool_refs") or {}
        if log:
            log(f"[index-db] Import {len(pool_refs)} pool_refs aus {json_path}")

        self._apply_pragmas(import_fast=True)
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("DELETE FROM snap_refs")
            c.execute("DELETE FROM snapshots")
            c.execute("DELETE FROM shas")

            sha_rows: List[Tuple[str, Any, Optional[str], Any]] = []
            ref_rows: List[Tuple[str, str, str]] = []
            snap_names: set[str] = set()

            for sha_raw, entry in pool_refs.items():
                sha = (sha_raw or "").lower()
                if not sha:
                    continue
                fileid, phash, size = _coords(entry)
                sha_rows.append((sha, fileid, _hash_text(phash), size))
                for snap, rels in _snapshots_map(entry).items():
                    snap_names.add(snap)
                    if not rels:
                        ref_rows.append((sha, snap, ""))
                    else:
                        for rp in rels:
                            ref_rows.append((sha, snap, rp if rp is not None else ""))

            c.executemany(
                "INSERT OR IGNORE INTO snapshots(name) VALUES (?)",
                [(n,) for n in snap_names],
            )
            c.executemany(
                "INSERT OR IGNORE INTO shas(sha, fileid, hash, size) VALUES (?,?,?,?)",
                sha_rows,
            )
            name_to_id = {
                r[0]: r[1] for r in c.execute("SELECT name, id FROM snapshots")
            }
            sha_to_id = {r[0]: r[1] for r in c.execute("SELECT sha, id FROM shas")}
            c.executemany(
                "INSERT OR IGNORE INTO snap_refs(snap_id, sha_id, relpath) VALUES (?,?,?)",
                [
                    (name_to_id[snap], sha_to_id[sha], rp)
                    for sha, snap, rp in ref_rows
                    if snap in name_to_id and sha in sha_to_id
                ],
            )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('imported_from', ?)",
                (os.path.abspath(json_path),),
            )
            c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('imported_at', ?)",
                (str(time.time()),),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._apply_pragmas(import_fast=False)

        try:
            c.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass

        st = {
            "shas": self.count_shas(),
            "snapshots": len(self.snapshot_names()),
            "pairs": int(c.execute("SELECT COUNT(*) FROM snap_refs").fetchone()[0]),
            "seconds": time.time() - t0,
        }
        self.refresh_master_metadata(json_path)
        content_digest = self.digest()["sha256"]
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_content_digest', ?)",
            (content_digest,),
        )
        self.conn.commit()
        if log:
            log(
                f"[index-db] Import fertig: {st['shas']} SHAs, {st['pairs']} Paare "
                f"in {st['seconds']:.1f}s"
            )
        return st

    def register_batch(
        self,
        snapshot: str,
        rows: Iterable[Tuple[Any, Any, Any, Any, Any]],
    ) -> int:
        """rows: (sha, relpath, fileid, hash, size). Coords fill NULLs only."""
        batch: List[Tuple[str, str, Any, Optional[str], Any]] = []
        for sha, relpath, fileid, phash, size in rows:
            sha_l = (sha or "").lower()
            if not sha_l:
                continue
            batch.append(
                (sha_l, relpath if relpath is not None else "", fileid, _hash_text(phash), size)
            )
        if not batch:
            return 0

        c = self.conn
        c.execute("BEGIN")
        try:
            snap_id = self._ensure_snapshot(snapshot)
            c.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _batch("
                "sha TEXT, relpath TEXT, fileid INTEGER, hash TEXT, size INTEGER)"
            )
            c.execute("DELETE FROM _batch")
            c.executemany("INSERT INTO _batch VALUES (?,?,?,?,?)", batch)
            c.execute(
                """
                INSERT INTO shas(sha, fileid, hash, size)
                SELECT sha, fileid, hash, size FROM _batch
                WHERE true
                ON CONFLICT(sha) DO UPDATE SET
                  fileid = CASE
                    WHEN shas.fileid IS NULL AND excluded.fileid IS NOT NULL
                    THEN excluded.fileid ELSE shas.fileid END,
                  hash = CASE
                    WHEN shas.hash IS NULL AND excluded.hash IS NOT NULL
                    THEN excluded.hash ELSE shas.hash END,
                  size = CASE
                    WHEN shas.size IS NULL AND excluded.size IS NOT NULL
                    THEN excluded.size ELSE shas.size END
                """
            )
            before = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (snap_id,)
                ).fetchone()[0]
            )
            c.execute(
                """
                INSERT OR IGNORE INTO snap_refs(snap_id, sha_id, relpath)
                SELECT ?, s.id, b.relpath
                FROM _batch b
                JOIN shas s ON s.sha = b.sha
                """,
                (snap_id,),
            )
            after = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (snap_id,)
                ).fetchone()[0]
            )
            c.execute("DROP TABLE IF EXISTS _batch")
            c.commit()
            return after - before
        except Exception:
            c.rollback()
            raise

    def merge_basis_snapshot(
        self,
        new_snapshot: str,
        basis_snapshot: str,
        cur_files: Mapping[str, str],
    ) -> dict:
        """Carry (sha, relpath) from basis iff cur_files[relpath] == sha."""
        t0 = time.time()
        basis_id = self._snapshot_id(basis_snapshot)
        if basis_id is None:
            return {
                "merged": 0,
                "skipped_removed": 0,
                "skipped_changed": 0,
                "seconds": time.time() - t0,
                "basis_missing": True,
            }

        pairs = [
            (rp, (sha or "").lower())
            for rp, sha in cur_files.items()
            if sha
        ]
        c = self.conn
        c.execute("BEGIN")
        try:
            new_id = self._ensure_snapshot(new_snapshot)
            c.execute(
                "CREATE TEMP TABLE IF NOT EXISTS cur_files("
                "relpath TEXT PRIMARY KEY, sha TEXT NOT NULL) WITHOUT ROWID"
            )
            c.execute("DELETE FROM cur_files")
            if pairs:
                c.executemany("INSERT OR REPLACE INTO cur_files VALUES (?,?)", pairs)

            skipped_removed = int(
                c.execute(
                    """
                    SELECT COUNT(*) FROM snap_refs r
                    LEFT JOIN cur_files cf ON cf.relpath = r.relpath
                    WHERE r.snap_id = ? AND cf.relpath IS NULL
                    """,
                    (basis_id,),
                ).fetchone()[0]
            )
            skipped_changed = int(
                c.execute(
                    """
                    SELECT COUNT(*) FROM snap_refs r
                    JOIN shas s ON s.id = r.sha_id
                    JOIN cur_files cf ON cf.relpath = r.relpath
                    WHERE r.snap_id = ? AND cf.sha != s.sha
                    """,
                    (basis_id,),
                ).fetchone()[0]
            )

            before = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (new_id,)
                ).fetchone()[0]
            )
            c.execute(
                """
                INSERT OR IGNORE INTO snap_refs(snap_id, sha_id, relpath)
                SELECT ?, r.sha_id, r.relpath
                FROM snap_refs r
                JOIN shas s ON s.id = r.sha_id
                JOIN cur_files cf ON cf.relpath = r.relpath AND cf.sha = s.sha
                WHERE r.snap_id = ?
                """,
                (new_id, basis_id),
            )
            after = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (new_id,)
                ).fetchone()[0]
            )
            c.execute("DROP TABLE IF EXISTS cur_files")
            c.commit()
        except Exception:
            c.rollback()
            raise

        return {
            "merged": after - before,
            "skipped_removed": skipped_removed,
            "skipped_changed": skipped_changed,
            "seconds": time.time() - t0,
            "basis_missing": False,
        }

    def merge_basis_from_archive_json(
        self,
        new_snapshot: str,
        archive_json_path: str,
        cur_files: Mapping[str, str],
    ) -> dict:
        """Same carry rule, seeded from a filtered archive/<basis>_index.json."""
        t0 = time.time()
        with open(archive_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pool_refs = data.get("pool_refs") or {}
        want = {(rp, (sha or "").lower()) for rp, sha in cur_files.items() if sha}

        obj_rows: List[Tuple[str, Any, Optional[str], Any]] = []
        ref_rows: List[Tuple[str, str]] = []
        skipped_removed = 0
        skipped_changed = 0

        for sha_raw, entry in pool_refs.items():
            sha = (sha_raw or "").lower()
            if not sha:
                continue
            fileid, phash, size = _coords(entry)
            obj_rows.append((sha, fileid, _hash_text(phash), size))
            for _snap, rels in _snapshots_map(entry).items():
                paths = rels if rels else [""]
                for rp in paths:
                    key = (rp, sha)
                    if key in want:
                        ref_rows.append((sha, rp))
                    elif rp not in cur_files:
                        skipped_removed += 1
                    else:
                        skipped_changed += 1

        c = self.conn
        c.execute("BEGIN")
        try:
            new_id = self._ensure_snapshot(new_snapshot)
            if obj_rows:
                c.executemany(
                    """
                    INSERT INTO shas(sha, fileid, hash, size) VALUES (?,?,?,?)
                    ON CONFLICT(sha) DO UPDATE SET
                      fileid = CASE
                        WHEN shas.fileid IS NULL AND excluded.fileid IS NOT NULL
                        THEN excluded.fileid ELSE shas.fileid END,
                      hash = CASE
                        WHEN shas.hash IS NULL AND excluded.hash IS NOT NULL
                        THEN excluded.hash ELSE shas.hash END,
                      size = CASE
                        WHEN shas.size IS NULL AND excluded.size IS NOT NULL
                        THEN excluded.size ELSE shas.size END
                    """,
                    obj_rows,
                )
            sha_to_id = {r[0]: r[1] for r in c.execute("SELECT sha, id FROM shas")}
            before = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (new_id,)
                ).fetchone()[0]
            )
            if ref_rows:
                c.executemany(
                    "INSERT OR IGNORE INTO snap_refs(snap_id, sha_id, relpath) VALUES (?,?,?)",
                    [(new_id, sha_to_id[sha], rp) for sha, rp in ref_rows if sha in sha_to_id],
                )
            after = int(
                c.execute(
                    "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (new_id,)
                ).fetchone()[0]
            )
            c.commit()
        except Exception:
            c.rollback()
            raise

        return {
            "merged": after - before,
            "skipped_removed": skipped_removed,
            "skipped_changed": skipped_changed,
            "seconds": time.time() - t0,
            "basis_missing": False,
        }

    def merge_cloned_refs(
        self,
        new_snapshot: str,
        basis_snapshot: str,
        cur_files: Mapping[str, str],
        *,
        archive_json_path: Optional[str] = None,
    ) -> dict:
        """Three-tier: db → archive JSON → full manifest register_batch."""
        if self.snapshot_pair_count(basis_snapshot) > 0:
            st = self.merge_basis_snapshot(new_snapshot, basis_snapshot, cur_files)
            st["tier"] = "db"
            return st
        if archive_json_path and os.path.isfile(archive_json_path):
            st = self.merge_basis_from_archive_json(
                new_snapshot, archive_json_path, cur_files
            )
            st["tier"] = "archive_json"
            return st
        t0 = time.time()
        n = self.register_batch(
            new_snapshot,
            ((sha, rp, None, None, None) for rp, sha in cur_files.items()),
        )
        return {
            "merged": n,
            "skipped_removed": 0,
            "skipped_changed": 0,
            "seconds": time.time() - t0,
            "basis_missing": True,
            "tier": "manifest",
        }

    def purge_snapshot(self, snapshot: str) -> int:
        sid = self._snapshot_id(snapshot)
        if sid is None:
            return 0
        c = self.conn
        n = int(
            c.execute(
                "SELECT COUNT(*) FROM snap_refs WHERE snap_id=?", (sid,)
            ).fetchone()[0]
        )
        c.execute("DELETE FROM snap_refs WHERE snap_id=?", (sid,))
        c.commit()
        return n

    def prune_snapshots(self, keep_names: Iterable[str]) -> int:
        keep = set(keep_names)
        names = self.snapshot_names()
        removed = 0
        c = self.conn
        c.execute("BEGIN")
        try:
            for name in names:
                if name not in keep:
                    sid = self._snapshot_id(name)
                    if sid is None:
                        continue
                    c.execute("DELETE FROM snapshots WHERE id=?", (sid,))
                    removed += 1
            c.commit()
        except Exception:
            c.rollback()
            raise
        return removed

    def build_snapshot_index(self, snapshot: str) -> dict:
        sid = self._snapshot_id(snapshot)
        if sid is None:
            return {"version": 2, "pool_refs": {}}
        filtered: Dict[str, dict] = {}
        for row in self.conn.execute(
            """
            SELECT s.sha, s.fileid, s.hash, s.size, r.relpath
            FROM snap_refs r
            JOIN shas s ON s.id = r.sha_id
            WHERE r.snap_id = ?
            ORDER BY s.sha, r.relpath
            """,
            (sid,),
        ):
            sha, fileid, phash, size, relpath = (
                row[0], row[1], row[2], row[3], row[4]
            )
            entry = filtered.get(sha)
            if entry is None:
                entry = {
                    "fileid": fileid,
                    "hash": _export_hash(phash),
                    "size": size,
                    "snapshots": {snapshot: []},
                }
                filtered[sha] = entry
            if relpath:
                entry["snapshots"][snapshot].append(relpath)
        return {"version": 2, "pool_refs": filtered}

    def export_content_index_json(self, out_path: str) -> dict:
        t0 = time.time()
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        tmp = out_path + ".tmp"
        snap_names = {
            int(r[0]): r[1]
            for r in self.conn.execute("SELECT id, name FROM snapshots")
        }
        cur_s = self.conn.execute(
            "SELECT id, sha, fileid, hash, size FROM shas ORDER BY id"
        )
        cur_r = self.conn.execute(
            "SELECT sha_id, snap_id, relpath FROM snap_refs ORDER BY sha_id, snap_id, relpath"
        )
        ref_iter = iter(cur_r)
        pending = next(ref_iter, None)
        n_shas = 0
        n_pairs = 0
        first = True

        with open(tmp, "w", encoding="utf-8", buffering=1024 * 1024) as f:
            f.write('{"version":2,"pool_refs":{')
            for sha_id, sha, fileid, phash, size in cur_s:
                n_shas += 1
                grouped: Dict[str, List[str]] = {}
                while pending is not None and int(pending[0]) == int(sha_id):
                    n_pairs += 1
                    _sid, snap_id, relpath = int(pending[0]), int(pending[1]), pending[2]
                    name = snap_names.get(snap_id)
                    if name is not None:
                        grouped.setdefault(name, [])
                        if relpath:
                            grouped[name].append(relpath)
                    pending = next(ref_iter, None)
                entry = {
                    "fileid": fileid,
                    "hash": _export_hash(phash),
                    "size": size,
                    "snapshots": {k: v for k, v in grouped.items()},
                }
                chunk = json.dumps(sha) + ":" + json.dumps(entry, separators=(",", ":"))
                if first:
                    f.write(chunk)
                    first = False
                else:
                    f.write(",")
                    f.write(chunk)
            f.write("}}")
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, out_path)
        try:
            os.chmod(out_path, 0o644)
        except OSError:
            pass
        nbytes = os.path.getsize(out_path)
        self.set_meta("last_export_at", str(time.time()))
        return {
            "shas": n_shas,
            "pairs": n_pairs,
            "bytes": nbytes,
            "seconds": time.time() - t0,
        }

    def record_master_fingerprint(self, master_path: str) -> None:
        try:
            st = os.stat(master_path)
        except OSError:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_size', ?)",
            (str(st.st_size),),
        )
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_mtime_ns', ?)",
            (str(mtime_ns),),
        )
        self.conn.commit()

    def record_master_file_hash(self, master_path: str) -> Optional[str]:
        if not os.path.isfile(master_path):
            return None
        digest = _hash_file_sha256(master_path)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_sha256', ?)",
            (digest,),
        )
        self.conn.commit()
        return digest

    def refresh_master_metadata(self, master_path: str) -> None:
        """mtime/size + Datei-SHA256 nach Import oder Skip-Re-Import."""
        self.record_master_fingerprint(master_path)
        self.record_master_file_hash(master_path)

    def master_file_sha256_matches(self, master_path: str) -> Optional[bool]:
        stored = self.get_meta("master_sha256")
        if not stored or not os.path.isfile(master_path):
            return None
        return _hash_file_sha256(master_path) == stored

    def can_skip_master_reimport(self, master_path: str) -> bool:
        """
        Re-Import vermeiden wenn Master-Datei byte-identisch (SHA256) und DB konsistent.
        mtime/size allein reichen nicht — GC schreibt Master oft neu ohne Inhalt zu ändern.
        """
        if self.count_shas() == 0:
            return False
        if not os.path.isfile(master_path):
            return False
        if self.master_fingerprint_matches(master_path) is True:
            return True
        if self.master_file_sha256_matches(master_path) is not True:
            return False
        stored_digest = self.get_meta("master_content_digest")
        if stored_digest and self.digest()["sha256"] != stored_digest:
            return False
        return True

    def master_fingerprint_matches(self, master_path: str) -> Optional[bool]:
        if not os.path.isfile(master_path):
            return None
        try:
            st = os.stat(master_path)
        except OSError:
            return None
        size_s = self.get_meta("master_size")
        mtime_s = self.get_meta("master_mtime_ns")
        if size_s is None or mtime_s is None:
            return False
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return str(st.st_size) == size_s and str(mtime_ns) == mtime_s

    def digest(self) -> dict:
        h = hashlib.sha256()
        n_pairs = 0
        for row in self.conn.execute(
            "SELECT sha, fileid, hash, size FROM shas ORDER BY sha"
        ):
            sha, fileid, phash, size = row[0], row[1], row[2], row[3]
            h.update(
                f"{sha}|{_digest_coord(fileid)}|{_digest_coord(phash)}|{_digest_coord(size)}\n".encode()
            )
        for row in self.conn.execute(
            """
            SELECT s.sha, n.name, r.relpath
            FROM snap_refs r
            JOIN shas s ON s.id = r.sha_id
            JOIN snapshots n ON n.id = r.snap_id
            ORDER BY s.sha, n.name, r.relpath
            """
        ):
            n_pairs += 1
            h.update(f"{row[0]}|{row[1]}|{row[2]}\n".encode())
        return {
            "n_shas": self.count_shas(),
            "n_pairs": n_pairs,
            "sha256": h.hexdigest(),
        }


def open_db(path: Optional[str] = None, *, create: bool = True) -> PoolIndexDB:
    db_path = path or default_db_path()
    if not create and not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    return PoolIndexDB(db_path)


def _cli_log(msg: str) -> None:
    print(msg, file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="C1 pool index SQLite (local hybrid).")
    ap.add_argument("--db", default=None, help="SQLite path (default: PCLOUD_POOL_INDEX_DB_PATH)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="Replace DB from content_index JSON")
    p_imp.add_argument("--json", required=True)

    p_exp = sub.add_parser("export", help="Stream v2 content_index.json")
    p_exp.add_argument("--out", required=True)

    p_ver = sub.add_parser("verify", help="digest(JSON) == digest(DB)")
    p_ver.add_argument("--json", required=True)

    sub.add_parser("stats", help="Print counts")
    p_status = sub.add_parser("status", help="Counts + master meta fingerprints")
    p_status.add_argument(
        "--master",
        default=None,
        help="Master JSON path (default: PCLOUD_ARCHIVE/indexes/content_index_master.json)",
    )

    p_refresh = sub.add_parser(
        "refresh-meta",
        help="Store master_sha256 + master_content_digest (no re-import)",
    )
    p_refresh.add_argument(
        "--master",
        default=None,
        help="Master JSON path (default: content_index_master.json)",
    )

    p_purge = sub.add_parser("purge-snapshot", help="Delete snap_refs for one snapshot")
    p_purge.add_argument("snapshot")

    args = ap.parse_args(argv)
    db_path = args.db or default_db_path()
    master_default = default_master_path()

    if args.cmd == "import":
        with open_db(db_path) as db:
            st = db.import_from_json(args.json, log=_cli_log)
            print(json.dumps(st, indent=2))
        return 0

    with open_db(db_path, create=os.path.isfile(db_path) or args.cmd in ("import",)) as db:
        if args.cmd == "export":
            st = db.export_content_index_json(args.out)
            print(json.dumps(st, indent=2))
            return 0
        if args.cmd in ("stats", "status"):
            master_path = getattr(args, "master", None) or master_default
            meta_rows = {
                row[0]: row[1]
                for row in db.conn.execute(
                    "SELECT key, value FROM meta WHERE key LIKE 'master%'"
                )
            }
            fp_match = db.master_fingerprint_matches(master_path)
            sha_match = db.master_file_sha256_matches(master_path)
            skip = db.can_skip_master_reimport(master_path) if fp_match is False else True
            out = {
                "db": db.path,
                "master": master_path,
                "shas": db.count_shas(),
                "snapshots": db.snapshot_names(),
                "pairs": int(
                    db.conn.execute("SELECT COUNT(*) FROM snap_refs").fetchone()[0]
                ),
                "meta": meta_rows,
                "checks": {
                    "fingerprint_match": fp_match,
                    "file_sha256_match": sha_match,
                    "can_skip_reimport": skip,
                },
            }
            if args.cmd == "stats":
                del out["meta"]
                del out["checks"]
                del out["master"]
            print(json.dumps(out, indent=2))
            return 0
        if args.cmd == "refresh-meta":
            master_path = args.master or master_default
            if not os.path.isfile(master_path):
                _cli_log(f"Master nicht gefunden: {master_path}")
                return 1
            db.refresh_master_metadata(master_path)
            digest = db.digest()["sha256"]
            db.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('master_content_digest', ?)",
                (digest,),
            )
            db.conn.commit()
            print(
                json.dumps(
                    {
                        "master": master_path,
                        "master_sha256": db.get_meta("master_sha256"),
                        "master_content_digest": digest,
                        "shas": db.count_shas(),
                    },
                    indent=2,
                )
            )
            return 0
        if args.cmd == "verify":
            d_db = db.digest()
            d_js = digest_from_json(args.json)
            ok = d_db == d_js
            print(json.dumps({"ok": ok, "db": d_db, "json": d_js}, indent=2))
            return 0 if ok else 1
        if args.cmd == "purge-snapshot":
            n = db.purge_snapshot(args.snapshot)
            print(json.dumps({"purged": n, "snapshot": args.snapshot}, indent=2))
            return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
