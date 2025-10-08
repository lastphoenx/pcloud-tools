#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_stat_bin.py – pCloud Binary Protocol: Einzelnes Objekt abfragen (stat)
NEU: --with-checksum ruft zusätzlich checksumfile und mapt sha1/sha256/md5 in die Ausgabe.

Beispiele:
  # Per Pfad (Tabelle):
  ./file_stat_bin.py --path /Backup/pfsense/Archiv/config-20250910.xml

  # Per fileid (JSON, mit Checksummen):
  ./file_stat_bin.py --fileid 73702193214 --with-checksum --format json

  # Mit filtermeta (optional):
  ./file_stat_bin.py --path /My Pictures --filtermeta name,created,modified,size,hash,path,isfolder
"""
import argparse, os, ssl, socket, struct, sys, csv, json

from typing import Dict, Any, Union

LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2

TYPE_HASH, TYPE_ARRAY, TYPE_FALSE, TYPE_TRUE, TYPE_DATA, TYPE_END = 16,17,18,19,20,255

# ---------- .env ----------
def load_env(p: str)->Dict[str,str]:
    d={}
    if os.path.isfile(p):
        for line in open(p,encoding="utf-8"):
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k,v=line.split("=",1); k=k.strip(); v=v.strip()
            if v[:1] in "\"'" and v[-1:] in "\"'": v=v[1:-1]
            d[k]=v
    return d

# ---------- Binary request ----------
def build_request(method: str, params: Dict[str, Union[str,int,bool]], *, data_len: int = 0) -> bytes:
    mb = method.encode("utf-8")
    has_data = 1 if data_len > 0 else 0
    parts=[]
    for name,value in params.items():
        nb=name.encode("utf-8")
        if isinstance(value,bool):
            parts.append(bytes([(PARAM_BOOL<<6)|len(nb)])+nb+(b"\x01" if value else b"\x00"))
        elif isinstance(value,int):
            parts.append(bytes([(PARAM_NUMBER<<6)|len(nb)])+nb+struct.pack(LE_U64,value))
        else:
            vb=str(value).encode("utf-8")
            parts.append(bytes([(PARAM_STRING<<6)|len(nb)])+nb+struct.pack(LE_U32,len(vb))+vb)
    body=bytearray()
    body.append((len(mb)&0x7F)|(0x80 if has_data else 0))
    if has_data: body+=struct.pack(LE_U64,data_len)
    body+=mb; body.append(len(params)); body+=b"".join(parts)
    return struct.pack(LE_U16,len(body))+bytes(body)

def recv_exact(s,n):
    buf=bytearray()
    while len(buf)<n:
        c=s.recv(n-len(buf))
        if not c: raise ConnectionError("Verbindung geschlossen")
        buf+=c
    return bytes(buf)

# ---------- Decoder ----------
class BinDecoder:
    def __init__(self,payload:bytes):
        self.b=payload; self.i=0; self.str=[]
    def take(self,n): 
        if self.i+n>len(self.b): raise ValueError("Antwort zu kurz")
        out=self.b[self.i:self.i+n]; self.i+=n; return out
    def u8(self): return self.take(1)[0]
    def read_string(self,t:int)->str:
        if 100<=t<=149:
            ln=t-100; s=self.take(ln).decode("utf-8","replace"); self.str.append(s); return s
        if t in (0,1,2,3):
            ln=int.from_bytes(self.take({0:1,1:2,2:3,3:4}[t]),"little"); s=self.take(ln).decode("utf-8","replace"); self.str.append(s); return s
        if 150<=t<=199: return self.str[t-150]
        if t in (4,5,6,7):
            sid=int.from_bytes(self.take({4:1,5:2,6:3,7:4}[t]),"little"); return self.str[sid]
        raise ValueError("String-Typ unbekannt")
    def read_number(self,t:int)->int:
        if 200<=t<=219: return t-200
        if 8<=t<=15: size=t-7; return int.from_bytes(self.take(size),"little")
        raise ValueError("Number-Typ unbekannt")
    def read_value(self):
        t=self.u8()
        if t==TYPE_HASH:
            obj={}
            while True:
                if self.b[self.i]==TYPE_END: self.i+=1; break
                k=self.read_value(); v=self.read_value(); obj[k]=v
            return obj
        if t==TYPE_ARRAY:
            arr=[]
            while True:
                if self.b[self.i]==TYPE_END: self.i+=1; break
                arr.append(self.read_value())
            return arr
        if t==TYPE_FALSE: return False
        if t==TYPE_TRUE: return True
        if t==TYPE_DATA:
            dlen=int.from_bytes(self.take(8),"little"); return {"__data_len__": dlen}
        if t in list(range(100,150))+[0,1,2,3,4,5,6,7] or 150<=t<=199: return self.read_string(t)
        if t in list(range(8,16))+list(range(200,220)): return self.read_number(t)
        raise ValueError("Unbekannter Typ")

def decode(payload:bytes)->Dict[str,Any]:
    d=BinDecoder(payload); top=d.read_value()
    if not isinstance(top,dict): raise ValueError("Top-Level ist kein Hash")
    return top

def rpc(host:str,port:int,timeout:int,method:str,params:Dict[str,Union[str,int,bool]])->Dict[str,Any]:
    req=build_request(method,params)
    raw=socket.create_connection((host,port),timeout=timeout)
    ctx=ssl.create_default_context()
    tls=ctx.wrap_socket(raw,server_hostname=host)
    tls.settimeout(timeout)
    tls.sendall(req)
    resp_len=struct.unpack(LE_U32,recv_exact(tls,4))[0]
    payload=recv_exact(tls,resp_len)
    tls.close()
    top=decode(payload)
    if top.get("result")!=0:
        raise RuntimeError(f"{method} fehlgeschlagen: {top}")
    return top

# ---------- Helpers ----------
def format_row(md: Dict[str,Any])->Dict[str,Any]:
    isfolder = md.get("isfolder", False)
    return {
        "type": "FOLDER" if isfolder else "FILE",
        "name": md.get("name"),
        "id":   md.get("folderid") if isfolder else md.get("fileid"),
        "parent": md.get("parentfolderid"),
        "path": md.get("path"),
        "created": md.get("created"),
        "modified": md.get("modified"),
        "size": md.get("size"),
        "contenttype": md.get("contenttype"),
        "hash": md.get("hash"),
        "category": md.get("category"),
        "ismine": md.get("ismine"),
        "isshared": md.get("isshared"),
    }

def print_table(row: Dict[str,Any]):
    cols = ["type","name","id","parent","path","created","modified","size","contenttype","hash","sha1","sha256","md5"]
    hdr="  ".join(c.upper() for c in cols); print(hdr); print("-"*max(80,len(hdr)+10))
    print("  ".join(str(row.get(c,"") if row.get(c,"") is not None else "") for c in cols))

def print_csv(row: Dict[str,Any], delim:str):
    cols = ["type","name","id","parent","path","created","modified","size","contenttype","hash","sha1","sha256","md5"]
    w = csv.writer(sys.stdout, delimiter=delim, quoting=csv.QUOTE_MINIMAL)
    w.writerow(cols); w.writerow([row.get(c,"") if row.get(c,"") is not None else "" for c in cols])

# ---------- Main ----------
def main():
    ap=argparse.ArgumentParser(description="pCloud Binary stat – Metadaten eines Objekts (Datei/Ordner), optional mit checksumfile.")
    ap.add_argument("--env-file",default="/opt/entropywatcher/pcloud/.env")
    ap.add_argument("--host"); ap.add_argument("--port",type=int); ap.add_argument("--timeout",type=int)
    ap.add_argument("--device")
    target=ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--path", help="Pfad zum Objekt")
    target.add_argument("--fileid", type=int, help="fileid des Objekts (nur Datei)")
    target.add_argument("--folderid", type=int, help="folderid (Ordner)")
    ap.add_argument("--filtermeta", help="Serverseitige Metafelder-Liste (optional)")
    ap.add_argument("--with-checksum", action="store_true", help="Zusätzlich checksumfile ausführen (für Dateien)")
    ap.add_argument("--format", choices=["table","csv","tsv","json"], default="table")
    args=ap.parse_args()

    env=load_env(args.env_file)
    token=env.get("PCLOUD_TOKEN") or os.environ.get("PCLOUD_TOKEN")
    if not token:
        print("Fehler: Kein Token gefunden (PCLOUD_TOKEN).", file=sys.stderr); sys.exit(2)

    host=args.host or env.get("PCLOUD_HOST","eapi.pcloud.com")
    port=args.port or int(env.get("PCLOUD_PORT","8399"))
    timeout=args.timeout or int(env.get("PCLOUD_TIMEOUT","30"))
    device=args.device or env.get("PCLOUD_DEVICE","entropywatcher/raspi")

    params={"access_token":token,"device":device}
    if args.path: params["path"]=args.path
    if args.fileid is not None: params["fileid"]=args.fileid
    if args.folderid is not None: params["folderid"]=args.folderid
    if args.filtermeta: params["filtermeta"]=args.filtermeta

    # 1) stat
    try:
        top=rpc(host,port,timeout,"stat",params)
    except Exception as e:
        print(f"Fehler bei stat: {e}", file=sys.stderr); sys.exit(1)

    md = top.get("metadata")
    if isinstance(md, list) and md:  # manche Antworten liefern Liste
        md = md[0]
    if not isinstance(md, dict):
        print("Unerwartete Antwortstruktur:", type(md), file=sys.stderr); sys.exit(1)

    row = format_row(md)

    # 2) checksumfile (optional, nur für Dateien)
    if args.with_checksum:
        isfolder = (row.get("type") == "FOLDER")
        if isfolder:
            # Für Ordner gibt es keine checksumfile – wir ignorieren sauber.
            row["sha1"]=row["sha256"]=row["md5"]=""
        else:
            cs_params = {"access_token": token, "device": device}
            if args.path:    cs_params["path"]=args.path
            elif args.fileid is not None: cs_params["fileid"]=args.fileid
            else:
                # Falls der user mit folderid o.Ä. kam, aber File herauskam (unwahrscheinlich):
                if md.get("fileid"): cs_params["fileid"]=md.get("fileid")
                elif md.get("path"):  cs_params["path"]=md.get("path")
            try:
                cs_top = rpc(host,port,timeout,"checksumfile",cs_params)
                # Felder: sha1, sha256 (EU), md5 (US), metadata{...}
                row["sha1"]   = cs_top.get("sha1")
                row["sha256"] = cs_top.get("sha256")
                row["md5"]    = cs_top.get("md5")
                # metadata aus checksumfile ist (meist) deckungsgleich – wir lassen row ansonsten unverändert
            except Exception as e:
                # Nicht hart abbrechen – nur Hinweis
                row["sha1"]=row["sha256"]=row["md5"]=""
                print(f"Hinweis: checksumfile fehlgeschlagen: {e}", file=sys.stderr)

    # Ausgabe
    if args.format=="json":
        json.dump(row, sys.stdout, ensure_ascii=False, indent=2); print(); return
    if args.format=="csv":
        print_csv(row, ","); return
    if args.format=="tsv":
        print_csv(row, "\t"); return
    print_table(row)

if __name__=="__main__":
    main()
