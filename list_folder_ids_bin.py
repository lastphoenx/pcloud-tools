#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
list_folder_ids_bin.py – pCloud Binary Protocol
Ordner-/Dateiübersicht mit Pfad, Tiefe, Pfad-Start, relativen Pfaden, Format-Export, Regex-Filter
NEU: --details / --columns / --filtermeta

Beispiele:
  # Nur Ordner (Default), Root, Tabelle:
  ./list_folder_ids_bin.py

  # Rekursiv, mit Dateien, bis Tiefe 3, Details:
  ./list_folder_ids_bin.py --recursive --include-files --max-depth 3 --details

  # Ab Pfad, als CSV, relative Pfade, nur bestimmte Spalten:
  ./list_folder_ids_bin.py --path /Backup/pfsense --recursive --include-files \
      --columns type,name,id,path,modified,size \
      --relative --format csv

  # Serverseitig Meta-Felder einschränken (optional – wenn Backend es ehrt):
  ./list_folder_ids_bin.py --recursive --filtermeta name,folderid,fileid,parentfolderid,created,modified,size,hash,path,isfolder
"""
import argparse, os, ssl, socket, struct, sys, re, csv, json
from typing import Dict, Any, Union, List, Tuple

# --- Binärformat Konstanten ---
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2

# Antworttypen
TYPE_HASH   = 16
TYPE_ARRAY  = 17
TYPE_FALSE  = 18
TYPE_TRUE   = 19
TYPE_DATA   = 20
TYPE_END    = 255

# ---------- .env laden ----------
def load_env(env_path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.isfile(env_path):
        return data
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip(); v = v.strip()
            if v.startswith(("'", '"')) and v.endswith(("'", '"')) and len(v) >= 2:
                v = v[1:-1]
            data[k] = v
    return data

# ---------- Request bauen ----------
def build_request(method: str, params: Dict[str, Union[str,int,bool]], *, data_len: int = 0) -> bytes:
    mb = method.encode("utf-8")
    has_data = 1 if data_len > 0 else 0
    parts = []
    for name, value in params.items():
        nb = name.encode("utf-8")
        if isinstance(value, bool):
            parts.append(bytes([(PARAM_BOOL<<6)|len(nb)]) + nb + (b"\x01" if value else b"\x00"))
        elif isinstance(value, int):
            parts.append(bytes([(PARAM_NUMBER<<6)|len(nb)]) + nb + struct.pack(LE_U64, value))
        else:
            vb = str(value).encode("utf-8")
            parts.append(bytes([(PARAM_STRING<<6)|len(nb)]) + nb + struct.pack(LE_U32, len(vb)) + vb)
    body = bytearray()
    method_len = (len(mb) & 0x7F) | (0x80 if has_data else 0x00)
    body.append(method_len)
    if has_data:
        body += struct.pack(LE_U64, data_len)
    body += mb
    body.append(len(params))
    body += b"".join(parts)
    return struct.pack(LE_U16, len(body)) + bytes(body)

def recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise ConnectionError("Verbindung geschlossen")
        buf += c
    return bytes(buf)

# ---------- Decoder ----------
class BinDecoder:
    def __init__(self, payload: bytes):
        self.b = payload; self.i = 0; self.string_table: List[str] = []
    def take(self, n:int)->bytes:
        if self.i+n > len(self.b): raise ValueError("Antwort zu kurz")
        out = self.b[self.i:self.i+n]; self.i += n; return out
    def u8(self): return self.take(1)[0]
    def read_string(self, t:int)->str:
        if 100 <= t <= 149:
            ln = t - 100
            s = self.take(ln).decode("utf-8","replace"); self.string_table.append(s); return s
        if t in (0,1,2,3):
            size = {0:1,1:2,2:3,3:4}[t]; ln = int.from_bytes(self.take(size),"little")
            s = self.take(ln).decode("utf-8","replace"); self.string_table.append(s); return s
        if 150 <= t <= 199:
            return self.string_table[t-150]
        if t in (4,5,6,7):
            size = {4:1,5:2,6:3,7:4}[t]; sid = int.from_bytes(self.take(size),"little"); return self.string_table[sid]
        raise ValueError(f"String-Typ unbekannt: {t}")
    def read_number(self, t:int)->int:
        if 200 <= t <= 219: return t-200
        if 8 <= t <= 15: size = t-7; return int.from_bytes(self.take(size),"little")
        raise ValueError(f"Number-Typ unbekannt: {t}")
    def read_value(self):
        t = self.u8()
        if t == TYPE_HASH:
            obj = {}
            while True:
                if self.b[self.i] == TYPE_END: self.i += 1; break
                k = self.read_value(); v = self.read_value(); obj[k] = v
            return obj
        if t == TYPE_ARRAY:
            arr = []
            while True:
                if self.b[self.i] == TYPE_END: self.i += 1; break
                arr.append(self.read_value())
            return arr
        if t == TYPE_FALSE: return False
        if t == TYPE_TRUE:  return True
        if t == TYPE_DATA:
            dlen = int.from_bytes(self.take(8),"little"); return {"__data_len__": dlen}
        if t in list(range(100,150)) + [0,1,2,3,4,5,6,7] or 150 <= t <= 199: return self.read_string(t)
        if t in list(range(8,16)) + list(range(200,220)): return self.read_number(t)
        raise ValueError(f"Unbekannter Typcode: {t}")

def decode(payload: bytes) -> Dict[str, Any]:
    dec = BinDecoder(payload)
    top = dec.read_value()
    if not isinstance(top, dict):
        raise ValueError("Top-Level ist kein Hash")
    return top

# ---------- Pfad-Helfer ----------
def norm_root(path: str) -> str:
    """Startpfad normalisieren: führender '/', doppelte // entfernen, trailing '/' entfernen (außer '/')."""
    if not path: return "/"
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    while '//' in path:
        path = path.replace('//','/')
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"

def join_path(parent: str, name: str) -> str:
    if not parent or parent == "/":
        if name == "/" or not name:
            return "/"
        return "/" + name
    if name == "/":
        return parent if parent.startswith("/") else "/" + parent
    return (parent.rstrip("/") + "/" + name)

# ---------- Baumlauf ----------
# Row als Dict -> flexibel für Spalten
def make_row(typ: str, name: str, idv: Union[int,None], parent: Union[int,None], path: str, depth: int, md: Dict[str, Any]) -> Dict[str, Any]:
    row = {
        "type": typ,
        "name": name,
        "id": idv,
        "parent": parent,
        "path": path,
        "depth": depth,
        # Zusatzfelder, wenn vorhanden:
        "created": md.get("created"),
        "modified": md.get("modified"),
        "size": md.get("size"),
        "contenttype": md.get("contenttype"),
        "hash": md.get("hash"),
        "category": md.get("category"),
    }
    return row

def walk_metadata(
    md: Dict[str, Any],
    rows: List[Dict[str, Any]],
    *,
    parent_path: str,
    include_files: bool,
    prefer_server_path: bool,
    depth: int,
    max_depth: Union[int, None],
    start_path: Union[str, None]
):
    isfolder = md.get("isfolder", False)
    name = md.get("name", "?")
    server_path = md.get("path") if prefer_server_path else None

    # Pfad bestimmen:
    if depth == 1:
        if start_path:
            my_path = server_path or start_path
        elif (md.get("folderid") == 0 or name == "/"):
            my_path = server_path or "/"
        else:
            my_path = server_path or join_path(parent_path or "/", name)
    else:
        my_path = server_path or join_path(parent_path or "/", name)

    parent = md.get("parentfolderid", None)

    if isfolder:
        rows.append(make_row("FOLDER", name, md.get("folderid"), parent, my_path, depth, md))
        if (max_depth is not None) and (depth >= max_depth):
            return
        for item in (md.get("contents") or []):
            walk_metadata(
                item, rows,
                parent_path=my_path,
                include_files=include_files,
                prefer_server_path=prefer_server_path,
                depth=depth+1,
                max_depth=max_depth,
                start_path=start_path
            )
    else:
        if include_files:
            file_path = server_path or join_path(parent_path or "/", name)
            rows.append(make_row("FILE", name, md.get("fileid"), parent, file_path, depth, md))

# ---------- RPC Helper ----------
def rpc(host: str, port: int, timeout: int, method: str, params: Dict[str, Union[str,int,bool]]) -> Dict[str, Any]:
    req = build_request(method, params)
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.settimeout(timeout)
    tls.sendall(req)
    resp_len = struct.unpack(LE_U32, recv_exact(tls, 4))[0]
    payload = recv_exact(tls, resp_len)
    tls.close()
    top = decode(payload)
    if top.get("result") != 0:
        raise RuntimeError(f"{method} fehlgeschlagen: {top}")
    return top

# ---------- Ausgabe ----------
DEFAULT_COLS = ["type","name","id","parent","path"]
DETAIL_COLS = ["created","modified","size","contenttype","hash"]

def print_table(rows: List[Dict[str,Any]], header_note: str, columns: List[str]):
    print(header_note)
    # simple, feste Spaltenbreiten für die ersten paar, Rest rechts anhängen
    hdr = "  ".join(col.upper() for col in columns)
    print(hdr)
    print("-" * max(80, len(hdr)+10))
    for r in rows:
        vals = []
        for col in columns:
            val = r.get(col, "")
            if val is None: val = ""
            vals.append(str(val))
        print("  ".join(vals))

def print_delimited(rows: List[Dict[str,Any]], dialect: str, columns: List[str]):
    writer = csv.writer(sys.stdout, delimiter=('\t' if dialect == 'tsv' else ','), quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r.get(c, "") if r.get(c, "") is not None else "" for c in columns])

def print_json(rows: List[Dict[str,Any]], columns: List[str]):
    out = [{c: (row.get(c, None)) for c in columns} for row in rows]
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()

def _norm_remote_path(p: str) -> str:
    if not p: return "/"
    p = p.strip()
    if not p.startswith("/"): p = "/" + p
    while "//" in p: p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"): p = p[:-1]
    return p or "/"

def ensure_remote_path(host: str, port: int, timeout: int, token: str, device: str, full_path: str) -> int:
    """
    Legt einen pCloud-Pfad (rekursiv) an und gibt die folderid zurück.
    Verwendet listfolder/createfolder via Binary-API. Idempotent.
    """
    full_path = _norm_remote_path(full_path)
    if full_path == "/":
        top = rpc(host, port, timeout, "listfolder", {"access_token": token, "device": device, "path": "/", "showpath": True})
        return int(top.get("metadata", {}).get("folderid", 0))

    parts = [p for p in full_path.split("/") if p]
    cur = "/"
    folderid = None
    for i in range(len(parts)):
        cur = "/" + "/".join(parts[:i+1])
        # existiert schon?
        try:
            top = rpc(host, port, timeout, "listfolder",
                      {"access_token": token, "device": device, "path": cur, "showpath": True})
            folderid = top.get("metadata", {}).get("folderid")
            continue
        except Exception:
            # anlegen
            top = rpc(host, port, timeout, "createfolder",
                      {"access_token": token, "device": device, "path": cur})
            folderid = top.get("metadata", {}).get("folderid")

    return int(folderid or 0)

def row_from_meta(meta: dict, path_hint: str = None) -> dict:
    """Konvertiert pCloud-Metadaten (Ordner oder Datei) in eine Row für die Ausgabe."""
    isfolder = bool(meta.get("isfolder"))
    r = {
        "type": "FOLDER" if isfolder else "FILE",
        "name": meta.get("name") or ("/" if isfolder and not meta.get("name") else ""),
        "id": meta.get("folderid") if isfolder else (meta.get("fileid") or meta.get("id")),
        "parent": meta.get("parentfolderid") if not isfolder else (meta.get("parentfolderid") or meta.get("folderid_parent") or None),
        "path": path_hint,  # bei Ordnern/Dateien setzen wir das explizit
        # Detailspalten:
        "created": meta.get("created"),
        "modified": meta.get("modified"),
        "size": meta.get("size"),
        "contenttype": meta.get("contenttype"),
        "hash": meta.get("hash"),
    }
    return r

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="pCloud Binary listfolder – Pfade, Tiefe, Start per Pfad + Details/Spalten/Filtermeta.")
    ap.add_argument("--env-file", default="/opt/entropywatcher/pcloud/.env", help="Pfad zur .env (Default: %(default)s)")

    ap.add_argument("--host", help="API-Host (Default aus .env oder eapi.pcloud.com)")
    ap.add_argument("--port", type=int, help="Port (Default aus .env oder 8399)")
    ap.add_argument("--timeout", type=int, help="Timeout Sekunden (Default aus .env oder 30)")
    ap.add_argument("--device", help="device Kennung (Default aus .env oder 'entropywatcher/raspi')")

    start = ap.add_mutually_exclusive_group()
    start.add_argument("--folderid", type=int, help="Start-Ordner (Default aus .env oder 0)")
    start.add_argument("--path", help="Startpfad (z. B. /Backup/Ordner)")

    ap.add_argument("--recursive", action="store_true", help="Rekursiv durchlaufen (Default: aus = nur aktuelle Ebene)")
    ap.add_argument("--include-files", action="store_true", help="Zusätzlich Dateien anzeigen (Default: aus)")
    ap.add_argument("--only-folders", action="store_true", help="Explizit nur Ordner (überschreibt --include-files)")
    ap.add_argument("--max-depth", type=int, default=None, help="Maximale Tiefe (1 = nur Startordner). Ohne Angabe: unbegrenzt.")
    ap.add_argument("--server-path", action="store_true", help="Falls vorhanden, 'path' aus Antwort bevorzugen")
    ap.add_argument("--nofiles-server", action="store_true", help="Serverseitig Dateien weglassen (setzt nofiles=1)")
    ap.add_argument("--filtermeta", help="Kommagetrennte Feldliste, die der Server liefern soll (Traffic sparen, wenn unterstützt)")

    ap.add_argument("--relative", action="store_true", help="Pfad relativ zum Start ausgeben")
    ap.add_argument("--format", choices=["table","csv","tsv","json"], default="table", help="Ausgabeformat (Default: table)")
    ap.add_argument("--details", action="store_true", help="Zusatzfelder (created, modified, size, contenttype, hash) ausgeben")
    ap.add_argument("--columns", help="Kommagetrennte Spaltenliste (z. B. 'type,name,id,path,modified,size'). Überschreibt --details.")

    ap.add_argument("--match", help="Regex zum Filtern nach NAME (nicht PATH).")
    ap.add_argument("--match-ignore-case", action="store_true", help="Regex Case-Insensitive")

    ap.add_argument("--ensure-path", action="store_true", help="Pfad unter --path rekursiv anlegen, falls nicht vorhanden.")
    ap.add_argument("--print-folderid", action="store_true", help="Nur die folderid der (ggf. angelegten) Pfadwurzel als JSON auf STDOUT ausgeben und beenden.")
    ap.add_argument("--file", action="store_true", help="Behandle --path als *Datei*-Pfad: zeige zuerst den Elternordner (Details), dann die Datei (Details)."
)

    args = ap.parse_args()
    env = load_env(args.env_file)

    token = env.get("PCLOUD_TOKEN") or os.environ.get("PCLOUD_TOKEN")
    if not token:
        print("Fehler: Kein Token gefunden. Bitte PCLOUD_TOKEN in .env setzen.", file=sys.stderr)
        sys.exit(2)

    host = args.host or env.get("PCLOUD_HOST", "eapi.pcloud.com")
    port = args.port or int(env.get("PCLOUD_PORT", "8399"))
    timeout = args.timeout or int(env.get("PCLOUD_TIMEOUT", "30"))
    device = args.device or env.get("PCLOUD_DEVICE", "entropywatcher/raspi")

    # Frühzeitiger Pfad-Ensure / FolderID-Print
    if args.ensure_path or args.print_folderid:
        if not args.path:
            print("Fehler: --ensure-path/--print-folderid benötigt --path.", file=sys.stderr)
            sys.exit(2)
        try:
            fid = ensure_remote_path(host, port, timeout, token, device, args.path)
        except Exception as e:
            print(f"Fehler beim ensure/listfolder für {args.path}: {e}", file=sys.stderr)
            sys.exit(1)

        if args.print_folderid:
            import json
            print(json.dumps({"folderid": fid}))
            sys.exit(0)
        else:
            # Nur ensure gewünscht: kurze Bestätigung und Ende
            print(f"OK: {args.path} (folderid={fid})")
            sys.exit(0)

    use_path = bool(args.path)
    folderid = args.folderid if args.folderid is not None else int(env.get("PCLOUD_DEFAULT_FOLDERID", "0"))
    start_path = norm_root(args.path) if use_path else None

    # --- NEU: Explizite Dateiansicht via --file ---
    if use_path and args.file:
        import os as _os

        file_path = start_path                         # absoluter Remote-Pfad (normalisiert)
        parent_path = _os.path.dirname(file_path) or "/"
        if parent_path != "/":
            parent_path = parent_path if parent_path.startswith("/") else "/" + parent_path

        # 1) Elternordner holen (Details)
        try:
            top_parent = rpc(host, port, timeout, "listfolder", {
                "access_token": token, "device": device,
                "path": parent_path, "showpath": 1
            })
        except Exception as e:
            print(f"Fehler bei listfolder (Elternordner): {e}", file=sys.stderr)
            sys.exit(1)

        mdp = top_parent.get("metadata") or {}
        # Der Ordner selbst ist in metadata; pflege PATH:
        parent_row = row_from_meta(mdp, path_hint=parent_path)
    
        # 2) Datei-Stat (Details)
        try:
            top_file = rpc(host, port, timeout, "stat", {
                "access_token": token, "device": device,
                "path": file_path
            })
        except Exception as e:
            print(f"Fehler bei stat (Datei): {e}", file=sys.stderr)
            sys.exit(1)

        mdf = top_file.get("metadata") or top_file.get("file") or {}
        if not mdf or mdf.get("isfolder"):
            print(f"Fehler: {file_path} ist keine Datei (oder nicht gefunden).", file=sys.stderr)
            sys.exit(2)

        file_row = row_from_meta(mdf, path_hint=file_path)

        # Ausgabe zusammenbauen: erst Ordner, dann Datei
        rows = [parent_row, file_row]

        # Spaltenlogik wie gewohnt
        if args.columns:
            columns = [c.strip() for c in args.columns.split(",") if c.strip()]
        elif args.details:
            columns = DEFAULT_COLS + DETAIL_COLS
        else:
            columns = DEFAULT_COLS

        # Header
        header_note = f"\nInhalt (Ordner & Datei) zu {file_path}:"

        # Ausgabeformat wie gewohnt
        if args.format == "table":
            print_table(rows, header_note, columns)
        elif args.format == "csv":
            print_delimited(rows, "csv", columns)
        elif args.format == "tsv":
            print_delimited(rows, "tsv", columns)
        elif args.format == "json":
            print_json(rows, columns)
        else:
            print_table(rows, header_note, columns)
        sys.exit(0)

    # Tiefe/Rekursion
    if args.max_depth is not None:
        if args.max_depth <= 0:
            print("Hinweis: --max-depth <= 0 ist ungültig. Verwende 1 für nur aktuelle Ebene.", file=sys.stderr)
            sys.exit(2)
        want_recursive = args.max_depth > 1
        max_depth = args.max_depth
    else:
        want_recursive = bool(args.recursive)
        max_depth = None

    # Request-Parameter
    params = {"access_token": token, "device": device}
    if use_path:
        params["path"] = start_path
    else:
        params["folderid"] = folderid
    if want_recursive:
        params["recursive"] = 1
    if args.nofiles_server:
        params["nofiles"] = 1
    if args.filtermeta:
        params["filtermeta"] = args.filtermeta  # optional; wird ignoriert, falls nicht unterstützt

    # RPC
    try:
        top = rpc(host, port, timeout, "listfolder", params)
    except Exception as e:
        print(f"Fehler bei listfolder: {e}", file=sys.stderr)
        sys.exit(1)

    md = top.get("metadata")
    if not isinstance(md, dict):
        print("Fehler: 'metadata' fehlt/unerwartet:", type(md), file=sys.stderr)
        sys.exit(1)

    # Zeilen sammeln
    rows: List[Dict[str,Any]] = []
    parent_path = start_path if use_path else ""
    include_files = False if args.only_folders else bool(args.include_files)
    prefer_server_path = bool(args.server_path)

    walk_metadata(
        md, rows,
        parent_path=parent_path,
        include_files=include_files,
        prefer_server_path=prefer_server_path,
        depth=1,
        max_depth=max_depth,
        start_path=start_path
    )

    # Filter (Regex auf NAME)
    if args.match:
        flags = re.IGNORECASE if args.match_ignore_case else 0
        try:
            rx = re.compile(args.match, flags)
        except re.error as ex:
            print(f"Ungültiger Regex in --match: {ex}", file=sys.stderr)
            sys.exit(2)
        rows = [r for r in rows if rx.search(r.get("name") or "")]

    # relative Pfade
    if args.relative and rows:
        base = start_path if use_path else rows[0].get("path") or "/"
        base_clean = base.rstrip("/")
        def rel(p: str) -> str:
            if p == base: return "."
            if p.startswith(base_clean + "/"): return p[len(base_clean)+1:]
            return p
        for r in rows:
            p = r.get("path") or ""
            r["path"] = rel(p)

    # Spalten bestimmen
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    elif args.details:
        columns = DEFAULT_COLS + DETAIL_COLS
    else:
        columns = DEFAULT_COLS

    # Ausgabe
    root_disp = (start_path or f"folderid={folderid}")
    scope = "rekursiv" if want_recursive else "nicht rekursiv"
    header_note = f"\nInhalt von {root_disp} ({scope}):"

    if args.format == "table":
        print_table(rows, header_note, columns)
    elif args.format == "csv":
        print_delimited(rows, "csv", columns)
    elif args.format == "tsv":
        print_delimited(rows, "tsv", columns)
    elif args.format == "json":
        print_json(rows, columns)
    else:
        print_table(rows, header_note, columns)

if __name__ == "__main__":
    main()
