#!/bin/bash
# ============================================================
# Production-Grade Test für Chunked Upload Integration
# Testet echten RTB-Snapshot mit Ordnerstruktur + große Datei
# ============================================================

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="/tmp/test_chunked_rtb_$(date +%s)"
PCLOUD_TEST_ROOT="/My Cloud/TEST_CHUNKED_RTB"

echo "============================================================"
echo "RTB Chunked Upload Integration Test"
echo "============================================================"
echo ""
echo "Test-Root:   $TEST_ROOT"
echo "pCloud-Root: $PCLOUD_TEST_ROOT"
echo ""

# ============================================================
# SCHRITT 1: Test-Datenstruktur erstellen (wie echtes Backup)
# ============================================================
echo "[1/7] Erstelle Test-Datenstruktur..."
mkdir -p "$TEST_ROOT/source"
mkdir -p "$TEST_ROOT/snapshots"
mkdir -p "$TEST_ROOT/manifests"

# Ordnerstruktur (wie echtes Backup)
mkdir -p "$TEST_ROOT/source/documents"
mkdir -p "$TEST_ROOT/source/media/videos"
mkdir -p "$TEST_ROOT/source/system"

# Kleine Dateien (Standard-Upload-Path)
echo "Test Document 1" > "$TEST_ROOT/source/documents/doc1.txt"
echo "Test Document 2" > "$TEST_ROOT/source/documents/doc2.txt"
dd if=/dev/urandom of="$TEST_ROOT/source/media/photo.jpg" bs=1M count=2 2>/dev/null
dd if=/dev/urandom of="$TEST_ROOT/source/system/config.db" bs=1K count=500 2>/dev/null

# GROSSE Datei (Chunked-Upload-Path) - 6 GB
echo "[1/7] Erstelle 6 GB Test-Datei (dauert ~2 Min)..."
dd if=/dev/urandom of="$TEST_ROOT/source/media/videos/large_video.mkv" bs=1M count=6144 status=progress

# Datei-Info
echo ""
echo "Test-Dateien erstellt:"
find "$TEST_ROOT/source" -type f -exec ls -lh {} \; | awk '{print $9, $5}'

# ============================================================
# SCHRITT 2: RTB-Snapshot erstellen
# ============================================================
echo ""
echo "[2/7] Erstelle RTB-Snapshot..."
SNAPSHOT_NAME="test_snapshot_$(date +%Y-%m-%d-%H%M%S)"

# RTB ausführen (ohne rsync zu pi-nas)
cd "$SCRIPT_DIR/../rtb"
./rsync_tmbackup.sh \
  "$TEST_ROOT/source/" \
  "$TEST_ROOT/snapshots/$SNAPSHOT_NAME" \
  "$TEST_ROOT/snapshots" \
  /dev/null  # Kein Exclude-File

echo "✓ Snapshot erstellt: $TEST_ROOT/snapshots/$SNAPSHOT_NAME"

# ============================================================
# SCHRITT 3: JSON-Manifest generieren
# ============================================================
echo ""
echo "[3/7] Generiere JSON-Manifest..."

cd "$SCRIPT_DIR/../pcloud-tools"
python3 << PYEOF
import os
import json
import hashlib
import time
from pathlib import Path

snapshot_dir = Path("$TEST_ROOT/snapshots/$SNAPSHOT_NAME")
manifest_file = Path("$TEST_ROOT/manifests/${SNAPSHOT_NAME}.json")

items = []
for root, dirs, files in os.walk(snapshot_dir):
    for fname in files:
        fpath = Path(root) / fname
        relpath = fpath.relative_to(snapshot_dir)
        
        stat = fpath.stat()
        
        # SHA256 berechnen
        h = hashlib.sha256()
        with open(fpath, "rb") as f:
            for chunk in iter(lambda: f.read(1024**2), b""):
                h.update(chunk)
        
        items.append({
            "type": "file",
            "relpath": str(relpath),
            "source_path": str(fpath),
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "sha256": h.hexdigest(),
            "ext": fpath.suffix.lstrip(".")
        })
        print(f"  + {relpath} ({stat.st_size/1024**2:.1f} MB)")

manifest = {
    "format_version": 1,
    "snapshot": "$SNAPSHOT_NAME",
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "source_root": str(snapshot_dir),
    "items": items
}

manifest_file.parent.mkdir(parents=True, exist_ok=True)
with open(manifest_file, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"\\n✓ Manifest erstellt: {manifest_file}")
print(f"  Items: {len(items)}")
print(f"  Gesamt: {sum(x['size'] for x in items)/1024**3:.2f} GB")
PYEOF

# ============================================================
# SCHRITT 4: Upload zu pCloud (Standard + Chunked Mix)
# ============================================================
echo ""
echo "[4/7] Starte Upload zu pCloud..."
echo "  Erwartung:"
echo "    - Kleine Dateien (< 5 GB): Standard-Upload"
echo "    - large_video.mkv (6 GB): Chunked-Upload mit Progress-Logs"
echo ""

cd "$SCRIPT_DIR/../pcloud-tools"
python3 pcloud_push_json_manifest_to_pcloud.py \
  --manifest "$TEST_ROOT/manifests/${SNAPSHOT_NAME}.json" \
  --dest-root "$PCLOUD_TEST_ROOT" \
  --snapshot-mode 1to1

echo ""
echo "✓ Upload abgeschlossen"

# ============================================================
# SCHRITT 5: State-Files prüfen
# ============================================================
echo ""
echo "[5/7] Prüfe State-Files..."
if [ -d "/srv/pcloud-archive/resume" ]; then
    STATE_DIR="/srv/pcloud-archive/resume"
elif [ -d "$HOME/.pcloud_resume" ]; then
    STATE_DIR="$HOME/.pcloud_resume"
else
    STATE_DIR="/tmp/pcloud_resume"
fi

echo "State-Dir: $STATE_DIR"
if [ -f "$STATE_DIR"/*.state.json ]; then
    echo "State-Files:"
    ls -lh "$STATE_DIR"/*.state.json
    echo ""
    echo "Inhalt (letztes State-File):"
    cat "$(ls -t "$STATE_DIR"/*.state.json | head -1)" | python3 -m json.tool
else
    echo "⚠️  Keine State-Files gefunden (Upload unter Threshold oder abgeschlossen)"
fi

# ============================================================
# SCHRITT 6: RESUME-TEST (Haupttest!)
# ============================================================
echo ""
echo "[6/7] RESUME-TEST (kritisch!)..."
echo "  Starte neuen Upload mit gleicher großer Datei..."
echo "  Erwartung: Sollte existierende Datei erkennen und skippen"
echo "  (Oder: Lösche remote Datei für echten Resume-Test)"
echo ""

read -p "Remote-Datei löschen für Resume-Test? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    python3 << PYEOF
import pcloud_bin_lib as pc
cfg = pc.load_cfg()
try:
    pc.deletefile(cfg, path="$PCLOUD_TEST_ROOT/_snapshots/$SNAPSHOT_NAME/media/videos/large_video.mkv")
    print("✓ Remote-Datei gelöscht")
except Exception as e:
    print(f"⚠️  Fehler beim Löschen: {e}")
PYEOF
    
    echo ""
    echo "Starte Upload erneut (sollte von vorne beginnen)..."
    python3 pcloud_push_json_manifest_to_pcloud.py \
      --manifest "$TEST_ROOT/manifests/${SNAPSHOT_NAME}.json" \
      --dest-root "$PCLOUD_TEST_ROOT" \
      --snapshot-mode 1to1 &
    
    UPLOAD_PID=$!
    echo "Upload-PID: $UPLOAD_PID"
    
    # Nach 60 Sekunden killen (ca. 10% Upload)
    echo "Warte 60s, dann Interrupt..."
    sleep 60
    kill $UPLOAD_PID 2>/dev/null || true
    wait $UPLOAD_PID 2>/dev/null || true
    
    echo ""
    echo "Upload unterbrochen! State-File:"
    cat "$(ls -t "$STATE_DIR"/*.state.json | head -1)" | python3 -m json.tool
    
    echo ""
    echo "Starte Upload erneut (sollte resumen!)..."
    python3 pcloud_push_json_manifest_to_pcloud.py \
      --manifest "$TEST_ROOT/manifests/${SNAPSHOT_NAME}.json" \
      --dest-root "$PCLOUD_TEST_ROOT" \
      --snapshot-mode 1to1
    
    echo ""
    echo "✓ Resume-Test abgeschlossen"
else
    echo "Resume-Test übersprungen"
fi

# ============================================================
# SCHRITT 7: Verifikation auf pCloud
# ============================================================
echo ""
echo "[7/7] Verifiziere Upload auf pCloud..."

python3 << PYEOF
import pcloud_bin_lib as pc
import json

cfg = pc.load_cfg()

# Manifest laden
with open("$TEST_ROOT/manifests/${SNAPSHOT_NAME}.json") as f:
    manifest = json.load(f)

print(f"Prüfe {len(manifest['items'])} Dateien auf pCloud...")
print("")

errors = 0
for item in manifest['items']:
    remote_path = f"$PCLOUD_TEST_ROOT/_snapshots/$SNAPSHOT_NAME/{item['relpath']}"
    
    try:
        md = pc.stat_file(cfg, path=remote_path)
        
        # Größe prüfen
        if md['size'] != item['size']:
            print(f"✗ {item['relpath']}: Größe-Mismatch (Lokal={item['size']}, Remote={md['size']})")
            errors += 1
        
        # SHA256 prüfen (nur für große Dateien)
        if item['size'] > 5 * 1024**3:
            checksum = pc.checksumfile(cfg, fileid=md['fileid'])
            if checksum['sha256'].lower() != item['sha256'].lower():
                print(f"✗ {item['relpath']}: SHA256-Mismatch")
                errors += 1
            else:
                print(f"✓ {item['relpath']}: SHA256 verifiziert ({item['size']/1024**3:.2f} GB)")
        else:
            print(f"✓ {item['relpath']}: Größe OK ({item['size']/1024:.0f} KB)")
    
    except Exception as e:
        print(f"✗ {item['relpath']}: {e}")
        errors += 1

print("")
if errors == 0:
    print("🎉 Alle Dateien erfolgreich verifiziert!")
else:
    print(f"⚠️  {errors} Fehler gefunden")
PYEOF

# ============================================================
# CLEANUP (optional)
# ============================================================
echo ""
echo "============================================================"
echo "Test abgeschlossen!"
echo "============================================================"
echo ""
echo "Cleanup:"
echo "  Lokal:  rm -rf $TEST_ROOT"
echo "  pCloud: python3 -c 'import pcloud_bin_lib as pc; pc.deletefolder_recursive(pc.load_cfg(), path=\"$PCLOUD_TEST_ROOT\")'"
echo ""
read -p "Jetzt cleanup ausführen? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Lösche lokale Test-Dateien..."
    rm -rf "$TEST_ROOT"
    
    echo "Lösche pCloud-Test-Ordner..."
    python3 << PYEOF
import pcloud_bin_lib as pc
cfg = pc.load_cfg()
try:
    pc.deletefolder_recursive(cfg, path="$PCLOUD_TEST_ROOT")
    print("✓ pCloud-Test-Ordner gelöscht")
except Exception as e:
    print(f"⚠️  Fehler: {e}")
PYEOF
    
    echo "✓ Cleanup abgeschlossen"
else
    echo "Cleanup übersprungen (manuelle Löschung nötig)"
fi
