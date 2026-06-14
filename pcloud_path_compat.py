#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pfad-Kompatibilität: lokales SMB/Linux vs. pCloud-API.

pCloud normalisiert Ordner-/Dateinamen teils anders als das lokale FS
(z. B. trailing space in Pfadsegmenten: „Sommer “ vs. „Sommer“).
Validation vergleicht sonst Manifest-Pfade strikt mit listfolder-Pfaden → False Positives.
"""

from __future__ import annotations

import fnmatch
import os
from typing import Iterable, List, Set


def normalize_path_segments(path: str) -> str:
    """
    Pro Pfadsegment führende/trailing Whitespace entfernen.
    „/a/Sommer /file.meta.json“ → „/a/Sommer/file.meta.json“
    """
    if not path:
        return path
    parts = path.split("/")
    norm = []
    for p in parts:
        if p == "":
            norm.append("")
            continue
        norm.append(p.strip())
    out = "/".join(norm)
    while "//" in out:
        out = out.replace("//", "/")
    if len(out) > 1 and out.endswith("/"):
        out = out[:-1]
    return out


def relpath_has_risky_segments(relpath: str) -> bool:
    """True wenn ein Segment führendes/trailing Whitespace hat (pCloud-Risiko)."""
    if not relpath:
        return False
    for seg in relpath.split("/"):
        if seg and seg != seg.strip():
            return True
    return False


def risky_segment_examples(relpath: str, limit: int = 3) -> List[str]:
    out: List[str] = []
    for seg in relpath.split("/"):
        if seg and seg != seg.strip():
            out.append(repr(seg))
            if len(out) >= limit:
                break
    return out


def build_stub_path_lookup(remote_stub_paths: Iterable[str]) -> Set[str]:
    """Lookup-Set: Original-Pfade + segment-normalisierte Varianten."""
    lookup: Set[str] = set()
    for p in remote_stub_paths:
        lookup.add(p)
        lookup.add(normalize_path_segments(p))
    return lookup


def stub_path_exists(expected_stub_path: str, lookup: Set[str]) -> bool:
    if expected_stub_path in lookup:
        return True
    return normalize_path_segments(expected_stub_path) in lookup


def parse_manifest_skip_globs() -> List[str]:
    """
    Glob-Patterns für Manifest-Scan (fnmatch auf relpath).
    Default: AppleDouble (**/._*), analog rtb/excludes.txt.
  """
    raw = os.environ.get("PCLOUD_MANIFEST_SKIP_GLOBS", "**/._*").strip()
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def relpath_excluded(relpath: str, skip_globs: List[str]) -> bool:
    if not skip_globs or not relpath:
        return False
    name = relpath.rsplit("/", 1)[-1]
    for pat in skip_globs:
        if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def manifest_warn_risky_paths_enabled() -> bool:
    return os.environ.get("PCLOUD_MANIFEST_WARN_RISKY_PATHS", "1") != "0"
