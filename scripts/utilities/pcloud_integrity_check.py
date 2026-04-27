#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_integrity_check.py — Vollständiger Integritäts-Check (v3)

Prüft alle Konsistenz-Ebenen:
1. Index → pCloud (Anchors, FileIDs)
2. Index → Checksummen (SHA256, Stichprobe)
3. Holders → Snapshots (Waisen)
4. Stubs → Index (Verweise)                [separate]
5. Stubs → pCloud (Anchors, FileIDs, SHA)  [separate]
6. Anchor-Zeitlinie (Holder-Konsistenz)
7. Stubs (kombiniert, 1 Pass)              [combined]

Per --stubs-mode wählbar: separate | combined | both
"""

import os, sys, json, argparse, time, random
from typing import Dict, List, Any, Tuple

try:
    import pcloud_bin_lib as pc
except Exception:
    print("Fehler: pcloud_bin_lib nicht gefunden", file=sys.stderr)
    sys.exit(2)

# ============================================================================
# Check 1: Index Anchors & FileIDs
# ============================================================================

def check_index_anchors(cfg: Dict, index: Dict) -> Dict[str, Any]:
    """Prüft ob alle Anchors im Index existieren und FileID stimmt"""
    print("[check] Index Anchors & FileIDs...")
    items = (index or {}).get("items", {}) or {}
    print(f"  → {len(items)} Content-Nodes im Index")

    broken_anchors: List[dict] = []
    broken_fileids: List[dict] = []
    checked = 0

    for sha, node in items.items():
        ap = (node or {}).get("anchor_path")
        index_fid = (node or {}).get("fileid")

        if not ap:
            continue

        checked += 1
        try:
            md = pc.stat_file(cfg, path=ap, with_checksum=False, enrich_path=False) or {}
            real_fid = md.get("fileid")
            if index_fid and real_fid and int(index_fid) != int(real_fid):
                broken_fileids.append({
                    "sha256": sha, "anchor_path": ap,
                    "index_fileid": index_fid, "real_fileid": real_fid
                })
        except Exception as e:
            msg = str(e).lower()
            if "2009" in msg or "not found" in msg or "does not exist" in msg:
                broken_anchors.append({"sha256": sha, "anchor_path": ap, "fileid": index_fid, "reason": str(e)})
            else:
                print(f"  [warn] Temporärer Fehler bei {ap}: {e}")

        if checked % 50 == 0:
            print(f"  [progress] {checked}/{len(items)}...")

    return {
        "checked": checked,
        "broken_anchors": len(broken_anchors),
        "broken_fileids": len(broken_fileids),
        "details_anchors": broken_anchors,
        "details_fileids": broken_fileids,
    }

# ============================================================================
# Check 2: Index Checksummen (Stichprobe)
# ============================================================================

def check_index_checksums(cfg: Dict, index: Dict, sample_size: int) -> Dict[str, Any]:
    """Prüft SHA256-Checksummen (Stichprobe)"""
    print(f"[check] Index Checksummen (Sample: {sample_size})...")
    items = (index or {}).get("items", {}) or {}
    if not items:
        return {"checked": 0, "mismatches": 0, "details": []}

    sample_items = random.sample(list(items.items()), min(sample_size, len(items)))
    mismatches: List[dict] = []
    checked = 0

    for sha, node in sample_items:
        ap = (node or {}).get("anchor_path")
        if not ap:
            continue
        checked += 1
        try:
            cs = pc.checksumfile(cfg, path=ap) or {}
            remote_sha = (cs.get("sha256") or "").lower()
            if remote_sha and remote_sha != sha.lower():
                mismatches.append({"sha256_index": sha, "sha256_remote": remote_sha, "anchor_path": ap})
        except Exception as e:
            print(f"  [warn] Checksum-Check fehlgeschlagen für {ap}: {e}")

        if checked % 10 == 0:
            print(f"  [progress] {checked}/{len(sample_items)}...")

    return {"checked": checked, "mismatches": len(mismatches), "details": mismatches}

# ============================================================================
# Check 3: Orphaned Holders
# ============================================================================

def check_orphaned_holders(cfg: Dict, index: Dict, snapshots_root: str) -> Dict[str, Any]:
    """Prüft ob Holder auf nicht-existente Snapshots verweisen"""
    print("[check] Orphaned Holders...")
    try:
        top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False) or {}
        contents = (top.get("metadata", {}) or {}).get("contents", []) or []
        remote_snaps = {c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index"}
    except Exception as e:
        return {"error": f"Konnte Snapshots nicht listen: {e}"}

    print(f"  → {len(remote_snaps)} Snapshots gefunden")
    items = (index or {}).get("items", {}) or {}
    orphaned: List[dict] = []

    for sha, node in items.items():
        for h in (node or {}).get("holders", []) or []:
            snap = h.get("snapshot")
            if snap not in remote_snaps:
                orphaned.append({"sha256": sha, "holder_snapshot": snap, "holder_relpath": h.get("relpath")})

    return {"remote_snapshots": len(remote_snaps), "orphaned_holders": len(orphaned), "details": orphaned}

# ============================================================================
# Check 4: Stubs → Index (separat)
# ============================================================================

def check_stubs_to_index(cfg: Dict, index: Dict, snapshots_root: str, sample_size: int) -> Dict[str, Any]:
    """Prüft ob Stubs einen Index-Eintrag haben (alle Snapshots)"""
    print(f"[check] Stubs → Index (Sample: {sample_size} pro Snapshot)...")
    try:
        top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False) or {}
        contents = (top.get("metadata", {}) or {}).get("contents", []) or []
        remote_snaps = [c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index"]
    except Exception as e:
        return {"error": f"Konnte Snapshots nicht listen: {e}"}

    if not remote_snaps:
        return {"checked": 0, "missing_in_index": 0, "details": []}

    print(f"  → Prüfe {len(remote_snaps)} Snapshots...")
    items = (index or {}).get("items", {}) or {}
    missing_in_index: List[dict] = []
    checked = 0

    for snap in remote_snaps:
        snap_path = f"{snapshots_root}/{snap}"
        try:
            top = pc.listfolder(cfg, path=snap_path, recursive=True, nofiles=False, showpath=False) or {}
        except Exception as e:
            print(f"  [warn] Konnte Snapshot {snap} nicht listen: {e}")
            continue

        def _find_stubs(md, parent_path="", is_root=True):
            if md.get("isfolder"):
                name = md.get("name", "")
                new_parent = "" if is_root else (f"{parent_path}/{name}" if parent_path else name)
                for c in md.get("contents", []) or []:
                    yield from _find_stubs(c, new_parent, is_root=False)
            else:
                if md.get("name", "").endswith(".meta.json"):
                    name = md.get("name", "")
                    full_path = f"{snap_path}/{parent_path}/{name}" if parent_path else f"{snap_path}/{name}"
                    md["_reconstructed_path"] = full_path.replace("//", "/")
                    yield md

        stubs = list(_find_stubs(top.get("metadata", {}) or {}, is_root=True))
        if stubs:
            print(f"    - {snap}: {len(stubs)} Stubs gefunden")

        sample = stubs if len(stubs) <= sample_size else random.sample(stubs, sample_size)
        for stub_md in sample:
            checked += 1
            stub_path = stub_md.get("_reconstructed_path")
            if not stub_path:
                print(f"  [warn] Stub ohne Pfad: {stub_md.get('name', 'unknown')}")
                continue
            try:
                txt = pc.get_textfile(cfg, path=stub_path)
                payload = json.loads(txt or "{}")
                sha = (payload.get("sha256") or "").lower()
                if not sha:
                    continue
                if sha not in items:
                    missing_in_index.append({"stub_path": stub_path, "sha256": sha, "anchor_path": payload.get("anchor_path")})
            except Exception as e:
                print(f"  [warn] Stub nicht lesbar: {stub_path}: {e}")

    return {"checked": checked, "missing_in_index": len(missing_in_index), "details": missing_in_index}

# ============================================================================
# Check 5: Stubs → Anchors & FileIDs (separat, mit Hash-Verifikation)
# ============================================================================

def check_stubs_to_anchors(cfg: Dict, snapshots_root: str, sample_size: int) -> Dict[str, Any]:
    """
    Prüft Stubs gegen reale pCloud-Dateien (FileID-first + optionale Hash-Verifikation).
    Hash-Verifikation: jede 10. geprüfte Datei.
    """
    print(f"[check] Stubs → Anchors & FileIDs (Sample: {sample_size} pro Snapshot, mit Hash-Verifikation)...")

    try:
        top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False) or {}
        contents = (top.get("metadata", {}) or {}).get("contents", []) or []
        remote_snaps = [c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index"]
    except Exception as e:
        return {"error": f"Konnte Snapshots nicht listen: {e}"}

    if not remote_snaps:
        return {"checked": 0, "hash_checked": 0, "broken_anchors": 0, "broken_fileids": 0, "moved_anchors": 0, "hash_mismatches": 0,
                "details_anchors": [], "details_fileids": [], "details_moved": [], "details_hash": []}

    print(f"  → Prüfe {len(remote_snaps)} Snapshots...")

    broken_anchors: List[dict] = []
    broken_fileids: List[dict] = []
    moved_anchors: List[dict] = []
    hash_mismatches: List[dict] = []
    checked = 0
    hash_checked = 0

    for snap in remote_snaps:
        snap_path = f"{snapshots_root}/{snap}"
        try:
            top = pc.listfolder(cfg, path=snap_path, recursive=True, nofiles=False, showpath=False) or {}
        except Exception as e:
            print(f"  [warn] Konnte Snapshot {snap} nicht listen: {e}")
            continue

        def _find_stubs(md, parent_path="", is_root=True):
            if md.get("isfolder"):
                name = md.get("name", "")
                new_parent = "" if is_root else (f"{parent_path}/{name}" if parent_path else name)
                for c in md.get("contents", []) or []:
                    yield from _find_stubs(c, new_parent, is_root=False)
            else:
                if md.get("name", "").endswith(".meta.json"):
                    name = md.get("name", "")
                    full_path = f"{snap_path}/{parent_path}/{name}" if parent_path else f"{snap_path}/{name}"
                    md["_reconstructed_path"] = full_path.replace("//", "/")
                    yield md

        stubs = list(_find_stubs(top.get("metadata", {}) or {}, is_root=True))
        if stubs:
            print(f"    - {snap}: {len(stubs)} Stubs gefunden")

        sample = stubs if len(stubs) <= sample_size else random.sample(stubs, sample_size)

        for stub_md in sample:
            checked += 1
            stub_path = stub_md.get("_reconstructed_path")
            if not stub_path:
                print(f"  [warn] Stub ohne Pfad: {stub_md.get('name', 'unknown')}")
                continue

            try:
                txt = pc.get_textfile(cfg, path=stub_path)
                payload = json.loads(txt or "{}")
                anchor_path = payload.get("anchor_path")
                stub_fileid = payload.get("fileid")
                stub_sha = (payload.get("sha256") or "").lower()
                if not anchor_path:
                    continue

                if stub_fileid:
                    try:
                        md_fid = pc.stat_file(cfg, fileid=int(stub_fileid), with_checksum=False, enrich_path=True) or {}
                        real_path = md_fid.get("path")

                        # jede 10. → Hash prüfen
                        if (checked % 10 == 0) and stub_sha:
                            hash_checked += 1
                            try:
                                cs = pc.checksumfile(cfg, fileid=int(stub_fileid)) or {}
                                real_sha = (cs.get("sha256") or "").lower()
                                if real_sha and real_sha != stub_sha:
                                    hash_mismatches.append({
                                        "stub_path": stub_path, "fileid": stub_fileid,
                                        "anchor_path": anchor_path, "stub_sha256": stub_sha, "real_sha256": real_sha
                                    })
                                    print(f"  [ERROR] Hash-Mismatch: {stub_path}")
                            except Exception as e:
                                print(f"  [warn] Checksum-Fehler bei FileID {stub_fileid}: {e}")

                        if real_path and real_path != anchor_path:
                            moved_anchors.append({
                                "stub_path": stub_path, "anchor_path": anchor_path,
                                "actual_path": real_path, "fileid": stub_fileid
                            })
                            print(f"  [info] Anchor verschoben: {stub_path}\n         Alt: {anchor_path}\n         Neu: {real_path}")

                    except Exception as e:
                        msg = str(e).lower()
                        if "2009" in msg or "not found" in msg:
                            broken_fileids.append({"stub_path": stub_path, "anchor_path": anchor_path, "stub_fileid": stub_fileid})
                        else:
                            print(f"  [warn] Temporärer Fehler bei FileID {stub_fileid}: {e}")

                else:
                    print(f"  [warn] Stub ohne FileID: {stub_path}")
                    try:
                        md_path = pc.stat_file(cfg, path=anchor_path, with_checksum=False, enrich_path=False) or {}
                        broken_fileids.append({
                            "stub_path": stub_path, "anchor_path": anchor_path,
                            "stub_fileid": None, "actual_fileid": md_path.get("fileid")
                        })
                    except Exception as e:
                        msg = str(e).lower()
                        if "2009" in msg or "not found" in msg:
                            broken_anchors.append({"stub_path": stub_path, "anchor_path": anchor_path})
                        else:
                            print(f"  [warn] Temporärer Fehler: {e}")

            except Exception as e:
                print(f"  [warn] Stub nicht lesbar: {stub_path}: {e}")

    return {
        "checked": checked,
        "hash_checked": hash_checked,
        "broken_anchors": len(broken_anchors),
        "broken_fileids": len(broken_fileids),
        "moved_anchors": len(moved_anchors),
        "hash_mismatches": len(hash_mismatches),
        "details_anchors": broken_anchors,
        "details_fileids": broken_fileids,
        "details_moved": moved_anchors,
        "details_hash": hash_mismatches,
    }

# ============================================================================
# Check 7: Kombiniert (1 Pass) – Stub → Index & Anchor/FileID & optional SHA
# ============================================================================

def check_stubs_combined(cfg, snaps_root: str, index_obj: dict, *,
                         sample_per_snapshot: int = 100,
                         level: str = "FILEID",
                         sha_prob: float = 0.0,
                         quiet: bool = False) -> Dict[str, Any]:
    """
    1-Pass je Snapshot:
      - Stub → Index (Existenz)
      - Stub → Anchor/FileID
      - optional SHA-Verifikation (level=SHA oder via sha_prob)
    """
    import json as _json

    def warn(msg: str):
        if not quiet:
            print(msg, file=sys.stderr)

    items = (index_obj or {}).get("items", {}) or {}
    index_shas = set(map(str.lower, items.keys()))

    c_idx_missing = 0
    c_checked = 0
    c_total = 0
    c_sampled = 0
    c_moved = 0
    c_fid_not_found = 0
    c_sha_mismatch = 0

    moved_examples: List[Tuple[str, str, str]] = []
    notfound_examples: List[Tuple[str, str]] = []
    sha_mismatch_examples: List[Tuple[str, str, str]] = []

    def _walk_files(root_path: str):
        stack = [root_path]
        seen = set()
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            try:
                js = pc.listfolder(cfg, path=p, recursive=False, nofiles=False, showpath=False) or {}
            except Exception:
                continue
            meta = js.get("metadata") or {}
            for c in meta.get("contents", []) or []:
                nm = c.get("name", "")
                if not nm:
                    continue
                if c.get("isfolder"):
                    if nm != ".":
                        stack.append(f"{p}/{nm}")
                else:
                    yield f"{p}/{nm}"

    # Snapshots auflisten
    try:
        top = pc.listfolder(cfg, path=snaps_root, recursive=False, nofiles=True, showpath=False) or {}
        contents = (top.get("metadata", {}) or {}).get("contents", []) or []
        remote_snaps = [c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index"]
    except Exception as e:
        return {"error": f"Konnte Snapshots nicht listen: {e}"}

    for snap in remote_snaps:
        snap_root = f"{snaps_root}/{snap}"
        try:
            all_files = list(_walk_files(snap_root))
        except Exception:
            all_files = []
        stubs = [f for f in all_files if f.endswith(".meta.json")]
        if not stubs:
            continue

        c_total += len(stubs)
        sample = stubs if len(stubs) <= sample_per_snapshot else random.sample(stubs, sample_per_snapshot)
        c_sampled += len(sample)

        for stub_path in sample:
            try:
                txt = pc.get_textfile(cfg, path=stub_path)
                payload = _json.loads(txt or "{}")
            except Exception:
                continue

            stub_sha = (payload.get("sha256") or "").lower()
            anchor_path = payload.get("anchor_path") or ""
            fid = payload.get("fileid")

            # Stub → Index
            in_index = False
            if stub_sha and stub_sha in index_shas:
                in_index = True
            else:
                # Fallback via anchor_path
                for key_sha, node in items.items():
                    if (node or {}).get("anchor_path") == anchor_path:
                        in_index = True
                        break
            if not in_index:
                c_idx_missing += 1

            # Stub → Anchor/FileID (+ optional SHA)
            try:
                if fid:
                    md = pc.stat_file(cfg, fileid=int(fid), with_checksum=False, enrich_path=True) or {}
                else:
                    md = pc.stat_file(cfg, path=anchor_path, with_checksum=False, enrich_path=True) or {}
                real_path = md.get("path") or ""
                if anchor_path and real_path and real_path != anchor_path:
                    c_moved += 1
                    if len(moved_examples) < 5:
                        moved_examples.append((stub_path, anchor_path, real_path))

                do_sha = (level.upper() == "SHA") or (sha_prob > 0.0 and random.random() < sha_prob)
                if do_sha and fid and stub_sha:
                    meta = pc.checksumfile(cfg, fileid=int(fid)) or {}
                    real_sha = (meta.get("sha256") or meta.get("hash") or "").lower()
                    if real_sha and real_sha != stub_sha:
                        c_sha_mismatch += 1
                        if len(sha_mismatch_examples) < 5:
                            sha_mismatch_examples.append((stub_path, stub_sha[:8], real_sha[:8]))

            except Exception as e:
                msg = str(e)
                if "2009" in msg or "not found" in msg.lower():
                    c_fid_not_found += 1
                    if len(notfound_examples) < 5:
                        notfound_examples.append((stub_path, msg))
                # sonst: temporär ignorieren

            c_checked += 1

    print(f"[RESULT] Stubs (combined/1-pass): total={c_total}, sampled={c_sampled}, checked={c_checked}, "
          f"idx_missing={c_idx_missing}, moved={c_moved}, fid_not_found={c_fid_not_found}, sha_mismatch={c_sha_mismatch}")
    for sp, oldp, newp in moved_examples:
        print(f"  [info] Anchor verschoben: {sp}\n         Alt: {oldp}\n         Neu: {newp}")
    for sp, msg in notfound_examples:
        print(f"  [warn] Stub FID nicht auffindbar: {sp}\n        err: {msg}")
    for sp, s8, r8 in sha_mismatch_examples:
        print(f"  [error] Stub SHA mismatch: {sp}\n          stub={s8} real={r8}")

    return {
        "total": c_total,
        "sampled": c_sampled,
        "checked": c_checked,
        "idx_missing": c_idx_missing,
        "moved": c_moved,
        "fid_not_found": c_fid_not_found,
        "sha_mismatch": c_sha_mismatch,
    }

# ============================================================================
# Check 8: Anchor-Zeitlinie
# ============================================================================

def check_anchor_timeline(cfg: Dict, index: Dict) -> Dict[str, Any]:
    """Prüft ob Anchor-Snapshot in Holder-Liste vorkommt"""
    print("[check] Anchor-Zeitlinie...")
    items = (index or {}).get("items", {}) or {}
    violations: List[dict] = []
    checked = 0

    for sha, node in items.items():
        ap = (node or {}).get("anchor_path")
        holders = (node or {}).get("holders", []) or []
        if not ap or not holders:
            continue

        checked += 1
        parts = ap.split("/")
        try:
            snap_idx = parts.index("_snapshots") + 1
            anchor_snap = parts[snap_idx]
        except Exception:
            continue

        holder_snaps = {h.get("snapshot") for h in holders}
        if anchor_snap not in holder_snaps:
            violations.append({
                "sha256": sha,
                "anchor_path": ap,
                "anchor_snapshot": anchor_snap,
                "holder_snapshots": sorted(holder_snaps),
                "reason": "Anchor-Snapshot ist kein Holder",
            })

    return {"checked": checked, "violations": len(violations), "details": violations}

# ============================================================================
# Auto-fix (safe)
# ============================================================================

def autofix_safe(cfg: dict, dest_root: str, plan: dict, *, dry: bool = False, index_obj: dict | None = None) -> dict:
    """
    Führt sichere Korrekturen aus (wenn Plan entsprechende Listen enthält):
      - anchor_missing: promote 1 holder -> anchor (rename/move)
      - stub_missing:   stub .meta.json neu schreiben
      - orphan_stub:    Stub löschen
    index_obj ist optional und wird hier nicht benötigt (nur kompatibel gehalten).
    """
    stats = {"promoted": 0, "stubs_rewritten": 0, "orphans_deleted": 0, "errors": 0}
    snap_root = f"{dest_root.rstrip('/')}/_snapshots"

    def _write_stub(snapshot: str, relpath: str, node: dict, file_item: dict):
        meta_path = f"{snap_root}/{snapshot}/{relpath}.meta.json"
        if dry:
            print(f"[dry] write stub {meta_path}")
            return
        pc.write_json_at_path(cfg, path=meta_path, obj={
            "type": "hardlink",
            "sha256": (file_item.get("sha256") or "").lower(),
            "size":   int(file_item.get("size") or 0),
            "mtime":  float(file_item.get("mtime") or 0.0),
            "inode":  file_item.get("inode") or {},
            "anchor_path": (node or {}).get("anchor_path"),
            "fileid": (node or {}).get("fileid"),
            "snapshot": snapshot,
            "relpath": relpath,
        })

    # 1) fehlende Anchor -> Promotion
    for item in (plan or {}).get("anchor_missing", []) or []:
        holders = item.get("holders") or []
        if not holders:
            continue
        h = holders[0]  # nimm den ersten Holder
        new_anchor = f"{snap_root}/{h['snapshot']}/{h['relpath']}"
        old_anchor = item["anchor_path"]
        try:
            if dry:
                print(f"[dry] promote anchor {old_anchor} -> {new_anchor}")
            else:
                pc.move(from_path=old_anchor, to_path=new_anchor, cfg=cfg)
            stats["promoted"] += 1
        except Exception as e:
            print(f"[autofix] promote failed: {e}", file=sys.stderr)
            stats["errors"] += 1

    # 2) fehlende Stubs neu schreiben
    for m in (plan or {}).get("stub_missing", []) or []:
        try:
            _write_stub(m["snapshot"], m["relpath"], m.get("node") or {}, m.get("file_item") or {})
            stats["stubs_rewritten"] += 1
        except Exception as e:
            print(f"[autofix] stub rewrite failed: {e}", file=sys.stderr)
            stats["errors"] += 1

    # 3) verwaiste Stubs löschen
    for o in (plan or {}).get("orphan_stub", []) or []:
        try:
            if dry:
                print(f"[dry] delete orphan stub {o['path']}")
            else:
                pc.delete_file(cfg, path=o["path"])
            stats["orphans_deleted"] += 1
        except Exception as e:
            print(f"[autofix] delete orphan failed: {e}", file=sys.stderr)
            stats["errors"] += 1

    return stats

# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(description="pCloud 1:1 Integrity Check (v3)")
    ap.add_argument("--dest-root", required=True)
    ap.add_argument("--env-file")
    ap.add_argument("--profile")
    ap.add_argument("--json-out", help="Report als JSON schreiben")

    # kombinierter/sep. Stub-Check steuern
    ap.add_argument("--stubs-mode",
                    choices=["separate", "combined", "both"],
                    default="separate",
                    help="Stub-Prüfung: 'separate' (heutiges Verhalten), 'combined' (1-Pass), oder 'both' (Zeitvergleich)")

    # Stichprobengrößen
    ap.add_argument("--stub-sample", type=int, default=100, help="Stichprobe pro Snapshot für Stub-Checks")
    ap.add_argument("--checksum-sample", type=int, default=50, help="Stichprobe für Index-Checksum-Checks")

    # Kombinierter Modus: Level/Sampling
    ap.add_argument("--level", choices=["FAST", "FILEID", "SHA"], default="FILEID",
                    help="Prüftiefe für kombinierten Stub-Check")
    ap.add_argument("--sha-prob", type=float, default=0.0,
                    help="Zusätzliche SHA-Quote 0..1 (nur combined)")

    # Autofix (optional)
    ap.add_argument("--autofix-safe", action="store_true",
                    help="Einfache, sichere Korrekturen automatisch ausführen (Anchor-Promotion, Stub-Rewrite, Orphans löschen).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Autofix nur simulieren (keine Schreiboperationen).")

    args = ap.parse_args()

    cfg = pc.effective_config(env_file=args.env_file, profile=args.profile)
    dest_root = pc._norm_remote_path(args.dest_root)
    snaps_root = f"{dest_root.rstrip('/')}/_snapshots"

    print(f"=== pCloud Integrity Check (v3) ===")
    print(f"Destination: {snaps_root}")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Index laden
    try:
        idx_txt = pc.get_textfile(cfg, path=f"{snaps_root}/_index/content_index.json")
        index = json.loads(idx_txt or '{"version":1,"items":{}}')
    except Exception as e:
        print(f"[ERROR] Index nicht lesbar: {e}")
        sys.exit(2)

    report: Dict[str, Any] = {"timestamp": time.time(), "dest_root": dest_root, "checks": {}}

    # Check 1
    print("=" * 70)
    r1 = check_index_anchors(cfg, index)
    report["checks"]["index_anchors"] = r1
    print(f"[RESULT] Anchors: {r1['checked']} geprüft, {r1['broken_anchors']} defekt, {r1['broken_fileids']} FileID-Fehler\n")

    # Check 2
    print("=" * 70)
    r2 = check_index_checksums(cfg, index, int(args.checksum_sample))
    report["checks"]["index_checksums"] = r2
    print(f"[RESULT] Checksums: {r2['checked']} geprüft, {r2['mismatches']} Abweichungen\n")

    # Check 3
    print("=" * 70)
    r3 = check_orphaned_holders(cfg, index, snaps_root)
    report["checks"]["orphaned_holders"] = r3
    print(f"[RESULT] Holders: {r3.get('remote_snapshots', 0)} Snapshots, {r3.get('orphaned_holders', 0)} Waisen\n")

    # Stub-Checks (wahlweise)
    if args.stubs_mode in ("combined", "both"):
        print("=" * 70)
        print("[check] Stubs (kombiniert: Index & Anchor/FileID/SHA)...")
        t0 = time.time()
        rc = check_stubs_combined(cfg, snaps_root, index,
                                  sample_per_snapshot=int(args.stub_sample),
                                  level=str(args.level or "FILEID").upper(),
                                  sha_prob=float(args.sha_prob or 0.0),
                                  quiet=False)
        print(f"[timing] stubs_combined_sec={time.time()-t0:.2f}\n")
        report["checks"]["stubs_combined"] = rc

    if args.stubs_mode in ("separate", "both"):
        print("=" * 70)
        print(f"[check] Stubs → Index (Sample: {args.stub_sample})...")
        t1 = time.time()
        r4 = check_stubs_to_index(cfg, index, snaps_root, int(args.stub_sample))
        print(f"[RESULT] Stubs→Index: {r4['checked']} geprüft, {r4['missing_in_index']} fehlen im Index")
        print(f"[timing] stubs_index_only_sec={time.time()-t1:.2f}\n")
        report["checks"]["stubs_to_index"] = r4

        print("=" * 70)
        print(f"[check] Stubs → Anchors & FileIDs (Sample: {args.stub_sample}, mit Hash-Verifikation)...")
        t2 = time.time()
        r5 = check_stubs_to_anchors(cfg, snaps_root, int(args.stub_sample))
        print(f"[RESULT] Stubs→Anchors: {r5['checked']} geprüft ({r5.get('hash_checked', 0)} Hash-Checks), "
              f"{r5['broken_anchors']} defekt, {r5['broken_fileids']} FileID-Fehler, "
              f"{r5.get('moved_anchors', 0)} verschoben, {r5.get('hash_mismatches', 0)} Hash-Abweichungen")
        print(f"[timing] stubs_anchor_only_sec={time.time()-t2:.2f}\n")
        report["checks"]["stubs_to_anchors"] = r5

    # Check 8
    print("=" * 70)
    r6 = check_anchor_timeline(cfg, index)
    report["checks"]["anchor_timeline"] = r6
    print(f"[RESULT] Timeline: {r6['checked']} geprüft, {r6['violations']} Zeitlinien-Fehler\n")

    # Zusammenfassung
    print("=" * 70)
    print("=== ZUSAMMENFASSUNG ===")

    total_issues = 0
    total_issues += r1.get("broken_anchors", 0)
    total_issues += r1.get("broken_fileids", 0)
    total_issues += r2.get("mismatches", 0)
    total_issues += r3.get("orphaned_holders", 0)

    if args.stubs_mode in ("separate", "both"):
        total_issues += report["checks"]["stubs_to_index"].get("missing_in_index", 0)
        total_issues += report["checks"]["stubs_to_anchors"].get("broken_anchors", 0)
        total_issues += report["checks"]["stubs_to_anchors"].get("broken_fileids", 0)
        total_issues += report["checks"]["stubs_to_anchors"].get("hash_mismatches", 0)

    if args.stubs_mode in ("combined", "both"):
        # combined hat eigene Zähler; wir interpretieren idx_missing/sha_mismatch als Issues
        total_issues += report["checks"]["stubs_combined"].get("idx_missing", 0)
        total_issues += report["checks"]["stubs_combined"].get("sha_mismatch", 0)

    total_issues += r6.get("violations", 0)
    report["total_issues"] = total_issues

    if total_issues == 0:
        print("[✓] Keine Probleme gefunden!")
        # verschobene Anchors sind Info – in beiden Modi berücksichtigen
        moved_info = 0
        if args.stubs_mode in ("separate", "both"):
            moved_info += report["checks"]["stubs_to_anchors"].get("moved_anchors", 0)
        if args.stubs_mode in ("combined", "both"):
            moved_info += report["checks"]["stubs_combined"].get("moved", 0)
        if moved_info > 0:
            print(f"    [i] {moved_info} verschobene Anchors (Info, kein Fehler)")
        exit_code = 0
    else:
        print(f"[!] {total_issues} Probleme gefunden:")
        if r1.get("broken_anchors", 0):  print(f"    - {r1['broken_anchors']} defekte Index-Anchors")
        if r1.get("broken_fileids", 0):  print(f"    - {r1['broken_fileids']} Index FileID-Fehler")
        if r2.get("mismatches", 0):      print(f"    - {r2['mismatches']} Checksum-Abweichungen")
        if r3.get("orphaned_holders", 0):print(f"    - {r3['orphaned_holders']} verwaiste Holders")
        if args.stubs_mode in ("separate", "both"):
            sidx = report["checks"]["stubs_to_index"]
            sanc = report["checks"]["stubs_to_anchors"]
            if sidx.get("missing_in_index", 0): print(f"    - {sidx['missing_in_index']} Stubs fehlen im Index")
            if sanc.get("broken_anchors", 0):   print(f"    - {sanc['broken_anchors']} defekte Stub-Anchors")
            if sanc.get("broken_fileids", 0):   print(f"    - {sanc['broken_fileids']} Stub FileID-Fehler")
            if sanc.get("hash_mismatches", 0):  print(f"    - {sanc['hash_mismatches']} Hash-Abweichungen (KRITISCH!)")
        if args.stubs_mode in ("combined", "both"):
            sc = report["checks"]["stubs_combined"]
            if sc.get("idx_missing", 0):        print(f"    - {sc['idx_missing']} Stubs fehlen im Index (combined)")
            if sc.get("sha_mismatch", 0):       print(f"    - {sc['sha_mismatch']} Stub SHA-Mismatches (combined)")
        if r6.get("violations", 0): print(f"    - {r6['violations']} Zeitlinien-Fehler")
        exit_code = 1

    # --- optionaler Autofix (safe) ---
    if args.autofix_safe:
        fix = autofix_safe(cfg, dest_root, report, dry=bool(args.dry_run), index_obj=index)
        report["autofix"] = fix

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport: {args.json_out}")

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
