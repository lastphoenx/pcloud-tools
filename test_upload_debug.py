#!/usr/bin/env python3
"""
Test-Script für pCloud Upload Debugging
Erstellt kleine Testdateien und uploadet sie mit verschiedenen Methoden.
"""
import os
import sys
import argparse
import tempfile
from pathlib import Path

# pcloud_bin_lib importieren
sys.path.insert(0, str(Path(__file__).parent))
import pcloud_bin_lib as pc

def create_test_file(size_mb: int, name: str) -> str:
    """Erstellt eine Testdatei mit random Daten."""
    filepath = os.path.join(tempfile.gettempdir(), name)
    print(f"Erstelle Testdatei: {filepath} ({size_mb} MB)")
    
    with open(filepath, 'wb') as f:
        # 1MB Blöcke schreiben
        for i in range(size_mb):
            f.write(os.urandom(1024 * 1024))
    
    print(f"✓ Testdatei erstellt: {os.path.getsize(filepath)} bytes")
    return filepath

def test_upload(cfg: dict, local_file: str, remote_path: str, method: str):
    """Testet einen Upload mit Logging."""
    print(f"\n{'='*60}")
    print(f"TEST: {method}")
    print(f"Local: {local_file} ({os.path.getsize(local_file) / (1024**2):.2f} MB)")
    print(f"Remote: {remote_path}")
    print(f"{'='*60}")
    
    try:
        if method == "normal_uploadfile":
            # Standard uploadfile (upload_file intern entscheidet)
            result = pc.upload_file(cfg, local_path=local_file, remote_path=remote_path)
            print(f"✓ ERFOLG (uploadfile): {result}")
            return True
            
        elif method == "force_chunked":
            # Erzwinge chunked upload (nutze upload_file mit kleinem Threshold)
            # WICHTIG: Kein lokales 'import os' - nutze das globale!
            old_threshold = os.environ.get("PCLOUD_CHUNK_THRESHOLD")
            os.environ["PCLOUD_CHUNK_THRESHOLD"] = "1"  # 1 Byte → immer chunked
            result = pc.upload_file(cfg, local_path=local_file, remote_path=remote_path)
            if old_threshold:
                os.environ["PCLOUD_CHUNK_THRESHOLD"] = old_threshold
            else:
                del os.environ["PCLOUD_CHUNK_THRESHOLD"]
            print(f"✓ ERFOLG (chunked): {result}")
            return True
            
        elif method == "direct_uploadfile_api":
            # Direkter uploadfile API-Call (kein Chunking)
            import requests
            session = pc._get_session()
            base_url = pc._rest_base(cfg)
            
            parent = pc._norm_remote_path(os.path.dirname(remote_path) or "/")
            fname = os.path.basename(remote_path)
            
            params = {
                "access_token": cfg["token"],
                "path": parent,
                "filename": fname,
            }
            
            with open(local_file, 'rb') as f:
                files = {'file': (fname, f)}
                r = session.post(f"{base_url}/uploadfile", params=params, files=files, timeout=(60, 300))
            
            j = r.json()
            if j.get("result") != 0:
                raise RuntimeError(f"uploadfile failed: {j}")
            
            print(f"✓ ERFOLG (direct uploadfile): {j}")
            return True
            
    except Exception as e:
        print(f"✗ FEHLER: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(description="pCloud Upload Debug Test")
    parser.add_argument("--env-file", help="Pfad zur .env Datei (optional)")
    args = parser.parse_args()
    
    print("pCloud Upload Debug Test\n")
    
    # Config laden (mit optionalem --env-file)
    cfg = pc.effective_config(env_file=args.env_file)
    print(f"✓ Config geladen (Host: {cfg.get('host', 'N/A')})")
    
    # Debug: Token-Check
    token = cfg.get('token', '')
    if not token:
        print(f"✗ FEHLER: Kein Token in Config!")
        print(f"  env-file Argument: {args.env_file or 'nicht gesetzt (nutzt defaults)'}")
        sys.exit(1)
    print(f"✓ Token geladen (Länge: {len(token)} Zeichen)")
    
    # Userinfo check
    try:
        info = pc.userinfo(cfg)
        print(f"✓ pCloud Login OK")
    except Exception as e:
        print(f"✗ FEHLER bei userinfo: {e}")
        sys.exit(1)
    
    # Test-Ordner vorbereiten
    test_folder = "/pcloud_upload_test"
    print(f"\nBereite Test-Ordner vor: {test_folder}")
    
    # Prüfe ob existiert und ob Datei oder Ordner
    try:
        md = pc.stat(cfg, path=test_folder)
        if not md.get('isfolder'):
            # Ist eine Datei → löschen
            print(f"  Lösche existierende Datei (fileid: {md.get('fileid')})")
            pc.delete_file(cfg, fileid=md.get('fileid'))
    except:
        pass  # Existiert nicht, ok
    
    # Jetzt Ordner erstellen
    try:
        pc.ensure_path(cfg, test_folder)
        print(f"✓ Ordner bereit")
    except Exception as e:
        print(f"✗ Ordner-Fehler: {e}")
        sys.exit(1)
    
    # Tests durchführen
    tests_passed = 0
    tests_total = 0
    
    # Test 1: Kleine Datei (2MB) - normal uploadfile
    print("\n" + "="*60)
    print("TEST 1: Kleine Datei (2MB) mit normalem Upload")
    print("="*60)
    f1 = create_test_file(2, "test_2mb.bin")
    tests_total += 1
    if test_upload(cfg, f1, f"{test_folder}/test_2mb.bin", "normal_uploadfile"):
        tests_passed += 1
    
    # Test 2: Kleine Datei (2MB) - direct uploadfile API
    print("\n" + "="*60)
    print("TEST 2: Kleine Datei (2MB) mit direktem uploadfile API")
    print("="*60)
    tests_total += 1
    if test_upload(cfg, f1, f"{test_folder}/test_2mb_direct.bin", "direct_uploadfile_api"):
        tests_passed += 1
    
    # Test 3: Mittlere Datei (15MB) - erzwinge chunked
    print("\n" + "="*60)
    print("TEST 3: Mittlere Datei (15MB) mit erzwungenem Chunked Upload")
    print("="*60)
    f2 = create_test_file(15, "test_15mb.bin")
    tests_total += 1
    if test_upload(cfg, f2, f"{test_folder}/test_15mb_chunked.bin", "force_chunked"):
        tests_passed += 1
    
    # Test 4: Große Datei (120MB) - auto chunked
    print("\n" + "="*60)
    print("TEST 4: Große Datei (120MB) mit Auto-Chunked Upload")
    print("="*60)
    f3 = create_test_file(120, "test_120mb.bin")
    tests_total += 1
    if test_upload(cfg, f3, f"{test_folder}/test_120mb.bin", "normal_uploadfile"):
        tests_passed += 1
    
    # Cleanup
    print("\n" + "="*60)
    print("CLEANUP")
    print("="*60)
    for f in [f1, f2, f3]:
        try:
            os.remove(f)
            print(f"✓ Gelöscht: {f}")
        except:
            pass
    
    # Zusammenfassung
    print("\n" + "="*60)
    print("ZUSAMMENFASSUNG")
    print("="*60)
    print(f"Tests bestanden: {tests_passed}/{tests_total}")
    
    if tests_passed == tests_total:
        print("✓ ALLE TESTS ERFOLGREICH!")
        return 0
    else:
        print(f"✗ {tests_total - tests_passed} Test(s) gescheitert")
        return 1

if __name__ == "__main__":
    sys.exit(main())
