#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_delete_bin.py – Sicheres Löschen (Datei/Ordner) via pCloud Binary-API, lib-basiert.

Dry-Run:
  - Löst IDs/Paths voll auf und zeigt immer: ART, ID, NAME, PFAD, PARENT-ID.
  - Nimmt NIE Änderungen vor.

Live:
  - Erst nach gleicher Validierung/Ausgabe, nur mit --yes/--confirm.
  - --recursive für Ordner erfordert --confirm-name (exakter Ordnername).
  - Optional: --trash-clear entfernt den Papierkorb-Eintrag endgültig.

Zieldefinition (genau EIN Modus):
  Direkt:
    --file-id | --file-path | --folder-id | --folder-path
  Parent + Name:
    (--parent-folderid | --parent-path) + (--file-name | --folder-name)
  Validierung (zusätzlich):
    (--parent-folderid | --parent-path) + (--file-id | --folder-id) + --verify-under-parent
"""

from __future__ import annotations
import argparse, sys, json, os, ssl
from typing import Dict, Any, Optional, Tuple
import pcloud_bin_lib as pc


# ---------- helpers ----------

def _resolve_from_direct(cfg: Dict[str,Any],
                         *,
                         file_id: int|None, file_path: str|None,
                         folder_id: int|None, folder_path: str|None
                         ) -> Tuple[str,int,str,str,int]:
    """
    Liefert (kind_typ, kind_id, kind_name, kind_path, parentfolderid).
    kind_typ in {"file","folder"}.
    """
    # Datei per ID/Pfad
    if file_id is not None or file_path:
        if file_id is not None:
            m = pc.stat_file(cfg, fileid=int(file_id), with_checksum=False) or {}
        else:
            m = pc.stat_file(cfg, path=pc._norm_remote_path(file_path or ""), with_checksum=False) or {}
        if not m or m.get("isfolder"):
            raise FileNotFoundError("Datei nicht gefunden.")
        name = m.get("name") or ""
        pfad = m.get("path")
        if not pfad:
            # Pfad aus Parent ableiten
            parent = int(m.get("parentfolderid") or 0)
            pfad = _compose_path_from_parent(cfg, parent, name)
        return ("file", int(m.get("fileid")), name, pfad or f"/{name}", int(m.get("parentfolderid") or 0))

    # Ordner per ID/Pfad
    if folder_id is not None or folder_path:
        if folder_id is not None:
            top = pc.listfolder(cfg, folderid=int(folder_id), recursive=False, nofiles=True, showpath=True)
        else:
            top = pc.listfolder(cfg, path=pc._norm_remote_path(folder_path or ""), recursive=False, nofiles=True, showpath=True)
        md = top.get("metadata") or {}
        if not md or not md.get("isfolder"):
            raise FileNotFoundError("Ordner nicht gefunden.")
        return ("folder",
                int(md.get("folderid")),
                md.get("name") or "",
                md.get("path") or "/",
                int(md.get("parentfolderid") or 0)
               )

    raise ValueError("Kein Direkt-Target angegeben.")


def _resolve_under_parent(cfg: Dict[str,Any],
                          *,
                          parent_folderid: int|None, parent_path: str|None,
                          file_name: str|None, folder_name: str|None
                          ) -> Tuple[str,int,str,str,int]:
    """Wie oben, nur Parent + Name (exakter Treffer)."""
    if (parent_folderid is None) == (parent_path is None):
        raise ValueError("Entweder --parent-folderid ODER --parent-path angeben.")
    if (file_name is None) == (folder_name is None):
        raise ValueError("Entweder --file-name ODER --folder-name angeben.")

    if parent_folderid is not None:
        top = pc.listfolder(cfg, folderid=int(parent_folderid), recursive=False, nofiles=False, showpath=True)
    else:
        top = pc.listfolder(cfg, path=pc._norm_remote_path(parent_path or ""), recursive=False, nofiles=False, showpath=True)

    mdp = top.get("metadata") or {}
    pfid = int(mdp.get("folderid") or 0)
    contents = mdp.get("contents") or []
    hits = []
    for ch in contents:
        if file_name and not ch.get("isfolder") and ch.get("name") == file_name:
            hits.append(("file", int(ch.get("fileid")), ch.get("name"), ch.get("path"), pfid))
        if folder_name and ch.get("isfolder") and ch.get("name") == folder_name:
            hits.append(("folder", int(ch.get("folderid")), ch.get("name"), ch.get("path"), pfid))
    if len(hits) == 0:
        raise FileNotFoundError("Kein Eintrag unter Parent gefunden.")
    if len(hits) > 1:
        raise RuntimeError("Mehrdeutige Treffer unter Parent.")
    return hits[0]


def _verify_belongs_to_parent(cfg: Dict[str,Any],
                              *,
                              parent_folderid: int|None, parent_path: str|None,
                              file_id: int|None, folder_id: int|None) -> None:
    """Validiert, dass die Kind-ID direkt unter Parent liegt."""
    if (parent_folderid is None) == (parent_path is None):
        raise ValueError("Entweder --parent-folderid ODER --parent-path.")
    if (file_id is None) == (folder_id is None):
        raise ValueError("Entweder --file-id ODER --folder-id.")

    if parent_folderid is not None:
        top = pc.listfolder(cfg, folderid=int(parent_folderid), recursive=False, nofiles=False, showpath=False)
    else:
        top = pc.listfolder(cfg, path=pc._norm_remote_path(parent_path or ""), recursive=False, nofiles=False, showpath=False)
    pfid = int((top.get("metadata") or {}).get("folderid") or 0)

    if file_id is not None:
        m = pc.stat_file(cfg, fileid=int(file_id), with_checksum=False) or {}
        if int(m.get("parentfolderid") or -1) != pfid:
            raise RuntimeError("file-id gehört nicht zum angegebenen Parent.")
    else:
        ctop = pc.listfolder(cfg, folderid=int(folder_id), recursive=False, nofiles=True, showpath=False)
        m = ctop.get("metadata") or {}
        if int(m.get("parentfolderid") or -1) != pfid:
            raise RuntimeError("folder-id gehört nicht zum angegebenen Parent.")


def _compose_path_from_parent(cfg: Dict[str,Any], parentfolderid: int, name: str|None) -> str|None:
    """Baut /pfad/eltern/name anhand des Parent-Ordnerpfades."""
    if not parentfolderid or not name:
        return None
    try:
        p = pc.listfolder(cfg, folderid=int(parentfolderid), recursive=False, nofiles=True, showpath=True)
        md = (p or {}).get("metadata") or {}
        base = md.get("path")
        if base:
            return (base.rstrip("/") + "/" + name).replace("//", "/")
    except Exception:
        pass
    return None


def _print_preview(kind: str, kid: int, name: str, path: str, parentfolderid: int, recursive: bool, dry_run: bool) -> None:
    """Klarer Preview vor dem Löschen (auch im Dry-Run)."""
    method = "deletefile" if kind == "file" else ("deletefolderrecursive" if recursive else "deletefolder")
    tag = "[dry-run] " if dry_run else ""
    safe_path = path or "/"  # nur kosmetisch
    print("Ziel:")
    print(f"  type      : {kind.upper()}")
    print(f"  id        : {kid}")
    print(f"  name      : {name}")
    print(f"  path      : {safe_path}")
    print(f"  parentfid : {parentfolderid}")
    print()
    print(f"{tag}{method} → {('fileid' if kind=='file' else 'folderid')}={kid}  # {safe_path}")


def _delete_file(cfg: Dict[str,Any], *, fileid: int|None=None, path: str|None=None) -> Dict[str,Any]:
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if fileid is not None:
        params["fileid"] = int(fileid)
    elif path is not None:
        params["path"] = pc._norm_remote_path(path)
    else:
        raise ValueError("delete_file: fileid ODER path.")
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "deletefile", params)
    pc._expect_ok(top)
    return top

def _delete_folder(cfg: Dict[str,Any], *, folderid: int|None=None, path: str|None=None, recursive: bool=False) -> Dict[str,Any]:
    method = "deletefolderrecursive" if recursive else "deletefolder"
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if folderid is not None:
        params["folderid"] = int(folderid)
    elif path is not None:
        params["path"] = pc._norm_remote_path(path)
    else:
        raise ValueError("delete_folder: folderid ODER path.")
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], method, params)
    pc._expect_ok(top)
    return top

def _trash_clear(cfg: Dict[str,Any], *, fileid: int|None=None, folderid: int|None=None) -> Dict[str,Any]:
    if (fileid is None) == (folderid is None):
        raise ValueError("trash_clear: genau eines von fileid/folderid.")
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if fileid is not None:
        params["fileid"] = int(fileid)
    else:
        params["folderid"] = int(folderid)
    top, _ = pc._rpc(cfg["host"], cfg["port"], cfg["timeout"], "trash_clear", params)
    pc._expect_ok(top)
    return top


def _print_result(kind: str,
                  deleted_meta: dict | None,
                  fallback_id: int | None,
                  path: str | None) -> None:
    """
    Freundliche Summary nach dem Delete:
      - kind: "file" | "folder"
      - deleted_meta: Server-Rückgabe (kann bei recursive nur Counters enthalten)
      - fallback_id: bekannte ID (falls metadata keine ID enthält)
      - path: bereits aufgelöster vollständiger Pfad
    """
    print("Ergebnis:")

    # Nichts bekommen? Wenigstens ID/Pfad zeigen.
    if not isinstance(deleted_meta, dict) or not deleted_meta:
        if kind == "file":
            print("  type      : FILE")
        else:
            print("  type      : FOLDER")
        if fallback_id is not None:
            print(f"  id        : {fallback_id}")
        if path:
            print(f"  path      : {path}")
        return

    is_folder_meta = bool(deleted_meta.get("isfolder"))
    if kind == "file":
        print("  type      : FILE")
        fid = deleted_meta.get("fileid") or deleted_meta.get("id") or fallback_id
        if fid is not None: print(f"  id        : {fid}")
        name = deleted_meta.get("name")
        if name:            print(f"  name      : {name}")
        rpath = deleted_meta.get("path") or path
        if rpath:           print(f"  path      : {rpath}")
        return

    # kind == "folder"
    if is_folder_meta:
        print("  type      : FOLDER")
        fid = deleted_meta.get("folderid") or fallback_id
        if fid is not None: print(f"  id        : {fid}")
        name = deleted_meta.get("name")
        if name:            print(f"  name      : {name}")
        rpath = deleted_meta.get("path") or path
        if rpath:           print(f"  path      : {rpath}")
        return

    # Rekursiv: nur Counters (keine Folder-Meta)
    print("  type      : FOLDER (recursive, counters)")
    if fallback_id is not None:
        print(f"  id        : {fallback_id}")
    if path:
        print(f"  path      : {path}")
    for k in ("deletedfiles", "deletedfolders", "failed", "deleted", "errors"):
        if k in deleted_meta:
            print(f"  {k:12}: {deleted_meta[k]}")


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="Sicheres Löschen (pCloud Binary-API), mit Dry-Run & Bestätigung.")

    # Direkt-Target
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--file-id", type=int)
    g.add_argument("--file-path")
    g.add_argument("--folder-id", type=int)
    g.add_argument("--folder-path")

    # Parent + Name
    ap.add_argument("--parent-folderid", type=int)
    ap.add_argument("--parent-path")
    ap.add_argument("--file-name")
    ap.add_argument("--folder-name")

    # Validierung zusätzlich
    ap.add_argument("--verify-under-parent", action="store_true")

    # Verhalten
    ap.add_argument("--recursive", action="store_true", help="Ordner rekursiv löschen.")
    ap.add_argument("--confirm-name", help="Erforderlich bei --recursive (exakter Ordnername).")
    ap.add_argument("--trash-clear", action="store_true", help="Papierkorb-Eintrag endgültig entfernen.")
    ap.add_argument("--print-metadata", action="store_true", help="Vorab Stat ausgeben (hilfreich bei Fehleranalyse).")
    ap.add_argument("--yes", "--confirm", dest="yes", action="store_true", help="Live ausführen (ohne = Dry-Run).")
    ap.add_argument("--dry-run", action="store_true", help="Expliziter Dry-Run (Default, wenn --yes fehlt).")

    # Config
    ap.add_argument("--env-file")
    ap.add_argument("--profile")
    ap.add_argument("--env-dir")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--device")
    ap.add_argument("--token")

    args = ap.parse_args()
    if not args.yes:
        args.dry_run = True

    # Config ziehen
    cfg = pc.effective_config(
        env_file=args.env_file,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token},
        profile=args.profile,
        env_dir=args.env_dir
    )

    # Root-Guards
    if args.folder_id is not None and int(args.folder_id) == 0:
        print("Sicherheitsabbruch: folderid=0 (Root) wird nie gelöscht.", file=sys.stderr)
        sys.exit(2)
    if args.folder_path is not None and pc._norm_remote_path(args.folder_path) == "/":
        print("Sicherheitsabbruch: '/' wird nie gelöscht.", file=sys.stderr)
        sys.exit(2)

    # Ziel ermitteln
    direct = any([args.file_id, args.file_path, args.folder_id, args.folder_path])
    parent_name = ((args.parent_folderid is not None or args.parent_path is not None)
                   and (args.file_name is not None or args.folder_name is not None))
    parent_id_val = ((args.parent_folderid is not None or args.parent_path is not None)
                     and (args.file_id is not None or args.folder_id is not None))

    if (direct and parent_name) or (parent_name and parent_id_val) or (direct and parent_id_val and not args.verify_under_parent):
        print("Fehler: Entweder Direkt-Target ODER Parent+Name. Parent+ID nur mit --verify-under-parent.", file=sys.stderr)
        sys.exit(2)

    try:
        if direct:
            # genau eine der 4 Optionen nutzen
            kind, kid, name, path, pfid = pc.resolve_target_direct(
                cfg,
                file_id=args.file_id, file_path=args.file_path,
                folder_id=args.folder_id, folder_path=args.folder_path
            )

        elif parent_name:
            # Parent bestimmen
            if args.parent_folderid is not None:
                pmd   = pc.get_folder_meta(cfg, folderid=int(args.parent_folderid), showpath=True) or {}
            else:
                pmd   = pc.get_folder_meta(cfg, path=pc._norm_remote_path(args.parent_path), showpath=True) or {}

            pfid  = int(pmd.get("folderid") or 0)
            ppath = pmd.get("path") or pc.resolve_full_path_for_folderid(cfg, pfid)

            kids = pc.list_folder_children(cfg, folderid=pfid, recursive=False, include_files=True, showpath=True)
            matches = []
            for ch in kids:
                if args.file_name and (not ch.get("isfolder")) and ch.get("name") == args.file_name:
                    matches.append(("file", int(ch.get("fileid")), ch.get("name"),
                                    ch.get("path") or (ppath.rstrip("/") + "/" + ch.get("name"))))
                if args.folder_name and ch.get("isfolder") and ch.get("name") == args.folder_name:
                    matches.append(("folder", int(ch.get("folderid")), ch.get("name"),
                                    ch.get("path") or (ppath.rstrip("/") + "/" + ch.get("name"))))

            if not matches:
                raise FileNotFoundError("Kein passender Eintrag unter dem Parent gefunden.")
            if len(matches) > 1:
                raise RuntimeError("Mehrdeutiger Treffer unter dem Parent.")

            kind, kid, name, path = matches[0]

        elif parent_id_val and args.verify_under_parent:
            # Zugehörigkeit prüfen …
            pc.verify_child_under_parent(
                cfg,
                parent_folderid=args.parent_folderid, parent_path=args.parent_path,
                file_id=args.file_id, folder_id=args.folder_id
            )
            # … und anschließend wie Direkt-Target auflösen (damit name/path schön befüllt sind)
            if args.file_id:
                kind, kid, name, path, pfid = pc.resolve_target_direct(cfg, file_id=args.file_id)
            else:
                kind, kid, name, path, pfid = pc.resolve_target_direct(cfg, folder_id=args.folder_id)

        else:
            print("Fehler: Ziel unklar – nutze Direkt-Target ODER Parent+Name.", file=sys.stderr)
            sys.exit(2)

    except FileNotFoundError as e:
        print(f"Nicht gefunden: {e}", file=sys.stderr); sys.exit(3)
    except Exception as e:
        print(f"Auflösung fehlgeschlagen: {e}", file=sys.stderr); sys.exit(4)

    # recursive-Guard
    if args.recursive:
        if kind != "folder":
            print("Sicherheitsabbruch: --recursive nur für Ordner.", file=sys.stderr); sys.exit(2)
        expected = name or (path.rsplit("/",1)[-1] if path else None)
        if not args.confirm_name or not expected or args.confirm_name != expected:
            print(f"Sicherheitsbremse: --recursive benötigt --confirm-name {expected}", file=sys.stderr); sys.exit(2)

    # optional Vorab-Stat
    if args.print_metadata:
        try:
            if kind == "file":
                md = pc.stat_file(cfg, fileid=kid, with_checksum=False) or {}
                print("Vorab-Stat (FILE):", json.dumps(md, ensure_ascii=False))
            else:
                top = pc.listfolder(cfg, folderid=kid, recursive=False, nofiles=True, showpath=True)
                print("Vorab-Stat (FOLDER):", json.dumps(top.get("metadata") or {}, ensure_ascii=False))
        except Exception as e:
            print(f"Warnung: Vorab-Stat fehlgeschlagen: {e}", file=sys.stderr)

    # immer Preview (auch im Live-Fall)
    _print_preview(kind, kid, name, path, pfid, args.recursive, args.dry_run)

    # Dry-Run?
    if args.dry_run:
        sys.exit(0)

    # Live löschen
    try:
        deleted_fileid = None
        deleted_folderid = None
        deleted_meta = {}

        if kind == "file":
            top = _delete_file(cfg, fileid=kid)
            deleted_meta = (top or {}).get("metadata") or {}
            deleted_fileid = int(deleted_meta.get("fileid") or kid)
        else:
            top = _delete_folder(cfg, folderid=kid, recursive=bool(args.recursive))
            deleted_meta = (top or {}).get("metadata") or {}
            if deleted_meta.get("isfolder"):
                deleted_folderid = int(deleted_meta.get("folderid") or kid)
            else:
                # recursive: häufig nur Counters
                deleted_folderid = kid

        print("Gelöscht.")
        _print_result(kind, deleted_meta, deleted_fileid or deleted_folderid or kid, path)

        if args.trash_clear:
            try:
                if deleted_fileid is not None:
                    _trash_clear(cfg, fileid=int(deleted_fileid))
                    print("Papierkorb-Eintrag (Datei) endgültig entfernt.")
                elif deleted_folderid is not None:
                    _trash_clear(cfg, folderid=int(deleted_folderid))
                    print("Papierkorb-Eintrag (Ordner) endgültig entfernt.")
                else:
                    print("Hinweis: Konnte keine ID für trash_clear ableiten.")
            except Exception as e:
                print(f"Warnung: trash_clear fehlgeschlagen: {e}", file=sys.stderr)

        sys.exit(0)


    except ssl.SSLError as e:
        print(f"TLS/SSL-Fehler: {e}", file=sys.stderr); sys.exit(4)
    except Exception as e:
        msg = str(e).lower()
        if (kind == "folder") and (not args.recursive) and any(s in msg for s in ["not empty", "non-empty", "nicht leer"]):
            print("Ordner ist nicht leer. Tipp: mit --recursive rekursiv löschen.", file=sys.stderr); sys.exit(4)
        print(f"API/Runtime-Fehler: {e}", file=sys.stderr); sys.exit(4)


if __name__ == "__main__":
    main()
