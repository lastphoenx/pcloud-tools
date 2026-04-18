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

# DB configuration
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-3306}"
DB_NAME="${DB_NAME:-pcloud_backup}"
DB_USER="${DB_USER:-pcloud_backup}"
DB_PASS="${DB_PASS:-}"
# If DB_DEFAULTS_FILE is set, use it (contains [client] section with password)
DB_DEFAULTS_FILE="${DB_DEFAULTS_FILE:-}"

# Ensure output directory exists
mkdir -p "$(dirname "$REPORTS_OUTPUT")"

# =====================================================
# Helper Functions
# =====================================================

log() {
  [[ $VERBOSE -eq 1 ]] && echo "[$(date '+%H:%M:%S')] $*" >&2
}

# Run a MySQL query and return result
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
    --database="${DB_NAME}" \
    -e "${query}" 2>/dev/null
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
      DATE_FORMAT(started_at, '%Y-%m-%dT%H:%i:%SZ') AS started_at,
      DATE_FORMAT(finished_at, '%Y-%m-%dT%H:%i:%SZ') AS finished_at,
      COALESCE(duration_sec, 0) AS duration_sec,
      COALESCE(files_uploaded, 0) AS files_uploaded,
      COALESCE(ROUND(bytes_uploaded / 1024 / 1024 / 1024, 2), 0) AS gb_uploaded,
      COALESCE(gap_backfill_mode, 0) AS gap_backfill_mode,
      COALESCE(error_message, '') AS error_message
    FROM backup_runs
    ORDER BY started_at DESC
    LIMIT 10;
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
      COALESCE(total_runs, 0),
      COALESCE(successful_runs, 0),
      COALESCE(failed_runs, 0),
      COALESCE(avg_duration_min, 0),
      COALESCE(total_gb_uploaded, 0),
      COALESCE(avg_gb_per_run, 0),
      COALESCE(gap_backfill_count, 0)
    FROM v_performance_stats;
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "{\"total_runs\":0,\"successful_runs\":0,\"failed_runs\":0,\"avg_duration_min\":0,\"total_gb_uploaded\":0,\"avg_gb_per_run\":0,\"gap_backfill_count\":0}"
    return
  fi

  IFS=$'\t' read -r total successful failed avg_dur total_gb avg_gb gap_count <<< "$result"

  echo "{\"total_runs\":${total},\"successful_runs\":${successful},\"failed_runs\":${failed},\"avg_duration_min\":${avg_dur},\"total_gb_uploaded\":${total_gb},\"avg_gb_per_run\":${avg_gb},\"gap_backfill_count\":${gap_count}}"
}

# =====================================================
# Query: Failed Backups (last 7 days)
# =====================================================
get_failed_backups() {
  local result
  result=$(db_query "
    SELECT
      snapshot_name,
      DATE_FORMAT(started_at, '%Y-%m-%dT%H:%i:%SZ') AS started_at,
      COALESCE(duration_sec, 0) AS duration_sec,
      COALESCE(error_message, '') AS error_message
    FROM v_failed_backups
    ORDER BY started_at DESC
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
# =====================================================
get_phase_stats() {
  local result
  result=$(db_query "
    SELECT
      phase_name,
      COUNT(*) AS total,
      SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful,
      SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
      ROUND(AVG(duration_sec), 1) AS avg_duration_sec,
      ROUND(AVG(bytes_processed) / 1024 / 1024 / 1024, 2) AS avg_gb
    FROM backup_phases
    WHERE started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
    GROUP BY phase_name
    ORDER BY FIELD(phase_name, 'manifest', 'folder_creation', 'upload', 'verify', 'retention_sync');
  " 2>/dev/null || echo "")

  if [[ -z "$result" ]]; then
    echo "[]"
    return
  fi

  local json="["
  local first=1

  while IFS=$'\t' read -r phase_name total successful failed avg_dur_sec avg_gb; do
    [[ "$first" -eq 0 ]] && json="${json},"
    first=0
    json="${json}{\"phase\":\"${phase_name}\",\"total\":${total},\"successful\":${successful},\"failed\":${failed},\"avg_duration_sec\":${avg_dur_sec},\"avg_gb\":${avg_gb}}"
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
  "phase_stats": []
}
ERREOF
  chmod 644 "$REPORTS_OUTPUT"
  exit 0
fi

log "Querying recent backups..."
RECENT_BACKUPS=$(get_recent_backups)

log "Querying performance stats..."
PERF_STATS=$(get_performance_stats)

log "Querying failed backups..."
FAILED_BACKUPS=$(get_failed_backups)

log "Querying phase stats..."
PHASE_STATS=$(get_phase_stats)

log "Writing output to: $REPORTS_OUTPUT"

cat > "$REPORTS_OUTPUT" << EOF
{
  "timestamp": "$TIMESTAMP",
  "recent_backups": $RECENT_BACKUPS,
  "performance_stats": $PERF_STATS,
  "failed_backups": $FAILED_BACKUPS,
  "phase_stats": $PHASE_STATS
}
EOF

chmod 644 "$REPORTS_OUTPUT"

log "Reports generation complete."
