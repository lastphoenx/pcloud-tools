#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pool_verify_backup.py - Vollstaendiger Integritaetscheck Source vs. Remote.

Zwei Kern-Checks, kein Stub-Lesen, reine Set-Operationen im RAM:

A) MANIFEST vs POOL (lokal vs remote):
   sha256 aus lokalen Manifesten (ground truth) vs sha256-Dateinamen im _pool.
   Fehlende => Datei nicht sicherbar. Plus: Pool-Objekte ohne Manifest-Referenz
   => GC-Kandidaten (Hinweis ob GC sinnvoll).

B) INDEX vs STUBS vs POOL (remote vs remote):
   Erwartete Stub-Pfade aus pool_refs vs tatsaechliche Stub-Pfade aus listfolder.
   Pool-Set (aus A, bereits im RAM) vs pool_refs-Keys. Sekunden.

Optional --stub-sample N:
   N zufaellige Stubs parallel downloaden, stub.sha256 + stub.pool_fileid gegen
   Manifest + pool_refs kreuzen => echte Inhalts-Verifikation der Stub-Chain.

Ladezeit:
  - listfolder(_pool)            ~1.5s  (18k Objekte)
  - listfolder(_snapshots, rec)  ~5s    (79k Stubs) -- Flaschenhals
  - content_index.json           ~0.5s
  Alle 3 parallel => ~5s Gesamtladezeit.
  Set-Operationen fuer ~80k Manifest-Dateien => <1s.
  Gesamtzeit 4 Snapshots: ~6-8s (ohne --stub-sample).

Aufruf:
  MAIN_DIR=/opt/apps/pcloud-tools/main \\
  python pool_verify_backup.py \\
    --env-file $ENV_FILE --dest-root /Backup/rtb_pool \\
    --manifests-dir /srv/pcloud-archive/manifests

  # Mit Stub-Inhalts-Probe:
  python pool_verify_backup.py ... --stub-sample 100
"""
from __future__ import annotations
import os, sys, json, argparse, time
import concurrent.futures
from dataclasses import dataclass, field
from typing import Dict, Set, List, Tuple, Optional

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc
import pcloud_path_compat as ppc


# ---------------------------------------------------------------------------
# Remote Pool+Index Cache (einmal pro Batch, Stubs weiter pro Snapshot)
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

    @classmethod
    def fetch(
        cls,
        cfg: dict,
        pool_root_raw: str,
        *,
        verbose: bool = False,
    ) -> "PoolRemoteCache":
        def _out(msg: str) -> None:
            if verbose:
                print(msg, flush=True)

        dest = pc._norm_remote_path(pool_root_raw).rstrip("/")
        pool_root = f"{dest}/_pool"
        idx_path = f"{dest}/_snapshots/_index/content_index.json"
        t0 = time.time()

        def _fetch_pool() -> Set[str]:
            res = pc.call_with_backoff(
                pc.listfolder, cfg, path=pool_root, recursive=True, nofiles=False,
            )
            return _walk_pool(res.get("metadata", {}))

        def _fetch_index() -> dict:
            txt = pc.get_textfile(cfg, path=idx_path, maxbytes=None)
            idx = json.loads(txt or "{}")
            return idx.get("pool_refs") or {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_pool = ex.submit(_fetch_pool)
            f_index = ex.submit(_fetch_index)
            pool_shas = f_pool.result()
            pool_refs = f_index.result()

        dt = time.time() - t0
        _out(
            f"[cache] Pool+Index geladen: {len(pool_shas)} SHA256s, "
            f"{len(pool_refs)} pool_refs ({dt:.1f}s)"
        )
        return cls(dest=dest, pool_shas=pool_shas, pool_refs=pool_refs)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _walk_pool(node) -> Set[str]:
    """Extrahiert sha256-Dateinamen (64-Hex) aus listfolder-Baum von _pool."""
    result: Set[str] = set()
    for child in node.get("contents", []) or []:
        if child.get("isfolder"):
            result |= _walk_pool(child)
        else:
            name = child.get("name", "")
            if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
                result.add(name)
    return result


def _walk_stubs(node, cur: str) -> Set[str]:
    """Extrahiert vollstaendige Pfade aller .meta.json-Dateien aus listfolder-Baum."""
    result: Set[str] = set()
    for child in node.get("contents", []) or []:
        name = child.get("name", "")
        path = f"{cur}/{name}"
        if child.get("isfolder"):
            result |= _walk_stubs(child, path)
        elif name.endswith(".meta.json"):
            result.add(path)
    return result


def _load_manifests(
    manifests_dir: str,
    snapshots: List[str],
) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """
    Laedt lokale Manifeste. Gibt ({snapshot: {relpath: sha256}}, corrupt_paths) zurueck.
    """
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
    """
    A) Manifest vs Pool.
    Fuer jede sha256 aus allen lokalen Manifesten: ist sie im Pool (Dateiname)?
    Zusatz: pool_shas - manifest_union = GC-Kandidaten (im Pool aber in keinem Manifest).
    """
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
            "missing_count": len(missing),
        }

    gc_candidates = pool_shas - manifest_union  # im Pool aber nicht in Manifesten
    index_not_in_pool = pool_refs_keys - pool_shas  # im Index aber Pool-Datei fehlt
    pool_not_in_index = pool_shas - pool_refs_keys  # im Pool aber nicht im Index

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


def _manifest_stub_path(snaps_root: str, snap: str, relpath: str) -> str:
    """Einheitlicher Stub-Pfad (Manifest-relpath -> pCloud-Pfad)."""
    rp = (relpath or "").replace("\\", "/").lstrip("/")
    return pc._norm_remote_path(f"{snaps_root}/{snap}/{rp}.meta.json")


def _norm_stub_path_set(paths: Set[str]) -> Set[str]:
    return {pc._norm_remote_path(p) for p in paths}


def check_stubs_vs_index(
    pool_refs: dict,
    stub_paths: Set[str],
    snaps_root: str,
    manifests: Dict[str, Dict[str, str]],
    snapshot_filter: Optional[Set[str]] = None,
) -> dict:
    """
    B) Index vs Stubs vs Pool.
    Erwartete Stubs aus pool_refs vs tatsaechliche Stubs aus listfolder.
    Zusatz: manifest-getriebener Stub-Check (jeder Manifest-relpath hat einen Stub?).

    Bei snapshot_filter: erwartete Stubs nur aus Manifesten (O(~80k)), nicht
    gesamter pool_refs×alle Snapshots (O(~106k×Snaps) — war ~25s Flaschenhals).
    """
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
        expected_norm = {ppc.normalize_path_segments(p) for p in expected}
        stub_norm = {ppc.normalize_path_segments(p) for p in norm_stubs}
        return {
            "expected_stubs": len(expected),
            "actual_stubs": len(norm_stubs),
            "missing_from_index": len(missing_stubs),
            "missing_from_index_examples": sorted(missing_stubs)[:5],
            "extra_not_in_index": len(stub_norm - expected_norm),
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
    extra_stubs = norm_stubs - expected

    return {
        "expected_stubs": len(expected),
        "actual_stubs": len(norm_stubs),
        "missing_from_index": len(missing_stubs),
        "missing_from_index_examples": sorted(missing_stubs)[:5],
        "extra_not_in_index": len(extra_stubs),
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
    """
    Optional: N Stubs parallel lesen, Inhalt gegen pool_refs + Manifest kreuzen.
    Stub.sha256 == pool_refs-Key? Stub.pool_fileid == pool_refs[sha].fileid?
    """
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
            idx_fid  = (pool_refs.get(expected_sha) or {}).get("fileid")
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
# Main
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
    """
    Fuehrt Integritaetscheck aus. Gibt strukturiertes Ergebnis-Dict zurueck.
    snapshot_filter: nur diese Snapshots pruefen (RAM-schonend, ein Snap nach Upload).
    remote_cache: Pool+Index einmal pro Batch laden, Stubs weiter pro Snapshot.
    """
    def _out(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    dest = pc._norm_remote_path(pool_root_raw).rstrip("/")
    pool_root = f"{dest}/_pool"
    snaps_root = f"{dest}/_snapshots"
    idx_path = f"{snaps_root}/_index/content_index.json"

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
    _out(f"Lokale Manifeste: {len(manifests)}/{len(remote_snaps)} ({total_manifest_files} Dateien total)")
    _out("")

    _out("[fetch] Lade remote Daten...")
    t_fetch = time.time()
    pool_shas: Set[str] = set()
    stub_paths: Set[str] = set()
    pool_refs: dict = {}

    use_cache = remote_cache is not None and remote_cache.matches(dest)
    if use_cache:
        pool_shas = remote_cache.pool_shas  # type: ignore[union-attr]
        pool_refs = remote_cache.pool_refs  # type: ignore[union-attr]
        _out(
            f"[fetch] Pool+Index aus Cache ({len(pool_shas)} SHA256s, "
            f"{len(pool_refs)} pool_refs)"
        )

    def _fetch_pool():
        res = pc.call_with_backoff(pc.listfolder, cfg, path=pool_root, recursive=True, nofiles=False)
        return _walk_pool(res.get("metadata", {}))

    def _fetch_snap_stubs(snap):
        snap_path = f"{snaps_root}/{snap}"
        res = pc.call_with_backoff(pc.listfolder, cfg, path=snap_path, recursive=True, nofiles=False)
        paths: Set[str] = set()

        def _walk(node, cur):
            for child in node.get("contents", []) or []:
                name = child.get("name", "")
                p = f"{cur}/{name}"
                if child.get("isfolder"):
                    _walk(child, p)
                elif name.endswith(".meta.json"):
                    paths.add(pc._norm_remote_path(p))

        _walk(res.get("metadata", {}), snap_path)
        return paths

    def _fetch_index():
        txt = pc.get_textfile(cfg, path=idx_path, maxbytes=None)
        idx = json.loads(txt or "{}")
        return idx.get("pool_refs") or {}

    if use_cache:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(remote_snaps) or 1) as ex:
            snap_futures = {snap: ex.submit(_fetch_snap_stubs, snap) for snap in remote_snaps}
            done = 0
            for snap, fut in snap_futures.items():
                try:
                    snap_stubs = fut.result()
                    stub_paths |= snap_stubs
                    done += 1
                    _out(f"[fetch] {done}/{len(remote_snaps)} {snap}: {len(snap_stubs)} stubs")
                except Exception as e:
                    done += 1
                    _out(f"[warn] Stub-Fetch fehlgeschlagen fuer {snap}: {e}")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            f_pool = ex.submit(_fetch_pool)
            f_index = ex.submit(_fetch_index)
            pool_refs = f_index.result()
            pool_shas = f_pool.result()
            snap_futures = {snap: ex.submit(_fetch_snap_stubs, snap) for snap in remote_snaps}
            done = 0
            for snap, fut in snap_futures.items():
                try:
                    snap_stubs = fut.result()
                    stub_paths |= snap_stubs
                    done += 1
                    _out(f"[fetch] {done}/{len(remote_snaps)} {snap}: {len(snap_stubs)} stubs")
                except Exception as e:
                    done += 1
                    _out(f"[warn] Stub-Fetch fehlgeschlagen fuer {snap}: {e}")

    dt_fetch = time.time() - t_fetch
    _out(f"[fetch] Pool: {len(pool_shas)} SHA256s | "
         f"Stubs: {len(stub_paths)} | "
         f"Index: {len(pool_refs)} pool_refs | "
         f"{dt_fetch:.1f}s")
    _out("")

    issues = 0

    _out("=== A) Manifest vs Pool ===")
    t_a = time.time()
    res_a = check_manifest_vs_pool(manifests, pool_shas, set(pool_refs.keys()))
    for snap, r in res_a["per_snapshot"].items():
        status = "✓" if r["missing_count"] == 0 else "✗"
        _out(f"  {status} {snap}: {r['total_files']} Dateien, "
             f"{r['unique_shas']} unique SHAs, "
             f"{r['missing_count']} fehlen im Pool")
        if r["missing_count"] > 0:
            issues += r["missing_count"]

    # Globaler Index/Pool-Drift nur bei Voll-Pool-Check — nicht pro Snapshot bewerten
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
        _out(f"  [info] {res_b['extra_not_in_index']} Extra-Stub(s) (nicht im Manifest)")
    pc_resolved = int(res_b.get("path_compat_resolved") or 0)
    if pc_resolved > 0:
        _out(f"  [info] {pc_resolved} Stub(s) per Segment-Normalisierung (z.B. trailing space)")
    mode = res_b.get("mode", "full_index")
    _out(f"  ({time.time()-t_b:.2f}s, {mode})")
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
    ap = argparse.ArgumentParser(description="Integritaetscheck: Source-Manifeste vs. Remote Pool + Stubs.")
    ap.add_argument("--env-file",       required=True)
    ap.add_argument("--pool-root",      help="Remote Pool-Root auf pCloud")
    ap.add_argument("--dest-root",      help="(deprecated) Alias fuer --pool-root")
    _archive = os.environ.get("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive")
    ap.add_argument("--manifests-dir",
                    default=os.path.join(_archive, "manifests"))
    ap.add_argument("--stub-sample",    type=int, default=0,
                    help="N Stubs Inhalt lesen + kreuzen (0 = aus, default 0)")
    ap.add_argument(
        "--snapshots",
        metavar="SNAP",
        help="Nur diese Snapshot(s) pruefen (kommagetrennt). RAM-schonend fuer Post-Upload.",
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
        print("[FAIL] --pool-root erforderlich (--dest-root ist deprecated)", file=sys.stderr)
        return 2
    if args.dest_root and not args.pool_root:
        print("[warn] --dest-root ist deprecated, bitte --pool-root verwenden")

    cfg = pc.effective_config(env_file=args.env_file)
    result = run_verify(
        cfg,
        pool_root_raw=pool_root_raw,
        manifests_dir=args.manifests_dir,
        snapshot_filter=snapshot_filter,
        stub_sample=args.stub_sample,
        verbose=True,
    )

    if args.json_out:
        os.makedirs(os.path.dirname(args.json_out) or ".", exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

    dt_total = result.get("duration_sec", 0)
    print("=" * 60)
    if result.get("ok"):
        print(f"✓ ALLE CHECKS OK — Backup vollstaendig integr ({dt_total:.1f}s)")
    else:
        err = result.get("error") or f"{result.get('issues', 0)} PROBLEM(E)"
        print(f"✗ {err} ({dt_total:.1f}s)")
    print("=" * 60)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
