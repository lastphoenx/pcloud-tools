#!/bin/bash
# prepare_fresh_test.sh — Bereitet Clean-State für Workflow-Test vor
#
# Löscht fehlerhafte Snapshots, leert Archive, setzt Test-Snapshot
# Für kompletten Workflow-Test nach massivem Code-Umbau

set -e  # Bei Fehler abbrechen

echo "════════════════════════════════════════════════════════════════"
echo "🧹 pCloud-Tools: Fresh Test Preparation"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Aktion: Clean-State für Workflow-Test"
echo "  - Lösche fehlerhafte Snapshots (2026-04-17-235901, 2026-04-18-004404)"
echo "  - Setze latest → 2026-04-10-075334"
echo "  - Leere Archive (manifests + indexes)"
echo "  - Cleanup Temp-Files"
echo ""
read -p "▶ Fortfahren? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Abgebrochen."
    exit 1
fi

# === Konfiguration ===
RTB_BASE="/mnt/backup/rtb_nas"
ARCHIVE_BASE="/srv/pcloud-archive"
KEEP_SNAPSHOT="2026-04-10-075334"
DELETE_SNAPSHOTS=("2026-04-17-235901" "2026-04-18-004404")

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[1/5] RTB-Snapshots aufräumen"
echo "════════════════════════════════════════════════════════════════"

# Aktuelle Snapshots anzeigen
echo "📋 Aktueller Stand:"
ls -lh "$RTB_BASE" | grep -E '^d|^l' || true
echo ""

# Fehlerhafte Snapshots löschen
for snapshot in "${DELETE_SNAPSHOTS[@]}"; do
    if [ -d "$RTB_BASE/$snapshot" ]; then
        echo "🗑️  Lösche: $snapshot"
        rm -rf "$RTB_BASE/$snapshot"
        echo "   ✓ Gelöscht"
    else
        echo "○  $snapshot bereits gelöscht"
    fi
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[2/5] Latest-Symlink setzen"
echo "════════════════════════════════════════════════════════════════"

# Prüfe ob Test-Snapshot existiert
if [ ! -d "$RTB_BASE/$KEEP_SNAPSHOT" ]; then
    echo "❌ FEHLER: Test-Snapshot nicht gefunden: $KEEP_SNAPSHOT"
    exit 1
fi

# Setze latest-Symlink
rm -f "$RTB_BASE/latest"
ln -s "$KEEP_SNAPSHOT" "$RTB_BASE/latest"
echo "🔗 Latest → $KEEP_SNAPSHOT"

# Kontrolle
LATEST_TARGET=$(readlink "$RTB_BASE/latest")
echo "   Kontrolle: latest zeigt auf $LATEST_TARGET"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[3/5] Archive leeren (manifests + indexes)"
echo "════════════════════════════════════════════════════════════════"

# Manifests leeren
MANIFEST_COUNT=$(ls -1 "$ARCHIVE_BASE/manifests/"*.json 2>/dev/null | wc -l)
if [ "$MANIFEST_COUNT" -gt 0 ]; then
    echo "🗑️  Lösche $MANIFEST_COUNT Manifests"
    rm -f "$ARCHIVE_BASE/manifests/"*.json
    echo "   ✓ Manifests gelöscht"
else
    echo "○  Manifests bereits leer"
fi

# Indexes leeren
INDEX_COUNT=$(ls -1 "$ARCHIVE_BASE/indexes/"*.json 2>/dev/null | wc -l)
if [ "$INDEX_COUNT" -gt 0 ]; then
    echo "🗑️  Lösche $INDEX_COUNT Indexes"
    rm -f "$ARCHIVE_BASE/indexes/"*.json
    echo "   ✓ Indexes gelöscht"
else
    echo "○  Indexes bereits leer"
fi

# Kontrolle
echo "   Kontrolle:"
echo "     manifests/: $(ls -1 "$ARCHIVE_BASE/manifests/" 2>/dev/null | wc -l) Dateien"
echo "     indexes/:   $(ls -1 "$ARCHIVE_BASE/indexes/" 2>/dev/null | wc -l) Dateien"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[4/5] Temp-Files aufräumen"
echo "════════════════════════════════════════════════════════════════"

# pCloud-Index-Caches
TEMP_COUNT=$(ls -1 /tmp/pcloud_index_*.json 2>/dev/null | wc -l)
if [ "$TEMP_COUNT" -gt 0 ]; then
    echo "🗑️  Lösche $TEMP_COUNT Temp-Files"
    rm -f /tmp/pcloud_index_*.json
    echo "   ✓ Temp-Files gelöscht"
else
    echo "○  Keine Temp-Files gefunden"
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[5/5] Final Check"
echo "════════════════════════════════════════════════════════════════"

echo "📊 Clean-State Status:"
echo ""
echo "RTB-Snapshots:"
ls -lh "$RTB_BASE" | grep -E '^d' | awk '{print "  " $9 " (" $5 ")"}'
echo ""
echo "Latest-Symlink:"
ls -lh "$RTB_BASE/latest" | awk '{print "  → " $11}'
echo ""
echo "Archive:"
echo "  manifests/: $(ls -1 "$ARCHIVE_BASE/manifests/" 2>/dev/null | wc -l) Dateien"
echo "  indexes/:   $(ls -1 "$ARCHIVE_BASE/indexes/" 2>/dev/null | wc -l) Dateien"
echo ""

echo "════════════════════════════════════════════════════════════════"
echo "✅ Clean-State vorbereitet!"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "🚀 Nächste Schritte:"
echo ""
echo "1. In pcloud-tools wechseln + venv aktivieren (in EINEM Befehl):"
echo "   source venv_switch.sh pcloud-tools"
echo ""
echo "   Alternative (manuell):"
echo "   cd /opt/apps/pcloud-tools && source venv/bin/activate"
echo ""
echo "2. Dry-Run Test:"
echo "   ./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT --dry-run"
echo ""
echo "3. Echter Upload (Full-Mode):"
echo "   ./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT"
echo ""
echo "4. Alternative via rtb_wrapper (von rtb/ aus):"
echo "   cd /opt/apps/rtb"
echo "   ./rtb_wrapper.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT"
echo ""
echo "5. Beobachten während Upload:"
echo "   ✓ Thread-Count (parallele Uploads)"
echo "   ✓ Debug-Output (CLI-Logs)"
echo "   ✓ Upload-Speed & Fortschritt"
echo "   ✓ Timeouts (keine Hänger)"
echo "   ✓ Manifest-Erstellung"
echo "   ✓ Index-Update"
echo ""
echo "💡 Tipp: Logs live verfolgen:"
echo "   tail -f /var/log/backup/pcloud_sync.log"
echo ""
echo "Good luck! 🍀"
echo ""
