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
REPORTS_JSON="${REPORTS_JSON:-/opt/apps/monitoring/reports.json}"
DASHBOARD_URL="${DASHBOARD_URL:-http://$(hostname -I | awk '{print $1}'):8080}"
VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

# Paths to companion scripts (override via env)
ENTROPYWATCHER_SAFETY_GATE="${ENTROPYWATCHER_SAFETY_GATE:-/opt/apps/entropywatcher/main/safety_gate.sh}"
RTB_WRAPPER_SCRIPT="${RTB_WRAPPER_SCRIPT:-/opt/apps/rtb/rtb_pool_wrapper.sh}"
FORECAST_SAFETY_GATE="${FORECAST_SAFETY_GATE:-/opt/apps/entropywatcher/main/scripts/forecast_safety_gate.sh}"

# Exported: visible inside subshell command-substitution calls below
export LIVE_SG_STATUS="N/A" LIVE_SG_DETAILS="" LIVE_SG_EXPLAIN="" LIVE_SG_TS=""

# Ensure output directory exists
mkdir -p "$(dirname "$MONITORING_OUTPUT")"

# =====================================================
# Helper Functions
# =====================================================

log() {
  if [[ $VERBOSE -eq 1 ]]; then
    echo "[$(date '+%H:%M:%S')] $*" >&2
  fi
  return 0
}

escape_json() {
  # RFC 8259-safe string content (without surrounding quotes) for manual JSON assembly.
  if command -v python3 &>/dev/null; then
    printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1], end="")'
  elif command -v jq &>/dev/null; then
    jq -n --arg s "$1" '$s' | sed -e 's/^"//' -e 's/"$//'
  else
    local str="$1"
    str="${str//\\/\\\\}"
    str="${str//\"/\\\"}"
    str="${str//$'\n'/\\n}"
    str="${str//$'\r'/\\r}"
    str="${str//$'\t'/\\t}"
    str="${str//$'\f'/\\f}"
    str="${str//$'\b'/\\b}"
    echo "$str"
  fi
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
    ts_iso=$(date -d "$ts_raw" -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "")
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
  local timer_stalled="false"
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
        
        # Stale-Check: is next_run in the past?
        local next_epoch current_epoch
        next_epoch=$(date -d "${next_date} ${next_time}" +%s 2>/dev/null || echo "0")
        current_epoch=$(date +%s)
        if [[ "$next_epoch" -gt 0 && "$current_epoch" -gt "$next_epoch" ]]; then
          timer_stalled="true"
          # If service is inactive and timer missed, mark as stalled
          if [[ "$status" == "inactive" ]]; then
            status="stalled"
            last_message="Timer missed: expected run at ${next_run} but service did not start"
          fi
        fi
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
    if [[ -n "${LIVE_SG_EXPLAIN:-}" ]]; then
      local esc_sg_explain
      esc_sg_explain=$(escape_json "${LIVE_SG_EXPLAIN}")
      extra_fields="${extra_fields},\"live_sg_explain\":\"${esc_sg_explain}\""
    fi
  fi

  echo "{\"status\":\"$status\",\"enabled\":\"$enabled\",\"last_start\":\"$last_start\",\"exit_code\":\"$exit_code\",\"next_run\":\"$next_run\",\"timer_stalled\":$timer_stalled,\"message\":\"$last_message\"${extra_fields}}"
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
  # Check for successful pipeline completion ([done] — rtb_pool_wrapper uses this, not [success])
  elif echo "$log_tail" | tail -40 | grep -qE '\[done\].*(Backup-Pipeline komplett|pCloud-Sync erfolgreich|RTB erfolgreich)'; then
    status="success"
    message=$(echo "$log_tail" | grep -E '\[done\].*(Backup-Pipeline komplett|pCloud-Sync erfolgreich|RTB erfolgreich)' | tail -1 | sed -E 's/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} //' || echo "Backup completed")
    safety_gate=$(echo "$log_tail" | tail -50 | grep -oP 'SAFETY-GATE: (GREEN|YELLOW|RED)' | tail -1 | grep -oP '(GREEN|YELLOW|RED)' || echo "GREEN")
    if echo "$log_tail" | tail -40 | grep -q 'Delta-Check failed.*non-critical'; then
      details="Upload OK; tamper-detect nur GC-Hinweis (non-critical)"
    fi
  # Check for success (legacy marker)
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
  # Check for error (ignore non-critical delta-verify warnings after successful upload)
  elif echo "$log_tail" | tail -20 | grep -qiE '\[error\]|fail' \
    && ! echo "$log_tail" | tail -40 | grep -qE '\[done\].*(Backup-Pipeline komplett|pCloud-Sync erfolgreich)' \
    && ! echo "$log_tail" | tail -20 | grep -q 'non-critical, upload succeeded'; then
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
  # Call rtb_wrapper.sh --check-only: flock + cache; no backup pipeline lock.
  #   exit 1 + "changes_detected" → backup will fire next run
  #   exit 0 + "no_changes"       → no backup needed
  #   exit 0 + "no_baseline"      → no prior snapshot yet
  #   exit 3 + "check_busy"       → skipped (another check running, no cache)
  local dry_run_result="unknown"
  local dry_run_stale=0
  local dry_run_ts=""
  local dry_run_delta_json=""
  local dry_run_backup_scope_json=""
  local dry_run_pipeline_only_json=""
  local rtb_exclude_policy_json=""
  if [[ -x "${RTB_WRAPPER_SCRIPT}" ]]; then
    local check_out check_rc
    set +e
    check_out=$("${RTB_WRAPPER_SCRIPT}" --check-only 2>&1)
    check_rc=$?
    set -e
    dry_run_ts=$(date '+%Y-%m-%d %H:%M:%S')
    # Parse output - robust against formatted/prefixed messages
    if echo "$check_out" | grep -q "check_busy"; then
      dry_run_result="busy"
    elif echo "$check_out" | grep -q "changes_detected"; then
      dry_run_result="changes_detected"
    elif echo "$check_out" | grep -qE "no_changes|only pipeline"; then
      dry_run_result="no_changes"
    elif [[ "$check_rc" -eq 1 ]]; then
      dry_run_result="changes_detected"
    elif [[ "$check_rc" -eq 3 ]]; then
      dry_run_result="busy"
    elif [[ "$check_rc" -eq 0 ]]; then
      dry_run_result="no_changes"
    elif [[ -n "$check_out" ]]; then
      dry_run_result="no_changes"
    else
      dry_run_result="unavailable"
    fi
    if echo "$check_out" | grep -q "check_cached"; then
      dry_run_stale=1
    fi
    if echo "$check_out" | grep -q '^\[RTB Delta JSON\]'; then
      dry_run_delta_json=$(echo "$check_out" | grep '^\[RTB Delta JSON\]' | sed 's/^\[RTB Delta JSON\] //' | head -1)
      if command -v jq &>/dev/null; then
        echo "$dry_run_delta_json" | jq empty 2>/dev/null || dry_run_delta_json=""
      fi
    fi
    if echo "$check_out" | grep -q '^\[RTB BackupScope JSON\]'; then
      dry_run_backup_scope_json=$(echo "$check_out" | grep '^\[RTB BackupScope JSON\]' | sed 's/^\[RTB BackupScope JSON\] //' | head -1)
      if command -v jq &>/dev/null; then
        echo "$dry_run_backup_scope_json" | jq empty 2>/dev/null || dry_run_backup_scope_json=""
      fi
    fi
    if echo "$check_out" | grep -q '^\[RTB PipelineOnly JSON\]'; then
      dry_run_pipeline_only_json=$(echo "$check_out" | grep '^\[RTB PipelineOnly JSON\]' | sed 's/^\[RTB PipelineOnly JSON\] //' | head -1)
      if command -v jq &>/dev/null; then
        echo "$dry_run_pipeline_only_json" | jq empty 2>/dev/null || dry_run_pipeline_only_json=""
      fi
    fi
    if echo "$check_out" | grep -q '^\[RTB ExcludePolicy JSON\]'; then
      rtb_exclude_policy_json=$(echo "$check_out" | grep '^\[RTB ExcludePolicy JSON\]' | sed 's/^\[RTB ExcludePolicy JSON\] //' | head -1)
      if command -v jq &>/dev/null; then
        echo "$rtb_exclude_policy_json" | jq empty 2>/dev/null || rtb_exclude_policy_json=""
      fi
    fi
  else
    dry_run_result="unavailable"
    dry_run_ts=$(date '+%Y-%m-%d %H:%M:%S')
  fi

  # Escape message and details
  message=$(escape_json "$message")
  details=$(escape_json "$details")
  
  # Live Safety-Gate: pre-computed in main and exported — no duplicate invocations.
  local live_safety_gate="${LIVE_SG_STATUS:-N/A}"
  local live_sg_details="${LIVE_SG_DETAILS:-}"
  local live_sg_explain="${LIVE_SG_EXPLAIN:-}"

  # Build JSON with optional details field
  local json="{\"status\":\"$status\",\"last_run\":\"$last_run\",\"snapshot_count\":$snapshot_count,\"latest_snapshot\":\"$latest_snapshot\",\"message\":\"$message\""
  if [[ -n "$details" ]]; then
    json="$json,\"details\":\"$details\""
  fi
  if [[ "$safety_gate" != "N/A" ]]; then
    json="$json,\"safety_gate\":\"$safety_gate\""
  fi
  json="$json,\"live_safety_gate\":\"$live_safety_gate\""
  if [[ -n "$live_sg_details" ]]; then
    local esc_live_sg
    esc_live_sg=$(escape_json "$live_sg_details")
    json="$json,\"live_sg_details\":\"$esc_live_sg\""
  fi
  if [[ -n "$live_sg_explain" ]]; then
    local esc_live_sg_explain
    esc_live_sg_explain=$(escape_json "$live_sg_explain")
    json="$json,\"live_sg_explain\":\"$esc_live_sg_explain\""
  fi
  if [[ "$dry_run_result" != "unknown" ]]; then
    json="$json,\"dry_run_result\":\"$dry_run_result\""
    if [[ -n "$dry_run_ts" ]]; then
      json="$json,\"dry_run_ts\":\"$dry_run_ts\""
    fi
    if [[ "$dry_run_stale" -eq 1 ]]; then
      json="$json,\"dry_run_cached\":true"
    fi
  fi
  if [[ -n "$dry_run_delta_json" ]]; then
    json="$json,\"dry_run_delta\":${dry_run_delta_json}"
  fi
  if [[ -n "$dry_run_backup_scope_json" ]]; then
    json="$json,\"dry_run_backup_scope\":${dry_run_backup_scope_json}"
  fi
  if [[ -n "$dry_run_pipeline_only_json" ]]; then
    json="$json,\"dry_run_pipeline_only\":${dry_run_pipeline_only_json}"
  fi
  if [[ -n "$rtb_exclude_policy_json" ]]; then
    json="$json,\"exclude_policy\":${rtb_exclude_policy_json}"
  fi
  json="$json}"
  
  echo "$json"
}

# =====================================================
# RTB JSON enrichment (pool-mode / empty log fallbacks)
# =====================================================
# When rtb_wrapper.log is empty (common with pool pipeline) but DB/pCloud
# show healthy backups, derive status from reports.json + pCloud health check.
enrich_rtb_json() {
  local rtb_json="$1"
  local pcloud_json="$2"

  if ! command -v jq &>/dev/null; then
    echo "$rtb_json"
    return
  fi

  local rtb_status rtb_snaps pcloud_snaps last_db_status last_db_snap last_db_time new_status new_msg
  rtb_status=$(echo "$rtb_json" | jq -r '.status // "unknown"')
  rtb_snaps=$(echo "$rtb_json" | jq -r '.snapshot_count // 0')
  pcloud_snaps=$(echo "$pcloud_json" | jq -r '.checks.backup_age.snapshot_count // 0' 2>/dev/null || echo "0")

  if [[ "$pcloud_snaps" =~ ^[0-9]+$ ]] && [[ "$pcloud_snaps" -gt "$rtb_snaps" ]]; then
    rtb_json=$(echo "$rtb_json" | jq --argjson n "$pcloud_snaps" '.snapshot_count = $n')
  fi

  if [[ "$rtb_status" != "empty_log" && "$rtb_status" != "unknown" && "$rtb_status" != "no_log" ]]; then
    echo "$rtb_json"
    return
  fi

  if [[ ! -f "$REPORTS_JSON" ]]; then
    echo "$rtb_json"
    return
  fi

  last_db_status=$(jq -r '.recent_backups[0].status // empty' "$REPORTS_JSON" 2>/dev/null || echo "")
  last_db_snap=$(jq -r '.recent_backups[0].snapshot // empty' "$REPORTS_JSON" 2>/dev/null || echo "")
  last_db_time=$(jq -r '.recent_backups[0].finished_at // .recent_backups[0].started_at // empty' "$REPORTS_JSON" 2>/dev/null || echo "")

  if [[ -z "$last_db_status" ]]; then
    echo "$rtb_json"
    return
  fi

  case "$last_db_status" in
    SUCCESS) new_status="success" ; new_msg="Letzter DB-Lauf OK (Log leer — Pool-Pipeline)" ;;
    FAILED)  new_status="failed"  ; new_msg="Letzter DB-Lauf fehlgeschlagen (Log leer)" ;;
    RUNNING) new_status="running" ; new_msg="Backup läuft (DB)" ;;
    *)       new_status="idle"    ; new_msg="Status aus DB: ${last_db_status} (rtb_wrapper.log leer)" ;;
  esac

  rtb_json=$(echo "$rtb_json" | jq \
    --arg st "$new_status" \
    --arg msg "$new_msg" \
    --arg lr "$last_db_time" \
    --arg ls "$last_db_snap" \
    '.status = $st
     | .message = $msg
     | (if ($lr | length) > 0 then .last_run = $lr else . end)
     | (if ($ls | length) > 0 then .latest_snapshot = $ls else . end)
     | .status_source = "db_fallback"')

  echo "$rtb_json"
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
#               LIVE_SG_EXPLAIN (German human-readable summary for dashboard)

sg_format_window() {
  local w="$1"
  if [[ -z "$w" || "$w" == "0" ]]; then
    echo "?"
  elif [[ "$w" -ge 120 ]] && (( w % 60 == 0 )); then
    echo "$(( w / 60 )) h"
  else
    echo "${w} Min."
  fi
}

sg_status_to_de() {
  case "${1,,}" in
    green)  echo "GRÜN" ;;
    yellow) echo "GELB" ;;
    red)    echo "ROT" ;;
    *)      echo "$1" ;;
  esac
}

sg_reason_to_de() {
  local code="$1" svc="$2" flagged="$3" window="$4"
  local win_txt
  win_txt=$(sg_format_window "$window")
  case "$code" in
    no_recent_runs)
      if [[ "$svc" == *"-av"* ]]; then
        echo "kein ClamAV-Lauf im Zeitfenster (${win_txt})"
      else
        echo "kein Entropy-Scan im Zeitfenster (${win_txt})"
      fi ;;
    too_fresh_to_trust)
      echo "letzter Lauf noch zu frisch (Abkühlzeit)" ;;
    av_findings)
      echo "ClamAV-Funde im Zeitfenster" ;;
    flagged_files)
      echo "geflaggte Dateien im Zeitfenster (${flagged})" ;;
    *)
      echo "$code" ;;
  esac
}

build_live_sg_explain() {
  local sg_output="$1" honeyfile="$2"
  local -a parts=()

  if [[ "$honeyfile" == "ALARM" ]]; then
    parts+=("Honeyfiles: verdächtiger Zugriff erkannt")
  fi

  if command -v jq &>/dev/null; then
    local idx=0
    local svc_names=("nas" "nas-av")
    while IFS= read -r jline; do
      [[ -z "$jline" ]] && continue
      local svc="${svc_names[$idx]:-unknown}"
      idx=$((idx + 1))
      local st flagged window reason_de=""
      st=$(echo "$jline" | jq -r '.status // "unknown"')
      [[ "${st,,}" == "green" ]] && continue
      flagged=$(echo "$jline" | jq -r '.counters.flagged // 0')
      window=$(echo "$jline" | jq -r '.window_min // 0')
      while IFS= read -r r; do
        [[ -z "$r" ]] && continue
        reason_de+="$(sg_reason_to_de "$r" "$svc" "$flagged" "$window"); "
      done < <(echo "$jline" | jq -r '.reasons[]?' 2>/dev/null || true)
      reason_de="${reason_de%; }"
      parts+=("${svc}: $(sg_status_to_de "$st") — ${reason_de:-unbekannter Grund}")
    done < <(echo "$sg_output" | grep '^{' || true)
  fi

  case "${LIVE_SG_STATUS}" in
    GREEN)
      LIVE_SG_EXPLAIN="Safety-Gate GRÜN: Alle Prüfungen bestanden (Honeyfiles, nas, nas-av)." ;;
    YELLOW)
      if [[ ${#parts[@]} -gt 0 ]]; then
        local joined
        joined=$(IFS=' · '; echo "${parts[*]}")
        LIVE_SG_EXPLAIN="Safety-Gate GELB — ${joined}. Backup mit Warnung erlaubt (--strict blockiert)."
      else
        LIVE_SG_EXPLAIN="Safety-Gate GELB — Warnung ohne Detailgrund. Backup mit Warnung erlaubt (--strict blockiert)."
      fi ;;
    RED)
      if [[ ${#parts[@]} -gt 0 ]]; then
        local joined
        joined=$(IFS=' · '; echo "${parts[*]}")
        LIVE_SG_EXPLAIN="Safety-Gate ROT — ${joined}. Backup blockiert!"
      else
        LIVE_SG_EXPLAIN="Safety-Gate ROT — Backup blockiert (Ransomware-/Viren-Verdacht)."
      fi ;;
    *)
      LIVE_SG_EXPLAIN="" ;;
  esac
}

check_live_safety_gate() {
  if [[ ! -x "$ENTROPYWATCHER_SAFETY_GATE" ]]; then
    log "Live Safety-Gate: script not found at $ENTROPYWATCHER_SAFETY_GATE"
    LIVE_SG_STATUS="UNKNOWN"
    export LIVE_SG_STATUS
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

  build_live_sg_explain "$sg_output" "$honeyfile"

  export LIVE_SG_STATUS LIVE_SG_DETAILS LIVE_SG_EXPLAIN LIVE_SG_TS
  log "Live Safety-Gate: ${LIVE_SG_STATUS}${LIVE_SG_DETAILS:+ (${LIVE_SG_DETAILS})}${LIVE_SG_EXPLAIN:+ — $LIVE_SG_EXPLAIN}"
}

# =====================================================
# Timer Status Collector
# =====================================================
# Collects systemd timer status for EntropyWatcher and Backup-Pipeline
# Returns: JSON array with timer data
collect_timer_status() {
  local timer_services=(
    "entropywatcher-nas"
    "entropywatcher-nas-av"
    "entropywatcher-nas-av-weekly"
    "entropywatcher-os"
    "entropywatcher-os-av"
    "entropywatcher-os-av-weekly"
    "backup-pipeline"
  )
  
  local json_array=()
  local current_epoch
  current_epoch=$(date +%s)
  
  for unit in "${timer_services[@]}"; do
    local enabled active
    enabled="$(systemctl is-enabled "${unit}.timer" 2>/dev/null || echo "unknown")"
    active="$(systemctl is-active "${unit}.timer" 2>/dev/null || echo "unknown")"
    
    # Get timer info from systemctl list-timers
    local timer_line
    timer_line="$(systemctl list-timers "${unit}.timer" --no-pager 2>/dev/null | sed -n '2p')"
    
    local next_dt="n/a" last_dt="n/a" next_delta="n/a" last_delta="n/a"
    local next_epoch=0 last_epoch=0
    
    if [[ -n "$timer_line" ]]; then
      # Parse datetime (robust against different timezones)
      local dates
      dates="$(echo "$timer_line" | grep -oE '[A-Za-z]+ [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')"
      next_dt="$(echo "$dates" | head -1)"
      last_dt="$(echo "$dates" | tail -1)"
      
      # Calculate epochs
      if [[ "$next_dt" != "n/a" && -n "$next_dt" ]]; then
        next_epoch=$(date -d "$next_dt" +%s 2>/dev/null || echo 0)
        if [[ $next_epoch -gt 0 ]]; then
          local next_delta_sec=$((next_epoch - current_epoch))
          [[ $next_delta_sec -lt 0 ]] && next_delta_sec=0
          
          local next_d=$((next_delta_sec / 86400))
          local next_h=$(((next_delta_sec % 86400) / 3600))
          local next_m=$(((next_delta_sec % 3600) / 60))
          next_delta=$(printf "%02dd %02dh %02dm" $next_d $next_h $next_m)
        fi
      fi
      
      if [[ "$last_dt" != "n/a" && -n "$last_dt" ]]; then
        last_epoch=$(date -d "$last_dt" +%s 2>/dev/null || echo 0)
        if [[ $last_epoch -gt 0 ]]; then
          local last_delta_sec=$((current_epoch - last_epoch))
          [[ $last_delta_sec -lt 0 ]] && last_delta_sec=0
          
          local last_d=$((last_delta_sec / 86400))
          local last_h=$(((last_delta_sec % 86400) / 3600))
          local last_m=$(((last_delta_sec % 3600) / 60))
          last_delta=$(printf "%02dd %02dh %02dm" $last_d $last_h $last_m)
        fi
      fi
    fi
    
    # Build JSON object for this timer
    local timer_json
    timer_json=$(cat <<-TIMER_EOF
    {
      "unit": "$(escape_json "${unit}.timer")",
      "enabled": "$(escape_json "$enabled")",
      "active": "$(escape_json "$active")",
      "last_run": "$(escape_json "$last_dt")",
      "last_delta": "$(escape_json "$last_delta")",
      "next_run": "$(escape_json "$next_dt")",
      "next_delta": "$(escape_json "$next_delta")"
    }
TIMER_EOF
    )
    
    json_array+=("$timer_json")
  done
  
  # Join array elements with commas
  local result="["
  for i in "${!json_array[@]}"; do
    result+="${json_array[$i]}"
    if [[ $i -lt $((${#json_array[@]} - 1)) ]]; then
      result+=","
    fi
  done
  result+="]"
  
  echo "$result"
}

# =====================================================
# Safety-Gate Forecast (predict next run status)
# =====================================================
# Returns: JSON object with forecast data or empty string if N/A
get_safety_gate_forecast() {
  if [[ ! -x "$FORECAST_SAFETY_GATE" ]]; then
    echo ""
    return
  fi

  local forecast_output forecast_status
  set +e
  forecast_output=$("$FORECAST_SAFETY_GATE" 2>/dev/null || echo "")
  set -e

  if [[ -z "$forecast_output" ]]; then
    echo ""
    return
  fi

  # forecast_safety_gate.sh liefert formatierte Output - wir extrahieren KEY INFO
  # Beispiel Output: "2026-04-19 12:00 | GREEN | nas: OK, nas-av: OK, honeyfile: OK"
  # Parse first line für forecast
  local forecast_line
  forecast_line=$(echo "$forecast_output" | head -1)
  
  if [[ "$forecast_line" =~ (GREEN|YELLOW|RED) ]]; then
    forecast_status="${BASH_REMATCH[1]}"
    local forecast_time
    forecast_time=$(echo "$forecast_line" | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}' | head -1 || echo "")
    
    if [[ -n "$forecast_time" ]]; then
      local esc_output
      esc_output=$(escape_json "$forecast_output")
      echo ",\"safety_gate_forecast\":{\"next_run\":\"${forecast_time}\",\"predicted_status\":\"${forecast_status}\",\"details\":\"${esc_output}\"}"
    else
      echo ""
    fi
  else
    echo ""
  fi
}

# =====================================================
# Main Aggregation Logic
# =====================================================

log "Starting status aggregation..."

log "Checking Live Safety-Gate..."
check_live_safety_gate

log "Getting Safety-Gate forecast..."
SG_FORECAST=$(get_safety_gate_forecast)

log "Collecting timer status..."
TIMER_STATUS_JSON=$(collect_timer_status)

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
SERVER_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "Europe/Berlin")

log "Checking systemd services..."

# Check all systemd services
SERVICES_JSON=""
service_count=0
total_services=${#SYSTEMD_SERVICES[@]}

for service in "${SYSTEMD_SERVICES[@]}"; do
  log "  → $service"
  service_status=$(check_systemd_service "$service")
  service_count=$((service_count + 1))
  
  # Real newlines only — never echo -e this blob (would reinterpret \\ in escaped messages).
  if [[ $service_count -lt $total_services ]]; then
    SERVICES_JSON="${SERVICES_JSON}    \"${service}\": ${service_status},
"
  else
    SERVICES_JSON="${SERVICES_JSON}    \"${service}\": ${service_status}
"
  fi
done

log "Checking RTB wrapper..."
RTB_JSON=$(check_rtb_wrapper)

log "Checking pCloud backup..."
PCLOUD_JSON=$(check_pcloud)

log "Enriching RTB status (pool-mode / DB fallback)..."
RTB_JSON=$(enrich_rtb_json "$RTB_JSON" "$PCLOUD_JSON")

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
if printf '%s' "$SERVICES_JSON" | grep -q '"status":"failed"'; then
  if [[ "$OVERALL_STATUS" != "CRITICAL" ]]; then
    OVERALL_STATUS="WARNING"
    EXIT_CODE=1
  fi
fi

# Build final JSON
log "Writing output to: $MONITORING_OUTPUT"

# Aggregate malware/entropy stats from entropywatcher services
TOTAL_FLAGGED=0
TOTAL_MISSING=0
JUMP_ALERTS=0
FLAGGED_STABLE_HIGH=0
FLAGGED_NEW_LAST=0
MISSING_RECENT=0
AV_EVENTS_7D=0
TOTAL_ACTIVE=0
TOTAL_ERRORS=0
SG_SUMMARY="${LIVE_SG_STATUS:-N/A}"

# Read integrity metrics from reports.json (preferred) with legacy fallbacks
if [[ -f "$REPORTS_JSON" ]] && command -v jq &>/dev/null; then
  TOTAL_FLAGGED=$(jq -r '.entropywatcher.integrity_summary.flagged_total // ([.entropywatcher.flagged_files | to_entries[] | .value] | add // 0)' "$REPORTS_JSON" 2>/dev/null || echo "0")
  TOTAL_MISSING=$(jq -r '.entropywatcher.integrity_summary.missing_total // (.entropywatcher.missing_files | length) // 0' "$REPORTS_JSON" 2>/dev/null || echo "0")
  JUMP_ALERTS=$(jq -r '.entropywatcher.integrity_summary.jump_alerts_window // 0' "$REPORTS_JSON" 2>/dev/null || echo "0")
  FLAGGED_STABLE_HIGH=$(jq -r '.entropywatcher.integrity_summary.flagged_stable_high // 0' "$REPORTS_JSON" 2>/dev/null || echo "0")
  FLAGGED_NEW_LAST=$(jq -r '.entropywatcher.integrity_summary.flagged_new_last_scan // 0' "$REPORTS_JSON" 2>/dev/null || echo "0")
  MISSING_RECENT=$(jq -r '.entropywatcher.integrity_summary.missing_recent // 0' "$REPORTS_JSON" 2>/dev/null || echo "0")
  AV_EVENTS_7D=$(jq -r '.entropywatcher.av_events | length' "$REPORTS_JSON" 2>/dev/null || echo "0")
fi

for service in "${SYSTEMD_SERVICES[@]}"; do
  if [[ "$service" == entropywatcher-* ]] || [[ "$service" == "honeyfile-monitor" ]]; then
    service_data=$(printf '%s' "$SERVICES_JSON" | grep "\"$service\"" -A 20 || echo "")
    if [[ -n "$service_data" ]]; then
      # Count active services
      if echo "$service_data" | grep -q '"status":\s*"active"'; then
        TOTAL_ACTIVE=$((TOTAL_ACTIVE + 1))
      fi
      
      # Count errors
      if echo "$service_data" | grep -q '"status":\s*"failed"'; then
        TOTAL_ERRORS=$((TOTAL_ERRORS + 1))
      fi
    fi
  fi
done

ESC_DASHBOARD_URL=$(escape_json "$DASHBOARD_URL")

cat > "$MONITORING_OUTPUT" <<EOF
{
  "timestamp": "$TIMESTAMP",
  "hostname": "$HOSTNAME",
  "server_timezone": "$SERVER_TZ",
  "dashboard_url": "$ESC_DASHBOARD_URL",
  "overall_status": "$OVERALL_STATUS",
  "exit_code": $EXIT_CODE,
  "live_safety_gate": "${LIVE_SG_STATUS:-N/A}",
  "live_sg_details": "$(escape_json "${LIVE_SG_DETAILS:-}")",
  "live_sg_explain": "$(escape_json "${LIVE_SG_EXPLAIN:-}")",
  "services": {
$(printf '%s' "$SERVICES_JSON")
  },
  "scripts": {
    "rtb_wrapper": $RTB_JSON,
    "pcloud_backup": $PCLOUD_JSON
  },
  "malware_summary": {
    "active_monitors": $TOTAL_ACTIVE,
    "jump_alerts_window": $JUMP_ALERTS,
    "flagged_new_last_scan": $FLAGGED_NEW_LAST,
    "flagged_stable_high": $FLAGGED_STABLE_HIGH,
    "av_events_7d": $AV_EVENTS_7D,
    "missing_recent": $MISSING_RECENT,
    "safety_gate": "$SG_SUMMARY",
    "flagged": $TOTAL_FLAGGED,
    "missing": $TOTAL_MISSING,
    "errors": $TOTAL_ERRORS
  },
  "timers": $TIMER_STATUS_JSON${SG_FORECAST}
}
EOF

# Set permissions (readable by web server)
chmod 644 "$MONITORING_OUTPUT"

if command -v jq &>/dev/null; then
  if ! jq empty "$MONITORING_OUTPUT" 2>/dev/null; then
    log "ERROR: invalid JSON written to $MONITORING_OUTPUT"
    exit 3
  fi
fi

log "Aggregation complete. Status: $OVERALL_STATUS"

exit $EXIT_CODE
