#!/bin/bash
# prepare_fresh_test.sh — Bereitet Clean-State für Workflow-Test vor
#
# Behält genau EINEN Snapshot, löscht alle anderen, setzt latest,
# leert Archive und räumt Temp-Files auf.

set -e  # Bei Fehler abbrechen

usage() {
    cat <<EOF
Usage:
    $0 --keep-snapshot <SNAPSHOT> [--keep-local-manifest yes|no] [--dry-run|--execute] [--yes]
     [--rtb-base <PATH>] [--archive-base <PATH>]

Parameter:
  --keep-snapshot <SNAPSHOT>     Snapshot der lokal behalten wird (Pflicht)
  --keep-local-manifest <yes|no> Lokales Manifest <SNAPSHOT>.json behalten (Default: yes)
  --keep-lokal-manifest <yes|no> Alias für --keep-local-manifest
  --rtb-base <PATH>              RTB Basis-Pfad (Default: /mnt/backup/rtb_nas)
    --archive-base <PATH>          Archiv-Basis (Default: /srv/pcloud-archive)
    --env-file <PATH>              .env-Datei für PCLOUD_TEMP_DIR (Default: /opt/apps/pcloud-tools/main/.env)
  --dry-run                      Nur anzeigen was gelöscht/gesetzt würde (Default)
  --execute                      Führt Änderungen wirklich aus (destruktiv!)
  --yes                          Ohne Rückfrage ausführen (nur mit --execute sinnvoll)
  -h, --help                     Hilfe anzeigen
EOF
}

to_yes_no() {
    case "$1" in
        yes|no) echo "$1" ;;
        *)
            echo "❌ Ungültiger Wert '$1' (erlaubt: yes|no)" >&2
            exit 2
            ;;
    esac
}

# === Defaults ===
RTB_BASE="/mnt/backup/rtb_nas"
ARCHIVE_BASE="/srv/pcloud-archive"
ENV_FILE="/opt/apps/pcloud-tools/main/.env"
PCLOUD_TEMP_DIR="${PCLOUD_TEMP_DIR:-/tmp}"
KEEP_SNAPSHOT=""
KEEP_LOCAL_MANIFEST="yes"
ASSUME_YES="no"
DRY_RUN="yes"

# === Args ===
while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-snapshot)
            KEEP_SNAPSHOT="$2"
            shift 2
            ;;
        --keep-local-manifest|--keep-lokal-manifest)
            KEEP_LOCAL_MANIFEST="$(to_yes_no "$2")"
            shift 2
            ;;
        --rtb-base)
            RTB_BASE="$2"
            shift 2
            ;;
        --archive-base)
            ARCHIVE_BASE="$2"
            shift 2
            ;;
        --env-file)
            ENV_FILE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN="yes"
            shift
            ;;
        --execute)
            DRY_RUN="no"
            shift
            ;;
        --yes)
            ASSUME_YES="yes"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Unbekannter Parameter: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [[ -z "$KEEP_SNAPSHOT" ]]; then
    echo "❌ --keep-snapshot ist erforderlich." >&2
    usage
    exit 2
fi

# PCLOUD_TEMP_DIR aus .env laden (überschreibt Default /tmp mit realem Wert)
if [[ -f "$ENV_FILE" ]]; then
    _env_temp=$(grep -E '^PCLOUD_TEMP_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- \
        | sed -e 's/[[:space:]]*#.*//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ -n "$_env_temp" ]] && PCLOUD_TEMP_DIR="$_env_temp"
fi

if [[ ! -d "$RTB_BASE/$KEEP_SNAPSHOT" ]]; then
    echo "❌ FEHLER: Keep-Snapshot nicht gefunden: $RTB_BASE/$KEEP_SNAPSHOT" >&2
    exit 1
fi

declare -a DELETE_SNAPSHOTS=()
while IFS= read -r -d '' dir; do
    name="$(basename "$dir")"
    if [[ "$name" != "$KEEP_SNAPSHOT" ]]; then
        DELETE_SNAPSHOTS+=("$name")
    fi
done < <(find "$RTB_BASE" -mindepth 1 -maxdepth 1 -type d -print0)

echo "════════════════════════════════════════════════════════════════"
echo "🧹 pCloud-Tools: Fresh Test Preparation"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Aktion: Clean-State für Workflow-Test"
echo "  - Modus: $( [[ "$DRY_RUN" = "yes" ]] && echo "DRY-RUN (keine Änderungen)" || echo "EXECUTE (destruktiv)" )"
echo "  - Behalte Snapshot: $KEEP_SNAPSHOT"
echo "  - Lösche alle anderen lokalen Snapshots (${#DELETE_SNAPSHOTS[@]})"
echo "  - Setze latest → $KEEP_SNAPSHOT"
echo "  - Leere Archive (indexes immer, manifests abhängig von --keep-local-manifest=$KEEP_LOCAL_MANIFEST)"
echo "  - Cleanup Temp-Files"
echo ""
if [[ "$DRY_RUN" = "yes" ]]; then
    echo "ℹ️  Dry-Run aktiv: Es werden keine Dateien gelöscht und keine Symlinks geändert."
elif [[ "$ASSUME_YES" != "yes" ]]; then
    read -p "▶ Fortfahren? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ Abgebrochen."
        exit 1
    fi
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[1/5] RTB-Snapshots aufräumen"
echo "════════════════════════════════════════════════════════════════"

# Aktuelle Snapshots anzeigen
echo "📋 Aktueller Stand:"
ls -lh "$RTB_BASE" | grep -E '^d|^l' || true
echo ""

# Alle anderen Snapshots löschen
for snapshot in "${DELETE_SNAPSHOTS[@]}"; do
    if [ -d "$RTB_BASE/$snapshot" ]; then
        if [[ "$DRY_RUN" = "yes" ]]; then
            echo "[dry] rm -rf $RTB_BASE/$snapshot"
        else
            echo "🗑️  Lösche: $snapshot"
            rm -rf "$RTB_BASE/$snapshot"
            echo "   ✓ Gelöscht"
        fi
    else
        echo "○  $snapshot bereits gelöscht"
    fi
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[2/5] Latest-Symlink setzen"
echo "════════════════════════════════════════════════════════════════"

# Setze latest-Symlink
if [[ "$DRY_RUN" = "yes" ]]; then
    echo "[dry] rm -f $RTB_BASE/latest"
    echo "[dry] ln -s $KEEP_SNAPSHOT $RTB_BASE/latest"
    echo "🔗 (dry) Latest würde gesetzt auf → $KEEP_SNAPSHOT"
else
    rm -f "$RTB_BASE/latest"
    ln -s "$KEEP_SNAPSHOT" "$RTB_BASE/latest"
    echo "🔗 Latest → $KEEP_SNAPSHOT"
fi

# Kontrolle
if [[ "$DRY_RUN" = "yes" ]]; then
    echo "   Kontrolle (dry): latest würde auf $KEEP_SNAPSHOT zeigen"
else
    LATEST_TARGET=$(readlink "$RTB_BASE/latest")
    echo "   Kontrolle: latest zeigt auf $LATEST_TARGET"
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[3/5] Archive leeren (manifests + indexes)"
echo "════════════════════════════════════════════════════════════════"

if [[ "$DRY_RUN" = "yes" ]]; then
    echo "[dry] mkdir -p $ARCHIVE_BASE/manifests $ARCHIVE_BASE/indexes"
else
    mkdir -p "$ARCHIVE_BASE/manifests" "$ARCHIVE_BASE/indexes"
fi

# Manifests leeren
MANIFEST_COUNT=$(ls -1 "$ARCHIVE_BASE/manifests/"*.json 2>/dev/null | wc -l)
KEEP_MANIFEST_PATH="$ARCHIVE_BASE/manifests/$KEEP_SNAPSHOT.json"
if [ "$KEEP_LOCAL_MANIFEST" = "yes" ]; then
    if [ "$MANIFEST_COUNT" -gt 0 ]; then
        echo "🧹 Lösche Manifests außer: $KEEP_SNAPSHOT.json"
        for mf in "$ARCHIVE_BASE/manifests/"*.json; do
            [ -e "$mf" ] || continue
            if [ "$(basename "$mf")" != "$KEEP_SNAPSHOT.json" ]; then
                if [[ "$DRY_RUN" = "yes" ]]; then
                    echo "[dry] rm -f $mf"
                else
                    rm -f "$mf"
                fi
            fi
        done
        if [ -f "$KEEP_MANIFEST_PATH" ]; then
            echo "   ✓ Keep-Manifest behalten: $KEEP_SNAPSHOT.json"
            # Manifest auch nach PCLOUD_TEMP_DIR kopieren damit der nächste Lauf es findet
            TEMP_MANI="${PCLOUD_TEMP_DIR}/pcloud_mani.${KEEP_SNAPSHOT}.json"
            if [[ "$DRY_RUN" = "yes" ]]; then
                echo "[dry] cp $KEEP_MANIFEST_PATH $TEMP_MANI"
            else
                cp "$KEEP_MANIFEST_PATH" "$TEMP_MANI"
                echo "   ✓ Manifest nach Temp kopiert: $TEMP_MANI"
            fi
        else
            echo "   ○ Keep-Manifest nicht vorhanden: $KEEP_SNAPSHOT.json"
        fi
    else
        echo "○  Manifests bereits leer"
    fi
else
    if [ "$MANIFEST_COUNT" -gt 0 ]; then
        echo "🗑️  Lösche $MANIFEST_COUNT Manifests"
        if [[ "$DRY_RUN" = "yes" ]]; then
            echo "[dry] rm -f $ARCHIVE_BASE/manifests/*.json"
        else
            rm -f "$ARCHIVE_BASE/manifests/"*.json
            echo "   ✓ Manifests gelöscht"
        fi
    else
        echo "○  Manifests bereits leer"
    fi
fi

# Indexes leeren
INDEX_COUNT=$(ls -1 "$ARCHIVE_BASE/indexes/"*.json 2>/dev/null | wc -l)
if [ "$INDEX_COUNT" -gt 0 ]; then
    echo "🗑️  Lösche $INDEX_COUNT Indexes"
    if [[ "$DRY_RUN" = "yes" ]]; then
        echo "[dry] rm -f $ARCHIVE_BASE/indexes/*.json"
    else
        rm -f "$ARCHIVE_BASE/indexes/"*.json
        echo "   ✓ Indexes gelöscht"
    fi
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
    if [[ "$DRY_RUN" = "yes" ]]; then
        echo "[dry] rm -f /tmp/pcloud_index_*.json"
    else
        rm -f /tmp/pcloud_index_*.json
        echo "   ✓ Temp-Files gelöscht"
    fi
else
    echo "○  Keine Temp-Files gefunden"
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[5/5] Final Check"
echo "════════════════════════════════════════════════════════════════"

FINAL_TEMP_MANI="${PCLOUD_TEMP_DIR}/pcloud_mani.${KEEP_SNAPSHOT}.json"

if [[ "$DRY_RUN" = "yes" ]]; then
    # Soll-Zustand berechnen (was nach --execute der Fall wäre)
    echo "📊 Erwarteter Soll-Zustand nach --execute:"
    echo ""
    echo "RTB-Snapshots:"
    echo "  $KEEP_SNAPSHOT (behalten)"
    if [ "${#DELETE_SNAPSHOTS[@]}" -gt 0 ]; then
        for s in "${DELETE_SNAPSHOTS[@]}"; do
            echo "  $s → gelöscht"
        done
    fi
    echo ""
    echo "Latest-Symlink:"
    echo "  → $KEEP_SNAPSHOT"
    echo ""
    echo "Archive:"
    if [ "$KEEP_LOCAL_MANIFEST" = "yes" ]; then
        _expected_manifests=0
        if [ -f "$KEEP_MANIFEST_PATH" ]; then
            _expected_manifests=1
        fi
        echo "  manifests/: $_expected_manifests Datei(en) (nur $KEEP_SNAPSHOT.json)"
        echo "  keep-local-manifest: yes -> Keep-Manifest bleibt im Archiv"
        echo "  temp-manifest: $FINAL_TEMP_MANI (wird aus Keep-Manifest kopiert, falls vorhanden)"
    else
        echo "  manifests/: 0 Dateien (alle gelöscht)"
        echo "  keep-local-manifest: no -> kein Keep-Manifest im Archiv"
        echo "  temp-manifest: keine Keep-Kopie nach $PCLOUD_TEMP_DIR"
    fi
    echo "  indexes/:   0 Dateien (alle gelöscht)"
    echo ""
    echo "  → Führe '--execute' aus um diesen Zustand herzustellen"
else
    # Ist-Zustand nach echter Ausführung
    echo "📊 Clean-State Status (Ist-Zustand):"
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
    if [ "$KEEP_LOCAL_MANIFEST" = "yes" ]; then
        if [ -f "$KEEP_MANIFEST_PATH" ]; then
            echo "  keep-manifest: vorhanden ($KEEP_SNAPSHOT.json)"
        else
            echo "  keep-manifest: nicht vorhanden ($KEEP_SNAPSHOT.json)"
        fi
        if [ -f "$FINAL_TEMP_MANI" ]; then
            echo "  temp-manifest: vorhanden ($FINAL_TEMP_MANI)"
        else
            echo "  temp-manifest: nicht vorhanden ($FINAL_TEMP_MANI)"
        fi
    else
        echo "  keep-manifest: deaktiviert (--keep-local-manifest no)"
        echo "  temp-manifest: deaktiviert (keine Keep-Kopie nach $PCLOUD_TEMP_DIR)"
    fi
fi
echo ""

echo "════════════════════════════════════════════════════════════════"
if [[ "$DRY_RUN" = "yes" ]]; then
    echo "✅ Dry-Run abgeschlossen (keine Änderungen durchgeführt)"
else
    echo "✅ Clean-State vorbereitet!"
fi
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "🚀 Nächste Schritte:"
echo ""
echo "1. venv aktivieren (voller Pfad):"
echo "   source /opt/apps/safe-ops-cli/main/tools/venv_switch.sh pcloud-tools"
echo ""
echo "2a. Test-Lauf via rtb_wrapper (check-only, read-only):"
echo "   /opt/apps/rtb/rtb_wrapper.sh --check-only"
echo ""
echo "2b. Produktions-Lauf via rtb_wrapper:"
echo "   /opt/apps/rtb/rtb_wrapper.sh"
echo ""
echo "3a. Hinweis: --check-only gibt es nur im rtb_wrapper"
echo "   Direkter pcloud-wrapper unterstützt jetzt: --dry-run, --use-delta-copy"
echo ""
echo "3b. Direkter pcloud-tools Produktions-Lauf:"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT"
echo "3c. Direkter pcloud-tools Dry-Run:"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT --dry-run"
echo "3d. Copy/Turbo erzwingen (direkter Wrapper):"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT --use-delta-copy"
echo ""
echo "4. Beobachten während Upload:"
echo "   ✓ Thread-Count (parallele Uploads)"
echo "   ✓ Debug-Output (CLI-Logs)"
echo "   ✓ Upload-Speed & Fortschritt"
echo "   ✓ Timeouts (keine Hänger)"
echo "   ✓ Manifest-Erstellung"
echo "   ✓ Index-Update"
echo ""
echo "⚡ Modus-Hinweis:"
echo "   - Copy-Modus: maximale Geschwindigkeit, höherer Speicherverbrauch"
echo "   - Smart-Logik: balanciert Upload-Zeit und Speicherverbrauch (empfohlen)"
echo "   - Default: Smart-Logik im /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh"
echo ""
echo "   Copy/Turbo explizit forcieren (Low-Level, ohne Wrapper):"
echo "   /opt/apps/pcloud-tools/venv/bin/python /opt/apps/pcloud-tools/main/pcloud_push_json_manifest_to_pcloud.py \\" 
echo "     --manifest ${PCLOUD_TEMP_DIR}/pcloud_mani.$KEEP_SNAPSHOT.json --dest-root /Backup/rtb_1to1 \\" 
echo "     --snapshot-mode 1to1 --use-delta-copy --env-file /opt/apps/pcloud-tools/main/.env"
echo "   (Alternativ jetzt direkt im Wrapper möglich: ...wrapper_pcloud_sync_1to1.sh <SNAPSHOT> --use-delta-copy)"
echo ""
echo "🔧 Wichtige Flags/Parameter:"
echo "   rtb_wrapper.sh:"
echo "     --check-only   (read-only Check)"
echo "     --force        (Safety-Gate umgehen)"
echo "     --upload-only /mnt/backup/rtb_nas/<SNAPSHOT>"
echo "   wrapper_pcloud_sync_1to1.sh:"
echo "     CLI: [SNAPSHOT|/path/to/SNAPSHOT] [--dry-run] [--use-delta-copy]"
echo "     --check-only gibt es NICHT (nur im rtb_wrapper)"
echo "     wichtige ENVs: PCLOUD_MANIFEST_MODE=smart|full, PCLOUD_GAP_STRATEGY=conservative|optimistic|aggressive"
echo ""
echo "💡 Tipp: Logs live verfolgen:"
echo "   tail -f /var/log/backup/pcloud_sync.log"
echo ""
echo "Good luck! 🍀"
echo ""
