#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test-Skript: Misst die Performance von pCloud listfolder(recursive=True, nofiles=True).
Hilft bei der Entscheidung, ob wir den Plan-Schritt (Ordner-Anlage) optimieren.
"""

import os
import sys
import json
import time
import argparse

# Pfad zu pcloud_bin_lib sicherstellen
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    import pcloud_bin_lib as pc
except ImportError:
    print("Fehler: pcloud_bin_lib.py nicht gefunden.")
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="Remote-Pfad, der rekursiv gelistet werden soll")
    parser.add_argument("--env-file", help="Pfad zur .env Datei")
    args = parser.parse_args()

    # Config laden
    cfg = pc.effective_config(env_file=args.env_file)
    
    print(f"--- Starte rekursiven List-Test ---")
    print(f"Pfad: {args.path}")
    
    start_time = time.time()
    
    try:
        # Rekursiver Aufruf: recursive=True, nofiles=True
        # pCloud API: listfolder gibt 'metadata' mit 'contents' zurück
        res = pc.listfolder(cfg, path=args.path, recursive=True, nofiles=True, showpath=True)
        
        end_time = time.time()
        elapsed = end_time - start_time
        
        metadata = res.get("metadata", {})
        contents = metadata.get("contents", [])
        
        print(f"Status: Erfolg")
        print(f"Dauer: {elapsed:.3f} Sekunden")
        
        # Rekursive Zählung der Ordner
        def count_folders(item_list):
            count = 0
            for item in item_list:
                if item.get("isfolder"):
                    count += 1
                    sub_contents = item.get("contents", [])
                    count += count_folders(sub_contents)
            return count

        total_folders = count_folders(contents)
        print(f"Gefundene Ordner: {total_folders}")
        
        # Struktur-Beispiel speichern
        out_file = "recursive_list_sample.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        
        print(f"Vollständiges Ergebnis gespeichert in: {out_file}")
        
    except Exception as e:
        print(f"Fehler beim API-Call: {e}")

if __name__ == "__main__":
    main()
