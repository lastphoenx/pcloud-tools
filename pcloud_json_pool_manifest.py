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
         jsonl_tmp: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Scannt Verzeichnisbaum und erzeugt Manifest-Items.
    
    NEU: JSONL-Streaming für crash-resistente, RAM-schonende Verarbeitung.
    - Wenn jsonl_tmp gegeben: Items werden sofort als JSONL geschrieben (append-only)
    - Resume: Existierende Zeilen in jsonl_tmp werden übersprungen
    - Return: Leere Liste (Items sind in jsonl_tmp), oder vollständige Liste falls jsonl_tmp=None
    """

    base = os.path.abspath(root)

    skip_globs = ppc.parse_manifest_skip_globs()
    skipped_files = 0
    risky_path_count = 0
    risky_samples: List[str] = []

    # === 1. Sortierte File-Liste erstellen (deterministisch!) ===
    _log("[scan] Erstelle sortierte File-Liste...")
    all_paths = []
    total_bytes = 0
    
    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        rel_cur = os.path.relpath(cur, base).replace("\\", "/")
        if rel_cur == ".": rel_cur = ""
        
        # Verzeichnis als Item
        if not (rel_cur and ppc.relpath_excluded(rel_cur, skip_globs)):
            all_paths.append(("dir", rel_cur, None, 0))
            if rel_cur and ppc.manifest_warn_risky_paths_enabled() and ppc.relpath_has_risky_segments(rel_cur):
                risky_path_count += 1
                if len(risky_samples) < 3:
                    risky_samples.append(rel_cur)
        
        # Dateien/Symlinks
        for name in files:
            ab = os.path.join(cur, name)
            rel = (os.path.join(rel_cur, name) if rel_cur else name).replace("\\", "/")

            if ppc.relpath_excluded(rel, skip_globs):
                skipped_files += 1
                continue

            if ppc.manifest_warn_risky_paths_enabled() and ppc.relpath_has_risky_segments(rel):
                risky_path_count += 1
                if len(risky_samples) < 3:
                    risky_samples.append(rel)
            
            try:
                st = os.lstat(ab)
                size = int(st.st_size) if not os.path.islink(ab) else 0
                all_paths.append(("file", rel, ab, size))
                if not os.path.islink(ab):
                    total_bytes += size
            except (FileNotFoundError, OSError):
                continue
    
    # Sortieren (garantiert identische Reihenfolge bei Resume)
    all_paths.sort(key=lambda x: x[1])  # Nach relpath sortieren
    total_files = sum(1 for t, _, _, _ in all_paths if t == "file")
    
    if skipped_files:
        _log(f"[scan] Übersprungen (PCLOUD_MANIFEST_SKIP_GLOBS): {skipped_files} Dateien")
    if risky_path_count:
        examples = ", ".join(repr(s) for s in risky_samples)
        _log(f"[scan][WARN] {risky_path_count} Pfad(e) mit Segment-Whitespace "
             f"(pCloud kann anders normalisieren) — z.B. {examples}")
    
    _log(f"[manifest] Starte: {total_files} Dateien, {_fmt_bytes(total_bytes)}")
    
    # === 2. Resume: Wie viele Items bereits verarbeitet? ===
    resume_from = 0
    if jsonl_tmp and os.path.exists(jsonl_tmp):
        with open(jsonl_tmp, encoding="utf-8") as f:
            resume_from = sum(1 for _ in f)
        _log(f"[resume] ✓ {resume_from}/{len(all_paths)} Items bereits verarbeitet - setze fort")
    
    # === 3. Streaming Processing ===
    items: List[Dict[str, Any]] = []  # Leer bei JSONL-Mode
    first_seen: dict[tuple[int,int], str] = {}
    
    jsonl_file = None
    if jsonl_tmp:
        jsonl_file = open(jsonl_tmp, "a", encoding="utf-8")
    
    done_files = 0
    done_bytes = 0
    t_start = time.monotonic()
    t_last_progress = t_start
    
    for idx, (item_type, relpath, abs_path, size) in enumerate(all_paths):
        # Skip bereits verarbeitete Items (Resume)
        if idx < resume_from:
            continue
        
        entry: Dict[str, Any] = {}
        
        # === DIR ===
        if item_type == "dir":
            entry = {
                "snapshot": snapshot,
                "relpath": relpath,
                "type": "dir",
            }
        
        # === FILE/SYMLINK ===
        else:
            try:
                st = os.lstat(abs_path)
            except (FileNotFoundError, OSError):
                continue
            
            # Symlink?
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
            
            # Reguläre Datei
            elif os.path.isfile(abs_path):
                dev = int(st.st_dev); ino = int(st.st_ino); nlink = int(st.st_nlink)
                inode_obj = {"dev": dev, "ino": ino, "nlink": nlink}
                
                _, ext = os.path.splitext(relpath)
                ext = ext if ext else None
                
                # Hash via Smart-Cache oder Berechnung
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
                
                # Hardlink-Ziel optional festhalten
                if store_hardlink_target and nlink > 1:
                    key = (dev, ino)
                    if key in first_seen:
                        entry["hardlink_of"] = first_seen[key]
                    else:
                        first_seen[key] = relpath
                        entry["hardlink_master"] = True
                
                # Progress
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
        
        # === Write: JSONL oder Memory ===
        if entry:
            if jsonl_file:
                jsonl_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                jsonl_file.flush()  # Sofort auf Disk (crash-resistent)
            else:
                items.append(entry)
    
    # Cleanup
    if jsonl_file:
        jsonl_file.close()
    
    return items

# ---------------- smart ref manifest picker ----------------

@dataclass(frozen=True)
class RefManifestPick:
    path: str
    snapshot: str
    hit_rate: float
    hits: int
    total_files: int
    candidates: int


def _mtime_size_cache_from_manifest(path: str) -> Tuple[str, Dict[str, Tuple[float, int]]]:
    """relpath → (mtime, size) aus archiviertem Manifest."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    snap = str(data.get("snapshot") or os.path.splitext(os.path.basename(path))[0])
    cache: Dict[str, Tuple[float, int]] = {}
    for item in data.get("items", []):
        if item.get("type") != "file":
            continue
        relpath = item.get("relpath")
        mtime = item.get("mtime")
        size = item.get("size")
        if not relpath or mtime is None or size is None:
            continue
        cache[str(relpath)] = (float(mtime), int(size))
    return snap, cache


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
    min_hit_rate: float = 0.0,
) -> Optional[RefManifestPick]:
    """
    Waehlt das Referenz-Manifest mit hoechster mtime/size-Deckung zum Ziel-Snapshot.

    Alle Kandidaten in manifests_dir (auch chronologisch neuere) werden verglichen —
    gleiche Metrik wie ReferenceCache.lookup (relpath + mtime + size).
    """
    if not os.path.isdir(manifests_dir):
        return None

    candidate_paths: List[str] = []
    for name in sorted(os.listdir(manifests_dir)):
        if not name.endswith(".json"):
            continue
        snap = name[:-5]
        if snap == snapshot_name:
            continue
        path = os.path.join(manifests_dir, name)
        if os.path.isfile(path):
            candidate_paths.append(path)

    if not candidate_paths:
        return None

    caches: Dict[str, Tuple[str, Dict[str, Tuple[float, int]]]] = {}
    for path in candidate_paths:
        try:
            caches[path] = _mtime_size_cache_from_manifest(path)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            _log(f"[ref-pick][warn] Ueberspringe {os.path.basename(path)}: {e}")

    if not caches:
        return None

    scores: Dict[str, int] = {p: 0 for p in caches}
    total_files = 0
    for relpath, mtime, size in _iter_snapshot_files_for_ref_score(
        snapshot_root, follow_symlinks=follow_symlinks,
    ):
        total_files += 1
        for path, (_snap, cache) in caches.items():
            ent = cache.get(relpath)
            if ent is not None and ent[0] == mtime and ent[1] == size:
                scores[path] += 1

    if total_files == 0:
        return None

    best_path = max(
        scores.keys(),
        key=lambda p: (scores[p] / total_files, scores[p], p),
    )
    hits = scores[best_path]
    hit_rate = hits / total_files
    if hit_rate < min_hit_rate:
        return None

    best_snap = caches[best_path][0]
    return RefManifestPick(
        path=best_path,
        snapshot=best_snap,
        hit_rate=hit_rate,
        hits=hits,
        total_files=total_files,
        candidates=len(caches),
    )


def run_pick_ref_manifest_cli(args: argparse.Namespace) -> int:
    manifests_dir = os.path.abspath(args.manifests_dir or "")
    if not manifests_dir:
        print("--manifests-dir erforderlich", file=sys.stderr)
        return 2

    try:
        min_hit = float(os.environ.get("PCLOUD_MANIFEST_REF_MIN_HIT_RATE", "0"))
    except ValueError:
        min_hit = 0.0

    pick = pick_best_ref_manifest(
        os.path.abspath(args.root),
        args.snapshot or os.path.basename(os.path.abspath(args.root)),
        manifests_dir,
        follow_symlinks=bool(args.follow_symlinks),
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
        help="Nur bestes Referenz-Manifest waehlen (mtime/size-Deckung); Pfad auf stdout",
    )
    ap.add_argument(
        "--manifests-dir",
        help="Archiv-Verzeichnis fuer --pick-ref-manifest (z. B. /srv/pcloud-archive/manifests)",
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
    
    # Smart-Mode: ReferenceCache initialisieren
    ref_cache = None
    if args.ref_manifest:
        ref_cache = ReferenceCache(args.ref_manifest)
    
    # JSONL-Streaming: Nur wenn --out gegeben (sonst stdout → kein Resume sinnvoll)
    jsonl_tmp = None
    if args.out:
        jsonl_tmp = f"{args.out}.tmp.jsonl"
    
    # Items sammeln (JSONL-Streaming oder Memory)
    items = walk(
        root,
        snap,
        hash_algo=hash_algo,
        follow_symlinks=bool(args.follow_symlinks),
        follow_hardlinks=bool(args.follow_hardlinks),
        store_hardlink_target=bool(args.store_hardlink_target),
        store_symlink_target=bool(args.store_symlink_target),
        ref_cache=ref_cache,
        jsonl_tmp=jsonl_tmp,
    )
    
    # Finalize: JSONL → Items laden (wenn JSONL-Modus aktiv war)
    if jsonl_tmp and os.path.exists(jsonl_tmp):
        _log("[finalize] Konvertiere JSONL → JSON...")
        with open(jsonl_tmp, encoding="utf-8") as f:
            items = [json.loads(line) for line in f if line.strip()]
        # Cleanup JSONL (erfolgreich abgeschlossen)
        os.remove(jsonl_tmp)
        _log(f"[finalize] ✓ {len(items)} Items geladen")
    
    # Schema 4 für Pool-Mode
    schema_version = 4
    mode = "pool_smart" if ref_cache else "pool_full"
    
    # total_files VOR if-Block berechnen (nicht nur im ref_cache-Block!)
    total_files = sum(1 for it in items if it.get("type") == "file")
    
    payload: Dict[str, Any] = {
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
        "items": items,
    }
    
    # Schema 4 Erweiterungen
    if ref_cache:
        payload["ref_manifest"] = {
            "path": args.ref_manifest,
            "snapshot": ref_cache.ref_snapshot or "?",
            "loaded_at": int(time.time()),
        }
        
        # Stats: Performance-Metriken (nur bei Smart-Mode)
        payload["stats"] = {
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
        # Full-Mode: keine Cache-Stats
        _log(f"[stats] total={total_files} | mode={mode} (kein Cache)")
    
    # Manifest schreiben (stdout oder Datei)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        _log(f"[manifest] ✓ Geschrieben: {args.out}")
    else:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)  # Trailing newline

if __name__ == "__main__":
    main()
