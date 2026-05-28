#!/usr/bin/env python3
"""
pCloud Legacy-Methoden Test (Clone & Promote)

Testet ob der Legacy-Ansatz (copyfolder + renamefile) Quota-effizienter ist 
als die reine Pool-Lösung.

Usage:
    python test_legacy_deduplication.py --env-file .env
"""

import sys
import os
import argparse
import time

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

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
    pc.ensure_path(cfg, test_dir)
    
    test_file_path = create_test_file(args.size, temp_dir=args.temp_dir)
    test_file_size = os.path.getsize(test_file_path)
    
    try:
        # 1. Quota Initial
        print("\n[2/7] Quota VOR Test:")
        q0 = get_quota(cfg)
        print(f"    Used: {format_bytes(q0['used'])}")
        
        # 2. Upload snap_1
        print(f"\n[3/7] Erstelle snap_1 (Upload {format_bytes(test_file_size)})...")
        pc.ensure_path(cfg, f"{test_dir}/snap_1")
        
        # Check if already uploaded
        try:
            pc.stat_file(cfg, path=f"{test_dir}/snap_1/data.bin")
            print(f"    ✓ snap_1/data.bin bereits vorhanden, überspringe Upload.")
        except Exception:
            print(f"    Lade {format_bytes(test_file_size)} hoch...")
            pc.upload_file(cfg, local_path=test_file_path, remote_path=f"{test_dir}/snap_1/data.bin")
        
        q1 = get_quota(cfg)
        print(f"    Used: {format_bytes(q1['used'])} (Diff: {format_bytes(q1['used']-q0['used'])})")
        
        # 3. Klon snap_1 -> snap_2 (Die Legacy-Methode)
        print("\n[4/7] Klon snap_1 -> snap_2 via copyfolder (Legacy-Sync)...")
        # Wir brauchen die FolderID von snap_1
        md = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_1"})
        fid = md["metadata"]["folderid"]
        
        # Wir nutzen topath statt name, da copyfolder kein 'name' Argument hat
        pc.copyfolder(cfg, from_folderid=fid, to_path=f"{test_dir}/snap_2")
        q2 = get_quota(cfg)
        print(f"    Used: {format_bytes(q2['used'])} (Diff zu snap_1: {format_bytes(q2['used']-q1['used'])})")
        
        # 4. Klon snap_2 -> snap_3
        print("\n[5/7] Klon snap_2 -> snap_3 via copyfolder...")
        md2 = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_2"})
        fid2 = md2["metadata"]["folderid"]
        pc.copyfolder(cfg, from_folderid=fid2, to_path=f"{test_dir}/snap_3")
        q3 = get_quota(cfg)
        print(f"    Used: {format_bytes(q3['used'])} (Diff zu snap_2: {format_bytes(q3['used']-q2['used'])})")
        
        # 5. Promotion (Move) snap_1/data.bin -> /test_legacy_experiment/promoted.bin
        print("\n[6/7] Teste Promotion (Move) snap_1/data.bin -> promoted.bin...")
        md_file = pc._rest_get(cfg, "stat", {"path": f"{test_dir}/snap_1/data.bin"})
        fileid = md_file["metadata"]["fileid"]
        
        pc._rest_get(cfg, "renamefile", {"fileid": fileid, "topath": f"{test_dir}/promoted.bin"})
        q4 = get_quota(cfg)
        print(f"    Used: {format_bytes(q4['used'])} (Diff zu snap_3: {format_bytes(q4['used']-q3['used'])})")
        
        # 6. Analyse
        print("\n[7/7] ANALYSE:")
        print("="*80)
        print(f"Upload snap_1:      {format_bytes(q1['used']-q0['used'])} (Soll: 1x File)")
        print(f"CopyFolder (1->2):  {format_bytes(q2['used']-q1['used'])} (Dedupe?)")
        print(f"CopyFolder (2->3):  {format_bytes(q3['used']-q2['used'])} (Dedupe?)")
        print(f"Promotion (Move):   {format_bytes(q4['used']-q3['used'])} (Soll: 0 B)")
        print("="*80)
        
        if (q2['used']-q1['used']) > (test_file_size * 0.9):
            print("\nERGEBNIS: Legacy-Klon (copyfolder) verbraucht VOLL Quota!")
            print("pCloud dedupliziert NICHT bei Server-seitigen Kopien.")
        else:
            print("\nERGEBNIS: Legacy-Klon ist sparsam!")
            
    finally:
        # Cleanup überspringen, um Zeit zu sparen / Ergebnisse zu prüfen
        # pc.delete_folder(cfg, path=test_dir, recursive=True)
        if os.path.exists(test_file_path): os.unlink(test_file_path)

if __name__ == "__main__":
    main()
