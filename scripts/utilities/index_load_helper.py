#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lokaler Master-Index vor Remote-Download (Utilities, RAM/Netz sparen)."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import pcloud_bin_lib as pc


def _local_master_candidates() -> list[str]:
    archive = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    paths: list[str] = []
    override = (os.environ.get("PCLOUD_POOL_INDEX_MASTER") or "").strip()
    if override:
        paths.append(override)
    paths.append(os.path.join(archive, "indexes", "content_index_master.json"))
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def load_content_index_v2(
    cfg: dict,
    snapshots_root: str,
    *,
    prefer_local: bool = True,
) -> Dict[str, Any]:
    """
    pool_refs-Index (v2). Zuerst lokaler Master auf pi-nas, sonst Remote.
    """
    if prefer_local:
        for path in _local_master_candidates():
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                if isinstance(j, dict):
                    j.setdefault("pool_refs", {})
                    j.setdefault("version", 2)
                    return j
            except (OSError, json.JSONDecodeError):
                continue
    idx_path = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    txt = pc.get_textfile(cfg, path=idx_path)
    j = json.loads(txt or "{}")
    if not isinstance(j, dict):
        return {"version": 2, "pool_refs": {}}
    j.setdefault("pool_refs", {})
    return j


def load_content_index_legacy_items(
    cfg: dict,
    snaps_root: str,
    *,
    prefer_local: bool = True,
) -> Dict[str, Any]:
    """v1 items-Index fuer pcloud_integrity_check."""
    if prefer_local:
        for path in _local_master_candidates():
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                if isinstance(j, dict):
                    if "items" not in j or not isinstance(j.get("items"), dict):
                        j["items"] = j.get("pool_refs") or {}
                    j.setdefault("version", 1)
                    return j
            except (OSError, json.JSONDecodeError):
                continue
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    txt = pc.get_textfile(cfg, path=idx_path)
    j = json.loads(txt or '{"version":1,"items":{}}')
    if "items" not in j or not isinstance(j.get("items"), dict):
        j["items"] = {}
    return j
