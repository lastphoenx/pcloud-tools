#!/usr/bin/env bash
# =====================================================
# Status Aggregator - Collect all monitoring data
# =====================================================
# Purpose: Aggregate health status from all backup/monitoring services
# Output: JSON file with combined status for dashboard consumption
#
# Monitored Components:
#   - Systemd Services (entropy-watcher, clamav, honeyfile, cleanup, backup-pipeline)
#   - RTB Wrapper (via log parsing)
#   - pCloud Backup (via pcloud_health_check.sh)
#
# Output Location:
#   /opt/apps/monitoring/status.json (default)
#   Override with: MONITORING_OUTPUT=/path/to/status.json
#
# Usage:
#   ./aggregate_status.sh [--verbose]
# =====================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PCLOUD_HEALTH_CHECK="${SCRIPT_DIR}/../pcloud_health_check.sh"

# Output configuration
MONITORING_OUTPUT="${MONITORING_OUTPUT:-/opt/apps/monitoring/status.json}"
VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

# Ensure output directory exists
mkdir -p "$(dirname "$MONITORING_OUTPUT")"

# =====================================================
# Helper Functions
# =====================================================

log() {
  [[ $VERBOSE -eq 1 ]] && echo "[$(date '+%H:%M:%S')] $*" >&2
}

escape_json() {
  local str="$1"
  str="${str//\\/\\\\}"  # Backslash
  str="${str//\"/\\\"}"  # Quote
  str="${str//$'\n'/\\n}" # Newline
  str="${str//$'\r'/}"    # Carriage return
  str="${str//$'\t'/ }"   # Tab to space
  echo "$str"
}

# =====================================================
# Systemd Service Check
# =====================================================
# Returns: status (active|inactive|failed), last_start, exit_code, last_message
check_systemd_service() {
  local service_name="$1"
  local status="unknown"
  local last_start="never"
  local exit_code="unknown"
  local last_message="N/A"
  local enabled="unknown"
  
  # Check if service exists
  if ! systemctl list-unit-files "${service_name}.service" &>/dev/null; then
    echo "{\"status\":\"not_installed\",\"enabled\":\"no\",\"last_start\":\"never\",\"exit_code\":\"N/A\",\"message\":\"Service not found\"}"
    return
  fi
  
  # Get service status
  if systemctl is-active "${service_name}.service" &>/dev/null; then
    status="active"
  elif systemctl is-failed "${service_name}.service" &>/dev/null; then
    status="failed"
  else
    status="inactive"
  fi
  
  # Check if enabled
  if systemctl is-enabled "${service_name}.service" &>/dev/null; then
    enabled="yes"
  else
    enabled="no"
  fi
  
  # Get last start time and messages from journal
  if command -v journalctl &>/dev/null; then
    # Get last 3 lines from service journal
    local journal_output
    journal_output=$(journalctl -u "${service_name}.service" -n 3 --no-pager --output=short-iso 2>/dev/null || echo "")
    
    if [[ -n "$journal_output" ]]; then
      # Extract timestamp from last line
      last_start=$(echo "$journal_output" | tail -1 | awk '{print $1}' || echo "unknown")
      
      # Get exit code from journal
      exit_code=$(journalctl -u "${service_name}.service" -n 50 --no-pager 2>/dev/null | grep -oP 'code=exited, status=\K[0-9]+' | tail -1)
      [[ -z "$exit_code" ]] && exit_code="unknown"
      [[ "$exit_code" == "0" || "$exit_code" == "unknown" ]] && exit_code="${exit_code}" || exit_code="${exit_code} (error)"
      
      # Get last meaningful message (skip systemd boilerplate)
      last_message=$(echo "$journal_output" | tail -1 | sed -E 's/^[^ ]+ [^ ]+ [^ ]+ //' | head -c 200 || echo "N/A")
    fi
  fi
  
  # Get next run time (for timer-based services)
  local next_run="N/A"
  if systemctl list-timers "${service_name}.timer" --no-pager --no-legend 2>/dev/null | grep -q "${service_name}.timer"; then
    next_run=$(systemctl list-timers "${service_name}.timer" --no-pager --no-legend 2>/dev/null | awk '{print $1, $2, $3, $4}' | head -1 || echo "N/A")
  fi
  
  # Escape message for JSON
  last_message=$(escape_json "$last_message")
  next_run=$(escape_json "$next_run")
  
  echo "{\"status\":\"$status\",\"enabled\":\"$enabled\",\"last_start\":\"$last_start\",\"exit_code\":\"$exit_code\",\"next_run\":\"$next_run\",\"message\":\"$last_message\"}"
}

# =====================================================
# RTB Wrapper Log Parser
# =====================================================
# Parses /var/log/backup/rtb_wrapper.log for last run status
check_rtb_wrapper() {
  local rtb_log="/var/log/backup/rtb_wrapper.log"
  local status="unknown"
  local last_run="never"
  local message="N/A"
  local snapshot_count="0"
  local safety_gate="N/A"
  local details=""
  
  if [[ ! -f "$rtb_log" ]]; then
    echo "{\"status\":\"no_log\",\"last_run\":\"never\",\"snapshot_count\":0,\"message\":\"Log file not found: $rtb_log\"}"
    return
  fi
  
  # Get last 200 lines for parsing
  local log_tail
  log_tail=$(tail -200 "$rtb_log" 2>/dev/null || echo "")
  
  if [[ -z "$log_tail" ]]; then
    echo "{\"status\":\"empty_log\",\"last_run\":\"never\",\"snapshot_count\":0,\"message\":\"Log file is empty\"}"
    return
  fi
  
  # Extract last run timestamp (format: 2026-04-15 14:30:00)
  last_run=$(echo "$log_tail" | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' | tail -1 || echo "never")
  
  # Check for ABORT (Safety-Gate RED block)
  if echo "$log_tail" | tail -30 | grep -q '\[ABORT\]'; then
    status="blocked"
    local abort_line
    abort_line=$(echo "$log_tail" | grep '\[ABORT\]' | tail -1 | sed -E 's/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} //' || echo "")
    message="Safety-Gate BLOCKED backup: $abort_line"
    
    # Extract Safety-Gate status details
    if echo "$log_tail" | tail -50 | grep -q 'SAFETY-GATE: RED'; then
      safety_gate="RED"
      # Get detailed service status
      local nas_status nas_av_status honeyfile_status
      nas_status=$(echo "$log_tail" | grep -oP 'nas: (RED|YELLOW|GREEN)' | tail -1 | grep -oP '(RED|YELLOW|GREEN)' || echo "unknown")
      nas_av_status=$(echo "$log_tail" | grep -oP 'nas-av: (RED|YELLOW|GREEN)' | tail -1 | grep -oP '(RED|YELLOW|GREEN)' || echo "unknown")
      
      if echo "$log_tail" | tail -50 | grep -q 'Honeyfiles: kein verdächtiger Zugriff'; then
        honeyfile_status="OK"
      elif echo "$log_tail" | tail -50 | grep -q 'Honeyfile.*ALARM'; then
        honeyfile_status="ALARM"
      else
        honeyfile_status="unknown"
      fi
      
      details="Safety-Gate: RED | Honeyfiles: $honeyfile_status | nas: $nas_status | nas-av: $nas_av_status"
    elif echo "$log_tail" | tail -50 | grep -q 'SAFETY-GATE: YELLOW'; then
      safety_gate="YELLOW"
      details="Safety-Gate: YELLOW - Warning conditions detected"
    fi
  # Check for success
  elif echo "$log_tail" | tail -20 | grep -q '\[success\]'; then
    status="success"
    message=$(echo "$log_tail" | grep '\[success\]' | tail -1 | sed -E 's/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} //' || echo "Backup completed")
    safety_gate=$(echo "$log_tail" | tail -50 | grep -oP 'SAFETY-GATE: (GREEN|YELLOW|RED)' | tail -1 | grep -oP '(GREEN|YELLOW|RED)' || echo "GREEN")
  # Check for skip (no changes, lock unavailable, etc.)
  elif echo "$log_tail" | tail -20 | grep -q '\[skip\]'; then
    status="skipped"
    local skip_line
    skip_line=$(echo "$log_tail" | grep '\[skip\]' | tail -1 | sed -E 's/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} //' || echo "")
    
    # Determine skip reason
    if echo "$skip_line" | grep -qi 'keine.*änderungen\|no.*changes\|dry-run'; then
      details="No changes detected (rsync --dry-run)"
    elif echo "$skip_line" | grep -qi 'lock\|gesperrt'; then
      details="Lock unavailable - another backup running"
    elif echo "$skip_line" | grep -qi 'safety.*yellow'; then
      details="Safety-Gate: YELLOW - Skipped as precaution"
    else
      details="$skip_line"
    fi
    message="Skipped: $details"
  # Check for error
  elif echo "$log_tail" | tail -20 | grep -qi '\[error\]\|fail'; then
    status="failed"
    message=$(echo "$log_tail" | grep -iE '\[error\]|fail' | tail -1 | sed -E 's/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} //' || echo "Error detected")
  # Check for running
  elif echo "$log_tail" | tail -20 | grep -q '\[start\]'; then
    status="running"
    message="Backup currently running"
    # Check if safety gate check is in progress
    if echo "$log_tail" | tail -10 | grep -q 'Safety-Gate prüft'; then
      message="Running: Safety-Gate checks in progress"
    fi
  else
    status="unknown"
    message="No status markers found in recent log entries"
  fi
  
  # Count snapshots in RTB destination (if accessible)
  if [[ -d "/mnt/backup/rtb_nas" ]]; then
    snapshot_count=$(find /mnt/backup/rtb_nas -maxdepth 1 -type d -name "20*" 2>/dev/null | wc -l || echo "0")
  fi
  
  # Escape message and details
  message=$(escape_json "$message")
  details=$(escape_json "$details")
  
  # Optional: Live Safety-Gate check (current status, not historical)
  local live_safety_gate="N/A"
  if [[ -x "/opt/apps/entropywatcher/main/safety_gate.sh" ]] && [[ "$status" != "running" ]]; then
    # Only check if not currently running to avoid conflicts
    if /opt/apps/entropywatcher/main/safety_gate.sh &>/dev/null; then
      live_safety_gate="GREEN"
    else
      local sg_exit=$?
      case $sg_exit in
        1) live_safety_gate="YELLOW" ;;
        2) live_safety_gate="RED" ;;
        *) live_safety_gate="UNKNOWN" ;;
      esac
    fi
  fi
  
  # Build JSON with optional details field
  local json="{\"status\":\"$status\",\"last_run\":\"$last_run\",\"snapshot_count\":$snapshot_count,\"message\":\"$message\""
  if [[ -n "$details" ]]; then
    json="$json,\"details\":\"$details\""
  fi
  if [[ "$safety_gate" != "N/A" ]]; then
    json="$json,\"safety_gate\":\"$safety_gate\""
  fi
  if [[ "$live_safety_gate" != "N/A" ]]; then
    json="$json,\"live_safety_gate\":\"$live_safety_gate\""
  fi
  json="$json}"
  
  echo "$json"
}

# =====================================================
# pCloud Health Check Integration
# =====================================================
check_pcloud() {
  if [[ ! -x "$PCLOUD_HEALTH_CHECK" ]]; then
    echo "{\"status_code\":3,\"status_text\":\"UNKNOWN\",\"message\":\"pcloud_health_check.sh not found or not executable\"}"
    return
  fi
  
  # Run health check in JSON mode (don't use || because CRITICAL status exits with code 2)
  local pcloud_json
  pcloud_json=$("$PCLOUD_HEALTH_CHECK" --json 2>&1)
  local exit_code=$?
  
  # Check if we got valid JSON output (starts with { and has status_code)
  if [[ "$pcloud_json" =~ ^\{.*\"status_code\" ]]; then
    # Valid JSON - return as-is
    echo "$pcloud_json"
  else
    # Script failed before producing valid JSON - return error
    echo "{\"status_code\":3,\"status_text\":\"ERROR\",\"message\":\"Health check failed with exit code $exit_code\"}"
  fi
}

# =====================================================
# Main Aggregation Logic
# =====================================================

log "Starting status aggregation..."

# Define services to monitor
SYSTEMD_SERVICES=(
  "entropywatcher-nas"
  "entropywatcher-os"
  "entropywatcher-nas-av"
  "entropywatcher-os-av"
  "honeyfile-monitor"
  "cleanup-samba-recycle"
  "backup-pipeline"
)

# Start building JSON
TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
HOSTNAME=$(hostname)

log "Checking systemd services..."

# Check all systemd services
SERVICES_JSON=""
service_count=0
total_services=${#SYSTEMD_SERVICES[@]}

for service in "${SYSTEMD_SERVICES[@]}"; do
  log "  → $service"
  service_status=$(check_systemd_service "$service")
  service_count=$((service_count + 1))
  
  # Add comma only if not last service
  if [[ $service_count -lt $total_services ]]; then
    SERVICES_JSON="${SERVICES_JSON}    \"${service}\": ${service_status},\n"
  else
    SERVICES_JSON="${SERVICES_JSON}    \"${service}\": ${service_status}\n"
  fi
done

log "Checking RTB wrapper..."
RTB_JSON=$(check_rtb_wrapper)

log "Checking pCloud backup..."
PCLOUD_JSON=$(check_pcloud)

# Determine overall status
# Priority: failed > running > skipped > success > unknown
OVERALL_STATUS="OK"
EXIT_CODE=0

# Parse pCloud status (trim newlines)
PCLOUD_STATUS_CODE=$(echo "$PCLOUD_JSON" | grep -oP '"status_code":\s*\K[0-9]+' | head -1 | tr -d '\n' || echo "3")
if [[ "$PCLOUD_STATUS_CODE" -eq 2 ]]; then
  OVERALL_STATUS="CRITICAL"
  EXIT_CODE=2
elif [[ "$PCLOUD_STATUS_CODE" -eq 1 && "$OVERALL_STATUS" != "CRITICAL" ]]; then
  OVERALL_STATUS="WARNING"
  EXIT_CODE=1
fi

# Parse RTB status (trim newlines)
RTB_STATUS=$(echo "$RTB_JSON" | grep -oP '"status":\s*"\K[^"]+' | head -1 | tr -d '\n' || echo "unknown")
if [[ "$RTB_STATUS" == "failed" ]]; then
  OVERALL_STATUS="CRITICAL"
  EXIT_CODE=2
elif [[ "$RTB_STATUS" == "running" && "$OVERALL_STATUS" != "CRITICAL" ]]; then
  OVERALL_STATUS="RUNNING"
fi

# Check systemd service failures
if echo -e "$SERVICES_JSON" | grep -q '"status":"failed"'; then
  if [[ "$OVERALL_STATUS" != "CRITICAL" ]]; then
    OVERALL_STATUS="WARNING"
    EXIT_CODE=1
  fi
fi

# Build final JSON
log "Writing output to: $MONITORING_OUTPUT"

cat > "$MONITORING_OUTPUT" <<EOF
{
  "timestamp": "$TIMESTAMP",
  "hostname": "$HOSTNAME",
  "overall_status": "$OVERALL_STATUS",
  "exit_code": $EXIT_CODE,
  "services": {
$(echo -e "$SERVICES_JSON")
  },
  "scripts": {
    "rtb_wrapper": $RTB_JSON,
    "pcloud_backup": $PCLOUD_JSON
  }
}
EOF

# Set permissions (readable by web server)
chmod 644 "$MONITORING_OUTPUT"

log "Aggregation complete. Status: $OVERALL_STATUS"

exit $EXIT_CODE
