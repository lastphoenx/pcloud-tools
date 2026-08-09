#!/usr/bin/env bash
set -euo pipefail

# =====================================================
# POOL-MODE Wrapper für pCloud Backup
# =====================================================
# Alle Snapshots als Stubs, Files im zentralen Pool.
# Architektur: /_pool/XX/[sha256] + /_snapshots/SNAP/*.meta.json
# Benefits: Quota-effizient, schnelle Retention, einfaches Restore
# =====================================================

# ========= Basiskonfiguration =========
MAIN_DIR=${MAIN_DIR:-/opt/apps/pcloud-tools/main}
RTB=${RTB:-/mnt/backup/rtb_nas}

ENV_FILE=${ENV_FILE:-${MAIN_DIR}/.env}
PCLOUD_DEST=${PCLOUD_DEST:-/Backup/rtb_pool}

MANI=${MANI:-${MAIN_DIR}/pcloud_json_pool_manifest.py}
PUSH=${PUSH:-${MAIN_DIR}/pcloud_push_json_pool_manifest_to_pcloud.py}
INTEGRITY_RUN=${INTEGRITY_RUN:-${MAIN_DIR}/scripts/utilities/pool_integrity_run.py}

# Python-Interpreter (dedizierte pcloud-venv bevorzugt)
if [[ -x "/opt/apps/pcloud-tools/venv/bin/python" ]]; then
  PY="/opt/apps/pcloud-tools/venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
else
  PY="${PY:-python3}"
fi

# Module auffindbar machen
export PYTHONPATH="${MAIN_DIR}:${PYTHONPATH:-}"

# Finalize: standardmäßig im Wrapper aus Performance-Gründen überspringen.
export PCLOUD_SKIP_FINALIZE=${PCLOUD_SKIP_FINALIZE:-1}

# Load all PCLOUD_* variables from .env file
if [[ -f "${ENV_FILE:-}" ]]; then
  while IFS='=' read -r key val; do
    # Kommentare und Leerzeilen überspringen
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    # PCLOUD_* vars exportieren
    if [[ "$key" =~ ^PCLOUD_ ]]; then
      # Inline-Kommentare entfernen (nur wenn # nach Whitespace kommt)
      val=$(echo "$val" | sed 's/[[:space:]]#.*//')
      # Trailing/leading whitespace entfernen
      val=$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
      # Quotes entfernen
      if [[ "$val" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
      elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
      fi
      export "${key}=${val}"
    fi
  done < "${ENV_FILE}"
fi

# Temp-Pfad aus Env oder Default
export PCLOUD_TEMP_DIR="${PCLOUD_TEMP_DIR:-/tmp}"
export PCLOUD_ARCHIVE_DIR="${PCLOUD_ARCHIVE_DIR:-/srv/pcloud-archive}"

# Verzeichnisse erstellen falls nicht vorhanden
mkdir -p "${PCLOUD_TEMP_DIR}" "${PCLOUD_ARCHIVE_DIR}/manifests" "${PCLOUD_ARCHIVE_DIR}/deltas" 2>/dev/null || true

# ========= Globales Lock =========
LOCKFILE=${LOCKFILE:-/run/backup_pipeline.lock}
WAIT_SEC=${WAIT_SEC:-7200}
NAS_HEAVY_OPS_LIB=${NAS_HEAVY_OPS_LIB:-/opt/apps/rtb/nas_heavy_ops_lock.sh}
if [[ -f "$NAS_HEAVY_OPS_LIB" ]]; then
  # shellcheck source=/opt/apps/rtb/nas_heavy_ops_lock.sh
  source "$NAS_HEAVY_OPS_LIB"
fi
SAFETY_DELAY_SEC=${SAFETY_DELAY_SEC:-120}
PCLOUD_PREFLIGHT_RETRIES=${PCLOUD_PREFLIGHT_RETRIES:-3}
PCLOUD_PREFLIGHT_RETRY_DELAY_SEC=${PCLOUD_PREFLIGHT_RETRY_DELAY_SEC:-5}

# ========= Logging =========
PCLOUD_LOG=${PCLOUD_LOG:-/var/log/backup/pcloud_sync.log}
PCLOUD_JSONL_LOG=${PCLOUD_JSONL_LOG:-${PCLOUD_LOG%.log}.jsonl}
PCLOUD_ENABLE_JSONL=${PCLOUD_ENABLE_JSONL:-1}  # 1=enabled, 0=disabled

mkdir -p "$(dirname "$PCLOUD_LOG")"
exec > >(tee -a "$PCLOUD_LOG") 2>&1

# Legacy log function (for backwards compatibility)
log(){ _log INFO "$@"; }

# Enhanced structured logging with levels
_log() {
  local level="${1:-INFO}"
  shift
  local msg="$*"
  local ts; ts="$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')"
  
  # Human-readable output (to stdout/file)
  printf "%s [%s] %s\n" "$ts" "$level" "$msg"
  
  # JSONL output (for monitoring/parsing)
  if [[ "${PCLOUD_ENABLE_JSONL}" == "1" ]]; then
    # Use jq if available, otherwise simple JSON
    if command -v jq &>/dev/null; then
      jq -nc \
        --arg ts "$ts" \
        --arg level "$level" \
        --arg msg "$msg" \
        --arg run_id "${RUN_ID:-}" \
        '{timestamp: $ts, level: $level, message: $msg, run_id: $run_id}' \
        >> "$PCLOUD_JSONL_LOG" 2>/dev/null || true
    else
      # Fallback: Manual JSON escaping
      printf '{"timestamp":"%s","level":"%s","message":"%s","run_id":"%s"}\n' \
        "$ts" "$level" "${msg//\"/\\\"}" "${RUN_ID:-}" \
        >> "$PCLOUD_JSONL_LOG" 2>/dev/null || true
    fi
  fi
}

# ========= MariaDB Run-History Tracking =========
# Config (loaded from .env via source)
PCLOUD_DB_HOST=${PCLOUD_DB_HOST:-localhost}
PCLOUD_DB_PORT=${PCLOUD_DB_PORT:-3306}
PCLOUD_DB_NAME=${PCLOUD_DB_NAME:-pcloud_backup}
PCLOUD_DB_USER=${PCLOUD_DB_USER:-pcloud_backup}
PCLOUD_DB_PASS=${PCLOUD_DB_PASS:-}
PCLOUD_ENABLE_DB=${PCLOUD_ENABLE_DB:-0}  # 0=disabled (default), 1=enabled
RUN_ID=""  # Will be set at start

# MySQL helper function (SECURE: password via env, not visible in ps aux)
_mysql() {
  MYSQL_PWD="$PCLOUD_DB_PASS" mysql -h "$PCLOUD_DB_HOST" \
        -P "$PCLOUD_DB_PORT" \
        -u "$PCLOUD_DB_USER" \
        -D "$PCLOUD_DB_NAME" \
        -sN \
        -e "$@" 2>/dev/null
}

# Initialize database connection
_db_init() {
  [[ "${PCLOUD_ENABLE_DB}" != "1" ]] && return 0
  
  # Test connection
  if ! _mysql "SELECT 1" >/dev/null 2>&1; then
    _log WARN "Failed to connect to MariaDB (host=$PCLOUD_DB_HOST, db=$PCLOUD_DB_NAME) - DB tracking disabled"
    PCLOUD_ENABLE_DB=0
    return 1
  fi
  
  # Check if tables exist
  local table_count; table_count=$(_mysql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$PCLOUD_DB_NAME' AND table_name='backup_runs'" 2>/dev/null || echo "0")
  
  if [[ "$table_count" == "0" ]]; then
    _log WARN "Table backup_runs not found in database $PCLOUD_DB_NAME - run: mysql < sql/init_pcloud_db.sql"
    PCLOUD_ENABLE_DB=0
    return 1
  fi
  
  _log INFO "MariaDB connection OK (database: $PCLOUD_DB_NAME)"
}

# Log backup run start
_db_run_start() {
  [[ "${PCLOUD_ENABLE_DB}" != "1" ]] && return 0
  
  local snapshot="$1"
  RUN_ID="$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$(date +%s)-$$")"
  
  _mysql "INSERT INTO backup_runs (run_id, snapshot_name, status, started_at) VALUES ('${RUN_ID}', '${snapshot}', 'RUNNING', NOW());" || {
    _log WARN "Failed to log run start to database"
    return 1
  }
  
  export RUN_ID
  _log INFO "Run ID: $RUN_ID"
}

# Log backup run end
_db_run_end() {
  [[ "${PCLOUD_ENABLE_DB}" != "1" || -z "$RUN_ID" ]] && return 0
  
  local status="$1"
  local error_msg="${2:-}"
  
  # Escape single quotes for SQL
  error_msg="${error_msg//\'/\'\\''}"
  
  _mysql "UPDATE backup_runs SET status='${status}', finished_at=NOW(), duration_sec=TIMESTAMPDIFF(SECOND, started_at, NOW()), error_message='${error_msg}' WHERE run_id='${RUN_ID}';" || {
    _log WARN "Failed to log run end to database"
    return 1
  }
}

# Log phase timing
_db_phase_log() {
  [[ "${PCLOUD_ENABLE_DB}" != "1" || -z "$RUN_ID" ]] && return 0
  
  local phase="$1"
  local action="${2:-start}"  # start/end
  local status="${3:-SUCCESS}"
  
  if [[ "$action" == "start" ]]; then
    _mysql "INSERT INTO backup_phases (run_id, phase_name, status, started_at) VALUES ('${RUN_ID}', '${phase}', 'RUNNING', NOW());" 2>/dev/null || true
  else
    _mysql "UPDATE backup_phases SET finished_at=NOW(), duration_sec=TIMESTAMPDIFF(SECOND, started_at, NOW()), status='${status}' WHERE run_id='${RUN_ID}' AND phase_name='${phase}' AND finished_at IS NULL;" 2>/dev/null || true
  fi
}

# Update run metrics (pass SQL SET clause fragments)
_db_update_metrics() {
  [[ "${PCLOUD_ENABLE_DB}" != "1" || -z "$RUN_ID" ]] && return 0
  
  local updates="$*"
  
  _mysql "UPDATE backup_runs SET ${updates} WHERE run_id='${RUN_ID}';" 2>/dev/null || true
}

require_file(){
  [[ -f "$1" ]] || { _log ERROR "Datei fehlt: $1"; exit 2; }
}

validate_inputs_or_exit() {
  require_file "$ENV_FILE"
  # Zielpfad prüfen/normalisieren
  if [[ -z "${PCLOUD_DEST:-}" || "${PCLOUD_DEST:0:1}" != "/" ]]; then
    _log ERROR "Ungültiger PCLOUD_DEST (muss mit / beginnen): '${PCLOUD_DEST:-<leer>}'"
    exit 2
  fi
  PCLOUD_DEST="${PCLOUD_DEST%/}"
  export PCLOUD_DEST
}

last_snapshot_mtime() {
  local latest_dir; latest_dir="$(readlink -f "${RTB}/latest" 2>/dev/null || true)"
  [[ -z "$latest_dir" ]] && echo 0 && return
  stat -c %Y "$latest_dir" 2>/dev/null || echo 0
}

# --- Preflight: liefert Status "OK|OVERQUOTA|DOWN: <reason>", keine Policy hier ---
preflight_or_mark_down() {
  "${PY}" - <<'PY'
import os, sys, json, traceback
sys.path.insert(0, os.environ.get("MAIN_DIR","/opt/apps/pcloud-tools/main"))
try:
    import pcloud_bin_lib as pc
except Exception as e:
    print(f"DOWN: Import error: {e}"); sys.exit(0)

try:
    cfg = pc.effective_config(env_file=os.environ.get("ENV_FILE"))

    # 1) Auth/Token + Quota via REST (pc._rest_get prüft bereits result==0)
    ui = pc._rest_get(cfg, "userinfo", {"getauth": 1})
    
    info = ui.get("userinfo") or {}
    used = int(info.get("usedquota") or 0)
    quota = int(info.get("quota") or 0)
    if quota and used >= quota:
        print("OVERQUOTA"); sys.exit(0)

    # 2) Reachability via listfolder('/')
    pc._rest_get(cfg, "listfolder", {"path": "/", "nofiles": 1, "showpath": 1})

    print("OK")
except Exception as e:
    # Sanitizing: Newlines entfernen für stabiles Bash-Parsing
    err_msg = str(e).replace('\n', ' ').strip()
    print(f"DOWN: {type(e).__name__}: {err_msg}")
PY
}

# --- Remote Snapshot Listing (Python/REST) ---
load_remote_snapshots() {
  "${PY}" - <<'PY'
import os, sys, json
sys.path.insert(0, os.environ.get("MAIN_DIR","/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc

cfg = pc.effective_config(env_file=os.environ.get("ENV_FILE"))
snap_root = f"{pc._norm_remote_path(os.environ.get('PCLOUD_DEST','/Backup/rtb_1to1')).rstrip('/')}/_snapshots"

# listfolder auf snap_root
try:
    js = pc._rest_get(cfg, "listfolder", {"path": snap_root, "nofiles": 1})
except Exception:
    # API down → wie "leer" behandeln (Preflight filtert solche Fälle bereits)
    print("")
    raise SystemExit(0)

if int(js.get("result", -1)) != 0:
    # Ordner existiert evtl. noch nicht: leer zurückgeben
    print("")
    raise SystemExit(0)

names = []
for c in (js.get("metadata") or {}).get("contents", []) or []:
    if c.get("isfolder") and c.get("name") != "_index":
        snapname = c["name"]
        # Prüfe ob Upload vollständig (.upload_complete Marker vorhanden)
        marker_path = f"{snap_root}/{snapname}/.upload_complete"
        if pc.upload_complete_matches_snapshot(cfg, marker_path, snapname):
            names.append(snapname)
for n in sorted(names):
    print(n)
PY
}

remote_has_snapshots() {
  local out; out="$(load_remote_snapshots || true)"
  [[ -n "$out" ]] && echo YES || echo NO
}

remote_snapshot_exists() {
  local snapname="$1"
  local marker_path="${PCLOUD_DEST}/_snapshots/${snapname}/.upload_complete"

  # YES = Marker da | NO = fehlt/ungueltig | ERR:... = API/Netzwerk (nicht „fehlt“)
  local result
  result=$(MARKER_PATH="$marker_path" SNAPNAME="$snapname" "${PY}" - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("MAIN_DIR","/opt/apps/pcloud-tools/main"))
import pcloud_bin_lib as pc

def _emit_err(exc: BaseException) -> None:
    msg = str(exc).replace("\n", " ").replace("\t", " ")[:240]
    print(f"ERR:{type(exc).__name__}: {msg}")

try:
    cfg = pc.effective_config(env_file=os.environ.get("ENV_FILE"))
    marker_path = os.environ.get("MARKER_PATH")
    snapname = os.environ.get("SNAPNAME", "")
    if pc.upload_complete_matches_snapshot(cfg, marker_path, snapname):
        print("YES")
    else:
        print("NO")
except Exception as e:
    _emit_err(e)
PY
)
  echo "$result"
}

# Nach build_and_push: complete | missing | api_unreachable
confirm_remote_upload_complete() {
  local snapname="$1"
  local max_attempts="${PCLOUD_MARKER_VERIFY_RETRIES:-5}"
  local delay="${PCLOUD_MARKER_VERIFY_RETRY_SEC:-2}"
  local attempt=1
  local result=""

  while [[ $attempt -le $max_attempts ]]; do
    result="$(remote_snapshot_exists "$snapname")"
    case "$result" in
      YES)
        echo "complete"
        return 0
        ;;
      NO)
        echo "missing"
        return 0
        ;;
      ERR:*)
        if [[ $attempt -lt $max_attempts ]]; then
          _log WARN "pCloud API nicht erreichbar (.upload_complete-Check ${snapname}, ${attempt}/${max_attempts}): ${result#ERR:}"
          sleep "$delay"
          if [[ $delay -lt 30 ]]; then delay=$(( delay * 2 )); fi
          attempt=$(( attempt + 1 ))
        else
          _log WARN "pCloud API nach ${max_attempts} Versuchen nicht erreichbar (${snapname}): ${result#ERR:}"
          _log WARN "Upload/Inline-Validation war erfolgreich; Marker-Check uebersprungen (kein False-FAIL)."
          echo "api_unreachable"
          return 0
        fi
        ;;
      *)
        _log WARN "Unerwartete Antwort remote_snapshot_exists (${snapname}): ${result}"
        echo "api_unreachable"
        return 0
        ;;
    esac
  done
}

# 0 = OK (Marker da oder API nach Retries unklar), 1 = Marker wirklich fehlt
_handle_upload_complete_check() {
  local snapname="$1"
  local verdict
  verdict="$(confirm_remote_upload_complete "$snapname")"
  case "$verdict" in
    complete) return 0 ;;
    api_unreachable) return 0 ;;
    missing)
      _log ERROR "Upload von ${snapname} scheinbar fertig, aber .upload_complete fehlt auf pCloud!"
      return 1
      ;;
    *)
      _log WARN "Upload von ${snapname}: unbekannter Marker-Check-Status: ${verdict}"
      return 0
      ;;
  esac
}

local_snapshot_names() {
  find "$RTB" -maxdepth 1 -type d -printf '%f\n' \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-' \
  | sort
}

remote_snapshot_names() { load_remote_snapshots; }

# Cached check (nutzt bereits geladenes remote_snaps Array)
# Für Read-Operationen: Vermeidet Python-Prozess-Start
is_remote_cached() {
  local snapname="$1"
  printf '%s\n' "${remote_snaps[@]}" | grep -qx "$snapname" && echo "YES" || echo "NO"
}

# --- Hinweis: Gap-/Chain-Logik und Retention-Sync entfallen im Pool-Modell. ---
# Snapshots sind eigenstaendig (Stubs -> dedupliziter Pool), es gibt keine Chain,
# die "brechen" koennte; ein fehlender Nachbar-Snapshot beschaedigt keine anderen.
# Catch-up = "lade jeden lokalen Snapshot, der remote (noch) kein .upload_complete hat".
# Platzfreigabe geloeschter Snapshots uebernimmt spaeter der Pool-GC (pcloud_pool_gc.py).

build_and_push() {
  local SNAP="$1" SNAPNAME; SNAPNAME="$(basename "$SNAP")"
  _log INFO "Uploading snapshot: $SNAPNAME"

  # DB-Tracking: ein Run pro Snapshot (wichtig für Bootstrap/Kettenläufe)
  _db_run_start "$SNAPNAME" "$SNAP"

  _db_fail_and_return() {
    local msg="$1"
    _db_run_end FAILED "$msg"
    RUN_ID=""
    return 1
  }

  # === Manifest: Deterministischer Filename (ohne PID für Resume/Reuse) ===
  local mani="${PCLOUD_TEMP_DIR}/pcloud_mani.${SNAPNAME}.json"
  local mani_jsonl="${mani}.tmp.jsonl"
  local manifest_exists=0
  local manifest_incomplete=0

  # Prüfe ob Manifest bereits vollständig vorhanden
  if [[ -f "$mani" ]]; then
    # Manifest existiert - prüfe ob gültig
    if jq -e '.items' "$mani" >/dev/null 2>&1; then
      manifest_exists=1
      _log INFO "✓ Verwende existierendes Manifest: $(basename "$mani")"
    else
      _log INFO "⚠ Manifest existiert aber ist ungültig - neu generieren"
      rm -f "$mani"
    fi
  elif [[ -f "$mani_jsonl" ]]; then
    # JSONL-Checkpoint vorhanden - unvollständige Generierung
    manifest_incomplete=1
    local jsonl_lines=$(wc -l < "$mani_jsonl" 2>/dev/null || echo 0)
    _log INFO "⚠ Unvollständige Manifest-Generierung erkannt (${jsonl_lines} Items) - setze fort"
  fi

  local T0=$(date +%s)
  
  # Nur neu generieren wenn nötig
  if [[ $manifest_exists -eq 0 ]]; then
    _db_phase_log "manifest" "start"
    
    # Smart-Mode: Referenz-Manifest per mtime/size-Deckung waehlen
    local MANIFEST_MODE="${PCLOUD_MANIFEST_MODE:-smart}"  # smart|full
    local ref_manifest_arg=""
    
    if [[ "$MANIFEST_MODE" == "smart" ]]; then
      local ref_manifest=""
      if [[ -n "${PCLOUD_MANIFEST_REF:-}" && -f "${PCLOUD_MANIFEST_REF}" ]]; then
        ref_manifest="${PCLOUD_MANIFEST_REF}"
        _log INFO "Manifest: Smart-Mode mit Referenz $(basename "$ref_manifest") (PCLOUD_MANIFEST_REF)"
      else
        ref_manifest="$("${PY}" "$MANI" --pick-ref-manifest \
          --root "$SNAP" --snapshot "$SNAPNAME" \
          --manifests-dir "${PCLOUD_ARCHIVE_DIR}/manifests" 2>>"$PCLOUD_LOG")"
      fi
      if [[ -n "$ref_manifest" && -f "$ref_manifest" ]]; then
        ref_manifest_arg="--ref-manifest $ref_manifest"
        if [[ -z "${PCLOUD_MANIFEST_REF:-}" ]]; then
          _log INFO "Manifest: Smart-Mode mit Referenz $(basename "$ref_manifest")"
        fi
      else
        _log INFO "Manifest: Full-Mode (kein passendes Referenz-Manifest)"
      fi
    else
      _log INFO "Manifest: Full-Mode (PCLOUD_MANIFEST_MODE=full)"
    fi
    
    "${PY}" "$MANI" --root "$SNAP" --snapshot "$SNAPNAME" --out "$mani" --hash sha256 $ref_manifest_arg || {
      _db_phase_log "manifest" "end" "FAILED"
      _db_fail_and_return "manifest_generation_failed"
    }
    
    local manifest_duration=$(( $(date +%s) - T0 ))
    _db_phase_log "manifest" "end" "SUCCESS"
    _db_update_metrics "manifest_duration_sec = $manifest_duration"
    [[ "${PCLOUD_TIMING:-0}" == "1" ]] && _log INFO "Manifest done (${manifest_duration}s)"
  else
    _log INFO "Manifest-Generierung übersprungen (bereits vorhanden)"
  fi

  # Upload phase (KEIN --retention-sync; Retention: pcloud_pool_gc.py --retention-apply)
  T0=$(date +%s)
  _db_phase_log "upload" "start"
  
  # Upload-Modus: POOL (deduplizierter Pool + Stub-Snapshots)
  _log INFO "Upload-Modus: POOL (deduplizierter File-Pool)"
  
  "${PY}" "$PUSH" --manifest "$mani" --dest-root "$PCLOUD_DEST" --snapshot-mode pool --env-file "$ENV_FILE" "${EXTRA_PUSH_ARGS[@]}" || {
    _db_phase_log "upload" "end" "FAILED"
    # Temp-Manifest behalten: Retry ueberspringt Regenerierung, Diagnose via jq moeglich
    _db_fail_and_return "upload_failed"
  }
  
  local upload_duration=$(( $(date +%s) - T0 ))
  _db_phase_log "upload" "end" "SUCCESS"
  _db_update_metrics "upload_duration_sec = $upload_duration"
  [[ "${PCLOUD_TIMING:-0}" == "1" ]] && _log INFO "Upload done (${upload_duration}s)"

  # Manifest-Archivierung wird bereits vom Push-Tool erledigt
  # (nach /srv/pcloud-archive/manifests/)
  
  # === Integritaetscheck nach erfolgreichem Upload (im Dry-Run uebersprungen) ===
  if [[ "$DRY_RUN" == "1" ]]; then
    _log INFO "Integrity verification uebersprungen (--dry-run)"
  else
  _integrity_mode="${PCLOUD_POST_UPLOAD_INTEGRITY:-1}"
  case "${_integrity_mode,,}" in
    0|skip|off|false|no)
      _integrity_mode=skip
      ;;
    *)
      _integrity_mode=run
      ;;
  esac

  if [[ "$_integrity_mode" == "skip" ]]; then
    _log INFO "Post-upload integrity uebersprungen (PCLOUD_POST_UPLOAD_INTEGRITY=skip)"
  else
  _log INFO "Starting integrity verification (snapshot=$SNAPNAME)..."
  local integrity_report="${PCLOUD_ARCHIVE_DIR}/integrity/integrity_${SNAPNAME}_pending.json"

  T0=$(date +%s)
  _db_phase_log "verify" "start"

  local _integrity_ok=0
  local _integrity_args=(
    --env-file "$ENV_FILE"
    --pool-root "$PCLOUD_DEST"
    --snapshot "$SNAPNAME"
    --check-type post_upload
    --json-out "$integrity_report"
  )
  if [[ -n "${RUN_ID:-}" ]]; then
    _integrity_args+=(--backup-run-id "$RUN_ID")
  fi

  if "${PY}" "$INTEGRITY_RUN" "${_integrity_args[@]}" 2>&1 | tee -a "$PCLOUD_LOG"; then
    _integrity_ok=1
  fi

  local verify_duration=$(( $(date +%s) - T0 ))
  if [[ $_integrity_ok -eq 1 ]]; then
    _db_phase_log "verify" "end" "SUCCESS"
    _db_update_metrics "verify_duration_sec = $verify_duration"
    _log INFO "Integrity check OK (${verify_duration}s)"
  else
    _db_phase_log "verify" "end" "FAILED"
    _db_update_metrics "verify_duration_sec = $verify_duration"
    _log WARN "Integrity check FAILED (non-critical, upload succeeded) — siehe snapshot_integrity_checks / $integrity_report"
  fi
  fi
  fi
  # === Ende Integritaetscheck ===
  
  # Explizites Cleanup (statt trap RETURN)
  rm -f "$mani" "$mani_jsonl" 2>/dev/null || true

  # Snapshot-Run erfolgreich abschließen
  _db_run_end SUCCESS 0
  RUN_ID=""
}

# ========= Start =========
# Optionaler Direktaufruf:
#   wrapper_pcloud_pool_sync_1to1.sh [SNAPSHOT|/pfad/zu/SNAPSHOT] [--dry-run]
# Flags werden whitelisted und sicher (Array) an das Push-Tool weitergereicht.
TARGET_SNAPSHOT=""
DRY_RUN=0
declare -a EXTRA_PUSH_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      EXTRA_PUSH_ARGS+=("$1")
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage:
  $0 [SNAPSHOT|/path/to/SNAPSHOT] [--dry-run]

Examples:
  $0
  $0 2026-04-27-173201 --dry-run

Notes:
  --dry-run wird an pcloud_push_json_pool_manifest_to_pcloud.py durchgereicht.
EOF
      exit 0
      ;;
    -*)
      _log ERROR "Unknown option: $1"
      exit 2
      ;;
    *)
      if [[ -n "$TARGET_SNAPSHOT" ]]; then
        _log ERROR "Only one snapshot argument allowed (got: '$TARGET_SNAPSHOT' and '$1')"
        exit 2
      fi
      TARGET_SNAPSHOT="$1"
      shift
      ;;
  esac
done

if [[ -n "$TARGET_SNAPSHOT" ]]; then
  # Falls Pfad/Symlink übergeben wurde, auf echten Snapshot-Namen normalisieren
  # (wichtig für Aufrufe wie: /mnt/backup/rtb_nas/latest)
  _target_candidate="$TARGET_SNAPSHOT"
  if [[ "$TARGET_SNAPSHOT" == "latest" ]]; then
    _target_candidate="${RTB}/latest"
  fi

  if [[ -e "$_target_candidate" || -L "$_target_candidate" ]]; then
    _resolved_target="$(readlink -f "$_target_candidate" 2>/dev/null || true)"
    if [[ -n "$_resolved_target" ]]; then
      TARGET_SNAPSHOT="$(basename "$_resolved_target")"
    else
      TARGET_SNAPSHOT="$(basename "$_target_candidate")"
    fi
  fi

  if [[ ! "$TARGET_SNAPSHOT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}- ]]; then
    _log ERROR "Invalid snapshot argument: '$TARGET_SNAPSHOT'"
    exit 2
  fi
  _log INFO "Target snapshot mode active: $TARGET_SNAPSHOT"
fi

if [[ ${#EXTRA_PUSH_ARGS[@]} -gt 0 ]]; then
  _log INFO "Extra push args: ${EXTRA_PUSH_ARGS[*]}"
fi

# Lock holen (mit Timeout) – überspringen wenn bereits von rtb_wrapper gehalten
if [[ "${BACKUP_PIPELINE_LOCKED:-0}" != "1" ]]; then
  exec 9>"$LOCKFILE"
  if ! flock -w "$WAIT_SEC" 9; then
    _log WARN "Konnte Lock innerhalb ${WAIT_SEC}s nicht bekommen"
    exit 0
  fi
fi

if declare -F apply_oom_score_adj &>/dev/null; then
  apply_oom_score_adj "${PCLOUD_OOM_SCORE_ADJ:--500}"
  _log INFO "OOM-Schutz: oom_score_adj=${PCLOUD_OOM_SCORE_ADJ:--500} (Upload-Prozess geschützt)"
fi

_log INFO "========== pCloud Sync 1to1 Start =========="

validate_inputs_or_exit

# Dry-Run darf die DB NIE anfassen: backup_runs trackt nur echte Backups.
# Alle _db_*-Funktionen sind hinter PCLOUD_ENABLE_DB gated -> hier global aus.
if [[ "$DRY_RUN" == "1" && "${PCLOUD_ENABLE_DB}" == "1" ]]; then
  PCLOUD_ENABLE_DB=0
  _log INFO "Dry-Run: DB-Tracking deaktiviert (kein Schreiben in backup_runs)"
fi

# Initialize database
_db_init

# Trap for cleanup on exit (preserve original non-zero exit code)
trap '_rc=$?; _db_run_end FAILED "$_rc" "Script interrupted or failed"; exit "$_rc"' INT TERM ERR

# Preflight (Status) + Policy im Wrapper
PF="DOWN"
attempt=1
while (( attempt <= PCLOUD_PREFLIGHT_RETRIES )); do
  PF="$(preflight_or_mark_down)"
  if [[ "$PF" == "OK" ]]; then
    _log INFO "pCloud Preflight: OK"
    break
  fi

  if (( attempt < PCLOUD_PREFLIGHT_RETRIES )); then
    _log WARN "pCloud Preflight attempt ${attempt}/${PCLOUD_PREFLIGHT_RETRIES}: ${PF} (retry in ${PCLOUD_PREFLIGHT_RETRY_DELAY_SEC}s)"
    sleep "$PCLOUD_PREFLIGHT_RETRY_DELAY_SEC"
  fi
  attempt=$((attempt + 1))
done

case "$PF" in
  OK)        ;;
  OVERQUOTA) _log WARN "pCloud Preflight: Konto über Quota – Sync wird übersprungen."; exit 0 ;;
  DOWN*)     _log WARN "pCloud Preflight fehlgeschlagen: $PF – Sync wird übersprungen (nach ${PCLOUD_PREFLIGHT_RETRIES} Versuch(en))."; exit 0 ;;
  *)         _log WARN "pCloud Preflight: unbekannter Status '$PF' – Sync wird übersprungen (nach ${PCLOUD_PREFLIGHT_RETRIES} Versuch(en))."; exit 0 ;;
esac

# Safety-Delay nach RTB
if [[ -L "${RTB}/latest" || -d "${RTB}/latest" ]]; then
  latest_dir="$(readlink -f "${RTB}/latest" 2>/dev/null || echo "")"
  if [[ -n "$latest_dir" && -d "$latest_dir" ]]; then
    now=$(date +%s); lm=$(stat -c '%Y' "$latest_dir" 2>/dev/null || echo 0)
    if (( lm > 0 && now - lm < SAFETY_DELAY_SEC )); then
      wait=$(( SAFETY_DELAY_SEC - (now - lm) ))
      _log INFO "Safety-delay ${wait}s (waiting after RTB)"
      sleep "$wait"
    fi
  fi
fi

# Bootstrap (remote leer - Initial Sync)
if [[ "$(remote_has_snapshots)" == "NO" ]]; then
  _log INFO "Bootstrap: Remote empty – uploading all local snapshots (initial sync)"
  mapfile -t SNAPS < <(find "$RTB" -maxdepth 1 -type d -printf '%f\n' | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-' | sort)
  if [[ ${#SNAPS[@]} -eq 0 ]]; then
    _log WARN "No local snapshots found"
    exit 0
  fi
  if [[ -n "$TARGET_SNAPSHOT" ]]; then
    if ! printf '%s\n' "${SNAPS[@]}" | grep -qx "$TARGET_SNAPSHOT"; then
      _log ERROR "Target snapshot not found locally: $TARGET_SNAPSHOT"
      exit 2
    fi
    # Filter: Keep all snapshots up to and including the target
    # This ensures a clean chain when starting from scratch
    filtered=()
    for s in "${SNAPS[@]}"; do
      filtered+=("$s")
      [[ "$s" == "$TARGET_SNAPSHOT" ]] && break
    done
    SNAPS=("${filtered[@]}")
    _log INFO "Bootstrap target filter active: uploading all snapshots up to $TARGET_SNAPSHOT (${#SNAPS[@]} total)"
  fi
  export PCLOUD_SKIP_FINALIZE=1
  for s in "${SNAPS[@]}"; do
    build_and_push "$RTB/$s" || exit 1
    # Harte Verifikation: Marker muss auf pCloud sein (im Dry-Run uebersprungen)
    if [[ "$DRY_RUN" != "1" ]] && ! _handle_upload_complete_check "$s"; then
      _log ERROR "Markiere als FAILED."
      exit 1
    fi
  done
  # (Kein finalize_index_fileids noetig: der Pool-Index ist bereits enriched mit fileid/hash.)
  _log INFO "Bootstrap completed successfully (folder template will be auto-created by pcloud_push)"
  exit 0
fi

# === Sync-Check (Pool-Modell): jeden lokalen Snapshot ohne remote .upload_complete laden ===
# Pool-Snapshots sind eigenstaendig (keine Ketten-Abhaengigkeit). Catch-up:
# - latest zuerst (frisch erstellter Snapshot hat Prioritaet)
# - bei Fehler eines Backlog-Snapshots weitermachen (kein Blockieren juengerer)
# - TARGET_SNAPSHOT / --upload-only: nur diesen einen, Fehler = harter Abbruch
_log INFO "Checking for missing snapshots..."
uploaded_count=0
catchup_failed=0

mapfile -t local_snaps < <(local_snapshot_names)
mapfile -t remote_snaps < <(remote_snapshot_names)   # nur Snapshots MIT .upload_complete

if [[ -n "$TARGET_SNAPSHOT" ]]; then
  if ! printf '%s\n' "${local_snaps[@]}" | grep -qx "$TARGET_SNAPSHOT"; then
    _log ERROR "Target snapshot not found locally: $TARGET_SNAPSHOT"
    exit 2
  fi
fi

missing_snaps=()
for s in "${local_snaps[@]}"; do
  [[ -n "$TARGET_SNAPSHOT" && "$s" != "$TARGET_SNAPSHOT" ]] && continue
  [[ "$(is_remote_cached "$s")" == "YES" ]] && continue
  missing_snaps+=("$s")
done

# Ohne explizites Target: latest vor restlichem Backlog (chronologisch)
if [[ -z "$TARGET_SNAPSHOT" && ${#missing_snaps[@]} -gt 1 ]]; then
  _latest_name=""
  _latest_dir="$(readlink -f "${RTB}/latest" 2>/dev/null || true)"
  if [[ -n "$_latest_dir" && -d "$_latest_dir" ]]; then
    _latest_name="$(basename "$_latest_dir")"
  fi
  if [[ -n "$_latest_name" ]]; then
    _ordered=()
    for s in "${missing_snaps[@]}"; do
      [[ "$s" == "$_latest_name" ]] && _ordered+=("$s")
    done
    for s in "${missing_snaps[@]}"; do
      [[ "$s" == "$_latest_name" ]] && continue
      _ordered+=("$s")
    done
    if [[ ${#_ordered[@]} -eq ${#missing_snaps[@]} ]]; then
      missing_snaps=("${_ordered[@]}")
      _log INFO "Catch-up order: latest-first ($_latest_name), dann ${#missing_snaps[@]} Snapshot(s)"
    fi
  fi
fi

for s in "${missing_snaps[@]}"; do
  _log INFO "Uploading missing snapshot: $s"
  if ! build_and_push "$RTB/$s"; then
    _log ERROR "Upload von $s fehlgeschlagen"
    if [[ -n "$TARGET_SNAPSHOT" ]]; then
      exit 1
    fi
    catchup_failed=1
    continue
  fi
  # Im Dry-Run wird nichts hochgeladen -> kein .upload_complete -> Verify ueberspringen
  if [[ "$DRY_RUN" != "1" ]] && ! _handle_upload_complete_check "$s"; then
    if [[ -n "$TARGET_SNAPSHOT" ]]; then
      exit 1
    fi
    catchup_failed=1
    continue
  fi
  uploaded_count=$((uploaded_count + 1))
done

if [[ $uploaded_count -eq 0 && $catchup_failed -eq 0 ]]; then
  _log INFO "All snapshots already on pCloud"
elif [[ $uploaded_count -gt 0 ]]; then
  _log INFO "Successfully uploaded $uploaded_count snapshot(s)"
  _db_update_metrics "new_snapshots = $uploaded_count"
fi

if [[ $catchup_failed -eq 1 ]]; then
  _log ERROR "Catch-up mit Fehlern beendet ($uploaded_count erfolgreich, $(( ${#missing_snaps[@]} - uploaded_count )) fehlgeschlagen/inkomplett)"
  exit 1
fi

# Cleanup: Alte Temp-Dateien löschen (>7 Tage)
if [[ -d "${PCLOUD_TEMP_DIR}" ]]; then
  find "${PCLOUD_TEMP_DIR}" -maxdepth 1 -type f \( -name "pcloud_mani.*.json" -o -name "pcloud_index_*.json" -o -name "delta*.json" \) -mtime +7 -delete 2>/dev/null || true
  _log INFO "Cleaned up old temp files (>7d) from ${PCLOUD_TEMP_DIR}"
fi

_log INFO "========== pCloud Sync 1to1 Complete =========="
