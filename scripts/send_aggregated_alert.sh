#!/usr/bin/env bash
# =====================================================
# Aggregated Alert Script - Multi-Service Monitoring
# =====================================================
# Purpose: Run aggregate status check and send alerts on changes
# Usage: ./send_aggregated_alert.sh [--force] [--test]
#
# Options:
#   --force   Send alert even if status hasn't changed
#   --test    Send test notification with current status
#
# This script uses aggregate_status.sh to collect status from:
#   - Systemd Services (entropy-watcher, clamav, honeyfile, etc.)
#   - RTB Wrapper backups
#   - pCloud backups
#
# Alerts are only sent when overall_status changes to prevent spam
# =====================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGGREGATOR="${SCRIPT_DIR}/aggregate_status.sh"
STATUS_JSON="${STATUS_JSON:-/opt/apps/monitoring/status.json}"
STATE_FILE="${SCRIPT_DIR}/.aggregated_status_last"

# Define notification tags (same as send_alert.sh)
NOTIFICATION_TAGS=("telegram" "discord" "ntfy")

# Auto-discover Apprise config location
APPRISE_CONFIG=""
for config_path in \
  "/opt/apps/apprise.yml" \
  "${HOME}/.config/apprise/apprise.yml" \
  "${SCRIPT_DIR}/../apprise.yml" \
  "/etc/apprise/apprise.yml"; do
  if [[ -f "$config_path" ]]; then
    APPRISE_CONFIG="$config_path"
    break
  fi
done

# Ensure aggregator exists
if [[ ! -f "$AGGREGATOR" ]]; then
  echo "ERROR: Aggregator script not found: $AGGREGATOR"
  exit 1
fi

# Check if apprise is installed
if ! command -v apprise &>/dev/null; then
  echo "ERROR: apprise is not installed"
  exit 1
fi

# Check if config exists
if [[ -z "$APPRISE_CONFIG" ]]; then
  echo "ERROR: No Apprise config found!"
  exit 1
fi

echo "Using config: $APPRISE_CONFIG"

# Parse arguments
FORCE=0
TEST=0
[[ "${1:-}" == "--force" ]] && FORCE=1
[[ "${1:-}" == "--test" ]] && TEST=1

# =====================================================
# TEST MODE
# =====================================================
if [[ $TEST -eq 1 ]]; then
  echo "Running aggregator to get current status..."
  bash "$AGGREGATOR" --verbose
  
  if [[ ! -f "$STATUS_JSON" ]]; then
    echo "ERROR: Status file not created: $STATUS_JSON"
    exit 1
  fi
  
  OVERALL_STATUS=$(grep -oP '"overall_status":\s*"\K[^"]+' "$STATUS_JSON" || echo "UNKNOWN")
  HOSTNAME=$(grep -oP '"hostname":\s*"\K[^"]+' "$STATUS_JSON" || hostname)
  
  # Determine emoji
  case $OVERALL_STATUS in
    OK) EMOJI="✅" ;;
    WARNING) EMOJI="⚠️" ;;
    CRITICAL) EMOJI="🚨" ;;
    RUNNING) EMOJI="🔄" ;;
    *) EMOJI="❓" ;;
  esac
  
  TITLE="🧪 Test Alert - System Monitoring ($HOSTNAME)"
  BODY="Current System Status: $EMOJI $OVERALL_STATUS

This is a test notification showing your current backup/monitoring status.
View full details at: $STATUS_JSON

Run 'aggregate_status.sh --verbose' for detailed output."
  
  echo "Sending test notification to all configured services..."
  for tag in "${NOTIFICATION_TAGS[@]}"; do
    echo "  → Sending to: $tag"
    apprise --config="$APPRISE_CONFIG" \
      --tag="$tag" \
      --title="$TITLE" \
      --body="$BODY" \
      --notification-type=info
  done
  echo "Test notifications sent!"
  exit 0
fi

# =====================================================
# NORMAL MODE: Check status and alert on change
# =====================================================

echo "Running status aggregation..."
bash "$AGGREGATOR"

if [[ ! -f "$STATUS_JSON" ]]; then
  echo "ERROR: Status file not created: $STATUS_JSON"
  exit 1
fi

# Extract status
OVERALL_STATUS=$(grep -oP '"overall_status":\s*"\K[^"]+' "$STATUS_JSON" || echo "UNKNOWN")
EXIT_CODE=$(grep -oP '"exit_code":\s*\K[0-9]+' "$STATUS_JSON" || echo "3")
HOSTNAME=$(grep -oP '"hostname":\s*"\K[^"]+' "$STATUS_JSON" || hostname)

# Read last status
LAST_STATUS="UNKNOWN"
if [[ -f "$STATE_FILE" ]]; then
  LAST_STATUS=$(cat "$STATE_FILE")
fi

# Check if status changed or forced
SEND_ALERT=0
ALERT_REASON=""
if [[ $FORCE -eq 1 ]]; then
  SEND_ALERT=1
  ALERT_REASON="Forced alert"
elif [[ "$OVERALL_STATUS" != "$LAST_STATUS" ]]; then
  SEND_ALERT=1
  ALERT_REASON="Status changed: $LAST_STATUS → $OVERALL_STATUS"
fi

# Send alert if needed
if [[ $SEND_ALERT -eq 1 ]]; then
  # Determine notification type and emoji
  case $OVERALL_STATUS in
    OK) NOTIF_TYPE="success"; EMOJI="✅" ;;
    WARNING) NOTIF_TYPE="warning"; EMOJI="⚠️" ;;
    CRITICAL) NOTIF_TYPE="failure"; EMOJI="🚨" ;;
    RUNNING) NOTIF_TYPE="info"; EMOJI="🔄" ;;
    *) NOTIF_TYPE="info"; EMOJI="❓" ;;
  esac
  
  # Build summary from status.json
  FAILED_SERVICES=$(grep -oP '"status":"failed"' "$STATUS_JSON" | wc -l || echo "0")
  INACTIVE_SERVICES=$(grep -oP '"status":"inactive"' "$STATUS_JSON" | wc -l || echo "0")
  
  # Build alert message
  TITLE="$EMOJI $OVERALL_STATUS - System Monitoring ($HOSTNAME)"
  BODY="Overall Status: $OVERALL_STATUS
Reason: $ALERT_REASON

Summary:
  • Failed Services: $FAILED_SERVICES
  • Inactive Services: $INACTIVE_SERVICES

Timestamp: $(date '+%Y-%m-%d %H:%M:%S')

View detailed status:
  cat $STATUS_JSON
  
Or run:
  /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh --verbose"

  # Send via Apprise to all configured services
  echo "Sending alert: $OVERALL_STATUS"
  for tag in "${NOTIFICATION_TAGS[@]}"; do
    echo "  → Sending to: $tag"
    apprise --config="$APPRISE_CONFIG" \
      --tag="$tag" \
      --title="$TITLE" \
      --body="$BODY" \
      --notification-type="$NOTIF_TYPE"
  done
  
  echo "Alerts sent to all services!"
else
  echo "Status unchanged ($OVERALL_STATUS) - no alert sent"
fi

# Save current status for next run
echo "$OVERALL_STATUS" > "$STATE_FILE"

exit 0
