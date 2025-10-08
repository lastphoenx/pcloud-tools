#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_binlib.py – Gemeinsame Hilfsbibliothek für pCloud Binary-API.

Ziele:
- Eine Stelle für Verbindungsaufbau, Request/Response, Fehlerbehandlung.
- Bequeme Wrapper: listfolder, createfolder, ensure_path, stat, checksumfile, upload_chunked.
- Keine Subprozesse zwischen eigenen Skripten nötig.

Konfiguration (.env oder ENV):
  PCLOUD_TOKEN
  PCLOUD_HOST (Default eapi.pcloud.com)
  PCLOUD_PORT (Default 8399)
  PCLOUD_TIMEOUT (Sek., Default 30)
  PCLOUD_DEVICE (Default "entropywatcher/raspi")

Hinweis zur Binary-API:
- Request: 2 Byte Längenfeld (nur Request, ohne Daten), danach Request-Header + Params, optional gefolgt von Daten (falls gesetztes Daten-Flag).
- Response: 4 Byte Länge und danach ein komprimierter Wertbaum (erste Wurzel ist immer Hash mit "result").

Diese Bibliothek dekodiert **nicht** den kompletten Baum generisch,
sondern extrahiert gezielt Felder für die verwendeten Methoden.
"""
from __future__ import annotations
import os, ssl, socket, struct, time, hashlib, inspect
from typing import Any, Dict, Callable, Optional, Tuple

# --- Konstanten / Formate ---
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2

# --- .env laden (mini) ---
def load_env_file(path: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path: return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"): continue
                if "=" not in s: continue
                k, v = s.split("=", 1)
                k = k.strip(); v = v.strip()
                if v and (v[0] == v[-1]) and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[k] = v
    except FileNotFoundError:
        pass
    return out

def _lib_dir() -> str:
    try:
        return os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
    except Exception:
        return os.getcwd()

def _candidate_env_paths(env_file: Optional[str],
                         env_dir: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Liefert (default_env_path, profile_base_dir).
    default_env_path: automatische Standard-.env
    profile_base_dir: Basisordner, in dem wir <profile>.env suchen
    """
    # 1) explizite Vorgaben
    if env_file:
        default_env = env_file
        prof_base = os.path.dirname(os.path.abspath(env_file))
        return default_env, prof_base
    if env_dir:
        default_env = os.path.join(env_dir, ".env")
        return default_env, env_dir

    # 2) ENV-PCLOUD_ENV_FILE (höhere Prio als Auto)
    env_hint = os.environ.get("PCLOUD_ENV_FILE")
    if env_hint:
        default_env = env_hint
        prof_base = os.path.dirname(os.path.abspath(env_hint))
        return default_env, prof_base

    # 3) Auto: zuerst Lib-Ordner, dann CWD
    libdir = _lib_dir()
    cwd = os.getcwd()
    for cand in (os.path.join(libdir, ".env"), os.path.join(cwd, ".env")):
        if os.path.isfile(cand):
            return cand, os.path.dirname(cand)
    # nichts gefunden
    return None, None

def _find_profile_env(profile: Optional[str], profile_base: Optional[str]) -> Optional[str]:
    if not profile: return None
    names = [f"{profile}.env"]
    dirs = []
    if profile_base:
        dirs.append(profile_base)
    libdir = _lib_dir()
    cwd = os.getcwd()
    # in profiles/ und im Basisordner probieren
    dirs.extend([os.path.join(libdir, "profiles"),
                 libdir,
                 os.path.join(cwd, "profiles"),
                 cwd])
    for d in dirs:
        for n in names:
            cand = os.path.join(d, n)
            if os.path.isfile(cand):
                return cand
    return None

# --- Request bauen / senden ---
def _build_request(method: str, params: Dict[str, Any], data_len: int = 0) -> bytes:
    mb = method.encode("utf-8")
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
    first = (len(mb) & 0x7F) | (0x80 if data_len>0 else 0x00)
    body = bytes([first])
    if data_len>0:
        body += struct.pack(LE_U64, data_len)   # <— sofort nach dem ersten Byte
    body += mb
    body += bytes([len(params)])
    body += b"".join(parts)
    return struct.pack(LE_U16, len(body)) + body

def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk: raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)

def _connect(host: str, port: int, timeout: int) -> ssl.SSLSocket:
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.settimeout(timeout)
    return tls

# ---------------------- meta-daten, pfad-details,  row-format-details -----------------------

def row_from_meta(meta: dict, path_hint: str | None = None) -> dict:
    isfolder = bool(meta.get("isfolder"))
    return {
        "type": "FOLDER" if isfolder else "FILE",
        "name": meta.get("name") or ("/" if isfolder and not meta.get("name") else ""),
        "id": meta.get("folderid") if isfolder else (meta.get("fileid") or meta.get("id")),
        "parent": meta.get("parentfolderid") if not isfolder else (meta.get("parentfolderid") or None),
        "path": path_hint,
        "created": meta.get("created"),
        "modified": meta.get("modified"),
        "size": meta.get("size"),
        "contenttype": meta.get("contenttype"),
        "hash": meta.get("hash"),
    }

def stat_path_kind(cfg: dict, path: str) -> tuple[str|None, dict|None]:
    """Ermittelt, ob path Ordner oder Datei ist. Gibt ('folder'|'file'|None, meta) zurück."""
    p = _norm_remote_path(path)
    try:
        top = listfolder(cfg, path=p, recursive=False, nofiles=True, showpath=True)
        md = top.get("metadata") or {}
        if md.get("isfolder"): return "folder", md
    except Exception:
        pass
    try:
        md = stat_file(cfg, path=p, with_checksum=False)
        if md and not md.get("isfolder"): return "file", md
    except Exception:
        pass
    return None, None

def list_folder_children(cfg: dict, *, path: str|None=None, folderid: int|None=None,
                         recursive: bool=False, include_files: bool=False, showpath: bool=False) -> list[dict]:
    top = listfolder(cfg, path=path, folderid=folderid, recursive=recursive,
                     nofiles=(not include_files), showpath=showpath)
    md = top.get("metadata") or {}
    return md.get("contents") or []

def get_folder_and_file_rows(cfg: dict, file_path: str) -> tuple[dict, dict]:
    """Für --file: Ordner-Row + Datei-Row (beide im row-Format) zurückgeben."""
    file_path = _norm_remote_path(file_path)
    parent = os.path.dirname(file_path) or "/"
    pmeta  = get_folder_meta(cfg, path=parent, showpath=True)
    fmeta  = stat_file(cfg, path=file_path, with_checksum=False, enrich_path=False)
    if not fmeta or fmeta.get("isfolder"):
        raise RuntimeError("angegebener Pfad ist keine Datei")
    return (
        row_from_meta(pmeta, path_hint=resolve_full_path_for_folderid(cfg, int(pmeta.get("folderid") or 0))),
        row_from_meta(fmeta, path_hint=file_path),
    )

# --- Wrapper (Kompatibilität: alte Funktionsnamen bleiben nutzbar)

def path_for_folderid(cfg: Dict[str, Any], folderid: int) -> str:
    return resolve_full_path_for_folderid(cfg, folderid)

def path_for_fileid(cfg: Dict[str, Any], fileid: int) -> str:
    return resolve_full_path_for_fileid(cfg, fileid)

# ----------- Pfad Helfer für rekursives durchhangeln bis zu Folder id=0 -----------------------
def get_folder_meta(cfg: dict, *, folderid: int | None = None, path: str | None = None, showpath: bool = True) -> dict:
    """Return folder metadata (single folder), or {} if not found."""
    if (folderid is None) == (path is None):
        raise ValueError("get_folder_meta: provide exactly one of folderid or path.")
    if folderid is not None:
        top = listfolder(cfg, folderid=folderid, recursive=False, nofiles=True, showpath=showpath)
    else:
        top = listfolder(cfg, path=_norm_remote_path(path or "/"), recursive=False, nofiles=True, showpath=showpath)
    return top.get("metadata") or {}

# --- Robuste Pfadauflösung (vollqualifiziert), unabhängig von showpath ---

def resolve_full_path_for_folderid(cfg: Dict[str, Any], folderid: int) -> str:
    fid = int(folderid)
    if fid == 0:
        return "/"

    segments: list[str] = []
    seen: set[int] = set()
    max_hops = 10000  # Zyklusschutz

    while fid != 0 and max_hops > 0:
        max_hops -= 1
        if fid in seen:
            raise RuntimeError("Zyklische parentfolderid-Kette erkannt.")
        seen.add(fid)

        top = listfolder(cfg, folderid=fid, recursive=False, nofiles=True, showpath=False)
        md = (top or {}).get("metadata") or {}
        name = (md.get("name") or "").strip("/")
        pfid = int(md.get("parentfolderid") or 0)
        if name:
            segments.append(name)
        fid = pfid

    return "/" if not segments else ("/" + "/".join(reversed(segments)))

def resolve_full_path_for_fileid(cfg: Dict[str, Any], fileid: int) -> str:
    """
    Vollqualifizierten Pfad für eine Datei ermitteln:
      1) stat(fileid) genau EINMAL (ohne enrich_path),
      2) parentfolderid -> resolve_full_path_for_folderid(),
      3) join(parent_path, name).
    """
    fmeta  = stat_file(cfg, fileid=int(fileid), with_checksum=False, enrich_path=False) or {}
    name  = (fmeta.get("name") or "").strip("/")
    pfid  = int(fmeta.get("parentfolderid") or 0)
    ppath = resolve_full_path_for_folderid(cfg, pfid)
    return (ppath.rstrip("/") + ("" if not name else "/" + name)).replace("//", "/")

def resolve_full_path(cfg: dict, *, kind: str, kid: int, name: str | None = None, parentfolderid: int | None = None, existing_path: str | None = None) -> str | None:
    """
    Unified helper: always return absolute path for file/folder IDs.
    """
    if existing_path:
        return _norm_remote_path(existing_path)
    if kind == "file":
        return resolve_full_path_for_fileid(cfg, kid)
    # folder
    p = resolve_full_path_for_folderid(cfg, kid)
    if p: return p
    # very last resort: parent path + name
    if parentfolderid and name:
        base = resolve_full_path_for_folderid(cfg, int(parentfolderid)) or "/"
        return (base.rstrip("/") + "/" + name).replace("//", "/")
    return None

def row_for_folderid(cfg: Dict[str, Any], folderid: int) -> Dict[str, Any]:
    md   = get_folder_meta(cfg, folderid=folderid, showpath=True)
    path = resolve_full_path_for_folderid(cfg, folderid)
    r    = row_from_meta(md, path_hint=path)
    r["type"]   = "FOLDER"
    r["id"]     = int(md.get("folderid") or folderid)
    r["parent"] = md.get("parentfolderid")
    return r

def row_for_fileid(cfg: Dict[str, Any], fileid: int, with_checksum: bool = False) -> Dict[str, Any]:
    md   = stat_file(cfg, fileid=fileid, with_checksum=with_checksum, enrich_path=False) or {}
    path = resolve_full_path_for_fileid(cfg, fileid)
    r    = row_from_meta(md, path_hint=path)
    r["type"]   = "FILE"
    r["id"]     = int(md.get("fileid") or md.get("id") or fileid)
    r["parent"] = int(md.get("parentfolderid") or 0)
    return r




# --- Minimaler Decoder: nur Top-Hash lesen und einfache Felder greifen ---
# Für unsere Zwecke reicht es, den ersten Hash so weit zu traversieren, bis wir
# "result", "metadata" und ggf. "data" (Typ 20) gefunden haben. Wir implementieren
# daher einen sehr kleinen Reader, der Strings/Numbers/Bools/Hash/Array/Data versteht.

# Typenbereiche:
T_STRING_NEW_MIN, T_STRING_NEW_MAX = 100,149
T_STRING_REUSE_MIN, T_STRING_REUSE_MAX = 150,199
T_NUMBER_MIN, T_NUMBER_MAX = 200,219
T_BOOL_FALSE, T_BOOL_TRUE = 18, 19
T_ARRAY, T_HASH, T_DATA, T_END = 17, 16, 20, 255

class _BinReader:
    def __init__(self, data: bytes):
        self.b = data
        self.i = 0
        self._strings: list[str] = []

    def _u8(self) -> int:
        v = self.b[self.i]; self.i+=1; return v
    def _u16(self) -> int:
        v = struct.unpack_from("<H", self.b, self.i)[0]; self.i+=2; return v
    def _u32(self) -> int:
        v = struct.unpack_from("<I", self.b, self.i)[0]; self.i+=4; return v
    def _u64(self) -> int:
        v = struct.unpack_from("<Q", self.b, self.i)[0]; self.i+=8; return v
    def _read_string(self, t: int) -> str:
        # New short strings [100..149]: length = t-100
        if T_STRING_NEW_MIN <= t <= T_STRING_NEW_MAX:
            ln = t - T_STRING_NEW_MIN
            s = self.b[self.i:self.i+ln].decode("utf-8", "replace"); self.i+=ln
            self._strings.append(s); return s
        # New strings types 0..3 -> len in 1..4 bytes
        if 0 <= t <= 3:
            nbytes = t+1
            ln = int.from_bytes(self.b[self.i:self.i+nbytes], "little"); self.i+=nbytes
            s = self.b[self.i:self.i+ln].decode("utf-8", "replace"); self.i+=ln
            self._strings.append(s); return s
        # Reuse strings [150..199] -> small ids inline
        if T_STRING_REUSE_MIN <= t <= T_STRING_REUSE_MAX:
            idx = t - T_STRING_REUSE_MIN
            return self._strings[idx]
        # Reuse ids 4..7 -> id in 1..4 bytes
        if 4 <= t <= 7:
            nbytes = t-3
            idx = int.from_bytes(self.b[self.i:self.i+nbytes], "little"); self.i+=nbytes
            return self._strings[idx]
        raise ValueError(f"unexpected string type {t}")

    def _read_number(self, t: int) -> int:
        if T_NUMBER_MIN <= t <= T_NUMBER_MAX:
            return t - T_NUMBER_MIN  # small immediates 0..19
        nbytes = (t - 7)  # 8->1 byte ... 15->8 bytes
        if not (1 <= nbytes <= 8): raise ValueError(f"bad number type {t}")
        v = int.from_bytes(self.b[self.i:self.i+nbytes], "little"); self.i+=nbytes
        return v

    def _read_value(self) -> Any:
        t = self._u8()
        if t == T_HASH:
            d: Dict[str, Any] = {}
            while True:
                tt = self.b[self.i]
                if tt == T_END:
                    self.i+=1; break
                key = self._read_value()
                val = self._read_value()
                d[str(key)] = val
            return d
        if t == T_ARRAY:
            arr = []
            while True:
                if self.b[self.i] == T_END:
                    self.i+=1; break
                arr.append(self._read_value())
            return arr
        if t == T_BOOL_FALSE: return False
        if t == T_BOOL_TRUE:  return True
        if t == T_DATA:
            ln = self._u64()
            # Diese Daten kommen *nach* dem JSON-Baum in der TCP-Stream, d. h.
            # wir merken uns nur die Länge; der Aufrufer liest sie direkt vom Socket.
            return {"__type__":"data","len":ln}
        if t <= 7 or (T_STRING_NEW_MIN <= t <= T_STRING_NEW_MAX) or \
           (T_STRING_REUSE_MIN <= t <= T_STRING_REUSE_MAX):
            return self._read_string(t)
        if t >= 8 and t <= 15 or (T_NUMBER_MIN <= t <= T_NUMBER_MAX):
            return self._read_number(t)
        raise ValueError(f"unknown type {t}")

def _rpc(host: str, port: int, timeout: int, method: str,
         params: Dict[str,Any], data: bytes|None=None) -> Tuple[Dict[str,Any], Optional[bytes]]:
    """Sendet einen Binary-Request; gibt (top_hash, data_bytes) zurück."""
    data_len = len(data) if data else 0
    req = _build_request(method, params, data_len)
    tls = _connect(host, port, timeout)
    try:
        tls.sendall(req)
        if data_len:
            tls.sendall(data)
        resp_len = struct.unpack(LE_U32, _recv_exact(tls, 4))[0]
        payload  = _recv_exact(tls, resp_len)
        # evtl. Datenteil nachschieben?
        reader = _BinReader(payload)
        top = reader._read_value()
        extra = None
        if isinstance(top, dict):
            # Wenn "data" Feld vorkommt, separat lesen
            dv = top.get("data")
            if isinstance(dv, dict) and dv.get("__type__")=="data":
                extra = _recv_exact(tls, int(dv["len"]))
        return top, extra
    finally:
        try: tls.close()
        except: pass

def _expect_ok(top: Dict[str,Any]) -> None:
    if not isinstance(top, dict): raise RuntimeError("unexpected response")
    res = top.get("result")
    if res not in (0, "0", 0.0, None):
        err = top.get("error")
        raise RuntimeError(f"API error {res}: {err}")

def stat_folder(cfg: Dict[str, Any], *, path: Optional[str]=None, folderid: Optional[int]=None) -> Dict[str, Any]:
    """
    Liefert Metadaten für einen Ordner (pfad- oder id-basiert). Wirft 2055-Fehler, wenn nicht vorhanden.
    """
    if not path and folderid is None:
        raise ValueError("stat_folder: path oder folderid erforderlich")
    params: Dict[str, Any] = {"access_token": cfg["token"], "device": cfg["device"]}
    if path:
        params["path"] = path
    else:
        params["folderid"] = int(folderid)
    host, port, timeout = cfg["host"], int(cfg["port"]), int(cfg["timeout"])
    top, _ = _rpc(host, port, timeout, "stat", params=params)
    _expect_ok(top)
    md = top.get("metadata") or {}
    if not md.get("isfolder", False):
        raise RuntimeError(f"API error: not a folder: {md!r}")

    # Pfad robust befüllen:
    if path:
        # wenn der Aufrufer schon einen Pfad gab, setzen wir ihn durch
        md.setdefault("path", _norm_remote_path(path))
    else:
        # folderid -> vollständigen Pfad auflösen
        try:
            md.setdefault("path", resolve_full_path_for_folderid(cfg, int(folderid)))  # z.B. "/Backup/foo"
        except Exception:
            pass

    return md

def getapiserver(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ruft 'getapiserver' (binary) auf und liefert das Top-Objekt zurück.
    Auth ist nicht erforderlich.
    """
    host = cfg["host"]; port = int(cfg["port"]); timeout = int(cfg["timeout"])
    top, _ = _rpc(host, port, timeout, "getapiserver", params={})
    _expect_ok(top)
    return top

def choose_nearest_bin_host(cfg: Dict[str, Any],
                            *,
                            attempts_per_host: int = 2,
                            connect_timeout_s: float = 3.0,
                            cache_ttl_s: int = 3600) -> str:
    """
    Wählt den effektiv nächsten Binär-API-Host anhand von TLS-Handshake-Zeitmessung.
    Kandidaten kommen aus getapiserver()['binapi']; Fallback ist cfg['host'].
    Ergebnis wird ~1h gecached.
    """
    import json, time, os, socket, ssl

    base_host = cfg["host"]; port = int(cfg["port"])

    # Cache lesen
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "pcloud")
    cache_file = os.path.join(cache_dir, "nearest_bin.json")
    try:
        if os.path.isfile(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                c = json.load(f)
            if (time.time() - float(c.get("ts", 0))) <= cache_ttl_s and c.get("host"):
                return c["host"]
    except Exception:
        pass

    # Kandidaten holen
    candidates: list[str] = []
    try:
        ap = getapiserver(cfg)
        binapi = ap.get("binapi") or []
        if isinstance(binapi, list):
            candidates.extend([h for h in binapi if isinstance(h, str)])
    except Exception:
        pass
    if base_host not in candidates:
        candidates.append(base_host)

    # Messen (connect + TLS handshake)
    def measure(host: str) -> float | None:
        best = None
        for _ in range(max(1, attempts_per_host)):
            t0 = time.perf_counter()
            try:
                raw = socket.create_connection((host, port), timeout=connect_timeout_s)
                ctx = ssl.create_default_context()
                tls = ctx.wrap_socket(raw, server_hostname=host)
                tls.close()
                dt = time.perf_counter() - t0
                best = dt if (best is None or dt < best) else best
            except Exception:
                return None
        return best

    scores = []
    for h in candidates:
        dt = measure(h)
        if dt is not None:
            scores.append((dt, h))
    if not scores:
        return base_host

    scores.sort()
    chosen = scores[0][1]

    # Cache schreiben (best effort)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"host": chosen, "ts": time.time(), "candidates": candidates}, f)
    except Exception:
        pass

    return chosen

def _norm_remote_path(p: str) -> str:
    if not p: return "/"
    p = p.strip()
    if not p.startswith("/"): p = "/" + p
    while "//" in p: p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"): p = p[:-1]
    return p or "/"

# --- Öffentliche Helfer ---

def effective_config(env_file: Optional[str]=None,
                     overrides: Optional[Dict[str,Any]]=None,
                     profile: Optional[str]=None,
                     env_dir: Optional[str]=None) -> Dict[str,Any]:
    """
    Baut die effektive Konfiguration:
      Prio: CLI overrides > ENV > PROFILE .env > DEFAULT .env > Defaults
    profile kann auch über ENV PCLOUD_PROFILE kommen.
    """
    profile = profile or os.environ.get("PCLOUD_PROFILE")

    # Default-.env + Basisordner ermitteln
    default_env_path, profile_base_dir = _candidate_env_paths(env_file, env_dir)
    default_env = load_env_file(default_env_path)

    # Profil-.env (optional)
    prof_path = _find_profile_env(profile, profile_base_dir)
    prof_env = load_env_file(prof_path)

    # Merge: Default .env -> Profil .env -> ENV -> Overrides
    # Defaults
    cfg = {
        "host": "eapi.pcloud.com",
        "port": 8399,
        "timeout": 30,
        "token": "",
        "device": "entropywatcher/raspi",
    }

    # Standard .env
    if default_env:
        cfg.update({
            "host": default_env.get("PCLOUD_HOST", cfg["host"]),
            "port": int(default_env.get("PCLOUD_PORT", cfg["port"])),
            "timeout": int(default_env.get("PCLOUD_TIMEOUT", cfg["timeout"])),
            "token": default_env.get("PCLOUD_TOKEN", cfg["token"]),
            "device": default_env.get("PCLOUD_DEVICE", cfg["device"]),
        })

    # Profil .env
    if prof_env:
        cfg.update({
            "host": prof_env.get("PCLOUD_HOST", cfg["host"]),
            "port": int(prof_env.get("PCLOUD_PORT", cfg["port"])),
            "timeout": int(prof_env.get("PCLOUD_TIMEOUT", cfg["timeout"])),
            "token": prof_env.get("PCLOUD_TOKEN", cfg["token"]),
            "device": prof_env.get("PCLOUD_DEVICE", cfg["device"]),
        })

    # ENV
    cfg.update({
        "host": os.environ.get("PCLOUD_HOST", cfg["host"]),
        "port": int(os.environ.get("PCLOUD_PORT", cfg["port"])),
        "timeout": int(os.environ.get("PCLOUD_TIMEOUT", cfg["timeout"])),
        "token": os.environ.get("PCLOUD_TOKEN", cfg["token"]),
        "device": os.environ.get("PCLOUD_DEVICE", cfg["device"]),
    })

    # CLI Overrides
    if overrides:
        for k, v in overrides.items():
            if v is None: continue
            if k in ("port", "timeout"): v = int(v)
            cfg[k] = v

    if not cfg["token"]:
        where = prof_path or default_env_path or "ENV/CLI"
        raise RuntimeError(f"Kein PCLOUD_TOKEN gefunden (Quelle: {where}).")
    return cfg

def listfolder(cfg: Dict[str,Any], *, path: Optional[str]=None,
               folderid: Optional[int]=None, recursive: bool=False,
               nofiles: bool=False, showpath: bool=False) -> Dict[str,Any]:
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if path is not None:
        params["path"] = _norm_remote_path(path)
    elif folderid is not None:
        params["folderid"] = int(folderid)
    else:
        params["folderid"] = 0
    if recursive: params["recursive"]=1
    if nofiles: params["nofiles"]=1
    if showpath: params["showpath"]=1
    top,_ = _rpc(cfg["host"], cfg["port"], cfg["timeout"], "listfolder", params)
    _expect_ok(top)
    return top

def createfolder(cfg: Dict[str,Any], path: str) -> Dict[str,Any]:
    params = {"access_token": cfg["token"], "device": cfg["device"], "path": _norm_remote_path(path)}
    top,_ = _rpc(cfg["host"], cfg["port"], cfg["timeout"], "createfolder", params)
    _expect_ok(top)
    return top

def ensure_path(cfg: Dict[str,Any], path: str) -> int:
    """Legt einen Pfad rekursiv an; gibt folderid zurück (idempotent)."""
    path = _norm_remote_path(path)
    if path == "/":
        top = listfolder(cfg, path="/", showpath=True)
        return int(top.get("metadata",{}).get("folderid",0))
    parts = [p for p in path.split("/") if p]
    cur = "/"; fid = 0
    for i in range(len(parts)):
        cur = "/" + "/".join(parts[:i+1])
        try:
            top = listfolder(cfg, path=cur, showpath=True)
        except Exception:
            top = createfolder(cfg, cur)
        md = top.get("metadata",{})
        fid = int(md.get("folderid") or md.get("id") or 0)
    return fid

def stat_file(cfg: Dict[str,Any], *, path: Optional[str]=None,
              fileid: Optional[int]=None, with_checksum: bool=False,
              enrich_path: bool=True) -> Dict[str,Any]:
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if path is not None:
        params["path"] = _norm_remote_path(path)
    elif fileid is not None:
        params["fileid"] = int(fileid)
    else:
        raise ValueError("stat_file: path oder fileid angeben.")

    # stat
    top, _ = _rpc(cfg["host"], int(cfg["port"]), int(cfg["timeout"]), "stat", params)
    _expect_ok(top)
    meta = top.get("metadata") or top.get("file") or {}

    # Checksummen (optional)
    if with_checksum:
        try:
            ctop, _ = _rpc(cfg["host"], int(cfg["port"]), int(cfg["timeout"]), "checksumfile", params)
            _expect_ok(ctop)
            if isinstance(ctop, dict):
                if "sha1"   in ctop: meta["sha1"]   = ctop["sha1"]
                if "sha256" in ctop: meta["sha256"] = ctop["sha256"]
                if "md5"    in ctop: meta["md5"]    = ctop["md5"]
        except Exception:
            pass

    # Pfad anreichern, OHNE erneut stat() aufzurufen (kein Loop!)
    try:
        if enrich_path and not meta.get("path"):
            if "path" in params:
                # Aufrufer hat einen Pfad angegeben → übernehmen
                meta["path"] = params["path"]
            elif "fileid" in params:
                # fileid-Fall: aus parentfolderid + name zusammensetzen,
                # wobei resolve_full_path_for_folderid KEIN stat() für die Datei braucht
                name = (meta.get("name") or "").strip("/")
                pfid = int(meta.get("parentfolderid") or 0)
                parent_path = resolve_full_path_for_folderid(cfg, pfid)
                meta["path"] = (parent_path.rstrip("/") + ("" if not name else "/" + name)).replace("//", "/")
    except Exception:
        pass

    return meta or {}


def find_child_fileid(cfg: Dict[str,Any], folderid: int, name: str) -> Optional[int]:
    """Sucht in einem Ordner (nicht rekursiv) nach einer Datei mit exakt diesem Namen."""
    top = listfolder(cfg, folderid=folderid, recursive=False, nofiles=False, showpath=False)
    files = top.get("metadata",{}).get("contents") or []
    for it in files:
        if it.get("isfolder"): continue
        if it.get("name") == name:
            return int(it.get("fileid") or it.get("id"))
    return None

def upload_chunked(cfg: Dict[str,Any], local_path: str, dest_folderid: int,
                   filename: Optional[str]=None, chunk_size: int=8*1024*1024,
                   progress: Optional[Callable[[int,int],None]]=None) -> Dict[str,Any]:
    """
    Chunked Upload via Binary-Protokoll:
      - Methode: "uploadfile" mit filename-Param + Datenblock
      - Eine Datei pro Request
    Rückgabe: Top-Hash (mit metadata)
    """
    fsize = os.path.getsize(local_path)
    if filename is None:
        filename = os.path.basename(local_path)

    sent = 0
    # Wir senden in EINEM Request (Daten-Length gesetzt); pCloud nimmt große Payloads entgegen.
    # Für extrem große Dateien könnte man vorab upload_start/upload_write/upload_finish (JSON) verwenden.
    with open(local_path, "rb") as f:
        # Wir buffering die Daten in RAM vermeiden -> wir streamen in Stückchen:
        # Dazu senden wir zuerst *nur* den Header? Geht mit Sockets nicht trivial,
        # daher packen wir die Datei in Memory? Bei 10GiB nicht sinnvoll.
        # => Lösung: Datei in RAM nicht möglich, wir senden mit einem kleinen Wrapper:
        # Wir bauen Request ohne data_len>0, stattdessen nutzt pCloud Binary "data flag" zwingend.
        # Workaround: Wir lesen file in Ganzen ist nicht tragbar.
        # Deshalb: Wir schicken *doch* alles über Socket nach Header peu à peu (geht, wir haben data_len).
        # Wir müssen aber die Länge vorher wissen -> fsize.
        params = {
            "access_token": cfg["token"],
            "device": cfg["device"],
            "folderid": int(dest_folderid),
            "filename": filename,
        }
        req = _build_request("uploadfile", params, fsize)
        tls = _connect(cfg["host"], cfg["port"], cfg["timeout"])
        try:
            tls.sendall(req)
            # streamen:
            while True:
                chunk = f.read(chunk_size)
                if not chunk: break
                tls.sendall(chunk)
                sent += len(chunk)
                if progress:
                    progress(sent, fsize)
            # Antwort lesen
            resp_len = struct.unpack(LE_U32, _recv_exact(tls, 4))[0]
            payload  = _recv_exact(tls, resp_len)
            reader = _BinReader(payload)
            top = reader._read_value()
            _expect_ok(top)
            return top
        finally:
            try: tls.close()
            except: pass

def sha1_file(path: str, bufsize: int = 1024 * 1024) -> str:
    """SHA-1 für lokale Datei berechnen (Streaming)."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def _norm_remote_path(p: str) -> str:
    """pCloud-Pfad robust normalisieren (führt führenden '/', entfernt doppelte // und trailing '/')."""
    if not p:
        return "/"
    s = p.strip()
    if not s.startswith("/"):
        s = "/" + s
    while "//" in s:
        s = s.replace("//", "/")
    if len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s or "/"

def checksumfile(cfg: Dict[str, Any], *, fileid: int | None = None, path: str | None = None) -> Dict[str, Any]:
    """
    Ruft 'checksumfile' auf und gibt (falls vorhanden) 'sha256' / 'sha1' zurück.
    Mindestens einer von (fileid, path) muss gesetzt sein.
    """
    if (fileid is None) and (not path):
        raise ValueError("checksumfile: fileid oder path angeben.")
    params = {"access_token": cfg["token"], "device": cfg["device"]}
    if fileid is not None:
        params["fileid"] = int(fileid)
    else:
        params["path"] = _norm_remote_path(path or "")
    # Request senden
    tls = _connect(cfg["host"], cfg["port"], cfg["timeout"])
    try:
        req = _build_request("checksumfile", params, 0)
        tls.sendall(req)
        resp_len = struct.unpack(LE_U32, _recv_exact(tls, 4))[0]
        payload = _recv_exact(tls, resp_len)
        reader = _BinReader(payload)
        top = reader._read_value()
        _expect_ok(top)
        return top
    finally:
        try:
            tls.close()
        except Exception:
            pass

def upload_streaming(cfg: Dict[str, Any],
                     local_path: str,
                     *,
                     dest_folderid: int | None = None,
                     dest_path: str | None = None,
                     filename: str | None = None,
                     rename_if_exists: bool = False,
                     progress_cb: Callable[[int, int], None] | None = None,
                     chunk_size: int = 4 * 1024 * 1024,
                     progresshash: str | None = None) -> Dict[str, Any]:
    """
    High-Level Upload (Binary, EIN File pro Request), wahlweise Ziel via folderid ODER Pfad.
    Nutzt einen einzigen Request mit vordefinierter Datenlänge (streamend).
    Gibt das Top-Objekt (inkl. ggf. 'metadata') zurück.
    """
    fsize = os.path.getsize(local_path)
    fname = filename or os.path.basename(local_path)

    params = {
        "access_token": cfg["token"],
        "device": cfg["device"],
        "filename": fname,
    }
    if dest_path:
        params["path"] = _norm_remote_path(dest_path)
    elif dest_folderid is not None:
        params["folderid"] = int(dest_folderid)
    else:
        # Default: Root
        params["folderid"] = 0

    if rename_if_exists:
        params["renameifexists"] = 1
    if progresshash:
        params["progresshash"] = progresshash

    tls = _connect(cfg["host"], cfg["port"], cfg["timeout"])
    sent = 0
    try:
        req = _build_request("uploadfile", params, fsize)
        tls.sendall(req)
        with open(local_path, "rb") as f:
            while True:
                buf = f.read(max(64 * 1024, int(chunk_size)))
                if not buf:
                    break
                tls.sendall(buf)
                sent += len(buf)
                if progress_cb:
                    try:
                        progress_cb(sent, fsize)
                    except Exception:
                        pass

        # Antwort lesen
        resp_len = struct.unpack(LE_U32, _recv_exact(tls, 4))[0]
        payload = _recv_exact(tls, resp_len)
        reader = _BinReader(payload)
        top = reader._read_value()
        _expect_ok(top)
        return top
    finally:
        try:
            tls.close()
        except Exception:
            pass

def verify_remote_vs_local(cfg: Dict[str, Any],
                           *,
                           fileid: int | None = None,
                           path: str | None = None,
                           local_path: str,
                           prefer_sha256: bool = True) -> tuple[bool, dict]:
    """
    Vergleicht lokale Checksumme (sha256/sha1) mit Server ('checksumfile').
    Rückgabe: (ok, server_reply_dict). 'ok' ist True, wenn (sha256 oder sha1) gleich.
    """
    local_sha256 = sha256_file(local_path)
    local_sha1 = sha1_file(local_path)

    cs = checksumfile(cfg, fileid=fileid, path=path)
    r256 = (cs.get("sha256") or "") or None
    r1 = (cs.get("sha1") or "") or None

    if prefer_sha256 and r256:
        return (r256.lower() == local_sha256.lower(), cs)
    if r256:  # ohne Präferenz
        return (r256.lower() == local_sha256.lower(), cs)
    if r1:
        return (r1.lower() == local_sha1.lower(), cs)
    # keine serverseitigen Hashes
    return (False, cs)

def sha256_file(local_path: str, bufsize: int=1024*1024) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(bufsize), b""):
            h.update(chunk)
    return h.hexdigest()

def unique_target_name(cfg: Dict[str, Any], *, folderid: int, filename: str, tag: Optional[str]=None) -> str:
    """
    Liefert einen eindeutigen Dateinamen im Ordner:
      base.ext -> base (1).ext, base (2).ext, ...
    Optionales 'tag' wird als 'base (n) [tag].ext' angefügt.
    """
    base, ext = os.path.splitext(filename)
    # Inhalte einmalig listen
    top = listfolder(cfg, folderid=folderid, recursive=False, nofiles=False, showpath=False)
    names = { (it.get("name") or "") for it in (top.get("metadata",{}).get("contents") or []) if not it.get("isfolder") }
    if filename not in names:
        return filename
    i = 1
    while True:
        if tag:
            cand = f"{base} ({i}) [{tag}]{ext}"
        else:
            cand = f"{base} ({i}){ext}"
        if cand not in names:
            return cand
        i += 1

# ---------- Target-Resolver & Parent-Verify (High-Level) ----------

def resolve_target_direct(
    cfg: Dict[str, Any],
    *,
    file_id: int | None = None,
    file_path: str | None = None,
    folder_id: int | None = None,
    folder_path: str | None = None,
) -> tuple[str, int, str | None, str | None, int | None]:
    """
    Liefert (kind, kid, name, path, parentfid), kind in {"file", "folder"}.
    - Wirft FileNotFoundError, falls Ziel nicht existiert / falscher Typ.
    - Pfade werden (wo möglich) vollständig aufgelöst (enrich_path / resolve_full_path_*).
    """
    if file_id is not None:
        md = stat_file(cfg, fileid=int(file_id), with_checksum=False, enrich_path=True)
        if not md or md.get("isfolder"):
            raise FileNotFoundError("Datei (fileid) nicht gefunden.")
        return ("file",
                int(md.get("fileid") or file_id),
                md.get("name"),
                md.get("path"),
                int(md.get("parentfolderid") or 0))

    if file_path is not None:
        rp = _norm_remote_path(file_path)
        md = stat_file(cfg, path=rp, with_checksum=False, enrich_path=True)
        if not md or md.get("isfolder"):
            raise FileNotFoundError("Datei (Pfad) nicht gefunden.")
        return ("file",
                int(md.get("fileid") or 0),
                md.get("name"),
                md.get("path") or rp,
                int(md.get("parentfolderid") or 0))

    if folder_id is not None:
        fmd = get_folder_meta(cfg, folderid=int(folder_id), showpath=False) or {}
        if not fmd or not fmd.get("isfolder"):
            raise FileNotFoundError("Ordner (folderid) nicht gefunden.")
        kid  = int(fmd.get("folderid") or folder_id)
        path = resolve_full_path_for_folderid(cfg, kid)
        return ("folder", kid, fmd.get("name"), path, int(fmd.get("parentfolderid") or 0))

    if folder_path is not None:
        rp  = _norm_remote_path(folder_path)
        fmd = get_folder_meta(cfg, path=rp, showpath=True) or {}
        if not fmd or not fmd.get("isfolder"):
            raise FileNotFoundError("Ordner (Pfad) nicht gefunden.")
        return ("folder",
                int(fmd.get("folderid") or 0),
                fmd.get("name"),
                fmd.get("path") or rp,
                int(fmd.get("parentfolderid") or 0))

    raise ValueError("resolve_target_direct: kein Parameter gesetzt.")


def verify_child_under_parent(
    cfg: Dict[str, Any],
    *,
    parent_folderid: int | None = None,
    parent_path: str | None = None,
    file_id: int | None = None,
    folder_id: int | None = None,
) -> None:
    """
    Prüft, ob die gegebene child-ID DIREKT unter dem Parent liegt.
    - parent: genau eines von (parent_folderid, parent_path)
    - child : genau eines von (file_id, folder_id)
    Wirft FileNotFoundError oder RuntimeError bei Nichtzugehörigkeit.
    """
    if (parent_folderid is None) == (parent_path is None):
        raise ValueError("genau eines von parent_folderid oder parent_path angeben")
    if (file_id is None) == (folder_id is None):
        raise ValueError("genau eines von file_id oder folder_id angeben")

    # Parent → folderid
    if parent_folderid is not None:
        pfid = int(parent_folderid)
    else:
        pmd = get_folder_meta(cfg, path=_norm_remote_path(parent_path), showpath=False) or {}
        pfid = int(pmd.get("folderid") or 0)
        if pfid == 0 and (pmd.get("name") != "/"):
            raise FileNotFoundError("Parent-Ordner nicht gefunden.")

    if file_id is not None:
        fmd = stat_file(cfg, fileid=int(file_id), with_checksum=False, enrich_path=False)
        if not fmd or fmd.get("isfolder"):
            raise FileNotFoundError("Datei (fileid) nicht gefunden.")
        if int(fmd.get("parentfolderid") or -1) != pfid:
            raise RuntimeError("fileid gehört NICHT zu diesem Parent.")
        return

    # folder_id
    md = get_folder_meta(cfg, folderid=int(folder_id), showpath=False) or {}
    if not md or not md.get("isfolder"):
        raise FileNotFoundError("Ordner (folderid) nicht gefunden.")
    if int(md.get("parentfolderid") or -1) != pfid:
        raise RuntimeError("folderid gehört NICHT zu diesem Parent.")

# ========= NEW: tree/rows helpers for debug-tool =========

def _join_remote(parent: str, name: str) -> str:
    parent = _norm_remote_path(parent or "/")
    name = (name or "").strip("/")
    if not name:
        return parent
    return parent + ("" if parent == "/" else "/") + name

def _walk_metadata(md: dict,
                   rows: list[dict],
                   *,
                   parent_path: str,
                   include_files: bool,
                   prefer_server_path: bool,
                   depth: int,
                   max_depth: int | None) -> None:
    isfolder = bool(md.get("isfolder"))
    name = md.get("name") or ("/" if isfolder else "")
    server_path = md.get("path") if prefer_server_path else None

    # eigenen Pfad bestimmen
    if depth == 1 and (server_path or parent_path):
        my_path = server_path or parent_path or "/"
    else:
        my_path = server_path or _join_remote(parent_path or "/", name)

    # row schreiben
    rows.append(row_from_meta(md, path_hint=my_path))

    # tiefer?
    if isfolder:
        if (max_depth is not None) and (depth >= max_depth):
            return
        for ch in (md.get("contents") or []):
            _walk_metadata(
                ch, rows,
                parent_path=my_path,
                include_files=include_files,
                prefer_server_path=prefer_server_path,
                depth=depth + 1,
                max_depth=max_depth
            )
    else:
        # Dateien werden nur gelistet, wenn include_files=True – hier schon gefiltert:
        pass

def list_rows(cfg: dict,
              *,
              path: str | None = None,
              folderid: int | None = None,
              recursive: bool = False,
              include_files: bool = False,
              max_depth: int | None = None,
              prefer_server_path: bool = False) -> list[dict]:
    """
    High-level Baumlauf -> Rows im 'row_from_meta' Format (type/name/id/parent/path/...).
    Entspricht der Kernlogik aus list_folder_ids* (vereinfacht).
    """
    if (path is None) and (folderid is None):
        folderid = 0
    params = {
        "recursive": bool(recursive),
        "nofiles": not include_files,
        "showpath": True,
    }
    if path is not None:
        top = listfolder(cfg, path=_norm_remote_path(path), **params)
        start_path = _norm_remote_path(path)
    else:
        top = listfolder(cfg, folderid=int(folderid), **params)
        # Wenn showpath vom Server nicht geliefert wird, fallbacken wir auf rekonstruierte Pfade:
        md0 = (top.get("metadata") or {})
        start_path = md0.get("path") or (resolve_full_path_for_folderid(cfg, int(md0.get("folderid") or folderid or 0)) if md0.get("isfolder") else "/")

    md = top.get("metadata") or {}
    rows: list[dict] = []
    # Wurzelknoten: falls nur Dateien gewünscht, trotzdem die Wurzel-Row ausgeben
    _walk_metadata(md, rows,
                   parent_path=start_path,
                   include_files=include_files,
                   prefer_server_path=prefer_server_path,
                   depth=1,
                   max_depth=max_depth)
    # Bei include_files=False waren Dateien im Wurzelknoten evtl. dabei; filtern:
    if not include_files:
        rows = [r for r in rows if r.get("type") == "FOLDER"]
    return rows

def relative_paths(rows: list[dict], base_path: str) -> list[dict]:
    """
    Macht die 'path' Felder relativ zu 'base_path'.
    """
    base = _norm_remote_path(base_path)
    base_clean = base.rstrip("/")
    out = []
    for r in rows:
        p = r.get("path") or ""
        if p == base:
            rp = "."
        elif p.startswith(base_clean + "/"):
            rp = p[len(base_clean) + 1:]
        else:
            rp = p
        nr = dict(r)
        nr["path"] = rp
        out.append(nr)
    return out
