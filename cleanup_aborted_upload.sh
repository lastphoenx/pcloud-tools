#!/bin/bash
# cleanup_aborted_upload.sh
# Räumt einen abgebrochenen pCloud-Upload auf und bereitet Neustart vor

set -e

SNAPSHOT_NAME="${1:-2026-04-12-141849}"
RTB_BASE="/mnt/backup/rtb_nas"
ARCHIVE_BASE="/srv/pcloud-archive"
PCLOUD_DEST="/Backup/rtb_1to1/_snapshots"

echo "═══════════════════════════════════════════════════════════"
echo "Cleanup für abgebrochenen Upload: $SNAPSHOT_NAME"
echo "═══════════════════════════════════════════════════════════"

# 1. RTB-Snapshot löschen (lokal)
echo "[1/6] Lösche RTB-Snapshot: $RTB_BASE/$SNAPSHOT_NAME"
if [ -d "$RTB_BASE/$SNAPSHOT_NAME" ]; then
    rm -rf "$RTB_BASE/$SNAPSHOT_NAME"
    echo "  ✓ Gelöscht"
else
    echo "  ○ Bereits gelöscht"
fi

# 2. Latest-Symlink zurücksetzen
echo "[2/6] Setze Latest-Symlink zurück"
PREVIOUS_SNAPSHOT=$(ls -t "$RTB_BASE" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}$' | head -1)
if [ -n "$PREVIOUS_SNAPSHOT" ]; then
    rm -f "$RTB_BASE/latest"
    ln -s "$RTB_BASE/$PREVIOUS_SNAPSHOT" "$RTB_BASE/latest"
    echo "  ✓ Latest → $PREVIOUS_SNAPSHOT"
else
    echo "  ⚠ Kein vorheriger Snapshot gefunden!"
fi

# 3. Manifest löschen (falls vorhanden)
echo "[3/6] Lösche Manifest: $ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json"
if [ -f "$ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json" ]; then
    rm -f "$ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json"
    echo "  ✓ Gelöscht"
else
    echo "  ○ Kein Manifest gefunden"
fi

# 4. Lokaler Index-Cache löschen
echo "[4/6] Lösche lokalen Index-Cache"
INDEX_CACHE="/tmp/pcloud_index_${SNAPSHOT_NAME}.json"
if [ -f "$INDEX_CACHE" ]; then
    rm -f "$INDEX_CACHE"
    echo "  ✓ Gelöscht: $INDEX_CACHE"
else
    echo "  ○ Kein Index-Cache gefunden"
fi

# 5. pCloud-Snapshot löschen (remote)
echo "[5/6] Lösche pCloud-Snapshot (wenn vorhanden)"
python3 -c "
import pcloud_bin_lib as pc
cfg = pc.effective_config()
try:
    pc.delete_folder(cfg, path='$PCLOUD_DEST/$SNAPSHOT_NAME', recursive=True)
    print('  ✓ pCloud-Snapshot gelöscht')
except Exception as e:
    if '2005' in str(e) or 'not found' in str(e).lower():
        print('  ○ pCloud-Snapshot bereits gelöscht')
    else:
        print(f'  ⚠ Fehler: {e}')
"

# 6. Verify
echo "[6/6] Verify Cleanup"
echo "  RTB-Snapshots:"
ls -lh "$RTB_BASE" | grep -E '(latest|[0-9]{4}-[0-9]{2}-[0-9]{2})' | tail -5
echo ""
echo "  Manifests:"
ls -lh "$ARCHIVE_BASE/manifests/" 2>/dev/null | tail -3 || echo "  (Ordner noch nicht vorhanden)"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✓ Cleanup abgeschlossen!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Nächste Schritte:"
echo "  1. git pull origin main  # Neue Features (Timestamps + Retry)"
echo "  2. sudo bash /opt/apps/rtb/rtb_wrapper.sh  # Neues Backup starten"
echo ""
