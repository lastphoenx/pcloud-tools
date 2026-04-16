#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_quick_delta.py — Schnelle Delta-Erkennung für pCloud-Backups.

Modus: tamper-detect
  Vergleicht den Live-Zustand auf pCloud (via listfolder) mit dem
  content_index.json, um unautorisierte Änderungen zu erkennen.

Checks:
  1. Anchor-Dateien: fileid + hash + size Abgleich (Index vs. Live)
  2. Fehlende Anchors: Im Index referenziert, aber auf pCloud nicht (mehr) vorhanden
  3. Unbekannte Dateien: Auf pCloud vorhanden, aber nicht im Index
  4. pcloud_hash-Lücken: Index-Nodes ohne pcloud_hash (z.B. resumed Files)

Für Nodes ohne pcloud_hash kann optional ein SHA256-Nachcheck erfolgen
(--backfill-check), der den tatsächlichen Remote-SHA256 via checksumfile
ermittelt und mit dem Index-Key abgleicht.

Benötigt: pcloud_bin_lib.py im selben Verzeichnis oder PYTHONPATH.
"""

from __future__ import annotations
import os, sys, json, argparse, time
from typing import Dict, List, Any, Optional, Set, Tuple

try:
    import pcloud_bin_lib as pc
except Exception:
    print("Fehler: pcloud_bin_lib nicht gefunden", file=sys.stderr)
    sys.exit(2)


# ============================================================================
# Helpers
# ============================================================================

def _flatten_tree(metadata: dict, parent_path: str = "", is_root: bool = True) -> List[dict]:
    """
    Flattens a recursive listfolder response into a list of file dicts.
    Each dict gets an extra '_full_path' key with the reconstructed remote path.
    
    Marker-Dateien (.upload_started, .upload_complete, etc.) werden NICHT zurückgegeben.
    """
    results: List[dict] = []
    name = metadata.get("name", "")
    
    # Marker-Dateien ignorieren
    MARKER_FILES = {".upload_started", ".upload_complete", ".upload_aborted", ".upload_incomplete"}
    if name in MARKER_FILES:
        return []  # Überspringe Marker-Dateien

    if is_root:
        current_path = parent_path
    else:
        current_path = f"{parent_path}/{name}" if parent_path else name

    if metadata.get("isfolder"):
        for child in metadata.get("contents", []) or []:
            results.extend(_flatten_tree(child, current_path, is_root=False))
    else:
        full = f"{current_path}/{name}" if is_root else current_path
        metadata["_full_path"] = full.replace("//", "/")
        results.append(metadata)

    return results


def _load_index(cfg: dict, snaps_root: str) -> dict:
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = json.loads(txt or '{"version":1,"items":{}}')
        if not isinstance(j, dict):
            j = {"version": 1, "items": {}}
    except Exception as e:
        print(f"[ERROR] Index nicht ladbar: {e}", file=sys.stderr)
        sys.exit(2)
    if "items" not in j or not isinstance(j["items"], dict):
        j["items"] = {}
    return j


# ============================================================================
# Phase 1: Recursive listfolder → flat file map
# ============================================================================

def fetch_remote_tree(cfg: dict, snaps_root: str) -> Tuple[Dict[int, dict], Dict[str, dict]]:
    """
    Holt den kompletten Dateibaum unter snaps_root via einem einzigen
    rekursiven listfolder-Call.

    Returns:
      by_fileid: {fileid: file_metadata_dict}    (für fileid-basierte Lookups)
      by_path:   {full_remote_path: metadata}     (für pfad-basierte Lookups)

    Stubs (.meta.json) werden separat getaggt aber mitgeführt.
    """
    print(f"[fetch] Lade Remote-Baum: {snaps_root} (recursive)...")
    t0 = time.time()

    top = pc.call_with_backoff(
        pc.listfolder, cfg, path=snaps_root, recursive=True, nofiles=False, showpath=False
    ) or {}
    metadata = top.get("metadata") or {}

    # Flatten – Pfade relativ zu snaps_root rekonstruieren
    flat = _flatten_tree(metadata, parent_path=snaps_root, is_root=True)

    by_fileid: Dict[int, dict] = {}
    by_path: Dict[str, dict] = {}

    for f in flat:
        fid = f.get("fileid")
        fp = f.get("_full_path", "")
        if fid is not None:
            by_fileid[int(fid)] = f
        if fp:
            by_path[fp] = f

    dt = time.time() - t0
    n_files = len(by_fileid)
    n_stubs = sum(1 for p in by_path if p.endswith(".meta.json"))
    n_real = n_files - n_stubs
    print(f"[fetch] {n_files} Dateien geladen ({n_real} real, {n_stubs} stubs) in {dt:.1f}s")

    return by_fileid, by_path


# ============================================================================
# Phase 2: Index vs. Remote vergleichen
# ============================================================================

def compare_index_vs_remote(
    index: dict,
    by_fileid: Dict[int, dict],
    by_path: Dict[str, dict],
) -> dict:
    """
    Vergleicht content_index.json gegen den Live-Baum.

    Prüft pro Index-Node (Key = sha256):
      - Anchor existiert auf pCloud? (fileid oder path)
      - fileid stimmt überein?
      - pcloud_hash stimmt überein? (falls im Index vorhanden)
      - size stimmt überein?
      - pcloud_hash fehlt im Index? (Lücke = potentiell resumed)

    Returns: Report-Dict mit allen Kategorien.
    """
    items = index.get("items", {})
    
    # Marker-Dateien ignorieren (Upload-Status-Tracking)
    MARKER_FILES = {".upload_started", ".upload_complete", ".upload_aborted", ".upload_incomplete"}

    # Ergebnis-Listen
    ok: List[str] = []
    missing_anchors: List[dict] = []
    fileid_mismatch: List[dict] = []
    hash_mismatch: List[dict] = []
    size_mismatch: List[dict] = []
    hash_missing_in_index: List[dict] = []

    # Set aller remote fileids für spätere "unbekannte Dateien"-Erkennung
    index_fileids: Set[int] = set()

    checked = 0
    for sha, node in items.items():
        if not isinstance(node, dict):
            continue

        anchor_path = node.get("anchor_path")
        index_fid = node.get("fileid")
        index_hash = node.get("pcloud_hash")
        index_size = None  # size nicht direkt im Node, aber in holders/stubs

        if not anchor_path:
            continue

        checked += 1

        # Track fileids from index
        if index_fid:
            index_fileids.add(int(index_fid))

        # --- Lookup: erst via fileid, dann via path ---
        remote_md = None
        if index_fid and int(index_fid) in by_fileid:
            remote_md = by_fileid[int(index_fid)]
        elif anchor_path in by_path:
            remote_md = by_path[anchor_path]

        if not remote_md:
            missing_anchors.append({
                "sha256": sha,
                "anchor_path": anchor_path,
                "fileid": index_fid,
                "holders": len(node.get("holders", [])),
            })
            continue

        # --- fileid check ---
        remote_fid = remote_md.get("fileid")
        if index_fid and remote_fid and int(index_fid) != int(remote_fid):
            fileid_mismatch.append({
                "sha256": sha,
                "anchor_path": anchor_path,
                "index_fileid": index_fid,
                "remote_fileid": remote_fid,
            })

        # --- pcloud_hash check ---
        remote_hash = remote_md.get("hash")
        if index_hash and remote_hash:
            if str(index_hash) != str(remote_hash):
                hash_mismatch.append({
                    "sha256": sha,
                    "anchor_path": anchor_path,
                    "index_hash": index_hash,
                    "remote_hash": remote_hash,
                })
        elif not index_hash and remote_hash:
            # Lücke: pcloud_hash fehlt im Index (resumed / nicht nachgezogen)
            hash_missing_in_index.append({
                "sha256": sha,
                "anchor_path": anchor_path,
                "fileid": index_fid,
                "remote_hash": remote_hash,
            })

        # --- size check ---
        remote_size = remote_md.get("size")
        # Versuche size aus holders oder direkt
        node_size = node.get("size")
        if node_size is not None and remote_size is not None:
            if int(node_size) != int(remote_size):
                size_mismatch.append({
                    "sha256": sha,
                    "anchor_path": anchor_path,
                    "index_size": node_size,
                    "remote_size": remote_size,
                })

        ok.append(sha)

    return {
        "checked": checked,
        "ok": len(ok),
        "missing_anchors": missing_anchors,
        "fileid_mismatch": fileid_mismatch,
        "hash_mismatch": hash_mismatch,
        "size_mismatch": size_mismatch,
        "hash_missing_in_index": hash_missing_in_index,
        "index_fileids": index_fileids,
    }


# ============================================================================
# Phase 3: Unbekannte Dateien auf pCloud (nicht im Index)
# ============================================================================

def find_unknown_files(
    by_fileid: Dict[int, dict],
    by_path: Dict[str, dict],
    index_fileids: Set[int],
    snaps_root: str,
) -> List[dict]:
    """
    Findet echte Dateien (keine .meta.json Stubs und kein content_index.json)
    auf pCloud, die in keinem Index-Node als Anchor referenziert werden.
    
    Ignoriert:
    - Stubs (.meta.json)
    - content_index.json
    - _index/ Ordner komplett
    - Marker-Dateien (.upload_started, .upload_complete, etc.)
    """
    unknown: List[dict] = []
    idx_path = f"{snaps_root.rstrip('/')}/_index/content_index.json"
    
    # Upload-Marker-Dateien (diese gehören zum Upload-Tracking, nicht zu Backups)
    MARKER_FILES = {".upload_started", ".upload_complete", ".upload_aborted", ".upload_incomplete"}

    for fid, md in by_fileid.items():
        fp = md.get("_full_path", "")
        fname = fp.split("/")[-1] if "/" in fp else fp

        # Stubs und Index selbst überspringen
        if fp.endswith(".meta.json"):
            continue
        if fp == idx_path:
            continue
        # _index-Ordner generell überspringen (Marker files etc.)
        if "/_index/" in fp:
            continue
        # Upload-Marker ignorieren
        if fname in MARKER_FILES:
            continue

        if fid not in index_fileids:
            unknown.append({
                "fileid": fid,
                "path": fp,
                "size": md.get("size"),
                "hash": md.get("hash"),
                "modified": md.get("modified"),
            })

    return unknown


# ============================================================================
# Phase 4 (optional): SHA256-Backfill-Check für Lücken
# ============================================================================

def backfill_sha256_check(
    cfg: dict,
    hash_missing: List[dict],
    *,
    sample_size: int = 0,
) -> Tuple[List[dict], List[dict]]:
    """
    Für Index-Nodes ohne pcloud_hash: Holt den tatsächlichen SHA256 via
    checksumfile und vergleicht mit dem Index-Key (sha256).

    Returns:
      sha_ok:       Nodes wo remote SHA256 == Index SHA256 (nur hash im Index fehlend)
      sha_mismatch: Nodes wo remote SHA256 != Index SHA256 (echtes Problem)
    """
    if not hash_missing:
        return [], []

    items = hash_missing
    if sample_size and sample_size < len(items):
        import random
        items = random.sample(items, sample_size)

    print(f"[backfill] Prüfe {len(items)} Nodes ohne pcloud_hash via SHA256...")

    sha_ok: List[dict] = []
    sha_mismatch: List[dict] = []

    for i, entry in enumerate(items, 1):
        fid = entry.get("fileid")
        sha_expected = entry.get("sha256", "").lower()
        anchor = entry.get("anchor_path", "")

        if not fid and not anchor:
            continue

        try:
            if fid:
                cs = pc.checksumfile(cfg, fileid=int(fid))
            else:
                cs = pc.checksumfile(cfg, path=anchor)

            remote_sha = (cs.get("sha256") or "").lower()
            if not remote_sha:
                continue

            if remote_sha == sha_expected:
                sha_ok.append({**entry, "remote_sha256": remote_sha, "status": "ok"})
            else:
                sha_mismatch.append({
                    **entry,
                    "remote_sha256": remote_sha,
                    "expected_sha256": sha_expected,
                    "status": "MISMATCH",
                })
        except Exception as e:
            print(f"  [warn] checksumfile fehlgeschlagen für {anchor or fid}: {e}")

        if i % 25 == 0:
            print(f"  [progress] {i}/{len(items)}...")

    return sha_ok, sha_mismatch


# ============================================================================
# Reporting
# ============================================================================

def print_report(report: dict, unknown_files: List[dict], backfill_ok: List[dict], backfill_bad: List[dict]) -> int:
    """Gibt den Report auf stdout aus. Returniert die Anzahl issues."""

    total_issues = 0

    print("\n" + "=" * 70)
    print("=== ERGEBNIS: tamper-detect ===")
    print("=" * 70)

    # OK
    print(f"\n  Geprüfte Index-Nodes:    {report['checked']}")
    print(f"  Davon OK:                {report['ok']}")

    # Missing anchors
    missing = report["missing_anchors"]
    print(f"\n  Fehlende Anchors:        {len(missing)}")
    total_issues += len(missing)
    for m in missing[:10]:
        print(f"    [MISSING] {m['anchor_path']}  (fid={m['fileid']}, holders={m['holders']})")
    if len(missing) > 10:
        print(f"    ... und {len(missing)-10} weitere")

    # FileID mismatch
    fid_mm = report["fileid_mismatch"]
    print(f"\n  FileID-Abweichungen:     {len(fid_mm)}")
    total_issues += len(fid_mm)
    for m in fid_mm[:10]:
        print(f"    [FID-DELTA] {m['anchor_path']}  index={m['index_fileid']} remote={m['remote_fileid']}")

    # Hash mismatch
    h_mm = report["hash_mismatch"]
    print(f"\n  Hash-Abweichungen:       {len(h_mm)}")
    total_issues += len(h_mm)
    for m in h_mm[:10]:
        print(f"    [HASH-DELTA] {m['anchor_path']}  index={m['index_hash']} remote={m['remote_hash']}")

    # Size mismatch
    s_mm = report["size_mismatch"]
    print(f"\n  Size-Abweichungen:       {len(s_mm)}")
    total_issues += len(s_mm)
    for m in s_mm[:10]:
        print(f"    [SIZE-DELTA] {m['anchor_path']}  index={m['index_size']} remote={m['remote_size']}")

    # Hash missing (Lücken)
    h_miss = report["hash_missing_in_index"]
    print(f"\n  pcloud_hash Lücken:      {len(h_miss)}")
    if h_miss:
        print(f"    (Index-Nodes ohne pcloud_hash — vermutlich resumed Files)")
        for m in h_miss[:5]:
            print(f"    [LÜCKE] {m['anchor_path']}  sha256={m['sha256'][:16]}...")

    # Backfill results
    if backfill_ok or backfill_bad:
        print(f"\n  SHA256-Backfill-Check:")
        print(f"    OK (nur hash im Index fehlend):  {len(backfill_ok)}")
        print(f"    MISMATCH (echtes Problem):       {len(backfill_bad)}")
        total_issues += len(backfill_bad)
        for m in backfill_bad[:10]:
            print(f"    [SHA-MISMATCH] {m['anchor_path']}  expected={m['expected_sha256'][:16]}... got={m['remote_sha256'][:16]}...")

    # Unknown files
    print(f"\n  Unbekannte Dateien:      {len(unknown_files)}")
    if unknown_files:
        total_issues += len(unknown_files)
        for u in unknown_files[:10]:
            print(f"    [UNBEKANNT] {u['path']}  (fid={u['fileid']}, size={u['size']})")
        if len(unknown_files) > 10:
            print(f"    ... und {len(unknown_files)-10} weitere")

    # Summary
    print(f"\n{'=' * 70}")
    if total_issues == 0:
        print("  ✓ KEINE ABWEICHUNGEN — pCloud-Zustand konsistent mit Index")
    else:
        print(f"  ✗ {total_issues} ABWEICHUNG(EN) GEFUNDEN — manuelle Prüfung empfohlen")
    print("=" * 70)

    return total_issues


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="pcloud_quick_delta — Schnelle Tamper-Detection für pCloud-Backups",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  # Standard-Check (schnell, nur listfolder + Index-Vergleich):
  python pcloud_quick_delta.py --dest-root /backup-nas

  # Mit SHA256-Backfill-Check für Lücken (langsamer, aber gründlicher):
  python pcloud_quick_delta.py --dest-root /backup-nas --backfill-check

  # Backfill nur Stichprobe von 50 Nodes:
  python pcloud_quick_delta.py --dest-root /backup-nas --backfill-check --backfill-sample 50

  # Report als JSON speichern:
  python pcloud_quick_delta.py --dest-root /backup-nas --json-out /srv/pcloud-temp/delta-report.json
""")
    ap.add_argument("--dest-root", required=True,
                    help="pCloud-Basispfad (z.B. /backup-nas)")
    ap.add_argument("--env-file",
                    help="Pfad zur .env-Datei (optional)")
    ap.add_argument("--profile",
                    help="pCloud-Profil (optional)")
    ap.add_argument("--backfill-check", action="store_true",
                    help="SHA256-Check für Index-Nodes ohne pcloud_hash (resumed Files)")
    ap.add_argument("--backfill-sample", type=int, default=0,
                    help="Stichprobe für Backfill-Check (0 = alle)")
    ap.add_argument("--json-out",
                    help="Report als JSON in Datei schreiben")

    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file, profile=args.profile)
    dest_root = pc._norm_remote_path(args.dest_root)
    snaps_root = f"{dest_root.rstrip('/')}/_snapshots"

    print(f"=== pcloud_quick_delta — tamper-detect ===")
    print(f"Destination: {snaps_root}")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    t_total = time.time()

    # 1) Index laden
    print("[phase 1] Lade content_index.json...")
    t0 = time.time()
    index = _load_index(cfg, snaps_root)
    n_items = len(index.get("items", {}))
    print(f"[phase 1] Index geladen: {n_items} Nodes ({time.time()-t0:.1f}s)")

    # 2) Remote-Baum einlesen (ein API-Call)
    print()
    by_fileid, by_path = fetch_remote_tree(cfg, snaps_root)

    # 3) Vergleich Index vs. Remote
    print()
    print("[phase 3] Vergleiche Index vs. Remote...")
    t0 = time.time()
    report = compare_index_vs_remote(index, by_fileid, by_path)
    print(f"[phase 3] Vergleich abgeschlossen ({time.time()-t0:.1f}s)")

    # 4) Unbekannte Dateien
    print()
    print("[phase 4] Suche unbekannte Dateien...")
    unknown = find_unknown_files(by_fileid, by_path, report["index_fileids"], snaps_root)
    print(f"[phase 4] {len(unknown)} unbekannte Dateien gefunden")

    # 5) Optional: SHA256-Backfill-Check
    backfill_ok: List[dict] = []
    backfill_bad: List[dict] = []
    if args.backfill_check and report["hash_missing_in_index"]:
        print()
        backfill_ok, backfill_bad = backfill_sha256_check(
            cfg,
            report["hash_missing_in_index"],
            sample_size=args.backfill_sample,
        )
        print(f"[backfill] OK={len(backfill_ok)}, Mismatch={len(backfill_bad)}")

    # 6) Report
    issues = print_report(report, unknown, backfill_ok, backfill_bad)

    dt_total = time.time() - t_total
    print(f"\n[timing] Gesamtlaufzeit: {dt_total:.1f}s")

    # 7) JSON-Output
    if args.json_out:
        json_report = {
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dest_root": dest_root,
            "snaps_root": snaps_root,
            "index_nodes": n_items,
            "remote_files": len(by_fileid),
            "duration_sec": round(dt_total, 2),
            "issues": issues,
            "missing_anchors": report["missing_anchors"],
            "fileid_mismatch": report["fileid_mismatch"],
            "hash_mismatch": report["hash_mismatch"],
            "size_mismatch": report["size_mismatch"],
            "hash_missing_in_index": len(report["hash_missing_in_index"]),
            "hash_missing_details": report["hash_missing_in_index"][:50],
            "unknown_files": unknown[:100],
            "backfill_ok": len(backfill_ok),
            "backfill_mismatch": backfill_bad,
        }
        out_path = args.json_out
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(json_report, f, indent=2, ensure_ascii=False)
        print(f"[output] JSON-Report geschrieben: {out_path}")

    sys.exit(1 if issues > 0 else 0)


if __name__ == "__main__":
    main()
