#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_push_json_manifest_to_pcloud.py

Lädt ein lokales Manifest (v2) nach pCloud.

Zwei Betriebsarten:
- --snapshot-mode objects  : Hash-Object-Store + JSON-Stubs (effizient, globale Dedupe).
- --snapshot-mode 1to1     : 1:1-Snapshot-Bäume; erstes Auftreten eines Inhalts wird "materialisiert"
                             (echte Datei im Snapshot), weitere Hardlinks als Stubs.
                             Content-Index in _snapshots/_index/content_index.json.

Erwartetes Manifest (schema=2):
{
  "schema": 2,
  "snapshot": "YYYY-mm-dd-HHMMSS" oder ähnlich,
  "root": "/abs/pfad/zum/snapshot",
  "hash": "sha256",
  "follow_symlinks": bool,
  "follow_hardlinks": bool,
  "items": [
    {"type":"dir","relpath":"..."},
    {"type":"file","relpath":"...", "size":..., "mtime":..., "source_path":"...", "sha256":"...", "ext":".txt",
     "inode": {"dev": 2049, "ino": 228196364, "nlink": 3}}
    ...
  ]
}

Benötigt: pcloud_bin_lib.py im selben Verzeichnis oder PYTHONPATH.
"""

from __future__ import annotations
import os, sys, json, argparse, time, datetime
import concurrent.futures
from typing import Dict, Any, Optional, Tuple


# ---- Logging mit Timestamp (RTB-Stil) ----
def _log(msg: str, *, file=sys.stderr) -> None:
    """Log-Ausgabe mit Timestamp"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", file=file, flush=True)


# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"Fehler: pcloud_bin_lib konnte nicht importiert werden: {e}", file=sys.stderr)
    sys.exit(2)


# Performance-Messung
fid_cache = {}
fid_lookups = 0          # Anzahl _fid_for Aufrufe
fid_cache_hits = 0       # Treffer im Cache
fid_rest_ms = 0.0        # aufsummierte Zeit in pc.resolve_fileid_cached
t_phase_start = time.time()

# --- shared fileid cache for this process ---
_fid_cache_shared: dict = {}

# --- Metrics (Prometheus-freundlich) ---
MET_UPLOADED_FILES = 0
MET_RESUMED_FILES  = 0
MET_STUBS_WRITTEN  = 0
MET_PROMOTED       = 0
MET_REMOVED_NODES  = 0
MET_API_RETRIES    = int(os.environ.get("PCLOUD_API_RETRIES", "0"))  # optional Zähler aus Lib/Wrapper

# ----------------- Utilities -----------------

def _ensure_parent(cfg, remote_path: str, *, dry: bool = False) -> None:
    """
    Stellt sicher, dass alle Elternordner für `remote_path` existieren.
    Delegiert vollständig an pcloud_bin_lib.ensure_parent_dirs(...).
    """
    if dry:
        return
    pc.ensure_parent_dirs(cfg, remote_path)


def write_hardlink_stub_1to1(cfg, snapshots_root, snapshot_name, relpath, file_item, node, dry=False):
    """
    Schreibt die .meta.json für einen 1:1-Hardlink-Stub und sorgt dafür,
    dass 'fileid' (falls möglich) gesetzt und im Index-Node mitgeführt wird.
    """
    meta_path = f"{snapshots_root.rstrip('/')}/{snapshot_name}/{relpath}.meta.json"

    # Ordner sicherstellen (nur via Lib-Helper)
    _ensure_parent(cfg, meta_path, dry=dry)

    # fileid nachziehen, falls im Node noch None
    fileid = node.get("fileid")
    if not fileid and node.get("anchor_path"):
        fid = pc.resolve_fileid_cached(cfg, path=node.get("anchor_path"), cache=_fid_cache_shared)
        if fid:
            fileid = fid
            node["fileid"] = fid

    payload = {
        "type":   "hardlink",
        "sha256": (file_item.get("sha256") or "").lower(),
        "size":   int(file_item.get("size") or 0),
        "mtime":  float(file_item.get("mtime") or 0.0),
        "inode":  {
            "dev":   int(((file_item.get("inode") or {}).get("dev")  or 0)),
            "ino":   int(((file_item.get("inode") or {}).get("ino")  or 0)),
            "nlink": int(((file_item.get("inode") or {}).get("nlink") or 1)),
        },
        "anchor_path": node.get("anchor_path"),
        "fileid": fileid if fileid is not None else None,
        "snapshot": snapshot_name,
        "relpath": relpath,
    }

    if dry:
        print(f"[dry] stub: {meta_path} -> {node.get('anchor_path')}")
    else:
        pc.write_json_at_path(cfg, path=meta_path, obj=payload)

    # Metrics
    globals()["MET_STUBS_WRITTEN"] += 1

    # holders[] pflegen
    holders = node.setdefault("holders", [])
    h = {"snapshot": snapshot_name, "relpath": relpath}
    if h not in holders:
        holders.append(h)

    return meta_path, payload


def stat_file_safe(cfg: dict, *, path: Optional[str]=None, fileid: Optional[int]=None) -> Optional[dict]:
    """Stat-Datei; gibt None bei 'not found' zurück (anstatt Exception)."""
    try:
        if path is not None:
            md = pc.stat_file(cfg, path=pc._norm_remote_path(path), with_checksum=False, enrich_path=True)
        else:
            md = pc.stat_file(cfg, fileid=int(fileid), with_checksum=False, enrich_path=True)
        if not md or md.get("isfolder"):
            return None
        return md
    except Exception:
        return None
def ensure_parent_dirs(cfg: dict, remote_path: str, *, dry: bool=False) -> None:
    """Sorgt dafür, dass alle Ordner bis zum parent von remote_path existieren."""
    p = pc._norm_remote_path(remote_path)
    parent = p.rsplit("/", 1)[0] or "/"
    if dry:
        return
    pc.ensure_path(cfg, parent)

def upload_json_stub(cfg: dict, remote_path: str, payload: dict, *, dry: bool=False) -> None:
    if dry:
        target = payload.get("object_path") or payload.get("anchor_path") or payload.get("sha256")
        print(f"[dry] stub: {remote_path} -> {target}")
        return
    pc.ensure_parent_dirs(cfg, remote_path)
    pc.write_json_at_path(cfg, remote_path, payload)

def _bytes_to_tempfile(b: bytes) -> str:
    import tempfile
    fd, p = tempfile.mkstemp(prefix="pcloud_stub_", suffix=".json")
    with os.fdopen(fd, "wb") as f:
        f.write(b)
    return p

def object_path_for(objects_root: str, sha256: str, ext: Optional[str], layout: str="two-level") -> str:
    """Pfad im Object-Store. layout='two-level' legt /_objects/xx/sha.ext an."""
    sha = (sha256 or "").lower()
    if not sha or len(sha) < 2:
        sub = "zz"
    else:
        sub = sha[:2]
    e = (ext or "").lstrip(".")
    tail = sha if not e else (sha + "." + e)
    return f"{objects_root.rstrip('/')}/{sub}/{tail}"

def snapshot_path_for(snapshots_root: str, snapshot: str, relpath: str) -> str:
    return f"{snapshots_root.rstrip('/')}/{snapshot}/{relpath}".replace("//", "/")

def stub_path_for(snapshots_root: str, snapshot: str, relpath: str) -> str:
    return f"{snapshots_root.rstrip('/')}/{snapshot}/{relpath}.meta.json".replace("//", "/")

def key_from_inode(item: dict) -> Optional[str]:
    """Erzeugt einen Key für Hardlink-Gruppierung; None wenn kein inode."""
    ino = (item.get("inode") or {})
    dev = ino.get("dev"); n = ino.get("ino")
    if dev is None or n is None:
        return None
    return f"{dev}:{n}"

def _compute_snapshot_stub_ratio(index: dict, snapshot_name: str) -> tuple:
    """
    Analysiert den lokalen Master-Index und berechnet die Stub-Ratio
    für einen gegebenen Snapshot – OHNE API-Calls, rein lokal, O(n).

    Ein Node "gehört" zu snapshot_name wenn:
      a) anchor_path den Snapshot-Namen enthält (→ echte Datei / Anchor)
      b) Ein Holder-Eintrag mit snapshot == snapshot_name existiert (→ Stub)

    Returns: (total, stubs, stub_ratio)
      total      = Anzahl Dateien, die in diesem Snapshot existieren
      stubs      = Davon Stubs (d.h. Holder, aber NICHT Anchor)
      stub_ratio = stubs / total (0.0 bis 1.0)
    """
    items = (index.get("items") or {})
    total = 0
    stub_count = 0

    for sha, node in items.items():
        anchor_path = node.get("anchor_path") or ""

        # Snapshot-Name aus anchor_path extrahieren:
        # Format: /.../_snapshots/YYYY-MM-DD-HHMMSS/relpath
        # → Segment nach "_snapshots/" ist der Snapshot-Name
        anchor_snap = ""
        if "/_snapshots/" in anchor_path:
            try:
                anchor_snap = anchor_path.split("/_snapshots/")[1].split("/")[0]
            except (IndexError, AttributeError):
                anchor_snap = ""

        # Prüfe ob Node in diesem Snapshot vorkommt (als Anchor ODER Holder)
        is_anchor = (anchor_snap == snapshot_name)
        is_holder = any(
            isinstance(h, dict) and h.get("snapshot") == snapshot_name
            for h in (node.get("holders") or [])
        )

        if is_anchor or is_holder:
            total += 1
            if not is_anchor:  # Holder aber kein Anchor → Stub
                stub_count += 1

    ratio = stub_count / total if total > 0 else 0.0
    return total, stub_count, ratio


def save_content_index_local(local_path: str, index: dict) -> None:
    """Speichert den Index lokal als JSON."""
    import tempfile
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    # Atomic write via tempfile
    dir_path = os.path.dirname(local_path)
    with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False, suffix='.tmp') as f:
        json.dump(index, f, separators=(',', ':'))
        temp_path = f.name
    os.replace(temp_path, local_path)

def load_content_index_local(local_path: str) -> dict:
    """Lädt den Index lokal, falls vorhanden."""
    try:
        with open(local_path, 'r') as f:
            j = json.load(f)
        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j
    except FileNotFoundError:
        return {"version": 1, "items": {}}
    except Exception:
        return {"version": 1, "items": {}}

def load_content_index(cfg: dict, snapshots_root: str) -> dict:
    """
    Lädt _snapshots/_index/content_index.json robust.
    - Wenn Datei fehlt/kaputt: leeren Index zurückgeben.
    - Ein 'result'≠0 im JSON gilt als API-Fehler (dann leerer Index).
    - Fehlt 'result' völlig (Normalfall bei echter Index-Datei) → OK.
    """
    idx_path = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = json.loads(txt)

        # Nur als API-Fehler werten, wenn 'result' vorhanden *und* != 0
        if isinstance(j, dict) and "result" in j and j.get("result") != 0:
            return {"version": 1, "items": {}}

        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j
    except Exception:
        return {"version": 1, "items": {}}

def save_content_index(cfg: dict, snapshots_root: str, index: dict, *, dry: bool=False) -> None:
    """
    content_index.json effizient schreiben:
    - ohne erneutes ensure()
    - minified JSON
    """
    idx_dir  = f"{snapshots_root.rstrip('/')}/_index"
    idx_name = "content_index.json"

    if dry:
        print(f"[dry] write index: {idx_dir}/{idx_name} (items={len(index.get('items',{}))})")
        return

    # Ordner muss existieren (wurde vorher per Batch-Ensure angelegt)
    fid = pc.stat_folderid_fast(cfg, idx_dir)
    if not fid:
        # sehr selten: Fallback (legt an und holt folderid)
        fid = pc.ensure_path(cfg, idx_dir)

    # Pretty-Print via ENV steuerbar
    pretty = os.environ.get("PCLOUD_PRETTY_JSON", "0") == "1"
    pc.write_json_to_folderid(cfg, folderid=int(fid), filename=idx_name, obj=index, minify=(not pretty))

def list_remote_snapshot_names(cfg: dict, snapshots_root: str) -> set[str]:
    """Liest die Ordnernamen unter <snapshots_root> (außer '_index')."""
    out: set[str] = set()
    try:
        top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False)
        for it in (top.get("metadata", {}) or {}).get("contents", []) or []:
            if it.get("isfolder") and it.get("name") and it.get("name") != "_index":
                out.add(it["name"])
    except Exception:
        pass
    return out

def list_local_snapshot_names(manifest_root: str) -> set[str]:
    """Liest Geschwister-Ordner des gegebenen Snapshot-Roots (RTB-Stil)."""
    base = os.path.dirname(os.path.abspath(manifest_root))  # parent von ".../<snapshot>"
    names = set()
    try:
        for n in os.listdir(base):
            p = os.path.join(base, n)
            if os.path.isdir(p) and n not in ("latest",):
                names.add(n)
    except Exception:
        pass
    return names


def finalize_index_fileids(cfg, snapshots_root):
    """
    Lädt <snapshots_root>/_index/content_index.json und füllt fehlende fileids
    (für Nodes mit anchor_path) via REST /stat nach. Schreibt nur bei Änderungen.
    Return: Anzahl reparierter Einträge.
    """
    start = time.time()

    idx_path = f"{pc._norm_remote_path(snapshots_root).rstrip('/')}/_index/content_index.json"
    try:
        index = json.loads(pc.get_textfile(cfg, path=idx_path))
    except Exception:
        return 0
    if not isinstance(index, dict):
        return 0

    items = index.get("items", {})
    if not isinstance(items, dict) or not items:
        return 0

    repaired = 0
    changed  = False

    # Gemeinsamer Cache mit dem Modul-Cache teilen:
    global _fid_cache_shared
    cache = _fid_cache_shared

    for sha, node in list(items.items()):
        if not isinstance(node, dict):
            continue
        if (node.get("fileid") in (None, "")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=cache)
            if fid:
                node["fileid"] = fid
                repaired += 1
                changed = True

    if changed:
        try:
            pc.put_textfile(cfg, path=idx_path, text=json.dumps(index, ensure_ascii=False, indent=2))
        except Exception:
            pc.write_json_at_path(cfg, path=idx_path, obj=index)

    if os.environ.get("PCLOUD_TIMING") == "1":
        print(f"[timing] finalize_index_fileids: fixed={repaired}, total={time.time()-start:.2f}s")

    return repaired

def _batch_ensure_paths(cfg: dict, paths: list[str], *, dry: bool = False) -> None:
    """
    Batch-Version von ensure_parent_dirs für mehrere Pfade.
    Nutzt createfolderrecursive (ein Call pro Parent-Kette).
    """
    if not paths:
        return

    # eindeutige Parents sammeln
    parents = { os.path.dirname(p.rstrip("/")) for p in paths if p }
    # stabile Reihenfolge (kann helfen beim Debug)
    parents = sorted(parents)

    for parent in parents:
        try:
            pc.ensure_path(cfg, parent, dry=dry)
        except Exception:
            # nicht hart abbrechen – idempotent, nächste versuchen
            continue

def _build_folder_cache_from_tree(cfg: dict, root_path: str) -> dict[str, int]:
    """
    Lädt Ordner-Struktur via listfolder (recursive=True, nofiles=True)
    und baut eine Map: {normalized_path: folderid}
    
    Performance:
      - 1× listfolder (recursive) API-Call
      - Statt N× ensure_path/stat für Parent-FolderID-Lookups
      - Typisch: 1 Call statt 1,000+ Calls (999x Reduktion)
    
    Returns:
        dict mapping normalized paths to folderids
        
    Example:
        cache = _build_folder_cache_from_tree(cfg, "/My Cloud/_snapshots/2026-04-17-120000")
        # cache = {"/My Cloud/_snapshots/2026-04-17-120000": 12345,
        #          "/My Cloud/_snapshots/2026-04-17-120000/dir1": 12346, ...}
    """
    try:
        result = pc.listfolder(cfg, path=root_path, recursive=True, nofiles=True)
    except Exception as e:
        # Root existiert noch nicht (erstes Upload) oder Fehler → leere Map
        if "2005" in str(e) or "not found" in str(e).lower():
            return {}
        # Bei anderen Fehlern auch leere Map (defensiv, kein Abbruch)
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[warn] listfolder für Folder-Cache fehlgeschlagen: {e}")
        return {}
    
    cache = {}
    
    def _traverse(node, parent_path=""):
        """Rekursiv alle Ordner aus dem Tree extrahieren"""
        if not isinstance(node, dict):
            return
        
        # Nur Ordner interessieren uns
        if not node.get("isfolder"):
            return
        
        folder_name = node.get("name", "")
        folderid = node.get("folderid")
        
        # Pfad konstruieren
        if parent_path:
            full_path = f"{parent_path}/{folder_name}"
        else:
            # Root-Node: verwende den übergebenen Pfad
            full_path = root_path
        
        # Normalisieren (wichtig für Map-Lookup!)
        normalized = pc._norm_remote_path(full_path)
        
        if folderid:
            cache[normalized] = int(folderid)
        
        # Rekursiv in Kinder eintauchen
        for child in node.get("contents") or []:
            _traverse(child, full_path)
    
    # Start mit metadata (Root-Ordner)
    metadata = result.get("metadata")
    if metadata:
        _traverse(metadata, parent_path="")
    
    return cache

def _batch_write_stubs(cfg: dict, stubs: list[tuple[str, dict]], *, dry: bool = False) -> None:
    """
    Schreibt gesammelte Stubs (.meta.json) in ihre Zielordner (parent folderid + filename).
    'stubs' ist eine Liste von Tuples: (remote_stub_path, payload_dict)
    Pretty-Print via ENV: PCLOUD_PRETTY_JSON=1
    Erweitert Payload um menschenlesbare Felder: format_version, kind, holder_type, mtime_iso
    """
    import datetime
    import threading

    if not stubs:
        return

    pretty = os.environ.get("PCLOUD_PRETTY_JSON", "0") == "1"
    
    # Progress-Tracking für Stub-Writing (thread-safe)
    _stubs_written = 0
    _stubs_failed = 0
    _stubs_lock = threading.Lock()
    _progress_interval = int(os.environ.get("PCLOUD_STUB_PROGRESS_INTERVAL", "500"))
    _last_progress_pct = 0

    # 1) nach Parent gruppieren
    by_parent: dict[str, list[tuple[str, dict]]] = {}
    for stub_path, payload in stubs:
        parent = os.path.dirname(stub_path.rstrip("/"))
        name = os.path.basename(stub_path)
        by_parent.setdefault(parent, []).append((name, payload))

    # 2) parent-fids auflösen (optimiert: listfolder + selective ensure)
    parent_fids: dict[str, int] = {}
    _total_parents = len(by_parent)
    _cache_hits = 0
    _cache_misses = 0
    _api_calls = 0
    
    # 2a) Batch-Lookup: Lade existierende Ordner-Struktur (1 API-Call)
    #     Extrahiere Snapshot-Root aus erstem Parent-Pfad
    if not dry and by_parent:
        # Snapshot-Root ermitteln (z.B. /My Cloud/_snapshots/2026-04-17-120000)
        first_parent = next(iter(by_parent.keys()))
        # Format: /.../snapshots_root/snapshot_name/... → extrahiere bis snapshot_name
        parts = first_parent.split("/")
        snapshot_root = None
        # Finde _snapshots Index
        try:
            snapshots_idx = parts.index("_snapshots")
            # snapshot_root = alles bis einschließlich snapshot_name (snapshots_idx + 2)
            if len(parts) > snapshots_idx + 1:
                snapshot_root = "/".join(parts[:snapshots_idx + 2])
            else:
                # Zu flache Struktur (kein snapshot_name nach _snapshots)
                _log(f"[stubs][WARN] Snapshot-Root-Extraktion fehlgeschlagen (zu flach): {first_parent}")
                snapshot_root = None
        except (ValueError, IndexError):
            # _snapshots nicht im Pfad gefunden (unerwartete Struktur)
            _log(f"[stubs][WARN] '_snapshots' nicht im Pfad gefunden: {first_parent}")
            snapshot_root = None
        
        # Cache-Build nur wenn snapshot_root valide ist
        if snapshot_root:
            _log(f"[stubs] Lade Ordner-Struktur via listfolder: {snapshot_root}")
            t_cache_start = time.time()
            folder_cache = _build_folder_cache_from_tree(cfg, snapshot_root)
            t_cache_ms = (time.time() - t_cache_start) * 1000.0
            _api_calls += 1  # Ein listfolder-Call
            if folder_cache:
                _log(f"[stubs] ✓ Folder-Cache geladen: {len(folder_cache)} Ordner in {t_cache_ms:.0f}ms")
            else:
                _log(f"[stubs][WARN] Folder-Cache leer nach listfolder (Snapshot existiert noch nicht?)")
        else:
            # Snapshot-Root ungültig → Skip Cache-Build (Legacy-Mode wird unten aktiviert)
            _log(f"[stubs][WARN] Überspringe Cache-Build (ungültige snapshot_root)")
            folder_cache = {}
    else:
        folder_cache = {}
    
    # 2b) Fallback-Detection: Wenn Cache leer ABER viele Parents → Legacy-Mode
    _use_legacy_mode = False
    if not dry and not folder_cache and _total_parents > 10:
        _log(f"[stubs][WARN] Folder-Cache leer ({len(folder_cache)} Einträge) trotz {_total_parents} Parents")
        _log(f"[stubs][WARN] → Fallback zu Legacy-Mode (sequential ensure_path)")
        _log(f"[stubs][WARN] → Erwartet: ~{int(_total_parents * 0.5 / 60)}min statt <5s")
        _use_legacy_mode = True
    
    # 2c) Parent-FIDs: Cache-Lookup (optimiert) oder Legacy-Mode (sequential)
    if _use_legacy_mode:
        _log(f"[stubs] Löse {_total_parents} Parent-FolderIDs auf (Legacy-Modus: sequential ensure_path)...")
    else:
        _log(f"[stubs] Löse {_total_parents} Parent-FolderIDs auf (Cache-Optimiert: {len(folder_cache)} gecacht)...")
    
    for parent in by_parent.keys():
        if dry:
            # im Dry-Run keine REST-Lookups; fiktive fid
            parent_fids[parent] = 0
            continue
        
        # Normalisieren für Cache-Lookup
        normalized_parent = pc._norm_remote_path(parent)
        
        # Cache-Lookup (O(1))
        if normalized_parent in folder_cache:
            parent_fids[parent] = folder_cache[normalized_parent]
            _cache_hits += 1
        else:
            # Cache-Miss: Ordner existiert noch nicht → anlegen
            try:
                fid = pc.ensure_path(cfg, path=parent)
                parent_fids[parent] = int(fid)
                folder_cache[normalized_parent] = int(fid)  # Cache updaten
                _cache_misses += 1
                _api_calls += 1  # Zähle ensure_path als API-Call
            except Exception as e:
                # Bei 2004 (Already exists): FolderID via stat nachziehen
                if "2004" in str(e):
                    try:
                        fid = pc.stat_folderid_fast(cfg, parent)
                        if fid:
                            parent_fids[parent] = int(fid)
                            folder_cache[normalized_parent] = int(fid)
                            _cache_misses += 1
                            _api_calls += 1
                            if os.environ.get("PCLOUD_VERBOSE") == "1":
                                _log(f"[info] Folder {parent} existiert bereits (2004) - FolderID via stat geholt: {fid}")
                        else:
                            _log(f"[warn] Folder {parent} existiert (2004), aber FolderID nicht auflösbar - Stubs werden übersprungen")
                    except Exception as e2:
                        _log(f"[warn] cannot resolve folderid for {parent}: {e} (fallback failed: {e2})")
                else:
                    _log(f"[warn] cannot resolve/ensure folderid for {parent}: {e}")
    
    # Performance-Report
    if not dry:
        _speedup = (_total_parents / _api_calls) if _api_calls > 0 else 0
        _log(f"[stubs] ✓ Parent-FIDs aufgelöst: {_cache_hits} Cache-Hits, {_cache_misses} neu angelegt")
        _log(f"[stubs] ✓ API-Calls: {_api_calls} (statt {_total_parents}) → {_speedup:.0f}x Reduktion")

    # 3) Schreibjobs bauen (nur Parents mit bekannter fid) + Payload anreichern
    tasks: list[tuple[str, str, dict]] = []
    for parent, items in by_parent.items():
        if parent not in parent_fids:
            continue
        for name, payload in items:
            # Payload "menschenfreundlich" erweitern (restore bleibt kompatibel)
            if "format_version" not in payload:
                payload["format_version"] = 1
            if "kind" not in payload:
                payload["kind"] = "stub"
            if "holder_type" not in payload and payload.get("type") == "hardlink":
                payload["holder_type"] = "hardlink"
            
            # mtime_iso hinzufügen falls mtime vorhanden
            mtime = payload.get("mtime")
            if mtime and "mtime_iso" not in payload:
                try:
                    payload["mtime_iso"] = datetime.datetime.fromtimestamp(float(mtime), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    pass
            
            tasks.append((parent, name, payload))

    if not tasks:
        return

    threads = int(os.environ.get("PCLOUD_STUB_THREADS", "4") or "4")
    total_tasks = len(tasks)
    
    # Start-Meldung
    _log(f"[stubs] Starte Batch-Write: {total_tasks} Stubs mit {threads} Threads...")

    def _upload_one(args: tuple[str, str, dict]):
        nonlocal _stubs_written, _stubs_failed, _last_progress_pct
        parent, name, payload = args
        
        if dry:
            # Pretty-Print auch im Dry-Run für Debug
            if pretty:
                txt = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                print(f"[dry] stub write: {parent}/{name}\n{txt}")
            else:
                print(f"[dry] stub write: {parent}/{name}")
            return True
        
        # Retry-Logik für robuste Stub-Writes (Timeout-Protection)
        try:
            ret = pc.call_with_backoff(
                pc.write_json_to_folderid,
                cfg,
                folderid=parent_fids[parent],
                filename=name,
                obj=payload,
                minify=(not pretty),
                attempts=5,
                max_sleep=30.0
            )
        except Exception as e:
            with _stubs_lock:
                _stubs_failed += 1
            _log(f"[warn] Stub-Write fehlgeschlagen ({_stubs_failed}): {parent}/{name}: {e}")
            return False
        
        # --- metriken: nur bei erfolgreichem write inkrementieren
        try:
            if ret:
                globals()["MET_STUBS_WRITTEN"] += 1
        except Exception:
            pass
        
        # Progress-Tracking (thread-safe)
        with _stubs_lock:
            _stubs_written += 1
            current_pct = int((_stubs_written / total_tasks) * 100)
            
            # Alle _progress_interval Stubs ODER bei Prozent-Änderung (10%, 20%, ...)
            show_progress = (
                _stubs_written % _progress_interval == 0 or 
                _stubs_written == total_tasks or
                (current_pct % 10 == 0 and current_pct != _last_progress_pct)
            )
            
            if show_progress:
                _last_progress_pct = current_pct
                eta_per_stub = 0.5  # Schätzung: ~0.5s pro Stub
                remaining = (total_tasks - _stubs_written) * eta_per_stub / threads
                eta_str = f"~{int(remaining/60)}min" if remaining > 60 else f"~{int(remaining)}s"
                _log(f"[stubs] {_stubs_written}/{total_tasks} ({current_pct}%) | {eta_str} verbleibend")
        
        return ret

    if threads > 1 and len(tasks) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
            list(ex.map(_upload_one, tasks))
    else:
        for t in tasks:
            _upload_one(t)
    
    # Abschluss-Meldung mit Fehler-Statistik
    if _stubs_failed > 0:
        _log(f"[warn] {_stubs_failed} Stubs fehlgeschlagen (von {total_tasks})")
    _log(f"[stubs] ✓ {_stubs_written}/{total_tasks} Stubs erfolgreich ({(_stubs_written/total_tasks*100):.1f}%)")

# ----------------- Haupt-Logik -----------------

def push_objects_mode(cfg: dict, manifest: dict, dest_root: str, *, dry: bool, objects_layout: str="two-level") -> None:
    """Hash-Object-Store + Stubs in Snapshot."""
    objects_root   = f"{dest_root.rstrip('/')}/_objects"
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    snapshot       = manifest["snapshot"]
    items          = manifest.get("items") or []

    uploaded = 0; skipped = 0; stubs = 0

    _log(f"[plan] objects={objects_root} snapshot={snapshots_root}/{snapshot}")

    # 1) echte Objekte sicherstellen
    for it in items:
        if it.get("type") != "file": continue
        sha = it.get("sha256")
        ext = (it.get("ext") or "").lstrip(".")
        if not sha:
            print(f"[warn] file ohne sha256: {it.get('relpath')}", file=sys.stderr)
            continue

        obj_path = object_path_for(objects_root, sha, ext, layout=objects_layout)
        md = stat_file_safe(cfg, path=obj_path)
        if md:
            skipped += 1
        else:
            if dry:
                print(f"[dry] upload object: {obj_path}  <- {it.get('source_path')}")
            else:
                ensure_parent_dirs(cfg, obj_path, dry=False)
                pc.upload_streaming(cfg, it["source_path"], dest_path=obj_path, filename=os.path.basename(obj_path))
            uploaded += 1

    print(f"objects: uploaded={uploaded} skipped={skipped}")

    # 2) Snapshot-Stubs erzeugen
    for it in items:
        if it.get("type") != "file": continue
        sha = it.get("sha256")
        ext = (it.get("ext") or "").lstrip(".")
        obj_path = object_path_for(objects_root, sha, ext, layout=objects_layout)
        stub_remote = stub_path_for(snapshots_root, snapshot, it["relpath"])
        payload = {
            "type": "link",
            "sha256": sha,
            "size": it.get("size"),
            "mtime": it.get("mtime"),
            "object_path": obj_path,
            "ext": ext or None,
            "inode": it.get("inode"),
            "snapshot": snapshot,
            "relpath": it.get("relpath"),
        }
        upload_json_stub(cfg, stub_remote, payload, dry=dry)
        stubs += 1

    print(f"stubs: {stubs} (snapshot={snapshot})")


def ensure_snapshots_layout(cfg: dict, dest_root: str, *, dry: bool = False) -> None:
    """
    Stellt sicher, dass <dest_root>/_snapshots und _snapshots/_index existieren
    und dass eine leere Index-Datei angelegt werden kann.
    """
    snapshots_root = f"{pc._norm_remote_path(dest_root).rstrip('/')}/_snapshots"
    index_dir = f"{snapshots_root}/_index"
    if dry:
        print(f"[dry] ensure: {snapshots_root}")
        print(f"[dry] ensure: {index_dir}")
        return
    pc.ensure_path(cfg, snapshots_root)
    pc.ensure_path(cfg, index_dir)

def push_1to1_mode(cfg, manifest, dest_root, *, dry=False, verbose=False, manifest_path=None):
    """
    1:1-Modus mit Resume-Unterstützung:
      - .upload_started Marker beim Start
      - .upload_complete Marker beim erfolgreichen Abschluss
      - Unvollständige Snapshots werden erkannt und neu gestartet
      - Nach Upload: Manifest-Archivierung (falls manifest_path gegeben)
    """
    t_phase_start = time.time()
    ensure_ms = 0.0
    upload_ms = 0.0
    write_ms  = 0.0

    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    
    # === Timeout-Protection für Mass-Uploads ===
    # Ensure minimum timeout (kritisch bei 19k+ Stub-Writes)
    if "timeout" not in cfg or cfg.get("timeout", 0) < 30:
        cfg["timeout"] = int(os.environ.get("PCLOUD_TIMEOUT", "60"))
        if os.environ.get("PCLOUD_VERBOSE") == "1":
            _log(f"[config] Timeout auf {cfg['timeout']}s gesetzt (Mass-Upload-Protection)")

    # === NEU: Upload-Status-Marker ===
    marker_started = f"{dest_snapshot_dir}/.upload_started"
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    
    # Prüfen ob unvollständiger Upload existiert
    incomplete_upload = False
    try:
        pc.stat_file(cfg, path=marker_started, with_checksum=False)
        # Started-Marker existiert
        try:
            pc.stat_file(cfg, path=marker_complete, with_checksum=False)
            # Complete-Marker auch da → Upload war erfolgreich
            _log(f"[info] Snapshot {snapshot_name} bereits vollständig hochgeladen")
            return {"uploaded": 0, "stubs": 0, "resumed": False}
        except:
            # Nur Started, kein Complete → unvollständig!
            incomplete_upload = True
            _log(f"[warn] Unvollständiger Upload erkannt für {snapshot_name} - starte neu")
    except:
        # Kein Started-Marker → frischer Upload
        pass
    
    # Bei unvollständigem Upload: Index-Driven Skip (keine Löschung)
    if incomplete_upload:
        _log(f"[resume] Setze Upload fort für {snapshot_name} (bereits verarbeitete Dateien werden übersprungen)")
    # === ENDE NEU ===

    _log(f"[plan] 1to1 snapshot={dest_snapshot_dir}")

    # === NEU: Started-Marker setzen ===
    if not dry:
        try:
            pc.call_with_backoff(pc.ensure_path, cfg, dest_snapshot_dir)
            pc.call_with_backoff(pc.put_textfile, cfg, path=marker_started,
                          text=json.dumps({
                              "snapshot": snapshot_name,
                              "started_at": time.time(),
                              "host": os.uname().nodename
                          }))
        except Exception as e:
            print(f"[warn] Konnte Started-Marker nicht setzen: {e}")
    # === ENDE NEU ===

    # --- kleine Helfer ---
    def _ensure(path: str) -> None:
        nonlocal ensure_ms
        if not path:
            return
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] ensure: {path}")
            return
        t0 = time.time()
        pc.call_with_backoff(pc.ensure_path, cfg, path)
        ensure_ms += (time.time() - t0) * 1000.0

    def _delete_if_exists(path: str) -> None:
        if dry:
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[dry] delete-if-exists: {path}")
            return
        try:
            md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=path) or {}
            fid = md.get("fileid")
            if fid:
                pc.delete_file(cfg, fileid=int(fid))
        except Exception:
            pass

    # Lokaler Index-Cache-Pfad (nur während Upload, wird am Ende hochgeladen)
    import tempfile
    _local_index_dir = os.getenv("PCLOUD_TEMP_DIR", tempfile.gettempdir())
    _local_index_path = os.path.join(_local_index_dir, f"pcloud_index_{snapshot_name}.json")
    os.makedirs(_local_index_dir, exist_ok=True)

    # Index laden: erst lokal (falls vorhanden), sonst von pCloud
    if os.path.exists(_local_index_path):
        _log(f"[resume] Lade lokalen Index: {_local_index_path}")
        index = load_content_index_local(_local_index_path)
    else:
        index = load_content_index(cfg, snapshots_root)
    items = index.setdefault("items", {})

    # Anchor-Cache aufbauen
    known_anchors = {}
    for sha, node in items.items():
        ap = node.get("anchor_path")
        fid = node.get("fileid")
        if ap and fid:
            known_anchors[sha] = (ap, fid)
    
    if known_anchors and os.environ.get("PCLOUD_VERBOSE") == "1":
        print(f"[prefetch] {len(known_anchors)} bekannte Anchors gecacht")

    # Hilfstabellen
    seen_inodes: dict[tuple[int,int], str] = {}
    uploaded = 0
    resumed = 0   # Bereits im Index für diesen Snapshot
    stubs = 0
    index_changed = False
    stubs_to_write: list[tuple[str, dict]] = []

    # --- Upload-Hilfsroutine ---
    def _upload_real_file(abs_src: str, dst_path: str) -> tuple:
        """Returns (fileid, pcloud_hash)"""
        nonlocal upload_ms
        parent = os.path.dirname(dst_path.rstrip("/"))
        if parent:
            _ensure(parent)
        if dry:
            print(f"[dry] upload 1to1: {dst_path}  <- {abs_src}")
            return (None, None)

        # Progress-Hinweis für große Dateien
        file_size = os.path.getsize(abs_src)
        if file_size > 100 * 1024**2:  # > 100MB
            print(f"[upload] Starte Upload: {os.path.basename(dst_path)} ({file_size/1024**2:.1f} MB)", flush=True)

        t0 = time.time()
        res = pc.call_with_backoff(pc.upload_file, cfg, local_path=abs_src, remote_path=dst_path, attempts=12, max_sleep=60.0)
        upload_ms += (time.time() - t0) * 1000.0

        # Metrics (Prometheus-freundlich), wenn definiert
        try:
            globals()["MET_UPLOADED_FILES"] += 1
        except Exception:
            pass

        # fileid + hash aus der Upload-Antwort
        try:
            md = (res or {}).get("metadata") or {}
            fileid = md.get("fileid")
            pcloud_hash = md.get("hash")  # pCloud's hash field
        except Exception:
            fileid = None
            pcloud_hash = None

        # Optional: Eager-FileID via stat, falls Upload keine liefert
        if (not fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
            try:
                stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=dst_path) or {}
                if not fileid:
                    fileid = stat_md.get("fileid")
                if not pcloud_hash:
                    pcloud_hash = stat_md.get("hash")
            except Exception:
                pass

        return (fileid, pcloud_hash)

    # --- Stub sammeln ---
    def _queue_stub(relpath: str, file_item: dict, node: dict) -> None:
        nonlocal stubs, index_changed

        eager = os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0"
        if eager and (not node.get("fileid")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=_fid_cache_shared)
            if fid:
                node["fileid"] = fid
                index_changed = True

        meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
        payload = {
            "type": "hardlink",
            "sha256": file_item.get("sha256"),
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "snapshot": snapshot_name,
            "relpath": relpath,
            "anchor_path": node.get("anchor_path"),
            "fileid": node.get("fileid") if node.get("fileid") is not None else None,
            "inode": file_item.get("inode"),
        }
        if dry:
            print(f"[dry] write stub: {meta_path}")
        else:
            stubs_to_write.append((meta_path, payload))
        stubs += 1

    # --- Hauptschleife: Items des Manifests ---
    _all_items = [it for it in (manifest.get("items") or []) if it.get("type") == "file"]
    _total_items = len(_all_items)
    _total_size = sum(it.get("size") or 0 for it in _all_items)
    _done_items = 0
    _done_size = 0
    _t_loop_start = time.time()
    _t_last_progress = _t_loop_start
    _PROGRESS_INTERVAL = float(os.environ.get("PCLOUD_PROGRESS_INTERVAL", "30"))
    _SAVE_INTERVAL = int(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL", "100"))
    _SAVE_INTERVAL_TIME = float(os.environ.get("PCLOUD_INDEX_SAVE_INTERVAL_TIME", "300"))  # 5min
    _last_saved_count = 0
    _t_last_index_save = time.time()
    _log(f"[push] Starte Upload: {_total_items} Dateien, {_total_size/1024**3:.2f} GB")

    # === Diff-basierte Ordner-Anlage (nur fehlende Ordner) ===
    # 1. Remote-Ordner sammeln via listfolder (ein API-Call, auch im Dry-Run)
    remote_folders = set()
    try:
        _log(f"[plan] Lade Remote-Ordnerstruktur: {dest_snapshot_dir}")
        result = pc.call_with_backoff(pc.listfolder, cfg, path=dest_snapshot_dir, recursive=True, nofiles=True)
        def _collect_folders(obj, parent_path=""):
            if isinstance(obj, dict) and obj.get("isfolder"):
                folder_name = obj.get("name", "")
                folder_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                remote_folders.add(folder_path)
                for child in obj.get("contents") or []:
                    _collect_folders(child, folder_path)
        # Direkt mit contents starten (nicht metadata selbst, das ist der Snapshot-Ordner)
        metadata = result.get("metadata") or {}
        for child in metadata.get("contents") or []:
            _collect_folders(child, "")
        _log(f"[plan] {len(remote_folders)} Remote-Ordner gefunden")
    except Exception as e:
        # Falls Snapshot-Ordner noch nicht existiert (erstes Upload) → okay
        if "2005" in str(e) or "not found" in str(e).lower():
            _log(f"[plan] Snapshot-Ordner existiert noch nicht (erstes Upload)")
        else:
            _log(f"[warn] listfolder fehlgeschlagen: {e}")
    
    # 2. Manifest-Ordner sammeln (leere relpaths filtern - das ist Root selbst)
    manifest_folders = set()
    for it in manifest.get("items") or []:
        if it.get("type") == "dir":
            relpath = it.get("relpath", "").rstrip("/")
            if relpath:  # Filter leere Strings (Root-Verzeichnis)
                manifest_folders.add(relpath)
    
    # 3. Differenz berechnen und nur fehlende Ordner anlegen
    missing_folders = manifest_folders - remote_folders
    if missing_folders:
        _log(f"[plan] Lege {len(missing_folders)} fehlende Ordner an (von {len(manifest_folders)} gesamt)")
        
        # Nach Tiefe gruppieren (Parents zuerst, dann parallel innerhalb Ebene)
        from collections import defaultdict
        import threading
        
        folders_by_depth = defaultdict(list)
        for reldir in missing_folders:
            depth = reldir.count("/")
            folders_by_depth[depth].append(reldir)
        
        max_depth = max(folders_by_depth.keys()) if folders_by_depth else 0
        threads = int(os.environ.get("PCLOUD_FOLDER_THREADS", "4"))
        
        _folders_created = 0
        _folders_lock = threading.Lock()
        _last_progress_pct = 0
        total_folders = len(missing_folders)
        
        def _create_folder(reldir: str) -> bool:
            nonlocal _folders_created, _last_progress_pct
            try:
                _ensure(f"{dest_snapshot_dir}/{reldir}")
                
                # Progress-Tracking (thread-safe)
                with _folders_lock:
                    _folders_created += 1
                    current_pct = int((_folders_created / total_folders) * 100)
                    
                    # Alle 100 Ordner ODER bei Prozent-Änderung (10%, 20%, ...)
                    show_progress = (
                        _folders_created % 100 == 0 or
                        _folders_created == total_folders or
                        (current_pct % 10 == 0 and current_pct != _last_progress_pct)
                    )
                    
                    if show_progress:
                        _last_progress_pct = current_pct
                        remaining_s = (total_folders - _folders_created) * 0.05 / threads
                        eta_str = f"~{int(remaining_s)}s" if remaining_s < 60 else f"~{int(remaining_s/60)}min"
                        _log(f"[folders] {_folders_created}/{total_folders} ({current_pct}%) | {eta_str} verbleibend")
                return True
            except Exception as e:
                print(f"[warn] Ordner-Anlage fehlgeschlagen für {reldir}: {e}", file=sys.stderr)
                return False
        
        # Ebenen nacheinander abarbeiten (innerhalb parallel)
        _log(f"[folders] {max_depth + 1} Ebenen, {threads} Threads pro Ebene")
        for depth in sorted(folders_by_depth.keys()):
            folders_at_depth = folders_by_depth[depth]
            
            if threads > 1 and len(folders_at_depth) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
                    list(ex.map(_create_folder, folders_at_depth))
            else:
                for folder in folders_at_depth:
                    _create_folder(folder)
        
        _log(f"[folders] ✓ {total_folders} Ordner erfolgreich angelegt")
    else:
        _log(f"[plan] Alle {len(manifest_folders)} Ordner existieren bereits")
    # === Ende Diff-basierte Ordner-Anlage ===

    for it in manifest.get("items") or []:
        if it.get("type") == "dir":
            # Ordner wurden bereits oben in Batch angelegt
            continue
        if it.get("type") != "file":
            continue

        _done_items += 1
        _done_size += it.get("size") or 0
        _now = time.time()
        if _now - _t_last_progress >= _PROGRESS_INTERVAL:
            _elapsed = _now - _t_loop_start
            _pct = _done_items / _total_items * 100 if _total_items else 0
            _pct_b = _done_size / _total_size * 100 if _total_size else 0
            _eta = (_elapsed / _done_size * (_total_size - _done_size)) if _done_size else 0
            _eta_str = f"~{int(_eta/60)}min" if _eta > 60 else f"~{int(_eta)}s"
            _log(
                f"[push] {_done_items}/{_total_items} ({_pct:.0f}%) | "
                f"{_done_size/1024**3:.2f}/{_total_size/1024**3:.2f} GB ({_pct_b:.0f}%) | "
                f"uploaded={uploaded} resumed={resumed} stubs={stubs} | {_eta_str} verbleibend"
            )
            _t_last_progress = _now

        relpath = it.get("relpath") or ""
        src_abs = it.get("source_path") or ""
        sha = it.get("sha256") or ""
        inode = it.get("inode") or {}
        dev = int(inode.get("dev") or 0)
        ino = int(inode.get("ino") or 0)
        ino_key = (dev, ino)

        dst_path = f"{dest_snapshot_dir}/{relpath}"

        node = items.setdefault(sha, {"holders": []})
        
        # === Index-Driven Skip: Prüfen ob bereits im Index für diesen Snapshot ===
        already_in_snapshot = any(
            h.get("snapshot") == snapshot_name and h.get("relpath") == relpath
            for h in node.get("holders", [])
        )
        if already_in_snapshot:
            # Bereits verarbeitet → skip
            # WICHTIG: Inode registrieren, damit weitere Hardlinks erkannt werden
            seen_inodes[ino_key] = relpath
            resumed += 1
            continue
        # === Ende Index-Driven Skip ===
        
        # --- NEU: Content-SHA auch als Feld im Node mitführen (Denormalisierung) ---
        # Der Index nutzt die SHA bereits als Key; das Feld erhöht Lesbarkeit/Tooling
        # und macht SHA-Checks im Quick-Checker robuster.
        if sha and node.get("sha256") != sha:
            node["sha256"] = sha
            index_changed = True
        
        if sha in known_anchors:
            anchor_path, anchor_fid = known_anchors[sha]
            if not node.get("anchor_path"):
                node["anchor_path"] = anchor_path
            if not node.get("fileid"):
                node["fileid"] = anchor_fid
        else:
            anchor_path = node.get("anchor_path") or ""
        
        is_anchor_here = (anchor_path == dst_path)

        if ino_key in seen_inodes:
            if not is_anchor_here:
                _queue_stub(relpath, it, node)
            else:
                _delete_if_exists(f"{dst_path}.meta.json")
            continue

        fid = None
        pcloud_hash = None
        if not anchor_path:
            fid, pcloud_hash = _upload_real_file(src_abs, dst_path)
            if node.get("anchor_path") != dst_path:
                node["anchor_path"] = dst_path
                index_changed = True
            if fid and node.get("fileid") != fid:
                node["fileid"] = fid
                index_changed = True
            if pcloud_hash and node.get("pcloud_hash") != pcloud_hash:
                node["pcloud_hash"] = pcloud_hash
                index_changed = True
            uploaded += 1
            _delete_if_exists(f"{dst_path}.meta.json")
        else:
            if is_anchor_here:
                resumed += 1
                _delete_if_exists(f"{dst_path}.meta.json")
            else:
                _queue_stub(relpath, it, node)

        h = {"snapshot": snapshot_name, "relpath": relpath}
        if h not in node["holders"]:
            node["holders"].append(h)
            index_changed = True

        seen_inodes[ino_key] = relpath

        # Periodisches lokales Index-Save (Hybrid: Anzahl ODER Zeit)
        _now_save = time.time()
        _count_trigger = _SAVE_INTERVAL > 0 and (uploaded + resumed + stubs) >= _last_saved_count + _SAVE_INTERVAL
        _time_trigger = _SAVE_INTERVAL_TIME > 0 and (_now_save - _t_last_index_save) >= _SAVE_INTERVAL_TIME
        if not dry and (_count_trigger or _time_trigger):
            save_content_index_local(_local_index_path, index)
            _last_saved_count = uploaded + resumed + stubs
            _t_last_index_save = _now_save
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                _reason = "count" if _count_trigger else "time"
                print(f"[index] Lokal gespeichert ({_reason}) nach {uploaded + resumed + stubs} Dateien")


    # --- Batch: Stubs & Index schreiben (einmaliges Ensure + Writes) ---
    if not dry and stubs_to_write:
        _log(f"[push] ✓ Loop abgeschlossen. Bereite Stub-Batch vor ({len(stubs_to_write)} Stubs)...")
        t0 = time.time()
        _batch_write_stubs(cfg, stubs_to_write, dry=False)  # sorgt intern für 1x Parent-Ensure
        write_ms += (time.time() - t0) * 1000.0


    # Index schreiben (lokal → pCloud → lokal löschen)
    if dry:
        print(f"[dry] write index: {snapshots_root}/_index/content_index.json (items={len(items)})")
    else:
        # Finaler lokaler Save (falls noch Änderungen seit letztem periodischen Save)
        if index_changed:
            save_content_index_local(_local_index_path, index)
            if os.environ.get("PCLOUD_VERBOSE") == "1":
                print(f"[index] Finaler lokaler Save vor Upload")
        
        # Index hochladen nach pCloud
        if os.path.exists(_local_index_path):
            t0 = time.time()
            save_content_index(cfg, snapshots_root, index, dry=False)
            dt_ms = (time.time() - t0) * 1000.0
            write_ms += dt_ms 
            print(f"[timing] index_write_ms={int(dt_ms)}")
            
            # Remote archivieren (Paranoia-Modus: Snapshot-isolierter Index für Recovery)
            try:
                idx_path = f"{snapshots_root}/_index/content_index.json"
                archive_path = f"{snapshots_root}/_index/archive/{snapshot_name}_index.json"
                pc.ensure_parent_dirs(cfg, archive_path)
                pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
                _log(f"[index] ✓ Content-Index remote archiviert: {archive_path}")
            except Exception as e:
                _log(f"[index][warn] Remote-Archivierung fehlgeschlagen: {e}")
            
            # Master-Index aktualisieren (alle Snapshots zusammen)
            try:
                master_index_path = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "indexes", "content_index_master.json")
                os.makedirs(os.path.dirname(master_index_path), exist_ok=True)
                save_content_index_local(master_index_path, index)
                _log(f"[index] ✓ Master-Index aktualisiert: {master_index_path}")
            except Exception as e:
                _log(f"[index][warn] Master-Index-Update fehlgeschlagen: {e}")
            
            # Lokale Index-Datei löschen
            try:
                os.remove(_local_index_path)
                if os.environ.get("PCLOUD_VERBOSE") == "1":
                    print(f"[index] Lokale Kopie gelöscht: {_local_index_path}")
            except Exception as e:
                print(f"[warn] Konnte lokale Index-Datei nicht löschen: {e}")
            
            # Manifest archivieren (falls Pfad gegeben und Upload erfolgreich)
            if manifest_path and not dry:
                try:
                    archive_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "manifests")
                    os.makedirs(archive_dir, exist_ok=True)
                    archive_path = os.path.join(archive_dir, f"{snapshot_name}.json")
                    
                    import shutil
                    shutil.copy2(manifest_path, archive_path)
                    _log(f"[archive] Manifest archiviert: {archive_path}")
                    
                    # Optional: Index auch archivieren
                    if os.environ.get("PCLOUD_ARCHIVE_INDEX") == "1":
                        index_archive_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "indexes")
                        os.makedirs(index_archive_dir, exist_ok=True)
                        index_src = os.path.join(_local_index_dir, f"pcloud_index_{snapshot_name}.json")
                        if os.path.exists(index_src):
                            index_archive_path = os.path.join(index_archive_dir, f"{snapshot_name}_index.json")
                            shutil.copy2(index_src, index_archive_path)
                            _log(f"[archive] Index archiviert: {index_archive_path}")
                except Exception as e:
                    print(f"[warn] Manifest-Archivierung fehlgeschlagen: {e}")
        else:
            print("[info] index unchanged (no write)")

    # FINALIZE
    if not dry:
        do_finalize = (os.environ.get("PCLOUD_SKIP_FINALIZE") in (None, "", "0"))
        if do_finalize and (uploaded > 0 or stubs > 0 or index_changed):
            try:
                finalize_index_fileids(cfg, snapshots_root)
            except Exception:
                pass

    # === NEU: Complete-Marker setzen ===
    if not dry:
        try:
            pc.put_textfile(cfg, path=marker_complete,
                          text=json.dumps({
                              "snapshot": snapshot_name,
                              "completed_at": time.time(),
                              "uploaded": uploaded,
                              "resumed": resumed,
                              "stubs": stubs
                          }))
            _log(f"[success] Upload-Complete-Marker gesetzt")
        except Exception as e:
            print(f"[warn] Konnte Complete-Marker nicht setzen: {e}")
    # === ENDE NEU ===

    if os.environ.get("PCLOUD_TIMING") == "1":
        total_ms = (time.time() - t_phase_start) * 1000.0
        print(f"[timing] push_1to1: total={total_ms/1000:.2f}s, ensure={ensure_ms:.0f}ms, upload={upload_ms:.0f}ms, writes={write_ms:.0f}ms")

    # Update global metrics
    globals()["MET_RESUMED_FILES"] += resumed

    print(f"1to1: uploaded={uploaded} resumed={resumed} stubs={stubs} (snapshot={snapshot_name})")
    return {"uploaded": uploaded, "resumed": resumed, "stubs": stubs}

def retention_sync_1to1(cfg, dest_root, *, local_snaps=None, dry=False, rewrite_stubs=True):
    """
    Retention/Prune für den 1:1-Modus, index-zentriert.

    Ablauf:
      - Remote-Snapshots unter <dest>/_snapshots mit lokalen (local_snaps) vergleichen.
      - Für jeden entfernten Remote-Snapshot:
          • Holders für gelöschte Snaps entfernen.
          • Liegt Anchor im gelöschten Snap:
              - Gibt es verbleibende Holder -> Anchor serverseitig in Pfad des jüngsten Holders moven,
                Index aktualisieren, Ziel-Stub entfernen, übrige Holder -> Stub (optional).
              - Keine Holder mehr -> Node entfernen.
          • Snapshot-Ordner löschen nur, wenn keine Blocker (z. B. fehlende fileid / Move-Fehler).
      - Index zuletzt schreiben (write-last), aber NUR wenn keine Blocker auftraten.
      - WICHTIG: am Anchor-Pfad gibt es KEINEN Stub; stale Stubs dort werden gelöscht.
    """
    # Timing / Metriken für Stub- und Index-Writes
    ret_stub_ms = 0.0
    ret_index_write_ms = 0.0
    ret_stub_writes = 0
    ret_index_changed = False

    # --- Hilfsfunktionen -----------------------------------------------------

    def _list_remote_snapshots(snapshots_root: str) -> list[str]:
        try:
            top = pc.listfolder(cfg, path=snapshots_root, recursive=False, nofiles=True, showpath=False) or {}
            contents = (top.get("metadata") or {}).get("contents") or []
            return sorted(c["name"] for c in contents if c.get("isfolder") and c.get("name") != "_index")
        except Exception:
            return []

    def _stat_fileid_safe(path: str):
        try:
            md = pc.stat_file(cfg, path=path, with_checksum=False) or {}
            return md.get("fileid")
        except Exception:
            return None

    def _load_index(snapshots_root: str) -> dict:
        idx_path = f"{snapshots_root}/_index/content_index.json"
        try:
            txt = pc.get_textfile(cfg, path=idx_path)
            j = json.loads(txt)
            if not isinstance(j, dict):
                j = {"version": 1, "items": {}}
        except Exception:
            j = {"version": 1, "items": {}}
        if "items" not in j or not isinstance(j["items"], dict):
            j["items"] = {}
        if "version" not in j:
            j["version"] = 1
        return j

    def _save_index(snapshots_root: str, idx: dict, simulate: bool):
        nonlocal ret_index_write_ms
        if simulate:
            print(f"[dry] save index: items={len(idx.get('items', {}))}")
        else:
            t0 = time.time()
            save_content_index(cfg, snapshots_root, idx, dry=False)
            dt = (time.time() - t0) * 1000.0
            ret_index_write_ms += dt
            if os.environ.get("PCLOUD_TIMING") == "1":
                print(f"[timing] retention_index_write_ms={int(dt)}")

    def _rewrite_stub(snapshots_root: str, snapshot: str, relpath: str, sha: str, new_anchor_path: str, fileid) -> None:
        """
        Stub-JSON effizient neu schreiben:
          - Parent-Folder per folderid (stat_folderid_fast/ensure_path)
          - Schreiben via write_json_to_folderid(..., minify=True)
          - Vorhandenes Stub-JSON (falls vorhanden) übernehmen/aktualisieren
        """
        nonlocal ret_stub_ms, ret_stub_writes
        
        # relpath in (Unter)ordner + Basisdatei splitten
        if "/" in relpath:
            stub_dir, base = relpath.rsplit("/", 1)
        else:
            stub_dir, base = "", relpath

        parent_dir = f"{snapshots_root.rstrip('/')}/{snapshot}"
        if stub_dir:
            parent_dir = f"{parent_dir}/{stub_dir}"
        filename = f"{base}.meta.json"
        meta_path = f"{parent_dir}/{filename}"

        if dry:
            print(f"[dry] rewrite stub: {meta_path} -> anchor={new_anchor_path}")
            return

        # 1) Parent-FolderID besorgen (ohne per-File ensure)
        fid = pc.stat_folderid_fast(cfg, parent_dir)
        if not fid:
            fid = pc.ensure_path(cfg, parent_dir)
        fid = int(fid)

        # 2) Vorhandenes Stub-JSON (best effort) laden
        try:
            old_txt = pc.get_textfile(cfg, path=meta_path)
            payload = json.loads(old_txt)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        # 3) Pflichtfelder setzen/aktualisieren
        payload.setdefault("type", "hardlink")
        payload["sha256"] = sha
        payload["relpath"] = relpath
        payload["snapshot"] = snapshot
        payload["anchor_path"] = new_anchor_path
        payload["fileid"] = fileid if fileid is not None else None

        # 4) Schreiben per folderid (minified)
        t0 = time.time()
        pc.write_json_to_folderid(cfg, folderid=fid, filename=filename, obj=payload, minify=True)
        dt = (time.time() - t0) * 1000.0
        ret_stub_ms += dt
        ret_stub_writes += 1

        if os.environ.get("PCLOUD_TIMING") == "1":
            print(f"[timing] retention_stub_write_ms={int(dt)} file={meta_path}")

    def _delete_file_if_exists(path: str) -> None:
        "Best-effort: löscht Datei (z. B. stale Stub) am Pfad, wenn vorhanden."
        if dry:
            print(f"[dry] delete-if-exists: {path}")
            return
        try:
            fid = _stat_fileid_safe(path)
            if fid:
                pc.delete_file(cfg, fileid=int(fid))
        except Exception:
            pass

    # --- Setup & Daten holen -------------------------------------------------

    ensure_snapshots_layout(cfg, dest_root, dry=dry)
    snapshots_root = f"{pc._norm_remote_path(dest_root).rstrip('/')}/_snapshots"

    remote_snaps = set(_list_remote_snapshots(snapshots_root))
    local_snaps = set(local_snaps or [])
    to_delete = sorted(s for s in remote_snaps if s not in local_snaps)
    keep_snaps = remote_snaps & local_snaps

    if not to_delete:
        if dry:
            print("[dry] retention: nichts zu löschen")
        return

    idx = _load_index(snapshots_root)
    items = idx.setdefault("items", {})

    promoted = 0
    removed_nodes = 0
    any_blockers = False

    # --- Hauptlogik pro zu löschendem Snapshot ------------------------------

    for sdel in to_delete:
        del_prefix = f"{snapshots_root}/{sdel}/"
        snapshot_blockers = False  # Pro-Snapshot Blocker-Flag

        for sha, node in list(items.items()):
            if not isinstance(node, dict):
                continue

            holders = list(node.get("holders") or [])
            anchor = node.get("anchor_path") or ""
            anchor_in_deleted = anchor.startswith(del_prefix)

            # Invariante: am Anchor-Pfad KEIN Stub (.meta.json) → best-effort Cleanup
            if anchor:
                _delete_file_if_exists(f"{anchor}.meta.json")

            # (A) Node ohne Holder, Anchor im gelöschten Snapshot -> Node weg
            if not holders and anchor_in_deleted:
                if dry:
                    print(f"[dry] drop node (no holders, anchor in {sdel}): {sha[:8]}…")
                else:
                    del items[sha]
                    removed_nodes += 1
                    ret_index_changed = True
                continue

            # (B) Holder splitten in keep/drop und im Node setzen
            keep_holders = [h for h in holders if h.get("snapshot") in keep_snaps]
            drop_holders = [h for h in holders if h.get("snapshot") in to_delete]
            if drop_holders or anchor_in_deleted:
                node["holders"] = keep_holders
                ret_index_changed = True

            # keine Keeper?
            if not keep_holders:
                if anchor_in_deleted:
                    if dry:
                        print(f"[dry] drop node (no keepers, anchor in {sdel}): {sha[:8]}…")
                    else:
                        del items[sha]
                        removed_nodes += 1
                continue

            # (C) Anchor liegt im gelöschten Snapshot -> Promotion (MOVE)
            if anchor_in_deleted:
                new_holder = max(keep_holders, key=lambda h: h.get("snapshot") or "")
                new_path = f"{snapshots_root}/{new_holder['snapshot']}/{new_holder['relpath']}"

                # No-Op-Guard
                if anchor == new_path:
                    node["anchor_path"] = new_path
                    # am Anchor KEIN Stub: ggf. stale Stub löschen
                    _delete_file_if_exists(f"{new_path}.meta.json")
                    # optional: Stubs der übrigen Holder neu schreiben
                    if rewrite_stubs:
                        for h in keep_holders:
                            if h is new_holder or (h["snapshot"] == new_holder["snapshot"] and h["relpath"] == new_holder["relpath"]):
                                _delete_file_if_exists(f"{snapshots_root}/{h['snapshot']}/{h['relpath']}.meta.json")
                                continue
                            _rewrite_stub(snapshots_root, h["snapshot"], h["relpath"], sha, node["anchor_path"], node.get("fileid"))
                    continue

                if dry:
                    print(f"[dry] promote (move) {sha[:8]}… {anchor} -> {new_path}")
                    node["anchor_path"] = new_path
                    promoted += 1
                else:
                    fid = node.get("fileid") or _stat_fileid_safe(anchor)
                    if not fid:
                        print(f"[warn] retention: fehlende fileid für Anchor {anchor}; Snapshot {sdel} wird NICHT gelöscht.", file=sys.stderr)
                        snapshot_blockers = True
                        any_blockers = True  # === NEU ===
                        continue

                    pc.ensure_parent_dirs(cfg, new_path)
                    # am Ziel darf kein Stub bleiben
                    _delete_file_if_exists(f"{new_path}.meta.json")

                    try:
                        pc.move(cfg, from_fileid=int(fid), to_path=new_path)
                    except Exception as e:
                        print(f"[warn] retention: move failed for fileid={fid} -> {new_path}: {e}", file=sys.stderr)
                        snapshot_blockers = True
                        any_blockers = True  # === NEU ===
                        continue

                    node["anchor_path"] = new_path
                    node["fileid"] = int(fid)
                    promoted += 1
                    ret_index_changed = True

                # übrige Holder: Stubs neu schreiben (Ziel-Holder auslassen)
                if rewrite_stubs:
                    for h in keep_holders:
                        if h is new_holder or (h["snapshot"] == new_holder["snapshot"] and h["relpath"] == new_holder["relpath"]):
                            _delete_file_if_exists(f"{snapshots_root}/{h['snapshot']}/{h['relpath']}.meta.json")
                            continue
                        _rewrite_stub(snapshots_root, h["snapshot"], h["relpath"], sha, node["anchor_path"], node.get("fileid"))

        # Snapshot nur löschen, wenn keine Blocker auftraten
        rmpath = f"{snapshots_root}/{sdel}"
        if snapshot_blockers:
            print(f"[warn] retention: Snapshot {sdel} bleibt bestehen (Blocker vorhanden).")
            continue

        if dry:
            print(f"[dry] delete snapshot dir: {rmpath}")
            print(f"[dry] delete manifest: /srv/pcloud-archive/manifests/{sdel}.json")
        else:
            pc.delete_folder(cfg, path=rmpath, recursive=True)
            
            # Paritäts-Cleanup: Manifest löschen wenn Remote-Snapshot gelöscht wird
            manifest_dir = os.path.join(os.getenv("PCLOUD_ARCHIVE_DIR", "/srv/pcloud-archive"), "manifests")
            manifest_file = os.path.join(manifest_dir, f"{sdel}.json")
            if os.path.exists(manifest_file):
                try:
                    os.remove(manifest_file)
                    print(f"[retention] Manifest gelöscht: {sdel}.json")
                except Exception as e:
                    print(f"[warn] Konnte Manifest nicht löschen: {manifest_file} ({e})", file=sys.stderr)

    # === NEU: Index nur schreiben wenn KEINE Blocker ===
    if any_blockers:
        print(f"[warn] retention: Index NICHT geschrieben wegen Blocker(n) in einem oder mehreren Snapshots")
    else:
        if ret_index_changed:
            _save_index(snapshots_root, idx, simulate=dry)
        else:
            print("[retention] no index changes")
    # === ENDE NEU ===

    if os.environ.get("PCLOUD_TIMING") == "1":
        print(f"[timing] retention: stubs_ms={int(ret_stub_ms)} index_ms={int(ret_index_write_ms)} stubs_written={ret_stub_writes}")

    msg = f"[retention] promoted={promoted} removed_nodes={removed_nodes}"
    print(msg if not dry else "[dry] " + msg[1:])
    # Metrics
    globals()["MET_PROMOTED"] += int(promoted)
    globals()["MET_REMOVED_NODES"] += int(removed_nodes)


# ----------------- Delta-Copy Mode (PoC) -----------------

def push_1to1_delta_mode(cfg, manifest, dest_root, *, dry=False, verbose=False, manifest_path=None):
    """
    Delta-Copy Mode: Server-seitiges Klonen + Selective Update
    
    Workflow:
      1. Finde letzten vollständigen Snapshot (via content_index.json)
      2. copyfolder() - Server-seitiges Klonen (2-5s statt 3.5h)
      3. Manifest-Diff berechnen (10s)
      4. DELETE-Loop: deleted + changed Dateien löschen
      5. WRITE-Loop: new + changed Dateien hochladen/stubben
      6. Content-Index aktualisieren
      
    Performance:
      - Typisch: 60x-210x schneller bei minimalen Änderungen
      - 100k Dateien, 1 Änderung: 3.5h → <2min
      
    Fallback:
      - Falls kein Basis-Snapshot existiert: Wechsel zu push_1to1_mode()
    """
    t_start = time.time()
    
    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"
    
    _log(f"[delta-copy] Start: {snapshot_name}")
    _log(f"[delta-copy] Ziel: {dest_snapshot_dir}")
    
    # === Config: Timeout Protection (copyfolder kann bei 20k+ Dateien lange dauern) ===
    # Delta-Copy Meta-Operationen brauchen ~60-120s, Standard-Timeout (30s) ist zu kurz
    current_timeout = int(cfg.get("timeout", 30))
    if current_timeout <= 60:  # Erhöhe nur bei Standard/niedrigen Werten
        cfg["timeout"] = 300  # 5 Minuten Puffer (Test: 67s bei ~20k Dateien)
        _log(f"[delta-copy] Timeout erhöht: {current_timeout}s → {cfg['timeout']}s (Meta-Operationen)")
    else:
        _log(f"[delta-copy] Timeout beibehalten: {current_timeout}s")
    
    # === Schritt 1: Finde Basis-Snapshot ===
    _log(f"[delta-copy][1/6] Suche letzten vollständigen Snapshot...")
    t_find_start = time.time()
    
    try:
        index = load_content_index(cfg, snapshots_root)
    except Exception as e:
        _log(f"[delta-copy][FALLBACK] Konnte content_index.json nicht laden: {e}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    # Finde letzten Snapshot mit .upload_complete Marker
    basis_snapshot = None
    remote_snapshots = list_remote_snapshot_names(cfg, snapshots_root)
    
    # Sortiere absteigend (neueste zuerst)
    sorted_snapshots = sorted(remote_snapshots, reverse=True)
    
    for candidate in sorted_snapshots:
        if candidate == snapshot_name:
            continue  # Überspringe den neuen Snapshot selbst
        
        # Prüfe ob .upload_complete existiert
        marker_complete = f"{snapshots_root}/{candidate}/.upload_complete"
        try:
            pc.stat_file(cfg, path=marker_complete, with_checksum=False)
            basis_snapshot = candidate
            _log(f"[delta-copy][1/6] Basis gefunden: {basis_snapshot}")
            break
        except Exception:
            # Kein Complete-Marker → überspringe
            if verbose:
                _log(f"[delta-copy][1/6] Überspringe {candidate} (kein Complete-Marker)")
            continue
    
    if not basis_snapshot:
        _log(f"[delta-copy][FALLBACK] Kein vollständiger Basis-Snapshot gefunden")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    t_find_ms = (time.time() - t_find_start) * 1000.0
    _log(f"[delta-copy][1/6] ✓ Basis: {basis_snapshot} ({t_find_ms:.0f}ms)")

    # === Schritt 1.5: Stub-Ratio-Check ===
    # copyfolder lohnt sich nur, wenn der Basis-Snapshot bereits überwiegend
    # aus Stubs besteht. Andernfalls würde copyfolder echte Dateien duplizieren
    # (doppelte Quota), statt nur leichte Stubs zu klonen.
    #
    # Threshold via ENV konfigurierbar:
    #   PCLOUD_COPYFOLDER_MIN_STUB_RATIO  (default 0.5 = 50% Stubs nötig)
    #   PCLOUD_COPYFOLDER_MIN_FILES       (default 100, vermeidet False-Positives bei kleinen Snapshots)
    _min_stub_ratio = float(os.environ.get("PCLOUD_COPYFOLDER_MIN_STUB_RATIO", "0.5"))
    _min_files      = int(os.environ.get("PCLOUD_COPYFOLDER_MIN_FILES", "100"))

    _basis_total, _basis_stubs, _basis_ratio = _compute_snapshot_stub_ratio(index, basis_snapshot)
    _log(f"[delta-copy][1.5/6] Basis-Analyse '{basis_snapshot}': "
         f"{_basis_total} Dateien, {_basis_stubs} Stubs ({_basis_ratio:.1%}) "
         f"[threshold: >={_min_stub_ratio:.0%} bei >={_min_files} Dateien]")

    if _basis_total < _min_files or _basis_ratio < _min_stub_ratio:
        _log(f"[delta-copy][SAFE-MODE] Basis hat zu wenig Stubs "
             f"({_basis_ratio:.1%} < {_min_stub_ratio:.0%} oder "
             f"{_basis_total} < {_min_files} Dateien)")
        _log(f"[delta-copy][SAFE-MODE] Baue Snapshot mit frischer Stub-Struktur auf "
             f"(einmalige Transformation, danach TURBO-MODE aktiv)")
        return push_1to1_mode(cfg, manifest, dest_root,
                              dry=dry, verbose=verbose, manifest_path=manifest_path)

    _log(f"[delta-copy][TURBO-MODE] Stub-Ratio OK ({_basis_ratio:.1%}) – nutze copyfolder + Delta")

    # === Schritt 2: copyfolder() - Server-seitiges Klonen ===
    _log(f"[delta-copy][2/6] Starte Server-Side Copy: {basis_snapshot} → {snapshot_name}")
    t_copy_start = time.time()
    
    basis_path = f"{snapshots_root}/{basis_snapshot}"
    
    # KRITISCH: Zielordner VORHER anlegen (copycontentonly erwartet existierenden Container)
    if not dry:
        try:
            pc.ensure_path(cfg, snapshots_root)  # Parent sicherstellen
            pc.ensure_path(cfg, dest_snapshot_dir)  # Zielordner anlegen!
            _log(f"[delta-copy][2/6] ✓ Zielordner angelegt: {dest_snapshot_dir}")
        except Exception as e:
            _log(f"[delta-copy][ERROR] Konnte Zielordner nicht anlegen: {e}")
            return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    if dry:
        _log(f"[dry] copyfolder (contentonly): {basis_path} → {dest_snapshot_dir}")
    else:
        try:
            # copyfolder mit copycontentonly=True
            # Kopiert NUR den INHALT von basis_snapshot in den neuen Ordner
            result = pc.copyfolder(cfg, 
                                   from_path=basis_path, 
                                   to_path=dest_snapshot_dir, 
                                   copycontentonly=True)
            
            if verbose:
                _log(f"[delta-copy][2/6] copyfolder result: {json.dumps(result, indent=2)}")
            
            # CRITICAL: Warte bis Ordner wirklich existiert (pCloud async)
            # copyfolder() returned sofort, aber Ordner braucht Zeit bis sichtbar
            _log(f"[delta-copy][2/6] Warte auf Ordner-Sichtbarkeit...")
            max_wait_sec = 30
            poll_interval = 0.5
            elapsed = 0.0
            folder_exists = False
            
            while elapsed < max_wait_sec:
                try:
                    # Prüfe ob Ordner existiert
                    pc.stat_file(cfg, path=dest_snapshot_dir, with_checksum=False)
                    folder_exists = True
                    _log(f"[delta-copy][2/6] ✓ Ordner sichtbar nach {elapsed:.1f}s")
                    break
                except Exception:
                    # Noch nicht sichtbar, warte
                    time.sleep(poll_interval)
                    elapsed += poll_interval
            
            if not folder_exists:
                raise Exception(f"Ordner {dest_snapshot_dir} nach {max_wait_sec}s immer noch nicht sichtbar")
        
        except Exception as e:
            _log(f"[delta-copy][ERROR] copyfolder fehlgeschlagen: {e}")
            _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
            # Cleanup: Versuche geklonten Snapshot zu löschen
            try:
                pc.delete_folder(cfg, path=dest_snapshot_dir, recursive=True)
            except Exception:
                pass
            return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    t_copy_ms = (time.time() - t_copy_start) * 1000.0
    _log(f"[delta-copy][2/6] ✓ Server-Copy abgeschlossen ({t_copy_ms:.0f}ms = {t_copy_ms/1000:.1f}s)")
    
    # === Schritt 3: Manifest-Diff berechnen ===
    _log(f"[delta-copy][3/6] Berechne Manifest-Diff...")
    t_diff_start = time.time()
    
    # Finde Basis-Manifest (lokal im Archive)
    archive_base = os.getenv("PCLOUD_MANIFEST_ARCHIVE", "/srv/pcloud-archive")
    basis_manifest_path = f"{archive_base}/manifests/{basis_snapshot}.json"
    
    if not os.path.exists(basis_manifest_path):
        _log(f"[delta-copy][ERROR] Basis-Manifest nicht gefunden: {basis_manifest_path}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        # Cleanup
        if not dry:
            try:
                pc.delete_folder(cfg, path=dest_snapshot_dir, recursive=True)
            except Exception:
                pass
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    # Import pcloud_manifest_diff
    try:
        import pcloud_manifest_diff
    except Exception as e:
        _log(f"[delta-copy][ERROR] Konnte pcloud_manifest_diff nicht importieren: {e}")
        _log(f"[delta-copy][FALLBACK] Wechsle zu vollständigem Upload...")
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    
    # Schreibe current manifest temporär (falls noch nicht gespeichert)
    import tempfile
    temp_current = None
    if not manifest_path or not os.path.exists(manifest_path):
        fd, temp_current = tempfile.mkstemp(suffix=".json", prefix="manifest_current_")
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
        current_manifest_path = temp_current
    else:
        current_manifest_path = manifest_path
    
    try:
        diff = pcloud_manifest_diff.compare_manifests(current_manifest_path, basis_manifest_path)
    except Exception as e:
        _log(f"[delta-copy][ERROR] Manifest-Diff fehlgeschlagen: {e}")
        if temp_current:
            os.unlink(temp_current)
        return push_1to1_mode(cfg, manifest, dest_root, dry=dry, verbose=verbose, manifest_path=manifest_path)
    finally:
        if temp_current and os.path.exists(temp_current):
            os.unlink(temp_current)
    
    t_diff_ms = (time.time() - t_diff_start) * 1000.0
    
    stats = diff["stats"]
    _log(f"[delta-copy][3/6] ✓ Manifest-Diff berechnet ({t_diff_ms:.0f}ms)")
    _log(f"[delta-copy][3/6]   Identisch: {stats['identical_count']}")
    _log(f"[delta-copy][3/6]   Neu:       {stats['new_count']}")
    _log(f"[delta-copy][3/6]   Geändert:  {stats['changed_count']}")
    _log(f"[delta-copy][3/6]   Gelöscht:  {stats['deleted_count']}")
    
    # === Schritt 4: DELETE-Loop ===
    _log(f"[delta-copy][4/6] Lösche geänderte und gelöschte Dateien...")
    t_delete_start = time.time()
    
    delete_count = 0
    delete_items = diff["deleted"] + diff["changed"]
    
    for item in delete_items:
        relpath = item.get("relpath")
        if not relpath:
            continue
        
        # Lösche echte Datei
        file_path = f"{dest_snapshot_dir}/{relpath}"
        
        if dry:
            _log(f"[dry] delete: {file_path}")
        else:
            try:
                pc.delete_file(cfg, path=file_path)
                delete_count += 1
            except Exception as e:
                if "2005" not in str(e) and "not found" not in str(e).lower():
                    _log(f"[delta-copy][4/6][warn] Konnte {file_path} nicht löschen: {e}")
        
        # Lösche auch .meta.json Stub (falls vorhanden)
        stub_path = f"{file_path}.meta.json"
        if dry:
            _log(f"[dry] delete stub: {stub_path}")
        else:
            try:
                pc.delete_file(cfg, path=stub_path)
            except Exception:
                pass  # Stub existiert nicht → okay
    
    t_delete_ms = (time.time() - t_delete_start) * 1000.0
    _log(f"[delta-copy][4/6] ✓ {delete_count} Dateien gelöscht ({t_delete_ms:.0f}ms)")
    
    # === Schritt 5: WRITE-Loop (new + changed) ===
    _log(f"[delta-copy][5/6] Schreibe neue und geänderte Dateien...")
    t_write_start = time.time()
    
    write_items = diff["new"] + diff["changed"]
    uploaded = 0
    stubs = 0
    
    # Hilfstabellen (ähnlich wie push_1to1_mode)
    seen_inodes: dict[tuple[int,int], str] = {}
    items_dict = index.setdefault("items", {})
    known_anchors = {}
    
    for sha, node in items_dict.items():
        ap = node.get("anchor_path")
        fid = node.get("fileid")
        if ap and fid:
            known_anchors[sha] = (ap, fid)
    
    # Sortiere nach Extension für bessere Fehlerdiagnose
    write_items.sort(key=lambda x: (x.get("ext") or "", x.get("relpath") or ""))
    
    index_changed = False
    stubs_to_write = []
    
    def _ensure(path: str) -> None:
        if not path or dry:
            return
        pc.call_with_backoff(pc.ensure_path, cfg, path)
    
    def _upload_real_file(abs_src: str, dst_path: str) -> tuple:
        """Returns (fileid, pcloud_hash)"""
        parent = os.path.dirname(dst_path.rstrip("/"))
        if parent:
            _ensure(parent)
        if dry:
            _log(f"[dry] upload: {dst_path} <- {abs_src}")
            return (None, None)
        
        res = pc.call_with_backoff(pc.upload_file, cfg, local_path=abs_src, remote_path=dst_path, attempts=12, max_sleep=60.0)
        
        try:
            md = (res or {}).get("metadata") or {}
            fileid = md.get("fileid")
            pcloud_hash = md.get("hash")
        except Exception:
            fileid = None
            pcloud_hash = None
        
        # Eager FileID
        if (not fileid or not pcloud_hash) and os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0":
            try:
                stat_md = pc.call_with_backoff(pc.stat_file_safe, cfg, path=dst_path) or {}
                if not fileid:
                    fileid = stat_md.get("fileid")
                if not pcloud_hash:
                    pcloud_hash = stat_md.get("hash")
            except Exception:
                pass
        
        return (fileid, pcloud_hash)
    
    def _queue_stub(relpath: str, file_item: dict, node: dict) -> None:
        nonlocal stubs, index_changed
        
        eager = os.environ.get("PCLOUD_EAGER_FILEID", "1") != "0"
        if eager and (not node.get("fileid")) and node.get("anchor_path"):
            fid = pc.resolve_fileid_cached(cfg, path=node["anchor_path"], cache=_fid_cache_shared)
            if fid:
                node["fileid"] = fid
                index_changed = True
        
        meta_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
        payload = {
            "type": "hardlink",
            "sha256": file_item.get("sha256"),
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "snapshot": snapshot_name,
            "relpath": relpath,
            "anchor_path": node.get("anchor_path"),
            "fileid": node.get("fileid") if node.get("fileid") is not None else None,
            "inode": file_item.get("inode"),
        }
        if dry:
            _log(f"[dry] write stub: {meta_path}")
        else:
            stubs_to_write.append((meta_path, payload))
        stubs += 1
    
    # Hauptschleife für WRITE
    for file_item in write_items:
        relpath = file_item.get("relpath")
        if not relpath:
            continue
        
        sha = (file_item.get("sha256") or "").lower()
        abs_src = file_item.get("source_path")
        ext = file_item.get("ext", "")
        
        if not sha or not abs_src:
            _log(f"[delta-copy][5/6][warn] Überspringe {relpath} (kein SHA256 oder source_path)")
            continue
        
        # Hardlink-Dedupe (innerhalb desselben Snapshots)
        ino_data = file_item.get("inode")
        key = None
        if ino_data and isinstance(ino_data, dict):
            dev = ino_data.get("dev")
            ino = ino_data.get("ino")
            if dev and ino:
                key = (dev, ino)
        
        if key and key in seen_inodes:
            # Hardlink zu bereits verarbeitetem File
            _queue_stub(relpath, file_item, items_dict.get(sha, {}))
            continue
        
        # Prüfe ob SHA256 bereits im Index existiert
        node = items_dict.get(sha)
        
        if node:
            # Hash existiert bereits → Stub schreiben
            _queue_stub(relpath, file_item, node)
            if key:
                seen_inodes[key] = sha
        else:
            # Neue Datei → Upload + Anchor registrieren
            dst_path = f"{dest_snapshot_dir}/{relpath}"
            
            fileid, pcloud_hash = _upload_real_file(abs_src, dst_path)
            uploaded += 1
            
            # Index-Eintrag erstellen
            node = {
                "anchor_path": dst_path,
                "anchor_snapshot": snapshot_name,
                "holders": [snapshot_name],
                "ext": ext,
            }
            if fileid:
                node["fileid"] = fileid
            if pcloud_hash:
                node["pcloud_hash"] = pcloud_hash
            
            items_dict[sha] = node
            index_changed = True
            
            if key:
                seen_inodes[key] = sha
    
    # Stubs schreiben (Batch)
    if stubs_to_write and not dry:
        _log(f"[delta-copy][5/6] Schreibe {len(stubs_to_write)} Stubs...")
        _batch_write_stubs(cfg, stubs_to_write, dry=dry)
        globals()["MET_STUBS_WRITTEN"] += len(stubs_to_write)
    
    t_write_ms = (time.time() - t_write_start) * 1000.0
    _log(f"[delta-copy][5/6] ✓ WRITE abgeschlossen: {uploaded} uploads, {stubs} stubs ({t_write_ms:.0f}ms)")
    
    # === Schritt 6: Content-Index aktualisieren ===
    _log(f"[delta-copy][6/6] Aktualisiere Content-Index...")
    t_index_start = time.time()
    
    # Holders aktualisieren: Snapshot zu allen verwendeten Hashes hinzufügen
    # WICHTIG: Iteriere über items_dict (enthält auch neue Nodes aus Phase 5!)
    for file_item in (manifest.get("items") or []):
        if file_item.get("type") != "file":
            continue
        
        sha = (file_item.get("sha256") or "").lower()
        if not sha:
            continue
        
        node = items_dict.get(sha)
        if not node:
            # Sollte nicht vorkommen (Phase 5 hätte Node erstellt)
            _log(f"[delta-copy][6/6][ERROR] SHA256 {sha[:16]}... nicht im Index!")
            continue
        
        holders = node.setdefault("holders", [])
        relpath = file_item.get("relpath") or ""
        
        # Holder mit vollständigen Metadaten (wie in Manifesten)
        holder_entry = {
            "snapshot": snapshot_name,
            "relpath": relpath,
            "size": file_item.get("size"),
            "mtime": file_item.get("mtime"),
            "inode": file_item.get("inode"),  # {"dev": ..., "ino": ..., "nlink": ...}
            "ext": file_item.get("ext"),
        }
        
        # Check if this exact holder already exists (robust gegen String-Leichen)
        holder_exists = any(
            isinstance(h, dict) and h.get("snapshot") == snapshot_name and h.get("relpath") == relpath
            for h in holders
        )
        
        if not holder_exists:
            holders.append(holder_entry)
            index_changed = True
    
    # Index speichern (remote + lokal)
    if index_changed:
        save_content_index(cfg, snapshots_root, index, dry=dry)
        _log(f"[delta-copy][6/6] ✓ Content-Index remote gespeichert")
        
        # Lokal archivieren (Snapshot-spezifisch)
        if not dry:
            archive_index_path = f"{archive_base}/indexes/content_index_{snapshot_name}.json"
            os.makedirs(os.path.dirname(archive_index_path), exist_ok=True)
            save_content_index_local(archive_index_path, index)
            _log(f"[delta-copy][6/6] ✓ Content-Index lokal archiviert: {archive_index_path}")
        
        # Master-Index aktualisieren (alle Snapshots zusammen)
        if not dry:
            master_index_path = f"{archive_base}/indexes/content_index_master.json"
            save_content_index_local(master_index_path, index)
            _log(f"[delta-copy][6/6] ✓ Master-Index aktualisiert: {master_index_path}")
        
        # Remote archivieren (Paranoia-Modus: Snapshot-isolierter Index für Recovery)
        if not dry:
            idx_path = f"{snapshots_root}/_index/content_index.json"
            archive_path = f"{snapshots_root}/_index/archive/{snapshot_name}_index.json"
            try:
                pc.ensure_parent_dirs(cfg, archive_path)
                pc.copyfile(cfg, from_path=idx_path, to_path=archive_path)
                _log(f"[delta-copy][6/6] ✓ Content-Index remote archiviert: {archive_path}")
            except Exception as e:
                _log(f"[delta-copy][6/6][warn] Remote-Archivierung fehlgeschlagen: {e}")
    else:
        _log(f"[delta-copy][6/6] Content-Index unverändert")
    
    t_index_ms = (time.time() - t_index_start) * 1000.0
    
    # === Upload-Complete Marker setzen ===
    marker_complete = f"{dest_snapshot_dir}/.upload_complete"
    if not dry:
        try:
            pc.put_textfile(cfg, path=marker_complete, text=json.dumps({
                "snapshot": snapshot_name,
                "completed_at": time.time(),
                "mode": "delta-copy",
                "basis_snapshot": basis_snapshot,
            }))
        except Exception as e:
            _log(f"[delta-copy][warn] Konnte Complete-Marker nicht setzen: {e}")
    
    # === Manifest archivieren ===
    if manifest_path and not dry:
        archive_dest = f"{archive_base}/manifests/{snapshot_name}.json"
        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        
        try:
            import shutil
            shutil.copy2(manifest_path, archive_dest)
            _log(f"[delta-copy] Manifest archiviert: {archive_dest}")
        except Exception as e:
            _log(f"[delta-copy][warn] Konnte Manifest nicht archivieren: {e}")
    
    # === Summary ===
    t_total_ms = (time.time() - t_start) * 1000.0
    
    _log(f"[delta-copy] ✓ ABGESCHLOSSEN ({t_total_ms/1000:.1f}s total)")
    _log(f"[delta-copy]   Server-Copy: {t_copy_ms:.0f}ms")
    _log(f"[delta-copy]   Manifest-Diff: {t_diff_ms:.0f}ms")
    _log(f"[delta-copy]   DELETE: {delete_count} Dateien, {t_delete_ms:.0f}ms")
    _log(f"[delta-copy]   WRITE: {uploaded} uploads, {stubs} stubs, {t_write_ms:.0f}ms")
    _log(f"[delta-copy]   Index: {t_index_ms:.0f}ms")
    
    # Metrics
    globals()["MET_UPLOADED_FILES"] += uploaded
    
    return {
        "uploaded": uploaded,
        "stubs": stubs,
        "deleted": delete_count,
        "mode": "delta-copy",
        "basis_snapshot": basis_snapshot,
        "timings": {
            "total_ms": t_total_ms,
            "find_ms": t_find_ms,
            "copy_ms": t_copy_ms,
            "diff_ms": t_diff_ms,
            "delete_ms": t_delete_ms,
            "write_ms": t_write_ms,
            "index_ms": t_index_ms,
        }
    }


# ----------------- CLI -----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Pusht ein JSON-Manifest nach pCloud (Object-Store- oder 1:1-Snapshot-Modus).")
    ap.add_argument("--manifest", required=True, help="Pfad zur Manifest-JSON (schema=2)")
    ap.add_argument("--dest-root", required=True, help="Remote-Wurzel, z.B. /Backup/pcloud-snapshots")
    ap.add_argument("--snapshot-mode", choices=["objects","1to1"], default="objects",
                    help="Upload-Strategie: objects (Hash-Object-Store + Stubs) oder 1to1 (Materialisieren + Stubs)")
    ap.add_argument("--use-delta-copy", action="store_true",
                    help="Delta-Copy-Modus: Server-seitiges Klonen + selective Updates (nur mit --snapshot-mode 1to1). "
                         "Erfordert vorherigen vollständigen Snapshot. Fallback zu Full-Mode wenn kein Basis existiert.")
    ap.add_argument("--objects-layout", choices=["two-level"], default="two-level",
                    help="Layout für Object-Store (aktuell nur two-level).")
    ap.add_argument("--retention-sync", action="store_true",
                    help="Vor dem Upload: entfernte Snapshots, die lokal fehlen, sauber promoten/löschen (nur relevant für --snapshot-mode 1to1).")
    ap.add_argument("--dry-run", action="store_true")


    # pCloud Config
    ap.add_argument("--env-file")
    ap.add_argument("--profile")
    ap.add_argument("--env-dir")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--device")
    ap.add_argument("--token")

    args = ap.parse_args()

    # Config
    cfg = pc.effective_config(
        env_file=args.env_file,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token},
        profile=args.profile,
        env_dir=args.env_dir
    )

    # --- Neu: Plausibilisierung & Preflight ---
    # Zielpfad normieren (führt führenden "/")
    args.dest_root = pc._norm_remote_path(args.dest_root)
    # ENV-File rein informativ: effective_config hat bereits geprüft, ob Token existiert
    try:
        pc.preflight_or_raise(cfg)   # → raise bei Auth/Quota/API down
    except Exception as e:
        print(f"[preflight][FAIL] {e}", file=sys.stderr)
        sys.exit(12)
    # --- Ende Neu ---

    # Manifest lesen
    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    if int(manifest.get("schema", 0)) < 2:
        print("Manifest schema>=2 erwartet (mit inode/ext/sha256).", file=sys.stderr)
        sys.exit(2)

    dest_root = pc._norm_remote_path(args.dest_root)

    # Optional: Retention-Sync (nur sinnvoll im 1:1-Modus)
    if args.retention_sync and args.snapshot_mode == "1to1":
        local_snaps = list_local_snapshot_names(manifest["root"])
        retention_sync_1to1(cfg, dest_root, local_snaps=local_snaps, dry=bool(args.dry_run))

    if args.snapshot_mode == "objects":
        push_objects_mode(cfg, manifest, dest_root, dry=bool(args.dry_run), objects_layout=args.objects_layout)
    else:
        # 1:1-Modus: Delta-Copy oder Full-Mode
        if args.use_delta_copy:
            _log("[mode] Delta-Copy-Modus aktiviert")
            push_1to1_delta_mode(cfg, manifest, dest_root, dry=bool(args.dry_run), manifest_path=args.manifest)
        else:
            push_1to1_mode(cfg, manifest, dest_root, dry=bool(args.dry_run), manifest_path=args.manifest)


    # --- metrics summary (einheitlich, greppbar) ---
    try:
        print(f"[metrics] uploaded_files={MET_UPLOADED_FILES} resumed_files={MET_RESUMED_FILES} "
              f"stubs_written={MET_STUBS_WRITTEN} promoted={MET_PROMOTED} removed_nodes={MET_REMOVED_NODES} "
              f"fid_cache_hits={fid_cache_hits} fid_lookups={fid_lookups} fid_rest_ms={int(fid_rest_ms)} "
              f"api_retries={MET_API_RETRIES}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
