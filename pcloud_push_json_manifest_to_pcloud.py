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
import os, sys, json, argparse
from typing import Dict, Any, Optional, Tuple

# ---- Lib laden ----
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"Fehler: pcloud_bin_lib konnte nicht importiert werden: {e}", file=sys.stderr)
    sys.exit(2)

# ----------------- Utilities -----------------

def resolve_fileid_for_path(cfg, path):
    """
    Holt die fileid für einen pCloud-Pfad effizient über die Binary-API (stat)
    und cached Ergebnisse in-memory pro Lauf.
    Gibt int(fileid) oder None zurück.
    """
    import pcloud_bin_lib as pc

    # Memoization
    cache = getattr(resolve_fileid_for_path, "_cache", None)
    if cache is None:
        cache = {}
        setattr(resolve_fileid_for_path, "_cache", cache)

    p = pc._norm_remote_path(path)
    if p in cache:
        return cache[p]

    try:
        md = pc.stat_file(cfg, path=p, with_checksum=False) or {}
        fid = md.get("fileid")
    except Exception:
        fid = None

    cache[p] = fid
    return fid

def write_hardlink_stub_1to1(cfg, snapshots_root, snapshot_name, relpath, file_item, node, dry=False):
    """
    Schreibt die .meta.json für einen 1:1-Hardlink-Stub und sorgt dafür,
    dass 'fileid' (falls möglich) gesetzt und im Index-Node mitgeführt wird.
    """
    import json as _json
    import pcloud_bin_lib as pc

    dest_snapshot_dir = f"{snapshots_root.rstrip('/')}/{snapshot_name}"
    meta_path = f"{dest_snapshot_dir.rstrip('/')}/{relpath}.meta.json"

    # Ordner sicherstellen
    pc.ensure_path(cfg, dest_snapshot_dir, folder=True, dry=dry)
    parent_dir = "/".join(meta_path.split("/")[:-1])
    if parent_dir and parent_dir != dest_snapshot_dir:
        pc.ensure_path(cfg, parent_dir, folder=True, dry=dry)

    # fileid nachziehen (Binary-API + Cache), falls im Node noch None
    fileid = node.get("fileid")
    if not fileid and node.get("anchor_path"):
        fid = resolve_fileid_for_path(cfg, node["anchor_path"])
        if fid:
            fileid = fid
            node["fileid"] = fid  # gleich im RAM-Index mitführen

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
    except Exception as e:
        msg = (str(e) or "").lower()
        if " 2055" in msg or "not found" in msg or "no such file" in msg:
            return None
        return None

def ensure_parent_dirs(cfg: dict, remote_path: str, *, dry: bool=False) -> None:
    """Sorgt dafür, dass alle Ordner bis zum parent von remote_path existieren."""
    p = pc._norm_remote_path(remote_path)
    parent = p.rsplit("/", 1)[0] or "/"
    if dry: return
    pc.ensure_path(cfg, parent)

def upload_json_stub(cfg: dict, remote_path: str, payload: dict, *, dry: bool=False) -> None:
    if dry:
        target = payload.get("object_path") or payload.get("anchor_path") or payload.get("sha256")
        print(f"[dry] stub: {remote_path} -> {target}")
        return
    pc.ensure_parent_dirs(cfg, remote_path)
    pc.write_json_at_path(cfg, remote_path, payload)

def _bytes_to_tempfile(b: bytes) -> str:
    import tempfile, os
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

def load_content_index(cfg: dict, snapshots_root: str) -> dict:
    """
    Lädt _snapshots/_index/content_index.json robust.
    - Wenn Datei fehlt/kaputt: leeren Index zurückgeben.
    - Ein 'result'≠0 im JSON gilt als API-Fehler (dann leerer Index).
    - Fehlt 'result' völlig (Normalfall bei echter Index-Datei) → OK.
    """
    import json as _json
    import pcloud_bin_lib as pc

    idx_path = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        j = _json.loads(txt)

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
    idx_remote = f"{snapshots_root.rstrip('/')}/_index/content_index.json"
    if dry:
        print(f"[dry] write index: {idx_remote} (items={len(index.get('items',{}))})")
        return
    pc.ensure_parent_dirs(cfg, idx_remote)
    pc.write_json_at_path(cfg, idx_remote, index)

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
    import os as _os
    base = _os.path.dirname(_os.path.abspath(manifest_root))  # parent von ".../<snapshot>"
    names = set()
    try:
        for n in _os.listdir(base):
            p = _os.path.join(base, n)
            if _os.path.isdir(p) and n not in ("latest",):
                names.add(n)
    except Exception:
        pass
    return names



# ----------------- Haupt-Logik -----------------

def push_objects_mode(cfg: dict, manifest: dict, dest_root: str, *, dry: bool, objects_layout: str="two-level") -> None:
    """Hash-Object-Store + Stubs in Snapshot."""
    objects_root   = f"{dest_root.rstrip('/')}/_objects"
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    snapshot       = manifest["snapshot"]
    items          = manifest.get("items") or []

    uploaded = 0; skipped = 0; stubs = 0

    print(f"[plan] objects={objects_root} snapshot={snapshots_root}/{snapshot}")

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


def push_1to1_mode(cfg, manifest, dest_root, *, dry=False, verbose=False):
    """
    1:1-Modus:
      - erstes Auftreten (SHA neu)  -> echte Datei materialisieren (Upload)
      - erneutes Auftreten (SHA bekannt) -> Stub (.meta.json) mit anchor_path *und* fileid
      - gleiche (dev,ino) *im selben Snapshot* -> Stub zum ersten Materialisieren in *diesem* Snapshot
      - content_index.json wird fortlaufend gepflegt (holders[], fileid)
    """
    import os, json
    import pcloud_bin_lib as pc

    snapshot_name = manifest.get("snapshot") or "SNAPSHOT"
    dest_root = pc._norm_remote_path(dest_root)
    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    dest_snapshot_dir = f"{snapshots_root}/{snapshot_name}"

    print(f"[plan] 1to1 snapshot={dest_snapshot_dir}")

    # --- kleine Helfer, die zur vorhandenen Lib passen ---
    def _ensure(path):
        if not path:
            return
        if dry:
            print(f"[dry] ensure: {path}")
        else:
            pc.ensure_path(cfg, path)

    # Index laden/initialisieren
    index = load_content_index(cfg, snapshots_root)
    items = index.setdefault("items", {})

    # Hilfstabellen
    seen_inodes = {}  # (dev,ino) -> relpath (erste Materialisierung in DIESEM Snapshot)

    # Ordner anlegen (Snapshot & _index)
    _ensure(snapshots_root)
    _ensure(f"{snapshots_root}/_index")
    _ensure(dest_snapshot_dir)

    uploaded = 0
    stubs = 0

    # --- Upload-Hilfsroutine ---
    def _upload_real_file(abs_src, dst_path):
        """lädt die reale Datei hoch (binäre API); erwartet vollständige Ziel-Pfad-Struktur."""
        parent = os.path.dirname(dst_path.rstrip("/"))
        if parent:
            _ensure(parent)
        if dry:
            print(f"[dry] upload 1to1: {dst_path}  <- {abs_src}")
            return None  # fileid unbekannt im Dry-Run
        res = pc.upload_file(cfg, local_path=abs_src, remote_path=dst_path)
        try:
            md = (res or {}).get("metadata") or {}
            return md.get("fileid")
        except Exception:
            return None

    # --- Stub schreiben + fileid ggf. auflösen ---
    def _write_stub(relpath, file_item, node):
        nonlocal stubs

        # fileid nachziehen, falls im Node noch None
        if not node.get("fileid") and node.get("anchor_path"):
            fid = resolve_fileid_for_path(cfg, node["anchor_path"])
            if fid:
                node["fileid"] = fid  # gleich im Index mitführen

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
            "fileid": node.get("fileid") if node.get("fileid") is not None else None,
            "snapshot": snapshot_name,
            "relpath": relpath,
        }

        meta_path = f"{dest_snapshot_dir.rstrip('/')}/{relpath}.meta.json"
        parent = os.path.dirname(meta_path.rstrip("/"))
        if parent:
            _ensure(parent)

        if dry:
            print(f"[dry] stub: {meta_path} -> {node.get('anchor_path')}")
        else:
            # WICHTIG: deine Lib hat write_json_at_path(..., obj=...)
            pc.write_json_at_path(cfg, path=meta_path, obj=payload)
        stubs += 1

        # holders[] pflegen
        holders = node.setdefault("holders", [])
        h = {"snapshot": snapshot_name, "relpath": relpath}
        if h not in holders:
            holders.append(h)

    # --- Hauptdurchlauf ---
    for it in manifest.get("items", []):
        if it.get("type") != "file":
            continue

        relpath = it["relpath"]
        sha = (it.get("sha256") or "").lower()
        inode = it.get("inode") or {}
        dev = int(inode.get("dev") or 0)
        ino = int(inode.get("ino") or 0)
        ino_key = (dev, ino)

        # A) SHA schon bekannt -> Stub mit anchor_path/fileid
        node = items.get(sha)
        if node and node.get("anchor_path"):
            _write_stub(relpath, it, node)
            continue

        # B) Gleiches (dev,ino) in DIESEM Snapshot -> Stub zum ersten Materialisieren dieses Snapshots
        if ino_key in seen_inodes:
            first_rel = seen_inodes[ino_key]
            local_anchor = f"{dest_snapshot_dir.rstrip('/')}/{first_rel}"
            local_node = {"anchor_path": local_anchor, "fileid": None}
            _write_stub(relpath, it, local_node)
            continue

        # C) Erstes Auftreten -> reale Datei hochladen & als Anchor registrieren
        abs_src = it.get("abspath") or it.get("local") or os.path.join(manifest["root"], relpath)
        dst_path = f"{dest_snapshot_dir.rstrip('/')}/{relpath}"
        fid = _upload_real_file(abs_src, dst_path)
        uploaded += 1

        node = items.setdefault(sha, {"holders": []})
        node["anchor_path"] = dst_path
        if fid:
            node["fileid"] = fid
        holders = node.setdefault("holders", [])
        h = {"snapshot": snapshot_name, "relpath": relpath}
        if h not in holders:
            holders.append(h)

        seen_inodes[ino_key] = relpath

    # Index speichern
    if dry:
        print(f"[dry] write index: {snapshots_root}/_index/content_index.json (items={len(items)})")
    else:
        save_content_index(cfg, snapshots_root, index, dry=False)

    print(f"1to1: uploaded={uploaded} stubs={stubs} (snapshot={snapshot_name})")
    return {"uploaded": uploaded, "stubs": stubs}

def retention_sync_1to1(cfg: dict, dest_root: str, *, local_snaps: set[str], dry: bool=False) -> None:
    """
    Für 1:1-Modus: Snapshots entfernen, die lokal nicht mehr existieren.
    Vorher ggf. Promotion der Anchors in verbleibende Snapshots (serverseitige Copy).
    """
    import json as _json, os as _os

    snapshots_root = f"{dest_root.rstrip('/')}/_snapshots"
    remote_snaps = list_remote_snapshot_names(cfg, snapshots_root)
    to_delete = sorted(s for s in remote_snaps if s not in local_snaps)
    if not to_delete:
        return

    # Index laden oder leeren Index anlegen
    idx_path = f"{snapshots_root}/_index/content_index.json"
    try:
        txt = pc.get_textfile(cfg, path=idx_path)
        idx = _json.loads(txt)
        if "items" not in idx: idx["items"] = {}
    except Exception:
        idx = {"version": 1, "items": {}}

    items = idx["items"]

    for sdel in to_delete:
        del_prefix = f"{snapshots_root}/{sdel}/"
        # Für alle SHA-Einträge prüfen, ob Anchor in sdel liegt
        for sha, node in list(items.items()):
            anchor = node.get("anchor_path") or ""
            if not anchor.startswith(del_prefix):
                continue

            holders = [h for h in (node.get("holders") or []) if h.get("snapshot") != sdel]
            if not holders:
                # Keine Referenzen mehr -> Eintrag entfernen; Datei verschwindet mit Snapshot
                del items[sha]
                continue

            # Neuen Holder wählen (z.B. jüngster Snapshotname)
            new_holder = max(holders, key=lambda h: h.get("snapshot") or "")
            new_path = f"{snapshots_root}/{new_holder['snapshot']}/{new_holder['relpath']}"
            new_parent = _os.path.dirname(new_path) or "/"

            if dry:
                print(f"[dry] promote anchor {sha}: {anchor} -> {new_path}")
                node["anchor_path"] = new_path
                # fileid bleibt wie war (wird beim nächsten Restore ggf. via stat nachgezogen)
            else:
                # Quelle: alte Anchor-FileID
                src_fid = node.get("fileid")
                if not src_fid:
                    m = stat_file_safe(cfg, path=anchor)
                    src_fid = m.get("fileid") if m else None
                if not src_fid:
                    raise RuntimeError(f"Promotion benötigt fileid für {anchor}")

                pc.ensure_path(cfg, new_parent)
                pc.copyfile(cfg, src_fileid=int(src_fid), dest_path=new_path)
                m2 = stat_file_safe(cfg, path=new_path)
                node["fileid"] = m2.get("fileid") if m2 else src_fid
                node["anchor_path"] = new_path

            # Halterliste ohne sdel zurückschreiben
            node["holders"] = holders

        # Jetzt Snapshot-Ordner serverseitig löschen
        rmpath = f"{snapshots_root}/{sdel}"
        if dry:
            print(f"[dry] delete snapshot dir: {rmpath}")
        else:
            try:
                pc.deletefolder_recursive(cfg, path=rmpath)
            except Exception as e:
                print(f"[warn] delete snapshot failed {rmpath}: {e}", file=sys.stderr)

    # Index zurückschreiben
    if dry:
        print(f"[dry] write updated content_index.json (items={len(items)})")
    else:
        pc.put_textfile(cfg, path=idx_path, text=json.dumps(idx, ensure_ascii=False, indent=2))


# ----------------- CLI -----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Pusht ein JSON-Manifest nach pCloud (Object-Store- oder 1:1-Snapshot-Modus).")
    ap.add_argument("--manifest", required=True, help="Pfad zur Manifest-JSON (schema=2)")
    ap.add_argument("--dest-root", required=True, help="Remote-Wurzel, z.B. /Backup/pcloud-snapshots")
    ap.add_argument("--snapshot-mode", choices=["objects","1to1"], default="objects",
                    help="Upload-Strategie: objects (Hash-Object-Store + Stubs) oder 1to1 (Materialisieren + Stubs)")
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
        push_1to1_mode(cfg, manifest, dest_root, dry=bool(args.dry_run))

if __name__ == "__main__":
    main()
