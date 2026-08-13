#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_verify_backup.py - Integritaetscheck Source-Manifeste vs. Remote Pool + Stubs.

A) Manifest-SHA vs. Pool-Dateinamen (_pool)
B) Manifest-Stubs vs. remote listfolder (Subtree-Batches, kein BFS pro Ordner)

Remote-Fetch:
  - Pool: listfolder_safe(_pool)
  - Index: ein Snapshot → _index/archive/<snap>_index.json; mehrere → content_index.json
  - Stubs: listfolder non-recursive auf Snap-Root, dann listfolder_safe pro Top-Level-Ordner

Optional --stub-sample N: Stub-Inhalte gegen Manifest + pool_refs pruefen.
"""
from __future__ import annotations
import os, sys, json, argparse, time, gc
import concurrent.futures
from dataclasses import dataclass, field
from typing import Dict, Set, List, Tuple, Optional

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc
import pcloud_path_compat as ppc


# ---------------------------------------------------------------------------
# Remote Pool+Index Cache (Batch-Audit: einmal Pool+Master-Index)
# ---------------------------------------------------------------------------

@dataclass
class PoolRemoteCache:
    """Gecachter _pool-SHA-Set + content_index pool_refs fuer mehrere Verify-Laeufe."""

    dest: str
    pool_shas: Set[str]
    pool_refs: dict
    fetched_at: float = field(default_factory=time.time)

    def matches(self, pool_root_raw: str) -> bool:
        return self.dest == pc._norm_remote_path(pool_root_raw).rstrip("/")

    def refresh_pool(self, cfg: dict) -> None:
        pool_root = f"{self.dest}/_pool"
        self.pool_shas = _collect_pool_shas(cfg, pool_root)
        self.fetched_at = time.time()

    @classmethod
    def fetch(cls, cfg: dict, pool_root_raw: str, *, verbose: bool = False) -> "PoolRemoteCache":
        def _out(msg: str) -> None:
            if verbose:
                print(msg, flush=True)

        dest = pc._norm_remote_path(pool_root_raw).rstrip("/")
        pool_root = f"{dest}/_pool"
        idx_path = f"{dest}/_snapshots/_index/content_index.json"
        t0 = time.time()
        pool_shas = _collect_pool_shas(cfg, pool_root)
        idx = _load_remote_json_at(cfg, idx_path)
        pool_refs = (idx or {}).get("pool_refs") or {}
        dt = time.time() - t0
        _out(
            f"[cache] Pool+Index geladen: {len(pool_shas)} SHA256s, "
            f"{len(pool_refs)} pool_refs ({dt:.1f}s)"
        )
        return cls(dest=dest, pool_shas=pool_shas, pool_refs=pool_refs)


# ---------------------------------------------------------------------------
# Remote fetch
# ---------------------------------------------------------------------------

def _sha_from_pool_entry(child: dict) -> Optional[str]:
    if child.get("isfolder"):
        return None
    name = child.get("name", "")
    if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
        return name
    return None


def _collect_pool_shas(cfg: dict, pool_root: str) -> Set[str]:
    flat = pc.call_with_backoff(pc.listfolder_safe, cfg, path=pool_root, nofiles=False)
    result: Set[str] = set()
    for child in flat:
        sha = _sha_from_pool_entry(child)
        if sha:
            result.add(sha)
    return result


def _stub_paths_from_flat(flat: list) -> Set[str]:
    result: Set[str] = set()
    for child in flat:
        path = child.get("_path") or ""
        name = child.get("name", "")
        if not child.get("isfolder") and name.endswith(".meta.json"):
            result.add(pc._norm_remote_path(path))
    return result


def _collect_stub_paths_subtree_batch(
    cfg: dict,
    snap_path: str,
    *,
    progress_cb=None,
) -> Set[str]:
    """
    Stub-Pfade: ein listfolder_safe pro Top-Level-Unterordner (Backup, Paperless, …).
    Schnell (~1 API-Call pro Bucket), RAM nur fuer einen Subtree — kein BFS pro Ordner.
    """
    root = pc._norm_remote_path(snap_path).rstrip("/")
    stubs: Set[str] = set()
    try:
        top = pc.call_with_backoff(
            pc.listfolder, cfg, path=root, recursive=False, nofiles=False,
        )
    except Exception:
        return stubs

    subfolders: List[str] = []
    for child in (top.get("metadata") or {}).get("contents") or []:
        name = child.get("name", "")
        path = f"{root}/{name}"
        if child.get("isfolder"):
            subfolders.append(path)
        elif name.endswith(".meta.json"):
            stubs.add(pc._norm_remote_path(path))

    total = len(subfolders)
    for i, folder_path in enumerate(subfolders, start=1):
        if progress_cb:
            progress_cb(i, total, folder_path)
        try:
            flat = pc.call_with_backoff(
                pc.listfolder_safe, cfg, path=folder_path, nofiles=False,
            )
            stubs |= _stub_paths_from_flat(flat)
        except Exception:
            continue
        del flat
        gc.collect()

    return stubs


def _load_remote_json_at(cfg: dict, path: str) -> Optional[dict]:
    """Remote JSON lesen; None wenn Datei fehlt (kein getfilelink-Exception)."""
    path = pc._norm_remote_path(path)
    if not pc.stat_file_safe(cfg, path=path):
        return None
    try:
        txt = pc.get_textfile(cfg, path=path, maxbytes=None)
        return json.loads(txt or "{}")
    except Exception:
        return None


def _fetch_pool_refs(
    cfg: dict,
    snaps_root: str,
    snapshot_filter: Optional[List[str]],
) -> Tuple[dict, str]:
    """pool_refs aus Snap-Archiv-Index (klein) oder Master content_index.json."""
    snaps_root = snaps_root.rstrip("/")
    if snapshot_filter and len(snapshot_filter) == 1:
        snap = snapshot_filter[0]
        archive_path = f"{snaps_root}/_index/archive/{snap}_index.json"
        idx = _load_remote_json_at(cfg, archive_path)
        if idx is not None:
            refs = idx.get("pool_refs") or {}
            return refs, f"archive/{snap}_index.json ({len(refs)} refs)"
        return {}, f"archive/{snap}_index.json (noch nicht vorhanden — Manifest-only)"

    master_path = f"{snaps_root}/_index/content_index.json"
    idx = _load_remote_json_at(cfg, master_path)
    if idx is None:
        return {}, "content_index.json (fehlt)"
    refs = idx.get("pool_refs") or {}
    return refs, f"content_index.json ({len(refs)} refs)"


def _manifest_stub_path(snaps_root: str, snap: str, relpath: str) -> str:
    rp = (relpath or "").replace("\\", "/").lstrip("/")
    return pc._norm_remote_path(f"{snaps_root}/{snap}/{rp}.meta.json")


def _load_manifests(
    manifests_dir: str,
    snapshots: List[str],
) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    result: Dict[str, Dict[str, str]] = {}
    corrupt: List[str] = []
    for snap in snapshots:
        path = os.path.join(manifests_dir, f"{snap}.json")
        if not os.path.exists(path):
            print(f"[warn] Manifest fehlt lokal: {path}")
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            if not raw.strip():
                print(f"[FAIL] Manifest leer: {path}")
                corrupt.append(path)
                continue
            m = json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[FAIL] Manifest ungueltig: {path} ({e})")
            corrupt.append(path)
            continue
        files = {
            it["relpath"]: (it.get("sha256") or "").lower()
            for it in m.get("items", [])
            if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
        }
        result[snap] = files
    return result, corrupt


# ---------------------------------------------------------------------------
# Kern-Checks
# ---------------------------------------------------------------------------

def check_manifest_vs_pool(
    manifests: Dict[str, Dict[str, str]],
    pool_shas: Set[str],
    pool_refs_keys: Set[str],
) -> dict:
    manifest_union: Set[str] = set()
    per_snap: Dict[str, dict] = {}

    for snap, files in manifests.items():
        snap_shas = set(files.values()) - {""}
        manifest_union |= snap_shas
        missing = snap_shas - pool_shas
        per_snap[snap] = {
            "total_files": len(files),
            "unique_shas": len(snap_shas),
            "missing_from_pool": sorted(missing)[:20],
            "missing_shas": sorted(missing),
            "missing_count": len(missing),
        }

    gc_candidates = pool_shas - manifest_union
    index_not_in_pool = pool_refs_keys - pool_shas
    pool_not_in_index = pool_shas - pool_refs_keys

    return {
        "manifest_sha_union": len(manifest_union),
        "pool_sha_count": len(pool_shas),
        "per_snapshot": per_snap,
        "gc_candidates": len(gc_candidates),
        "index_not_in_pool": sorted(index_not_in_pool)[:10],
        "pool_not_in_index": sorted(pool_not_in_index)[:10],
        "index_not_in_pool_count": len(index_not_in_pool),
        "pool_not_in_index_count": len(pool_not_in_index),
    }


def _norm_stub_path_set(paths: Set[str]) -> Set[str]:
    return {pc._norm_remote_path(p) for p in paths}


def _expected_norm_keys(manifests: Dict[str, Dict[str, str]], snaps_root: str) -> Set[str]:
    keys: Set[str] = set()
    for snap, files in manifests.items():
        for rp in files:
            ep = _manifest_stub_path(snaps_root, snap, rp)
            keys.add(ep)
            keys.add(ppc.normalize_path_segments(ep))
    return keys


def _orphan_stubs(
    norm_stubs: Set[str],
    manifests: Dict[str, Dict[str, str]],
    snaps_root: str,
) -> List[str]:
    expected_keys = _expected_norm_keys(manifests, snaps_root)
    orphans: List[str] = []
    for sp in norm_stubs:
        if sp in expected_keys:
            continue
        if ppc.normalize_path_segments(sp) in expected_keys:
            continue
        orphans.append(sp)
    return orphans


def check_stubs_vs_index(
    pool_refs: dict,
    stub_paths: Set[str],
    snaps_root: str,
    manifests: Dict[str, Dict[str, str]],
    snapshot_filter: Optional[Set[str]] = None,
) -> dict:
    filter_set = snapshot_filter if snapshot_filter else None
    norm_stubs = _norm_stub_path_set(stub_paths)
    stub_lookup = ppc.build_stub_path_lookup(norm_stubs)
    snaps_root = pc._norm_remote_path(snaps_root)

    manifest_missing: Dict[str, List[str]] = {}
    manifest_missing_total = 0
    path_compat_resolved = 0
    for snap, files in manifests.items():
        m_missing = []
        for rp in files:
            stub_path = _manifest_stub_path(snaps_root, snap, rp)
            if ppc.stub_path_exists(stub_path, stub_lookup):
                if ppc.relpath_has_risky_segments(rp) and stub_path not in norm_stubs:
                    path_compat_resolved += 1
                continue
            m_missing.append(rp)
        manifest_missing_total += len(m_missing)
        if m_missing:
            manifest_missing[snap] = m_missing[:20]

    if filter_set is not None:
        expected = {
            _manifest_stub_path(snaps_root, snap, rp)
            for snap, files in manifests.items()
            for rp in files
        }
        missing_stubs = {
            p for p in expected if not ppc.stub_path_exists(p, stub_lookup)
        }
        orphans = _orphan_stubs(norm_stubs, manifests, snaps_root)
        return {
            "expected_stubs": len(expected),
            "actual_stubs": len(norm_stubs),
            "missing_from_index": len(missing_stubs),
            "missing_from_index_examples": sorted(missing_stubs)[:5],
            "extra_not_in_index": len(orphans),
            "extra_stub_examples": orphans[:5],
            "manifest_missing_stubs": manifest_missing,
            "manifest_missing_total": manifest_missing_total,
            "path_compat_resolved": path_compat_resolved,
            "mode": "manifest_scoped",
        }

    expected: Set[str] = set()
    for sha, entry in pool_refs.items():
        if not isinstance(entry, dict):
            continue
        snaps_map = entry.get("snapshots")
        if not isinstance(snaps_map, dict):
            continue
        for snap, relpaths in snaps_map.items():
            for rp in (relpaths or []):
                expected.add(_manifest_stub_path(snaps_root, snap, rp))

    missing_stubs = {p for p in expected if not ppc.stub_path_exists(p, stub_lookup)}
    orphans = _orphan_stubs(norm_stubs, manifests, snaps_root)

    return {
        "expected_stubs": len(expected),
        "actual_stubs": len(norm_stubs),
        "missing_from_index": len(missing_stubs),
        "missing_from_index_examples": sorted(missing_stubs)[:5],
        "extra_not_in_index": len(orphans),
        "extra_stub_examples": orphans[:5],
        "manifest_missing_stubs": manifest_missing,
        "manifest_missing_total": manifest_missing_total,
        "path_compat_resolved": path_compat_resolved,
        "mode": "full_index",
    }


def check_stub_sample(
    cfg: dict,
    pool_refs: dict,
    manifests: Dict[str, Dict[str, str]],
    snaps_root: str,
    n: int,
    threads: int = 8,
) -> dict:
    import random
    candidates = []
    for snap, files in manifests.items():
        for rp, sha in files.items():
            if sha in pool_refs:
                candidates.append((snap, rp, sha))
    if not candidates:
        return {"sampled": 0, "ok": 0, "errors": []}
    sample = random.sample(candidates, min(n, len(candidates)))

    ok = 0
    errors = []

    def _check(args):
        snap, rp, expected_sha = args
        stub_path = f"{snaps_root}/{snap}/{rp}.meta.json"
        try:
            content = pc.get_textfile(cfg, path=stub_path, maxbytes=4096)
            stub = json.loads(content or "{}")
            stub_sha = (stub.get("sha256") or "").lower()
            stub_fid = stub.get("pool_fileid")
            idx_fid = (pool_refs.get(expected_sha) or {}).get("fileid")
            errs = []
            if stub_sha != expected_sha:
                errs.append(f"sha mismatch: stub={stub_sha[:12]} expected={expected_sha[:12]}")
            if stub_fid and idx_fid and int(stub_fid) != int(idx_fid):
                errs.append(f"fileid mismatch: stub={stub_fid} index={idx_fid}")
            return (True, errs)
        except Exception as e:
            return (False, [f"read error: {e}"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        results = list(ex.map(_check, sample))

    for success, errs in results:
        if success and not errs:
            ok += 1
        else:
            errors.extend(errs[:3])

    return {
        "sampled": len(sample),
        "ok": ok,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Main verify
# ---------------------------------------------------------------------------

def run_verify(
    cfg: dict,
    *,
    pool_root_raw: str,
    manifests_dir: str,
    snapshot_filter: List[str] | None = None,
    stub_sample: int = 0,
    verbose: bool = True,
    remote_cache: Optional[PoolRemoteCache] = None,
) -> dict:
    def _out(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    dest = pc._norm_remote_path(pool_root_raw).rstrip("/")
    pool_root = f"{dest}/_pool"
    snaps_root = f"{dest}/_snapshots"

    t0 = time.time()
    _out("=== pool_verify_backup ===")
    _out(f"Dest:      {dest}")
    _out(f"Manifeste: {manifests_dir}")
    if snapshot_filter:
        _out(f"Snapshots: {', '.join(snapshot_filter)} (gefiltert)")
    _out("")

    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
    remote_snaps_all = sorted(
        c["name"] for c in (top.get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
    )
    if snapshot_filter:
        unknown = set(snapshot_filter) - set(remote_snaps_all)
        if unknown:
            _out(f"[warn] Snapshots nicht remote: {', '.join(sorted(unknown))}")
        remote_snaps = [s for s in snapshot_filter if s in remote_snaps_all]
        if not remote_snaps:
            return {
                "ok": False,
                "issues": 1,
                "error": "no_valid_snapshots",
                "duration_sec": round(time.time() - t0, 2),
            }
    else:
        remote_snaps = remote_snaps_all

    _out(f"Remote-Snapshots: {remote_snaps}")

    manifests, corrupt_manifests = _load_manifests(manifests_dir, remote_snaps)
    if corrupt_manifests:
        _out(f"[FAIL] {len(corrupt_manifests)} defekte Manifest-Datei(en)")
        return {
            "ok": False,
            "issues": len(corrupt_manifests),
            "error": "corrupt_manifests",
            "corrupt_manifests": corrupt_manifests,
            "duration_sec": round(time.time() - t0, 2),
        }
    if not manifests:
        _out("[FAIL] Keine lokalen Manifeste gefunden.")
        return {
            "ok": False,
            "issues": 1,
            "error": "no_manifests",
            "duration_sec": round(time.time() - t0, 2),
        }
    total_manifest_files = sum(len(v) for v in manifests.values())
    _out(
        f"Lokale Manifeste: {len(manifests)}/{len(remote_snaps)} "
        f"({total_manifest_files} Dateien total)"
    )
    _out("")

    _out("[fetch] Lade remote Daten (subtree listfolder)...")
    t_fetch = time.time()
    stub_paths: Set[str] = set()
    use_cache = remote_cache is not None and remote_cache.matches(dest)
    single_snap = snapshot_filter and len(snapshot_filter) == 1

    if use_cache:
        pool_shas = remote_cache.pool_shas  # type: ignore[union-attr]
        _out(f"[fetch] Pool aus Cache ({len(pool_shas)} SHA256s)")
    else:
        _out("[fetch] Pool...")
        pool_shas = _collect_pool_shas(cfg, pool_root)
        _out(f"[fetch] Pool: {len(pool_shas)} SHA256s")
        gc.collect()

    if use_cache and not single_snap:
        pool_refs = remote_cache.pool_refs  # type: ignore[union-attr]
        index_src = f"cache ({len(pool_refs)} pool_refs)"
    else:
        _out("[fetch] Index...")
        pool_refs, index_src = _fetch_pool_refs(cfg, snaps_root, snapshot_filter)
        _out(f"[fetch] Index: {index_src}")
        gc.collect()

    for i, snap in enumerate(remote_snaps, start=1):
        snap_path = f"{snaps_root}/{snap}"

        def _progress(j: int, total: int, folder_path: str) -> None:
            label = folder_path.rsplit("/", 1)[-1] or folder_path
            _out(f"[fetch]   {snap}: subtree {j}/{total} ({label})")

        try:
            snap_stubs = _collect_stub_paths_subtree_batch(
                cfg, snap_path, progress_cb=_progress,
            )
            stub_paths |= snap_stubs
            _out(f"[fetch] {i}/{len(remote_snaps)} {snap}: {len(snap_stubs)} stubs")
        except Exception as e:
            _out(f"[warn] Stub-Fetch fehlgeschlagen fuer {snap}: {e}")
        gc.collect()

    dt_fetch = time.time() - t_fetch
    _out(
        f"[fetch] fertig: Pool {len(pool_shas)} SHA | "
        f"Stubs {len(stub_paths)} | Index {len(pool_refs)} refs | {dt_fetch:.1f}s"
    )
    _out("")

    issues = 0

    _out("=== A) Manifest vs Pool ===")
    t_a = time.time()
    res_a = check_manifest_vs_pool(manifests, pool_shas, set(pool_refs.keys()))
    for snap, r in res_a["per_snapshot"].items():
        status = "✓" if r["missing_count"] == 0 else "✗"
        _out(
            f"  {status} {snap}: {r['total_files']} Dateien, "
            f"{r['unique_shas']} unique SHAs, "
            f"{r['missing_count']} fehlen im Pool"
        )
        if r["missing_count"] > 0:
            issues += r["missing_count"]

    if not snapshot_filter and res_a["index_not_in_pool_count"] > 0:
        issues += res_a["index_not_in_pool_count"]
        _out(f"  [warn] Index ohne Pool-Datei (global): {res_a['index_not_in_pool_count']}")
    elif snapshot_filter and res_a["index_not_in_pool_count"] > 0:
        _out(
            f"  [info] Index ohne Pool-Datei (global, ignoriert): "
            f"{res_a['index_not_in_pool_count']}"
        )
    _out(f"  ({time.time()-t_a:.2f}s)")
    _out("")

    _out("=== B) Stubs vs Index vs Pool ===")
    t_b = time.time()
    stub_scope = set(snapshot_filter) if snapshot_filter else None
    res_b = check_stubs_vs_index(
        pool_refs, stub_paths, snaps_root, manifests, snapshot_filter=stub_scope,
    )
    mm = int(res_b.get("manifest_missing_total") or 0)
    if mm > 0:
        issues += mm
        _out(f"  ✗ {mm} Stub(s) fehlen (Manifest vs. remote)")
        for snap, paths in (res_b.get("manifest_missing_stubs") or {}).items():
            for rp in paths[:5]:
                _out(f"      {snap}: {rp}")
    elif stub_scope is not None and int(res_b.get("missing_from_index") or 0) > 0:
        mi = int(res_b["missing_from_index"])
        issues += mi
        _out(f"  ✗ {mi} Stub-Pfad(e) fehlen")
        for ex in res_b.get("missing_from_index_examples") or []:
            _out(f"      {ex}")
    elif mm == 0 and int(res_b.get("extra_not_in_index") or 0) > 0:
        extra = int(res_b["extra_not_in_index"])
        actual = int(res_b.get("actual_stubs") or 0)
        expected = int(res_b.get("expected_stubs") or 0)
        _out(
            f"  [info] {extra} überschüssige Stub(s) im Snapshot-Ordner "
            f"({actual} remote, {expected} im Manifest)"
        )
        for ex in res_b.get("extra_stub_examples") or []:
            _out(f"      {ex}")
    pc_resolved = int(res_b.get("path_compat_resolved") or 0)
    if pc_resolved > 0:
        _out(f"  [info] {pc_resolved} Stub(s) per Segment-Normalisierung")
    _out(f"  ({time.time()-t_b:.2f}s, {res_b.get('mode', 'listfolder')})")
    _out("")

    res_c = None
    if stub_sample > 0:
        _out(f"=== C) Stub-Sample ({stub_sample}) ===")
        res_c = check_stub_sample(cfg, pool_refs, manifests, snaps_root, stub_sample)
        if res_c.get("errors"):
            issues += len(res_c["errors"])
        _out("")

    dt_total = time.time() - t0
    summary_parts = []
    if not snapshot_filter and res_a["index_not_in_pool_count"] > 0:
        summary_parts.append(f"{res_a['index_not_in_pool_count']} pool missing")
    if res_b["manifest_missing_total"] > 0:
        summary_parts.append(f"{res_b['manifest_missing_total']} stubs missing")
    for snap, r in res_a["per_snapshot"].items():
        if r["missing_count"] > 0:
            summary_parts.append(f"{snap}: {r['missing_count']} manifest pool gaps")

    return {
        "ok": issues == 0,
        "issues": issues,
        "duration_sec": round(dt_total, 2),
        "snapshots": remote_snaps,
        "manifest_vs_pool": res_a,
        "stubs_vs_index": res_b,
        "stub_sample": res_c,
        "error_summary": "; ".join(summary_parts) if summary_parts else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Integritaetscheck: Source-Manifeste vs. Remote Pool + Stubs.",
    )
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--pool-root", help="Remote Pool-Root auf pCloud")
    ap.add_argument("--dest-root", help="(deprecated) Alias fuer --pool-root")
    _archive = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    ap.add_argument("--manifests-dir", default=os.path.join(_archive, "manifests"))
    ap.add_argument("--stub-sample", type=int, default=0)
    ap.add_argument(
        "--snapshots",
        metavar="SNAP",
        help="Nur diese Snapshot(s) pruefen (kommagetrennt).",
    )
    ap.add_argument("--json-out", help="Ergebnis als JSON schreiben")
    args = ap.parse_args()

    snapshot_filter: List[str] | None = None
    if args.snapshots:
        snapshot_filter = [
            s.strip() for part in args.snapshots.split(",")
            for s in part.split() if s.strip()
        ]

    pool_root_raw = args.pool_root or args.dest_root
    if not pool_root_raw:
        print("[FAIL] --pool-root erforderlich", file=sys.stderr)
        return 2

    cfg = pc.effective_config(env_file=args.env_file)
    result: dict
    exit_code = 0
    try:
        result = run_verify(
            cfg,
            pool_root_raw=pool_root_raw,
            manifests_dir=args.manifests_dir,
            snapshot_filter=snapshot_filter,
            stub_sample=args.stub_sample,
            verbose=True,
        )
        exit_code = 0 if result.get("ok") else 1
    except Exception as e:
        result = {
            "ok": False,
            "issues": 1,
            "error": str(e),
            "duration_sec": 0,
        }
        exit_code = 1
        print(f"[FAIL] {e}", file=sys.stderr)

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    dt_total = result.get("duration_sec", 0)
    print("=" * 60)
    if result.get("ok"):
        print(f"✓ ALLE CHECKS OK — Backup vollstaendig integr ({dt_total:.1f}s)")
    else:
        err = result.get("error") or result.get("error_summary") or f"{result.get('issues', 0)} PROBLEM(E)"
        print(f"✗ {err} ({dt_total:.1f}s)")
    print("=" * 60)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
