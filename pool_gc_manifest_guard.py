#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GC-Sicherheitsnetz: lokale Manifeste der live Remote-Snapshots.

Zweite Quelle neben Ops-Index — Pool-SHAs aus Manifesten werden nicht gelöscht,
wenn der Index Snapshots vergessen hat (z. B. fehlende snap_refs nach Pipeline-Fehler).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional, Set

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


def load_manifest_shas_for_snapshots(
    manifests_dir: str,
    snapshot_names: Set[str],
    *,
    log: Optional[LogFn] = None,
) -> Set[str]:
    """SHA256-Union aus lokalen Manifest-JSONs (ein Snapshot pro Datei, kein Master)."""
    protected: Set[str] = set()
    missing_files = 0
    for snap in sorted(snapshot_names):
        path = os.path.join(manifests_dir, f"{snap}.json")
        if not os.path.isfile(path):
            missing_files += 1
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            missing_files += 1
            continue
        for it in data.get("items") or []:
            if not isinstance(it, dict) or it.get("type") != "file":
                continue
            sha = it.get("sha256")
            if sha:
                protected.add(str(sha).lower())
    if log:
        log(
            f"[gc-guard] Manifest-SHAs: {len(protected)} aus "
            f"{len(snapshot_names) - missing_files}/{len(snapshot_names)} Dateien"
        )
        if missing_files:
            log(f"[gc-guard][warn] {missing_files} live Snapshot(s) ohne Manifest-Datei")
    return protected


@dataclass
class GcManifestGuard:
    remote_snaps: Set[str]
    missing_in_ops_db: list[str] = field(default_factory=list)
    missing_manifest: list[str] = field(default_factory=list)
    protected_shas: Set[str] = field(default_factory=set)
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
    Preflight + geschützte SHA-Menge.
    Abbruch nur wenn ein live Snapshot weder Ops-DB-Eintrag noch Manifest hat.
    """
    guard = GcManifestGuard(remote_snaps=set(remote_snaps))
    if os.environ.get("PCLOUD_GC_MANIFEST_GUARD", "1") == "0":
        log("[gc-guard] deaktiviert (PCLOUD_GC_MANIFEST_GUARD=0)")
        return guard

    if not remote_snaps:
        guard.abort_reason = "no_remote_snapshots"
        return guard

    manifests_dir = _manifests_dir(env_vars)
    db_names: Set[str] = set()
    if os.path.isfile(ops_db_path):
        try:
            db_names = snapshot_names_in_ops_db(ops_db_path)
        except Exception as e:
            log(f"[gc-guard][ERROR] Ops-DB lesen: {e}")
            guard.abort_reason = f"ops_db_read_failed: {e}"
            return guard
    else:
        log(f"[gc-guard][warn] Ops-DB fehlt: {ops_db_path}")

    guard.missing_in_ops_db = sorted(remote_snaps - db_names)
    if guard.missing_in_ops_db:
        log(
            f"[gc-guard][CRITICAL] {len(guard.missing_in_ops_db)} live Snapshot(s) "
            f"fehlen in Ops-DB snapshots-Tabelle: "
            + ", ".join(guard.missing_in_ops_db[:8])
            + (" …" if len(guard.missing_in_ops_db) > 8 else "")
        )

    for snap in guard.missing_in_ops_db:
        mpath = os.path.join(manifests_dir, f"{snap}.json")
        if not os.path.isfile(mpath):
            guard.missing_manifest.append(snap)

    if guard.missing_manifest:
        guard.abort_reason = (
            "live_snapshots_without_index_and_manifest: "
            + ",".join(guard.missing_manifest)
        )
        log(f"[gc-guard][ERROR] Abbruch — kein Index und kein Manifest: {guard.missing_manifest}")
        return guard

    guard.protected_shas = load_manifest_shas_for_snapshots(
        manifests_dir, remote_snaps, log=log,
    )
    if guard.missing_in_ops_db:
        log(
            f"[gc-guard] Manifest-Schutz aktiv für {len(guard.protected_shas)} SHAs "
            f"(Index-Lücken: {len(guard.missing_in_ops_db)} Snapshot(s))"
        )
    return guard


class ManifestProtectedShaLookup:
    """Membership: Ops-Index ODER Manifest-Union (kein zweites Millionen-Set im RAM nötig)."""

    def __init__(self, index_lookup, manifest_shas: Set[str]):
        self._index = index_lookup
        self._manifest = manifest_shas

    def __contains__(self, sha: object) -> bool:
        if sha in self._index:
            return True
        return str(sha).lower() in self._manifest

    def __len__(self) -> int:
        return len(self._index)
