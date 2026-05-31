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
from typing import Dict, Set, List, Tuple

sys.path.insert(0, os.environ.get("MAIN_DIR", "/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc


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


def _load_manifests(manifests_dir: str, snapshots: List[str]) -> Dict[str, Dict[str, str]]:
    """
    Laedt lokale Manifeste. Gibt {snapshot: {relpath: sha256}} zurueck.
    Nur Snapshots fuer die ein Manifest existiert.
    """
    result = {}
    for snap in snapshots:
        path = os.path.join(manifests_dir, f"{snap}.json")
        if not os.path.exists(path):
            print(f"[warn] Manifest fehlt lokal: {path}")
            continue
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        files = {
            it["relpath"]: (it.get("sha256") or "").lower()
            for it in m.get("items", [])
            if it.get("type") == "file" and it.get("relpath") and it.get("sha256")
        }
        result[snap] = files
    return result


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


def check_stubs_vs_index(
    pool_refs: dict,
    stub_paths: Set[str],
    snaps_root: str,
    manifests: Dict[str, Dict[str, str]],
) -> dict:
    """
    B) Index vs Stubs vs Pool.
    Erwartete Stubs aus pool_refs vs tatsaechliche Stubs aus listfolder.
    Zusatz: manifest-getriebener Stub-Check (jeder Manifest-relpath hat einen Stub?).
    """
    expected: Set[str] = set()
    for sha, entry in pool_refs.items():
        if not isinstance(entry, dict):
            continue
        snaps_map = entry.get("snapshots")
        if not isinstance(snaps_map, dict):
            continue
        for snap, relpaths in snaps_map.items():
            for rp in (relpaths or []):
                expected.add(f"{snaps_root}/{snap}/{rp}.meta.json")

    missing_stubs = expected - stub_paths       # Index sagt: da, aber nicht remote
    extra_stubs   = stub_paths - expected        # remote da, aber nicht im Index

    # Manifest-getriebener Stub-Check (ground truth)
    manifest_missing: Dict[str, List[str]] = {}
    for snap, files in manifests.items():
        m_missing = []
        for rp in files:
            stub_path = f"{snaps_root}/{snap}/{rp}.meta.json"
            if stub_path not in stub_paths:
                m_missing.append(rp)
        if m_missing:
            manifest_missing[snap] = m_missing[:20]

    return {
        "expected_stubs": len(expected),
        "actual_stubs": len(stub_paths),
        "missing_from_index": len(missing_stubs),
        "missing_from_index_examples": sorted(missing_stubs)[:5],
        "extra_not_in_index": len(extra_stubs),
        "manifest_missing_stubs": manifest_missing,
        "manifest_missing_total": sum(len(v) for v in manifest_missing.values()),
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

def main() -> int:
    ap = argparse.ArgumentParser(description="Integritaetscheck: Source-Manifeste vs. Remote Pool + Stubs.")
    ap.add_argument("--env-file",       required=True)
    ap.add_argument("--dest-root",      required=True)
    ap.add_argument("--manifests-dir",  default="/srv/pcloud-archive/manifests")
    ap.add_argument("--stub-sample",    type=int, default=0,
                    help="N Stubs Inhalt lesen + kreuzen (0 = aus, default 0)")
    args = ap.parse_args()

    cfg       = pc.effective_config(env_file=args.env_file)
    dest      = pc._norm_remote_path(args.dest_root).rstrip("/")
    pool_root = f"{dest}/_pool"
    snaps_root= f"{dest}/_snapshots"
    idx_path  = f"{snaps_root}/_index/content_index.json"

    t0 = time.time()
    print("=== pool_verify_backup ===")
    print(f"Dest:      {dest}")
    print(f"Manifeste: {args.manifests_dir}")
    print()

    # ---- Remote-Snapshots ermitteln ----
    top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True)
    remote_snaps = sorted(
        c["name"] for c in (top.get("metadata", {}) or {}).get("contents", []) or []
        if c.get("isfolder") and c.get("name") and c.get("name") != "_index"
    )
    print(f"Remote-Snapshots: {remote_snaps}")

    # ---- Lokale Manifeste laden ----
    manifests = _load_manifests(args.manifests_dir, remote_snaps)
    if not manifests:
        print("[FAIL] Keine lokalen Manifeste gefunden.")
        return 1
    total_manifest_files = sum(len(v) for v in manifests.values())
    print(f"Lokale Manifeste: {len(manifests)}/{len(remote_snaps)} ({total_manifest_files} Dateien total)")
    print()

    # ---- Phase 0: 3 parallele Remote-Ladevorgaenge ----
    print("[fetch] Lade remote Daten (parallel)...")
    t_fetch = time.time()
    pool_shas:  Set[str] = set()
    stub_paths: Set[str] = set()
    pool_refs:  dict     = {}

    def _fetch_pool():
        res = pc.call_with_backoff(pc.listfolder, cfg, path=pool_root, recursive=True, nofiles=False)
        return _walk_pool(res.get("metadata", {}))

    def _fetch_stubs():
        res = pc.call_with_backoff(pc.listfolder, cfg, path=snaps_root, recursive=True, nofiles=False)
        return _walk_stubs(res.get("metadata", {}), snaps_root)

    def _fetch_index():
        txt = pc.get_textfile(cfg, path=idx_path, maxbytes=None)
        idx = json.loads(txt or "{}")
        return idx.get("pool_refs") or {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_pool  = ex.submit(_fetch_pool)
        f_stubs = ex.submit(_fetch_stubs)
        f_index = ex.submit(_fetch_index)
        pool_shas  = f_pool.result()
        stub_paths = f_stubs.result()
        pool_refs  = f_index.result()

    dt_fetch = time.time() - t_fetch
    print(f"[fetch] Pool: {len(pool_shas)} SHA256s | "
          f"Stubs: {len(stub_paths)} | "
          f"Index: {len(pool_refs)} pool_refs | "
          f"{dt_fetch:.1f}s")
    print()

    issues = 0

    # ---- Check A: Manifest vs Pool ----
    print("=== A) Manifest vs Pool ===")
    t_a = time.time()
    res_a = check_manifest_vs_pool(manifests, pool_shas, set(pool_refs.keys()))
    for snap, r in res_a["per_snapshot"].items():
        status = "✓" if r["missing_count"] == 0 else "✗"
        print(f"  {status} {snap}: {r['total_files']} Dateien, "
              f"{r['unique_shas']} unique SHAs, "
              f"{r['missing_count']} fehlen im Pool")
        if r["missing_count"] > 0:
            issues += r["missing_count"]
            for s in r["missing_from_pool"][:5]:
                print(f"    [FEHLT] sha={s[:16]}...")

    print(f"  Manifest-SHA-Union: {res_a['manifest_sha_union']} | "
          f"Pool: {res_a['pool_sha_count']}")

    if res_a["gc_candidates"] > 0:
        print(f"  [GC-Hinweis] {res_a['gc_candidates']} Pool-Objekte "
              f"nicht in Manifesten (GC-Kandidaten)")
    if res_a["index_not_in_pool_count"] > 0:
        print(f"  [FEHLER] Index referenziert {res_a['index_not_in_pool_count']} "
              f"SHAs die im Pool FEHLEN")
        issues += res_a["index_not_in_pool_count"]
    if res_a["pool_not_in_index_count"] > 0:
        print(f"  [warn] {res_a['pool_not_in_index_count']} Pool-Objekte "
              f"nicht im Index (Waise, GC bereinigt)")
    print(f"  ({time.time()-t_a:.2f}s)")
    print()

    # ---- Check B: Stubs vs Index vs Pool ----
    print("=== B) Stubs vs Index vs Pool ===")
    t_b = time.time()
    res_b = check_stubs_vs_index(pool_refs, stub_paths, snaps_root, manifests)

    if res_b["manifest_missing_total"] == 0:
        print(f"  ✓ Manifest-Stub-Check: alle Stubs vorhanden "
              f"({res_b['actual_stubs']} remote)")
    else:
        issues += res_b["manifest_missing_total"]
        print(f"  ✗ Manifest-Stub-Check: {res_b['manifest_missing_total']} Stubs fehlen")
        for snap, missing in res_b["manifest_missing_stubs"].items():
            print(f"    {snap}: {len(missing)} fehlend (z.B. {missing[0]})")

    if res_b["missing_from_index"] > 0:
        print(f"  [warn] {res_b['missing_from_index']} Index-Stubs remote nicht gefunden")
    if res_b["extra_not_in_index"] > 0:
        print(f"  [info] {res_b['extra_not_in_index']} Stubs remote ohne Index-Eintrag")
    print(f"  ({time.time()-t_b:.2f}s)")
    print()

    # ---- Check C: Stub-Sample (optional) ----
    if args.stub_sample > 0:
        print(f"=== C) Stub-Sample ({args.stub_sample} Stubs, Inhalt lesen) ===")
        t_c = time.time()
        res_c = check_stub_sample(cfg, pool_refs, manifests, snaps_root,
                                  args.stub_sample)
        if not res_c["errors"]:
            print(f"  ✓ {res_c['ok']}/{res_c['sampled']} Stubs: "
                  f"sha256 + fileid korrekt")
        else:
            issues += len(res_c["errors"])
            print(f"  ✗ {res_c['ok']}/{res_c['sampled']} OK, "
                  f"{len(res_c['errors'])} Fehler:")
            for e in res_c["errors"][:5]:
                print(f"    {e}")
        print(f"  ({time.time()-t_c:.2f}s)")
        print()

    # ---- Ergebnis ----
    dt_total = time.time() - t0
    print("=" * 60)
    if issues == 0:
        print(f"✓ ALLE CHECKS OK — Backup vollstaendig integr ({dt_total:.1f}s)")
    else:
        print(f"✗ {issues} PROBLEM(E) gefunden ({dt_total:.1f}s)")
    print("=" * 60)
    return 0 if issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
