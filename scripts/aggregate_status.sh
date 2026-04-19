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

# Paths to companion scripts (override via env)
ENTROPYWATCHER_SAFETY_GATE="${ENTROPYWATCHER_SAFETY_GATE:-/opt/apps/entropywatcher/main/safety_gate.sh}"
RTB_WRAPPER_SCRIPT="${RTB_WRAPPER_SCRIPT:-/opt/apps/rtb/rtb_wrapper.sh}"

# Exported: visible inside subshell command-substitution calls below
export LIVE_SG_STATUS="N/A" LIVE_SG_DETAILS="" LIVE_SG_TS=""

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
  
  # ── Reliable timestamps + exit code via systemctl show ──────────────
  # InactiveEnterTimestamp = when service last finished (for inactive oneshot).
  # ActiveEnterTimestamp   = when service first became active (= current start).
  # ExecMainStatus         = last exit code of the main process (persistent).
  local show_props
  show_props=$(systemctl show "${service_name}.service" \
    -p InactiveEnterTimestamp,ActiveEnterTimestamp,ExecMainStatus \
    2>/dev/null || echo "")

  if [[ "$show_props" =~ ExecMainStatus=([0-9]+) ]]; then
    exit_code="${BASH_REMATCH[1]}"
  fi

  # Pick the most useful timestamp
  local ts_raw=""
  if [[ "$status" == "active" ]]; then
    ts_raw=$(echo "$show_props" | grep -oP 'ActiveEnterTimestamp=\K.+' | head -1 || echo "")
  else
    ts_raw=$(echo "$show_props" | grep -oP 'InactiveEnterTimestamp=\K.+' | head -1 || echo "")
  fi
  if [[ -n "$ts_raw" && "$ts_raw" != "n/a" ]]; then
    # Convert to ISO 8601 (fmtTs in dashboard expects parseable date string)
    local ts_iso
    ts_iso=$(date -d "$ts_raw" '+%Y-%m-%dT%H:%M:%S' 2>/dev/null || echo "")
    [[ -n "$ts_iso" ]] && last_start="$ts_iso"
  fi

  # ── Last meaningful message from journal ────────────────────────────
  # Skip systemd boilerplate: "Consumed N CPU time", "Started/Starting/Stopped/..."
  if command -v journalctl &>/dev/null; then
    local journal_output
    journal_output=$(journalctl -u "${service_name}.service" -n 15 \
      --no-pager --output=cat 2>/dev/null || echo "")
    if [[ -n "$journal_output" ]]; then
      local filtered
      filtered=$(echo "$journal_output" | grep -vE \
        'Consumed [0-9]|^Starting |^Stopping |^Started |^Stopped |^Deactivated |^Finished ' \
        | tail -1 | head -c 200 || echo "")
      [[ -n "$filtered" ]] && last_message="$filtered" || \
        last_message=$(echo "$journal_output" | tail -1 | head -c 200)
    fi
  fi

  # Annotate non-zero exit codes with context
  if [[ "$service_name" == "backup-pipeline" ]] && [[ "$exit_code" =~ ^[12]$ ]]; then
    exit_code="${exit_code} (blocked)"
    last_message="Safety-Gate blockierte vorherigen Lauf. Live: ${LIVE_SG_STATUS:-N/A}. Nachricht: ${last_message}"
  elif [[ "$exit_code" != "0" && "$exit_code" != "unknown" ]] && [[ "$exit_code" =~ ^[0-9]+$ ]]; then
    exit_code="${exit_code} (error)"
  fi
  
  # Get next run time (for timer-based services)
  # Parse timer info from systemctl list-timers and convert to ISO 8601
  local next_run="N/A"
  if systemctl list-timers "${service_name}.timer" --no-pager --no-legend 2>/dev/null | grep -q "${service_name}.timer"; then
    local timer_line
    timer_line=$(systemctl list-timers "${service_name}.timer" --no-pager --no-legend 2>/dev/null | sed -n '1p')
    if [[ -n "$timer_line" ]]; then
      # Extract NEXT datetime: "Day YYYY-MM-DD HH:MM:SS TZ ..." → take columns 2+3
      local next_date next_time
      next_date=$(echo "$timer_line" | awk '{print $2}')
      next_time=$(echo "$timer_line" | awk '{print $3}')
      if [[ "$next_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] && [[ "$next_time" =~ ^[0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]; then
        next_run="${next_date}T${next_time}"  # ISO 8601 format
      else
        next_run="N/A"
      fi
    fi
  fi
  
  # Escape message for JSON
  last_message=$(escape_json "$last_message")
  next_run=$(escape_json "$next_run")
  
  # For backup-pipeline: attach live safety-gate data (exported from main)
  local extra_fields=""
  if [[ "$service_name" == "backup-pipeline" ]] && [[ "${LIVE_SG_STATUS:-N/A}" != "N/A" ]]; then
    extra_fields=",\"live_safety_gate\":\"${LIVE_SG_STATUS}\""
    if [[ -n "${LIVE_SG_DETAILS:-}" ]]; then
      local esc_sg
      esc_sg=$(escape_json "${LIVE_SG_DETAILS}")
      extra_fields="${extra_fields},\"live_sg_details\":\"${esc_sg}\""
    fi
  fi

  echo "{\"status\":\"$status\",\"enabled\":\"$enabled\",\"last_start\":\"$last_start\",\"exit_code\":\"$exit_code\",\"next_run\":\"$next_run\",\"message\":\"$last_message\"${extra_fields}}"
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
  
  # Count snapshots + latest snapshot name from RTB filesystem (authoritative source of truth)
  local latest_snapshot=""
  if [[ -d "/mnt/backup/rtb_nas" ]]; then
    snapshot_count=$(find /mnt/backup/rtb_nas -maxdepth 1 -type d -name "20*" 2>/dev/null | wc -l || echo "0")
    latest_snapshot=$(find /mnt/backup/rtb_nas -maxdepth 1 -type d -name "20*" 2>/dev/null \
      | sort -r | head -1 | xargs -r basename 2>/dev/null || echo "")
  fi

  # ---- Live Dry-Run pre-check ----
  # Call rtb_wrapper.sh --check-only: pure rsync -ni, no lock / no log / no backup.
  #   exit 1 + "changes_detected" → backup will fire next run
  #   exit 0 + "no_changes"       → no backup needed
  #   exit 0 + "no_baseline"      → no prior snapshot yet
  local dry_run_result="unknown"
  local dry_run_ts=""
  if [[ -x "${RTB_WRAPPER_SCRIPT}" ]]; then
    local check_out check_rc
    set +e
    check_out=$("${RTB_WRAPPER_SCRIPT}" --check-only 2>/dev/null)
    check_rc=$?
    set -e
    dry_run_ts=$(date '+%Y-%m-%d %H:%M:%S')
    case "$check_out" in
      changes_detected)        dry_run_result="changes_detected" ;;
      no_changes|no_baseline)  dry_run_result="no_changes" ;;
    esac
  fi

  # Escape message and details
  message=$(escape_json "$message")
  details=$(escape_json "$details")
  
  # Live Safety-Gate: pre-computed in main and exported — no duplicate invocations.
  local live_safety_gate="${LIVE_SG_STATUS:-N/A}"
  local live_sg_details="${LIVE_SG_DETAILS:-}"

  # Build JSON with optional details field
  local json="{\"status\":\"$status\",\"last_run\":\"$last_run\",\"snapshot_count\":$snapshot_count,\"latest_snapshot\":\"$latest_snapshot\",\"message\":\"$message\""
  if [[ -n "$details" ]]; then
    json="$json,\"details\":\"$details\""
  fi
  if [[ "$safety_gate" != "N/A" ]]; then
    json="$json,\"safety_gate\":\"$safety_gate\""
  fi
  if [[ "$live_safety_gate" != "N/A" ]]; then
    json="$json,\"live_safety_gate\":\"$live_safety_gate\""
    if [[ -n "$live_sg_details" ]]; then
      local esc_live_sg
      esc_live_sg=$(escape_json "$live_sg_details")
      json="$json,\"live_sg_details\":\"$esc_live_sg\""
    fi
  fi
  if [[ "$dry_run_result" != "unknown" ]]; then
    json="$json,\"dry_run_result\":\"$dry_run_result\""
    if [[ -n "$dry_run_ts" ]]; then
      json="$json,\"dry_run_ts\":\"$dry_run_ts\""
    fi
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
# Live Safety-Gate Check (run once in main, result shared)
# =====================================================
# Sets+exports: LIVE_SG_STATUS (GREEN|YELLOW|RED|UNKNOWN|N/A)
#               LIVE_SG_DETAILS ("Honeyfiles: OK | nas: GREEN | nas-av: GREEN")
check_live_safety_gate() {
  if [[ ! -x "$ENTROPYWATCHER_SAFETY_GATE" ]]; then
    log "Live Safety-Gate: script not found at $ENTROPYWATCHER_SAFETY_GATE"
    return
  fi

  local sg_output sg_exit
  set +e
  sg_output=$("$ENTROPYWATCHER_SAFETY_GATE" 2>&1)
  sg_exit=$?
  set -e

  case $sg_exit in
    0) LIVE_SG_STATUS="GREEN" ;;
    1) LIVE_SG_STATUS="YELLOW" ;;
    2) LIVE_SG_STATUS="RED" ;;
    *) LIVE_SG_STATUS="UNKNOWN" ;;
  esac
  LIVE_SG_TS=$(date '+%Y-%m-%dT%H:%M:%SZ')

  # Parse individual component states from safety_gate.sh stdout
  local honeyfile nas nas_av
  if echo "$sg_output" | grep -q 'kein verdächtiger Zugriff'; then
    honeyfile="OK"
  elif echo "$sg_output" | grep -qE 'HONEYFILE-ALARM|Honeyfile-Alarm'; then
    honeyfile="ALARM"
  else
    honeyfile="unknown"
  fi
  # " nas: GREEN" but NOT "nas-av: GREEN" — space-prefix distinguishes them
  nas=$(echo "$sg_output"    | grep -oP '(?<= nas: )(GREEN|YELLOW|RED)'    | head -1 || echo "")
  nas_av=$(echo "$sg_output" | grep -oP '(?<=nas-av: )(GREEN|YELLOW|RED)'  | head -1 || echo "")

  if [[ -n "$nas" || -n "$nas_av" ]]; then
    LIVE_SG_DETAILS="Honeyfiles: ${honeyfile} | nas: ${nas:-?} | nas-av: ${nas_av:-?}"
  fi

  export LIVE_SG_STATUS LIVE_SG_DETAILS LIVE_SG_TS
  log "Live Safety-Gate: ${LIVE_SG_STATUS}${LIVE_SG_DETAILS:+ (${LIVE_SG_DETAILS})}"
}

# =====================================================
# Main Aggregation Logic
# =====================================================

log "Starting status aggregation..."

log "Checking Live Safety-Gate..."
check_live_safety_gate

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
