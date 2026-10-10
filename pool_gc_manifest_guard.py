#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GC-Sicherheitsnetz: lokale Manifeste nur bei Ops-Index-Lücken.

Zweite Quelle neben Ops-Index — Pool-SHAs aus Manifesten werden nicht gelöscht,
wenn der Index Snapshots vergessen hat (fehlende Zeile oder 0 snap_refs).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set, Tuple

LogFn = Callable[[str], None]


def _manifests_dir(env_vars: Optional[dict]) -> str:
    if env_vars and env_vars.get("PCLOUD_ARCHIVE_DIR"):
        base = env_vars["PCLOUD_ARCHIVE_DIR"]
    else:
        base = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    return os.path.join(base, "manifests")


def snapshot_names_in_ops_db(ops_db_path: str) -> Set[str]:
    uri = f"file:{os.path.abspath(ops_db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("SELECT name FROM snapshots").fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    finally:
        conn.close()


def classify_live_ops_coverage(
    ops_db_path: str,
    live: Set[str],
) -> Tuple[List[str], List[str]]:
    """
    Returns (missing_row, zero_refs) für live Snapshots.
    missing_row: kein Eintrag in snapshots; zero_refs: Zeile, aber keine snap_refs.
    """
    if not live:
        return [], []
    if not os.path.isfile(ops_db_path):
        return sorted(live), []

    uri = f"file:{os.path.abspath(ops_db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        names = sorted(live)
        placeholders = ",".join("?" * len(names))
        rows = conn.execute(
            f"""
            SELECT n.name, COUNT(r.sha_id) AS ref_cnt
            FROM snapshots n
            LEFT JOIN snap_refs r ON r.snap_id = n.id
            WHERE n.name IN ({placeholders})
            GROUP BY n.id
            """,
            names,
        ).fetchall()
        present = {str(r[0]): int(r[1]) for r in rows}
        missing_row = sorted(s for s in live if s not in present)
        zero_refs = sorted(s for s in live if present.get(s, -1) == 0)
        return missing_row, zero_refs
    finally:
        conn.close()


class ManifestProtectedShaStore:
    """Geschützte SHAs in :memory:-SQLite (kein riesiges Python-Set)."""

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE prot (sha TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        self._count = 0

    def add_sha(self, sha: str) -> None:
        h = str(sha).lower()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO prot(sha) VALUES (?)", (h,),
        )
        if cur.rowcount:
            self._count += 1

    def __contains__(self, sha: object) -> bool:
        h = str(sha).lower()
        row = self._conn.execute(
            "SELECT 1 FROM prot WHERE sha=? LIMIT 1", (h,),
        ).fetchone()
        return row is not None

    def __len__(self) -> int:
        return self._count

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def _stream_manifest_shas(path: str, store: ManifestProtectedShaStore) -> None:
    try:
        import ijson  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "ijson fehlt — pip install ijson (Manifest-Guard Streaming)"
        ) from e
    with open(path, "rb") as f:
        for it in ijson.items(f, "items.item"):
            if not isinstance(it, dict) or it.get("type") != "file":
                continue
            sha = it.get("sha256")
            if sha:
                store.add_sha(str(sha))


def load_gap_manifest_protection(
    manifests_dir: str,
    gap_snapshots: Set[str],
    *,
    log: Optional[LogFn] = None,
) -> ManifestProtectedShaStore:
    """Nur Lücken-Snapshots: ijson-Stream → Temp-Tabelle."""
    store = ManifestProtectedShaStore()
    for snap in sorted(gap_snapshots):
        path = os.path.join(manifests_dir, f"{snap}.json")
        _stream_manifest_shas(path, store)
    if log:
        log(
            f"[gc-guard] Manifest-Schutz: {len(store)} SHAs aus "
            f"{len(gap_snapshots)} Lücken-Snapshot(s)"
        )
    return store


@dataclass
class GcManifestGuard:
    remote_snaps: Set[str]
    missing_in_ops_db: list[str] = field(default_factory=list)
    zero_refs_in_ops_db: list[str] = field(default_factory=list)
    manifest_gap_snapshots: list[str] = field(default_factory=list)
    missing_manifest: list[str] = field(default_factory=list)
    corrupt_manifest: list[str] = field(default_factory=list)
    protected: ManifestProtectedShaStore = field(
        default_factory=ManifestProtectedShaStore,
    )
    abort_reason: Optional[str] = None

    def enabled(self) -> bool:
        return self.abort_reason is None


def build_gc_manifest_guard(
    remote_snaps: Set[str],
    ops_db_path: str,
    env_vars: Optional[dict],
    *,
    log: LogFn,
) -> GcManifestGuard:
    """
    Preflight + geschützte SHA-Menge nur bei Index-Lücken.
    Abbruch wenn Lücken-Snapshot ohne lesbares Manifest.
    """
    guard = GcManifestGuard(remote_snaps=set(remote_snaps))
    if os.environ.get("PCLOUD_GC_MANIFEST_GUARD", "1") == "0":
        log("[gc-guard] deaktiviert (PCLOUD_GC_MANIFEST_GUARD=0)")
        return guard

    if not remote_snaps:
        guard.abort_reason = "no_remote_snapshots"
        return guard

    manifests_dir = _manifests_dir(env_vars)
    try:
        guard.missing_in_ops_db, guard.zero_refs_in_ops_db = classify_live_ops_coverage(
            ops_db_path, remote_snaps,
        )
    except Exception as e:
        log(f"[gc-guard][ERROR] Ops-DB lesen: {e}")
        guard.abort_reason = f"ops_db_read_failed: {e}"
        return guard

    gap = set(guard.missing_in_ops_db) | set(guard.zero_refs_in_ops_db)
    guard.manifest_gap_snapshots = sorted(gap)

    if guard.missing_in_ops_db:
        log(
            f"[gc-guard][CRITICAL] {len(guard.missing_in_ops_db)} live Snapshot(s) "
            f"fehlen in Ops-DB snapshots-Tabelle: "
            + ", ".join(guard.missing_in_ops_db[:8])
            + (" …" if len(guard.missing_in_ops_db) > 8 else "")
        )
    if guard.zero_refs_in_ops_db:
        log(
            f"[gc-guard][CRITICAL] {len(guard.zero_refs_in_ops_db)} live Snapshot(s) "
            f"ohne snap_refs in Ops-DB: "
            + ", ".join(guard.zero_refs_in_ops_db[:8])
            + (" …" if len(guard.zero_refs_in_ops_db) > 8 else "")
        )

    if not gap:
        log("[gc-guard] Ops-Index deckt alle live Snapshots ab — kein Manifest-Lesen")
        return guard

    guard.protected.close()
    guard.protected = ManifestProtectedShaStore()
    for snap in guard.manifest_gap_snapshots:
        mpath = os.path.join(manifests_dir, f"{snap}.json")
        if not os.path.isfile(mpath):
            guard.missing_manifest.append(snap)
            continue
        try:
            _stream_manifest_shas(mpath, guard.protected)
        except Exception as e:
            guard.corrupt_manifest.append(snap)
            log(f"[gc-guard][ERROR] Manifest unlesbar {snap}: {e}")

    if guard.missing_manifest:
        guard.protected.close()
        guard.abort_reason = (
            "live_snapshots_without_index_and_manifest: "
            + ",".join(guard.missing_manifest)
        )
        log(
            f"[gc-guard][ERROR] Abbruch — kein Index und kein Manifest: "
            f"{guard.missing_manifest}"
        )
        return guard

    if guard.corrupt_manifest:
        guard.protected.close()
        guard.abort_reason = (
            "live_gap_snapshots_corrupt_manifest: "
            + ",".join(guard.corrupt_manifest)
        )
        log(f"[gc-guard][ERROR] Abbruch — defektes Manifest: {guard.corrupt_manifest}")
        return guard

    log(
        f"[gc-guard] Manifest-Schutz: {len(guard.protected)} SHAs aus "
        f"{len(guard.manifest_gap_snapshots)} Lücken-Snapshot(s)"
    )
    return guard


class ManifestProtectedShaLookup:
    """Membership: Ops-Index ODER Manifest-Temp-DB."""

    def __init__(self, index_lookup, manifest_protected: ManifestProtectedShaStore):
        self._index = index_lookup
        self._manifest = manifest_protected

    def __contains__(self, sha: object) -> bool:
        if sha in self._index:
            return True
        return sha in self._manifest

    def __len__(self) -> int:
        return len(self._index)
