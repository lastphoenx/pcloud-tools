#!/usr/bin/env python3
"""
pCloud copyfile Deduplizierungs-Test

Testet ob pCloud's NATIVE Content-Deduplication (Storage-Level) funktioniert.

HYPOTHESIS: pCloud nutzt Content-Addressable Storage (wie Git!)
→ Identischer Content wird NUR EINMAL physisch gespeichert
→ Quota steigt nur um Metadata (~4-5 KB pro File), NICHT um volle File-Größe!

Test-Design:
1. Upload original.bin (1000 MB, repetitives Pattern)
2. copyfile() 5× → copy_0.bin bis copy_4.bin
3. Alle haben IDENTISCHEN Content (SHA256)

Erwartung bei Content-Dedup:
- Quota-Increase: ~30 KB (nur Metadata für 6 Files)
- ✓ Pool-Modell MACHBAR!

Erwartung OHNE Content-Dedup:
- Quota-Increase: ~6 GB (6× 1000 MB)
- ✗ Pool-Modell zu teuer!

Usage:
    python test_copyfile_deduplication.py --env-file .env
    python test_copyfile_deduplication.py --env-file .env --size 10000  # 10 GB Test
"""

import sys
import os
import argparse
import tempfile

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

import pcloud_bin_lib as pc


def create_test_file(size_mb=10, temp_dir="/mnt/backup"):
    """Erstellt Test-Datei mit zufälligem Content."""
    print(f"[1/6] Erstelle Test-Datei ({size_mb} MB)...")
    print(f"    Temp-Directory: {temp_dir}")
    
    # Test file path in temp_dir (NOT /tmp - might be too small!)
    path = os.path.join(temp_dir, "pcloud_dedup_test.bin")
    
    with open(path, 'wb') as f:
        # Write random bytes (so it's compressible/dedup-testable)
        pattern = b"DEDUP_TEST_PATTERN_" * 50  # 1 KB pattern
        for _ in range(size_mb * 1024):  # size_mb × 1 MB
            f.write(pattern)
    
    size = os.path.getsize(path)
    print(f"    Test-Datei erstellt: {path}")
    print(f"    Größe: {size:,} Bytes ({size / 1024 / 1024:.2f} MB)")
    return path


def get_quota(cfg):
    """Holt aktuelle Quota-Informationen."""
    # REST API: userinfo endpoint
    result = pc._rest_get(cfg, "userinfo", {})
    if int(result.get("result", -1)) != 0:
        raise RuntimeError(f"userinfo failed: {result}")
    
    quota = result.get("quota", 0)
    usage = result.get("usedquota", 0)
    return {
        "total": quota,
        "used": usage,
        "free": quota - usage
    }


def format_bytes(b):
    """Formatiert Bytes human-readable."""
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.2f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / 1024 / 1024:.2f} MB"
    else:
        return f"{b / 1024 / 1024 / 1024:.2f} GB"


def main():
    ap = argparse.ArgumentParser(description="Test pCloud copyfile Deduplizierung")
    ap.add_argument("--env-file", default=".env", help="Path to .env file")
    ap.add_argument("--size", type=int, default=5000, help="Test-File Größe in MB (default: 5000)")
    ap.add_argument("--copies", type=int, default=5, help="Anzahl Kopien (default: 5)")
    ap.add_argument("--temp-dir", default="/mnt/backup", help="Temp directory for test file (default: /mnt/backup)")
    args = ap.parse_args()
    
    # Load config
    cfg = pc.effective_config(env_file=args.env_file)
    
    print("=" * 80)
    print("pCloud copyfile Deduplizierungs-Test")
    print("=" * 80)
    print()
    
    # Step 1: Create test file
    test_file_path = create_test_file(args.size, temp_dir=args.temp_dir)
    test_file_size = os.path.getsize(test_file_path)
    
    try:
        # Step 2: Check quota BEFORE
        print()
        print("[2/6] Quota VOR dem Test:")
        quota_before = get_quota(cfg)
        print(f"    Gesamt: {format_bytes(quota_before['total'])}")
        print(f"    Belegt: {format_bytes(quota_before['used'])}")
        print(f"    Frei:   {format_bytes(quota_before['free'])}")
        
        # Step 3: Upload original
        print()
        print("[3/6] Upload Original-Datei...")
        print(f"    Größe: {format_bytes(test_file_size)}")
        
        # Geschätzte Upload-Dauer (basierend auf typischen pCloud Upload-Speeds)
        # Annahme: 10-50 Mbit/s Upload-Geschwindigkeit
        est_minutes_low = (test_file_size * 8) / (10 * 1024 * 1024) / 60  # Bei 10 Mbit/s
        est_minutes_high = (test_file_size * 8) / (50 * 1024 * 1024) / 60  # Bei 50 Mbit/s
        print(f"    Geschätzte Dauer: {est_minutes_low:.0f}-{est_minutes_high:.0f} Minuten")
        print(f"    (Chunked Upload: 5 MB Chunks)")
        print()
        
        test_dir = "/test_dedup_experiment"
        
        # CLEANUP: Lösche alte Test-Daten (für sauberen Test!)
        print(f"[CLEANUP] Lösche alte Test-Daten in {test_dir}...")
        try:
            pc.delete_folder(cfg, path=test_dir, recursive=True)
            print(f"    ✓ Alte Daten gelöscht")
        except Exception as e:
            if "not found" not in str(e).lower():
                print(f"    ! Cleanup fehlgeschlagen: {e}")
            else:
                print(f"    ✓ Keine alten Daten vorhanden")
        
        # Test-Ordner neu erstellen
        print()
        print("    Upload läuft... (bitte warten, kein Fortschrittsbalken)")
        pc.ensure_path(cfg, test_dir)
        original_path = f"{test_dir}/original.bin"
        
        import time
        start_upload = time.time()
        
        # Upload via upload_file function
        pc.upload_file(cfg, local_path=test_file_path, remote_path=original_path)
        
        upload_duration = time.time() - start_upload
        upload_speed_mbps = (test_file_size * 8) / upload_duration / (1024 * 1024)
        
        print()
        print(f"    ✓ Upload abgeschlossen!")
        print(f"    Dauer: {upload_duration:.1f}s ({upload_duration/60:.1f} Minuten)")
        print(f"    Speed: {upload_speed_mbps:.1f} Mbit/s")
        print(f"    Pfad: {original_path}")
        
        # Get fileid
        stat_result = pc._rest_get(cfg, "stat", {"path": original_path})
        original_fid = stat_result["metadata"]["fileid"]
        print(f"    FileID: {original_fid}")
        
        # Step 4: copyfile N times
        print()
        print(f"[4/6] Erstelle {args.copies}× Kopien via copyfile...")
        for i in range(args.copies):
            copy_path = f"{test_dir}/copy_{i}.bin"
            pc.copyfile(cfg, from_fileid=original_fid, to_path=copy_path)
            print(f"    ✓ Kopie {i+1}/{args.copies}: {copy_path}")
        
        # Step 5: Check quota AFTER
        print()
        print("[5/6] Quota NACH dem Test:")
        quota_after = get_quota(cfg)
        print(f"    Gesamt: {format_bytes(quota_after['total'])}")
        print(f"    Belegt: {format_bytes(quota_after['used'])}")
        print(f"    Frei:   {format_bytes(quota_after['free'])}")
        
        # Step 6: Analysis
        print()
        print("[6/6] ANALYSE:")
        print("=" * 80)
        
        diff = quota_after['used'] - quota_before['used']
        expected_no_dedup = test_file_size * (args.copies + 1)  # Original + N Kopien
        expected_with_dedup = test_file_size  # Nur Original zählt
        
        print(f"Quota-Differenz:        {format_bytes(diff)}")
        print(f"Test-File Größe:        {format_bytes(test_file_size)}")
        print(f"Hochgeladen (nominal):  1× Original + {args.copies}× Kopien = {args.copies + 1} Files")
        print()
        print(f"Erwartung OHNE Dedup:   {format_bytes(expected_no_dedup)} ({args.copies + 1}× File-Größe)")
        print(f"Erwartung MIT Dedup:    {format_bytes(expected_with_dedup)} (1× File-Größe)")
        print()
        
        # Decision
        dedup_factor = diff / test_file_size if test_file_size > 0 else 0
        print(f"Gemessener Faktor:      {dedup_factor:.2f}×")
        print()
        
        if dedup_factor < 1.5:  # Weniger als 1.5× (mit Toleranz für Metadata)
            print("✓✓✓ ERGEBNIS: pCloud CONTENT-DEDUPLICATION FUNKTIONIERT! ✓✓✓")
            print()
            print("pCloud speichert identischen Content NUR EINMAL (Content-Addressable Storage)!")
            print("→ Pool-Modell ist MACHBAR ohne Quota-Explosion!")
            print("→ copyfile() nutzt pCloud's native Deduplication!")
            print("→ Empfehlung: Pool-Modell implementieren mit copyfile() statt Stubs!")
            result_code = 0
        elif dedup_factor > (args.copies * 0.8):  # Mehr als 80% der Kopien zählen voll
            print("✗✗✗ ERGEBNIS: KEINE CONTENT-DEDUPLICATION! ✗✗✗")
            print()
            print("pCloud speichert jeden copyfile() als VOLL-Kopie!")
            print("→ Pool-Modell mit copyfile NICHT sinnvoll (Quota-Explosion)!")
            print("→ Empfehlung: Stub-basierter Pool-Mode (aktuelle Implementierung)!")
            result_code = 1
        else:
            print("??? ERGEBNIS: UNKLAR ???")
            print()
            print("Teildeduplizierung oder Caching-Effekte?")
            print("→ Weitere Tests mit größeren Files nötig!")
            print("→ Vorsicht geboten bei Pool-Modell!")
            result_code = 2
        
        print("=" * 80)
        
        # Cleanup
        print()
        print("Cleanup: Lösche Test-Ordner...")
        try:
            pc.delete_folder(cfg, path=test_dir, recursive=True)
            print("    ✓ Test-Ordner gelöscht")
        except Exception as e:
            print(f"    ! Cleanup fehlgeschlagen: {e}")
            print(f"    Bitte manuell löschen: {test_dir}")
        
        return result_code
        
    finally:
        # Delete local test file
        if os.path.exists(test_file_path):
            os.unlink(test_file_path)
            print(f"    ✓ Lokale Test-Datei gelöscht")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nTest abgebrochen!")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nFEHLER: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
