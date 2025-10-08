#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upload_file_bin.py – pCloud Binary Protocol: Datei-Upload

Funktion:
- Baut einen Binary-Request für die Methode 'uploadfile'.
- Sendet die Datei als "data" hinter dem Request-Header (ohne sie vollständig in den RAM zu laden).
- Liest die Binary-Antwort und druckt result/fileids/metadata kompakt aus.

Wichtige Doku-Hinweise:
- "Sending files": filename-Parameter setzen, Dateidaten als Data senden. (pCloud Binary Protocol)
- 'uploadfile': Ziel per folderid (oder path) und Pflicht-Param 'filename'. (pCloud Methods)

Doku:
- Binary Protocol / Sending files: https://docs.pcloud.com/protocols/binary_protocol/sending_files.html
- uploadfile: https://docs.pcloud.com/methods/file/uploadfile.html

Konfiguration:
- .env (Standard: /opt/entropywatcher/pcloud/.env), z.B.:
    PCLOUD_TOKEN="dein_token"
    PCLOUD_HOST="eapi.pcloud.com"
    PCLOUD_PORT="8399"
    PCLOUD_TIMEOUT="30"
    PCLOUD_DEVICE="entropywatcher/raspi"
    PCLOUD_DEFAULT_FOLDERID="0"

Beispiele:
  # Datei in Root-Folder (0) hochladen, Name = Dateiname, Fortschrittshash automatisch:
  ./upload_file_bin.py /path/zur/datei.bin

  # Zielordner setzen und bestehende Dateien nicht überschreiben (rename-if-exists):
  ./upload_file_bin.py /tmp/test.txt --folderid 123456 --renameifexists

  # Chunk-Größe beim Streamen ändern (nur Netzwerk-Chunking, API bleibt ein Request):
  ./upload_file_bin.py /big/file.img --chunk-size 8M

Alle Ausgaben/Kommentare auf Deutsch.
"""
import argparse, os, ssl, socket, struct, sys, uuid
from typing import Dict, Any, Tuple, Union

# --- Binärformat Konstanten (Little Endian) ---
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"

# Parametertypen: 0=string, 1=number(64-bit), 2=bool
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2

# Antworttypen laut Doku
TYPE_HASH   = 16
TYPE_ARRAY  = 17
TYPE_FALSE  = 18
TYPE_TRUE   = 19
TYPE_DATA   = 20
TYPE_END    = 255

# Strings (neu): 100..149 (0..49 Byte direkt); Längen-codes 0..3 => 1..4 Byte Längenfeld
# Strings (Reuse): 150..199 (Pointer auf frühere Strings)
# Numbers: 200..219 => kleine Zahlen (0..19) direkt; sonst 1..8 Byte Länge (Codes 8..15) – hier vereinfachen wir auf 1,2,4,8 Byte und 0..19 direkt.

# --------- Hilfsfunktionen ----------
def parse_size(s: str) -> int:
    """'64K', '8M', '1G' zu Bytes. Standard: Bytes."""
    m = s.strip().upper()
    if m.endswith("K"): return int(float(m[:-1]) * 1024)
    if m.endswith("M"): return int(float(m[:-1]) * 1024**2)
    if m.endswith("G"): return int(float(m[:-1]) * 1024**3)
    return int(m)

def load_env(env_path: str) -> Dict[str, str]:
    """Einfacher .env-Lader (kein externes Paket nötig). Unterstützt KEY=..., KEY="...". Keine Escapes."""
    data: Dict[str, str] = {}
    if not os.path.isfile(env_path):
        return data
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"): 
                continue
            if "=" not in line: 
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith(("'", '"')) and v.endswith(("'", '"")) and len(v) >= 2:
                v = v[1:-1]
            data[k] = v
    return data

def build_request(method: str, params: Dict[str, Union[str,int,bool]], *, data_len: int = 0) -> bytes:
    """
    Baut den Request-Frame (ohne die Datei selbst).
    - 2 Byte: Länge des Request-Blocks (ohne Daten)
    - 1 Byte: method_len (Bits 0..6) + Has-Data-Flag (Bit 7)
    - [8 Byte: Datenlänge, falls Flag gesetzt]
    - method bytes
    - 1 Byte: Anzahl Parameter
    - pro Parameter: (Typ<<6 | name_len) + name + (Wert: string/number/bool)
    """
    mb = method.encode("utf-8")
    has_data = 1 if data_len > 0 else 0

    parts: list[bytes] = []
    for name, value in params.items():
        nb = name.encode("utf-8")
        if isinstance(value, bool):
            parts.append(bytes([(PARAM_BOOL << 6) | len(nb)]) + nb + (b"\x01" if value else b"\x00"))
        elif isinstance(value, int):
            parts.append(bytes([(PARAM_NUMBER << 6) | len(nb)]) + nb + struct.pack(LE_U64, value))
        else:
            vb = str(value).encode("utf-8")
            parts.append(bytes([(PARAM_STRING << 6) | len(nb)]) + nb + struct.pack(LE_U32, len(vb)) + vb)

    header = bytearray()
    method_len_byte = (len(mb) & 0x7F) | (0x80 if has_data else 0x00)
    header.append(method_len_byte)
    if has_data:
        header += struct.pack(LE_U64, data_len)
    header += mb
    header.append(len(params))
    header += b"".join(parts)

    return struct.pack(LE_U16, len(header)) + bytes(header)

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Liest exakt n Bytes (oder wirft Exception bei Abbruch)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Verbindung vorzeitig geschlossen")
        buf += chunk
    return bytes(buf)

# ------ Decoder für Binary Response (ausreichend für die gängigen Felder) ------
class BinDecoder:
    def __init__(self, payload: bytes):
        self.b = payload
        self.i = 0
        self.string_table: list[str] = []  # Für String-Reuse

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.b):
            raise ValueError("Antwort zu kurz")
        out = self.b[self.i:self.i+n]
        self.i += n
        return out

    def read_u8(self) -> int:
        return self.take(1)[0]

    def read_u16(self) -> int:
        return struct.unpack(LE_U16, self.take(2))[0]

    def read_u32(self) -> int:
        return struct.unpack(LE_U32, self.take(4))[0]

    def read_u64(self) -> int:
        return struct.unpack(LE_U64, self.take(8))[0]

    def read_string(self, t: int) -> str:
        # Neue Strings: 100..149 (0..49 direkt) ODER 0..3 (1..4 Byte Länge folgen)
        if 100 <= t <= 149:
            ln = t - 100
            s = self.take(ln).decode("utf-8", "replace")
            self.string_table.append(s)
            return s
        if t in (0, 1, 2, 3):
            size_bytes = {0:1, 1:2, 2:3, 3:4}[t]
            ln = int.from_bytes(self.take(size_bytes), "little", signed=False)
            s = self.take(ln).decode("utf-8", "replace")
            self.string_table.append(s)
            return s
        # Reused strings: 150..199 direkt (id 0..49), bzw. 4..7 => id in 1..4 Byte
        if 150 <= t <= 199:
            sid = t - 150
            return self.string_table[sid]
        if t in (4, 5, 6, 7):
            size_bytes = {4:1, 5:2, 6:3, 7:4}[t]
            sid = int.from_bytes(self.take(size_bytes), "little", signed=False)
            return self.string_table[sid]
        raise ValueError(f"Unbekannter String-Typ: {t}")

    def read_number(self, t: int) -> int:
        # 200..219 => 0..19 direkt
        if 200 <= t <= 219:
            return t - 200
        # 8..15 => 1..8 Byte Zahl
        if 8 <= t <= 15:
            size = t - 7  # 8->1, 9->2, ..., 15->8
            return int.from_bytes(self.take(size), "little", signed=False)
        raise ValueError(f"Unbekannter Number-Typ: {t}")

    def read_value(self) -> Any:
        t = self.read_u8()
        if t == TYPE_HASH:
            obj = {}
            while True:
                nxt = self.b[self.i]
                if nxt == TYPE_END:
                    self.i += 1
                    break
                key = self.read_value()  # immer String
                val = self.read_value()
                obj[key] = val
            return obj
        if t == TYPE_ARRAY:
            arr = []
            while True:
                nxt = self.b[self.i]
                if nxt == TYPE_END:
                    self.i += 1
                    break
                arr.append(self.read_value())
            return arr
        if t == TYPE_FALSE:
            return False
        if t == TYPE_TRUE:
            return True
        if t == TYPE_DATA:
            # nach der Antwort folgt noch zusätzlicher Datenstream; hier ignorieren wir (für Uploads unkritisch)
            data_len = self.read_u64()
            return {"__data_len__": data_len}
        # sonst: String / Zahl je nach Typgruppen
        if t in list(range(100,150)) + [0,1,2,3,4,5,6,7] or 150 <= t <= 199:
            return self.read_string(t)
        if t in list(range(8,16)) + list(range(200,220)):
            return self.read_number(t)
        raise ValueError(f"Unbekannter Typcode: {t}")

def decode_response(payload: bytes) -> Dict[str, Any]:
    dec = BinDecoder(payload)
    top = dec.read_value()  # sollte Hash sein
    if not isinstance(top, dict):
        raise ValueError("Top-Level Antwort ist nicht Hash")
    return top

# --------- I/O Hauptlogik ----------
def main():
    ap = argparse.ArgumentParser(description="pCloud Binary Upload (gestreamt, ohne Token auf CLI).")
    ap.add_argument("file", help="Pfad zur Quelldatei")
    ap.add_argument("--env-file", default="/opt/entropywatcher/pcloud/.env", help="Pfad zur .env (Standard: %(default)s)")

    ap.add_argument("--host", help="API-Host (Default aus .env oder eapi.pcloud.com)")
    ap.add_argument("--port", type=int, help="Port (Default aus .env oder 8399/TLS Binary)")
    ap.add_argument("--timeout", type=int, help="Timeout Sekunden (Default aus .env oder 30)")
    ap.add_argument("--device", help="device Kennung fürs Binary-Protokoll (Default aus .env oder 'entropywatcher/raspi')")

    ap.add_argument("--folderid", type=int, help="Zielordner (folderid). Default aus .env oder 0 (Root).")
    ap.add_argument("--filename", help="Zieldateiname. Standard: Name der Quelldatei")
    ap.add_argument("--renameifexists", action="store_true", help="Falls gesetzt, bei Namenskonflikt umbenennen statt überschreiben.")
    ap.add_argument("--nopartial", action="store_true", help="Falls gesetzt, unvollständige Uploads nicht speichern.")
    ap.add_argument("--progresshash", help="Optionaler Fortschrittshash (z.B. zur Verwendung mit 'uploadprogress').")

    ap.add_argument("--chunk-size", default="4M", help="Chunkgröße fürs Senden über den Socket (nur Streaming, API bleibt single-request). Standard: 4M")

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

    folderid = args.folderid if args.folderid is not None else int(env.get("PCLOUD_DEFAULT_FOLDERID", "0"))
    src_path = args.file
    if not os.path.isfile(src_path):
        print(f"Fehler: Datei nicht gefunden: {src_path}", file=sys.stderr)
        sys.exit(2)
    filename = args.filename or os.path.basename(src_path)
    chunk_bytes = parse_size(args.chunk_size)

    # Optionaler progresshash – automatisch generieren, wenn Flag/Fortschritt gewünscht, aber nicht gesetzt
    progresshash = args.progresshash or str(uuid.uuid4())

    # Request-Parameter (global + method-spezifisch)
    params = {
        "access_token": token,
        "device": device,
        "folderid": folderid,
        "filename": filename,
    }
    if args.renameifexists:
        params["renameifexists"] = 1
    if args.nopartial:
        params["nopartial"] = 1
    if progresshash:
        params["progresshash"] = progresshash

    file_size = os.path.getsize(src_path)

    # Request-Frame (mit Data-Länge)
    req = build_request("uploadfile", params, data_len=file_size)

    # TLS-Verbindung aufbauen
    raw = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(raw, server_hostname=host)
    tls.settimeout(timeout)

    # Header senden
    tls.sendall(req)

    # Datei gestreamt senden (Chunking auf Socket-Ebene)
    sent = 0
    with open(src_path, "rb") as f:
        while True:
            buf = f.read(chunk_bytes)
            if not buf:
                break
            tls.sendall(buf)
            sent += len(buf)
            # Optional: Mini-Progress (lokal)
            print(f"\rGesendet: {sent}/{file_size} Bytes ({sent*100//max(1,file_size)}%)", end="", flush=True)
    print()

    # Antwort lesen: 4 Byte Länge + Payload
    resp_len = struct.unpack(LE_U32, recv_exact(tls, 4))[0]
    payload = recv_exact(tls, resp_len)
    tls.close()

    # Dekodieren
    try:
        top = decode_response(payload)
    except Exception as e:
        print("Dekodierfehler – Rohbytes (hex):")
        print(payload.hex())
        raise

    # Ergebnis anzeigen
    result = top.get("result")
    print("result:", result)
    if result == 0:
        fileids = top.get("fileids")
        metadata = top.get("metadata")
        print("Upload OK.")
        if fileids:
            print("fileids:", fileids)
        if metadata:
            # kompaktes Metadata-Summary
            for i, md in enumerate(metadata, 1):
                name = md.get("name")
                fid  = md.get("fileid")
                size = md.get("size")
                parent = md.get("parentfolderid")
                print(f"[{i}] {name}  fileid={fid}  size={size}  parentfolderid={parent}")
        print(f"progresshash: {progresshash}")
    else:
        print("Fehlgeschlagen.")
        # ggf. ganze Antwort ausgeben
        print(top)

if __name__ == "__main__":
    main()
