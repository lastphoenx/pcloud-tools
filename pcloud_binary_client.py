#!/usr/bin/env python3
"""
Minimal, high-performance pCloud Binary Protocol client (Python).

✅ Eine einzelne TLS-Verbindung (Port 8399)
✅ Token aus .env (robust geladen) ODER via CLI-Flag --token
✅ Streaming-Upload (Chunked) ohne die Datei komplett in RAM zu laden
✅ Response-Parser (Hash/Array/String/Number/Bool/Data) als Gerüst

--------------------------------------------------------------------
.env (example)
--------------------------------------------------------------------
# Your OAuth2 bearer token (as used by rclone)
PCLOUD_TOKEN="<paste-your-access-token>"

# Optional overrides
PCLOUD_HOST="api.pcloud.com"   # or eu.api.pcloud.com
PCLOUD_PORT="8399"
PCLOUD_TIMEOUT_SECS="90"
PCLOUD_UPLOAD_METHOD="uploadfile"  # can be changed if needed
PCLOUD_DEFAULT_FOLDERID="0"        # 0 is the root folder in pCloud

--------------------------------------------------------------------
Usage
--------------------------------------------------------------------
$ python pcloud_binary_client.py test-auth
$ python pcloud_binary_client.py upload --file /path/to/archive.borg
$ python pcloud_binary_client.py upload --file /path/to/archive.borg --folderid 123456

Override via CLI (ohne .env):
$ python pcloud_binary_client.py --token 'XYZ' --host eu.api.pcloud.com test-auth
"""
from __future__ import annotations

import argparse
import os
import socket
import ssl
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Union

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None  # optional dependency
    find_dotenv = None  # type: ignore

# ----------------------------- Helpers & Types ----------------------------- #

LE_U16 = "<H"   # little-endian unsigned short (2 bytes)
LE_U32 = "<I"   # little-endian unsigned int   (4 bytes)
LE_U64 = "<Q"   # little-endian unsigned long long (8 bytes)

PARAM_TYPE_STRING = 0
PARAM_TYPE_NUMBER = 1
PARAM_TYPE_BOOL   = 2

# Value type tags (subset, per docs)
VT_HASH   = 16
VT_ARRAY  = 17
VT_FALSE  = 18
VT_TRUE   = 19
VT_DATA   = 20
VT_END    = 255

# String compact range (short inline 0..49 bytes)
STR_INLINE_MIN = 100
STR_INLINE_MAX = 149
STR_REUSE_MIN  = 150
STR_REUSE_MAX  = 199

# Numbers inline range 0..19 encoded in [200..219]
NUM_INLINE_MIN = 200
NUM_INLINE_MAX = 219

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB socket write chunks


class BinaryProtoError(RuntimeError):
    pass


@dataclass
class PCloudConfig:
    # Keine ENV-Reads bei Definition! (werden erst NACH dotenv gelesen)
    host: str = "api.pcloud.com"
    port: int = 8399
    timeout: int = 90
    token: str = ""
    upload_method: str = "uploadfile"
    default_folderid: int = 0


# ----------------------------- Connection Layer ---------------------------- #

class PCloudBinaryConnection:
    """Hält eine TLS-Verbindung und kapselt Send/Recv (mit Lock)."""

    def __init__(self, cfg: PCloudConfig):
        self.cfg = cfg
        self._tls: Optional[ssl.SSLSocket] = None
        self._lock = threading.RLock()

    def connect(self) -> None:
        if self._tls is not None:
            return
        raw = socket.create_connection((self.cfg.host, self.cfg.port), timeout=self.cfg.timeout)
        ctx = ssl.create_default_context()
        self._tls = ctx.wrap_socket(raw, server_hostname=self.cfg.host)
        self._tls.settimeout(self.cfg.timeout)

    def close(self) -> None:
        with self._lock:
            try:
                if self._tls:
                    self._tls.close()
            finally:
                self._tls = None

    def _sendall(self, data: bytes) -> None:
        assert self._tls is not None, "Not connected"
        view = memoryview(data)
        total = 0
        while total < len(data):
            n = self._tls.send(view[total:])
            if n == 0:
                raise ConnectionError("socket send returned 0 bytes")
            total += n

    def _recvall(self, n: int) -> bytes:
        assert self._tls is not None, "Not connected"
        buf = bytearray()
        while len(buf) < n:
            chunk = self._tls.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed during recv")
            buf.extend(chunk)
        return bytes(buf)

    # --- Binary protocol request/response --- #

    def call(
        self,
        method: str,
        params: Dict[str, Union[str, int, bool]],
        data_stream: Optional[BinaryIO] = None,
        data_len: int = 0,
        write_chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> bytes:
        """Send one binary-protocol request and return raw response bytes."""
        with self._lock:
            self.connect()
            assert self._tls is not None

            # Request (ohne Daten) bauen
            has_data = 1 if data_stream is not None else 0
            method_bytes = method.encode("utf-8")
            if not (0 < len(method_bytes) < 128):
                raise ValueError("method name must be 1..127 bytes")

            # Parameter serialisieren
            param_parts: list[bytes] = []
            for name, value in params.items():
                name_b = name.encode("utf-8")
                if len(name_b) > 63:
                    raise ValueError("parameter name too long (max 63 bytes)")

                if isinstance(value, bool):
                    header = bytes([(PARAM_TYPE_BOOL << 6) | len(name_b)]) + name_b
                    enc = bytes([1 if value else 0])
                elif isinstance(value, int):
                    header = bytes([(PARAM_TYPE_NUMBER << 6) | len(name_b)]) + name_b
                    enc = struct.pack(LE_U64, value)
                else:
                    header = bytes([(PARAM_TYPE_STRING << 6) | len(name_b)]) + name_b
                    vb = str(value).encode("utf-8")
                    enc = struct.pack(LE_U32, len(vb)) + vb
                param_parts.append(header + enc)

            params_blob = b"".join(param_parts)

            # Header
            request_no_len = []
            request_no_len.append(bytes([(len(method_bytes) & 0x7F) | (0x80 if has_data else 0x00)]))
            if has_data:
                if data_len <= 0:
                    raise ValueError("data_len must be > 0 when sending data")
                request_no_len.append(struct.pack(LE_U64, data_len))
            request_no_len.append(method_bytes)
            request_no_len.append(bytes([len(params)]))
            request_no_len.append(params_blob)
            request_body = b"".join(request_no_len)
            if len(request_body) >= 65536:
                raise ValueError("request (without data) exceeds 64 KiB")

            request = struct.pack(LE_U16, len(request_body)) + request_body

            # Senden
            self._sendall(request)

            # Datenstream nachschieben
            if has_data and data_stream is not None:
                remaining = data_len
                while remaining > 0:
                    chunk = data_stream.read(min(write_chunk_size, remaining))
                    if not chunk:
                        raise IOError("unexpected EOF in data_stream before reaching data_len")
                    self._sendall(chunk)
                    remaining -= len(chunk)

            # Antwort (4-Byte Länge + Payload)
            resp_len_bytes = self._recvall(4)
            (resp_len,) = struct.unpack(LE_U32, resp_len_bytes)
            payload = self._recvall(resp_len)
            return payload


# ----------------------------- Response Parser ----------------------------- #

class ResponseParser:
    """Parser für pCloud Binary Responses (nützliches Subset)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self._strings: list[str] = []  # for reuse ids

    def _need(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise BinaryProtoError("response truncated")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def _read_u8(self) -> int:  return self._need(1)[0]
    def _read_u16(self) -> int: return struct.unpack(LE_U16, self._need(2))[0]
    def _read_u32(self) -> int: return struct.unpack(LE_U32, self._need(4))[0]
    def _read_u64(self) -> int: return struct.unpack(LE_U64, self._need(8))[0]

    def _read_string_value(self, tag: int) -> str:
        if STR_INLINE_MIN <= tag <= STR_INLINE_MAX:
            ln = tag - STR_INLINE_MIN
            s = self._need(ln).decode("utf-8", errors="replace")
            self._strings.append(s);  return s
        if STR_REUSE_MIN <= tag <= STR_REUSE_MAX:
            idx = tag - STR_REUSE_MIN
            try: return self._strings[idx]
            except IndexError as e: raise BinaryProtoError(f"bad reused string id {idx}") from e
        if tag in (0, 1, 2, 3):
            lensz = tag + 1
            if lensz == 1: ln = self._read_u8()
            elif lensz == 2: ln = self._read_u16()
            elif lensz == 3:
                b = self._need(3); ln = b[0] | (b[1] << 8) | (b[2] << 16)
            else:
                ln = self._read_u32()
            s = self._need(ln).decode("utf-8", errors="replace")
            self._strings.append(s);  return s
        raise BinaryProtoError(f"unknown string tag {tag}")

    def _read_number_value(self, tag: int) -> int:
        if NUM_INLINE_MIN <= tag <= NUM_INLINE_MAX:
            return tag - NUM_INLINE_MIN
        size_tag_map = {8:1, 9:2, 10:3, 11:4, 12:5, 13:6, 14:7, 15:8}
        if tag not in size_tag_map:
            raise BinaryProtoError(f"unknown number tag {tag}")
        sz = size_tag_map[tag]
        b = self._need(sz)
        val = 0
        for i, by in enumerate(b):
            val |= (by << (8 * i))
        return val

    def _read_value(self):
        tag = self._read_u8()
        if tag in (VT_FALSE, VT_TRUE): return (tag == VT_TRUE)
        if tag == VT_HASH:  return self._read_hash()
        if tag == VT_ARRAY: return self._read_array()
        if tag == VT_DATA:
            length = self._read_u64()
            return {"__type": "data", "length": length}
        if tag in (*range(0, 4), *range(STR_INLINE_MIN, STR_INLINE_MAX + 1), *range(STR_REUSE_MIN, STR_REUSE_MAX + 1)):
            return self._read_string_value(tag)
        if tag in (*range(8, 16), *range(NUM_INLINE_MIN, NUM_INLINE_MAX + 1)):
            return self._read_number_value(tag)
        raise BinaryProtoError(f"unknown value tag {tag}")

    def _read_array(self):
        arr = []
        while True:
            if self.pos < len(self.data) and self.data[self.pos] == VT_END:
                self.pos += 1;  break
            arr.append(self._read_value())
        return arr

    def _read_hash(self):
        obj: Dict[str, object] = {}
        while True:
            if self.pos < len(self.data) and self.data[self.pos] == VT_END:
                self.pos += 1;  break
            key = self._read_value()
            if not isinstance(key, str):
                raise BinaryProtoError("hash key is not a string")
            val = self._read_value()
            obj[key] = val
        return obj

    def parse(self) -> Dict[str, object]:
        root = self._read_value()
        if not isinstance(root, dict):
            raise BinaryProtoError("top-level is not a hash")
        return root


# ----------------------------- High-level Client --------------------------- #

class PCloudClient:
    def __init__(self, cfg: Optional[PCloudConfig] = None):
        # .env robust laden: 1) neben diesem Skript, 2) entlang des CWD-Baums
        if load_dotenv is not None:
            try:
                script_env = Path(__file__).with_name('.env')
                if script_env.exists():
                    load_dotenv(dotenv_path=script_env, override=False)
                if find_dotenv is not None:
                    found = find_dotenv(usecwd=True)
                    if found:
                        load_dotenv(found, override=True)
            except Exception:
                pass

        # Config NACH dotenv aus ENV bauen, außer cfg ist übergeben
        if cfg is None:
            cfg = PCloudConfig(
                host=os.getenv("PCLOUD_HOST", "api.pcloud.com"),
                port=int(os.getenv("PCLOUD_PORT", "8399")),
                timeout=int(os.getenv("PCLOUD_TIMEOUT_SECS", "90")),
                token=os.getenv("PCLOUD_TOKEN", ""),
                upload_method=os.getenv("PCLOUD_UPLOAD_METHOD", "uploadfile"),
                default_folderid=int(os.getenv("PCLOUD_DEFAULT_FOLDERID", "0")),
            )
        self.cfg = cfg
        if not self.cfg.token:
            raise RuntimeError("PCLOUD_TOKEN is required (set it in .env or environment)")
        self.conn = PCloudBinaryConnection(self.cfg)

    # NOTE: pCloud nutzt teils 'auth', teils 'access_token'.
    def _auth_params(self) -> Dict[str, Union[str, int, bool]]:
        return {"auth": self.cfg.token, "access_token": self.cfg.token}

    def call_raw(self, method: str, params: Dict[str, Union[str, int, bool]]) -> Dict[str, object]:
        payload = self.conn.call(method, params)
        return ResponseParser(payload).parse()

    def test_auth(self) -> Dict[str, object]:
        return self.call_raw("userinfo", {**self._auth_params()})

    def upload_file(
        self,
        file_path: Union[str, Path],
        folderid: Optional[int] = None,
        filename: Optional[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        overwrite: bool = True,
    ) -> Dict[str, object]:
        """Upload einer Datei via Binary Protocol (gestreamt)."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        fname = filename or path.name
        folderid = int(folderid if folderid is not None else self.cfg.default_folderid)
        file_size = path.stat().st_size

        params: Dict[str, Union[str, int, bool]] = {
            **self._auth_params(),
            "filename": fname,
            "folderid": folderid,
            "mtime": int(path.stat().st_mtime),
            "overwrite": bool(overwrite),
        }

        method = self.cfg.upload_method  # default 'uploadfile' (konfigurierbar)

        with path.open("rb") as f:
            payload = self.conn.call(
                method=method,
                params=params,
                data_stream=f,
                data_len=file_size,
                write_chunk_size=chunk_size,
            )
        return ResponseParser(payload).parse()


# ----------------------------- CLI Entrypoint ------------------------------ #

def _cli() -> int:
    ap = argparse.ArgumentParser(description="pCloud Binary Protocol client (uploads)")
    ap.add_argument("--token", help="Access token (overrides .env)")
    ap.add_argument("--host", help="API host override (e.g., eu.api.pcloud.com)")
    ap.add_argument("--port", type=int, help="API port override (default 8399)")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test-auth", help="Verify token and print user info")

    sp_up = sub.add_parser("upload", help="Upload a file via binary protocol")
    sp_up.add_argument("--file", required=True, help="Path to file to upload")
    sp_up.add_argument("--folderid", type=int, default=None, help="Target folder id (default from .env)")
    sp_up.add_argument("--name", default=None, help="Remote filename (defaults to basename)")
    sp_up.add_argument("--chunk", type=int, default=DEFAULT_CHUNK_SIZE, help="Socket write chunk size (bytes)")
    sp_up.add_argument("--no-overwrite", action="store_true", help="Do not overwrite existing file")

    args = ap.parse_args()

    # CLI-Overrides in ENV setzen, damit der Client sie aufnimmt
    if args.token:
        os.environ["PCLOUD_TOKEN"] = args.token
    if args.host:
        os.environ["PCLOUD_HOST"] = args.host
    if args.port:
        os.environ["PCLOUD_PORT"] = str(args.port)

    client = PCloudClient()

    if args.cmd == "test-auth":
        info = client.test_auth()
        print(info)
        return 0

    if args.cmd == "upload":
        res = client.upload_file(
            file_path=args.file,
            folderid=args.folderid,
            filename=args.name,
            chunk_size=args.chunk,
            overwrite=not args.no_overwrite,
        )
        print(res)
        return 0

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(_cli())
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        raise SystemExit(130)
