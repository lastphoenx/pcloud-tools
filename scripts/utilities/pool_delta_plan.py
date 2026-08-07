#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_delta_plan.py — Delta vs. Full planen (Manifest-Diff, Phase-3-Aufwand).

Schätzt pro Snapshot, was Turbo-Delta (Scout + copyfolder + Phase 3 Bereinigung)
kosten würde — ohne Upload. Hilft bei Catch-up: wann PCLOUD_SCOUT_ENABLED=0 (Full).

Beispiele (pi-nas):
  cd /opt/apps/pcloud-tools/main
  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --missing-only

  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --snapshot 2026-07-25-040041

  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --missing-only --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Set, Tuple

MAIN_DIR = os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main")
sys.path.insert(0, MAIN_DIR)
import pcloud_bin_lib as pc  # noqa: E402

_SNAP_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}-\d{6}$")


@dataclass
class DeltaPlanRow:
    snapshot: str
    remote_ok: bool
    basis: Optional[str]
    similarity_pct: float
    files: int
    added: int
    deleted: int
    changed: int
    unchanged: int
    phase3_total: int
    phase3_dead_dirs: int
    phase3_single_stubs: int
    phase4_tasks: int
    recommend: str
    reason: str
    basis_newer_than_target: bool


def _manifest_files(manifest: dict) -> Dict[str, str]:
    return {
        str(it["relpath"]): str(it.get("sha256") or "")
        for it in manifest.get("items", [])
        if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
    }


def _load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _compute_diff(current: Dict[str, str], basis: Dict[str, str]) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    current_paths = set(current)
    basis_paths = set(basis)
    added = current_paths - basis_paths
    deleted = basis_paths - current_paths
    common = current_paths & basis_paths
    changed = {p for p in common if current[p] != basis[p]}
    unchanged = common - changed
    return added, deleted, changed, unchanged


def _plan_phase3(paths_to_remove: Set[str], current_paths: Set[str]) -> Tuple[int, int, int]:
    """Wie push_pool_delta_mode Phase 3 (ohne Remote-Klon-Abgleich)."""
    kept_dirs: Set[str] = set()
    for rp in current_paths:
        d = os.path.dirname(rp)
        while d:
            kept_dirs.add(d)
            d = os.path.dirname(d)

    dead_top_dirs: Set[str] = set()
    single_stubs: List[str] = []
    for rp in paths_to_remove:
        parent = os.path.dirname(rp)
        if parent and parent not in kept_dirs:
            top = parent
            while True:
                gp = os.path.dirname(top)
                if gp and gp not in kept_dirs:
                    top = gp
                else:
                    break
            dead_top_dirs.add(top)
        else:
            single_stubs.append(rp)

    total = len(paths_to_remove)
    return len(dead_top_dirs), len(single_stubs), total


def _scout_best_basis(
    manifest: dict,
    archive_dir: str,
    remote_snaps: Set[str],
) -> Tuple[Optional[str], float]:
    """Jaccard wie scout_best_pool_basis, ohne Log."""
    current_name = manifest.get("snapshot")
    current_files = _manifest_files(manifest)
    if not current_files:
        return None, 0.0

    manifests_path = os.path.join(archive_dir, "manifests")
    candidates = sorted(remote_snaps - {current_name}, reverse=True)
    best_snap: Optional[str] = None
    best_score = 0.0

    for snap_name in candidates:
        basis_path = os.path.join(manifests_path, f"{snap_name}.json")
        if not os.path.isfile(basis_path):
            continue
        try:
            basis_manifest = _load_manifest(basis_path)
            basis_files = _manifest_files(basis_manifest)
            if not basis_files:
                continue
            matches = sum(1 for rp, sha in current_files.items() if basis_files.get(rp) == sha)
            score = matches / len(current_files)
            if score > best_score:
                best_score = score
                best_snap = snap_name
            if best_score > 0.95:
                break
        except (OSError, json.JSONDecodeError, KeyError):
            continue

    return best_snap, best_score


def _recommend(
    *,
    snapshot: str,
    basis: Optional[str],
    similarity: float,
    deleted: int,
    added: int,
    changed: int,
    phase3_total: int,
    scout_threshold: float,
    delete_full_threshold: int,
) -> Tuple[str, str]:
    if not basis:
        return "FULL", "kein Remote-Basis-Manifest"
    if similarity < scout_threshold:
        return "FULL", f"Scout {similarity * 100:.1f}% < {scout_threshold * 100:.0f}%"
    if basis > snapshot:
        return "FULL", f"Basis {basis} chronologisch neuer als Ziel"
    if deleted > delete_full_threshold:
        return "FULL", f"Phase-3-Bereinigung {deleted} > {delete_full_threshold}"
    tasks = added + changed
    if deleted > 2000 and phase3_total == deleted:
        return "FULL", f"viele Einzel-Loeschungen ({deleted})"
    if tasks < 3000 and deleted < 1000:
        return "DELTA", "kleines Diff"
    if deleted > added + changed:
        return "FULL", "mehr Deletes als Upload-Aufgaben"
    return "DELTA", "Diff ok"


def _list_rtb_snapshots(rtb_root: str) -> List[str]:
    if not os.path.isdir(rtb_root):
        return []
    return sorted(
        n for n in os.listdir(rtb_root)
        if _SNAP_RE.match(n) and os.path.isdir(os.path.join(rtb_root, n))
    )


def _remote_complete_snaps(cfg: dict, snaps_root: str, workers: int = 8) -> Set[str]:
    import concurrent.futures

    remote = sorted(
        c["name"]
        for c in (pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
                  .get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
        and _SNAP_RE.match(c.get("name", ""))
    )

    def _ok(snap: str) -> Tuple[str, bool]:
        path = f"{snaps_root}/{snap}/.upload_complete"
        md = pc.stat_file_safe(cfg, path=path)
        if not md or not md.get("fileid"):
            return snap, False
        try:
            return snap, pc.upload_complete_matches_snapshot(cfg, path, snap)
        except Exception:
            return snap, False

    out: Set[str] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for snap, ok in ex.map(_ok, remote):
            if ok:
                out.add(snap)
    return out


def _plan_one(
    cfg: dict,
    *,
    snapshot: str,
    archive_dir: str,
    snaps_root: str,
    remote_ok: Set[str],
    scout_threshold: float,
    delete_full_threshold: int,
) -> Optional[DeltaPlanRow]:
    manifest_path = os.path.join(archive_dir, "manifests", f"{snapshot}.json")
    if not os.path.isfile(manifest_path):
        return None

    manifest = _load_manifest(manifest_path)
    current_files = _manifest_files(manifest)
    if not current_files:
        return None

    remote_folders = set(
        c["name"]
        for c in (pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
                  .get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
    )

    basis, similarity = _scout_best_basis(manifest, archive_dir, remote_folders)
    added: Set[str] = set()
    deleted: Set[str] = set()
    changed: Set[str] = set()
    unchanged: Set[str] = set()

    if basis:
        basis_path = os.path.join(archive_dir, "manifests", f"{basis}.json")
        basis_files = _manifest_files(_load_manifest(basis_path))
        added, deleted, changed, unchanged = _compute_diff(current_files, basis_files)

    paths_to_remove = set(deleted)
    dead_dirs, single_stubs, phase3_total = _plan_phase3(paths_to_remove, set(current_files))
    rec, reason = _recommend(
        snapshot=snapshot,
        basis=basis,
        similarity=similarity,
        deleted=len(deleted),
        added=len(added),
        changed=len(changed),
        phase3_total=phase3_total,
        scout_threshold=scout_threshold,
        delete_full_threshold=delete_full_threshold,
    )

    return DeltaPlanRow(
        snapshot=snapshot,
        remote_ok=snapshot in remote_ok,
        basis=basis,
        similarity_pct=round(similarity * 100, 1),
        files=len(current_files),
        added=len(added),
        deleted=len(deleted),
        changed=len(changed),
        unchanged=len(unchanged),
        phase3_total=phase3_total,
        phase3_dead_dirs=dead_dirs,
        phase3_single_stubs=single_stubs,
        phase4_tasks=len(added) + len(changed),
        recommend=rec,
        reason=reason,
        basis_newer_than_target=bool(basis and basis > snapshot),
    )


def _print_table(rows: List[DeltaPlanRow]) -> None:
    hdr = (
        f"{'Snapshot':<22} {'Remote':^6} {'Empf.':^5} {'Basis':<22} {'Sim%':>5} "
        f"{'+':>6} {'-':>6} {'Δ':>5} {'=':>6} {'P3':>6} {'P3dir':>5} {'P3stub':>6} {'P4':>6}  Grund"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        basis = r.basis or "-"
        remote = "ok" if r.remote_ok else "MISS"
        print(
            f"{r.snapshot:<22} {remote:^6} {r.recommend:^5} {basis:<22} {r.similarity_pct:5.1f} "
            f"{r.added:6d} {r.deleted:6d} {r.changed:5d} {r.unchanged:6d} "
            f"{r.phase3_total:6d} {r.phase3_dead_dirs:5d} {r.phase3_single_stubs:6d} {r.phase4_tasks:6d}  "
            f"{r.reason}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Delta vs Full Planung pro Snapshot (Manifest-Diff).")
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--dest-root", default=None, help="Default: PCLOUD_DEST aus .env")
    ap.add_argument("--archive-dir", default=None, help="Default: PCLOUD_ARCHIVE_DIR")
    ap.add_argument("--rtb", default=os.environ.get("RTB", "/mnt/backup/rtb_nas"))
    ap.add_argument("--snapshot", action="append", default=[], help="Einzelner Snapshot (mehrfach möglich)")
    ap.add_argument("--missing-only", action="store_true", help="Nur RTB-Snapshots ohne remote .upload_complete")
    ap.add_argument("--all-rtb", action="store_true", help="Alle RTB-Snapshots mit lokalem Manifest")
    ap.add_argument("--scout-threshold", type=float, default=None, help="Default: PCLOUD_SCOUT_THRESHOLD oder 0.70")
    ap.add_argument(
        "--delete-full-threshold",
        type=int,
        default=None,
        help="Ab so vielen Phase-3-Loeschungen → FULL (Default: PCLOUD_DELTA_PLAN_DELETE_FULL oder 5000)",
    )
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file)
    env_vars = pc.load_env_file(args.env_file)
    dest = pc._norm_remote_path(
        args.dest_root or env_vars.get("PCLOUD_DEST") or os.environ.get("PCLOUD_DEST", "/Backup/rtb_pool")
    )
    dest = dest.rstrip("/")
    snaps_root = f"{dest}/_snapshots"
    archive_dir = args.archive_dir or env_vars.get("PCLOUD_ARCHIVE_DIR") or os.environ.get(
        "PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"
    )
    scout_threshold = float(
        args.scout_threshold
        if args.scout_threshold is not None
        else os.environ.get("PCLOUD_SCOUT_THRESHOLD", "0.70")
    )
    delete_full_threshold = int(
        args.delete_full_threshold
        if args.delete_full_threshold is not None
        else os.environ.get("PCLOUD_DELTA_PLAN_DELETE_FULL", "5000")
    )

    if not args.snapshot and not args.missing_only and not args.all_rtb:
        ap.error("Eines von --snapshot, --missing-only oder --all-rtb angeben.")

    pc.preflight_or_raise(cfg)

    t0 = time.time()
    remote_ok = _remote_complete_snaps(cfg, snaps_root)
    rtb_snaps = _list_rtb_snapshots(args.rtb)

    if args.snapshot:
        targets = sorted(set(args.snapshot))
    elif args.missing_only:
        targets = [s for s in rtb_snaps if s not in remote_ok]
    else:
        targets = rtb_snaps

    rows: List[DeltaPlanRow] = []
    skipped: List[str] = []
    for snap in targets:
        row = _plan_one(
            cfg,
            snapshot=snap,
            archive_dir=archive_dir,
            snaps_root=snaps_root,
            remote_ok=remote_ok,
            scout_threshold=scout_threshold,
            delete_full_threshold=delete_full_threshold,
        )
        if row:
            rows.append(row)
        else:
            skipped.append(snap)

    if args.as_json:
        print(json.dumps({
            "remote_complete_count": len(remote_ok),
            "rows": [asdict(r) for r in rows],
            "skipped_no_manifest": skipped,
            "elapsed_sec": round(time.time() - t0, 1),
        }, indent=2, ensure_ascii=False))
    else:
        print(f"Remote ok: {len(remote_ok)} | Geplant: {len(rows)} | Manifest fehlt: {len(skipped)}")
        if skipped:
            print(f"  (kein Manifest: {', '.join(skipped[:8])}{'…' if len(skipped) > 8 else ''})")
        print()
        _print_table(rows)
        n_full = sum(1 for r in rows if r.recommend == "FULL")
        n_delta = sum(1 for r in rows if r.recommend == "DELTA")
        print()
        print(f"Empfehlung: {n_full}× FULL, {n_delta}× DELTA (Schwellen: Scout≥{scout_threshold*100:.0f}%, P3−>{delete_full_threshold})")
        print(f"Laufzeit: {time.time() - t0:.1f}s")
        print()
        print("Legende: P3=Phase-3-Loeschungen, P3dir=rekursive Ordner, P3stub=Einzel-Stubs, P4=neu/geaendert")
        print("Catch-up: FULL → PCLOUD_SCOUT_ENABLED=0 ./wrapper_pcloud_pool_sync_1to1.sh <SNAP>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
