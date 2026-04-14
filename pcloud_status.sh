#!/usr/bin/env bash
# =====================================================
# pCloud Backup Status Dashboard
# =====================================================
# Purpose: Query run history from SQLite DB and display status
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
PCLOUD_DB="${PCLOUD_DB:-/var/lib/pcloud-backup/runs.db}"
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

# Check if DB exists
if [[ ! -f "$PCLOUD_DB" ]]; then
  echo "⚠️  Database not found: $PCLOUD_DB"
  echo "Run at least one backup to initialize the database."
  exit 1
fi

# Check if sqlite3 is available
if ! command -v sqlite3 &>/dev/null; then
  echo "⚠️  sqlite3 command not found. Install with: sudo apt install sqlite3"
  exit 1
fi

# Helper: Format duration
format_duration() {
  local seconds="$1"
  if [[ -z "$seconds" || "$seconds" == "NULL" ]]; then
    echo "N/A"
  elif (( seconds < 60 )); then
    echo "${seconds}s"
  elif (( seconds < 3600 )); then
    printf "%dm %ds" $((seconds / 60)) $((seconds % 60))
  else
    printf "%dh %dm" $((seconds / 3600)) $(( (seconds % 3600) / 60 ))
  fi
}

# Helper: Format bytes
format_bytes() {
  local bytes="$1"
  if [[ -z "$bytes" || "$bytes" == "NULL" || "$bytes" == "0" ]]; then
    echo "0 B"
  elif (( bytes < 1024 )); then
    echo "${bytes} B"
  elif (( bytes < 1048576 )); then
    printf "%.1f KB" "$(bc -l <<< "$bytes / 1024")"
  elif (( bytes < 1073741824 )); then
    printf "%.1f MB" "$(bc -l <<< "$bytes / 1048576")"
  else
    printf "%.2f GB" "$(bc -l <<< "$bytes / 1073741824")"
  fi
}

# Helper: Status color
status_color() {
  case "$1" in
    SUCCESS) echo -e "${GREEN}✓ SUCCESS${NC}" ;;
    RUNNING) echo -e "${CYAN}⏳ RUNNING${NC}" ;;
    FAILED) echo -e "${RED}✗ FAILED${NC}" ;;
    PARTIAL) echo -e "${YELLOW}⚠ PARTIAL${NC}" ;;
    *) echo "$1" ;;
  esac
}

# ==================================================
# MODE: recent (default - show last N runs)
# ==================================================
show_recent() {
  local limit="${1:-10}"
  
  echo -e "${BOLD}╔════════════════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}║          pCloud Backup Status - Last $limit Runs                                 ║${NC}"
  echo -e "${BOLD}╠════════════════════════════════════════════════════════════════════════════╣${NC}"
  
  # Header
  printf "${BOLD}%-20s %-10s %-25s %-10s %-8s %-10s${NC}\n" \
    "Start Time" "Status" "Snapshot" "Duration" "Files" "Uploaded"
  echo -e "${BOLD}────────────────────────────────────────────────────────────────────────────${NC}"
  
 sqlite3 "$PCLOUD_DB" <<SQL | while IFS='|' read -r start status snapshot duration files uploaded gaps; do
SELECT 
  datetime(start_time, 'localtime') AS start,
  status,
  snapshot_name,
  COALESCE(duration_seconds, 0) AS duration,
  COALESCE(files_total, 0) AS files,
  COALESCE(bytes_uploaded, 0) AS bytes,
  COALESCE(gaps_backfilled, 0) AS gaps
FROM backup_runs
ORDER BY start_time DESC
LIMIT $limit;
SQL
    local status_col; status_col="$(status_color "$status")"
    local duration_fmt; duration_fmt="$(format_duration "$duration")"
    local uploaded_fmt; uploaded_fmt="$(format_bytes "$uploaded")"
    
    # Highlight gaps
    local gap_info=""
    if [[ "$gaps" != "0" && "$gaps" != "NULL" ]]; then
      gap_info=" ${YELLOW}(+$gaps gaps)${NC}"
    fi
    
    printf "%-20s %-22s %-25s %-10s %-8s %-10s%s\n" \
      "${start:0:16}" "$status_col" "${snapshot:0:25}" \
      "$duration_fmt" "$files" "$uploaded_fmt" "$gap_info"
  done
  
  echo -e "${BOLD}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
}

# ==================================================
# MODE: failures (show only failed backups)
# ==================================================
show_failures() {
  echo -e "${BOLD}${RED}╔════════════════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}${RED}║                    pCloud Backup Failures                                  ║${NC}"
  echo -e "${BOLD}${RED}╠════════════════════════════════════════════════════════════════════════════╣${NC}"
  
  local count; count=$(sqlite3 "$PCLOUD_DB" "SELECT COUNT(*) FROM backup_runs WHERE status = 'FAILED';")
  
  if [[ "$count" == "0" ]]; then
    echo -e "${GREEN}✓ No failures recorded!${NC}"
    echo -e "${BOLD}${RED}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
    return
  fi
  
  printf "${BOLD}%-20s %-25s %-10s %s${NC}\n" \
    "Timestamp" "Snapshot" "Exit Code" "Error"
  echo -e "${BOLD}────────────────────────────────────────────────────────────────────────────${NC}"
  
  sqlite3 "$PCLOUD_DB" <<SQL | while IFS='|' read -r ts snapshot code error; do
SELECT 
  datetime(start_time, 'localtime') AS ts,
  snapshot_name,
  COALESCE(exit_code, 'N/A') AS code,
  COALESCE(SUBSTR(error_message, 1, 40), 'No error message') AS error
FROM backup_runs
WHERE status = 'FAILED'
ORDER BY start_time DESC;
SQL
    printf "${RED}%-20s %-25s %-10s %s${NC}\n" \
      "${ts:0:16}" "${snapshot:0:25}" "$code" "${error:0:40}"
  done
  
  echo -e "${BOLD}${RED}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
}

# ==================================================
# MODE: stats (30-day statistics)
# ==================================================
show_stats() {
  echo -e "${BOLD}${CYAN}╔════════════════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}${CYAN}║          pCloud Backup Statistics (Last 30 Days)                           ║${NC}"
  echo -e "${BOLD}${CYAN}╠════════════════════════════════════════════════════════════════════════════╣${NC}"
  
  # Query stats
  sqlite3 "$PCLOUD_DB" <<SQL | while IFS='|' read -r total success failed avg_dur avg_files total_gb; do
SELECT 
  COUNT(*) AS total,
  SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS success,
  SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
  ROUND(AVG(duration_seconds), 1) AS avg_dur,
  ROUND(AVG(files_total), 0) AS avg_files,
  ROUND(SUM(bytes_uploaded) / 1024.0 / 1024.0 / 1024.0, 2) AS total_gb
FROM backup_runs
WHERE start_time >= datetime('now', '-30 days');
SQL
    local success_rate=0
    if [[ "$total" != "0" ]]; then
      success_rate=$(bc -l <<< "scale=1; ($success / $total) * 100")
    fi
    
    echo -e "Total Runs:          ${BOLD}$total${NC}"
    echo -e "Successful:          ${GREEN}$success${NC} (${success_rate}%)"
    echo -e "Failed:              ${RED}$failed${NC}"
    echo -e "Avg Duration:        $(format_duration "${avg_dur%.*}")"
    echo -e "Avg Files/Backup:    ${BOLD}$avg_files${NC}"
    echo -e "Total Data Uploaded: ${BOLD}${total_gb} GB${NC}"
  done
  
  echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
}

# ==================================================
# MODE: current (show running backup)
# ==================================================
show_current() {
  echo -e "${BOLD}${CYAN}╔════════════════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}${CYAN}║                    Currently Running Backup                                ║${NC}"
  echo -e "${BOLD}${CYAN}╠════════════════════════════════════════════════════════════════════════════╣${NC}"
  
  local running; running=$(sqlite3 "$PCLOUD_DB" "SELECT COUNT(*) FROM backup_runs WHERE status = 'RUNNING';")
  
  if [[ "$running" == "0" ]]; then
    echo -e "${YELLOW}⏸  No backup currently running${NC}"
    echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
    return
  fi
  
  sqlite3 "$PCLOUD_DB" <<SQL | while IFS='|' read -r run_id start snapshot elapsed phases; do
SELECT 
  run_id,
  datetime(start_time, 'localtime') AS start,
  snapshot_name,
  CAST((julianday('now') - julianday(start_time)) * 86400 AS INTEGER) AS elapsed,
  (SELECT GROUP_CONCAT(phase_name || ':' || status, ', ') FROM backup_phases WHERE run_id = backup_runs.run_id) AS phases
FROM backup_runs
WHERE status = 'RUNNING'
ORDER BY start_time DESC
LIMIT 1;
SQL
    echo -e "Run ID:      ${BOLD}$run_id${NC}"
    echo -e "Snapshot:    ${BOLD}$snapshot${NC}"
    echo -e "Started:     $start"
    echo -e "Elapsed:     $(format_duration "$elapsed")"
    echo -e "Phases:      $phases"
  done
  
  echo -e "${BOLD}${CYAN}╚════════════════════════════════════════════════════════════════════════════╝${NC}"
}

# ==================================================
# MODE: html (generate HTML dashboard)
# ==================================================
generate_html() {
  local output="$1"
  
  cat > "$output" <<'HTML_HEADER'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>pCloud Backup Dashboard</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif; background: #0a0e1a; color: #e2e8f0; margin: 0; padding: 2rem; }
    h1 { background: linear-gradient(135deg, #10b981 0%, #3b82f6 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    table { width: 100%; border-collapse: collapse; background: #111827; border-radius: 8px; overflow: hidden; margin: 1rem 0; }
    th { background: #1a1f2e; padding: 0.75rem; text-align: left; color: #10b981; font-weight: 600; }
    td { padding: 0.75rem; border-bottom: 1px solid #1e2530; }
    tr:last-child td { border-bottom: none; }
    .success { color: #10b981; }
    .failed { color: #ef4444; }
    .running { color: #60a5fa; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 2rem 0; }
    .stat-card { background: #111827; border: 1px solid #1f2937; border-radius: 8px; padding: 1rem; }
    .stat-value { font-size: 2rem; font-weight: 700; background: linear-gradient(135deg, #10b981 0%, #3b82f6 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .stat-label { color: #9ca3af; font-size: 0.85rem; text-transform: uppercase; }
  </style>
</head>
<body>
HTML_HEADER

  # Stats
  sqlite3 "$PCLOUD_DB" <<SQL >> "$output"
.mode html
SELECT '<h1>pCloud Backup Dashboard</h1>';
SELECT '<p style="color: #9ca3af;">Last updated: ' || datetime('now', 'localtime') || '</p>';

SELECT '<div class="stats">';
SELECT '<div class="stat-card"><div class="stat-label">Total Runs (30d)</div><div class="stat-value">' || COUNT(*) || '</div></div>' FROM backup_runs WHERE start_time >= datetime('now', '-30 days');
SELECT '<div class="stat-card"><div class="stat-label">Success Rate</div><div class="stat-value">' || ROUND((CAST(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS FLOAT) / COUNT(*)) * 100, 1) || '%</div></div>' FROM backup_runs WHERE start_time >= datetime('now', '-30 days');
SELECT '<div class="stat-card"><div class="stat-label">Total Uploaded (30d)</div><div class="stat-value">' || ROUND(SUM(bytes_uploaded) / 1024.0 / 1024.0 / 1024.0, 1) || ' GB</div></div>' FROM backup_runs WHERE start_time >= datetime('now', '-30 days');
SELECT '<div class="stat-card"><div class="stat-label">Avg Duration</div><div class="stat-value">' || ROUND(AVG(duration_seconds) / 60, 0) || ' min</div></div>' FROM backup_runs WHERE start_time >= datetime('now', '-30 days');
SELECT '</div>';

SELECT '<h2>Recent Backups (Last 20)</h2>';
SELECT '<table><tr><th>Timestamp</th><th>Status</th><th>Snapshot</th><th>Duration</th><th>Files</th><th>Uploaded</th></tr>';
SELECT 
  '<tr><td>' || datetime(start_time, 'localtime') || '</td>' ||
  '<td class="' || LOWER(status) || '">' || status || '</td>' ||
  '<td>' || snapshot_name || '</td>' ||
  '<td>' || COALESCE(ROUND(duration_seconds / 60.0, 1) || ' min', 'N/A') || '</td>' ||
  '<td>' || COALESCE(files_total, 0) || '</td>' ||
  '<td>' || ROUND(COALESCE(bytes_uploaded, 0) / 1024.0 / 1024.0, 1) || ' MB</td></tr>'
FROM backup_runs
ORDER BY start_time DESC
LIMIT 20;
SELECT '</table>';
SQL

  cat >> "$output" <<'HTML_FOOTER'
</body>
</html>
HTML_FOOTER

  echo "✓ HTML dashboard generated: $output"
}

# ==================================================
# Main
# ==================================================
case "$MODE" in
  --last-n)
    show_recent "$ARG"
    ;;
  --failures)
    show_failures
    ;;
  --stats)
    show_stats
    ;;
  --current)
    show_current
    ;;
  html)
    if [[ -z "$ARG" ]]; then
      echo "Usage: $0 html OUTPUT.html"
      exit 1
    fi
    generate_html "$ARG"
    ;;
  recent|*)
    show_recent 10
    ;;
esac
