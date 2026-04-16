#!/usr/bin/env bash
set -euo pipefail

# ========= Basiskonfiguration =========
MAIN_DIR=${MAIN_DIR:-/opt/apps/pcloud-tools/main}
RTB=${RTB:-/mnt/backup/rtb_nas}

ENV_FILE=${ENV_FILE:-${MAIN_DIR}/.env}
PCLOUD_DEST=${PCLOUD_DEST:-/Backup/rtb_1to1}

MANI=${MANI:-${MAIN_DIR}/pcloud_json_manifest.py}
PUSH=${PUSH:-${MAIN_DIR}/pcloud_push_json_manifest_to_pcloud.py}
DELTA_CHECK=${DELTA_CHECK:-${MAIN_DIR}/pcloud_quick_delta.py}

# Python-Interpreter (venv bevorzugt)
if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif [[ -x "/opt/apps/pcloud-tools/venv/bin/python" ]]; then
  PY="/opt/apps/pcloud-tools/venv/bin/python"
else
  PY="${PY:-python3}"
fi

# Module auffindbar machen
export PYTHONPATH="${MAIN_DIR}:${PYTHONPATH:-}"

# Finalize: standardmäßig im Wrapper aus Performance-Gründen überspringen.
export PCLOUD_SKIP_FINALIZE=${PCLOUD_SKIP_FINALIZE:-1}

# Pretty-Print für JSON (Stubs + Index) - aus .env-File lesen falls vorhanden
if [[ -f "${ENV_FILE:-}" ]]; then
  while IFS='=' read -r key val; do
    # Kommentare und Leerzeilen überspringen
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    # PCLOUD_* vars exportieren
    if [[ "$key" =~ ^PCLOUD_ ]]; then
      # Quotes entfernen falls vorhanden
      val="${val%\"}"; val="${val#\"}"
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
SAFETY_DELAY_SEC=${SAFETY_DELAY_SEC:-120}

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

# --- Preflight: liefert Status "OK|OVERQUOTA|DOWN", keine Policy hier ---
preflight_or_mark_down() {
  "${PY}" - <<'PY'
import os, sys, json, traceback
sys.path.insert(0, os.environ.get("MAIN_DIR","/opt/apps/pcloud-tools/main"))
try:
    import pcloud_bin_lib as pc
except Exception:
    print("DOWN"); sys.exit(0)

try:
    cfg = pc.effective_config(env_file=os.environ.get("ENV_FILE"))
    dest_root = pc._norm_remote_path(os.environ.get("PCLOUD_DEST","/Backup/rtb_1to1"))

    # 1) Auth/Token + Quota via REST
    ui = pc._rest_get(cfg, "userinfo", {"getauth": 1})
    if int(ui.get("result", -1)) != 0:
        print("DOWN"); sys.exit(0)
    info = ui.get("userinfo") or {}
    used = int(info.get("usedquota") or 0)
    quota = int(info.get("quota") or 0)
    if quota and used >= quota:
        print("OVERQUOTA"); sys.exit(0)

    # 2) Reachability via listfolder('/')
    lf = pc._rest_get(cfg, "listfolder", {"path": "/", "nofiles": 1, "showpath": 1})
    if int(lf.get("result", -1)) != 0:
        print("DOWN"); sys.exit(0)

    print("OK")
except Exception:
    # Netzwerk/Timeout/etc.
    print("DOWN")
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
        names.append(c["name"])
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
  local out; out="$(load_remote_snapshots || true)"
  grep -qx "$snapname" <<<"$out" && echo YES || echo NO
}

local_snapshot_names() {
  find "$RTB" -maxdepth 1 -type d -printf '%f\n' \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-' \
  | sort
}

remote_snapshot_names() { load_remote_snapshots; }

need_retention_sync() {
  local locals remotes s remote_only=""
  locals="$(local_snapshot_names | sort -u)"
  remotes="$(remote_snapshot_names | sort -u)"
  while IFS= read -r s; do
    [[ -z "$s" ]] && continue
    if ! grep -qxF "$s" <<<"$locals"; then
      remote_only+="$s"$'\n'
    fi
  done <<<"$remotes"
  [[ -n "$remote_only" ]] && echo YES || echo NO
}

build_and_push() {
  local SNAP="$1" SNAPNAME; SNAPNAME="$(basename "$SNAP")"
  _log INFO "Uploading snapshot: $SNAPNAME"
  
  # Log run start (only once per wrapper invocation)
  if [[ -z "$RUN_ID" ]]; then
    _db_run_start "$SNAPNAME" "$SNAP"
  fi

  # Manifest im PCLOUD_TEMP_DIR erstellen (statt system /tmp)
  local mani; mani="${PCLOUD_TEMP_DIR}/pcloud_mani.${SNAPNAME}.$$.json"
  # TRAP ENTFERNT: Würde Manifest bei jedem Fehler löschen (FileNotFoundError!)  
  # Cleanup erfolgt explizit am Ende

  local T0=$(date +%s)
  _db_phase_log "manifest" "start"
  
  # Smart-Mode: Auto-detect letztes Manifest als Referenz (Schema v3)
  local MANIFEST_MODE="${PCLOUD_MANIFEST_MODE:-smart}"  # smart|full
  local ref_manifest_arg=""
  
  if [[ "$MANIFEST_MODE" == "smart" ]]; then
    # Suche letztes Manifest im manifests/-Unterordner (Push-Tool archiviert dort)
    local last_manifest
    last_manifest="$(find "${PCLOUD_ARCHIVE_DIR}/manifests" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort -r | head -n1)"
    
    if [[ -n "$last_manifest" && -f "$last_manifest" ]]; then
      ref_manifest_arg="--ref-manifest $last_manifest"
      _log INFO "Manifest: Smart-Mode mit Referenz $(basename "$last_manifest")"
    else
      _log INFO "Manifest: Full-Mode (kein Referenz-Manifest)"
    fi
  else
    _log INFO "Manifest: Full-Mode (PCLOUD_MANIFEST_MODE=full)"
  fi
  
  "${PY}" "$MANI" --root "$SNAP" --snapshot "$SNAPNAME" --out "$mani" --hash sha256 $ref_manifest_arg || {
    _db_phase_log "manifest" "end" "FAILED"
    return 1
  }
  
  local manifest_duration=$(( $(date +%s) - T0 ))
  _db_phase_log "manifest" "end" "SUCCESS"
  _db_update_metrics "manifest_duration_sec = $manifest_duration"
  [[ "${PCLOUD_TIMING:-0}" == "1" ]] && _log INFO "Manifest done (${manifest_duration}s)"

  local RET=""
  [[ "$(need_retention_sync)" == "YES" ]] && RET="--retention-sync"

  # Delta-Copy-Flag (PoC)
  local DELTA_FLAG=""
  [[ "${PCLOUD_USE_DELTA_COPY:-0}" == "1" ]] && DELTA_FLAG="--use-delta-copy"

  # Upload phase
  T0=$(date +%s)
  _db_phase_log "upload" "start"
  
  # Log-Hinweis bei Delta-Copy
  if [[ "${PCLOUD_USE_DELTA_COPY:-0}" == "1" ]]; then
    _log INFO "Upload-Modus: Delta-Copy (Server-Side Clone + Selective Update)"
  else
    _log INFO "Upload-Modus: Full-Mode (alle Dateien/Stubs neu schreiben)"
  fi
  
  "${PY}" "$PUSH" --manifest "$mani" --dest-root "$PCLOUD_DEST" --snapshot-mode 1to1 $RET $DELTA_FLAG --env-file "$ENV_FILE" || {
    _db_phase_log "upload" "end" "FAILED"
    rm -f "$mani" 2>/dev/null || true
    return 1
  }
  
  local upload_duration=$(( $(date +%s) - T0 ))
  _db_phase_log "upload" "end" "SUCCESS"
  _db_update_metrics "upload_duration_sec = $upload_duration"
  [[ "${PCLOUD_TIMING:-0}" == "1" ]] && _log INFO "Upload done (${upload_duration}s)"

  # Manifest-Archivierung wird bereits vom Push-Tool erledigt
  # (nach /srv/pcloud-archive/manifests/)
  
  # === Delta-Check nach erfolgreichem Upload ===
  _log INFO "Starting delta verification..."
  local delta_report="${PCLOUD_TEMP_DIR}/delta_verify_${SNAPNAME}.json"
  
  T0=$(date +%s)
  _db_phase_log "verify" "start"
  
  if "${PY}" "$DELTA_CHECK" \
    --dest-root "$PCLOUD_DEST" \
    --env-file "$ENV_FILE" \
    --json-out "$delta_report" 2>&1 | tee -a "$PCLOUD_LOG"; then
    
    local verify_duration=$(( $(date +%s) - T0 ))
    _db_phase_log "verify" "end" "SUCCESS"
    _db_update_metrics "verify_duration_sec = $verify_duration"
    _log INFO "Delta-Check successful (${verify_duration}s)"
    
    # Delta-Report archivieren
    if [[ -f "$delta_report" ]]; then
      mv "$delta_report" "${PCLOUD_ARCHIVE_DIR}/deltas/" 2>/dev/null || true
      _log INFO "Delta report archived: delta_verify_${SNAPNAME}.json"
    fi
  else
    local verify_duration=$(( $(date +%s) - T0 ))
    _db_phase_log "verify" "end" "FAILED"
    _log WARN "Delta-Check failed (non-critical, upload succeeded)"
  fi
  # === Ende Delta-Check ===
  
  # Explizites Cleanup (statt trap RETURN)
  rm -f "$mani" 2>/dev/null || true
}

# ========= Start =========
# Lock holen (mit Timeout) – überspringen wenn bereits von rtb_wrapper gehalten
if [[ "${BACKUP_PIPELINE_LOCKED:-0}" != "1" ]]; then
  exec 9>"$LOCKFILE"
  if ! flock -w "$WAIT_SEC" 9; then
    _log WARN "Konnte Lock innerhalb ${WAIT_SEC}s nicht bekommen"
    exit 0
  fi
fi

_log INFO "========== pCloud Sync 1to1 Start =========="

validate_inputs_or_exit

# Initialize database
_db_init

# Trap for cleanup on exit
trap '_db_run_end FAILED $? "Script interrupted or failed"; exit' INT TERM ERR

# Preflight (Status) + Policy im Wrapper
PF="$(preflight_or_mark_down)"
case "$PF" in
  OK)        _log INFO "pCloud Preflight: OK" ;;
  OVERQUOTA) _log WARN "pCloud Preflight: Konto über Quota – Sync wird übersprungen."; exit 0 ;;
  DOWN)      _log WARN "pCloud Preflight: API/Auth nicht erreichbar – Sync wird übersprungen."; exit 0 ;;
  *)         _log WARN "pCloud Preflight: unbekannter Status '$PF' – Sync wird übersprungen."; exit 0 ;;
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

# Bootstrap (remote leer)
if [[ "$(remote_has_snapshots)" == "NO" ]]; then
  _log INFO "Bootstrap: Remote empty – backfilling all local snapshots (old → new)"
  mapfile -t SNAPS < <(find "$RTB" -maxdepth 1 -type d -printf '%f\n' | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}-' | sort)
  if [[ ${#SNAPS[@]} -eq 0 ]]; then
    _log WARN "No local snapshots found"
    _db_run_end SUCCESS 0
    exit 0
  fi
  export PCLOUD_SKIP_FINALIZE=1
  for s in "${SNAPS[@]}"; do
    build_and_push "$RTB/$s"
  done
  # einmaliges Finalize
  "${PY}" - <<'PY'
import os
import pcloud_bin_lib as pc
from pcloud_push_json_manifest_to_pcloud import finalize_index_fileids
cfg = pc.effective_config(env_file=os.environ.get("ENV_FILE"))
dest_root = os.environ.get("PCLOUD_DEST","/Backup/rtb_1to1")
snapshots_root = f"{pc._norm_remote_path(dest_root).rstrip('/')}/_snapshots"
fixed = finalize_index_fileids(cfg, snapshots_root)
print(f"[finalize] index fileids fixed={fixed}")
PY
  _db_run_end SUCCESS 0
  _log INFO "Bootstrap/backfill completed successfully"
  exit 0
fi

# Intelligentes Gap-Backfilling (statt nur latest)
_log INFO "Checking for missing snapshots..."
uploaded_count=0

for s in $(local_snapshot_names); do
  if [[ "$(remote_snapshot_exists "$s")" == "NO" ]]; then
    _log WARN "Gap detected: Snapshot $s missing remote – backfilling..."
    build_and_push "$RTB/$s" || {
      _db_run_end FAILED 1 "Gap backfill failed for $s"
      exit 1
    }
    uploaded_count=$((uploaded_count + 1))
  fi
done

if [[ $uploaded_count -eq 0 ]]; then
  _log INFO "All snapshots already on pCloud"
else
  _log INFO "Successfully uploaded $uploaded_count snapshot(s)"
  _db_update_metrics "gaps_backfilled = $uploaded_count"
fi

# Cleanup: Alte Temp-Dateien löschen (>7 Tage)
if [[ -d "${PCLOUD_TEMP_DIR}" ]]; then
  find "${PCLOUD_TEMP_DIR}" -maxdepth 1 -type f \( -name "pcloud_mani.*.json" -o -name "pcloud_index_*.json" -o -name "delta*.json" \) -mtime +7 -delete 2>/dev/null || true
  _log INFO "Cleaned up old temp files (>7d) from ${PCLOUD_TEMP_DIR}"
fi

# Success!
_db_run_end SUCCESS 0
_log INFO "========== pCloud Sync 1to1 Complete =========="
