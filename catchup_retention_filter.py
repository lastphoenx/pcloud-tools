#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Catch-up-Filter: dieselbe Zeit-Retention wie pool_gc, mit Sicherheitsgurt.

- Nur Snapshots überspringen, die älter als DAYS_FULL sind und in to_delete stehen.
- Neuester lokaler Snapshot (latest) wird nie übersprungen.
- rtb-spiegel / unset: kein Retention-Filter (nur PCLOUD_CATCHUP_SKIP_SNAPSHOTS).
"""
from __future__ import annotations

import datetime
import os
import re
from typing import Dict, List, Optional, Set

import pool_retention_safety as prs
from pcloud_pool_gc import _parse_snapshot_dt, compute_time_retention_keep

_SNAP_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}-\d{6}$")
DEFAULT_MAX_SKIP_WARN = 5


def parse_env_skip_snapshots(env_vars: Optional[Dict[str, str]]) -> Set[str]:
    raw = (prs.env_get(env_vars, "PCLOUD_CATCHUP_SKIP_SNAPSHOTS", "") or "").strip()
    if not raw:
        return set()
    out: Set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        if name:
            out.add(name)
    return out


def resolve_latest_local_name(
    local_snaps: Set[str],
    rtb_root: Optional[str] = None,
) -> Optional[str]:
    if rtb_root:
        latest_path = os.path.join(rtb_root, "latest")
        try:
            if os.path.islink(latest_path) or os.path.isdir(latest_path):
                real = os.path.realpath(latest_path)
                name = os.path.basename(real)
                if name in local_snaps:
                    return name
        except OSError:
            pass
    best_name: Optional[str] = None
    best_dt: Optional[datetime.datetime] = None
    for name in local_snaps:
        dt = _parse_snapshot_dt(name)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_name = name
    return best_name


def _time_retention_active(env_vars: Optional[Dict[str, str]]) -> bool:
    mode, _err = prs.resolve_retention_mode(env_vars, allow_rtb_mode=False)
    return mode == prs.MODE_TIME and prs.time_retention_enabled(env_vars)


def filter_catchup_missing(
    missing: List[str],
    remote_complete: Set[str],
    local_snaps: Set[str],
    env_vars: Optional[Dict[str, str]] = None,
    *,
    latest_name: Optional[str] = None,
    rtb_root: Optional[str] = None,
    max_skip_warn: int = DEFAULT_MAX_SKIP_WARN,
) -> Dict[str, object]:
    """
    Returns dict with keys: ok, catchup, skipped_retention, skipped_env, warnings, latest.
    On error (ok=False): catchup = original missing unchanged.
    """
    warnings: List[str] = []
    ordered_missing = list(missing)

    if latest_name is None:
        latest_name = resolve_latest_local_name(local_snaps, rtb_root)

    try:
        env_skips = parse_env_skip_snapshots(env_vars)
        to_delete: Set[str] = set()
        mode_filter = _time_retention_active(env_vars)
        days_full = 0
        cutoff: Optional[datetime.datetime] = None
        if mode_filter:
            days_full, _weekly = prs.time_retention_params(env_vars)
            cutoff = datetime.datetime.now() - datetime.timedelta(days=days_full)
            union = set(remote_complete) | set(local_snaps)
            _, to_delete = compute_time_retention_keep(union, env_vars)

        catchup: List[str] = []
        skipped_retention: List[str] = []
        skipped_env: List[str] = []

        for snap in ordered_missing:
            if snap in env_skips:
                skipped_env.append(snap)
                continue
            if mode_filter and snap in to_delete:
                if snap == latest_name:
                    catchup.append(snap)
                    continue
                dt = _parse_snapshot_dt(snap)
                if dt is None or cutoff is None:
                    catchup.append(snap)
                    continue
                if dt >= cutoff:
                    catchup.append(snap)
                    continue
                skipped_retention.append(snap)
                continue
            catchup.append(snap)

        if len(skipped_retention) > max_skip_warn:
            warnings.append(
                f"catchup_skip_retention_unusually_large:{len(skipped_retention)}"
            )

        return {
            "ok": True,
            "catchup": catchup,
            "skipped_retention": sorted(skipped_retention),
            "skipped_env": sorted(skipped_env),
            "warnings": warnings,
            "latest": latest_name,
            "days_full": days_full,
        }
    except Exception as exc:
        warnings.append(f"catchup_filter_error:{exc}")
        return {
            "ok": False,
            "catchup": ordered_missing,
            "skipped_retention": [],
            "skipped_env": [],
            "warnings": warnings,
            "latest": latest_name,
        }
