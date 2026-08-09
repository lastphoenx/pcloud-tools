#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_json_pool_manifest.py – erzeugt ein lokales Snapshot-Manifest für POOL-MODE (Schema v4).

=== POOL-MODE VARIANT ===
Basierend auf pcloud_json_manifest.py v3, erweitert für Pool-basierte Uploads:
- Gleiche Manifest-Struktur wie v3
- Vorbereitung für Pool-Upload (SHA256 → /_pool/XX/)
- Spätere Integration mit pcloud_push_json_pool_manifest_to_pcloud.py

Features
- Verzeichnisbaum unter --root erfassen (dirs, files, symlinks)
- Pro Item: snapshot, relpath, type, size/mtime (bei file), sha256 (optional), ext, inode(dev,ino,nlink)
- Smart-Mode: SHA256-Wiederverwendung via mtime/size-Check gegen Referenz-Manifest (40× schneller)
- Optionen für Hash, Hardlink-/Symlink-Handhabung

Beispiel (Full Mode - alle SHA256 neu berechnen)
  SNAP=$(readlink -f /mnt/backup/rtb_nas/latest)
  python pcloud_json_pool_manifest.py \
    --root "$SNAP" \
    --out /srv/pcloud-temp/snap.json \
    --hash sha256 \
    --no-follow-hardlinks \
    --store-hardlink-target \
    --store-symlink-target \
    --follow-symlinks

Beispiel (Smart Mode - mtime/size-Cache gegen Vorgänger)
  python pcloud_json_pool_manifest.py \
    --root "$SNAP" \
    --out /srv/pcloud-temp/snap.json \
    --ref-manifest /srv/pcloud-archive/2026-04-10-075334.manifest.json \
    --hash sha256
"""

from __future__ import annotations
import os, sys, json, argparse, hashlib, time, datetime
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional, Iterator

import pcloud_bin_lib as pcl
import pcloud_path_compat as ppc

# ---- Logging mit Timestamp (RTB-Stil) ----
def _log(msg: str, *, file=sys.stderr) -> None:
    """Log-Ausgabe mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", file=file, flush=True)

# ---------------- reference manifest cache ----------------

class ReferenceCache:
    """Cache für SHA256-Wiederverwendung aus Referenz-Manifest (mtime/size-basiert)"""
    
    def __init__(self, ref_manifest_path: Optional[str] = None):
        self.ref_manifest_path = ref_manifest_path
        self.ref_snapshot = None  # Snapshot-Name des Referenz-Manifests
        self.mtime_cache: Dict[str, Dict[str, Any]] = {}  # relpath → {sha256, mtime, size}
        self.inode_cache: Dict[Tuple[int, int], str] = {}  # (dev, ino) → sha256
        self.stats = {
            "reused_from_ref_mtime": 0,
            "reused_from_hardlink": 0,
            "calculated_sha256": 0,
        }
        
        if ref_manifest_path:
            self._load_reference(ref_manifest_path)
    
    def _load_reference(self, path: str) -> None:
        """Lade Referenz-Manifest und baue Caches auf"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                ref = json.load(f)
            
            self.ref_snapshot = ref.get("snapshot", "?")
            _log(f"[ref] Lade Referenz-Manifest: {self.ref_snapshot} ({path})")
            
            loaded_count = 0
            for item in ref.get("items", []):
                if item.get("type") != "file":
                    continue
                
                relpath = item.get("relpath")
                sha256 = item.get("sha256")
                mtime = item.get("mtime")
                size = item.get("size")
                
                if not relpath or not sha256:
                    continue
                
                # mtime/size-Cache
                self.mtime_cache[relpath] = {
                    "sha256": sha256,
                    "mtime": mtime,
                    "size": size,
                }
                
                # inode-Cache (für Hardlinks)
                inode = item.get("inode")
                if inode:
                    dev = inode.get("dev")
                    ino = inode.get("ino")
                    if dev is not None and ino is not None:
                        self.inode_cache[(dev, ino)] = sha256
                
                loaded_count += 1
            
            _log(f"[ref] ✓ {loaded_count} Dateien im Cache (mtime/size + inode)")
        
        except FileNotFoundError:
            print(f"[ref] ⚠ Referenz-Manifest nicht gefunden: {path}", file=sys.stderr)
        except Exception as e:
            print(f"[ref] ⚠ Fehler beim Laden: {e}", file=sys.stderr)
    
    def lookup(self, relpath: str, st_mtime: float, st_size: int, dev: int, ino: int) -> Optional[str]:
        """
        SHA256 nachschlagen via mtime/size oder inode
        
        Returns:
            SHA256 wenn Cache-Hit, sonst None
        """
        # Strategie 1: mtime + size Match in gleichem relpath
        if relpath in self.mtime_cache:
            cached = self.mtime_cache[relpath]
            if cached["mtime"] == st_mtime and cached["size"] == st_size:
                self.stats["reused_from_ref_mtime"] += 1
                return cached["sha256"]
        
        # Strategie 2: Hardlink-Match via inode (wenn nlink > 1)
        inode_key = (dev, ino)
        if inode_key in self.inode_cache:
            self.stats["reused_from_hardlink"] += 1
            return self.inode_cache[inode_key]
        
        return None
    
    def record_calculated(self, relpath: str, sha256: str, st_mtime: float, st_size: int, dev: int, ino: int) -> None:
        """Neu berechneten SHA256 in Cache aufnehmen (für spätere Hardlink-Matches)"""
        self.stats["calculated_sha256"] += 1
        self.inode_cache[(dev, ino)] = sha256

# ---------------- util ----------------

def sha256_file(p: str, buf: int = int(os.environ.get("MANIFEST_HASH_BUFSIZE", 4*1024*1024))) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------- walker ----------------

def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}"
        b /= 1024


@dataclass
class WalkStats:
    total_files: int
    total_bytes: int
    total_items: int


# Aliase für CLI-Kompatibilität (Implementierung in pcloud_bin_lib)
RefManifestPick = pcl.ManifestRefPick


def walk(root: str,
         snapshot: str,
         *,
         hash_algo: Optional[str],          # "sha256" oder None
         follow_symlinks: bool,
         follow_hardlinks: bool,
         store_hardlink_target: bool,
         store_symlink_target: bool,
         progress_interval: float = 30.0,
         ref_cache: Optional[ReferenceCache] = None,
         auto_ref_manifests_dir: Optional[str] = None,
         jsonl_tmp: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[ReferenceCache], WalkStats]:
    """
    Scannt Verzeichnisbaum und erzeugt Manifest-Items.

    Scan-Index landet auf SSD (TSV + externes sort), nicht in RAM.
    Mit jsonl_tmp: Items sofort als JSONL schreiben (append-only, resume-fähig).
    """

    base = os.path.abspath(root)

    skip_globs = ppc.parse_manifest_skip_globs()
    check_risky = (
        ppc.relpath_has_risky_segments
        if ppc.manifest_warn_risky_paths_enabled()
        else None
    )

    def _excluded(rel: str) -> bool:
        return ppc.relpath_excluded(rel, skip_globs)

    # === 1. Scan → sortierte TSV auf Disk (pcloud_bin_lib) ===
    _log("[scan] Erstelle sortierte File-Liste (disk-backed)...")
    scan = pcl.manifest_scan_tree_to_sorted_tsv(
        base,
        follow_symlinks=follow_symlinks,
        relpath_excluded=_excluded,
        check_risky_relpath=check_risky,
        scan_base=jsonl_tmp,
    )
    total_files = scan.total_files
    total_bytes = scan.total_bytes
    total_items = scan.total_items

    if scan.skipped_files:
        _log(f"[scan] Übersprungen (PCLOUD_MANIFEST_SKIP_GLOBS): {scan.skipped_files} Dateien")
    if scan.risky_path_count:
        examples = ", ".join(repr(s) for s in scan.risky_samples)
        _log(f"[scan][WARN] {scan.risky_path_count} Pfad(e) mit Segment-Whitespace "
             f"(pCloud kann anders normalisieren) — z.B. {examples}")

    if ref_cache is None and auto_ref_manifests_dir:
        pick = pcl.manifest_pick_ref_from_scan(
            snapshot,
            auto_ref_manifests_dir,
            pcl.manifest_iter_file_scan_from_sorted(scan.sorted_path),
            total_files=total_files,
            max_candidates=pcl.manifest_ref_max_candidates(),
            min_hit_rate=pcl.manifest_ref_min_hit_rate(),
            on_candidate_skip=lambda p, e: _log(
                f"[ref-pick][warn] Ueberspringe {os.path.basename(p)}: {e}"
            ),
        )
        if pick:
            _log(
                f"[ref-pick] ✓ {pick.snapshot} "
                f"({pick.hit_rate * 100:.1f}% mtime/size, {pick.hits}/{pick.total_files}, "
                f"{pick.candidates} Kandidaten)"
            )
            ref_cache = ReferenceCache(pick.path)
        else:
            _log("[ref-pick] Kein Referenz-Manifest gewaehlt — Full-Hash")

    _log(f"[manifest] Starte: {total_files} Dateien, {_fmt_bytes(total_bytes)}")

    # === 2. Resume ===
    resume_from = pcl.manifest_jsonl_line_count(jsonl_tmp) if jsonl_tmp else 0
    if resume_from:
        _log(f"[resume] ✓ {resume_from}/{total_items} Items bereits verarbeitet - setze fort")

    # === 3. Streaming Processing ===
    items: List[Dict[str, Any]] = []
    first_seen: dict[tuple[int, int], str] = {}

    jsonl_file = None
    if jsonl_tmp:
        jsonl_file = open(jsonl_tmp, "a", encoding="utf-8")

    done_files = 0
    done_bytes = 0
    t_start = time.monotonic()
    t_last_progress = t_start

    for idx, (item_type, relpath, abs_path, size, _scan_mtime) in enumerate(
        pcl.manifest_iter_scan_records(scan.sorted_path)
    ):
        if idx < resume_from:
            continue

        entry: Dict[str, Any] = {}

        if item_type == "dir":
            entry = {
                "snapshot": snapshot,
                "relpath": relpath,
                "type": "dir",
            }
        else:
            try:
                st = os.lstat(abs_path)
            except (FileNotFoundError, OSError):
                continue

            if os.path.islink(abs_path):
                entry = {
                    "snapshot": snapshot,
                    "relpath": relpath,
                    "type": "symlink",
                    "lmode": oct(st.st_mode),
                }
                if store_symlink_target:
                    try:
                        entry["target"] = os.readlink(abs_path)
                    except OSError as e:
                        entry["target_error"] = str(e)

            elif os.path.isfile(abs_path):
                dev = int(st.st_dev); ino = int(st.st_ino); nlink = int(st.st_nlink)
                inode_obj = {"dev": dev, "ino": ino, "nlink": nlink}

                _, ext = os.path.splitext(relpath)
                ext = ext if ext else None

                file_hash = None
                if hash_algo == "sha256":
                    if ref_cache:
                        file_hash = ref_cache.lookup(relpath, float(st.st_mtime), int(st.st_size), dev, ino)

                    if not file_hash:
                        try:
                            file_hash = sha256_file(abs_path)
                            if ref_cache:
                                ref_cache.record_calculated(relpath, file_hash, float(st.st_mtime), int(st.st_size), dev, ino)
                        except Exception as e:
                            print(f"[warn] hash fail: {abs_path}: {e}", file=sys.stderr)

                entry = {
                    "snapshot": snapshot,
                    "type": "file",
                    "relpath": relpath,
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "source_path": os.path.abspath(abs_path),
                    "ext": ext,
                    "inode": inode_obj,
                }
                if file_hash:
                    entry["sha256"] = file_hash

                done_files += 1
                done_bytes += int(st.st_size)

                if store_hardlink_target and nlink > 1:
                    key = (dev, ino)
                    if key in first_seen:
                        entry["hardlink_of"] = first_seen[key]
                    else:
                        first_seen[key] = relpath
                        entry["hardlink_master"] = True

                now = time.monotonic()
                if now - t_last_progress >= progress_interval:
                    elapsed = now - t_start
                    pct_files = done_files / total_files * 100 if total_files else 0
                    pct_bytes = done_bytes / total_bytes * 100 if total_bytes else 0
                    eta_s = (elapsed / done_bytes * (total_bytes - done_bytes)) if done_bytes else 0
                    eta_str = f"~{int(eta_s/60)}min" if eta_s > 60 else f"~{int(eta_s)}s"
                    _log(
                        f"[manifest] {done_files}/{total_files} Dateien ({pct_files:.0f}%) | "
                        f"{_fmt_bytes(done_bytes)} / {_fmt_bytes(total_bytes)} ({pct_bytes:.0f}%) | "
                        f"{eta_str} verbleibend"
                    )
                    t_last_progress = now

        if entry:
            if jsonl_file:
                jsonl_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                jsonl_file.flush()
            else:
                items.append(entry)

    if jsonl_file:
        jsonl_file.close()

    pcl.manifest_cleanup_scan_files(scan)

    stats = WalkStats(total_files=total_files, total_bytes=total_bytes, total_items=total_items)
    return items, ref_cache, stats

# ---------------- smart ref manifest picker (CLI; Kern in pcloud_bin_lib) ----------------

def pick_best_ref_manifest_from_scan(
    snapshot_name: str,
    manifests_dir: str,
    file_scan,
    *,
    total_files: int,
    max_candidates: int = 6,
    min_hit_rate: float = 0.0,
) -> Optional[RefManifestPick]:
    return pcl.manifest_pick_ref_from_scan(
        snapshot_name,
        manifests_dir,
        file_scan,
        total_files=total_files,
        max_candidates=max_candidates,
        min_hit_rate=min_hit_rate,
    )


def _iter_snapshot_files_for_ref_score(
    root: str,
    *,
    follow_symlinks: bool = False,
) -> Iterator[Tuple[str, float, int]]:
    """Stat-Walk fuer Referenz-Scoring (gleiche Skip-Regeln wie Manifest-Walk)."""
    base = os.path.abspath(root)
    skip_globs = ppc.parse_manifest_skip_globs()
    for cur, _dirs, files in os.walk(base, followlinks=follow_symlinks):
        rel_cur = os.path.relpath(cur, base).replace("\\", "/")
        if rel_cur == ".":
            rel_cur = ""
        for name in files:
            rel = (os.path.join(rel_cur, name) if rel_cur else name).replace("\\", "/")
            if ppc.relpath_excluded(rel, skip_globs):
                continue
            ab = os.path.join(cur, name)
            try:
                st = os.lstat(ab)
            except (FileNotFoundError, OSError):
                continue
            if os.path.islink(ab):
                continue
            yield rel, float(st.st_mtime), int(st.st_size)


def pick_best_ref_manifest(
    snapshot_root: str,
    snapshot_name: str,
    manifests_dir: str,
    *,
    follow_symlinks: bool = False,
    max_candidates: int = 6,
    min_hit_rate: float = 0.0,
) -> Optional[RefManifestPick]:
    """CLI-Helfer: ein Walk, dann Scoring (gleiche Kandidaten-Logik wie im Manifest-Lauf)."""
    file_scan = list(_iter_snapshot_files_for_ref_score(
        snapshot_root, follow_symlinks=follow_symlinks,
    ))
    return pcl.manifest_pick_ref_from_scan(
        snapshot_name,
        manifests_dir,
        file_scan,
        total_files=len(file_scan),
        max_candidates=max_candidates,
        min_hit_rate=min_hit_rate,
    )


def run_pick_ref_manifest_cli(args: argparse.Namespace) -> int:
    manifests_dir = os.path.abspath(args.manifests_dir or "")
    if not manifests_dir:
        print("--manifests-dir erforderlich", file=sys.stderr)
        return 2

    try:
        min_hit = pcl.manifest_ref_min_hit_rate()
    except ValueError:
        min_hit = 0.0

    pick = pick_best_ref_manifest(
        os.path.abspath(args.root),
        args.snapshot or os.path.basename(os.path.abspath(args.root)),
        manifests_dir,
        follow_symlinks=bool(args.follow_symlinks),
        max_candidates=pcl.manifest_ref_max_candidates(),
        min_hit_rate=min_hit,
    )
    if pick is None:
        _log("[ref-pick] Kein Referenz-Manifest gewaehlt (keine Kandidaten oder 0% Deckung)")
        return 0

    _log(
        f"[ref-pick] ✓ {pick.snapshot} "
        f"({pick.hit_rate * 100:.1f}% mtime/size, {pick.hits}/{pick.total_files}, "
        f"{pick.candidates} Kandidaten)"
    )
    print(pick.path)
    return 0

# ---------------- main ----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Lokales Snapshot-Manifest erzeugen (Pool-Mode Schema v4).")

    ap.add_argument("--root", required=True, help="Lokales Quellverzeichnis (z. B. ein RTB-Snapshot)")
    ap.add_argument("--snapshot", help="Snapshot-Name (Default: YYYYmmdd-HHMMSS)")
    ap.add_argument("--out", help="Manifest-Zieldatei (JSON). Default: stdout")
    
    # Smart-Mode (NEU in Schema v3)
    ap.add_argument("--ref-manifest", help="Referenz-Manifest für Smart-Mode (mtime/size-Cache, 40× schneller)")
    ap.add_argument(
        "--pick-ref-manifest",
        action="store_true",
        help="Nur bestes Referenz-Manifest waehlen (ein Walk, max. 6 Kandidaten); Pfad auf stdout",
    )
    ap.add_argument(
        "--auto-ref-manifest",
        action="store_true",
        help="Referenz nach Scan automatisch waehlen (ein Walk, integriert in Manifest-Lauf)",
    )
    ap.add_argument(
        "--manifests-dir",
        help="Archiv-Verzeichnis fuer --pick-ref-manifest / --auto-ref-manifest",
    )

    # Verhalten
    ap.add_argument("--hash", choices=["sha256", "none"], default="sha256", help="Datei-Hash aufnehmen (Default: sha256)")
    ap.add_argument("--follow-symlinks", action="store_true", help="Symlinks als Dateien traversieren (Default: nein)")
    ap.add_argument("--no-follow-hardlinks", dest="follow_hardlinks", action="store_false",
                    help="Hardlinks NICHT zusammenführen (nur Info, Default: folgen=True)")
    ap.set_defaults(follow_hardlinks=True)
    ap.add_argument("--store-hardlink-target", action="store_true",
                    help="relpath des ersten Auftretens (dev,ino) mitschreiben")
    ap.add_argument("--store-symlink-target", action="store_true",
                    help="Symlink-Ziel (readlink) mitschreiben")

    args = ap.parse_args()

    if args.pick_ref_manifest:
        sys.exit(run_pick_ref_manifest_cli(args))

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"root not found: {root}", file=sys.stderr)
        sys.exit(2)

    snap = args.snapshot or time.strftime("%Y%m%d-%H%M%S")
    hash_algo = None if args.hash == "none" else args.hash
    
    # Smart-Mode: ReferenceCache (explizit oder Auto-Pick im Scan)
    ref_cache = None
    if args.ref_manifest:
        ref_cache = ReferenceCache(args.ref_manifest)

    auto_ref_dir: Optional[str] = None
    if args.auto_ref_manifest:
        if not args.manifests_dir:
            print("--manifests-dir erforderlich mit --auto-ref-manifest", file=sys.stderr)
            sys.exit(2)
        auto_ref_dir = os.path.abspath(args.manifests_dir)
    
    # JSONL-Streaming: Nur wenn --out gegeben (sonst stdout → kein Resume sinnvoll)
    jsonl_tmp = None
    if args.out:
        jsonl_tmp = f"{args.out}.tmp.jsonl"
    
    # Items sammeln (JSONL-Streaming oder Memory)
    items, ref_cache, walk_stats = walk(
        root,
        snap,
        hash_algo=hash_algo,
        follow_symlinks=bool(args.follow_symlinks),
        follow_hardlinks=bool(args.follow_hardlinks),
        store_hardlink_target=bool(args.store_hardlink_target),
        store_symlink_target=bool(args.store_symlink_target),
        ref_cache=ref_cache,
        auto_ref_manifests_dir=auto_ref_dir,
        jsonl_tmp=jsonl_tmp,
    )

    total_files = walk_stats.total_files

    # Schema 4 für Pool-Mode
    schema_version = 4
    mode = "pool_smart" if ref_cache else "pool_full"

    payload_meta: Dict[str, Any] = {
        "schema": schema_version,
        "mode": mode,
        "snapshot": snap,
        "root": root,
        "created": int(time.time()),
        "hash": (hash_algo or "none"),
        "follow_symlinks": bool(args.follow_symlinks),
        "follow_hardlinks": bool(args.follow_hardlinks),
        "store_hardlink_target": bool(args.store_hardlink_target),
        "store_symlink_target": bool(args.store_symlink_target),
    }

    if ref_cache:
        ref_path = ref_cache.ref_manifest_path or args.ref_manifest or ""
        payload_meta["ref_manifest"] = {
            "path": ref_path,
            "snapshot": ref_cache.ref_snapshot or "?",
            "loaded_at": int(time.time()),
        }
        payload_meta["stats"] = {
            "total_files": total_files,
            "reused_from_ref_mtime": ref_cache.stats["reused_from_ref_mtime"],
            "reused_from_hardlink": ref_cache.stats["reused_from_hardlink"],
            "calculated_sha256": ref_cache.stats["calculated_sha256"],
        }
        _log(f"[stats] total={total_files} | "
             f"reused_mtime={ref_cache.stats['reused_from_ref_mtime']} | "
             f"reused_hardlink={ref_cache.stats['reused_from_hardlink']} | "
             f"calculated={ref_cache.stats['calculated_sha256']}")
    else:
        _log(f"[stats] total={total_files} | mode={mode} (kein Cache)")

    # Manifest schreiben (stdout oder Datei)
    if jsonl_tmp and os.path.exists(jsonl_tmp) and args.out:
        _log("[finalize] JSONL → JSON (streaming, kein RAM-Spike)...")
        item_count = pcl.manifest_write_json_from_jsonl(
            args.out,
            meta=payload_meta,
            jsonl_path=jsonl_tmp,
        )
        os.remove(jsonl_tmp)
        _log(f"[finalize] ✓ {item_count} Items geschrieben")
        _log(f"[manifest] ✓ Geschrieben: {args.out}")
    elif args.out:
        payload = dict(payload_meta)
        payload["items"] = items
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        _log(f"[manifest] ✓ Geschrieben: {args.out}")
    else:
        payload = dict(payload_meta)
        payload["items"] = items
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)  # Trailing newline

if __name__ == "__main__":
    main()
