#!/usr/bin/env bash
# =====================================================
# Reports Generator - DB → reports.json
# =====================================================
# Purpose: Query MariaDB pcloud_backup database and write
#          structured JSON for dashboard consumption.
#
# Output Location:
#   /opt/apps/monitoring/reports.json (default)
#   Override with: REPORTS_OUTPUT=/path/to/reports.json
#
# DB Configuration (override via environment):
#   DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
#   Or use DB_DEFAULTS_FILE (MySQL defaults-file)
#
# Usage:
#   ./generate_reports.sh [--verbose]
#
# Systemd: Triggered by monitoring-reports.timer (every 15min)
# =====================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Output configuration
REPORTS_OUTPUT="${REPORTS_OUTPUT:-/opt/apps/monitoring/reports.json}"
VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

# Load pCloud DB config from /opt/apps/pcloud-tools/main/.env
PCLOUD_ENV="/opt/apps/pcloud-tools/main/.env"
if [[ -f "$PCLOUD_ENV" ]]; then
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    if [[ "$key" =~ ^PCLOUD_DB_ ]]; then
      # Strip inline comments ONLY if # is preceded by whitespace
      val=$(echo "$val" | sed 's/[[:space:]]#.*//')
      
      # Trim leading/trailing whitespace
      val=$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
      
      # Remove surrounding quotes if present
      if [[ "$val" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
      elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
      fi
      
      export "${key}=${val}"
    fi
  done < "$PCLOUD_ENV"
fi

# Load EntropyWatcher DB config from /opt/apps/entropywatcher/config/common.env
EW_ENV="/opt/apps/entropywatcher/config/common.env"
if [[ -f "$EW_ENV" ]]; then
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    if [[ "$key" =~ ^DB_ ]]; then
      # Strip inline comments ONLY if # is preceded by whitespace
      # This preserves # characters that are part of the password
      val=$(echo "$val" | sed 's/[[:space:]]#.*//')
      
      # Trim leading/trailing whitespace
      val=$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
      
      # Remove surrounding quotes if present (single pass, safe)
      if [[ "$val" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
      elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
      fi
      
      # Map to EW_DB_* variables
      case "$key" in
        DB_HOST) export EW_DB_HOST="$val" ;;
        DB_PORT) export EW_DB_PORT="$val" ;;
        DB_NAME) export EW_DB_NAME="$val" ;;
        DB_USER) export EW_DB_USER="$val" ;;
        DB_PASS) export EW_DB_PASS="$val" ;;
      esac
    fi
  done < "$EW_ENV"
fi

# DB configuration - pCloud Backup
DB_HOST="${PCLOUD_DB_HOST:-localhost}"
DB_PORT="${PCLOUD_DB_PORT:-3306}"
DB_NAME="${PCLOUD_DB_NAME:-pcloud_backup}"
DB_USER="${PCLOUD_DB_USER:-pcloud_backup}"
DB_PASS="${PCLOUD_DB_PASS:-}"

# DB configuration - EntropyWatcher
EW_DB_HOST="${EW_DB_HOST:-localhost}"
EW_DB_PORT="${EW_DB_PORT:-3306}"
EW_DB_NAME="${EW_DB_NAME:-entropywatcher}"
EW_DB_USER="${EW_DB_USER:-entropyuser}"
EW_DB_PASS="${EW_DB_PASS:-}"

# Jump-alert window (minutes) — aligned with nas HEALTH_WINDOW_MIN default
EW_JUMP_WINDOW_MIN="${EW_JUMP_WINDOW_MIN:-75}"
EW_MISSING_RECENT_DAYS="${EW_MISSING_RECENT_DAYS:-7}"

# If DB_DEFAULTS_FILE is set, use it (contains [client] section with password)
DB_DEFAULTS_FILE="${DB_DEFAULTS_FILE:-}"

# Ensure output directory exists
mkdir -p "$(dirname "$REPORTS_OUTPUT")"

# =====================================================
# Helper Functions
# =====================================================

log() {
  if [[ $VERBOSE -eq 1 ]]; then
    echo "[$(date '+%H:%M:%S')] $*" >&2
  fi
  return 0
}

# Run a MySQL query and return result (pCloud DB)
db_query() {
  local query="$1"

  local mysql_opts=()
  if [[ -n "$DB_DEFAULTS_FILE" && -f "$DB_DEFAULTS_FILE" ]]; then
    mysql_opts+=("--defaults-file=${DB_DEFAULTS_FILE}")
  else
    mysql_opts+=("-u${DB_USER}")
    [[ -n "$DB_PASS" ]] && mysql_opts+=("-p${DB_PASS}")
    mysql_opts+=("-h${DB_HOST}" "-P${DB_PORT}")
  fi

  mysql "${mysql_opts[@]}" \
    --silent --skip-column-names \
    --skip-ssl \
    --database="${DB_NAME}" \
    -e "${query}" 2>/dev/null
}

# Run a MySQL query for EntropyWatcher DB
ew_db_query() {
  local query="$1"

  # Debug: Log connection details in verbose mode
  if [[ $VERBOSE -eq 1 ]]; then
    log "EW DB Connection: host=${EW_DB_HOST}, port=${EW_DB_PORT}, db=${EW_DB_NAME}, user=${EW_DB_USER}, pass_len=${#EW_DB_PASS}"
  fi

  # Use MYSQL_PWD environment variable (safe for special characters like %)
  # Add --skip-ssl to suppress SSL warnings that pollute output
  local result
  result=$(MYSQL_PWD="${EW_DB_PASS}" mysql \
    -h "${EW_DB_HOST}" \
    -P "${EW_DB_PORT}" \
    -u "${EW_DB_USER}" \
    --silent --skip-column-names \
    --skip-ssl \
    --database="${EW_DB_NAME}" \
    -e "${query}" 2>/dev/null)
  
  local exit_code=$?
  
  if [[ $exit_code -ne 0 ]]; then
    [[ $VERBOSE -eq 1 ]] && log "EW DB Query failed (exit $exit_code)"
    return 1
  fi
  
  echo "$result"
}

# Escape a string for JSON
escape_json() {
  local str="$1"
  str="${str//\\/\\\\}"
  str="${str//\"/\\\"}"
  str="${str//$'\n'/\\n}"
  str="${str//$'\r'/}"
  str="${str//$'\t'/\\t}"
  echo "$str"
}

# MariaDB DATETIME = naive pi-nas wall clock (NOW()). Export as UTC ISO-8601 for dashboard JS.
MDB_TZ="${MDB_TZ:-Europe/Berlin}"
MDB_TS_MODE="${MDB_TS_MODE:-utc}"

init_mdb_timezone() {
  local detected test
  detected=$(db_query "SELECT IF(@@global.time_zone IN ('SYSTEM',''), @@system_time_zone, @@global.time_zone)" 2>/dev/null | tr -d '[:space:]' || true)
  [[ -n "$detected" ]] && MDB_TZ="$detected"
  test=$(db_query "SELECT CONVERT_TZ('2026-01-01 12:00:00', '${MDB_TZ}', '+00:00')" 2>/dev/null | tr -d '[:space:]' || true)
  if [[ -z "$test" || "$test" == "NULL" ]]; then
    MDB_TS_MODE="naive"
    log "WARN: CONVERT_TZ unavailable — exporting naive local timestamps (no Z suffix)"
  else
    MDB_TS_MODE="utc"
    log "DB timestamps: ${MDB_TZ} → UTC (ISO-8601 Z)"
  fi
}

# SQL expression: naive DATETIME column → JSON timestamp string
mdb_ts_sql() {
  local col="$1"
  if [[ "$MDB_TS_MODE" == "naive" ]]; then
    echo "DATE_FORMAT(${col}, '%Y-%m-%dT%H:%i:%s')"
  else
    echo "DATE_FORMAT(CONVERT_TZ(${col}, '${MDB_TZ}', '+00:00'), '%Y-%m-%dT%H:%i:%sZ')"
  fi
}

# =====================================================
# Check DB connectivity
# =====================================================
check_db() {
  if ! command -v mysql &>/dev/null; then
    echo '{"error":"mysql client not found","timestamp":"'"$(date -u '+%Y-%m-%dT%H:%M:%SZ')"'"}'
    return 1
  fi

  if ! db_query "SELECT 1;" &>/dev/null; then
    echo '{"error":"DB connection failed","timestamp":"'"$(date -u '+%Y-%m-%dT%H:%M:%SZ')"'"}'
    return 1
  fi

  return 0
}

# =====================================================
# Query: Recent Backups (last 10)
# =====================================================
get_recent_backups() {
  local result
  result=$(db_query "
    SELECT
      snapshot_name,
      status,
      $(mdb_ts_sql started_at) AS started_at,
      $(mdb_ts_sql finished_at) AS finished_at,
      COALESCE(duration_sec, 0) AS duration_sec,
      COALESCE(files_uploaded, 0) AS files_uploaded,
      COALESCE(ROUND(bytes_uploaded / 1024 / 1024 / 1024, 2), 0) AS gb_uploaded,
      COALESCE(gap_backfill_mode, 0) AS gap_backfill_mode,
      COALESCE(error_message, '') AS error_message
    FROM backup_runs
    ORDER BY started_at DESC
    LIMIT 15;
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r snapshot_name status started_at finished_at duration_sec files_uploaded gb_uploaded gap_backfill_mode error_message; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local err_escaped
    err_escaped=$(escape_json "$error_message")
    local finished_json="null"
    [[ "$finished_at" != "NULL" && -n "$finished_at" ]] && finished_json="\"${finished_at}\""
    json="${json}{"
    json="${json}\"snapshot\":\"${snapshot_name}\","
    json="${json}\"status\":\"${status}\","
    json="${json}\"started_at\":\"${started_at}\","
    json="${json}\"finished_at\":${finished_json},"
    json="${json}\"duration_sec\":${duration_sec},"
    json="${json}\"files_uploaded\":${files_uploaded},"
    json="${json}\"gb_uploaded\":${gb_uploaded},"
    json="${json}\"gap_backfill\":$([ "$gap_backfill_mode" = "1" ] && echo "true" || echo "false"),"
    json="${json}\"error\":\"${err_escaped}\""
    json="${json}}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: Performance Statistics (last 30 days)
# =====================================================
get_performance_stats() {
  local result
  result=$(db_query "
    SELECT
      COALESCE(COUNT(*), 0),
      COALESCE(SUM(br.status = 'SUCCESS'), 0),
      COALESCE(SUM(
        br.status = 'FAILED'
        AND NOT EXISTS (
          SELECT 1 FROM backup_runs ok
          WHERE ok.snapshot_name = br.snapshot_name
            AND ok.status = 'SUCCESS'
            AND ok.started_at > br.started_at
        )
      ), 0),
      COALESCE(ROUND(AVG(br.duration_sec) / 60, 2), 0),
      COALESCE(ROUND(SUM(br.bytes_uploaded) / 1024 / 1024 / 1024, 2), 0),
      COALESCE(SUM(COALESCE(br.files_uploaded, 0)), 0),
      COALESCE(SUM(br.gap_backfill_mode), 0)
    FROM backup_runs br
    WHERE br.started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY);
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "{\"total_runs\":0,\"successful_runs\":0,\"failed_runs\":0,\"unresolved_failed_runs\":0,\"avg_duration_min\":0,\"total_gb_uploaded\":0,\"files_uploaded_30d\":0,\"gap_backfill_count\":0}"
    return
  fi

  IFS=$'\t' read -r total successful unresolved avg_dur total_gb files_30d gap_count <<< "$result"

  echo "{\"total_runs\":${total},\"successful_runs\":${successful},\"failed_runs\":${unresolved},\"unresolved_failed_runs\":${unresolved},\"avg_duration_min\":${avg_dur},\"total_gb_uploaded\":${total_gb},\"files_uploaded_30d\":${files_30d},\"gap_backfill_count\":${gap_count}}"
}

# =====================================================
# Query: Failed Backups (last 7 days)
# =====================================================
get_failed_backups() {
  local result
  result=$(db_query "
    SELECT
      br.snapshot_name,
      $(mdb_ts_sql br.started_at) AS started_at,
      COALESCE(br.duration_sec, 0) AS duration_sec,
      COALESCE(br.error_message, '') AS error_message
    FROM backup_runs br
    WHERE br.status = 'FAILED'
      AND br.started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
      AND br.error_message IS NOT NULL
      AND TRIM(br.error_message) != ''
      AND NOT EXISTS (
          SELECT 1
          FROM backup_runs br_ok
          WHERE br_ok.snapshot_name = br.snapshot_name
            AND br_ok.status = 'SUCCESS'
            AND br_ok.started_at > br.started_at
      )
    ORDER BY br.started_at DESC
    LIMIT 5;
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r snapshot_name started_at duration_sec error_message; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local err_escaped
    err_escaped=$(escape_json "$error_message")
    json="${json}{\"snapshot\":\"${snapshot_name}\",\"started_at\":\"${started_at}\",\"duration_sec\":${duration_sec},\"error\":\"${err_escaped}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: Phase Performance (avg per phase, last 30 days)
# Pool-Pipeline: manifest → upload → verify (Retention/GC separat via pcloud_pool_gc.py)
# Legacy 1:1: folder_creation, retention_sync (historische Daten)
# =====================================================
get_phase_stats() {
  local result
  # failed  = Phase fehlgeschlagen UND Lauf FAILED (echter Fehler)
  # warnings = Phase FAILED aber Lauf SUCCESS (z.B. Delta-Verify non-critical)
  result=$(db_query "
    SELECT
      bp.phase_name,
      COUNT(*) AS total,
      SUM(CASE WHEN bp.status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful,
      SUM(CASE WHEN bp.status = 'FAILED' AND br.status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
      SUM(CASE WHEN bp.status = 'FAILED' AND br.status = 'SUCCESS' THEN 1 ELSE 0 END) AS warnings,
      ROUND(AVG(bp.duration_sec), 1) AS avg_duration_sec,
      ROUND(AVG(bp.bytes_processed) / 1024 / 1024 / 1024, 2) AS avg_gb
    FROM backup_phases bp
    JOIN backup_runs br ON br.run_id = bp.run_id
    WHERE bp.started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
    GROUP BY bp.phase_name
    ORDER BY FIELD(bp.phase_name,
      'manifest', 'upload', 'verify',
      'pool_retention', 'pool_gc',
      'folder_creation', 'retention_sync');
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r phase_name total successful failed warnings avg_dur_sec avg_gb; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    json="${json}{\"phase\":\"${phase_name}\",\"total\":${total},\"successful\":${successful},\"failed\":${failed},\"warnings\":${warnings},\"avg_duration_sec\":${avg_dur_sec},\"avg_gb\":${avg_gb}}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: Pool backup integrity (snapshot_integrity_checks)
# =====================================================
get_pool_integrity_summary() {
  local result
  # post_upload_superseded = FAILED Gate, aber späterer Audit OK (kein Live-Problem)
  # post_upload_open       = FAILED Gate ohne späteren OK-Audit
  result=$(db_query "
    SELECT
      SUM(CASE WHEN audit_freshness = 'OK' THEN 1 ELSE 0 END),
      SUM(CASE WHEN audit_freshness = 'FAILED' THEN 1 ELSE 0 END),
      SUM(CASE WHEN audit_freshness = 'STALE' THEN 1 ELSE 0 END),
      SUM(CASE WHEN audit_freshness = 'UNKNOWN' THEN 1 ELSE 0 END),
      SUM(CASE WHEN post_upload_status = 'FAILED' THEN 1 ELSE 0 END),
      SUM(CASE WHEN post_upload_status = 'FAILED'
                AND monthly_audit_status = 'OK'
                AND monthly_audit_at IS NOT NULL
                AND (post_upload_at IS NULL OR monthly_audit_at >= post_upload_at)
           THEN 1 ELSE 0 END),
      SUM(CASE WHEN post_upload_status = 'FAILED'
                AND NOT (
                  monthly_audit_status = 'OK'
                  AND monthly_audit_at IS NOT NULL
                  AND (post_upload_at IS NULL OR monthly_audit_at >= post_upload_at)
                )
           THEN 1 ELSE 0 END),
      COUNT(*)
    FROM v_snapshot_integrity_status;
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "{\"audit_ok\":0,\"audit_failed\":0,\"audit_stale\":0,\"audit_unknown\":0,\"post_upload_failed\":0,\"post_upload_superseded\":0,\"post_upload_open\":0,\"total\":0}"
    return
  fi

  IFS=$'\t' read -r audit_ok audit_failed audit_stale audit_unknown post_failed post_superseded post_open total <<< "$result"
  echo "{\"audit_ok\":${audit_ok:-0},\"audit_failed\":${audit_failed:-0},\"audit_stale\":${audit_stale:-0},\"audit_unknown\":${audit_unknown:-0},\"post_upload_failed\":${post_failed:-0},\"post_upload_superseded\":${post_superseded:-0},\"post_upload_open\":${post_open:-0},\"total\":${total:-0}}"
}

get_pool_integrity_snapshots() {
  local result
  result=$(db_query "
    SELECT COALESCE(JSON_ARRAYAGG(j), JSON_ARRAY())
    FROM (
      SELECT JSON_OBJECT(
        'snapshot', snapshot_name,
        'backup_status', COALESCE(backup_status, ''),
        'post_upload_status', COALESCE(post_upload_status, ''),
        'post_upload_at', COALESCE($(mdb_ts_sql post_upload_at), ''),
        'post_upload_issues', COALESCE(post_upload_issues, 0),
        'monthly_audit_status', COALESCE(monthly_audit_status, ''),
        'monthly_audit_at', COALESCE($(mdb_ts_sql monthly_audit_at), ''),
        'monthly_audit_issues', COALESCE(monthly_audit_issues, 0),
        'monthly_audit_summary', COALESCE(LEFT(monthly_audit_summary, 180), ''),
        'audit_freshness', COALESCE(audit_freshness, 'UNKNOWN')
      ) AS j
      FROM v_snapshot_integrity_status
      ORDER BY
        FIELD(COALESCE(post_upload_status, ''), 'FAILED', 'OK', ''),
        FIELD(audit_freshness, 'FAILED', 'STALE', 'UNKNOWN', 'OK'),
        snapshot_name DESC
      LIMIT 40
    ) sub;
  " 2>/dev/null || echo "[]")

  if [[ -z "$result" || "$result" == "NULL" ]]; then
    echo "[]"
    return
  fi
  echo "$result"
}

# =====================================================
# Query: EntropyWatcher - Last Scans (per source)
# =====================================================
get_ew_last_scans() {
  local result
  result=$(ew_db_query "
    SELECT
      source,
      $(mdb_ts_sql finished_at) AS finished_at,
      COALESCE(files_processed, 0) AS files_processed,
      COALESCE(flagged_new_count, 0) AS flagged_new,
      COALESCE(missing_count, 0) AS missing_count,
      COALESCE(av_found_count, 0) AS av_found
    FROM scan_summary
    WHERE source IS NOT NULL
    ORDER BY finished_at DESC
    LIMIT 10;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source finished_at files_processed flagged_new missing_count av_found; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    json="${json}{\"source\":\"${source}\",\"finished_at\":\"${finished_at}\",\"files_processed\":${files_processed},\"flagged_new\":${flagged_new},\"missing_count\":${missing_count},\"av_found\":${av_found}}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Flagged Files Count (per source)
# =====================================================
get_ew_flagged_files() {
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      COUNT(*) AS flagged_count
    FROM files
    WHERE flagged = 1
    GROUP BY source
    ORDER BY source;
  ")

  if [[ -z "$result" ]]; then
    echo "{}"
    return
  fi

  local json="{"
  local first=1

  while IFS=$'\t' read -r source count; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    json="${json}\"${source}\":${count}"
  done <<< "$result"

  json="${json}}"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Recent AV Events (last 7 days)
# =====================================================
get_ew_av_events() {
  local result
  result=$(ew_db_query "
    SELECT
      $(mdb_ts_sql detected_at) AS detected_at,
      COALESCE(source, 'unknown') AS source,
      signature,
      action
    FROM av_events
    WHERE detected_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
    ORDER BY detected_at DESC
    LIMIT 10;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r detected_at source signature action; do
    [["$first" -eq 0 ]] && json="${json},"
    first=0
    local sig_escaped
    sig_escaped=$(escape_json "$signature")
    json="${json}{\"detected_at\":\"${detected_at}\",\"source\":\"${source}\",\"signature\":\"${sig_escaped}\",\"action\":\"${action}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Flagged Files (detailed)
# =====================================================
get_ew_flagged_files_detailed() {
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      CONVERT(path USING utf8mb4) AS path,
      ROUND(last_entropy, 3) AS entropy,
      COALESCE(note, '') AS reason
    FROM files
    WHERE flagged = 1
    ORDER BY last_entropy DESC
    LIMIT 100;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source path entropy reason; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local path_escaped reason_escaped
    path_escaped=$(escape_json "$path")
    reason_escaped=$(escape_json "$reason")
    json="${json}{\"source\":\"${source}\",\"path\":\"${path_escaped}\",\"entropy\":${entropy},\"reason\":\"${reason_escaped}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Missing Files (ALL from DB)
# =====================================================
get_ew_missing_files() {
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      CONVERT(path USING utf8mb4) AS path,
      DATE_FORMAT(missing_since, '%Y-%m-%d %H:%i:%S') AS missing_since
    FROM files
    WHERE missing_since IS NOT NULL
    ORDER BY missing_since DESC
    LIMIT 100;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source path missing_since; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local path_escaped
    path_escaped=$(escape_json "$path")
    json="${json}{\"source\":\"${source}\",\"path\":\"${path_escaped}\",\"missing_since\":\"${missing_since}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Missing Files (recent only)
# =====================================================
get_ew_missing_files_recent() {
  local days="${EW_MISSING_RECENT_DAYS:-7}"
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      CONVERT(path USING utf8mb4) AS path,
      DATE_FORMAT(missing_since, '%Y-%m-%d %H:%i:%S') AS missing_since
    FROM files
    WHERE missing_since IS NOT NULL
      AND missing_since >= DATE_SUB(NOW(), INTERVAL ${days} DAY)
    ORDER BY missing_since DESC
    LIMIT 100;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source path missing_since; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local path_escaped
    path_escaped=$(escape_json "$path")
    json="${json}{\"source\":\"${source}\",\"path\":\"${path_escaped}\",\"missing_since\":\"${missing_since}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Integrity summary counters
# =====================================================
get_ew_integrity_summary() {
  local win="${EW_JUMP_WINDOW_MIN:-75}"
  local days="${EW_MISSING_RECENT_DAYS:-7}"

  local jump_alerts stable_high flagged_total flagged_new missing_recent missing_total
  jump_alerts=$(ew_db_query "
    SELECT COUNT(*) FROM files
    WHERE flagged = 1 AND note LIKE '%jump%'
      AND last_time >= DATE_SUB(NOW(), INTERVAL ${win} MINUTE);
  " | head -1)
  stable_high=$(ew_db_query "
    SELECT COUNT(*) FROM files
    WHERE flagged = 1 AND note LIKE '%abs%'
      AND (note NOT LIKE '%jump%' OR note IS NULL);
  " | head -1)
  flagged_total=$(ew_db_query "
    SELECT COUNT(*) FROM files WHERE flagged = 1;
  " | head -1)
  flagged_new=$(ew_db_query "
    SELECT COALESCE(SUM(s.flagged_new_count), 0)
    FROM scan_summary s
    INNER JOIN (
      SELECT source, MAX(finished_at) AS max_finished
      FROM scan_summary
      WHERE source IS NOT NULL
      GROUP BY source
    ) latest ON s.source = latest.source AND s.finished_at = latest.max_finished;
  " | head -1)
  missing_recent=$(ew_db_query "
    SELECT COUNT(*) FROM files
    WHERE missing_since IS NOT NULL
      AND missing_since >= DATE_SUB(NOW(), INTERVAL ${days} DAY);
  " | head -1)
  missing_total=$(ew_db_query "
    SELECT COUNT(*) FROM files WHERE missing_since IS NOT NULL;
  " | head -1)

  jump_alerts="${jump_alerts:-0}"
  stable_high="${stable_high:-0}"
  flagged_total="${flagged_total:-0}"
  flagged_new="${flagged_new:-0}"
  missing_recent="${missing_recent:-0}"
  missing_total="${missing_total:-0}"

  cat <<SUMEOF
{
  "jump_alerts_window": ${jump_alerts},
  "jump_window_minutes": ${win},
  "flagged_stable_high": ${stable_high},
  "flagged_total": ${flagged_total},
  "flagged_new_last_scan": ${flagged_new},
  "missing_recent_days": ${days},
  "missing_recent": ${missing_recent},
  "missing_total": ${missing_total}
}
SUMEOF
}

# =====================================================
# Query: EntropyWatcher - Jump alerts (detailed, windowed)
# =====================================================
get_ew_flagged_jumps_detailed() {
  local win="${EW_JUMP_WINDOW_MIN:-75}"
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      CONVERT(path USING utf8mb4) AS path,
      ROUND(COALESCE(prev_entropy, 0), 3) AS prev_entropy,
      ROUND(COALESCE(last_entropy, 0), 3) AS entropy,
      COALESCE(note, '') AS reason,
      DATE_FORMAT(last_time, '%Y-%m-%d %H:%i:%S') AS last_time
    FROM files
    WHERE flagged = 1 AND note LIKE '%jump%'
      AND last_time >= DATE_SUB(NOW(), INTERVAL ${win} MINUTE)
    ORDER BY last_time DESC
    LIMIT 100;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source path prev_entropy entropy reason last_time; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local path_escaped reason_escaped
    path_escaped=$(escape_json "$path")
    reason_escaped=$(escape_json "$reason")
    json="${json}{\"source\":\"${source}\",\"path\":\"${path_escaped}\",\"prev_entropy\":${prev_entropy},\"entropy\":${entropy},\"reason\":\"${reason_escaped}\",\"last_time\":\"${last_time}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Query: EntropyWatcher - Stable high entropy (detailed)
# =====================================================
get_ew_flagged_stable_detailed() {
  local result
  result=$(ew_db_query "
    SELECT
      COALESCE(source, 'unknown') AS source,
      CONVERT(path USING utf8mb4) AS path,
      ROUND(COALESCE(last_entropy, 0), 3) AS entropy,
      COALESCE(note, '') AS reason
    FROM files
    WHERE flagged = 1 AND note LIKE '%abs%'
      AND (note NOT LIKE '%jump%' OR note IS NULL)
    ORDER BY last_entropy DESC
    LIMIT 50;
  ")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r source path entropy reason; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    local path_escaped reason_escaped
    path_escaped=$(escape_json "$path")
    reason_escaped=$(escape_json "$reason")
    json="${json}{\"source\":\"${source}\",\"path\":\"${path_escaped}\",\"entropy\":${entropy},\"reason\":\"${reason_escaped}\"}"
  done <<< "$result"

  json="${json}]"
  echo "$json"
}

# =====================================================
# Main
# =====================================================

log "Starting reports generation..."

TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

# Check DB connectivity
if ! check_db &>/dev/null; then
  log "ERROR: DB not reachable, writing error report"
  cat > "$REPORTS_OUTPUT" << ERREOF
{
  "timestamp": "$TIMESTAMP",
  "error": "Database connection failed",
  "recent_backups": [],
  "performance_stats": {},
  "failed_backups": [],
    "phase_stats": [],
    "pool_integrity": {
      "summary": {},
      "snapshots": []
    },
    "entropywatcher": {
    "last_scans": [],
    "integrity_summary": {},
    "flagged_files": {},
    "flagged_jumps_detailed": [],
    "flagged_stable_detailed": [],
    "missing_files_recent": [],
    "av_events": []
  }
}
ERREOF
  chmod 644 "$REPORTS_OUTPUT"
  exit 0
fi

init_mdb_timezone

log "Querying recent backups..."
RECENT_BACKUPS=$(get_recent_backups)
[[ -z "$RECENT_BACKUPS" ]] && RECENT_BACKUPS="[]"

log "Querying performance stats..."
PERF_STATS=$(get_performance_stats)
[[ -z "$PERF_STATS" ]] && PERF_STATS="{}"

log "Querying failed backups..."
FAILED_BACKUPS=$(get_failed_backups)
[[ -z "$FAILED_BACKUPS" ]] && FAILED_BACKUPS="[]"

log "Querying phase stats..."
PHASE_STATS=$(get_phase_stats)
[[ -z "$PHASE_STATS" ]] && PHASE_STATS="[]"

log "Querying EntropyWatcher last scans..."
EW_LAST_SCANS=$(get_ew_last_scans)
[[ -z "$EW_LAST_SCANS" || "$EW_LAST_SCANS" == "" ]] && EW_LAST_SCANS="[]"

log "Querying EntropyWatcher flagged files..."
EW_FLAGGED=$(get_ew_flagged_files)
[[ -z "$EW_FLAGGED" || "$EW_FLAGGED" == "" ]] && EW_FLAGGED="{}"

log "Querying EntropyWatcher flagged files (detailed)..."
EW_FLAGGED_FILES_DETAILED=$(get_ew_flagged_files_detailed)
[[ -z "$EW_FLAGGED_FILES_DETAILED" || "$EW_FLAGGED_FILES_DETAILED" == "" ]] && EW_FLAGGED_FILES_DETAILED="[]"

log "Querying EntropyWatcher AV events..."
EW_AV_EVENTS=$(get_ew_av_events)
[[ -z "$EW_AV_EVENTS" || "$EW_AV_EVENTS" == "" ]] && EW_AV_EVENTS="[]"

log "Querying EntropyWatcher missing files..."
EW_MISSING_FILES=$(get_ew_missing_files)
[[ -z "$EW_MISSING_FILES" || "$EW_MISSING_FILES" == "" ]] && EW_MISSING_FILES="[]"

log "Querying EntropyWatcher integrity summary..."
EW_INTEGRITY_SUMMARY=$(get_ew_integrity_summary)
[[ -z "$EW_INTEGRITY_SUMMARY" || "$EW_INTEGRITY_SUMMARY" == "" ]] && EW_INTEGRITY_SUMMARY="{}"

log "Querying EntropyWatcher jump alerts (detailed)..."
EW_FLAGGED_JUMPS=$(get_ew_flagged_jumps_detailed)
[[ -z "$EW_FLAGGED_JUMPS" || "$EW_FLAGGED_JUMPS" == "" ]] && EW_FLAGGED_JUMPS="[]"

log "Querying EntropyWatcher stable-high flagged (detailed)..."
EW_FLAGGED_STABLE=$(get_ew_flagged_stable_detailed)
[[ -z "$EW_FLAGGED_STABLE" || "$EW_FLAGGED_STABLE" == "" ]] && EW_FLAGGED_STABLE="[]"

log "Querying EntropyWatcher missing files (recent)..."
EW_MISSING_RECENT=$(get_ew_missing_files_recent)
[[ -z "$EW_MISSING_RECENT" || "$EW_MISSING_RECENT" == "" ]] && EW_MISSING_RECENT="[]"

log "Querying pool integrity snapshots..."
POOL_INTEGRITY_SNAPSHOTS=$(get_pool_integrity_snapshots)
[[ -z "$POOL_INTEGRITY_SNAPSHOTS" ]] && POOL_INTEGRITY_SNAPSHOTS="[]"

log "Querying pool integrity summary..."
POOL_INTEGRITY_SUMMARY=$(get_pool_integrity_summary)
[[ -z "$POOL_INTEGRITY_SUMMARY" ]] && POOL_INTEGRITY_SUMMARY="{}"

log "Writing output to: $REPORTS_OUTPUT"

# Debug: Show variable lengths in verbose mode
if [[ $VERBOSE -eq 1 ]]; then
  log "Variable lengths: RECENT_BACKUPS=${#RECENT_BACKUPS}, PERF_STATS=${#PERF_STATS}, FAILED_BACKUPS=${#FAILED_BACKUPS}, PHASE_STATS=${#PHASE_STATS}"
  log "EW Variables: EW_LAST_SCANS=${#EW_LAST_SCANS}, EW_FLAGGED=${#EW_FLAGGED}, EW_AV_EVENTS=${#EW_AV_EVENTS}"
  log "First 100 chars of EW_LAST_SCANS: ${EW_LAST_SCANS:0:100}"
  log "First 100 chars of EW_FLAGGED: ${EW_FLAGGED:0:100}"
fi

# Generate JSON (heredoc with quoted EOF prevents variable expansion issues)
cat > "$REPORTS_OUTPUT" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "server_timezone": "${MDB_TZ}",
  "timestamps_utc": $([ "$MDB_TS_MODE" = "utc" ] && echo "true" || echo "false"),
  "recent_backups": ${RECENT_BACKUPS},
  "performance_stats": ${PERF_STATS},
  "failed_backups": ${FAILED_BACKUPS},
  "phase_stats": ${PHASE_STATS},
  "pool_integrity": {
    "summary": ${POOL_INTEGRITY_SUMMARY},
    "snapshots": ${POOL_INTEGRITY_SNAPSHOTS}
  },
  "entropywatcher": {
    "last_scans": ${EW_LAST_SCANS},
    "integrity_summary": ${EW_INTEGRITY_SUMMARY},
    "flagged_files": ${EW_FLAGGED},
    "flagged_files_detailed": ${EW_FLAGGED_FILES_DETAILED},
    "flagged_jumps_detailed": ${EW_FLAGGED_JUMPS},
    "flagged_stable_detailed": ${EW_FLAGGED_STABLE},
    "av_events": ${EW_AV_EVENTS},
    "missing_files": ${EW_MISSING_FILES},
    "missing_files_recent": ${EW_MISSING_RECENT}
  }
}
EOF

# Validate JSON before finalizing (only if jq is available)
if command -v jq &>/dev/null; then
  if ! jq empty "$REPORTS_OUTPUT" 2>/dev/null; then
    log "ERROR: Generated JSON is invalid! Check $REPORTS_OUTPUT"
    
    if [[ $VERBOSE -eq 1 ]]; then
      log "Dumping variables to /tmp for debugging:"
      echo "$RECENT_BACKUPS" > /tmp/debug_recent_backups.json
      echo "$PERF_STATS" > /tmp/debug_perf_stats.json
      echo "$FAILED_BACKUPS" > /tmp/debug_failed_backups.json
      echo "$PHASE_STATS" > /tmp/debug_phase_stats.json
      echo "$EW_LAST_SCANS" > /tmp/debug_ew_last_scans.json
      echo "$EW_FLAGGED" > /tmp/debug_ew_flagged.json
      echo "$EW_AV_EVENTS" > /tmp/debug_ew_av_events.json
      log "Debug files written to /tmp/debug_*.json"
      log "Check with: jq . /tmp/debug_*.json"
    fi
    
    exit 1
  fi
else
  # jq not available - skip validation but warn in verbose mode
  [[ $VERBOSE -eq 1 ]] && log "Warning: jq not found, skipping JSON validation"
fi

chmod 644 "$REPORTS_OUTPUT"

log "Reports generation complete."

exit 0
