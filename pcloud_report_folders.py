#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
"""
pCloud Folder Report (EU, Binary Protocol) – fast recursive version.

- Default: ein rekursiver API-Call (listfolderrecursive) → sehr schnell, kein Loop.
- Fallback: listfolder mit recursive=True, falls listfolderrecursive nicht verfügbar.
- Optional: langsamer Walk-Modus (--slow), falls du es erzwingen willst.

Token-Quellen (in Reihenfolge):
  1) /opt/entropywatcher/pcloud/token.json  -> {"access_token":"..."}
  2) /opt/entropywatcher/pcloud/.env        -> PCLOUD_TOKEN=...
  3) Umgebungsvariable PCLOUD_TOKEN

Beispiele:
  python pcloud_report_folders.py walk --folderid 0
  python pcloud_report_folders.py walk --path /Backup --csv folders.csv
  python pcloud_report_folders.py walk --folderid 0 --max-depth 3 --limit 5000
"""

import argparse
import csv
import json
import os
import socket
import ssl
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

# ------------------- Defaults (EU) -------------------
HOST_DEFAULT = "eapi.pcloud.com"
PORT_DEFAULT = 8399
TIMEOUT_DEFAULT = 30
DEVICE_DEFAULT = "entropywatcher/raspi"

BASE_DIR   = "/opt/entropywatcher/pcloud"
TOKEN_FILE = f"{BASE_DIR}/token.json"
ENV_FILE   = f"{BASE_DIR}/.env"

# ------------------- Binary protocol const -------------------
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PT_STR, PT_NUM, PT_BOOL = 0, 1, 2
VT_HASH, VT_ARRAY, VT_FALSE, VT_TRUE, VT_DATA, VT_END = 16, 17, 18, 19, 20, 255
STR_INLINE_MIN, STR_INLINE_MAX = 100, 149
STR_REUSE_MIN, STR_REUSE_MAX   = 150, 199
NUM_INLINE_MIN, NUM_INLINE_MAX = 200, 219

class ProtoErr(RuntimeError): pass

# ------------------- Minimal dotenv (no dependency) -------------------
def load_dotenv_minimal(path: str) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    if not os.path.isfile(path):
        return vals
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            vals[k] = v
    return vals

def load_token_and_host() -> Tuple[str, str, int, int, str]:
    token = ""
    try:
        if os.path.isfile(TOKEN_FILE):
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in ("access_token", "token", "BearerToken", "bearer"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    token = v.strip()
                    break
    except Exception:
        token = ""

    envvals = load_dotenv_minimal(ENV_FILE)
    if not token:
        token = (envvals.get("PCLOUD_TOKEN") or "").strip()
    if not token:
        token = (os.getenv("PCLOUD_TOKEN") or "").strip()

    if not token:
        raise SystemExit(
            f"Kein Token gefunden. Lege {TOKEN_FILE} mit {{\"access_token\":\"…\"}} an "
            f"oder setze PCLOUD_TOKEN in {ENV_FILE} / als Umgebungsvariable."
        )

    host = (envvals.get("PCLOUD_HOST") or os.getenv("PCLOUD_HOST") or HOST_DEFAULT).strip() or HOST_DEFAULT
    port = int(envvals.get("PCLOUD_PORT") or os.getenv("PCLOUD_PORT") or PORT_DEFAULT)
    timeout = int(envvals.get("PCLOUD_TIMEOUT_SECS") or os.getenv("PCLOUD_TIMEOUT_SECS") or TIMEOUT_DEFAULT)
    device = (envvals.get("PCLOUD_DEVICE") or os.getenv("PCLOUD_DEVICE") or DEVICE_DEFAULT).strip() or DEVICE_DEFAULT
    return token, host, port, timeout, device

# ------------------- Binary helpers -------------------
def build_request(method: str, params: Dict[str, Union[str,int,bool]], has_data=False, data_len=0) -> bytes:
    mb = method.encode("utf-8")
    if not (0 < len(mb) < 128):
        raise ValueError("method name 1..127 bytes")
    parts = []
    for name, value in params.items():
        nb = name.encode("utf-8")
        if len(nb) > 63:
            raise ValueError("param name too long")
        if isinstance(value, bool):
            header = bytes([(PT_BOOL<<6)|len(nb)]) + nb
            enc = b"\x01" if value else b"\x00"
        elif isinstance(value, int):
            header = bytes([(PT_NUM<<6)|len(nb)]) + nb
            enc = struct.pack(LE_U64, value)
        else:
            vb = str(value).encode("utf-8")
            header = bytes([(PT_STR<<6)|len(nb)]) + nb
            enc = struct.pack(LE_U32, len(vb)) + vb
        parts.append(header + enc)
    blob = b"".join(parts)
    first = (len(mb) & 0x7F) | (0x80 if has_data else 0x00)
    body = bytes([first]) + (struct.pack(LE_U64, data_len) if has_data else b"") + mb + bytes([len(params)]) + blob
    if len(body) >= 65536:
        raise ValueError("request too large")
    return struct.pack(LE_U16, len(body)) + body

def recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)

def call_binary(host: str, port: int, timeout: int, method: str, params: Dict[str, Union[str,int,bool]]) -> bytes:
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.settimeout(timeout)
    req = build_request(method, params)
    tls.sendall(req)
    resp_len = struct.unpack(LE_U32, recv_exact(tls, 4))[0]
    payload  = recv_exact(tls, resp_len)
    try:
        tls.close()
    except Exception:
        pass
    return payload

# ------------------- Parser -------------------
def parse_response(payload: bytes) -> Dict[str, object]:
    p = 0
    strings: List[str] = []

    def need(n: int) -> bytes:
        nonlocal p
        if p + n > len(payload): raise ProtoErr("truncated")
        b = payload[p:p+n]; p += n; return b
    def ru8() -> int:  return need(1)[0]
    def ru16() -> int: return struct.unpack(LE_U16, need(2))[0]
    def ru32() -> int: return struct.unpack(LE_U32, need(4))[0]
    def ru64() -> int: return struct.unpack(LE_U64, need(8))[0]

    def read_string_with_tag(tag: int) -> str:
        if STR_INLINE_MIN <= tag <= STR_INLINE_MAX:
            ln = tag - STR_INLINE_MIN
            s = need(ln).decode("utf-8", "replace")
            strings.append(s); return s
        if STR_REUSE_MIN <= tag <= STR_REUSE_MAX:
            idx = tag - STR_REUSE_MIN
            if 0 <= idx < len(strings): return strings[idx]
            raise ProtoErr(f"bad reused str id {idx}")
        if 4 <= tag <= 7:  # reuse id len 1..4
            id_len = tag - 3
            b = need(id_len)
            idx = int.from_bytes(b, "little")
            if 0 <= idx < len(strings): return strings[idx]
            raise ProtoErr(f"bad reused str id {idx}")
        if tag in (0, 1, 2, 3):
            sz = tag + 1
            if sz == 1: ln = ru8()
            elif sz == 2: ln = ru16()
            elif sz == 3:
                b = need(3); ln = b[0] | (b[1] << 8) | (b[2] << 16)
            else:
                ln = ru32()
            s = need(ln).decode("utf-8", "replace")
            strings.append(s); return s
        raise ProtoErr(f"unknown string tag {tag}")

    def read_number_with_tag(tag: int) -> int:
        if NUM_INLINE_MIN <= tag <= NUM_INLINE_MAX:
            return tag - NUM_INLINE_MIN
        size_map = {8:1,9:2,10:3,11:4,12:5,13:6,14:7,15:8}
        if tag not in size_map: raise ProtoErr(f"unknown num tag {tag}")
        b = need(size_map[tag]); val = 0
        for i, by in enumerate(b): val |= (by << (8*i))
        return val

    def read_value():
        t = ru8()
        if t in (VT_FALSE, VT_TRUE): return (t == VT_TRUE)
        if t == VT_HASH:  return read_hash()
        if t == VT_ARRAY: return read_array()
        if t == VT_DATA:  length = ru64(); return {"__type":"data","length":length}
        if t in (*range(0,4), *range(4,8), *range(STR_INLINE_MIN,STR_INLINE_MAX+1), *range(STR_REUSE_MIN,STR_REUSE_MAX+1)):
            return read_string_with_tag(t)
        if t in (*range(8,16), *range(NUM_INLINE_MIN,NUM_INLINE_MAX+1)):
            return read_number_with_tag(t)
        raise ProtoErr(f"unknown value tag {t}")

    def read_array():
        nonlocal p
        arr = []
        while True:
            if p < len(payload) and payload[p] == VT_END:
                p += 1; break
            arr.append(read_value())
        return arr

    def read_hash():
        nonlocal p
        obj: Dict[str, object] = {}
        while True:
            if p < len(payload) and payload[p] == VT_END:
                p += 1; break
            k = read_value()
            if not isinstance(k, str): raise ProtoErr("non-string key")
            v = read_value()
            obj[k] = v
        return obj

    root = read_value()
    if not isinstance(root, dict): raise ProtoErr("top-level not hash")
    return root

# ------------------- API wrappers -------------------
def api(method: str, params: Dict[str, Union[str,int,bool]], host: str, port: int, timeout: int) -> Dict[str, object]:
    payload = call_binary(host, port, timeout, method, params)
    return parse_response(payload)

def auth_params(token: str, device: str) -> Dict[str, Union[str,int,bool]]:
    return {"access_token": token, "device": device}

def listfolder(host: str, port: int, timeout: int, token: str, device: str, **kwargs) -> Dict[str, object]:
    p = {**auth_params(token, device), "nofiles": True, "showpath": True, **kwargs}
    return api("listfolder", p, host, port, timeout)

def listfolderrecursive(host: str, port: int, timeout: int, token: str, device: str, **kwargs) -> Dict[str, object]:
    # Einige Deployments haben die Methode als eigenen Namen:
    p = {**auth_params(token, device), "nofiles": True, "showpath": True, **kwargs}
    try:
        return api("listfolderrecursive", p, host, port, timeout)
    except Exception:
        # Fallback: manche akzeptieren listfolder(recursive=True)
        p2 = dict(p); p2["recursive"] = True
        return api("listfolder", p2, host, port, timeout)

def stat_by_path(host: str, port: int, timeout: int, token: str, device: str, path: str) -> Dict[str, object]:
    return api("stat", {**auth_params(token, device), "path": path}, host, port, timeout)

def stat_by_id(host: str, port: int, timeout: int, token: str, device: str, folderid: int) -> Dict[str, object]:
    return api("stat", {**auth_params(token, device), "folderid": folderid}, host, port, timeout)

# ------------------- Resolve start -------------------
def resolve_start(host: str, port: int, timeout: int, token: str, device: str,
                  folderid: Optional[int], path: Optional[str]) -> Tuple[int, str]:
    if path:
        r = stat_by_path(host, port, timeout, token, device, path)
        if int(r.get("result", 1)) != 0:
            raise SystemExit(f"Fehler: stat(path={path!r}) => result={r.get('result')} error={r.get('error')}")
        meta = r.get("metadata") or r.get("meta") or {}
        fid = meta.get("folderid") or meta.get("id")
        if not isinstance(fid, int):
            raise SystemExit("Fehler: Pfad verweist nicht auf einen Ordner")
        norm = meta.get("path") or path
        return int(fid), str(norm)

    fid = int(folderid if folderid is not None else 0)
    if fid == 0:
        # Best guess path for root:
        return 0, "/"

    r = stat_by_id(host, port, timeout, token, device, fid)
    if int(r.get("result", 1)) == 0:
        meta = r.get("metadata") or {}
        return fid, str(meta.get("path") or "/")
    # Fallback:
    r2 = listfolder(host, port, timeout, token, device, folderid=fid)
    if int(r2.get("result", 1)) == 0:
        meta = r2.get("metadata") or {}
        return fid, str(meta.get("path") or "/")
    raise SystemExit(f"Fehler: Konnte Start-Ordner {fid} nicht ermitteln (result={r.get('result')} / {r2.get('result')})")

# ------------------- Model -------------------
@dataclass
class FolderRow:
    folderid: int
    parentid: Optional[int]
    name: str
    path: str
    depth: int

# ------------------- Fast recursive fetch -------------------
def fetch_all_folders_fast(host: str, port: int, timeout: int, token: str, device: str,
                           start_folderid: int, start_path: str) -> List[FolderRow]:
    # Versuche rekursiv in EINEM Call:
    res = listfolderrecursive(host, port, timeout, token, device,
                              folderid=start_folderid if start_folderid != 0 else None,
                              path=None if start_folderid != 0 else "/")
    if int(res.get("result", 1)) != 0:
        raise SystemExit(f"Fehler: listfolderrecursive => result={res.get('result')} error={res.get('error')}")

    meta = res.get("metadata") or {}
    # Die rekursive Antwort liefert alle Einträge in meta["contents"] (Dir + Files),
    # plus nested contents; wir laufen flach darüber und picken nur Ordner.
    rows: List[FolderRow] = []

    def walk_meta(node: Dict, parentid: Optional[int], parent_path: str, depth: int):
        # node kann ein Ordner (isfolder=True) sein oder Datei – wir überspringen Dateien.
        if not isinstance(node, dict):
            return
        if node.get("isfolder"):
            fid = node.get("folderid") or node.get("id")
            name = node.get("name", "")
            if isinstance(fid, int):
                # Pfad: server liefert oft node["path"] – wenn nicht, lokal zusammensetzen.
                pth = node.get("path") or (parent_path.rstrip("/") + "/" + name if parent_path != "/" else "/" + name)
                rows.append(FolderRow(folderid=int(fid), parentid=parentid, name=name, path=pth, depth=depth))
                # Kinder laufen
                for child in node.get("contents") or []:
                    walk_meta(child, int(fid), pth, depth + 1)
        else:
            # Datei: ignorieren
            return

    # Root-Knoten selbst hinzufügen (start_folderid, start_path)
    rows.append(FolderRow(folderid=start_folderid, parentid=None, name=meta.get("name", "/"), path=start_path, depth=0))
    # Und dann alle Kinder des Start-Ordners
    for child in meta.get("contents") or []:
        walk_meta(child, start_folderid if start_folderid != 0 else None, start_path, 1)

    return rows

# ------------------- Slow iterative (fallback/optional) -------------------
def fetch_all_folders_slow(host: str, port: int, timeout: int, token: str, device: str,
                           start_folderid: int, start_path: str,
                           max_depth: Optional[int]=None, limit: Optional[int]=None) -> List[FolderRow]:
    rows: List[FolderRow] = []
    stack: List[Tuple[int, Optional[int], str, str, int]] = []  # (id, parentid, name, path, depth)
    # Start
    try:
        res0 = listfolder(host, port, timeout, token, device, folderid=start_folderid if start_folderid != 0 else None, path="/" if start_folderid == 0 else None)
        meta0 = res0.get("metadata") or {}
        start_name = meta0.get("name", "/")
        start_path = meta0.get("path") or start_path
    except Exception:
        start_name = "/"
    stack.append((start_folderid, None, start_name, start_path, 0))
    seen = set()

    while stack:
        fid, parentid, name, path, depth = stack.pop()
        if fid in seen:
            continue
        seen.add(fid)
        rows.append(FolderRow(folderid=fid, parentid=parentid, name=name, path=path, depth=depth))
        if limit and len(rows) >= limit:
            break
        if max_depth is not None and depth >= max_depth:
            continue

        try:
            r = listfolder(host, port, timeout, token, device, folderid=fid if fid != 0 else None, path="/" if fid == 0 else None)
            meta = r.get("metadata") or {}
            contents = meta.get("contents") or []
        except Exception as e:
            print(f"WARN: listfolder({fid}) -> {e}", file=sys.stderr)
            continue

        for item in contents:
            if not isinstance(item, dict) or not item.get("isfolder"):
                continue
            child_id = item.get("folderid") or item.get("id")
            child_name = item.get("name", "")
            if not isinstance(child_id, int):
                continue
            child_path = path.rstrip("/") + "/" + child_name if path != "/" else "/" + child_name
            stack.append((int(child_id), fid, child_name, child_path, depth + 1))

        # simple cooperativity to avoid long blocking without output
        if len(rows) % 1000 == 0:
            print(f"[progress] folders collected: {len(rows)}", file=sys.stderr)

    return rows

# ------------------- Output helpers -------------------
def print_cli(rows: Iterable[FolderRow]) -> None:
    rows = list(rows)
    idw = max(8, max((len(str(r.folderid)) for r in rows), default=8))
    dw  = max(5, max((len(str(r.depth)) for r in rows), default=5))
    print(f"{'FOLDERID'.ljust(idw)}  {'DEPTH'.ljust(dw)}  PATH")
    print("-" * (idw + dw + 6 + 40))
    for r in sorted(rows, key=lambda x: (x.depth, x.path)):
        print(f"{str(r.folderid).ljust(idw)}  {str(r.depth).ljust(dw)}  {r.path}")

def write_csv(rows: Iterable[FolderRow], out_path: Path) -> None:
    rows = list(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["folderid", "parentid", "depth", "name", "path"])
        for r in rows:
            w.writerow([r.folderid, r.parentid if r.parentid is not None else "", r.depth, r.name, r.path])

def write_json(rows: Iterable[FolderRow], out_path: Path) -> None:
    rows = list(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = [r.__dict__ for r in rows]
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ------------------- CLI -------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="pCloud Folder Report (EU, Binary Protocol) – fast recursive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("walk", help="Alle Ordner rekursiv auflisten")

    g = sp.add_mutually_exclusive_group()
    g.add_argument("--folderid", type=int, help="Start-FolderID (Default 0)")
    g.add_argument("--path", help="Startpfad, z.B. /Backup")

    sp.add_argument("--csv", help="Pfad zur CSV-Datei für Export")
    sp.add_argument("--json", help="Pfad zur JSON-Datei für Export")

    sp.add_argument("--max-depth", type=int, default=None, help="Maximale Tiefe (nur im --slow Modus wirksam)")
    sp.add_argument("--limit", type=int, default=None, help="Max. Anzahl Ordner (nur im --slow Modus wirksam)")
    sp.add_argument("--slow", action="store_true", help="Langsamer Modus (iterativer Walk, viele API Calls)")

    args = ap.parse_args()

    token, host, port, timeout, device = load_token_and_host()
    start_id, start_path = resolve_start(host, port, timeout, token, device, args.folderid, args.path)

    if args.slow:
        rows = fetch_all_folders_slow(host, port, timeout, token, device, start_id, start_path,
                                      max_depth=args.max_depth, limit=args.limit)
    else:
        rows = fetch_all_folders_fast(host, port, timeout, token, device, start_id, start_path)

    print_cli(rows)
    if args.csv:
        write_csv(rows, Path(args.csv))
        print(f"CSV geschrieben: {args.csv}")
    if args.json:
        write_json(rows, Path(args.json))
        print(f"JSON geschrieben: {args.json}")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
