import time
import json
import os
import sys
import requests

# Pfad-Hacking damit pcloud_bin_lib gefunden wird
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_DIR = os.path.dirname(SCRIPT_DIR)
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc

def test_copy_content_only_strategy(source_path, target_snapshot_path):
    cfg = pc.effective_config()
    timeout = 300 
    
    print(f"🚀 Schritt 1: Zielordner vorab anlegen (ensure_path)...")
    try:
        pc.ensure_path(cfg, target_snapshot_path)
        print(f"   ✓ Ordner angelegt: {target_snapshot_path}")
    except Exception as e:
        print(f"❌ FEHLER bei ensure_path: {e}")
        return

    # Parameter exakt nach deiner richtigen Lösung:
    params = {
        "access_token": cfg["token"],
        "path": source_path,
        "topath": target_snapshot_path,
        "copycontentonly": 1,  # DAS ist der entscheidende Parameter!
        "noover": 1
    }
    
    url = f"https://{cfg['host']}/copyfolder"
    
    print(f"🚀 Schritt 2: Nur Inhalt kopieren (copycontentonly=1)...")
    print(f"   Quelle: {source_path}")
    print(f"   Ziel:   {target_snapshot_path}")
    print("-" * 50)

    start_time = time.time()
    try:
        response = requests.get(url, params=params, timeout=timeout)
        res_json = response.json()
        duration = time.time() - start_time
        
        if res_json.get("result") == 0:
            print(f"✅ ERFOLG! Inhalt wurde nach {duration:.2f}s kopiert.")
            print(f"   Der Ordner behält seinen Namen: {os.path.basename(target_snapshot_path)}")
        else:
            print(f"❌ API FEHLER {res_json.get('result')}: {res_json.get('error')}")
            
        print(f"📝 Response: {json.dumps(res_json, indent=2)}")
        
    except Exception as e:
        print(f"❌ KRITISCHER FEHLER: {e}")

if __name__ == "__main__":
    # Test-Pfade
    src = "/Backup/rtb_1to1/_snapshots/2026-04-12-163517"
    # Der neue Snapshot-Ordner den wir anlegen wollen
    dest = "/Backup/rtb_1to1/_snapshots/test_strategy_ok_" + time.strftime("%H%M%S")
    
    test_copy_content_only_strategy(src, dest)
