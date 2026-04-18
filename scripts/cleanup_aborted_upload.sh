#!/bin/bash
# cleanup_aborted_upload.sh — Cleanup für abgebrochene pCloud-Uploads
# 
# Räumt einen abgebrochenen pCloud-Upload auf und bereitet Neustart vor.
# Funktioniert für beide Upload-Modi:
#   - Full-Mode: Alle Dateien/Stubs neu schreiben
#   - Delta-Copy-Mode: Server-Side Clone + Selective Update
#
# Bei Delta-Copy PoC Failures:
#   - Geklonter Snapshot wird gelöscht (remote via --remote)
#   - Lokaler RTB-Snapshot wird nur gelöscht wenn --local gesetzt
#   - Latest-Symlink wird auf vorherigen Snapshot gesetzt

set -e

# === Script-Verzeichnis ermitteln (für Python-Imports) ===
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SCRIPT_DIR

# === Argument-Handling ===
SNAPSHOT_NAME=""
DO_REMOTE_DELETE=false
DRY_RUN=false

# Unterstützt beide Formen:
#   $0 SNAPSHOT_NAME [--remote] [--dry-run]          (positional)
#   $0 --snapshot SNAPSHOT_NAME [--remote] [--dry-run] (named flag)
while [ $# -gt 0 ]; do
    case "$1" in
        --snapshot)
            SNAPSHOT_NAME="$2"
            shift 2
            ;;
        --remote)
            DO_REMOTE_DELETE=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -*)
            echo "❌ FEHLER: Unbekanntes Flag: $1" >&2
            exit 1
            ;;
        *)
            # Erstes Positional-Argument = Snapshot-Name
            if [ -z "$SNAPSHOT_NAME" ]; then
                SNAPSHOT_NAME="$1"
            fi
            shift
            ;;
    esac
done

if [ -z "$SNAPSHOT_NAME" ]; then
    echo "❌ FEHLER: Bitte Snapshot-Namen angeben!"
    echo ""
    echo "Usage:"
    echo "  $0 SNAPSHOT_NAME [--remote] [--dry-run]"
    echo "  $0 --snapshot SNAPSHOT_NAME [--remote] [--dry-run]"
    echo ""
    echo "Beispiele:"
    echo "  $0 2026-04-12-141849                        # Nur lokaler Cleanup"
    echo "  $0 2026-04-12-141849 --remote               # Auch pCloud-Snapshot löschen"
    echo "  $0 --snapshot 2026-04-12-141849 --remote    # Named-Flag-Syntax"
    echo "  $0 2026-04-12-141849 --dry-run              # Zeige was passieren würde (sicher)"
    echo "  $0 2026-04-12-141849 --remote --dry-run     # Vollständiger Dry-Run"
    echo ""
    echo "Standard: Nur lokaler Cleanup (pCloud manuell via UI prüfen)"
    exit 1
fi

RTB_BASE="/mnt/backup/rtb_nas"
ARCHIVE_BASE="/srv/pcloud-archive"
PCLOUD_DEST="/Backup/rtb_1to1/_snapshots"

echo "═══════════════════════════════════════════════════════════"
if [ "$DRY_RUN" = true ]; then
    echo "🔍 DRY-RUN: Cleanup für abgebrochenen Upload: $SNAPSHOT_NAME"
    echo "           (Keine Änderungen werden vorgenommen)"
else
    echo "Cleanup für abgebrochenen Upload: $SNAPSHOT_NAME"
fi
if [ "$DO_REMOTE_DELETE" = true ]; then
    echo "Modus: Lokal + Remote (pCloud-Snapshot wird gelöscht)"
else
    echo "Modus: Nur lokal (pCloud manuell prüfen)"
fi
echo "═══════════════════════════════════════════════════════════"

# 1. RTB-Snapshot löschen (lokal)
echo "[1/6] Lösche RTB-Snapshot: $RTB_BASE/$SNAPSHOT_NAME"
if [ -d "$RTB_BASE/$SNAPSHOT_NAME" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] Würde löschen: $RTB_BASE/$SNAPSHOT_NAME"
    else
        rm -rf "$RTB_BASE/$SNAPSHOT_NAME"
        echo "  ✓ Gelöscht"
    fi
else
    echo "  ○ Bereits gelöscht"
fi

# 2. Latest-Symlink zurücksetzen
echo "[2/6] Setze Latest-Symlink zurück"
# Alphabetische Sortierung (robuster bei ISO-Datum) + Error-Handling
# WICHTIG: Zu löschenden Snapshot ausschließen (grep -v)
PREVIOUS_SNAPSHOT=$(ls -1 "$RTB_BASE" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}$' | grep -v "^$SNAPSHOT_NAME$" | tail -1)
if [ -n "$PREVIOUS_SNAPSHOT" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] Würde setzen: Latest → $PREVIOUS_SNAPSHOT"
    else
        rm -f "$RTB_BASE/latest"
        ln -s "$RTB_BASE/$PREVIOUS_SNAPSHOT" "$RTB_BASE/latest"
        echo "  ✓ Latest → $PREVIOUS_SNAPSHOT"
    fi
else
    echo "  ⚠ Kein vorheriger Snapshot gefunden!"
fi

# 3. Manifest löschen (falls vorhanden)
echo "[3/6] Lösche Manifest: $ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json"
if [ -f "$ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] Würde löschen: $ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json"
    else
        rm -f "$ARCHIVE_BASE/manifests/$SNAPSHOT_NAME.json"
        echo "  ✓ Gelöscht"
    fi
else
    echo "  ○ Kein Manifest gefunden"
fi

# 4. Lokaler Index-Cache löschen
echo "[4/6] Lösche lokalen Index-Cache"
INDEX_CACHE="/tmp/pcloud_index_${SNAPSHOT_NAME}.json"
if [ -f "$INDEX_CACHE" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] Würde löschen: $INDEX_CACHE"
    else
        rm -f "$INDEX_CACHE"
        echo "  ✓ Gelöscht: $INDEX_CACHE"
    fi
else
    echo "  ○ Kein Index-Cache gefunden"
fi

# 5. pCloud-Snapshot löschen (remote - optional)
if [ "$DO_REMOTE_DELETE" = true ]; then
    echo "[5/6] Lösche pCloud-Snapshot (remote)"
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] Würde löschen: $PCLOUD_DEST/$SNAPSHOT_NAME (remote via API)"
    else
        python3 -c "
import sys
import os

# KRITISCH: sys.path für pcloud_bin_lib setzen
script_dir = os.environ.get('SCRIPT_DIR', '/opt/apps/pcloud-tools/main')
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

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
    fi
else
    echo "[5/6] pCloud-Snapshot (remote) NICHT gelöscht (--remote Flag nicht gesetzt)"
    echo "  ℹ️  Bitte manuell via pCloud Web-UI prüfen und löschen:"
    echo "  ℹ️  https://my.pcloud.com → $PCLOUD_DEST/$SNAPSHOT_NAME"
    echo "  ℹ️  Vorteil: Du siehst, was wirklich hochgeladen wurde"
fi

# 6. Verify
echo "[6/6] Verify Cleanup"
echo "  RTB-Snapshots:"
ls -lh "$RTB_BASE" | grep -E '(latest|[0-9]{4}-[0-9]{2}-[0-9]{2})' | tail -5
echo ""
echo "  Manifests:"
ls -lh "$ARCHIVE_BASE/manifests/" 2>/dev/null | tail -3 || echo "  (Ordner noch nicht vorhanden)"

echo ""
echo "═══════════════════════════════════════════════════════════"
if [ "$DRY_RUN" = true ]; then
    echo "🔍 DRY-RUN abgeschlossen (keine Änderungen vorgenommen)"
    echo "═══════════════════════════════════════════════════════════"
    echo ""
    echo "ℹ️  Um die Änderungen tatsächlich durchzuführen:"
    echo "   Führe das Script OHNE --dry-run aus:"
    if [ "$DO_REMOTE_DELETE" = true ]; then
        echo "   ./cleanup_aborted_upload.sh $SNAPSHOT_NAME --remote"
    else
        echo "   ./cleanup_aborted_upload.sh $SNAPSHOT_NAME"
    fi
else
    echo "✓ Lokaler Cleanup abgeschlossen!"
    echo "═══════════════════════════════════════════════════════════"
    echo ""

    if [ "$DO_REMOTE_DELETE" = true ]; then
        echo "ℹ️  pCloud-Snapshot wurde remote gelöscht"
    else
        echo "⚠️  pCloud-Snapshot wurde NICHT gelöscht (nur lokal)"
        echo "   → Bitte manuell via pCloud Web-UI prüfen und löschen:"
        echo "   → https://my.pcloud.com"
        echo "   → Pfad: $PCLOUD_DEST/$SNAPSHOT_NAME"
        echo ""
    fi

    echo "Nächste Schritte:"
    echo "  1. git pull origin main  # Neue Features (Hardening + Timestamps)"
    echo "  2. sudo bash /opt/apps/rtb/rtb_wrapper.sh  # Neues Backup starten"
fi
echo ""
