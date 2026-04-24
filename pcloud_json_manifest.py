#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_json_manifest.py – erzeugt ein lokales Snapshot-Manifest (Schema v3).

Features
- Verzeichnisbaum unter --root erfassen (dirs, files, symlinks)
- Pro Item: snapshot, relpath, type, size/mtime (bei file), sha256 (optional), ext, inode(dev,ino,nlink)
- Smart-Mode: SHA256-Wiederverwendung via mtime/size-Check gegen Referenz-Manifest (40× schneller)
- Optionen für Hash, Hardlink-/Symlink-Handhabung

Beispiel (Full Mode - alle SHA256 neu berechnen)
  SNAP=$(readlink -f /mnt/backup/rtb_nas/latest)
  python pcloud_json_manifest.py \
    --root "$SNAP" \
    --out /srv/pcloud-temp/snap.json \
    --hash sha256 \
    --no-follow-hardlinks \
    --store-hardlink-target \
    --store-symlink-target \
    --follow-symlinks

Beispiel (Smart Mode - mtime/size-Cache gegen Vorgänger)
  python pcloud_json_manifest.py \
    --root "$SNAP" \
    --out /srv/pcloud-temp/snap.json \
    --ref-manifest /srv/pcloud-archive/2026-04-10-075334.manifest.json \
    --hash sha256
"""

from __future__ import annotations
import os, sys, json, argparse, hashlib, time, datetime
from typing import Dict, Any, List, Tuple, Optional

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
         ref_cache: Optional[ReferenceCache] = None) -> List[Dict[str, Any]]:

    items: List[Dict[str, Any]] = []
    base = os.path.abspath(root)

    # Für optionale Hardlink-Zielverfolgung: erste Sicht pro (dev,ino)
    first_seen: dict[tuple[int,int], str] = {}

    # Für Fortschritts-Reporting: Gesamtgröße vorab ermitteln
    total_bytes = 0
    total_files = 0
    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        for name in files:
            ab = os.path.join(cur, name)
            if not os.path.islink(ab) and os.path.isfile(ab):
                try:
                    total_bytes += os.path.getsize(ab)
                    total_files += 1
                except OSError:
                    pass
    print(f"[manifest] Starte: {total_files} Dateien, {_fmt_bytes(total_bytes)}", file=sys.stderr)

    done_files = 0
    done_bytes = 0
    t_start = time.monotonic()
    t_last_progress = t_start

    for cur, dirs, files in os.walk(base, followlinks=follow_symlinks):
        rel_cur = os.path.relpath(cur, base).replace("\\", "/")
        if rel_cur == ".": rel_cur = ""

        # DIR
        items.append({
            "snapshot": snapshot,
            "relpath": rel_cur,
            "type": "dir",
        })

        # FILES
        for name in files:
            ab = os.path.join(cur, name)
            rel = (os.path.join(rel_cur, name) if rel_cur else name).replace("\\", "/")

            try:
                st = os.lstat(ab)  # lstat! (Symlink-Metadaten)
            except FileNotFoundError:
                # Zwischenzeitlich verschwunden – überspringen
                continue

            # Symlink?
            if os.path.islink(ab):
                entry: Dict[str, Any] = {
                    "snapshot": snapshot,
                    "relpath": rel,
                    "type": "symlink",
                    "lmode": oct(st.st_mode),
                }
                if store_symlink_target:
                    try:
                        entry["target"] = os.readlink(ab)
                    except OSError as e:
                        entry["target_error"] = str(e)
                items.append(entry)
                continue

            # Nur reguläre Dateien erfassen (keine Sockets/Devices/…)
            if not os.path.isfile(ab):
                continue

            # Inode/Hardlink-Infos
            dev = int(st.st_dev); ino = int(st.st_ino); nlink = int(st.st_nlink)
            inode_obj = {"dev": dev, "ino": ino, "nlink": nlink}

            # Extension bestimmen
            _, ext = os.path.splitext(rel)
            ext = ext if ext else None

            # Hash via Smart-Cache oder Berechnung
            file_hash = None
            if hash_algo == "sha256":
                # Strategie: Versuche Cache-Lookup, sonst berechne
                if ref_cache:
                    file_hash = ref_cache.lookup(rel, float(st.st_mtime), int(st.st_size), dev, ino)
                
                if not file_hash:
                    # Kein Cache-Hit → berechne SHA256
                    try:
                        file_hash = sha256_file(ab)
                        if ref_cache:
                            ref_cache.record_calculated(rel, file_hash, float(st.st_mtime), int(st.st_size), dev, ino)
                    except Exception as e:
                        print(f"[warn] hash fail: {ab}: {e}", file=sys.stderr)

            entry: Dict[str, Any] = {
                "snapshot": snapshot,
                "type": "file",
                "relpath": rel,
                "size": int(st.st_size),
                "mtime": float(st.st_mtime),
                "source_path": os.path.abspath(ab),
                "ext": ext,
                "inode": inode_obj,
            }
            if file_hash:
                entry["sha256"] = file_hash

            done_files += 1
            done_bytes += int(st.st_size)

            # Fortschritt alle progress_interval Sekunden
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

            # Hardlink-Ziel optional festhalten
            if store_hardlink_target and nlink > 1:
                key = (dev, ino)
                if key in first_seen:
                    entry["hardlink_of"] = first_seen[key]  # relpath der ersten Sicht
                else:
                    first_seen[key] = rel
                    entry["hardlink_master"] = True

            items.append(entry)

    return items

# ---------------- main ----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Lokales Snapshot-Manifest erzeugen (Schema v3).")

    ap.add_argument("--root", required=True, help="Lokales Quellverzeichnis (z. B. ein RTB-Snapshot)")
    ap.add_argument("--snapshot", help="Snapshot-Name (Default: YYYYmmdd-HHMMSS)")
    ap.add_argument("--out", help="Manifest-Zieldatei (JSON). Default: stdout")
    
    # Smart-Mode (NEU in Schema v3)
    ap.add_argument("--ref-manifest", help="Referenz-Manifest für Smart-Mode (mtime/size-Cache, 40× schneller)")

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
    
    # Items sammeln (mit optionalem Cache)
    items = walk(
        root,
        snap,
        hash_algo=hash_algo,
        follow_symlinks=bool(args.follow_symlinks),
        follow_hardlinks=bool(args.follow_hardlinks),
        store_hardlink_target=bool(args.store_hardlink_target),
        store_symlink_target=bool(args.store_symlink_target),
        ref_cache=ref_cache,
    )
    
    # Schema 3 wenn Smart-Mode, sonst Schema 2 (backward compat)
    schema_version = 3 if ref_cache else 2
    mode = "smart" if ref_cache else "full"
    
    # total_files VOR if-Block berechnen (nicht nur im ref_cache-Block!)
    total_files = sum(1 for it in items if it.get("type") == "file")
    
    payload: Dict[str, Any] = {
        "schema": schema_version,
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
    
    # Schema 3 Erweiterungen
    if ref_cache:
        payload["mode"] = mode
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
        _log(f"[stats] total={total_files} | mode=full (kein Cache)")
    
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
