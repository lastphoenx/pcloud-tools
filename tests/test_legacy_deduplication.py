#!/usr/bin/env python3
"""
pCloud copyfolder Deduplizierungs-Test (copycontentonly-Methode)

Testet ob der Legacy-Ansatz (copyfolder + copycontentonly) Quota-effizient ist
und FileIDs erhält (echte Deduplizierung).

STRATEGIE:
1. Upload snap_1/data.bin (1 GB Test-File)
2. Erstelle snap_2 (leerer Ordner)
3. copyfolder(from=snap_1, to=snap_2, copycontentonly=True) → FileIDs bleiben!
4. Erstelle snap_3 (leerer Ordner)
5. copyfolder(from=snap_2, to=snap_3, copycontentonly=True) → FileIDs bleiben!
6. FileID-Check: snap_1, snap_2, snap_3 haben IDENTISCHE FileIDs?
7. Quota-Check: Steigt Quota nur minimal (Metadata)?

ERWARTUNG:
- FileIDs identisch → Echte Dedupe (kein physischer Speicher)
- Quota steigt minimal → Nur Metadata (Folder-Struktur)

Usage:
    python test_legacy_deduplication.py --env-file .env --size 1000
"""

import sys
import os
import argparse
import time

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pcloud_bin_lib as pc


def create_test_file(size_mb=10, temp_dir="/mnt/backup"):
    path = os.path.join(temp_dir, "pcloud_legacy_test.bin")
    print(f"[1/7] Erstelle Test-Datei ({size_mb} MB)...")
    with open(path, 'wb') as f:
        pattern = b"LEGACY_TEST_PATTERN_" * 50
        for _ in range(size_mb * 1024):
            f.write(pattern)
    return path


def get_quota(cfg):
    result = pc._rest_get(cfg, "userinfo", {})
    return {
        "total": result.get("quota", 0),
        "used": result.get("usedquota", 0),
        "free": result.get("quota", 0) - result.get("usedquota", 0)
    }


def format_bytes(b):
    if b < 1024**3:
        return f"{b / 1024 / 1024:.2f} MB"
    else:
        return f"{b / 1024 / 1024 / 1024:.2f} GB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--size", type=int, default=1000)  # 1 GB reicht für Test
    ap.add_argument("--temp-dir", default="/mnt/backup")
    args = ap.parse_args()
    
    cfg = pc.effective_config(env_file=args.env_file)
    test_dir = "/test_legacy_experiment"
    
    # CLEANUP: Lösche alte Test-Ordner (für sauberen Test!)
    print(f"\n[CLEANUP] Lösche alte Test-Daten in {test_dir}...")
    try:
        pc.delete_folder(cfg, path=test_dir, recursive=True)
        print(f"    ✓ Alte Daten gelöscht")
    except Exception as e:
        if "not found" not in str(e).lower():
            print(f"    ! Cleanup fehlgeschlagen: {e}")
        else:
            print(f"    ✓ Keine alten Daten vorhanden")
    
    # Test-Ordner neu erstellen
    pc.ensure_path(cfg, test_dir)
    
    test_file_path = create_test_file(args.size, temp_dir=args.temp_dir)
    test_file_size = os.path.getsize(test_file_path)
    
    try:
        # 1. Quota Initial
        print("\n[2/7] Quota VOR Test:")
        q0 = get_quota(cfg)
        print(f"    Used: {format_bytes(q0['used'])}")
        
        # 2. Upload snap_1 (IMMER neu hochladen für sauberen Test!)
        print(f"\n[3/7] Erstelle snap_1 (Upload {format_bytes(test_file_size)})...")
        pc.ensure_path(cfg, f"{test_dir}/snap_1")
        
        print(f"    Lade {format_bytes(test_file_size)} hoch...")
        pc.upload_file(cfg, local_path=test_file_path, remote_path=f"{test_dir}/snap_1/data.bin")
        
        q1 = get_quota(cfg)
        print(f"    Used: {format_bytes(q1['used'])} (Diff: {format_bytes(q1['used']-q0['used'])})")
        
        # 3. Klon snap_1 -> snap_2 (Die Legacy-Methode mit copycontentonly!)
        print("\n[4/7] Klon snap_1 -> snap_2 via copyfolder (Legacy-Sync)...")
        
        # WICHTIG: snap_2 VORHER erstellen (Ziel-Container!)
        pc.ensure_path(cfg, f"{test_dir}/snap_2")
        md2_parent = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_2"})
        fid2 = md2_parent["metadata"]["folderid"]
        
        # copyfolder mit copycontentonly=True → FileIDs bleiben erhalten!
        # KRITISCH: from_path statt from_folderid (wie im Delta-Mode L2947!)
        resp2 = pc.copyfolder(cfg, 
                              from_path=f"{test_dir}/snap_1",  # ← PATH statt FolderID!
                              to_folderid=fid2, 
                              copycontentonly=True)
        
        print(f"    ✓ snap_2 erstellt (Inhalt kopiert, FileIDs erhalten)")
        print(f"    snap_2 FolderID: {fid2}")
        print(f"    copyfolder result: {resp2.get('result', 'N/A')}")
        q2 = get_quota(cfg)
        print(f"    Used: {format_bytes(q2['used'])} (Diff zu snap_1: {format_bytes(q2['used']-q1['used'])})")
        
        # 4. Klon snap_2 -> snap_3 (ebenfalls mit copycontentonly!)
        print("\n[5/7] Klon snap_2 -> snap_3 via copyfolder...")
        
        # snap_3 vorher erstellen
        pc.ensure_path(cfg, f"{test_dir}/snap_3")
        md3_parent = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_3"})
        fid3 = md3_parent["metadata"]["folderid"]
        
        # Inhalt von snap_2 nach snap_3 kopieren (from_path!)
        resp3 = pc.copyfolder(cfg, 
                              from_path=f"{test_dir}/snap_2",  # ← PATH statt FolderID!
                              to_folderid=fid3, 
                              copycontentonly=True)
        
        print(f"    ✓ snap_3 erstellt (Inhalt kopiert, FileIDs erhalten)")
        print(f"    snap_3 FolderID: {fid3}")
        print(f"    copyfolder result: {resp3.get('result', 'N/A')}")
        q3 = get_quota(cfg)
        print(f"    Used: {format_bytes(q3['used'])} (Diff zu snap_2: {format_bytes(q3['used']-q2['used'])})")
        
        # 5. FileID-Check (Beweis für Dedupe!)
        print("\n[6/7] FileID-Check (Beweis für copycontentonly-Dedupe)...")
        snap1_file = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_1/data.bin"})
        snap2_file = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_2/data.bin"})
        snap3_file = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_3/data.bin"})
        
        fid_snap1 = snap1_file["metadata"]["fileid"]
        fid_snap2 = snap2_file["metadata"]["fileid"]
        fid_snap3 = snap3_file["metadata"]["fileid"]
        
        print(f"    snap_1/data.bin FileID: {fid_snap1}")
        print(f"    snap_2/data.bin FileID: {fid_snap2}")
        print(f"    snap_3/data.bin FileID: {fid_snap3}")
        
        if fid_snap1 == fid_snap2 == fid_snap3:
            print(f"    ✓✓✓ ALLE FileIDs IDENTISCH → copycontentonly dedupliziert! ✓✓✓")
        else:
            print(f"    ✗ FileIDs UNTERSCHIEDLICH → copycontentonly dedupliziert NICHT!")
        
        # 6. Promotion (Move) snap_2/data.bin -> /test_legacy_experiment/promoted.bin
        print("\n[7/7] Teste Promotion (Move) snap_2/data.bin -> promoted.bin...")
        md_file = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_2/data.bin"})
        fileid = md_file["metadata"]["fileid"]
        
        pc._rest_get(cfg, "renamefile", {"fileid": fileid, "topath": f"{test_dir}/promoted.bin"})
        q4 = get_quota(cfg)
        print(f"    Used: {format_bytes(q4['used'])} (Diff zu snap_3: {format_bytes(q4['used']-q3['used'])})")
        
        # 7. Finale Analyse
        print("\nFINALE ANALYSE:")
        print("="*80)
        print(f"Upload snap_1:           {format_bytes(q1['used']-q0['used'])} (Soll: 1x File)")
        print(f"CopyFolder (1->2):       {format_bytes(q2['used']-q1['used'])} (copycontentonly=True)")
        print(f"CopyFolder (2->3):       {format_bytes(q3['used']-q2['used'])} (copycontentonly=True)")
        print(f"Promotion (Move):        {format_bytes(q4['used']-q3['used'])} (Soll: 0 B)")
        print()
        print(f"FileID snap_1/data.bin:  {fid_snap1}")
        print(f"FileID snap_2/data.bin:  {fid_snap2}")
        print(f"FileID snap_3/data.bin:  {fid_snap3}")
        print(f"FileIDs identisch:       {fid_snap1 == fid_snap2 == fid_snap3}")
        print("="*80)
        
        # Bewertung
        fileids_match = (fid_snap1 == fid_snap2 == fid_snap3)
        quota_increase_minimal = (q2['used']-q1['used']) < (test_file_size * 0.1)  # < 10% Quota-Anstieg
        
        if fileids_match and quota_increase_minimal:
            print("\n✓✓✓ ERGEBNIS: copycontentonly DEDUPLIZIERT PERFEKT! ✓✓✓")
            print()
            print("- FileIDs bleiben IDENTISCH (keine Duplikate)")
            print("- Quota steigt MINIMAL (nur Metadata)")
            print("→ Legacy-Methode (copyfolder + copycontentonly) ist QUOTA-SAFE!")
            print("→ Perfekt für Delta-Snapshots!")
        elif fileids_match:
            print("\n✓ ERGEBNIS: FileIDs bleiben, aber Quota steigt")
            print()
            print("- FileIDs identisch (gut!)")
            print(f"- Quota-Anstieg: {format_bytes(q2['used']-q1['used'])} (Metadata-Overhead?)")
        else:
            print("\n✗ ERGEBNIS: copycontentonly dedupliziert NICHT wie erwartet!")
            print()
            print("- FileIDs UNTERSCHIEDLICH (neue Kopien!)")
            print("- pCloud verhält sich anders als dokumentiert")
            
    finally:
        # Cleanup überspringen, um Zeit zu sparen / Ergebnisse zu prüfen
        # pc.delete_folder(cfg, path=test_dir, recursive=True)
        if os.path.exists(test_file_path): os.unlink(test_file_path)

if __name__ == "__main__":
    main()
