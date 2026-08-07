#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_delta_plan.py — Delta vs. Full planen (Manifest-Diff, Phase-3-Aufwand).

Schätzt pro Snapshot, was Turbo-Delta (Scout + copyfolder + Phase 3 Bereinigung)
kosten würde — ohne Upload. Scout bevorzugt chronologischen Vorgänger (wie Production nach Fix).

Beispiele (pi-nas):
  cd /opt/apps/pcloud-tools/main
  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --missing-only

  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --snapshot 2026-07-25-040041

  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --missing-only --simulate-catchup

  /opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \\
    --env-file .env --missing-only --simulate-catchup --compare-static
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
    basis_strategy: str
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
    catchup_step: int = 0
    plan_mode: str = "static"


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
    scout_threshold: float,
) -> Tuple[Optional[str], float, str]:
    """Wie scout_best_pool_basis (pc.scout_pool_basis)."""
    basis, score, strategy = pc.scout_pool_basis(
        manifest, archive_dir, remote_snaps, scout_threshold=scout_threshold,
    )
    return basis, score, strategy


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
        return "FULL", "keine passende Basis (Similarity < Schwelle)"
    if similarity < scout_threshold:
        return "FULL", f"Similarity {similarity * 100:.1f}% < {scout_threshold * 100:.0f}%"
    tasks = added + changed
    if deleted > delete_full_threshold and deleted > tasks * 2:
        return "FULL", f"Phase-3 {deleted} >> Upload {tasks}"
    if deleted > tasks and deleted > delete_full_threshold:
        return "FULL", "mehr Deletes als Upload-Aufgaben"
    if tasks < 3000 and deleted < 1000:
        return "DELTA", "kleines Diff"
    if similarity >= 0.90 and deleted < delete_full_threshold:
        return "DELTA", f"hohe Similarity ({similarity * 100:.1f}%)"
    if deleted > delete_full_threshold:
        return "DELTA", f"Phase-3 hoch ({deleted}), aber Diff ausgewogen"
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


def _list_remote_snapshot_folders(cfg: dict, snaps_root: str) -> Set[str]:
    return {
        c["name"]
        for c in (pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
                  .get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
        and _SNAP_RE.match(c.get("name", ""))
    }


def _plan_one(
    *,
    snapshot: str,
    archive_dir: str,
    remote_ok: Set[str],
    remote_available: Set[str],
    scout_threshold: float,
    delete_full_threshold: int,
    catchup_step: int = 0,
    plan_mode: str = "static",
) -> Optional[DeltaPlanRow]:
    manifest_path = os.path.join(archive_dir, "manifests", f"{snapshot}.json")
    if not os.path.isfile(manifest_path):
        return None

    manifest = _load_manifest(manifest_path)
    current_files = _manifest_files(manifest)
    if not current_files:
        return None

    basis, similarity, strategy = _scout_best_basis(
        manifest, archive_dir, remote_available, scout_threshold,
    )
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
        basis_strategy=strategy,
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
        catchup_step=catchup_step,
        plan_mode=plan_mode,
    )


def _plan_targets(
    *,
    targets: List[str],
    archive_dir: str,
    remote_ok: Set[str],
    remote_available: Set[str],
    scout_threshold: float,
    delete_full_threshold: int,
    plan_mode: str,
    simulate_growth: bool,
) -> Tuple[List[DeltaPlanRow], List[str]]:
    """Plant Snapshots; bei simulate_growth wächst remote_available nach jedem Schritt."""
    rows: List[DeltaPlanRow] = []
    skipped: List[str] = []
    available = set(remote_available)
    ordered = sorted(targets)

    for step, snap in enumerate(ordered, start=1):
        row = _plan_one(
            snapshot=snap,
            archive_dir=archive_dir,
            remote_ok=remote_ok,
            remote_available=available,
            scout_threshold=scout_threshold,
            delete_full_threshold=delete_full_threshold,
            catchup_step=step if simulate_growth else 0,
            plan_mode=plan_mode,
        )
        if row:
            rows.append(row)
            if simulate_growth:
                available.add(snap)
        else:
            skipped.append(snap)

    return rows, skipped


def _print_table(rows: List[DeltaPlanRow], *, show_step: bool = False) -> None:
    step_col = f"{'#':>3} " if show_step else ""
    hdr = (
        f"{step_col}{'Snapshot':<22} {'Remote':^6} {'Empf.':^5} {'Basis':<22} {'Strat':^5} {'Sim%':>5} "
        f"{'+':>6} {'-':>6} {'Δ':>5} {'=':>6} {'P3':>6} {'P3dir':>5} {'P3stub':>6} {'P4':>6}  Grund"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        basis = r.basis or "-"
        remote = "ok" if r.remote_ok else "MISS"
        strat = (r.basis_strategy or "-")[:5]
        step_prefix = f"{r.catchup_step:3d} " if show_step else ""
        print(
            f"{step_prefix}{r.snapshot:<22} {remote:^6} {r.recommend:^5} {basis:<22} {strat:^5} {r.similarity_pct:5.1f} "
            f"{r.added:6d} {r.deleted:6d} {r.changed:5d} {r.unchanged:6d} "
            f"{r.phase3_total:6d} {r.phase3_dead_dirs:5d} {r.phase3_single_stubs:6d} {r.phase4_tasks:6d}  "
            f"{r.reason}"
        )


def _print_catchup_summary(rows: List[DeltaPlanRow]) -> None:
    if not rows:
        return
    total_p3 = sum(r.phase3_total for r in rows)
    total_p4 = sum(r.phase4_tasks for r in rows)
    n_full = sum(1 for r in rows if r.recommend == "FULL")
    n_delta = sum(1 for r in rows if r.recommend == "DELTA")
    first, last = rows[0], rows[-1]
    print()
    print(
        f"Catch-up gesamt ({len(rows)} Schritte): {n_delta}× DELTA, {n_full}× FULL | "
        f"P3={total_p3} P4={total_p4}"
    )
    print(
        f"  Schritt 1 ({first.snapshot}): Sim {first.similarity_pct}% Basis {first.basis or '-'} "
        f"P3={first.phase3_total} P4={first.phase4_tasks}"
    )
    print(
        f"  Schritt {last.catchup_step} ({last.snapshot}): Sim {last.similarity_pct}% Basis {last.basis or '-'} "
        f"P3={last.phase3_total} P4={last.phase4_tasks}"
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
    ap.add_argument(
        "--simulate-catchup",
        action="store_true",
        help="Chronologische Reihenfolge: Remote-Basis waechst nach jedem geplanten Snapshot",
    )
    ap.add_argument(
        "--static",
        action="store_true",
        help="Nur aktuellen Remote-Stand (keine Catch-up-Simulation)",
    )
    ap.add_argument(
        "--compare-static",
        action="store_true",
        help="Zusaetzlich statische Planung zum Vergleich anzeigen",
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
    remote_folders = _list_remote_snapshot_folders(cfg, snaps_root)
    rtb_snaps = _list_rtb_snapshots(args.rtb)

    if args.snapshot:
        targets = sorted(set(args.snapshot))
    elif args.missing_only:
        targets = [s for s in rtb_snaps if s not in remote_ok]
    else:
        targets = rtb_snaps

    simulate = args.simulate_catchup or (args.missing_only and not args.static)

    rows, skipped = _plan_targets(
        targets=targets,
        archive_dir=archive_dir,
        remote_ok=remote_ok,
        remote_available=remote_ok if simulate else remote_folders,
        scout_threshold=scout_threshold,
        delete_full_threshold=delete_full_threshold,
        plan_mode="catchup" if simulate else "static",
        simulate_growth=simulate,
    )

    static_rows: List[DeltaPlanRow] = []
    if args.compare_static and simulate:
        static_rows, static_skipped = _plan_targets(
            targets=targets,
            archive_dir=archive_dir,
            remote_ok=remote_ok,
            remote_available=remote_folders,
            scout_threshold=scout_threshold,
            delete_full_threshold=delete_full_threshold,
            plan_mode="static",
            simulate_growth=False,
        )
        skipped = sorted(set(skipped) | set(static_skipped))

    if args.as_json:
        payload: dict = {
            "remote_complete_count": len(remote_ok),
            "simulate_catchup": simulate,
            "rows": [asdict(r) for r in rows],
            "skipped_no_manifest": skipped,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        if static_rows:
            payload["static_rows"] = [asdict(r) for r in static_rows]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        mode = "Catch-up-Simulation (chronologisch)" if simulate else "Statisch (aktueller Remote-Stand)"
        print(f"Modus: {mode}")
        print(f"Remote ok: {len(remote_ok)} | Geplant: {len(rows)} | Manifest fehlt: {len(skipped)}")
        if skipped:
            print(f"  (kein Manifest: {', '.join(skipped[:8])}{'…' if len(skipped) > 8 else ''})")
        print()
        _print_table(rows, show_step=simulate)
        n_full = sum(1 for r in rows if r.recommend == "FULL")
        n_delta = sum(1 for r in rows if r.recommend == "DELTA")
        print()
        print(f"Empfehlung: {n_full}× FULL, {n_delta}× DELTA (Schwellen: Scout≥{scout_threshold*100:.0f}%, P3−>{delete_full_threshold})")
        if simulate:
            _print_catchup_summary(rows)
        if static_rows:
            print()
            print("=== Vergleich: Statisch (ohne Reihenfolge-Effekt) ===")
            _print_table(static_rows, show_step=False)
            n_full_s = sum(1 for r in static_rows if r.recommend == "FULL")
            n_delta_s = sum(1 for r in static_rows if r.recommend == "DELTA")
            print()
            print(f"Statisch: {n_full_s}× FULL, {n_delta_s}× DELTA")
        print(f"Laufzeit: {time.time() - t0:.1f}s")
        print()
        print("Legende: P3=Phase-3-Loeschungen, Strat=chrono|jaccard, P4=neu/geaendert, #=Catch-up-Schritt")
        print("Catch-up: chronologisch hochladen — jeder Schritt verbessert die Basis fuer den naechsten")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
