#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upload_file_bin.py – pCloud Binary Protocol: Datei(en) hochladen (streamend in Chunks, ohne RAM-Voll-Ladevorgang)

Neu:
- Fortschrittsanzeige mit Prozent, Durchsatz und ETA (auf STDERR), steuerbar per --progress/--no-progress und --progress-interval.

Funktionen:
- Binär-Upload via Methode 'uploadfile' mit Dateidaten als Data-Payload (eine Datei pro Request).
- Streamender Versand in konfigurierbaren Chunks (RAM-schonend, stabil).
- Zielwahl per --dest-path ODER --dest-folderid (wenn beides fehlt -> Root /).
- Optional: Dateiname auf pCloud-Seite via --filename überschreiben.
- Optional: --rename-if-exists (pCloud benennt automatisch um, statt Fehler zu werfen).
- Optional: --progresshash (kompatibel zu serverseitigem 'uploadprogress' – nicht extra abgefragt).
- Optional: --verify (ruft 'checksumfile' nach dem Upload auf und vergleicht lokale SHA-256/SHA-1).
- Optional: --nearest (holt per 'getapiserver' den nächsten binären API-Server und verwendet ihn).
- .env-Unterstützung analog zu deinen anderen Skripten.

Beispiele:
  # 1) Einfacher Upload in Root, Dateiname bleibt gleich:
  ./upload_file_bin.py --src /tmp/image.jpg

  # 2) Upload in konkreten pCloud-Ordner per Pfad:
  ./upload_file_bin.py --src /tmp/config.xml --dest-path "/Backup/binary-upload-test"

  # 3) Upload in Ziel-Ordner per folderid, mit neuem Namen + Rename-Option:
  ./upload_file_bin.py --src ./foo.bin --dest-folderid 11432140592 --filename foo_2025.bin --rename-if-exists

  # 4) Upload mit 8 MiB-Chunks, Fortschritts-Hash und Verify:
  ./upload_file_bin.py --src ./bigfile.iso --dest-path "/Backup/Images" \
      --chunk-size 8388608 --progresshash "myupload-42" --verify

  # 5) Erst passenden binären Server ermitteln (--nearest):
  ./upload_file_bin.py --src /tmp/a.tar.gz --dest-path "/My Files" --nearest
"""

import argparse, os, sys, ssl, socket, struct, hashlib, time
from typing import Dict, Any, Union, Optional, List

# === Binärformat Konstanten ===
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2

# Antworttypen
TYPE_HASH   = 16
TYPE_ARRAY  = 17
TYPE_FALSE  = 18
TYPE_TRUE   = 19
TYPE_DATA   = 20
TYPE_END    = 255

# ===== .env laden (robust) =====
def load_env(env_path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.isfile(env_path):
        return data
    with open(env_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            # robuste Quote-Erkennung (vermeidet Kopierartefakte)
            if len(v) >= 2 and ((v[0] == v[-1]) and v[0] in ('"', "'")):
                v = v[1:-1]
            data[k] = v
    return data

# ===== Binary-Request bauen =====
def build_request(method: str, params: Dict[str, Union[str,int,bool]], *, data_len: int = 0) -> bytes:
    """
    Baut den Binär-Request (Header) – bei data_len>0 wird das Data-Flag gesetzt und die 8-Byte-Datenlänge angehängt.
    Danach folgen Methodename, Param-Anzahl und die Parameter. Die Datei senden wir getrennt (streamend).
    """
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
    body.append((len(mb) & 0x7F) | (0x80 if has_data else 0))  # bit7 = Data-Flag
    if has_data:
        body += struct.pack(LE_U64, data_len)  # 8-Byte Data-Länge (Little Endian)
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

# ===== Decoder für die Antwort =====
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
        raise ValueError("String-Typ unbekannt")
    def read_number(self, t:int)->int:
        if 200 <= t <= 219: return t-200
        if 8 <= t <= 15: size = t-7; return int.from_bytes(self.take(size),"little")
        raise ValueError("Number-Typ unbekannt")
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

# ===== RPC Call =====
def rpc(host: str, port: int, timeout: int, method: str, params: Dict[str, Union[str,int,bool]], *, data_reader=None, data_len: int = 0) -> Dict[str, Any]:
    """
    Führt einen Binary-RPC Call aus. Wenn data_reader gesetzt ist (callable, das Bytes liefert),
    wird zuerst der Request gesendet, danach der Datenstrom in Chunks; am Ende wird die Antwort gelesen.
    """
    # 1) Socket + TLS
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.settimeout(timeout)

    try:
        # 2) Request-Header schicken
        req = build_request(method, params, data_len=data_len if data_reader else 0)
        tls.sendall(req)

        # 3) Optional: Datei streamen
        if data_reader:
            for chunk in data_reader():
                tls.sendall(chunk)

        # 4) Antwort lesen
        resp_len = struct.unpack(LE_U32, recv_exact(tls, 4))[0]
        payload  = recv_exact(tls, resp_len)
    finally:
        try: tls.close()
        except: pass

    # 5) Dekodieren + Fehlercheck
    top = decode(payload)
    if top.get("result") != 0:
        raise RuntimeError(f"{method} fehlgeschlagen: {top}")
    return top

# ===== Hilfsfunktionen =====
def human_bytes(n: int) -> str:
    units = ["B","KiB","MiB","GiB","TiB"]
    x = float(n); i = 0
    while x >= 1024.0 and i < len(units)-1:
        x /= 1024.0; i += 1
    return f"{x:.2f} {units[i]}"

def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h{m:02d}m{s:02d}s"
    if m: return f"{m}m{s:02d}s"
    return f"{s}s"

def sha256_file(path: str, bufsize: int = 1024*1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b: break
            h.update(b)
    return h.hexdigest()

def sha1_file(path: str, bufsize: int = 1024*1024) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b: break
            h.update(b)
    return h.hexdigest()

def norm_root(path: str) -> str:
    """pCloud-Pfad normalisieren: führenden '/', doppelte // entfernen, trailing '/' entfernen (außer '/')."""
    if not path: return "/"
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    while '//' in path:
        path = path.replace('//','/')
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path or "/"

def pick_working_bin_host(base_host: str, port: int, timeout: int) -> str:
    try:
        top = rpc(base_host, port, timeout, "getapiserver", {})
        candidates = []
        # 1) Bevorzugt binapi-Liste
        binapi = top.get("binapi") or []
        if isinstance(binapi, list):
            candidates.extend(binapi)
        # 2) Fallback: api/eapi, falls vorhanden
        for k in ("api", "eapi"):
            v = top.get(k)
            if isinstance(v, str):
                candidates.append(v)
        # 3) Immer den ursprünglichen Host zuletzt anhängen
        if base_host not in candidates:
            candidates.append(base_host)

        # nacheinander testen
        for h in candidates:
            try:
                s = socket.create_connection((h, port), timeout=5)
                s.close()
                return h
            except Exception:
                continue
    except Exception:
        pass
    return base_host  # Fallback

# ===== Main =====
def main():
    ap = argparse.ArgumentParser(description="pCloud Binary Upload – streamender Datei-Upload mit Fortschritt und optionaler Verifikation (checksumfile).")
    ap.add_argument("--env-file", default="/opt/entropywatcher/pcloud/.env", help="Pfad zur .env (Default: %(default)s)")

    # Quelle/Ziel
    ap.add_argument("--src", required=True, help="Lokaler Dateipfad zur Quelle (eine Datei pro Aufruf).")
    dest = ap.add_mutually_exclusive_group()
    dest.add_argument("--dest-path", help="Zielordner als pCloud-Pfad (z. B. /Backup/pfsense/Archiv)")
    dest.add_argument("--dest-folderid", type=int, help="Zielordner als folderid")
    ap.add_argument("--filename", help="Dateiname im Ziel (Default: wie lokale Quelldatei)")

    # Upload-Optionen
    ap.add_argument("--rename-if-exists", action="store_true", help="Wenn Datei existiert: automatisch umbenennen (statt Fehler).")
    ap.add_argument("--chunk-size", type=int, default=4*1024*1024, help="Chunk-Größe in Bytes für den Socket-Transfer (Default: 4 MiB).")
    ap.add_argument("--progresshash", help="Optionaler Progress-Hash (kompatibel zu 'uploadprogress', gleicher Server).")

    # Fortschritt
    ap.add_argument("--progress", action="store_true", help="Fortschritt anzeigen (erzwingen, auch wenn kein TTY).")
    ap.add_argument("--no-progress", action="store_true", help="Fortschritt unterdrücken.")
    ap.add_argument("--progress-interval", type=float, default=0.5, help="Aktualisierungsintervall in Sekunden (Default: 0.5).")

    # Verifikation
    ap.add_argument("--verify", action="store_true", help="Nach Upload 'checksumfile' aufrufen und lokale SHA-256/SHA-1 vergleichen.")

    # Verbindung / Serverwahl
    ap.add_argument("--nearest", action="store_true", help="Per 'getapiserver' nächsten binären API-Server ermitteln und nutzen.")
    ap.add_argument("--host", help="API-Host (Default aus .env oder eapi.pcloud.com)")
    ap.add_argument("--port", type=int, help="Port (Default aus .env oder 8399)")
    ap.add_argument("--timeout", type=int, help="Timeout Sekunden (Default aus .env oder 60)")
    ap.add_argument("--device", help="device Kennung (Default aus .env oder 'entropywatcher/raspi')")

    args = ap.parse_args()

    # --- .env / Defaults laden ---
    env = load_env(args.env_file)
    token = env.get("PCLOUD_TOKEN") or os.environ.get("PCLOUD_TOKEN")
    if not token:
        print("Fehler: Kein Token gefunden. Bitte PCLOUD_TOKEN in .env setzen.", file=sys.stderr)
        sys.exit(2)

    host = args.host or env.get("PCLOUD_HOST", "eapi.pcloud.com")
    port = args.port or int(env.get("PCLOUD_PORT", "8399"))
    timeout = args.timeout or int(env.get("PCLOUD_TIMEOUT", "60"))
    device = args.device or env.get("PCLOUD_DEVICE", "entropywatcher/raspi")

    src_path = os.path.abspath(args.src)
    if not os.path.isfile(src_path):
        print(f"Fehler: Quelle existiert nicht oder ist keine Datei: {src_path}", file=sys.stderr)
        sys.exit(2)

    file_size = os.path.getsize(src_path)
    file_name = args.filename if args.filename else os.path.basename(src_path)

    # Zielpfad ggf. normalisieren
    dest_path = norm_root(args.dest_path) if args.dest_path else None
    dest_folderid = args.dest_folderid

    # --- Optional: nächstgelegenen binären Server holen ---
    if args.nearest:
        new_host = pick_working_bin_host(host, port, timeout)
        if new_host != host:
            print(f"[Info] Verwende erreichbaren Binärserver: {new_host}")
            host = new_host
        else:
            print(f"[Info] Bleibe bei Host: {host}")

    # --- Upload-Parameter für 'uploadfile' vorbereiten ---
    params = {
        "access_token": token,
        "device": device,
        "filename": file_name,
    }
    if dest_path:
        params["path"] = dest_path  # Zielordner per Pfad
    elif dest_folderid is not None:
        params["folderid"] = int(dest_folderid)  # Zielordner per ID
    if args.rename_if_exists:
        params["renameifexists"] = 1
    if args.progresshash:
        params["progresshash"] = args.progresshash

    # --- Fortschrittssteuerung vorbereiten ---
    # Automatik: zeige Fortschritt, wenn TTY vorhanden (stderr) und nicht explizit abgeschaltet.
    show_progress = (args.progress or (sys.stderr.isatty() and not args.no_progress)) and (file_size > 0)
    interval = max(0.1, float(args.progress_interval))
    last_update = 0.0
    start_ts = time.time()
    sent_bytes = 0

    def emit_progress(final: bool = False):
        nonlocal last_update
        now = time.time()
        if not final and (now - last_update) < interval:
            return
        last_update = now
        elapsed = max(1e-6, (now - start_ts))
        speed = sent_bytes / elapsed
        pct = (sent_bytes / file_size) * 100.0 if file_size > 0 else 0.0
        remain = max(0.0, file_size - sent_bytes)
        eta = remain / speed if speed > 0 else float("inf")
        # Fortschritt als einzeilige Statusmeldung auf STDERR
        bar_w = 28
        filled = int((pct/100.0)*bar_w)
        bar = "#" * filled + "-" * (bar_w - filled)
        msg = f"\r[{bar}] {pct:6.2f}%  {human_bytes(sent_bytes)}/{human_bytes(file_size)}  @ {human_bytes(int(speed))}/s  ETA {format_eta(eta)}"
        try:
            sys.stderr.write(msg)
            sys.stderr.flush()
        except Exception:
            pass
        if final:
            try:
                sys.stderr.write("\n")
                sys.stderr.flush()
            except Exception:
                pass

    chunk = max(64*1024, int(args.chunk_size))  # Untergrenze 64 KiB

    def data_reader():
        nonlocal sent_bytes
        with open(src_path, "rb") as f:
            while True:
                buf = f.read(chunk)
                if not buf: break
                sent_bytes += len(buf)
                if show_progress:
                    emit_progress(final=False)
                yield buf
        if show_progress:
            # Am Ende sicherstellen, dass 100% gezeigt werden.
            emit_progress(final=True)

    # --- Upload durchführen ---
    try:
        top = rpc(host, port, timeout, "uploadfile", params, data_reader=data_reader, data_len=file_size)
    except Exception as e:
        # Stelle sicher, dass bei einem Fehler die Zeile sauber endet
        if show_progress:
            sys.stderr.write("\n")
        print(f"Fehler beim Upload: {e}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - start_ts
    rate = (sent_bytes/elapsed) if elapsed > 0 else 0.0
    print(f"Upload OK: {file_name}  ->  {dest_path or f'folderid={dest_folderid}' or '/'}")
    print(f"Größe: {human_bytes(file_size)}  Dauer: {elapsed:.2f}s  Rate: {human_bytes(int(rate))}/s")

    # --- Response inspizieren: oft 'metadata' (Liste) zurück ---
    md = top.get("metadata")
    meta = None
    if isinstance(md, list) and md:
        meta = md[0]
    elif isinstance(md, dict):
        meta = md

    if meta:
        isfolder = meta.get("isfolder", False)
        up_id = meta.get("fileid" if not isfolder else "folderid")
        up_path = meta.get("path")
        up_size = meta.get("size")
        print(f"Ziel-Objekt: {'FILE' if not isfolder else 'FOLDER'} id={up_id} size={up_size} path={up_path}")

    # --- Optional: Verify (checksumfile) ---
    if args.verify:
        print("Prüfe Checksummen (lokal vs. Server)...")
        local_sha256 = sha256_file(src_path)
        local_sha1   = sha1_file(src_path)
        cs_params = {"access_token": token, "device": device}
        if meta and meta.get("fileid"):
            cs_params["fileid"] = meta["fileid"]
        else:
            if dest_path:
                cs_params["path"] = f"{dest_path}/{file_name}" if dest_path != "/" else f"/{file_name}"
            else:
                print("[Hinweis] Keine fileid/path aus Upload-Antwort – checksumfile ggf. separat per 'stat' + 'checksumfile' ausführen.")
        try:
            cs_top = rpc(host, port, timeout, "checksumfile", cs_params)
            r_sha256 = cs_top.get("sha256")
            r_sha1   = cs_top.get("sha1")
            ok256 = (r_sha256 is not None and r_sha256.lower() == local_sha256.lower())
            ok1   = (r_sha1   is not None and r_sha1.lower()   == local_sha1.lower())
            if r_sha256:
                print(f"SHA-256: {'OK' if ok256 else 'MISMATCH'}")
            elif r_sha1:
                print(f"SHA-1:   {'OK' if ok1   else 'MISMATCH'} (SHA-256 serverseitig nicht verfügbar)")
            else:
                print("Hinweis: Server lieferte keine SHA-256/SHA-1 (Region/Dateityp?).")
            if (r_sha256 and not ok256) or (not r_sha256 and r_sha1 and not ok1):
                sys.exit(3)
        except Exception as e:
            print(f"[Warnung] checksumfile fehlgeschlagen: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
