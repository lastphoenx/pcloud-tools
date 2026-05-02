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
    [--auto-remote-cleanup yes|no] [--auto-db-cleanup yes|no]
    [--rtb-base <PATH>] [--archive-base <PATH>]

Parameter:
  --keep-snapshot <SNAPSHOT>     Snapshot der lokal behalten wird (Pflicht)
  --keep-local-manifest <yes|no> Lokales Manifest <SNAPSHOT>.json behalten (Default: yes)
  --keep-lokal-manifest <yes|no> Alias für --keep-local-manifest
    --auto-remote-cleanup <yes|no> pCloud Remote-Cleanup automatisch ausführen (Default: yes)
    --auto-db-cleanup <yes|no>     DB-Cleanup automatisch ausführen (Default: yes)
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
PCLOUD_DEST="${PCLOUD_DEST:-/Backup/rtb_1to1}"
PCLOUD_DB_NAME="${PCLOUD_DB_NAME:-pcloud_backup}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"
KEEP_SNAPSHOT=""
KEEP_LOCAL_MANIFEST="yes"
AUTO_REMOTE_CLEANUP="yes"
AUTO_DB_CLEANUP="yes"
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
        --auto-remote-cleanup)
            AUTO_REMOTE_CLEANUP="$(to_yes_no "$2")"
            shift 2
            ;;
        --auto-db-cleanup)
            AUTO_DB_CLEANUP="$(to_yes_no "$2")"
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

    _env_dest=$(grep -E '^PCLOUD_DEST=' "$ENV_FILE" | head -1 | cut -d= -f2- \
        | sed -e 's/[[:space:]]*#.*//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ -n "$_env_dest" ]] && PCLOUD_DEST="$_env_dest"

    _env_db_name=$(grep -E '^PCLOUD_DB_NAME=' "$ENV_FILE" | head -1 | cut -d= -f2- \
        | sed -e 's/[[:space:]]*#.*//' -e 's/^["'"'"']//' -e 's/["'"'"']$//' \
              -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ -n "$_env_db_name" ]] && PCLOUD_DB_NAME="$_env_db_name"
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
echo "  - pCloud Remote-Cleanup: $AUTO_REMOTE_CLEANUP"
echo "  - DB-Cleanup: $AUTO_DB_CLEANUP"
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
echo "[6/5] pCloud Remote Cleanup & Index Restore"
echo "════════════════════════════════════════════════════════════════"

if [[ "$AUTO_REMOTE_CLEANUP" != "yes" ]]; then
    echo "⏭️  Übersprungen (--auto-remote-cleanup no): Remote-Cleanup bleibt manuell."
else

# Python-Pfad: venv bevorzugen, Fallback auf System-Python
PCLOUD_PYTHON="${PCLOUD_PYTHON:-/opt/apps/pcloud-tools/venv/bin/python3}"
if [[ ! -x "$PCLOUD_PYTHON" ]]; then
    PCLOUD_PYTHON="$(command -v python3 2>/dev/null || echo python3)"
fi
PCLOUD_LIB_DIR="${PCLOUD_LIB_DIR:-/opt/apps/pcloud-tools/main}"

# Inline-Python-Snippet: cfg einmal laden, alle Remote-Ops ausfuehren
_REMOTE_CLEANUP_PY=$(cat <<'PYEOF'
import sys, os
sys.path.insert(0, os.environ["PCLOUD_LIB_DIR"])
import pcloud_bin_lib as pc

env_file   = os.environ["ENV_FILE"]
dest       = os.environ["PCLOUD_DEST"].rstrip("/")
keep       = os.environ["KEEP_SNAPSHOT"]
dry        = os.environ.get("DRY_RUN", "yes") == "yes"
mode       = "[dry] " if dry else ""

snapshots_root = f"{dest}/_snapshots"
index_dir      = f"{snapshots_root}/_index"
archive_dir    = f"{index_dir}/archive"
target_index   = f"{index_dir}/content_index.json"
keep_archive   = f"{archive_dir}/{keep}_index.json"

delete_list = [s.strip() for s in os.environ.get("DELETE_SNAPSHOTS_CSV", "").split(",") if s.strip()]

cfg = pc.effective_config(env_file=env_file)

# --- 1. Remote-Snapshots loeschen ---
print("\n  [6a] Remote-Snapshot-Ordner loeschen:")
if not delete_list:
    print("   o  Nichts zu loeschen (keine DELETE_SNAPSHOTS)")
for snap in delete_list:
    remote_snap = f"{snapshots_root}/{snap}"
    print(f"   {mode}delete_folder(recursive) -> {remote_snap}")
    if not dry:
        try:
            pc.delete_folder(cfg, path=remote_snap, recursive=True)
            print(f"   OK Geloescht: {snap}")
        except Exception as e:
            print(f"   WARN {snap}: {e}", file=sys.stderr)

# --- 2. Archiv aufraumen: alle _index.json ausser keep ---
print("\n  [6b] Remote Index-Archiv bereinigen:")
try:
    children = pc.list_folder_children(cfg, path=archive_dir, include_files=True)
    files = [c for c in children if not c.get("isfolder", False)]
    keep_filename = f"{keep}_index.json"
    to_delete = [f for f in files if f.get("name") != keep_filename]
    if not to_delete:
        print(f"   o  Archiv bereits sauber (nur {keep_filename} oder leer)")
    for f in to_delete:
        fname = f.get("name", "?")
        fid   = f.get("fileid")
        print(f"   {mode}delete_file -> {archive_dir}/{fname}")
        if not dry:
            try:
                pc.delete_file(cfg, fileid=fid)
                print(f"   OK Geloescht: {fname}")
            except Exception as e:
                print(f"   WARN {fname}: {e}", file=sys.stderr)
except Exception as e:
    print(f"   WARN Archiv-Listing fehlgeschlagen (evtl. nicht vorhanden): {e}")

# --- 3. content_index.json aus Keep-Archiv-Eintrag wiederherstellen ---
print("\n  [6c] content_index.json wiederherstellen:")
print(f"   Quelle: {keep_archive}")
print(f"   Ziel:   {target_index}")
print(f"   {mode}copyfile(overwrite=True)")
if not dry:
    try:
        pc.copyfile(cfg, from_path=keep_archive, to_path=target_index, overwrite=True)
        print(f"   OK Index wiederhergestellt")
    except Exception as e:
        print(f"   FAIL copyfile fehlgeschlagen: {e}", file=sys.stderr)
        sys.exit(1)
PYEOF
)

# DELETE_SNAPSHOTS als CSV uebergeben (Bash-Array -> Python)
_CSV_SNAPSHOTS=$(IFS=,; echo "${DELETE_SNAPSHOTS[*]}")

PCLOUD_LIB_DIR="$PCLOUD_LIB_DIR" \
ENV_FILE="$ENV_FILE" \
PCLOUD_DEST="$PCLOUD_DEST" \
KEEP_SNAPSHOT="$KEEP_SNAPSHOT" \
DRY_RUN="$DRY_RUN" \
DELETE_SNAPSHOTS_CSV="$_CSV_SNAPSHOTS" \
"$PCLOUD_PYTHON" -c "$_REMOTE_CLEANUP_PY" || {
    echo "FAIL Remote-Cleanup-Script fehlgeschlagen (Exit $?)" >&2
    exit 1
}
fi

# mysql als root direkt, sonst per sudo (auch für manuelle Hinweise unten)
MYSQL_PREFIX=()
if [[ "$EUID" -ne 0 ]]; then
    MYSQL_PREFIX=(sudo)
fi

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "[7/5] DB Run-History bereinigen"
echo "════════════════════════════════════════════════════════════════"

if [[ "$AUTO_DB_CLEANUP" != "yes" ]]; then
    echo "⏭️  Übersprungen (--auto-db-cleanup no): DB-Cleanup bleibt manuell."
else

# Snapshot-Name SQL-sicher machen (single quote escapen)
SQL_KEEP_SNAPSHOT="${KEEP_SNAPSHOT//\'/\'\'}"

if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
    echo "❌ FEHLER: mysql nicht gefunden (MYSQL_BIN=$MYSQL_BIN)" >&2
    exit 1
fi

if ! "${MYSQL_PREFIX[@]}" "$MYSQL_BIN" -N -e "SELECT 1 FROM ${PCLOUD_DB_NAME}.backup_runs LIMIT 1;" >/dev/null 2>&1; then
    echo "❌ FEHLER: DB/Tabelle nicht erreichbar: ${PCLOUD_DB_NAME}.backup_runs" >&2
    echo "   Tipp: DB initialisieren oder PCLOUD_DB_NAME/DB-Rechte prüfen." >&2
    exit 1
fi

echo "📋 Kandidaten (alles außer Keep-Snapshot):"
"${MYSQL_PREFIX[@]}" "$MYSQL_BIN" -e "SELECT run_id, snapshot_name, status, started_at FROM ${PCLOUD_DB_NAME}.backup_runs WHERE snapshot_name <> '${SQL_KEEP_SNAPSHOT}' ORDER BY started_at DESC;"

echo ""
echo "📊 DB-Zähler vor Cleanup:"
"${MYSQL_PREFIX[@]}" "$MYSQL_BIN" -e "SELECT COUNT(*) AS runs FROM ${PCLOUD_DB_NAME}.backup_runs; SELECT COUNT(*) AS phases FROM ${PCLOUD_DB_NAME}.backup_phases; SELECT COUNT(*) AS backfills FROM ${PCLOUD_DB_NAME}.gap_backfills;"

if [[ "$DRY_RUN" = "yes" ]]; then
    echo ""
    echo "[dry] DELETE FROM ${PCLOUD_DB_NAME}.backup_runs WHERE snapshot_name <> '${SQL_KEEP_SNAPSHOT}';"
    echo "ℹ️  Dry-Run: keine DB-Änderung durchgeführt"
else
    echo ""
    echo "🗑️  Lösche DB-Runs außer Keep-Snapshot: $KEEP_SNAPSHOT"
    "${MYSQL_PREFIX[@]}" "$MYSQL_BIN" -e "DELETE FROM ${PCLOUD_DB_NAME}.backup_runs WHERE snapshot_name <> '${SQL_KEEP_SNAPSHOT}';"
    echo "   ✓ DB-Cleanup ausgeführt"
fi

echo ""
echo "📊 DB-Zähler nach Cleanup:"
"${MYSQL_PREFIX[@]}" "$MYSQL_BIN" -e "SELECT COUNT(*) AS runs FROM ${PCLOUD_DB_NAME}.backup_runs; SELECT COUNT(*) AS phases FROM ${PCLOUD_DB_NAME}.backup_phases; SELECT COUNT(*) AS backfills FROM ${PCLOUD_DB_NAME}.gap_backfills;"
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
if [[ "$AUTO_DB_CLEANUP" = "yes" ]]; then
    echo "1. DB-Cleanup wurde von Schritt [7/5] automatisch durchgeführt."
    echo "   (DELETE auf backup_runs außer Keep-Snapshot; backup_phases/gap_backfills via ON DELETE CASCADE)"
    echo "   Optional manuell verifizieren:"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"SELECT COUNT(*) AS runs FROM ${PCLOUD_DB_NAME}.backup_runs; SELECT COUNT(*) AS phases FROM ${PCLOUD_DB_NAME}.backup_phases; SELECT COUNT(*) AS backfills FROM ${PCLOUD_DB_NAME}.gap_backfills;\""
else
    echo "1. DB prüfen und verwaiste pCloud-Runs manuell entfernen (empfohlen als Erstes):"
    echo "   # 1a) Anzeigen: vorhandene Runs für Keep-Snapshot und andere Snapshots"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"SELECT snapshot_name, status, started_at, finished_at FROM ${PCLOUD_DB_NAME}.backup_runs ORDER BY started_at DESC LIMIT 50;\""
    echo ""
    echo "   # 1b) Kandidaten anzeigen (alles außer Keep-Snapshot):"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"SELECT run_id, snapshot_name, status, started_at FROM ${PCLOUD_DB_NAME}.backup_runs WHERE snapshot_name <> '${KEEP_SNAPSHOT}' ORDER BY started_at DESC;\""
    echo ""
    echo "   # 1c) Löschen (manuell freigeben):"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"DELETE FROM ${PCLOUD_DB_NAME}.backup_runs WHERE snapshot_name <> '${KEEP_SNAPSHOT}';\""
    echo "   # Hinweis: backup_phases/gap_backfills hängen via ON DELETE CASCADE an backup_runs."
    echo ""
    echo "   # 1d) Verifikation (wichtig: COUNT(*) statt COUNT()):"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"SELECT COUNT(*) AS runs FROM ${PCLOUD_DB_NAME}.backup_runs; SELECT COUNT(*) AS phases FROM ${PCLOUD_DB_NAME}.backup_phases; SELECT COUNT(*) AS backfills FROM ${PCLOUD_DB_NAME}.gap_backfills;\""
    echo ""
    echo "   # 1e) Optional: Full-Reset aller pCloud-Runs (nur wenn Remote wirklich leer ist):"
    echo "   ${MYSQL_PREFIX[*]} ${MYSQL_BIN} -e \"DELETE FROM ${PCLOUD_DB_NAME}.backup_runs;\""
fi
echo ""
if [[ "$AUTO_REMOTE_CLEANUP" = "yes" ]]; then
    echo "2. pCloud Remote-Cleanup: wurde von Schritt [6/5] automatisch erledigt."
    echo "   Bei --execute wurden durchgeführt:"
    echo "   [6a] Remote-Snapshot-Ordner gelöscht (alle ausser ${KEEP_SNAPSHOT})"
    echo "   [6b] _index/archive/ bereinigt (nur ${KEEP_SNAPSHOT}_index.json behalten)"
    echo "   [6c] content_index.json auf Stand ${KEEP_SNAPSHOT} restored"
    echo "   Bei Problemen manuell prüfen:"
    echo "     ${PCLOUD_DEST}/_snapshots/_index/content_index.json"
else
    echo "2. pCloud Remote-Cleanup manuell prüfen/bereinigen (vor neuem Lauf):"
    echo "   - Snapshot-Ordner löschen, die nicht mehr gelten:"
    echo "     ${PCLOUD_DEST}/_snapshots/<SNAPSHOT_NAME>"
    echo "   - Aktiven Index prüfen/löschen falls inkonsistent:"
    echo "     ${PCLOUD_DEST}/_snapshots/_index/content_index.json"
    echo "   - Optional: aus Archiv wiederherstellen (falls vorhanden):"
    echo "     ${PCLOUD_DEST}/_snapshots/_index/archive/${KEEP_SNAPSHOT}_index.json"
    echo "       -> kopieren nach ${PCLOUD_DEST}/_snapshots/_index/content_index.json"
fi
echo ""
if [[ "$KEEP_LOCAL_MANIFEST" = "yes" ]]; then
    echo "   - Lokal bleibt Keep-Manifest aktiv: ${ARCHIVE_BASE}/manifests/${KEEP_SNAPSHOT}.json"
    echo "   - Temp-Manifest für Speed-Start: ${PCLOUD_TEMP_DIR}/pcloud_mani.${KEEP_SNAPSHOT}.json"
else
    echo "   - Keep-Manifest ist deaktiviert (--keep-local-manifest no):"
    echo "     nächster Lauf erzeugt Manifest neu (kein Speed-Start aus lokaler Keep-Kopie)."
fi
echo ""
echo "3. venv aktivieren (voller Pfad):"
echo "   source /opt/apps/safe-ops-cli/main/tools/venv_switch.sh pcloud-tools"
echo ""
echo "4a. Test-Lauf via rtb_wrapper (check-only, read-only):"
echo "   /opt/apps/rtb/rtb_wrapper.sh --check-only"
echo ""
echo "4b. Produktions-Lauf via rtb_wrapper:"
echo "   /opt/apps/rtb/rtb_wrapper.sh"
echo ""
echo "5a. Hinweis: --check-only gibt es nur im rtb_wrapper"
echo "   Direkter pcloud-wrapper unterstützt jetzt: --dry-run, --use-delta-copy"
echo ""
echo "5b. Direkter pcloud-tools Produktions-Lauf:"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT"
echo "5c. Direkter pcloud-tools Dry-Run:"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT --dry-run"
echo "5d. Copy/Turbo erzwingen (direkter Wrapper):"
echo "   /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/$KEEP_SNAPSHOT --use-delta-copy"
echo ""
echo "6. Beobachten während Upload:"
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
