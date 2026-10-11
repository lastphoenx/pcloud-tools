#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsamer .env-Parser (Inline-Kommentare wie Bash)."""
from __future__ import annotations

import os
from typing import Dict


def strip_env_value(raw: str) -> str:
    """Entfernt Inline-#-Kommentare (nur außerhalb von Anführungszeichen)."""
    value = raw.strip()
    if not value:
        return value
    in_single = False
    in_double = False
    for i, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or value[i - 1].isspace():
                value = value[:i].rstrip()
                break
    return value.strip().strip("'\"")


def load_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            if not key:
                continue
            out[key] = strip_env_value(v)
    return out
