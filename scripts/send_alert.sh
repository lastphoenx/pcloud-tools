#!/usr/bin/env bash
# =====================================================
# Send Alert Script - Apprise Integration
# =====================================================
# Purpose: Run health check and send alerts on status changes
# Usage: ./send_alert.sh [--force] [--test]
#
# Options:
#   --force   Send alert even if status hasn't changed
#   --test    Send test notification
# =====================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HEALTH_CHECK="${SCRIPT_DIR}/../pcloud_health_check.sh"
APPRISE_CONFIG="${SCRIPT_DIR}/../apprise.yml"
STATE_FILE="${SCRIPT_DIR}/.status_last"

# Ensure health check exists
if [[ ! -f "$HEALTH_CHECK" ]]; then
  echo "ERROR: Health check script not found: $HEALTH_CHECK"
  exit 1
fi

# Check if apprise is installed
if ! command -v apprise &>/dev/null; then
  echo "ERROR: apprise is not installed"
  echo "Install: pip3 install apprise"
  exit 1
fi

# Check if config exists
if [[ ! -f "$APPRISE_CONFIG" ]]; then
  echo "ERROR: Apprise config not found: $APPRISE_CONFIG"
  echo "Copy apprise.yml.example to apprise.yml and configure your endpoints"
  exit 1
fi

# Parse arguments
FORCE=0
TEST=0
[[ "${1:-}" == "--force" ]] && FORCE=1
[[ "${1:-}" == "--test" ]] && TEST=1

# =====================================================
# TEST MODE: Send test notification
# =====================================================
if [[ $TEST -eq 1 ]]; then
  echo "Sending test notification..."
  apprise --config="$APPRISE_CONFIG" \
    --title="🧪 Test Alert - pCloud Backup" \
    --body="This is a test notification from your Raspberry Pi monitoring system. If you received this, Apprise is configured correctly! ✅" \
    --notification-type=info
  echo "Test notification sent!"
  exit 0
fi

# =====================================================
# NORMAL MODE: Check status and alert on change
# =====================================================

# Run health check in JSON mode
JSON_OUTPUT=$("$HEALTH_CHECK" --json 2>/dev/null || echo "{}")

# Extract status code (0=OK, 1=WARN, 2=CRIT, 3=UNKNOWN)
STATUS_CODE=$(echo "$JSON_OUTPUT" | grep -oP '"status_code":\s*\K[0-9]+' || echo "3")
STATUS_TEXT=$(echo "$JSON_OUTPUT" | grep -oP '"status_text":\s*"\K[^"]+' || echo "UNKNOWN")
HOSTNAME=$(echo "$JSON_OUTPUT" | grep -oP '"hostname":\s*"\K[^"]+' || hostname)

# Read last status (default to -1 if file doesn't exist)
LAST_STATUS=-1
if [[ -f "$STATE_FILE" ]]; then
  LAST_STATUS=$(cat "$STATE_FILE")
fi

# Check if status changed or forced
SEND_ALERT=0
if [[ $FORCE -eq 1 ]]; then
  SEND_ALERT=1
  ALERT_REASON="Forced alert"
elif [[ $STATUS_CODE -ne $LAST_STATUS ]]; then
  SEND_ALERT=1
  ALERT_REASON="Status changed: $(status_name "$LAST_STATUS") → $STATUS_TEXT"
fi

# Helper: Convert status code to name
status_name() {
  case "$1" in
    0) echo "OK" ;;
    1) echo "WARNING" ;;
    2) echo "CRITICAL" ;;
    3) echo "UNKNOWN" ;;
    -1) echo "FIRST_RUN" ;;
    *) echo "INVALID" ;;
  esac
}

# Send alert if needed
if [[ $SEND_ALERT -eq 1 ]]; then
  # Determine notification type and emoji
  case $STATUS_CODE in
    0) NOTIF_TYPE="success"; EMOJI="✅" ;;
    1) NOTIF_TYPE="warning"; EMOJI="⚠️" ;;
    2) NOTIF_TYPE="failure"; EMOJI="🚨" ;;
    3) NOTIF_TYPE="info"; EMOJI="❓" ;;
  esac
  
  # Extract issues from JSON
  ISSUES=$(echo "$JSON_OUTPUT" | grep -oP '"issues":\s*\[\K[^\]]+' | sed 's/"severity"://g; s/"message"://g; s/[{},]//g' | tr -d '"' | sed 's/^/  • /g' || echo "  No details available")
  
  # Build alert message
  TITLE="$EMOJI $STATUS_TEXT - pCloud Backup ($HOSTNAME)"
  BODY="Status: $STATUS_TEXT (Code: $STATUS_CODE)
Reason: $ALERT_REASON

Issues:
$ISSUES

Timestamp: $(date '+%Y-%m-%d %H:%M:%S')
Run: ./pcloud_health_check.sh --verbose for details"

  # Send via Apprise
  echo "Sending alert: $STATUS_TEXT"
  apprise --config="$APPRISE_CONFIG" \
    --title="$TITLE" \
    --body="$BODY" \
    --notification-type="$NOTIF_TYPE"
  
  echo "Alert sent successfully!"
else
  echo "Status unchanged ($STATUS_TEXT) - no alert sent"
fi

# Save current status for next run
echo "$STATUS_CODE" > "$STATE_FILE"

exit 0
