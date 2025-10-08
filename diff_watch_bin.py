#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diff_watch_bin.py – pCloud Binary Protocol: Änderungs-Feed (diff)

NEU:
  --now            Baseline auf "jetzt" setzen (intern last=0). Ohne --watch beendet sich das Tool still nach dem Setzen.
                   Mit --watch startet es live ab jetzt.
  --last N         Liefert die letzten N Events (höchste diffids). --last 1 ~ "nur neuestes Event".
  --after DATETIME Events nach Zeitpunkt (z. B. 2025-10-03T12:00:00Z).
  --block          Serverseitiges Blocken, bis ein Event eintrifft (nur mit diffid / also mit --since/State/--now). Kombiniere mit hohem --timeout.
  --limit N        Obergrenze der Events pro Antwort.
  --exec CMD       Pro Event externes Kommando ausführen. Platzhalter: {etype},{type},{name},{id},{parent},{path},{modified},{size},{hash}
  --latest-only    Pro Abruf nur letztes Event ausgeben (bei --last 1 redundant, aber praktisch im Watch-Mode).
  --minimal        Nur name und id ausgeben (kompakte Pipes/Trigger).

Best Practice:
  # Baseline "ab jetzt" + live, effizient:
  ./diff_watch_bin.py --now --watch --block --timeout 600 --include-files

  # Neueste 10 Events (einmalig):
  ./diff_watch_bin.py --last 10 --include-files --format table

  # Nur neuestes Event (einmalig) minimal:
  ./diff_watch_bin.py --last 1 --include-files --minimal

  # Seit bestimmter Zeit:
  ./diff_watch_bin.py --after "2025-10-02T18:00:00Z" --include-files --format json

  # Hook pro Event:
  ./diff_watch_bin.py --now --watch --include-files --exec 'logger pcloud:{etype} {type} {name} {id}'
"""
import argparse, os, ssl, socket, struct, sys, time, json, csv, re, shlex, subprocess
from typing import Dict, Any, Union, List, Optional

# ---- Binary konstants ----
LE_U16, LE_U32, LE_U64 = "<H", "<I", "<Q"
PARAM_STRING, PARAM_NUMBER, PARAM_BOOL = 0, 1, 2
TYPE_HASH, TYPE_ARRAY, TYPE_FALSE, TYPE_TRUE, TYPE_DATA, TYPE_END = 16, 17, 18, 19, 20, 255

DEFAULT_STATE = "/opt/apps/pcloud-tools/main/state/.pcloud_diff_state.json"

# -------------------- .env --------------------
def load_env(p: str)->Dict[str,str]:
    d={}
    if os.path.isfile(p):
        with open(p,encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k,v=line.split("=",1); k=k.strip(); v=v.strip()
                if v[:1] in "\"'" and v[-1:] in "\"'": v=v[1:-1]
                d[k]=v
    return d

# -------------------- Binary helpers --------------------
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

# -------------------- State --------------------
def load_state(path:str)->Optional[int]:
    try:
        with open(path,"r",encoding="utf-8") as f:
            obj=json.load(f)
        return int(obj.get("diffid"))
    except Exception:
        return None

def save_state(path:str,diffid:int):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path,"w",encoding="utf-8") as f:
            json.dump({"diffid": diffid}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warnung: Konnte State nicht speichern: {e}", file=sys.stderr)

# -------------------- Rows & Printing --------------------
def as_row(entry: Dict[str,Any])->Dict[str,Any]:
    md = entry.get("metadata") or {}
    # isfolder ableiten: erst Top-Level, sonst metadata, sonst aus Event-Typ
    isfolder = entry.get("isfolder")
    if isfolder is None:
        isfolder = md.get("isfolder")
    if isfolder is None:
        # Heuristik aus Eventnamen:
        et = (entry.get("event") or "").lower()
        isfolder = ("folder" in et)

    # Felder bevorzugt aus metadata ziehen; dann Top-Level; dann leer
    name     = md.get("name") or entry.get("name") or ""
    path     = md.get("path") or entry.get("path") or ""
    parent   = md.get("parentfolderid") or entry.get("parentfolderid")
    modified = md.get("modified") or entry.get("modified") or entry.get("time")  # time = Eventzeit als Fallback
    size     = md.get("size") or entry.get("size")
    chash    = md.get("hash") or entry.get("hash")

    # IDs: je nach Typ folderid oder fileid
    if isfolder:
        _id = md.get("folderid") or entry.get("folderid")
        typ = "FOLDER"
    else:
        _id = md.get("fileid") or entry.get("fileid")
        typ = "FILE"

    # etype & diffid/time mitgeben (nützlich in JSON/CSV)
    etype   = entry.get("event")
    ediffid = entry.get("diffid")
    etime   = entry.get("time")

    return {
        "etype": etype,
        "type": typ,
        "name": name,
        "id": _id,
        "parent": parent,
        "path": path,
        "modified": modified,
        "size": size,
        "hash": chash,
        "event_time": etime,
        "event_diffid": ediffid,
    }

def print_rows(rows: List[Dict[str,Any]], fmt: str, *, minimal: bool, minimal_plus: bool = False, columns_opt: Optional[str] = None):
    if columns_opt:
        columns = [c.strip() for c in columns_opt.split(",") if c.strip()]
    elif minimal_plus:
        columns = ["name","id","etype","event_time"]
    elif minimal:
        columns = ["name","id"]
    else:
        columns = ["etype","type","name","id","parent","path","modified","size","hash","event_time","event_diffid"]

    if fmt=="json":
        out = [{c: (r.get(c,"") if r.get(c,"") is not None else "") for c in columns} for r in rows]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2); print(); return

    if fmt in ("csv","tsv"):
        writer = csv.writer(sys.stdout, delimiter=('\t' if fmt=="tsv" else ','), quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([r.get(c,"") if r.get(c,"") is not None else "" for c in columns])
        return

    hdr="  ".join(c.upper() for c in columns); print(hdr); print("-"*max(60,len(hdr)+10))
    for r in rows:
        print("  ".join(str(r.get(c,"") if r.get(c,"") is not None else "") for c in columns))

# -------------------- Exec Hook --------------------
def run_exec(cmd_template:str, row:Dict[str,Any]):
    mapping = {k: ("" if v is None else str(v)) for k,v in row.items()}
    try:
        cmd = cmd_template.format(**mapping)
    except KeyError as ke:
        print(f"Warnung: Platzhalter {ke} nicht vorhanden – Kommando ausgelassen.", file=sys.stderr)
        return
    try:
        args = shlex.split(cmd)
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if res.returncode != 0:
            print(f"[exec] Exit {res.returncode}: {cmd}\nSTDERR: {res.stderr.strip()}", file=sys.stderr)
    except Exception as e:
        print(f"[exec] Fehler: {e}", file=sys.stderr)

# --- einfacher In-Memory-Cache für Folder-Metadaten, damit wir Eltern nicht zigmal abfragen
_FOLDER_CACHE: Dict[int, Dict[str, Any]] = {}

def resolve_path_if_needed(row: Dict[str,Any], host, port, timeout, token, device):
    """
    Ergänzt row['path'], wenn möglich.
    Strategie:
      A) stat(objekt) -> wenn metadata.path existiert, nimm ihn.
      B) Sonst: baue Pfad über Parent-Kette nach (stat auf parentfolderid, dann dessen parent, ...).
         Setze path = "/" + "/".join(namen_von_root_bis_datei).
    Hinweise:
      - Bei delete-Events (Objekt existiert nicht mehr) bleibt path leer.
      - Wir cachen Folder-Metadaten (_FOLDER_CACHE) nach folderid für Performance.
    """
    if row.get("path"):  # schon vorhanden
        return

    is_folder = (row.get("type") == "FOLDER")
    obj_id = row.get("id")
    name = row.get("name") or ""
    parent = row.get("parent")

    if not obj_id:
        return

    def _stat_file(file_id: int) -> Optional[Dict[str,Any]]:
        params = {"access_token": token, "device": device, "fileid": int(file_id)}
        try:
            top = rpc(host, port, timeout, "stat", params)
            md = top.get("metadata")
            if isinstance(md, list) and md:
                md = md[0]
            return md if isinstance(md, dict) else None
        except Exception:
            return None

    def _stat_folder(folder_id: int) -> Optional[Dict[str,Any]]:
        # erst Cache
        if folder_id in _FOLDER_CACHE:
            return _FOLDER_CACHE[folder_id]
        params = {"access_token": token, "device": device, "folderid": int(folder_id)}
        try:
            top = rpc(host, port, timeout, "stat", params)
            md = top.get("metadata")
            if isinstance(md, list) and md:
                md = md[0]
            if isinstance(md, dict):
                _FOLDER_CACHE[folder_id] = md
                return md
        except Exception:
            pass
        return None

    # A) direktes stat() – falls path existiert, super
    md = _stat_folder(int(obj_id)) if is_folder else _stat_file(int(obj_id))
    if md and md.get("path"):
        row["path"] = md.get("path")
        return

    # B) Pfad aus Elternkette zusammensetzen
    parts: List[str] = []
    # für Ordner: nimm dessen Namen als letztes Segment,
    # für Datei: nimm den Dateinamen als letztes Segment
    if name:
        parts.append(name)

    # ab hier: Eltern hochlaufen
    cur_parent = parent
    safety = 0
    while cur_parent is not None and int(cur_parent) != 0 and safety < 1024:
        fmd = _stat_folder(int(cur_parent))
        if not fmd:
            break
        fname = fmd.get("name") or ""
        if fname and fname != "/":
            parts.append(fname)
        cur_parent = fmd.get("parentfolderid")
        safety += 1

    # Root hinzufügen ("/")
    parts.append("")  # damit join mit führendem Slash endet: "/a/b/c"
    parts.reverse()
    # sauber joinen und doppelte Slashes vermeiden
    path = "/".join(p.strip("/") for p in parts)
    if not path.startswith("/"):
        path = "/" + path
    row["path"] = path


# -------------------- Core --------------------
def do_once(host:str,port:int,timeout:int,token:str,device:str,
            diffid:Optional[int], name_rx:Optional[re.Pattern],
            include_files:bool, only_folders:bool,
            fmt:str, filtermeta:Optional[str],
            latest_only:bool, minimal:bool,
            exec_cmd:Optional[str],
            last:Optional[int], after:Optional[str], block:bool, limit:Optional[int],
            resolve_path: bool, minimal_plus: bool, columns_opt: Optional[str]) -> int:

    params={"access_token":token,"device":device}

    # Request-Varianten gemäß Doku:
    if diffid is not None:
        params["diffid"]=int(diffid)
        if block:
            params["block"]=1
    if last is not None:
        params["last"]=int(last)
    if after:
        params["after"]=after
    if limit is not None:
        params["limit"]=int(limit)
    if filtermeta:
        params["filtermeta"]=filtermeta

    top = rpc(host,port,timeout,"diff",params)

    next_diffid = int(top.get("diffid") or top.get("nextdiffid") or 0)
    entries = top.get("entries") or top.get("changes") or []

    rows=[]
    for e in entries:
        md = e.get("metadata") or {}
        # Typ-Filter
        isfolder_entry = e.get("isfolder")
        if isfolder_entry is None:
            isfolder_entry = md.get("isfolder")
        if isfolder_entry is None:
            isfolder_entry = ("folder" in (e.get("event") or "").lower())

        if only_folders and not isfolder_entry:
            continue
        if (not include_files) and (not isfolder_entry):
            continue

        # Name für Regex ermitteln (metadata->Top-Level)
        _name = md.get("name") or e.get("name") or ""
        if name_rx and not name_rx.search(_name):
            continue

        rows.append(as_row(e))

    if latest_only and rows:
        rows = [rows[-1]]  # letztes Event der Antwort

    # >>> HIER den Pfad-Resolver einhängen <<<
    if resolve_path and rows:
        for r in rows:
            resolve_path_if_needed(r, host, port, timeout, token, device)

    if rows:
        print_rows(rows, fmt, minimal=minimal, minimal_plus=minimal_plus, columns_opt=columns_opt)
        if exec_cmd:
            for r in rows:
                run_exec(exec_cmd, r)

    else:
        if not latest_only:
            print("(keine passenden Änderungen)")

    return next_diffid

def get_current_diffid_via_last0(host:str,port:int,timeout:int,token:str,device:str, filtermeta:Optional[str])->int:
    # "Optimized do nothing": last=0 liefert nur aktuelle diffid
    params={"access_token":token,"device":device,"last":0}
    if filtermeta: params["filtermeta"]=filtermeta
    top = rpc(host,port,timeout,"diff",params)
    return int(top.get("diffid") or top.get("nextdiffid") or 0)

def main():
    ap=argparse.ArgumentParser(description="pCloud Binary diff – Änderungen abrufen/mitverfolgen (now/last/after/block/limit + exec/minimal).")
    ap.add_argument("--env-file", default="/opt/entropywatcher/pcloud/.env")

    ap.add_argument("--host"); ap.add_argument("--port",type=int); ap.add_argument("--timeout",type=int)
    ap.add_argument("--device")

    ap.add_argument("--since", type=int, help="Start-diffid (überschreibt gespeicherten State)")
    ap.add_argument("--now", action="store_true", help="Baseline auf jetzt setzen (intern last=0). Ohne --watch nur setzen & beenden.")
    ap.add_argument("--state-file", default=DEFAULT_STATE, help=f"Datei für diffid-State (Default: {DEFAULT_STATE})")

    ap.add_argument("--watch", action="store_true", help="Fortlaufend pollen (oder blocken mit --block)")
    ap.add_argument("--interval", type=int, default=30, help="Poll-Intervall in Sekunden (Default: 30; bei --block häufig 0 sinnvoll)")

    ap.add_argument("--include-files", action="store_true", help="Datei-Events ausgeben (Default: nur Ordner)")
    ap.add_argument("--only-folders", action="store_true", help="Nur Ordner-Events (überschreibt --include-files)")

    ap.add_argument("--match", help="Regex Filter für NAME")
    ap.add_argument("--match-ignore-case", action="store_true")

    ap.add_argument("--filtermeta", help="Serverseitige Metafelder-Liste (optional)")
    ap.add_argument("--format", choices=["table","csv","tsv","json"], default="table")

    ap.add_argument("--latest-only", action="store_true", help="Pro Abruf nur das neueste Event ausgeben")
    ap.add_argument("--minimal", action="store_true", help="Nur name und id ausgeben")
    ap.add_argument("--exec", dest="exec_cmd", help="Externes Kommando, Platzhalter: {etype},{type},{name},{id},{parent},{path},{modified},{size},{hash}")

    # NEU aus Doku:
    ap.add_argument("--last", type=int, help="Letzte N Events zurückgeben (last=N). last=0 liefert nur die aktuelle diffid.")
    ap.add_argument("--after", help='Events nach Zeitpunkt (z. B. "2025-10-03T12:00:00Z")')
    ap.add_argument("--block", action="store_true", help="Serverseitig blocken, bis Event eintrifft (nur mit diffid nutzbar)")
    ap.add_argument("--limit", type=int, help="Max. Anzahl Events in einer Antwort")

    ap.add_argument("--minimal-plus", action="store_true", help="Kompakt: name,id,etype,event_time")
    ap.add_argument("--columns", help="Kommagetrennt, z.B. 'etype,name,id,event_time'")
    ap.add_argument("--resolve-path", action="store_true", help="Fehlende PATHs per stat() nachschlagen (langsamer)")


    args=ap.parse_args()
    env=load_env(args.env_file)
    token=env.get("PCLOUD_TOKEN") or os.environ.get("PCLOUD_TOKEN")
    if not token:
        print("Fehler: Kein Token gefunden (PCLOUD_TOKEN).", file=sys.stderr); sys.exit(2)

    host=args.host or env.get("PCLOUD_HOST","eapi.pcloud.com")
    port=args.port or int(env.get("PCLOUD_PORT","8399"))
    timeout=args.timeout or int(env.get("PCLOUD_TIMEOUT","30"))
    device=args.device or env.get("PCLOUD_DEVICE","entropywatcher/raspi")

    # Regex vorbereiten
    name_rx=None
    if args.match:
        flags = re.IGNORECASE if args.match_ignore_case else 0
        try:
            name_rx = re.compile(args.match, flags)
        except re.error as ex:
            print(f"Ungültiger Regex in --match: {ex}", file=sys.stderr); sys.exit(2)

    include_files = False if args.only_folders else bool(args.include_files)

    # Baseline bestimmen
    if args.now:
        try:
            now_diffid = get_current_diffid_via_last0(host,port,timeout,token,device,args.filtermeta)
        except Exception as e:
            print(f"Fehler bei --now (last=0): {e}", file=sys.stderr); sys.exit(1)
        save_state(args.state_file, now_diffid)
        if not args.watch:
            return
        diffid = now_diffid
    else:
        diffid = args.since if args.since is not None else load_state(args.state_file)

    # Einmalig (kein watch)
    if not args.watch:
        try:
            next_diffid = do_once(host,port,timeout,token,device,
                                  diffid, name_rx, include_files, args.only_folders,
                                  args.format, args.filtermeta,
                                  args.latest_only, args.minimal, args.exec_cmd,
                                  args.last, args.after, args.block, args.limit,
                                  args.resolve_path, args.minimal_plus, args.columns)
            if next_diffid:
                save_state(args.state_file, next_diffid)
        except socket.timeout:
            print("(timeout ohne Events)")
        except Exception as e:
            print(f"Fehler: {e}", file=sys.stderr); sys.exit(1)
        return

    # Watch-Loop
    while True:
        try:
            next_diffid = do_once(host,port,timeout,token,device,
                                  diffid, name_rx, include_files, args.only_folders,
                                  args.format, args.filtermeta,
                                  args.latest_only, args.minimal, args.exec_cmd,
                                  args.last, args.after, args.block, args.limit,
                                  args.resolve_path, args.minimal_plus, args.columns)
            if next_diffid:
                save_state(args.state_file, next_diffid)
                diffid = next_diffid
        except KeyboardInterrupt:
            print("\nBeendet per Ctrl-C."); break
        except socket.timeout:
            # bei blockendem Call ok → einfach nächste Runde
            pass
        except Exception as e:
            print(f"Fehler im Watch-Loop: {e}", file=sys.stderr)
        # bei --block ist Intervall typischerweise 0 sinnvoll
        time.sleep(max(0, args.interval))

if __name__=="__main__":
    main()
