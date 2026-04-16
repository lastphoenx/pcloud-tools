import time
import json
import os
import sys
import requests

# Pfad-Hacking damit pcloud_bin_lib gefunden wird, auch wenn man im scripts-Ordner ist
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_DIR = os.path.dirname(SCRIPT_DIR)
if MAIN_DIR not in sys.path:
    sys.path.insert(0, MAIN_DIR)

import pcloud_bin_lib as pc

def test_copy_performance(source_path, target_parent, new_name):
    # Lade Konfiguration (Token, Host etc.)
    cfg = pc.effective_config()
    
    # Wir setzen den Timeout hier manuell extrem hoch für diesen Test
    # pCloud braucht bei 20k Dateien oft > 30s
    timeout = 300 
    
    params = {
        "access_token": cfg["token"],
        "path": source_path,
        "topath": target_parent,
        "toname": new_name,
        "noover": 0
    }
    
    url = f"https://{cfg['host']}/copyfolder"
    
    print(f"🚀 Starte Debug-Copy...")
    print(f"   Quelle:  {source_path}")
    print(f"   Ziel:    {target_parent}/{new_name}")
    print(f"   Timeout: {timeout}s")
    print("-" * 50)

    start_time = time.time()
    try:
        # Wir nutzen stream=True, um zu sehen, ob pCloud Bruchstücke sendet
        # und setzen den Timeout explizit auf den hohen Wert
        response = requests.get(url, params=params, timeout=timeout, stream=True)
        
        print(f"📡 Status-Code: {response.status_code}")
        # Zeige interessante Header (z.B. Transfer-Encoding, Server)
        interesting_headers = ["Transfer-Encoding", "Connection", "Server", "Date"]
        headers_to_show = {h: response.headers.get(h) for h in interesting_headers if h in response.headers}
        print(f"📡 Relevante Headers: {json.dumps(headers_to_show, indent=2)}")
        
        # Den gesamten Body lesen
        content = response.text
        end_time = time.time()
        
        duration = end_time - start_time
        print("-" * 50)
        print(f"✅ Antwort erhalten nach {duration:.2f} Sekunden")
        
        # Versuche als JSON zu parsen für schönere Ausgabe
        try:
            res_json = json.loads(content)
            print(f"📝 Response Body (JSON):")
            print(json.dumps(res_json, indent=2))
        except:
            print(f"📝 Response Body (Raw): {content}")
            
    except requests.exceptions.ReadTimeout:
        print(f"❌ TIMEOUT nach {time.time() - start_time:.2f}s!")
        print(f"   Hinweis: pCloud arbeitet im Hintergrund evtl. trotzdem weiter.")
    except Exception as e:
        print(f"❌ FEHLER: {e}")

if __name__ == "__main__":
    # Test-Pfade (basierend auf deinen Logs)
    # Nutze einen existierenden Snapshot als Quelle
    src = "/Backup/rtb_1to1/_snapshots/2026-04-12-163517"
    parent = "/Backup/rtb_1to1/_snapshots"
    
    # Eindeutiger Name für den Test-Snapshot
    name = "debug_copy_test_" + time.strftime("%Y%m%d_%H%M%S")
    
    test_copy_performance(src, parent, name)
