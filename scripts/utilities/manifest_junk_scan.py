#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan archivierte Manifeste auf gmk-evo / Full-System Altlasten (ohne du auf RTB).

Erkennt Pfade, die evo-backup.sh heute NICHT mehr liefert (usr/var/boot/home, ollama-Modelle, rocm).
Config-only Snapshots haben typisch <1 MB unter Backup/gmk-evo/.

Beispiel (pi-nas):
  python3 scripts/utilities/manifest_junk_scan.py --env-file .env
  python3 scripts/utilities/manifest_junk_scan.py --env-file .env --min-mb 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)

# RTB excludes + bekannte Full-Backup-Muster (relpath, case-insensitive)
_JUNK_RULES: List[Tuple[str, re.Pattern[str]]] = [
    ("gmk-evo/usr", re.compile(r"(^|/)backup/gmk-evo/usr/", re.I)),
    ("gmk-evo/var", re.compile(r"(^|/)backup/gmk-evo/var/", re.I)),
    ("gmk-evo/boot", re.compile(r"(^|/)backup/gmk-evo/boot/", re.I)),
    ("gmk-evo/home", re.compile(r"(^|/)backup/gmk-evo/home/", re.I)),
    ("gmk-evo/opt/ollama", re.compile(r"(^|/)backup/gmk-evo/opt/ollama", re.I)),
    ("gmk-evo/ollama-lib", re.compile(r"(^|/)backup/gmk-evo/usr/local/lib/ollama", re.I)),
    ("gmk-evo/rocm", re.compile(r"(^|/)backup/gmk-evo/.*rocm", re.I)),
    ("ollama/models", re.compile(r"ollama/models", re.I)),
]


def _load_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _norm_rel(relpath: str) -> str:
    return (relpath or "").replace("\\", "/").lstrip("/")


def _classify_junk(relpath: str) -> str | None:
    rel = _norm_rel(relpath)
    if not rel:
        return None
    for label, pat in _JUNK_RULES:
        if pat.search(rel):
            return label
    return None


def _scan_manifest(path: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "files": 0,
        "bytes": 0,
        "gmk_total_files": 0,
        "gmk_total_bytes": 0,
        "junk_by_rule": {},
        "samples": [],
    }
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        out["error"] = str(e)
        return out

    for it in data.get("items", []) or []:
        if it.get("type") != "file":
            continue
        rel = _norm_rel(it.get("relpath") or "")
        size = int(it.get("size") or 0)
        if re.search(r"(^|/)backup/gmk-evo/", rel, re.I):
            out["gmk_total_files"] += 1
            out["gmk_total_bytes"] += size
        rule = _classify_junk(rel)
        if not rule:
            continue
        out["files"] += 1
        out["bytes"] += size
        bucket = out["junk_by_rule"].setdefault(rule, {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        if len(out["samples"]) < 5:
            out["samples"].append(rel)

    return out


def _fmt_mb(n: int) -> str:
    return f"{n / (1024 ** 2):.2f}"


def _fmt_gb(n: int) -> str:
    return f"{n / (1024 ** 3):.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Manifest-Scan: gmk-evo Full-System Altlasten")
    ap.add_argument("--env-file", default=f"{MAIN_DIR}/.env")
    ap.add_argument("--manifests-dir", help="default: PCLOUD_ARCHIVE_DIR/manifests")
    ap.add_argument(
        "--min-mb",
        type=float,
        default=0.5,
        help="Nur Snapshots mit Junk >= MB anzeigen (default 0.5)",
    )
    ap.add_argument("--all", action="store_true", help="Alle Manifeste, auch ohne Junk")
    args = ap.parse_args()

    env = _load_env(args.env_file)
    archive = env.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    manifests_dir = args.manifests_dir or os.path.join(archive, "manifests")
    if not os.path.isdir(manifests_dir):
        print(f"[FAIL] Kein Manifest-Ordner: {manifests_dir}", file=sys.stderr)
        return 1

    min_bytes = int(args.min_mb * 1024 * 1024)
    names = sorted(
        n[:-5] for n in os.listdir(manifests_dir)
        if n.endswith(".json") and n[:4].isdigit()
    )

    print("=== manifest_junk_scan ===")
    print(f"Manifeste: {manifests_dir}")
    print(f"Junk-Schwellwert: {args.min_mb} MB")
    print()
    print(
        f"{'SNAPSHOT':<22} {'JUNK_MB':>9} {'JUNK_FILES':>10} "
        f"{'GMK_MB':>9} {'GMK_FILES':>9}  RULES"
    )
    print("-" * 95)

    flagged = 0
    for snap in names:
        path = os.path.join(manifests_dir, f"{snap}.json")
        st = _scan_manifest(path)
        if st.get("error"):
            print(f"{snap:<22} ERROR: {st['error']}")
            continue
        junk_b = st["bytes"]
        gmk_b = st["gmk_total_bytes"]
        if junk_b < min_bytes and not args.all:
            continue
        flagged += 1
        rules = ",".join(sorted(st["junk_by_rule"].keys())) or "—"
        print(
            f"{snap:<22} {_fmt_mb(junk_b):>9} {st['files']:>10} "
            f"{_fmt_mb(gmk_b):>9} {st['gmk_total_files']:>9}  {rules}"
        )
        for s in st.get("samples", []):
            print(f"  sample: {s}")

    print("-" * 95)
    print(f"Manifeste gesamt: {len(names)}, angezeigt: {flagged}")
    print()
    print("Interpretation:")
    print("  JUNK_*     = usr/var/boot/home/ollama-models/rocm (Full-System Altlast)")
    print("  GMK_*      = alles unter Backup/gmk-evo/ (Config-only ≈ wenige KB–MB)")
    print("  Kein Manifest + RTB lokal → du nur auf Unterpfad (siehe Doku)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
