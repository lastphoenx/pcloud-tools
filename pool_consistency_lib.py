#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Konsistenzregeln: Live-Snapshots vs. Indexe/Manifeste (ohne pCloud-API)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

LEVEL_OK = 0
LEVEL_WARN = 1
LEVEL_CRITICAL = 2


def evaluate_pool_consistency(
    *,
    live: Set[str],
    ops_db_names: Set[str],
    backup_db_names: Set[str],
    manifest_names: Set[str],
    last_live_count: Optional[int] = None,
    live_drop_warn_ratio: float = 0.10,
    upload_pending: bool = False,
    reapply_purge: str = "",
    pool_gaps: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Exit-Level: 0 OK, 1 Warnung, 2 kritisch.
    Kritisch: live fehlt in Ops- oder Backup-DB; Live-Anzahl stark gesunken; Pool-Gaps.
    """
    live_missing_ops = sorted(live - ops_db_names)
    live_missing_backup = sorted(live - backup_db_names)
    live_missing_manifest = sorted(live - manifest_names)
    stale_ops = sorted(ops_db_names - live)
    stale_manifests = sorted(manifest_names - live)

    critical: List[str] = []
    warn: List[str] = []

    if live_missing_ops:
        critical.append(
            f"live_not_in_ops_db:{len(live_missing_ops)}"
        )
    if live_missing_backup:
        critical.append(
            f"live_not_in_backup_db:{len(live_missing_backup)}"
        )
    if live_missing_manifest:
        critical.append(
            f"live_without_local_manifest:{len(live_missing_manifest)}"
        )

    if last_live_count is not None and last_live_count > 0 and live:
        drop = (last_live_count - len(live)) / last_live_count
        if drop > live_drop_warn_ratio:
            critical.append(
                f"live_count_drop:{last_live_count}->{len(live)}"
            )

    if pool_gaps is not None and pool_gaps > 0:
        critical.append(f"pool_files_missing:{pool_gaps}")

    if upload_pending:
        warn.append("ops_upload_pending")
    if (reapply_purge or "").strip():
        warn.append("ops_reapply_purge_after_sync")

    if stale_ops:
        warn.append(f"stale_ops_snapshots:{len(stale_ops)}")
    if stale_manifests:
        warn.append(f"stale_local_manifests:{len(stale_manifests)}")

    if critical:
        level = LEVEL_CRITICAL
    elif warn:
        level = LEVEL_WARN
    else:
        level = LEVEL_OK

    return {
        "level": level,
        "level_name": ("ok", "warn", "critical")[level],
        "live_count": len(live),
        "ops_db_count": len(ops_db_names),
        "backup_db_count": len(backup_db_names),
        "manifest_count": len(manifest_names),
        "live_missing_ops_db": live_missing_ops,
        "live_missing_backup_db": live_missing_backup,
        "live_missing_manifest": live_missing_manifest,
        "stale_ops_snapshots": stale_ops[:20],
        "stale_ops_count": len(stale_ops),
        "stale_manifest_count": len(stale_manifests),
        "critical_reasons": critical,
        "warn_reasons": warn,
        "pool_gaps": pool_gaps,
    }
