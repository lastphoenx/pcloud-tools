#!/usr/bin/env python3
"""
Offline Decision Simulator for Smart-Strategy 2.0.

Purpose:
- Compare current vs. basis manifest.
- Compute absolute delta metrics used by Smart-Strategy 2.0.
- Simulate the final decision without touching pCloud APIs.

Notes:
- This simulator expects stub_ratio as input because real stub_ratio comes
  from remote content_index analysis in production.
- Decision logic mirrors SmartStrategyController in
  pcloud_push_json_manifest_to_pcloud.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, Tuple


def _load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Manifest is not a JSON object: {path}")
    return data


def _files_by_relpath(manifest: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for item in (manifest.get("items") or []):
        if item.get("type") != "file":
            continue
        relpath = item.get("relpath")
        if relpath:
            out[relpath] = item
    return out


def _compute_delta_metrics(current: dict, basis: dict) -> dict:
    current_files = _files_by_relpath(current)
    basis_files = _files_by_relpath(basis)

    current_paths = set(current_files.keys())
    basis_paths = set(basis_files.keys())

    new_paths = current_paths - basis_paths
    deleted_paths = basis_paths - current_paths
    common_paths = current_paths & basis_paths

    identical_count = 0
    changed_count = 0
    changed_paths = set()

    for relpath in common_paths:
        cur_item = current_files[relpath]
        bas_item = basis_files[relpath]

        cur_sha = (cur_item.get("sha256") or "").lower()
        bas_sha = (bas_item.get("sha256") or "").lower()
        cur_mtime = cur_item.get("mtime")
        bas_mtime = bas_item.get("mtime")

        if cur_sha == bas_sha and cur_mtime == bas_mtime:
            identical_count += 1
        else:
            changed_count += 1
            changed_paths.add(relpath)

    new_count = len(new_paths)
    deleted_count = len(deleted_paths)

    upload_bytes = 0
    for relpath in (new_paths | changed_paths):
        size = current_files.get(relpath, {}).get("size")
        if isinstance(size, (int, float)):
            upload_bytes += int(size)

    total_files = len(current_files)
    basis_total_files = len(basis_files)
    match_ratio = (identical_count / total_files) if total_files else 0.0

    # Smart-Strategy 2.0 absolute work units
    saved_calls = identical_count
    cleanup_calls = deleted_count + changed_count
    upload_calls = new_count + changed_count

    return {
        "total_files": total_files,
        "basis_total_files": basis_total_files,
        "match_count": identical_count,
        "identical_count": identical_count,
        "new_count": new_count,
        "changed_count": changed_count,
        "deleted_count": deleted_count,
        "saved_calls": saved_calls,
        "cleanup_calls": cleanup_calls,
        "upload_calls": upload_calls,
        "upload_bytes": upload_bytes,
        "match_ratio": match_ratio,
    }


def _decide_strategy(metrics: dict, *, source_snapshots: int, stub_ratio: float,
                     template_exists: bool, template_match: float,
                     stub_transform_threshold: float,
                     saved_calls_min: int,
                     template_strong_threshold: float) -> Tuple[str, str]:
    # Hard gate 1: Initial upload stays SAFE.
    if source_snapshots <= 1:
        return ("SAFE-MODE", "initial_upload")

    # Hard gate 2: Quota-safe transformation run.
    if stub_ratio < stub_transform_threshold:
        return ("SAFE-MODE", "transformation_to_stubs")

    # Efficiency gate: only turbo if we save more than cleanup and enough absolute gain.
    if metrics["saved_calls"] > metrics["cleanup_calls"] and metrics["saved_calls"] >= saved_calls_min:
        return ("TURBO-MODE", "api_efficiency_gain")

    # Template fallback.
    if template_exists and template_match >= template_strong_threshold:
        return ("TEMPLATE-DELTA-SAFE", "template_fallback")

    return ("SAFE-MODE", "default_safe")


def _fmt_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    units = ["KiB", "MiB", "GiB", "TiB"]
    value = float(num)
    for unit in units:
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PiB"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Offline Decision Simulator for Smart-Strategy 2.0",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--current", required=True, help="Current manifest JSON path")
    p.add_argument("--basis", required=True, help="Basis/reference manifest JSON path")

    p.add_argument("--source-snapshots", type=int, default=2,
                   help="How many source snapshots are considered available")
    p.add_argument("--stub-ratio", type=float, required=True,
                   help="Stub ratio for basis snapshot (normally from remote index)")
    p.add_argument("--template-exists", action="store_true",
                   help="Set if folder template exists")
    p.add_argument("--template-match", type=float, default=0.0,
                   help="Template match ratio")

    p.add_argument("--stub-transform-threshold", type=float,
                   default=float(os.environ.get("PCLOUD_SMART_STUB_TRANSFORM_THRESHOLD", "0.80")),
                   help="If stub_ratio is below this, force SAFE transformation run")
    p.add_argument("--saved-calls-min", type=int,
                   default=int(os.environ.get("PCLOUD_SMART_SAVED_CALLS_MIN", "1000")),
                   help="Minimum saved_calls needed for TURBO")
    p.add_argument("--template-strong-threshold", type=float,
                   default=float(os.environ.get("PCLOUD_SMART_TEMPLATE_STRONG_THRESHOLD", "0.90")),
                   help="Template match threshold for TEMPLATE-DELTA-SAFE")

    p.add_argument("--json-out", help="Write full result JSON to file")
    p.add_argument("--json-only", action="store_true", help="Print only JSON to stdout")
    return p


def main() -> int:
    args = _build_parser().parse_args()

    current = _load_manifest(args.current)
    basis = _load_manifest(args.basis)
    delta = _compute_delta_metrics(current, basis)

    mode, reason = _decide_strategy(
        delta,
        source_snapshots=args.source_snapshots,
        stub_ratio=args.stub_ratio,
        template_exists=args.template_exists,
        template_match=args.template_match,
        stub_transform_threshold=args.stub_transform_threshold,
        saved_calls_min=args.saved_calls_min,
        template_strong_threshold=args.template_strong_threshold,
    )

    result = {
        "decision": {
            "mode": mode,
            "reason": reason,
        },
        "inputs": {
            "source_snapshots": args.source_snapshots,
            "stub_ratio": args.stub_ratio,
            "template_exists": args.template_exists,
            "template_match": args.template_match,
            "stub_transform_threshold": args.stub_transform_threshold,
            "saved_calls_min": args.saved_calls_min,
            "template_strong_threshold": args.template_strong_threshold,
        },
        "metrics": delta,
    }

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    if args.json_only:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print("Smart-Strategy Decision Simulator")
    print("=" * 72)
    print(f"Decision:      {mode} ({reason})")
    print(f"Current/Basis: {delta['total_files']} / {delta['basis_total_files']} files")
    print(f"Match Count:   {delta['match_count']}")
    print(f"Match Ratio:   {delta['match_ratio']:.3f}")
    print(f"Stub Ratio:    {args.stub_ratio:.3f}")
    print("-" * 72)
    print(f"Saved Calls:   {delta['saved_calls']}")
    print(f"Cleanup Calls: {delta['cleanup_calls']}  (deleted+changed)")
    print(f"Upload Calls:  {delta['upload_calls']}  (new+changed)")
    print(f"Upload Bytes:  {delta['upload_bytes']} ({_fmt_bytes(delta['upload_bytes'])})")
    print("-" * 72)
    print(f"identical/new/changed/deleted: {delta['identical_count']}/{delta['new_count']}/{delta['changed_count']}/{delta['deleted_count']}")

    if args.json_out:
        print(f"Result JSON:   {args.json_out}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
