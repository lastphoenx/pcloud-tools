#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# cleanup_orphaned_manifests.sh
# ==============================================================================
# 
# ZWECK:
#   Findet und löscht "orphaned" Manifeste - JSON-Dateien ohne zugehörigen
#   RTB-Snapshot. Nützlich nach manuellen Snapshot-Löschungen oder als
#   Safety-Net bei Retention-Problemen.
#
# VORTEILE:
#   ✓ Einmalig nutzbar für "Leichen-Bereinigung"
#   ✓ Ad-hoc Wartung nach manuellen Changes
#   ✓ Safety-Net (Debugging-Tool)
#   ✓ Dry-run Modus (sicher testen)
#   ✓ Zeigt welche Manifeste betroffen sind
#
# NACHTEILE:
#   ⚠ Extra Script zu warten
#   ⚠ Kann Probleme verschleiern (besser: Root Cause fixen)
#   ⚠ Einmalige Lösung (nicht für Automation gedacht)
#
# WANN NUTZEN:
#   - Nach manuellen RTB-Eingriffen (Snapshots per Hand gelöscht)
#   - Nach Retention-Läufen (einmal monatlich checken)
#   - Debugging (wenn Manifest-Counts komisch aussehen)
#   - Migration/Cleanup (einmalig alte Testdaten entfernen)
#
# HINWEIS:
#   Ab Commit aa1cdcb läuft Paritäts-Cleanup automatisch bei retention_sync.
#   Dieses Script ist nur noch für Legacy-Cleanup oder manuelle Eingriffe nötig.
#
# USAGE:
#   ./cleanup_orphaned_manifests.sh [--dry-run]
#
# EXAMPLES:
#   # Test-Lauf (zeigt nur was gelöscht würde)
#   ./cleanup_orphaned_manifests.sh --dry-run
#
#   # Tatsächlich löschen
#   ./cleanup_orphaned_manifests.sh
#
# ==============================================================================

# ===== Konfiguration =====
RTB_BASE=${RTB_BASE:-/mnt/backup/rtb_nas}
MANIFEST_DIR=${MANIFEST_DIR:-/srv/pcloud-archive/manifests}

# Dry-run Modus (default: false)
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ===== Validierung =====
if [[ ! -d "$RTB_BASE" ]]; then
  echo "Fehler: RTB_BASE nicht gefunden: $RTB_BASE" >&2
  exit 1
fi

if [[ ! -d "$MANIFEST_DIR" ]]; then
  echo "Fehler: MANIFEST_DIR nicht gefunden: $MANIFEST_DIR" >&2
  exit 1
fi

# ===== Header =====
echo "═══════════════════════════════════════════════════════════"
if $DRY_RUN; then
  echo "🔍 DRY-RUN: Orphaned Manifest Cleanup"
  echo "           (Keine Änderungen werden vorgenommen)"
else
  echo "Orphaned Manifest Cleanup"
fi
echo "═══════════════════════════════════════════════════════════"
echo "RTB-Snapshots:  $RTB_BASE"
echo "Manifeste:      $MANIFEST_DIR"
echo ""

# ===== Cleanup =====
ORPHANED_COUNT=0
VALID_COUNT=0
TOTAL_COUNT=0

# Iteriere über alle Manifeste
for manifest in "$MANIFEST_DIR"/*.json; do
  # Skip wenn keine Manifeste vorhanden
  [[ ! -f "$manifest" ]] && continue
  
  TOTAL_COUNT=$((TOTAL_COUNT + 1))
  snapshot=$(basename "$manifest" .json)
  
  # Prüfe ob RTB-Snapshot existiert
  if [[ ! -d "$RTB_BASE/$snapshot" ]]; then
    # Orphan gefunden!
    ORPHANED_COUNT=$((ORPHANED_COUNT + 1))
    
    if $DRY_RUN; then
      echo "  [dry-run] Würde löschen: $snapshot.json (kein RTB-Snapshot)"
    else
      rm -f "$manifest"
      echo "  ✓ Gelöscht: $snapshot.json (orphan)"
    fi
  else
    # Manifest ist valid (RTB-Snapshot existiert)
    VALID_COUNT=$((VALID_COUNT + 1))
  fi
done

# ===== Zusammenfassung =====
echo ""
echo "═══════════════════════════════════════════════════════════"
if $DRY_RUN; then
  echo "🔍 DRY-RUN abgeschlossen"
else
  echo "✓ Cleanup abgeschlossen"
fi
echo "═══════════════════════════════════════════════════════════"
echo "Gesamt:   $TOTAL_COUNT Manifeste"
echo "Valid:    $VALID_COUNT (RTB-Snapshot existiert)"
echo "Orphaned: $ORPHANED_COUNT (RTB-Snapshot fehlt)"

if [[ $ORPHANED_COUNT -gt 0 ]]; then
  if $DRY_RUN; then
    echo ""
    echo "ℹ️  Um die Änderungen tatsächlich durchzuführen:"
    echo "   Führe das Script OHNE --dry-run aus:"
    echo "   ./cleanup_orphaned_manifests.sh"
  else
    echo ""
    echo "✓ $ORPHANED_COUNT orphaned Manifeste gelöscht"
  fi
else
  echo ""
  echo "✓ Keine orphaned Manifeste gefunden - alles sauber!"
fi

exit 0
