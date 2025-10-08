#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_debug_tools.py – Read-only + Admin-Helpers für pCloud/NAS:
- local-exists      : prüft, ob lokale Datei/Ordner existiert
- stat-local        : zeigt Größe/mtime/sha256 einer lokalen Datei
- remote-exists     : prüft, ob Remote-Datei/Ordner existiert (per path)
- stat-remote       : zeigt Remote-Metadaten (Datei oder Ordner) anhand PATH
- verify            : vergleicht lokale Datei gegen Remote (path oder fileid)
- hash-remote       : zeigt remote-Hashes (checksumfile/stat) kompakt
NEU:
- ls                : listet Inhalte (wie list_folder_ids), Pfad oder folderid
- stat-id           : Details zu fileid/folderid (mit vollständigem Pfad)
- ensure-path       : legt Pfad rekursiv an und zeigt folderid
- print-folderid    : nur folderid einer Pfadwurzel als JSON ausgeben
- examplse          : Praxisbeispiele anzeigen

"""

from __future__ import annotations
import argparse, os, sys, datetime as dt, csv, json
import pcloud_bin_lib as pc

def _fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")

def _cfg(args) -> dict:
    return pc.effective_config(
        env_file=args.env_file, env_dir=args.env_dir, profile=args.profile,
        overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                   "device": args.device, "token": args.token}
    )

# ---------- simple printers ----------
DEFAULT_COLS = ["type","name","id","parent","path"]
DETAIL_COLS = ["created","modified","size","contenttype","hash"]

def _print_table(rows: list[dict], columns: list[str], header: str | None = None) -> None:
    if header:
        print(header)
    hdr = "  ".join(c.upper() for c in columns)
    print(hdr)
    print("-" * max(80, len(hdr)+10))
    for r in rows:
        vals = []
        for c in columns:
            v = r.get(c, "")
            if v is None: v = ""
            vals.append(str(v))
        print("  ".join(vals))

def _print_delim(rows: list[dict], columns: list[str], delim: str) -> None:
    w = csv.writer(sys.stdout, delimiter=delim, quoting=csv.QUOTE_MINIMAL)
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") if r.get(c, "") is not None else "" for c in columns])

def _print_json(rows: list[dict], columns: list[str]) -> None:
    out = [{c: (row.get(c, None)) for c in columns} for row in rows]
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()

# ---------- existing commands ----------
def cmd_local_exists(args):
    p = os.path.abspath(args.path)
    if args.kind == "file":
        ok = os.path.isfile(p)
    elif args.kind == "dir":
        ok = os.path.isdir(p)
    else:
        ok = os.path.exists(p)
    print(f"{args.kind or 'path'}: {'YES' if ok else 'NO'} -> {p}")

def cmd_stat_local(args):
    p = os.path.abspath(args.path)
    if not os.path.isfile(p):
        print("NO (not a file)", file=sys.stderr); sys.exit(2)
    size = os.path.getsize(p)
    mtime = os.path.getmtime(p)
    sha256 = pc.sha256_file(p)
    print(f"local path : {p}")
    print(f"size       : {size}")
    print(f"mtime      : {_fmt_ts(mtime)}")
    print(f"sha256     : {sha256}")

def cmd_remote_exists(args):
    cfg = _cfg(args)
    rp = pc._norm_remote_path(args.path)
    # Datei?
    try:
        md = pc.stat_file(cfg, path=rp, with_checksum=False)
        if md and not md.get("isfolder", False):
            print(f"remote file : YES -> {rp} (fileid={md.get('fileid')}, size={md.get('size')})")
            return
    except RuntimeError as e:
        if " 2055" not in str(e):
            raise
    # Ordner?
    try:
        md = pc.stat_folder(cfg, path=rp)
        if md and md.get("isfolder", False):
            print(f"remote folder : YES -> {rp} (folderid={md.get('folderid')})")
            return
    except RuntimeError as e:
        if " 2055" not in str(e):
            raise
    print(f"remote path : NO -> {rp}")

def cmd_stat_remote(args):
    cfg = _cfg(args)
    rp = pc._norm_remote_path(args.path)
    try:
        md = pc.stat_file(cfg, path=rp, with_checksum=True)
        print("REMOTE FILE")
        print(f"path    : {md.get('path')}")
        print(f"fileid  : {md.get('fileid')}")
        print(f"size    : {md.get('size')}")
        print(f"sha256  : {md.get('sha256')}")
        print(f"sha1    : {md.get('sha1')}")
        return
    except RuntimeError as e:
        if " 2055" not in str(e): raise
    try:
        md = pc.stat_folder(cfg, path=rp)
        print("REMOTE FOLDER")
        print(f"path     : {md.get('path')}")
        print(f"folderid : {md.get('folderid')}")
    except RuntimeError as e:
        print(f"NOT FOUND: {rp} ({e})", file=sys.stderr); sys.exit(3)

def cmd_verify(args):
    cfg = _cfg(args)
    lp = os.path.abspath(args.local)
    if not os.path.isfile(lp):
        print("local file missing", file=sys.stderr); sys.exit(2)
    if args.remote_path:
        ok, cs = pc.verify_remote_vs_local(cfg, path=pc._norm_remote_path(args.remote_path), local_path=lp)
        where = args.remote_path
    else:
        ok, cs = pc.verify_remote_vs_local(cfg, fileid=int(args.fileid), local_path=lp)
        where = f"fileid={args.fileid}"
    print(f"LOCAL : {lp}")
    print(f"REMOTE: {where}")
    print(f"RESULT: {'IDENTICAL' if ok else 'DIFFERENT/UNKNOWN'}")
    print(f"sha256(remote): {cs.get('sha256')}")
    print(f"sha1(remote)  : {cs.get('sha1')}")
    try:
        lsha256 = pc.sha256_file(lp)
        print(f"sha256(local) : {lsha256}")
    except Exception:
        pass

def cmd_hash_remote(args):
    cfg = _cfg(args)
    if args.path:
        cs = pc.checksumfile(cfg, path=pc._norm_remote_path(args.path))
        where = args.path
    else:
        cs = pc.checksumfile(cfg, fileid=int(args.fileid))
        where = f"fileid={args.fileid}"
    print(f"REMOTE: {where}")
    print(f"sha256: {cs.get('sha256')}")
    print(f"sha1  : {cs.get('sha1')}")

# ---------- NEW: ls / stat-id / ensure-path / print-folderid ----------

def cmd_ls(args):
    cfg = _cfg(args)
    rows = pc.list_rows(
        cfg,
        path=pc._norm_remote_path(args.path) if args.path else None,
        folderid=args.folderid,
        recursive=bool(args.recursive or args.max_depth and args.max_depth > 1),
        include_files=bool(args.files),
        max_depth=args.max_depth,
        prefer_server_path=bool(args.server_path)
    )
    # match (Regex auf NAME)
    if args.match:
        import re
        flags = re.IGNORECASE if args.match_ignore_case else 0
        rx = re.compile(args.match, flags)
        rows = [r for r in rows if rx.search(r.get("name") or "")]
    # relative?
    base = args.path if args.path else (rows[0].get("path") if rows else "/")
    if args.relative and base:
        rows = pc.relative_paths(rows, base)

    # Spalten
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    elif args.details:
        columns = DEFAULT_COLS + DETAIL_COLS
    else:
        columns = DEFAULT_COLS

    header = None
    if args.header:
        root_disp = args.path or (f"folderid={args.folderid}" if args.folderid is not None else "/")
        scope = "rekursiv" if (args.recursive or (args.max_depth and args.max_depth > 1)) else "nicht rekursiv"
        header = f"\nInhalt von {root_disp} ({scope}):"

    # Ausgabeformat
    if args.format == "table":
        _print_table(rows, columns, header)
    elif args.format == "csv":
        if header: print(header)
        _print_delim(rows, columns, ",")
    elif args.format == "tsv":
        if header: print(header)
        _print_delim(rows, columns, "\t")
    else:
        _print_json(rows, columns)

def cmd_stat_id(args):
    cfg = _cfg(args)
    if args.fileid is not None:
        r = pc.row_for_fileid(cfg, int(args.fileid), with_checksum=bool(args.details))
    else:
        r = pc.row_for_folderid(cfg, int(args.folderid))
    cols = (DEFAULT_COLS + DETAIL_COLS) if args.details else DEFAULT_COLS
    _print_table([r], cols, header="IDENT")

def cmd_ensure_path(args):
    cfg = _cfg(args)
    p = pc._norm_remote_path(args.path)
    fid = pc.ensure_path(cfg, p)
    print(f"OK: {p} (folderid={fid})")

def cmd_print_folderid(args):
    cfg = _cfg(args)
    p = pc._norm_remote_path(args.path)
    fid = pc.ensure_path(cfg, p) if args.ensure else pc.get_folder_meta(cfg, path=p, showpath=True).get("folderid")
    if not fid:
        print("Fehler: Konnte folderid nicht ermitteln.", file=sys.stderr); sys.exit(3)
    print(json.dumps({"folderid": int(fid)}))


# ------------------------- examples ----------------------
def _print_examples_and_exit():
    examples = r"""
Beispiele:

  # Baum-Listing wie im Explorer:
  pcloud_debug_tools.py ls --path "/Backup" --files --recursive --max-depth 2 --details --header

  # Pfade relativ zum Start anzeigen:
  pcloud_debug_tools.py ls --path "/Backup/av-quarantine" --files --relative

  # Nach NAME matchen (Regex, case-insensitive):
  pcloud_debug_tools.py ls --path "/Backup" --files --match ".*\.png$" --match-ignore-case

  # Über folderid starten, als JSON:
  pcloud_debug_tools.py ls --folderid 19430439097 --files --format json

  # Einzel-Objekt per ID:
  pcloud_debug_tools.py stat-id --fileid 75453148696 --details
  pcloud_debug_tools.py stat-id --folderid 19430947711

  # Remote-Path existiert?
  pcloud_debug_tools.py remote-exists "/Backup/av-quarantine/neuerOrdner2"

  # Remote-Hashes:
  pcloud_debug_tools.py hash-remote --fileid 75453148696

  # Lokale Datei vs. Remote validieren:
  pcloud_debug_tools.py verify --remote-path "/Backup/av-quarantine/neu 66.txt" --local ./neu\ 66.txt

  # Pfad sicher anlegen:
  pcloud_debug_tools.py ensure-path "/Backup/new/subdir"

  # Nur die folderid als JSON:
  pcloud_debug_tools.py print-folderid "/Backup/new/subdir"
  pcloud_debug_tools.py print-folderid "/Backup/new/subdir" --ensure
"""
    print(examples.strip())
    raise SystemExit(0)



# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="pCloud/NAS Diagnose + Admin-Tools")
    ap.add_argument("--env-file"); ap.add_argument("--env-dir"); ap.add_argument("--profile")
    ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--timeout", type=int)
    ap.add_argument("--device"); ap.add_argument("--token")

#    sub = ap.add_subparsers(dest="cmd", required=True)

    ap.add_argument("--examples", action="store_true", help="Beispiele anzeigen und beenden")

    # subparsers NICHT required machen
    sub = ap.add_subparsers(dest="cmd")  # kein required=True

    s = sub.add_parser("local-exists"); s.add_argument("path"); s.add_argument("--kind", choices=["file","dir","any"], default="any"); s.set_defaults(func=cmd_local_exists)
    s = sub.add_parser("stat-local");   s.add_argument("path"); s.set_defaults(func=cmd_stat_local)
    s = sub.add_parser("remote-exists"); s.add_argument("path"); s.set_defaults(func=cmd_remote_exists)
    s = sub.add_parser("stat-remote");  s.add_argument("path"); s.set_defaults(func=cmd_stat_remote)

    s = sub.add_parser("verify")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--remote-path")
    g.add_argument("--fileid", type=int)
    s.add_argument("--local", required=True)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("hash-remote")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--path")
    g.add_argument("--fileid", type=int)
    s.set_defaults(func=cmd_hash_remote)

    # NEW: ls
    s = sub.add_parser("ls", help="Ordner/Dateien auflisten (Pfad oder folderid).")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--path")
    g.add_argument("--folderid", type=int)
    s.add_argument("--files", action="store_true", help="Dateien mit anzeigen")
    s.add_argument("--recursive", action="store_true", help="rekursiv (oder --max-depth)")
    s.add_argument("--max-depth", type=int)
    s.add_argument("--server-path", action="store_true", help="Serverpfad bevorzugen (falls geliefert)")
    s.add_argument("--relative", action="store_true", help="Pfade relativ zum Start ausgeben")
    s.add_argument("--details", action="store_true", help="Zusatzspalten (created, modified, size, contenttype, hash)")
    s.add_argument("--columns", help="Kommagetrennte Spaltenliste")
    s.add_argument("--format", choices=["table","csv","tsv","json"], default="table")
    s.add_argument("--match", help="Regex auf NAME")
    s.add_argument("--match-ignore-case", action="store_true")
    s.add_argument("--header", action="store_true", help="Header mit Root/Scope ausgeben")
    s.set_defaults(func=cmd_ls)

    # NEW: stat-id
    s = sub.add_parser("stat-id", help="Zeigt Details zu fileid/folderid")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--fileid", type=int)
    g.add_argument("--folderid", type=int)
    s.add_argument("--details", action="store_true")
    s.set_defaults(func=cmd_stat_id)

    # NEW: ensure-path
    s = sub.add_parser("ensure-path", help="Pfad rekursiv anlegen")
    s.add_argument("path"); s.set_defaults(func=cmd_ensure_path)

    # NEW: print-folderid
    s = sub.add_parser("print-folderid", help="Nur folderid der Pfadwurzel ausgeben (JSON)")
    s.add_argument("path"); s.add_argument("--ensure", action="store_true")
    s.set_defaults(func=cmd_print_folderid)

    args = ap.parse_args()
    if args.examples:
        _print_examples_and_exit()

    args.func(args)

if __name__ == "__main__":
    main()
