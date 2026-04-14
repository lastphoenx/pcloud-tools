#!/usr/bin/env bash
# =====================================================
# pCloud Backup Status Dashboard (MariaDB)
# =====================================================
# Purpose: Query run history from MariaDB and display status
# Usage:
#   ./pcloud_status.sh                  # Show last 10 runs
#   ./pcloud_status.sh --last-n 20      # Show last 20 runs
#   ./pcloud_status.sh --failures       # Show only failures
#   ./pcloud_status.sh --stats          # Show 30-day statistics
#   ./pcloud_status.sh --current        # Show currently running backup
#   ./pcloud_status.sh html OUTPUT.html # Generate HTML dashboard
# =====================================================

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# Load .env if exists
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

PCLOUD_DB_HOST="${PCLOUD_DB_HOST:-localhost}"
PCLOUD_DB_PORT="${PCLOUD_DB_PORT:-3306}"
PCLOUD_DB_NAME="${PCLOUD_DB_NAME:-pcloud_backup}"
PCLOUD_DB_USER="${PCLOUD_DB_USER:-pcloud_backup}"
PCLOUD_DB_PASS="${PCLOUD_DB_PASS:-}"

MODE="${1:-recent}"
ARG="${2:-10}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# MySQL helper
_mysql() {
  mysql -h "$PCLOUD_DB_HOST" \
        -P "$PCLOUD_DB_PORT" \
        -u "$PCLOUD_DB_USER" \
        -p"$PCLOUD_DB_PASS" \
        -D "$PCLOUD_DB_NAME" \
        -sN \
        -e "$@" 2>/dev/null
}

# Check if DB connection works
if ! _mysql "SELECT 1" >/dev/null 2>&1; then
  echo -e "${RED}ERROR: Cannot connect to MariaDB${NC}"
  echo ""
  echo "Database: $PCLOUD_DB_NAME@$PCLOUD_DB_HOST:$PCLOUD_DB_PORT"
  echo "User: $PCLOUD_DB_USER"
  echo ""
  echo "Checks:"
  echo "  1. Is MariaDB running? (sudo systemctl status mariadb)"
  echo "  2. Does database exist? (mysql -u root -p -e 'SHOW DATABASES;')"
  echo "  3. Are credentials in .env correct?"
  echo "  4. Has schema been initialized? (mysql < sql/init_pcloud_db.sql)"
  exit 1
fi

# Helper: Format bytes to human-readable
format_bytes() {
  local bytes="$1"
  if [[ "$bytes" -lt 1024 ]]; then
    echo "${bytes} B"
  elif [[ "$bytes" -lt 1048576 ]]; then
    echo "$(awk "BEGIN {printf \"%.2f\", $bytes/1024}") KB"
  elif [[ "$bytes" -lt 1073741824 ]]; then
    echo "$(awk "BEGIN {printf \"%.2f\", $bytes/1048576}") MB"
  else
    echo "$(awk "BEGIN {printf \"%.2f\", $bytes/1073741824}") GB"
  fi
}

# Helper: Format duration to human-readable
format_duration() {
  local seconds="$1"
  if [[ -z "$seconds" || "$seconds" == "NULL" ]]; then
    echo "N/A"
    return
  fi
  
  local hours=$((seconds / 3600))
  local mins=$(((seconds % 3600) / 60))
  local secs=$((seconds % 60))
  
  if [[ $hours -gt 0 ]]; then
    printf "%dh %dm %ds" "$hours" "$mins" "$secs"
  elif [[ $mins -gt 0 ]]; then
    printf "%dm %ds" "$mins" "$secs"
  else
    printf "%ds" "$secs"
  fi
}

# Helper: Colorize status
colorize_status() {
  local status="$1"
  case "$status" in
    SUCCESS) echo -e "${GREEN}${status}${NC}" ;;
    FAILED) echo -e "${RED}${status}${NC}" ;;
    RUNNING) echo -e "${CYAN}${status}${NC}" ;;
    *) echo "$status" ;;
  esac
}

# =====================================================
# MODE: Recent Backups
# =====================================================
if [[ "$MODE" == "recent" || "$MODE" == "--last-n" ]]; then
  LIMIT="${ARG}"
  
  echo -e "${BOLD}pCloud Backup Status - Last ${LIMIT} Runs${NC}"
  echo "========================================"
  echo ""
  
  # Query recent runs
  _mysql "SELECT run_id, snapshot_name, status, started_at, finished_at, duration_sec, files_uploaded, bytes_uploaded 
    FROM backup_runs 
    ORDER BY started_at DESC 
    LIMIT $LIMIT" | while IFS=$'\t' read -r run_id snapshot status started finished duration files bytes; do
    
    echo -e "${BOLD}Run ID:${NC} $run_id"
    echo "  Snapshot: $snapshot"
    echo -e "  Status: $(colorize_status "$status")"
    echo "  Started: $started"
    echo "  Finished: ${finished:-N/A}"
    echo "  Duration: $(format_duration "$duration")"
    echo "  Files: ${files:-0}"
    echo "  Bytes: $(format_bytes "${bytes:-0}")"
    echo ""
  done

# =====================================================
# MODE: Failures Only
# =====================================================
elif [[ "$MODE" == "--failures" ]]; then
  echo -e "${BOLD}pCloud Backup Status - Recent Failures${NC}"
  echo "========================================"
  echo ""
  
  _mysql "SELECT run_id, snapshot_name, started_at, error_message 
    FROM backup_runs 
    WHERE status='FAILED' 
    ORDER BY started_at DESC 
    LIMIT 20" | while IFS=$'\t' read -r run_id snapshot started error; do
    
    echo -e "${RED}✗${NC} ${BOLD}$snapshot${NC} ($(date -d "$started" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$started"))"
    echo "  Run ID: $run_id"
    echo "  Error: ${error:-Unknown error}"
    echo ""
  done

# =====================================================
# MODE: Statistics
# =====================================================
elif [[ "$MODE" == "--stats" ]]; then
  echo -e "${BOLD}pCloud Backup Statistics (Last 30 Days)${NC}"
  echo "========================================"
  echo ""
  
  # Query stats
  read -r total success failed avg_dur total_gb avg_gb gaps < <(_mysql "SELECT 
    COUNT(*) AS total_runs,
    SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS successful,
    SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed,
    ROUND(AVG(duration_sec)/60, 2) AS avg_duration_min,
    ROUND(SUM(bytes_uploaded)/1073741824, 2) AS total_gb,
    ROUND(AVG(bytes_uploaded)/1073741824, 2) AS avg_gb,
    SUM(gap_backfill_mode) AS gaps
    FROM backup_runs 
    WHERE started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)")
  
  echo "  Total Runs: $total"
  echo -e "  Successful: ${GREEN}$success${NC}"
  echo -e "  Failed: ${RED}$failed${NC}"
  echo "  Success Rate: $(awk "BEGIN {printf \"%.1f%%\", ($success/$total)*100}" 2>/dev/null || echo "N/A")"
  echo ""
  echo "  Average Duration: ${avg_dur} minutes"
  echo "  Total Data: ${total_gb} GB"
  echo "  Average per Run: ${avg_gb} GB"
  echo "  Gap Backfills: $gaps"
  echo ""

# =====================================================
# MODE: Current Running Backup
# =====================================================
elif [[ "$MODE" == "--current" ]]; then
  echo -e "${BOLD}pCloud Backup Status - Currently Running${NC}"
  echo "========================================"
  echo ""
  
  running=$(_mysql "SELECT run_id, snapshot_name, started_at, TIMESTAMPDIFF(SECOND, started_at, NOW()) AS elapsed 
    FROM backup_runs 
    WHERE status='RUNNING' 
    ORDER BY started_at DESC 
    LIMIT 1" || echo "")
  
  if [[ -z "$running" ]]; then
    echo "No backup currently running."
  else
    read -r run_id snapshot started elapsed <<< "$running"
    echo -e "${CYAN}●${NC} ${BOLD}Backup in progress${NC}"
    echo "  Snapshot: $snapshot"
    echo "  Run ID: $run_id"
    echo "  Started: $started"
    echo "  Elapsed: $(format_duration "$elapsed")"
    echo ""
    
    # Show phases
    echo "Phases:"
    _mysql "SELECT phase_name, status, started_at, finished_at 
      FROM backup_phases 
      WHERE run_id='$run_id' 
      ORDER BY started_at" | while IFS=$'\t' read -r phase status phase_start phase_end; do
      
      if [[ "$status" == "RUNNING" ]]; then
        echo -e "  ${CYAN}▶${NC} $phase (running...)"
      elif [[ "$status" == "SUCCESS" ]]; then
        echo -e "  ${GREEN}✓${NC} $phase (done)"
      else
        echo -e "  ${RED}✗${NC} $phase (failed)"
      fi
    done
  fi
  echo ""

# =====================================================
# MODE: HTML Dashboard
# =====================================================
elif [[ "$MODE" == "html" ]]; then
  OUTPUT="${ARG:-dashboard.html}"
  
  cat > "$OUTPUT" <<'HTML_HEADER'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>pCloud Backup Dashboard</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #1a1a1a; color: #e0e0e0; padding: 20px; }
    h1 { margin-bottom: 20px; color: #4CAF50; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 30px; }
    .stat-card { background: #2a2a2a; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.3); }
    .stat-card h3 { font-size: 14px; color: #999; margin-bottom: 10px; }
    .stat-card .value { font-size: 32px; font-weight: bold; color: #4CAF50; }
    table { width: 100%; border-collapse: collapse; background: #2a2a2a; border-radius: 8px; overflow: hidden; }
    th { background: #333; color: #4CAF50; padding: 15px; text-align: left; font-weight: 600; }
    td { padding: 12px 15px; border-bottom: 1px solid #3a3a3a; }
    tr:hover { background: #333; }
    .status-success { color: #4CAF50; font-weight: bold; }
    .status-failed { color: #f44336; font-weight: bold; }
    .status-running { color: #2196F3; font-weight: bold; }
  </style>
</head>
<body>
  <h1>📊 pCloud Backup Dashboard</h1>
HTML_HEADER

  # Stats cards
  read -r total success failed avg_dur total_gb < <(_mysql "SELECT COUNT(*), SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END), SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END), ROUND(AVG(duration_sec)/60,1), ROUND(SUM(bytes_uploaded)/1073741824,2) FROM backup_runs WHERE started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)")
  
  cat >> "$OUTPUT" <<EOF
  <div class="stats">
    <div class="stat-card"><h3>Total Runs (30d)</h3><div class="value">$total</div></div>
    <div class="stat-card"><h3>Successful</h3><div class="value" style="color:#4CAF50">$success</div></div>
    <div class="stat-card"><h3>Failed</h3><div class="value" style="color:#f44336">$failed</div></div>
    <div class="stat-card"><h3>Avg Duration</h3><div class="value">${avg_dur}m</div></div>
    <div class="stat-card"><h3>Total Data</h3><div class="value">${total_gb} GB</div></div>
  </div>
  
  <h2>Recent Backups</h2>
  <table>
    <tr><th>Snapshot</th><th>Status</th><th>Started</th><th>Duration</th><th>Files</th><th>Size</th></tr>
EOF

  # Recent runs table
  _mysql "SELECT snapshot_name, status, started_at, duration_sec, files_uploaded, bytes_uploaded FROM backup_runs ORDER BY started_at DESC LIMIT 20" | while IFS=$'\t' read -r snapshot status started duration files bytes; do
    status_class="status-${status,,}"
    cat >> "$OUTPUT" <<EOF
    <tr>
      <td>$snapshot</td>
      <td class="$status_class">$status</td>
      <td>$started</td>
      <td>$(format_duration "$duration")</td>
      <td>${files:-0}</td>
      <td>$(format_bytes "${bytes:-0}")</td>
    </tr>
EOF
  done
  
  cat >> "$OUTPUT" <<'HTML_FOOTER'
  </table>
  <p style="margin-top:20px; color:#666; font-size:12px;">Auto-refresh every 5 minutes</p>
</body>
</html>
HTML_FOOTER

  echo "HTML dashboard generated: $OUTPUT"

else
  echo "Usage: $0 [recent|--last-n N|--failures|--stats|--current|html OUTPUT.html]"
  exit 1
fi
