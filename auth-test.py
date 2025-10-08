#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pCloud EU auth check (Binary Protocol) – ohne CLI-Token

Token-Quellen (in dieser Reihenfolge):
  1) /opt/entropywatcher/pcloud/token.json   -> {"access_token":"..."}
  2) /opt/entropywatcher/.env                -> PCLOUD_TOKEN=...
  3) (kein dritter Fallback: sauberer Fehler)

Host: eapi.pcloud.com:8399 (Europa)
Methode: userinfo
Ausgabe: result (0 bei Erfolg), uid, email
"""

from __future__ import annotations
import os, ssl, socket, struct, json, sys
from typing import Dict, Union

HOST, PORT, TIMEOUT = "eapi.pcloud.com", 8399, 20

#TOKEN_FILE = "/opt/entropywatcher/pcloud/token.json"
ENV_FILE   = "/opt/entropywatcher/pcloud/.env"

LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2
VT_HASH, VT_ARRAY, VT_FALSE, VT_TRUE, VT_DATA, VT_END = 16, 17, 18, 19, 20, 255
STR_INLINE_MIN, STR_INLINE_MAX = 100, 149
STR_REUSE_MIN, STR_REUSE_MAX   = 150, 199
NUM_INLINE_MIN, NUM_INLINE_MAX = 200, 219

class ProtoErr(RuntimeError): pass

def load_token() -> str:
    # 1) JSON-Datei
    try:
        if os.path.isfile(TOKEN_FILE):
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in ("access_token", "token", "BearerToken", "bearer"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    except Exception:
        pass
    # 2) .env
    try:
        if os.path.isfile(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == "PCLOUD_TOKEN":
                        v = v.strip().strip("'").strip('"')
                        if v:
                            return v
    except Exception:
        pass
    raise RuntimeError(
        f"Kein Token gefunden. Lege {TOKEN_FILE} mit {{\"access_token\":\"…\"}} an "
        f"oder setze PCLOUD_TOKEN in {ENV_FILE}."
    )

def build_request(method: str, params: Dict[str, Union[str,int,bool]]) -> bytes:
    mb = method.encode("utf-8")
    if not (0 < len(mb) < 128):
        raise ValueError("method name 1..127 bytes")
    parts = []
    for name, value in params.items():
        nb = name.encode("utf-8")
        if len(nb) > 63:
            raise ValueError("param name too long")
        if isinstance(value, bool):
            header = bytes([(PARAM_BOOL<<6)|len(nb)]) + nb
            enc = b"\x01" if value else b"\x00"
        elif isinstance(value, int):
            header = bytes([(PARAM_NUMBER<<6)|len(nb)]) + nb
            enc = struct.pack(LE_U64, value)
        else:
            vb = str(value).encode("utf-8")
            header = bytes([(PARAM_STRING<<6)|len(nb)]) + nb
            enc = struct.pack(LE_U32, len(vb)) + vb
        parts.append(header + enc)
    body = bytes([len(mb) & 0x7F]) + mb + bytes([len(params)]) + b"".join(parts)  # kein Datenflag
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

def parse_response(payload: bytes) -> Dict[str, object]:
    p = 0
    strings: list[str] = []

    def need(n: int) -> bytes:
        nonlocal p
        if p + n > len(payload): raise ProtoErr("truncated")
        b = payload[p:p+n]; p += n; return b
    def ru8() -> int:
        # liest 1 Byte; p wird in need() angepasst
        return need(1)[0]
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
        if tag in (0,1,2,3):
            sz = tag + 1
            if sz == 1: ln = ru8()
            elif sz == 2: ln = ru16()
            elif sz == 3:
                b = need(3); ln = b[0] | (b[1]<<8) | (b[2]<<16)
            else: ln = ru32()
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
        if t in (*range(0,4), *range(STR_INLINE_MIN,STR_INLINE_MAX+1), *range(STR_REUSE_MIN,STR_REUSE_MAX+1)):
            return read_string_with_tag(t)
        if t in (*range(8,16), *range(NUM_INLINE_MIN,NUM_INLINE_MAX+1)):
            return read_number_with_tag(t)
        raise ProtoErr(f"unknown value tag {t}")

    def read_array():
        nonlocal p  # wir manipulieren p (consume VT_END)
        arr = []
        while True:
            if p < len(payload) and payload[p] == VT_END:
                p += 1
                break
            arr.append(read_value())
        return arr

    def read_hash():
        nonlocal p  # wir manipulieren p (consume VT_END)
        obj: Dict[str, object] = {}
        while True:
            if p < len(payload) and payload[p] == VT_END:
                p += 1
                break
            k = read_value()
            if not isinstance(k, str): raise ProtoErr("non-string key")
            v = read_value()
            obj[k] = v
        return obj

    root = read_value()
    if not isinstance(root, dict): raise ProtoErr("top-level not hash")
    return root

def main() -> int:
    # Token laden (ohne CLI)
    token = load_token()

    params = {
        "access_token": token,          # Bearer-Token als Parameter
        "device": "entropywatcher/raspi",  # optionale Kennung
    }
    req = build_request("userinfo", params)

    try:
        raw = socket.create_connection((HOST, PORT), timeout=TIMEOUT)
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(raw, server_hostname=HOST)
        tls.settimeout(TIMEOUT)
        tls.sendall(req)
        resp_len = struct.unpack(LE_U32, recv_exact(tls, 4))[0]
        payload  = recv_exact(tls, resp_len)
    except Exception as e:
        print(f"CONNECT/IO ERROR: {e}", file=sys.stderr); return 3
    finally:
        try: tls.close()
        except Exception: pass

    try:
        obj = parse_response(payload)
    except Exception as e:
        print(f"PARSE ERROR: {e}", file=sys.stderr)
        print(f"RAW({len(payload)}B): {payload[:128].hex()}...", file=sys.stderr)
        return 4

    result = obj.get("result")
    uid    = obj.get("userid") or obj.get("uid")
    email  = obj.get("email") or obj.get("mail") or obj.get("emailaddress")

    print("result:", result)
    if uid is not None:   print("uid:", uid)
    if email is not None: print("email:", email)
    if result == 0:
        print("OK: authenticated against eapi.pcloud.com")
        return 0
    else:
        print("FAILED: result != 0", file=sys.stderr)
        print("full:", obj)
        return 5

if __name__ == "__main__":
    sys.exit(main())
