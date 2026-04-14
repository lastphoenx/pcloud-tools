#!/usr/bin/env bash
# =====================================================
# pCloud Backup Health Check Script
# =====================================================
# Purpose: Monitor backup health, detect gaps, alert on issues
# Usage:
#   ./pcloud_health_check.sh            # Run all checks, exit with status code
#   ./pcloud_health_check.sh --verbose  # Detailed output
#   ./pcloud_health_check.sh --nagios   # Nagios/Zabbix compatible output
# 
# Exit Codes:
#   0 = All healthy (no issues)
#   1 = Warning (degraded, action recommended)
#   2 = Critical (requires immediate action)
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

# Config from .env or defaults
RTB_SNAPSHOT_DIR="${RTB_SNAPSHOT_DIR:-/mnt/backup/rtb_nas}"
PCLOUD_TEMP_DIR="${PCLOUD_TEMP_DIR:-/srv/pcloud-temp}"
PCLOUD_DB_HOST="${PCLOUD_DB_HOST:-localhost}"
PCLOUD_DB_PORT="${PCLOUD_DB_PORT:-3306}"
PCLOUD_DB_NAME="${PCLOUD_DB_NAME:-pcloud_backup}"
PCLOUD_DB_USER="${PCLOUD_DB_USER:-pcloud_backup}"
PCLOUD_DB_PASS="${PCLOUD_DB_PASS:-}"
PCLOUD_ENABLE_DB="${PCLOUD_ENABLE_DB:-0}"

PCLOUD_TOKEN="${PCLOUD_TOKEN:-}"
PCLOUD_API_HOST="${PCLOUD_API_HOST:-eapi.pcloud.com}"

# Thresholds (configurable via .env)
BACKUP_AGE_WARNING_HOURS="${BACKUP_AGE_WARNING_HOURS:-48}"
BACKUP_AGE_CRITICAL_HOURS="${BACKUP_AGE_CRITICAL_HOURS:-72}"
QUOTA_WARNING_GB="${QUOTA_WARNING_GB:-500}"    # 5% of 10TB
QUOTA_CRITICAL_GB="${QUOTA_CRITICAL_GB:-200}"  # 2% of 10TB
DISK_WARNING_PERCENT="${DISK_WARNING_PERCENT:-10}"

# Mode
MODE="${1:-normal}"
VERBOSE=0
[[ "$MODE" == "--verbose" ]] && VERBOSE=1
[[ "$MODE" == "--nagios" ]] && NAGIOS=1 || NAGIOS=0

# Colors (only for non-nagios mode)
if [[ $NAGIOS -eq 0 ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  NC='\033[0m'
else
  RED=''
  GREEN=''
  YELLOW=''
  NC=''
fi

# Status tracking
GLOBAL_STATUS=0  # 0=OK, 1=WARNING, 2=CRITICAL
ISSUES=()

# Helper: Set status (keep highest severity)
set_status() {
  local new_status=$1
  if [[ $new_status -gt $GLOBAL_STATUS ]]; then
    GLOBAL_STATUS=$new_status
  fi
}

# Helper: Log issue
log_issue() {
  local severity="$1"
  local message="$2"
  ISSUES+=("$severity: $message")
  
  if [[ $VERBOSE -eq 1 ]]; then
    case "$severity" in
      CRITICAL) echo -e "${RED}✗ CRITICAL${NC}: $message" ;;
      WARNING)  echo -e "${YELLOW}⚠ WARNING${NC}: $message" ;;
      OK)       echo -e "${GREEN}✓ OK${NC}: $message" ;;
    esac
  fi
}

# MySQL helper (same as in wrapper)
_mysql() {
  MYSQL_PWD="$PCLOUD_DB_PASS" mysql -h "$PCLOUD_DB_HOST" \
        -P "$PCLOUD_DB_PORT" \
        -u "$PCLOUD_DB_USER" \
        -D "$PCLOUD_DB_NAME" \
        -sN \
        -e "$@" 2>/dev/null
}

# =====================================================
# CHECK 1: Backup Age & Gap Detection
# =====================================================
# Logic: RTB runs only on changes → No backup could be normal!
# Gap = New RTB snapshot exists BUT pCloud backup is old
check_backup_age() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[1] Backup Age & Gap Detection${NC}"
  
  # Get latest RTB snapshot timestamp
  if [[ ! -d "$RTB_SNAPSHOT_DIR" ]]; then
    log_issue "WARNING" "RTB snapshot directory not found: $RTB_SNAPSHOT_DIR"
    set_status 1
    return
  fi
  
  # Find newest snapshot folder (format: YYYY-MM-DD__HH-MM-SS)
  local latest_rtb_snapshot
  latest_rtb_snapshot=$(find "$RTB_SNAPSHOT_DIR" -maxdepth 1 -type d -name "20*" | sort -r | head -n 1 | xargs basename 2>/dev/null || echo "")
  
  if [[ -z "$latest_rtb_snapshot" ]]; then
    log_issue "WARNING" "No RTB snapshots found in $RTB_SNAPSHOT_DIR"
    set_status 1
    return
  fi
  
  # Parse RTB snapshot timestamp (format: 2026-04-14__22-00-01)
  # Split into date and time, only replace dashes in time portion
  local rtb_date="${latest_rtb_snapshot%%__*}"  # 2026-04-14
  local rtb_time="${latest_rtb_snapshot##*__}"  # 22-00-01
  rtb_time="${rtb_time//-/:}"                    # 22:00:01
  local rtb_timestamp="$rtb_date $rtb_time"      # 2026-04-14 22:00:01
  
  local rtb_epoch
  rtb_epoch=$(date -d "$rtb_timestamp" +%s 2>/dev/null || echo "0")
  local rtb_age_hours=$(( ($(date +%s) - rtb_epoch) / 3600 ))
  local rtb_age_days=$(( rtb_age_hours / 24 ))
  
  # Format age display (show days if >= 24h)
  local rtb_age_display
  if [[ $rtb_age_hours -ge 24 ]]; then
    rtb_age_display="${rtb_age_days}d $((rtb_age_hours % 24))h ago"
  else
    rtb_age_display="${rtb_age_hours}h ago"
  fi
  
  [[ $VERBOSE -eq 1 ]] && echo "  Latest RTB snapshot: $latest_rtb_snapshot ($rtb_age_display)"
  
  # Get latest successful pCloud backup from DB (if enabled)
  local pcloud_backup_age_hours=999999
  local pcloud_snapshot=""
  
  if [[ "$PCLOUD_ENABLE_DB" == "1" ]]; then
    local last_success
    last_success=$(_mysql "SELECT snapshot_name, TIMESTAMPDIFF(HOUR, finished_at, NOW()) AS age_hours FROM backup_runs WHERE status='SUCCESS' ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo "")
    
    if [[ -n "$last_success" ]]; then
      pcloud_snapshot=$(echo "$last_success" | cut -f1)
      pcloud_backup_age_hours=$(echo "$last_success" | cut -f2)
      
      # Format age display
      local pcloud_age_display
      local pcloud_age_days=$(( pcloud_backup_age_hours / 24 ))
      if [[ $pcloud_backup_age_hours -ge 24 ]]; then
        pcloud_age_display="${pcloud_age_days}d $((pcloud_backup_age_hours % 24))h ago"
      else
        pcloud_age_display="${pcloud_backup_age_hours}h ago"
      fi
      
      [[ $VERBOSE -eq 1 ]] && echo "  Latest pCloud backup: $pcloud_snapshot ($pcloud_age_display)"
    else
      [[ $VERBOSE -eq 1 ]] && echo "  No successful pCloud backups in database"
    fi
  else
    [[ $VERBOSE -eq 1 ]] && echo "  Database tracking disabled - cannot check pCloud backup age"
  fi
  
  # Gap Detection Logic
  if [[ "$PCLOUD_ENABLE_DB" == "1" && -n "$pcloud_snapshot" ]]; then
    # Compare RTB vs pCloud timestamps
    local gap_hours=$((pcloud_backup_age_hours - rtb_age_hours))
    
    if [[ $gap_hours -gt 24 ]]; then
      # pCloud backup is significantly older than RTB → GAP!
      log_issue "CRITICAL" "Backup gap detected! RTB has new snapshot ($latest_rtb_snapshot, $rtb_age_display) but pCloud backup is old ($pcloud_snapshot, $pcloud_age_display)"
      set_status 2
    elif [[ $pcloud_backup_age_hours -gt $BACKUP_AGE_CRITICAL_HOURS ]]; then
      log_issue "CRITICAL" "Last pCloud backup too old: $pcloud_age_display (threshold: ${BACKUP_AGE_CRITICAL_HOURS}h)"
      set_status 2
    elif [[ $pcloud_backup_age_hours -gt $BACKUP_AGE_WARNING_HOURS ]]; then
      log_issue "WARNING" "Last pCloud backup aging: $pcloud_age_display (threshold: ${BACKUP_AGE_WARNING_HOURS}h)"
      set_status 1
    else
      log_issue "OK" "Backup age healthy ($pcloud_age_display)"
    fi
  else
    # Fallback: Just check RTB age if DB disabled
    if [[ $rtb_age_hours -gt $BACKUP_AGE_CRITICAL_HOURS ]]; then
      log_issue "CRITICAL" "Last RTB snapshot too old: $rtb_age_display (threshold: ${BACKUP_AGE_CRITICAL_HOURS}h)"
      set_status 2
    elif [[ $rtb_age_hours -gt $BACKUP_AGE_WARNING_HOURS ]]; then
      log_issue "WARNING" "Last RTB snapshot aging: $rtb_age_display (threshold: ${BACKUP_AGE_WARNING_HOURS}h)"
      set_status 1
    else
      log_issue "OK" "RTB snapshot age healthy ($rtb_age_display)"
    fi
  fi
}

# =====================================================
# CHECK 2: pCloud Quota
# =====================================================
check_pcloud_quota() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[2] pCloud Quota${NC}"
  
  if [[ -z "$PCLOUD_TOKEN" ]]; then
    log_issue "WARNING" "pCloud token not configured - cannot check quota"
    set_status 1
    return
  fi
  
  # Query pCloud API for quota info
  local quota_response
  quota_response=$(curl -s "https://${PCLOUD_API_HOST}/userinfo?getauth=1&access_token=${PCLOUD_TOKEN}" 2>/dev/null || echo "")
  
  if [[ -z "$quota_response" ]]; then
    log_issue "WARNING" "Failed to query pCloud API for quota"
    set_status 1
    return
  fi
  
  # Parse JSON (requires jq, fallback to grep if not available)
  # Fields are nested in .userinfo when getauth=1 is used
  local quota_total quota_used quota_free
  if command -v jq &>/dev/null; then
    quota_total=$(echo "$quota_response" | jq -r '.userinfo.quota // .quota // 0')
    quota_used=$(echo "$quota_response" | jq -r '.userinfo.usedquota // .usedquota // 0')
  else
    # Fallback: grep parsing (fragile but works)
    # Try nested first, then root
    quota_total=$(echo "$quota_response" | grep -oP '"userinfo":\{.*?"quota":\s*\K[0-9]+' || echo "$quota_response" | grep -oP '"quota":\s*\K[0-9]+' || echo "0")
    quota_used=$(echo "$quota_response" | grep -oP '"userinfo":\{.*?"usedquota":\s*\K[0-9]+' || echo "$quota_response" | grep -oP '"usedquota":\s*\K[0-9]+' || echo "0")
  fi
  
  quota_free=$((quota_total - quota_used))
  
  # Convert to GB
  local quota_free_gb=$((quota_free / 1073741824))
  local quota_total_gb=$((quota_total / 1073741824))
  local quota_used_gb=$((quota_used / 1073741824))
  
  [[ $VERBOSE -eq 1 ]] && echo "  Total: ${quota_total_gb} GB | Used: ${quota_used_gb} GB | Free: ${quota_free_gb} GB"
  
  # Check thresholds
  if [[ $quota_free_gb -lt $QUOTA_CRITICAL_GB ]]; then
    log_issue "CRITICAL" "pCloud quota critically low: ${quota_free_gb} GB free (threshold: ${QUOTA_CRITICAL_GB} GB)"
    set_status 2
  elif [[ $quota_free_gb -lt $QUOTA_WARNING_GB ]]; then
    log_issue "WARNING" "pCloud quota running low: ${quota_free_gb} GB free (threshold: ${QUOTA_WARNING_GB} GB)"
    set_status 1
  else
    log_issue "OK" "pCloud quota healthy: ${quota_free_gb} GB free"
  fi
}

# =====================================================
# CHECK 3: Disk Space (/srv - mergerfs)
# =====================================================
check_disk_space() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[3] Disk Space (temp storage)${NC}"
  
  # Check parent directory /srv (where mergerfs is typically mounted)
  # PCLOUD_TEMP_DIR might be /srv/pcloud-temp which is a subdirectory of /srv/nas
  local check_path="/srv"
  [[ -d "$PCLOUD_TEMP_DIR" ]] && check_path="$PCLOUD_TEMP_DIR"
  
  # Get disk usage via df
  local disk_info
  disk_info=$(df -h "$check_path" | tail -n 1)
  
  # Extract mount point to show user which filesystem we're checking
  local mount_point
  mount_point=$(echo "$disk_info" | awk '{print $6}')
  
  local disk_used_percent
  disk_used_percent=$(echo "$disk_info" | awk '{print $5}' | tr -d '%')
  
  local disk_avail
  disk_avail=$(echo "$disk_info" | awk '{print $4}')
  
  [[ $VERBOSE -eq 1 ]] && echo "  Mount: $mount_point | Usage: ${disk_used_percent}% | Available: ${disk_avail}"
  
  # Check thresholds (inverted: high usage = problem)
  local disk_free_percent=$((100 - disk_used_percent))
  
  if [[ $disk_free_percent -lt 5 ]]; then
    log_issue "CRITICAL" "Disk space critically low: ${disk_free_percent}% free (${disk_avail} available)"
    set_status 2
  elif [[ $disk_free_percent -lt $DISK_WARNING_PERCENT ]]; then
    log_issue "WARNING" "Disk space running low: ${disk_free_percent}% free (${disk_avail} available)"
    set_status 1
  else
    log_issue "OK" "Disk space healthy: ${disk_free_percent}% free (${disk_avail} available)"
  fi
}

# =====================================================
# CHECK 4: Database Connectivity (if enabled)
# =====================================================
check_database() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[4] Database Connectivity${NC}"
  
  if [[ "$PCLOUD_ENABLE_DB" != "1" ]]; then
    [[ $VERBOSE -eq 1 ]] && echo "  Database tracking disabled - skipping check"
    return
  fi
  
  # Test connection
  if ! _mysql "SELECT 1" >/dev/null 2>&1; then
    log_issue "WARNING" "Cannot connect to MariaDB (host=$PCLOUD_DB_HOST, db=$PCLOUD_DB_NAME)"
    set_status 1
  else
    log_issue "OK" "Database connection healthy"
  fi
}

# =====================================================
# Run All Checks
# =====================================================
[[ $VERBOSE -eq 1 ]] && echo -e "${GREEN}=== pCloud Backup Health Check ===${NC}"

check_backup_age
check_pcloud_quota
check_disk_space
check_database

# =====================================================
# Output Summary
# =====================================================
if [[ $NAGIOS -eq 1 ]]; then
  # Nagios format: STATUS_TEXT | performance_data
  case $GLOBAL_STATUS in
    0) echo "OK - All backup health checks passed" ;;
    1) echo "WARNING - ${#ISSUES[@]} issue(s) detected" ;;
    2) echo "CRITICAL - ${#ISSUES[@]} issue(s) require attention" ;;
  esac
  
  # Output issues
  for issue in "${ISSUES[@]}"; do
    echo "$issue"
  done
else
  # Standard format
  echo ""
  echo "========================================"
  case $GLOBAL_STATUS in
    0) echo -e "${GREEN}✓ Status: HEALTHY${NC}" ;;
    1) echo -e "${YELLOW}⚠ Status: WARNING${NC}" ;;
    2) echo -e "${RED}✗ Status: CRITICAL${NC}" ;;
  esac
  echo "========================================"
  
  if [[ ${#ISSUES[@]} -gt 0 && $VERBOSE -eq 0 ]]; then
    echo ""
    echo "Issues detected:"
    for issue in "${ISSUES[@]}"; do
      severity=$(echo "$issue" | cut -d: -f1)
      message=$(echo "$issue" | cut -d: -f2-)
      case "$severity" in
        CRITICAL) echo -e "${RED}  ✗${NC}$message" ;;
        WARNING)  echo -e "${YELLOW}  ⚠${NC}$message" ;;
        OK)       ;;  # Skip OK messages in summary
      esac
    done
  fi
  
  echo ""
  echo "Run with --verbose for detailed output"
fi

exit $GLOBAL_STATUS
