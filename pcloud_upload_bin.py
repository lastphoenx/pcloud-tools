# Datei: pcloud_upload_bin.py
# Beschreibung: Verschlanktes Binary-Upload-CLI für pCloud (nutzt pcloud_bin_lib)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse, os, sys, time
import pcloud_bin_lib as pc

def _progress_printer():
    """TTY-freundlicher Fortschrittsbalken."""
    start = time.time()
    last = 0.0
    def _emit(sent: int, total: int):
        nonlocal last
        now = time.time()
        if (now - last) < 0.3 and sent < total:
            return
        last = now
        elapsed = max(1e-6, now - start)
        rate = sent / elapsed
        pct = (sent / total * 100.0) if total else 0.0
        remain = max(0, total - sent)
        eta = (remain / rate) if rate > 0 else 0.0
        bar_w = 28
        filled = int((pct/100.0) * bar_w)
        bar = "#" * filled + "-" * (bar_w - filled)
        msg = f"\r[{bar}] {pct:6.2f}%  {sent}/{total}  @ {int(rate)}/s  ETA {int(eta)}s"
        try:
            sys.stderr.write(msg); sys.stderr.flush()
        except Exception:
            pass
        if sent >= total:
            try:
                sys.stderr.write("\n"); sys.stderr.flush()
            except Exception:
                pass
    return _emit

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="pCloud Binary Upload (gestreamt, mit optionalem Verify und Nearest-Serverwahl).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="""
Beispiele:
  # 1) Einfachster Upload in Root:
  %(prog)s --src /tmp/image.jpg

  # 2) Upload in Ordner per Pfad:
  %(prog)s --src /tmp/config.xml --dest-path "/Backup/binary-upload-test"

  # 3) Upload per folderid + Server-Rename bei Konflikt:
  %(prog)s --src ./foo.bin --dest-folderid 11432140592 --rename-if-exists

  # 4) Upload mit 8 MiB-Chunks + Verify + Nearest:
  %(prog)s --src ./big.iso --dest-path "/Backup/Images" --chunk-size 8388608 --verify --nearest

  # 5) Kollision nur dann hochladen, wenn Datei *nicht* identisch ist:
  %(prog)s --src ./report.pdf --dest-path "/Docs" --on-exists skip-if-identical
        """,
    )
    # .env/Profile/Overrides
    ap.add_argument("--env-file", help=".env Pfad (optional).")
    ap.add_argument("--env-dir", help="Basisverzeichnis für .env & Profile (optional).")
    ap.add_argument("--profile", help="<name>.env Profil im env-dir/Lib-Ordner/CWD (optional).")

    # Quelle/Ziel
    ap.add_argument("--src", required=True, help="Lokaler Dateipfad (eine Datei pro Aufruf).")
    dst = ap.add_mutually_exclusive_group()
    dst.add_argument("--dest-path", help="Zielordner als pCloud-Pfad (z. B. /Backup/foo).")
    dst.add_argument("--dest-folderid", type=int, help="Zielordner als folderid.")
    ap.add_argument("--filename", help="Zieldateiname (Default: wie Quelle).")

    # Verhalten bei Kollisionen (ohne Delete, passend zur aktuellen Lib)
    ap.add_argument("--on-exists",
                    choices=["abort", "skip-if-identical"],
                    default="abort",
                    help="Strategie bei vorhandener Zieldatei (ohne Löschen).")
    ap.add_argument("--rename-if-exists", action="store_true",
                    help="Serverseitig automatisch umbenennen (pCloud 'renameifexists').")

    # Upload/Verify/Nearest
    ap.add_argument("--chunk-size", type=int, default=4*1024*1024, help="Chunkgröße in Bytes.")
    ap.add_argument("--progress", action="store_true", help="Fortschritt erzwingen (auch ohne TTY).")
    ap.add_argument("--no-progress", action="store_true", help="Fortschritt unterdrücken.")
    ap.add_argument("--progresshash", help="Optionaler Fortschrittshash für Server-Seite.")
    ap.add_argument("--verify", action="store_true",
                    help="Nach Upload Checksummen vergleichen (Server checksumfile vs. lokale SHA-256/SHA-1).")
    ap.add_argument("--nearest", action="store_true",
                    help="Schnellsten Binär-Host wählen (Lib: choose_nearest_bin_host).")

    # Overrides (selten nötig – Lib zieht Defaults/.env)
    ap.add_argument("--host", help="API-Host override.")
    ap.add_argument("--port", type=int, help="Port override.")
    ap.add_argument("--timeout", type=int, help="Timeout (Sek.) override.")
    ap.add_argument("--device", help="Device-String override.")
    ap.add_argument("--token", help="Access-Token override.")
    return ap.parse_args()

def _preflight_exists(cfg: dict, *, dest_path: str|None, dest_folderid: int|None,
                      filename: str, local_path: str, mode: str) -> str|None:
    """
    Minimal-Preflight (ohne Löschen, passend zur aktuellen Lib):
      - 'abort' : raise FileExistsError, wenn gleichnamige Datei existiert
      - 'skip-if-identical' : bei identisch -> return "__SKIP__", sonst None
    Existenz/Identität wird via stat_file + ggf. checksumfile geprüft.
    """
    # Ziel-Pfad bestimmen
    if dest_path:
        target = pc._norm_remote_path(dest_path).rstrip("/") + "/" + filename
        try:
            meta = pc.stat_file(cfg, path=target, with_checksum=True)
        except RuntimeError as e:
            # 2055 = "File or folder not found" -> für Preflight bedeutet das: Datei existiert NICHT
            if " 2055" in str(e) or "File or folder not found" in str(e):
                meta = {}
            else:
                raise
    else:
        # folderid + name -> fileid suchen
        fid = pc.find_child_fileid(cfg, int(dest_folderid), filename)
        if fid:
            try:
                meta = pc.stat_file(cfg, fileid=fid, with_checksum=True)
            except RuntimeError as e:
                if " 2055" in str(e) or "File or folder not found" in str(e):
                    meta = {}
                else:
                    raise
        else:
            meta = {}
    if not meta:
        return None  # keine Kollision

    if mode == "abort":
        raise FileExistsError(f"Zieldatei existiert bereits: {filename}")

    if mode == "skip-if-identical":
        # Identität prüfen (SHA-256 bevorzugt, sonst SHA-1, sonst Größe)
        lsize = os.path.getsize(local_path)
        rsha256 = (meta.get("sha256") or "").lower()
        rsha1   = (meta.get("sha1")   or "").lower()
        identical = False
        if rsha256:
            identical = (rsha256 == pc.sha256_file(local_path).lower())
        elif rsha1:
            identical = (rsha1 == pc.sha1_file(local_path).lower())
        else:
            identical = (int(meta.get("size", -1)) == int(lsize))
        return "__SKIP__" if identical else None

    raise ValueError(f"unbekannter mode: {mode}")

def main():
    args = _parse_args()

    # Quelle prüfen
    src = os.path.abspath(args.src)
    if not os.path.isfile(src):
        print(f"Fehler: Quelle existiert nicht oder ist keine Datei: {src}", file=sys.stderr)
        sys.exit(2)

    # Effektive Config aus der Lib
    cfg = pc.effective_config(
        env_file=args.env_file,
        env_dir=args.env_dir,
        profile=args.profile,
        overrides={
            "host": args.host, "port": args.port, "timeout": args.timeout,
            "device": args.device, "token": args.token
        }
    )

    # Nearest-Serverwahl (jetzt zentral aus der Lib)
    if args.nearest:
        best = pc.choose_nearest_bin_host(cfg)
        if best != cfg["host"]:
            print(f"[Info] Nearest bin host gewählt: {best} (vorher {cfg['host']})")
            cfg["host"] = best

    # Ziel ermitteln
    dest_path = args.dest_path
    dest_folderid = args.dest_folderid
    if not dest_path and dest_folderid is None:
        # Default: Root
        dest_folderid = 0

    # Wenn rename-if-exists aktiv ist, benötigen wir den Ziel-Ordner als folderid
    # (für die clientseitige Namensfindung).
    if args.rename_if_exists:
        if dest_folderid is None:
            # Pfad -> folderid auflösen (exakter Ordner, trailing Slash egal)
            folder_md = pc.stat_folder(cfg, path=pc._norm_remote_path(dest_path).rstrip("/"))
            dest_folderid = int(folder_md["folderid"])


    # Fortschritt
    want_progress = (args.progress or (sys.stderr.isatty() and not args.no_progress))
    progress_cb = _progress_printer() if want_progress else None

    # Dateiname bestimmen
    filename = args.filename or os.path.basename(src)

    # Kollisionsbehandlung:
    # - mit --rename-if-exists -> clientseitig eindeutigen Namen wählen
    # - sonst -> klassisches Preflight (abort / skip-if-identical)
    if args.rename_if_exists:
        # verwende kleines Tag, damit sichtbar ist, dass unser Tool umbenannt hat
        filename = pc.unique_target_name(cfg, folderid=int(dest_folderid or 0),
                                         filename=(args.filename or os.path.basename(src)),
                                         tag="pupload")
    else:
        if args.on_exists in ("abort", "skip-if-identical"):
            try:
                pre = _preflight_exists(cfg,
                                        dest_path=dest_path,
                                        dest_folderid=dest_folderid,
                                        filename=filename,
                                        local_path=src,
                                        mode=args.on_exists)
                if pre == "__SKIP__":
                    print(f"↷ Skip: Ziel enthält bereits eine identische Datei – {filename}")
                    sys.exit(0)
            except FileExistsError as fe:
                print(f"Abbruch: {fe}", file=sys.stderr)
                sys.exit(3)

    # Upload starten
    try:
        top = pc.upload_streaming(
            cfg,
            src,
            dest_folderid=dest_folderid,
            dest_path=dest_path,
            filename=filename,
            rename_if_exists=args.rename_if_exists,
            chunk_size=args.chunk_size,
            progresshash=args.progresshash,
            progress_cb=progress_cb,
        )
    except Exception as e:
        if want_progress:
            try:
                sys.stderr.write("\n"); sys.stderr.flush()
            except Exception:
                pass
        print(f"Fehler beim Upload: {e}", file=sys.stderr)
        sys.exit(1)

    # Kurzausgabe
    md = top.get("metadata")
    meta = (md[0] if isinstance(md, list) and md else md) if isinstance(md, (list, dict)) else None
    if meta:
        kind = "FOLDER" if meta.get("isfolder") else "FILE"
        fid = meta.get("fileid") if not meta.get("isfolder") else meta.get("folderid")
        path = meta.get("path")
        size = meta.get("size")
        print(f"Upload OK: {kind} id={fid} size={size} path={path}")
    else:
        print("Upload OK (keine detailierte metadata im Response).")

    # Verify (optional)
    if args.verify:
        fileid = (meta.get("fileid") if meta else None)
        r_path = None
        if fileid is None:
            if dest_path:
                base = pc._norm_remote_path(dest_path)
            else:
                base = pc.resolve_full_path_for_folderid(cfg, int(dest_folderid or 0))
            r_path = base + ("" if base.endswith("/") else "/") + filename
        ok, _cs = pc.verify_remote_vs_local(cfg, fileid=fileid, path=r_path, local_path=src)
        if ok:
            print("Verify: OK (Server-Checksumme = lokale Checksumme).")
        else:
            print("Verify: FEHLER – Checksummen unterscheiden sich oder Server liefert keine Hashes.", file=sys.stderr)
            sys.exit(4)

if __name__ == "__main__":
    main()
