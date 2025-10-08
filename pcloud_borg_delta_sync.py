#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcloud_borg_delta_sync.py – Delta-Sync NAS -> pCloud (ohne Subprozesse).

Features:
- Dry-Run schreibt nie State.
- Unterordner-Spiegelung bei --dest-path (idempotent via ensure_path).
- Skip-Existing: size (schnell) oder hash (exakt/SHA-256).
- Chunked Upload mit Progress.
"""
import os, sys, re, time, json, argparse
from typing import List, Tuple
import pcloud_bin_lib as pc

STATE_DEFAULT = "/opt/entropywatcher/pcloud/state/borg_delta_state.json"

EXCLUDE_DEFAULT = [
    r'/cache/', r'/tmp/', r'/\.cache/', r'/\.locks?/', r'/lock\.exclusive$', r'/lock\.roster$',
    r'/txn\.tmp$', r'/lost\+found(/|$)'
]

def load_state(path):
    if not os.path.isfile(path): return {"last_run": 0.0, "retry": []}
    try:    return json.load(open(path,"r",encoding="utf-8"))
    except: return {"last_run": 0.0, "retry": []}

def save_state(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def compile_filters(incl, excl):
    rx_incl=[re.compile(p) for p in incl]
    rx_excl=[re.compile(p) for p in (excl or []) + EXCLUDE_DEFAULT]
    return rx_incl, rx_excl

def want(rel, rx_incl, rx_excl):
    if any(rx.search(rel) for rx in rx_excl): return False
    if rx_incl and not any(rx.search(rel) for rx in rx_incl): return False
    return True

def find_new_since(src_root, last_run, slack, rx_incl, rx_excl):
    out=[]; base=os.path.abspath(src_root)
    for root,_,files in os.walk(base):
        rel_root=os.path.relpath(root, base); rel_root="" if rel_root=="." else rel_root
        for name in files:
            ab=os.path.join(root,name)
            try: st=os.stat(ab, follow_symlinks=False)
            except FileNotFoundError: continue
            if st.st_mtime <= (last_run - slack): continue
            rel=os.path.join(rel_root,name).replace("\\","/")
            if not want(rel, rx_incl, rx_excl): continue
            out.append((rel,ab,int(st.st_size),float(st.st_mtime)))
    out.sort(key=lambda t: t[3])
    return out

def main():
    ap = argparse.ArgumentParser(description="Delta-Sync NAS -> pCloud (Binary, ohne Subprozesse).")
    ap.add_argument("--env-file", help=".env Pfad (optional).")
    ap.add_argument("--src-root", required=True, help="Lokales Borg/Quell-Verzeichnis (Wurzel).")
    dst = ap.add_mutually_exclusive_group(required=True)
    dst.add_argument("--dest-path", help="Zielwurzel als pCloud-Pfad (z. B. /Backup/borg-mirror).")
    dst.add_argument("--dest-folderid", type=int, help="Zielwurzel als folderid.")
    ap.add_argument("--flatten", action="store_true",
                    help="Nicht spiegeln: alle Dateien flach in den Zielordner laden (nur bei --dest-path).")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Vor Upload identische Dateien überspringen.")
    ap.add_argument("--exist-check", choices=["size","hash"], default="size",
                    help="Kriterium für --skip-existing: size (schnell) oder hash (SHA-256).")
    ap.add_argument("--state-file", default=STATE_DEFAULT, help=f"State-Datei (Default: {STATE_DEFAULT})")
    ap.add_argument("--mtime-slack", type=int, default=2, help="Toleranz Sekunden beim mtime-Vergleich.")
    ap.add_argument("--include", action="append", default=[], help="Regex (mehrfach) – nur passende Dateien.")
    ap.add_argument("--exclude", action="append", default=[], help="Regex (mehrfach) – ausschließen.")
    ap.add_argument("--chunk-size", type=int, default=8*1024*1024)
    ap.add_argument("--timeout", type=int, help="Override Timeout (Sek.)")
    ap.add_argument("--host", help="Override Host")
    ap.add_argument("--port", type=int, help="Override Port")
    ap.add_argument("--device", help="Override Device")
    ap.add_argument("--token", help="Override Token")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--verify", action="store_true", help="Nach Upload SHA-256 lokal vs. Server prüfen (Europa).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--profile", help="Name des pCloud-Profils (lädt z.B. profiles/<profile>.env).")
    ap.add_argument("--env-dir", help="Basisordner für .env und profiles/.")

    args = ap.parse_args()

    if not os.path.isdir(args.src_root):
        print(f"Fehler: src-root nicht gefunden: {args.src_root}", file=sys.stderr); sys.exit(2)

    # Effektive Konfiguration aus .env/ENV + Overrides
    cfg = pc.effective_config
      env_file=args.env_file,
      overrides={"host": args.host, "port": args.port, "timeout": args.timeout,
                 "device": args.device, "token": args.token},
      profile=args.profile,
      env_dir=args.env_dir
    )
    
    state = load_state(args.state_file)
    last_run = float(state.get("last_run", 0.0))
    retry = list(dict.fromkeys(state.get("retry", [])))

    rx_incl, rx_excl = compile_filters(args.include, args.exclude)
    new_files = find_new_since(args.src_root, last_run, args.mtime_slack, rx_incl, rx_excl)

    print(f"Neue Dateien seit {time.strftime('%F %T', time.localtime(last_run))}: {len(new_files)}")
    if retry:
        print(f"Offene Retries: {len(retry)}")

    base = os.path.abspath(args.src_root)
    queue: List[Tuple[str,str]] = []
    for ab in retry:
        if os.path.isfile(ab):
            rel = os.path.relpath(ab, base).replace("\\","/")
            if want(rel, rx_incl, rx_excl):
                queue.append((rel, ab))
    for rel, ab, _, _ in new_files:
        queue.append((rel, ab))

    if not queue:
        print("Nichts zu tun.")
        if not retry and not args.dry_run:
            state["last_run"] = time.time()
            save_state(args.state_file, state)
        sys.exit(0)

    # Zielwurzel bestimmen (und ggf. anlegen)
    dest_folderid = args.dest_folderid
    dest_path = None
    if dest_folderid is None:
        dest_path = args.dest_path.rstrip("/") if args.dest_path != "/" else "/"
        if args.dry_run:
            # im Dry-Run sicherstellen, dass wir den geplanten Pfad ausgeben
            print(f"[dry-run] Zielpfad: {dest_path}")
            # keine Netzwerkaktion nötig
            pass
        else:
            dest_folderid = pc.ensure_path(cfg, dest_path)
        print(f"[Info] Ziel: {dest_path} (folderid={dest_folderid})")

    failed = []

    def _progress(done: int, total: int):
        if not args.progress: return
        width = 28
        ratio = 0 if total==0 else done/total
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        done_str = f"{done/1024/1024:.2f} MiB"
        total_str = f"{total/1024/1024:.2f} MiB"
        print(f"\r[{bar}] {ratio*100:6.2f}%  {done_str}/{total_str}", end="", flush=True)
        if done==total:
            print()

    for rel, ab in queue:
        # Zielordner (Unterordner-Spiegelung)
        target_folderid = dest_folderid
        target_label = f"folderid={target_folderid}"
        if dest_path is not None and not args.flatten:
            rel_dir = os.path.dirname(rel).replace("\\","/")
            sub_path = dest_path if not rel_dir or rel_dir=="." else dest_path.rstrip("/") + "/" + rel_dir
            if args.dry_run:
                target_label = sub_path
            else:
                target_folderid = pc.ensure_path(cfg, sub_path)
                target_label = f"folderid={target_folderid}"

        # Skip-Existing?
        if args.skip_existing:
            if args.exist_check == "size":
                # nur Größe vergleichen
                try:
                    st = os.stat(ab, follow_symlinks=False)
                except FileNotFoundError:
                    st = None
                remote_info = None
                if dest_path is not None and not args.flatten:
                    # exakt per Pfad
                    rfile = (dest_path.rstrip("/") + "/" + rel) if dest_path else rel
                    rfile = rfile.replace("//","/")
                    remote_info = pc.stat_file(cfg, path=rfile, with_checksum=False) or None
                else:
                    # flach: per Name im Ordner suchen
                    fid = pc.find_child_fileid(cfg, target_folderid, os.path.basename(rel))
                    if fid: remote_info = pc.stat_file(cfg, fileid=fid, with_checksum=False)
                if st and remote_info and int(remote_info.get("size", -1)) == int(st.st_size):
                    print(f"↷ Skip (identisch, size): {rel}")
                    continue
            else:
                # Hash-Vergleich (SHA-256)
                lhash = pc.sha256_file(ab)
                remote_info = None
                if dest_path is not None and not args.flatten:
                    rfile = (dest_path.rstrip("/") + "/" + rel) if dest_path else rel
                    rfile = rfile.replace("//","/")
                    remote_info = pc.stat_file(cfg, path=rfile, with_checksum=True) or None
                else:
                    fid = pc.find_child_fileid(cfg, target_folderid, os.path.basename(rel))
                    if fid: remote_info = pc.stat_file(cfg, fileid=fid, with_checksum=True)
                if remote_info and remote_info.get("sha256","").lower() == lhash.lower():
                    print(f"↷ Skip (identisch, hash): {rel}")
                    continue

        # Upload
        if args.dry_run:
            print(f"[dry-run] upload: {rel} -> {target_label}")
            continue

        print(f"→ Upload: {rel}")
        try:
            top = pc.upload_chunked(cfg, ab, target_folderid,
                                    filename=os.path.basename(rel),
                                    chunk_size=args.chunk_size,
                                    progress=_progress if args.progress else None)
            md = top.get("metadata",{}) if isinstance(top, dict) else {}
            print(f"Upload OK: {os.path.basename(rel)}  ->  folderid={target_folderid}")
            if args.verify:
                # SHA-256 gegen Server prüfen (Europa)
                fid = int((md.get("fileid") or md.get("id") or 0))
                rmeta = pc.stat_file(cfg, fileid=fid, with_checksum=True)
                if rmeta and rmeta.get("sha256"):
                    lhash = pc.sha256_file(ab)
                    ok = rmeta["sha256"].lower()==lhash.lower()
                    print(f"Prüfe Checksummen (SHA-256): {'OK' if ok else 'FAILED'}")
                else:
                    print("Hinweis: Server-Checksum nicht verfügbar.")
        except Exception as e:
            print(f"[Warnung] Upload fehlgeschlagen: {rel} – {e}", file=sys.stderr)
            failed.append(ab)

    # State aktualisieren (nie im Dry-Run)
    if not args.dry_run:
        state["retry"] = failed
        if not failed:
            state["last_run"] = time.time()
        save_state(args.state_file, state)

    print(f"\nFertig. Versucht: {len(queue)}  Fehlgeschlagen: {len(failed)}")
    if failed and not args.dry_run:
        print("Fehlerhafte bleiben in retry und werden beim nächsten Lauf erneut versucht.")
        sys.exit(3)

if __name__ == "__main__":
    main()
