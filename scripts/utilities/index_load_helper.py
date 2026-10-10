#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Index-Laden für Utilities.

- Lokale Master-Datei nur wenn klein genug (PCLOUD_MAX_LOCAL_INDEX_BYTES, Default 64 MiB).
  Große v2-Master (~2 GB): kein json.load — Remote oder SQLite-Tools nutzen.
- v1-Utilities (items/holders): nie pool_refs als items maskieren.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import pcloud_bin_lib as pc

V2_POOL_INDEX_MSG = (
    "v2 pool_refs content_index — nutze pool_integrity_run.py / "
    "pool_verify_backup.py (nicht pcloud_integrity_check / repair_index v1)"
)


def _max_local_index_bytes() -> int:
    return int(os.environ.get("PCLOUD_MAX_LOCAL_INDEX_BYTES", str(64 * 1024 * 1024)))


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


_V2_HEAD_PEEK_BYTES = 384
_V2_REMOTE_PEEK_BYTES = 512


def _bytes_look_like_v2_pool_index(head: bytes) -> bool:
    if not head:
        return False
    if head.startswith(b'{"version":2'):
        return True
    window = head[:_V2_HEAD_PEEK_BYTES]
    if b'"pool_refs"' in window and b'"items"' not in window:
        return True
    return False


def _peek_local_v2_pool_index(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if st.st_size <= _max_local_index_bytes():
        return False
    try:
        with open(path, "rb") as f:
            return _bytes_look_like_v2_pool_index(f.read(_V2_HEAD_PEEK_BYTES))
    except OSError:
        return False


def _peek_remote_v2_pool_index(cfg: dict, idx_path: str) -> bool:
    try:
        txt = pc.get_textfile(cfg, path=idx_path, maxbytes=_V2_REMOTE_PEEK_BYTES)
    except Exception:
        return False
    return _bytes_look_like_v2_pool_index(txt.encode("utf-8", errors="replace"))


def is_v2_pool_index(j: dict) -> bool:
    if not isinstance(j, dict):
        return False
    if int(j.get("version") or 0) == 2:
        return True
    pool_refs = j.get("pool_refs")
    items = j.get("items")
    if isinstance(pool_refs, dict) and pool_refs:
        if not items or not isinstance(items, dict) or not items:
            return True
    return False


def _try_read_local_json(path: str) -> Optional[dict]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size > _max_local_index_bytes():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_content_index_v2(
    cfg: dict,
    snapshots_root: str,
    *,
    prefer_local: bool = True,
) -> Dict[str, Any]:
    """
    v2 pool_refs. Lokale Datei nur unter Größenlimit; sonst Remote (json.load — OOM bei ~2 GB).
    """
    if prefer_local:
        for path in _local_master_candidates():
            j = _try_read_local_json(path)
            if j is None:
                continue
            j.setdefault("pool_refs", {})
            j.setdefault("version", 2)
            return j
    idx_path = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    if _peek_remote_v2_pool_index(cfg, idx_path):
        raise RuntimeError(V2_POOL_INDEX_MSG)
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
    """
    v1 items/holders (pre-pool). v2-Master wird nicht auf items gemappt.
    """
    if prefer_local:
        for path in _local_master_candidates():
            if _peek_local_v2_pool_index(path):
                raise RuntimeError(V2_POOL_INDEX_MSG)
            j = _try_read_local_json(path)
            if j is None:
                continue
            if is_v2_pool_index(j):
                continue
            items = j.get("items")
            if not isinstance(items, dict):
                continue
            j.setdefault("version", 1)
            return j
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    if _peek_remote_v2_pool_index(cfg, idx_path):
        raise RuntimeError(V2_POOL_INDEX_MSG)
    txt = pc.get_textfile(cfg, path=idx_path)
    j = json.loads(txt or '{"version":1,"items":{}}')
    if not isinstance(j, dict):
        j = {"version": 1, "items": {}}
    if is_v2_pool_index(j):
        raise RuntimeError(V2_POOL_INDEX_MSG)
    if "items" not in j or not isinstance(j.get("items"), dict):
        j["items"] = {}
    return j
