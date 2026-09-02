#!/usr/bin/env bash
# =====================================================
# pCloud Backup Health Check Script
# =====================================================
# Purpose: Monitor backup health, detect gaps, alert on issues
# Usage:
#   ./pcloud_health_check.sh            # Run all checks, exit with status code
#   ./pcloud_health_check.sh --verbose  # Detailed output
#   ./pcloud_health_check.sh --nagios   # Nagios/Zabbix compatible output
#   ./pcloud_health_check.sh --json     # JSON output for aggregation
# 
# Exit Codes:
#   0 = All healthy (no issues)
#   1 = Warning (degraded, action recommended)
#   2 = Critical (requires immediate action)
#   3 = Unknown (check failed to run)
# =====================================================

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

# Load .env if exists (filter inline comments for safety)
if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key val; do
    # Skip comments and empty lines
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    # Remove inline comments (only if # is preceded by whitespace)
    val=$(echo "$val" | sed 's/[[:space:]]#.*//')
    # Trim whitespace
    val=$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    # Remove quotes
    if [[ "$val" =~ ^\"(.*)\"$ ]]; then
      val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
      val="${BASH_REMATCH[1]}"
    fi
    # Export variable
    export "${key}=${val}"
  done < "$ENV_FILE"
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
NAGIOS=0
JSON_MODE=0

[[ "$MODE" == "--verbose" ]] && VERBOSE=1
[[ "$MODE" == "--nagios" ]] && NAGIOS=1
[[ "$MODE" == "--json" ]] && JSON_MODE=1

# Colors (only for interactive modes)
if [[ $NAGIOS -eq 0 && $JSON_MODE -eq 0 ]]; then
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
GLOBAL_STATUS=0  # 0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN
ISSUES=()

# JSON data accumulator (for --json mode)
declare -A CHECK_RESULTS
CHECK_RESULTS[backup_age_status]=0
CHECK_RESULTS[backup_age_message]=""
CHECK_RESULTS[quota_status]=0
CHECK_RESULTS[quota_message]=""
CHECK_RESULTS[disk_status]=0
CHECK_RESULTS[disk_message]=""
CHECK_RESULTS[database_status]=0
CHECK_RESULTS[database_message]=""
CHECK_RESULTS[sync_backlog_json]="{\"catchup_count\":0,\"incomplete_count\":0,\"running_count\":0,\"catchup\":[],\"incomplete_remote\":[],\"running\":[]}"

# Helper: Set status (keep highest severity)
set_status() {
  local new_status=$1
  if [[ $new_status -gt $GLOBAL_STATUS ]]; then
    GLOBAL_STATUS=$new_status
  fi
}

# Helper: Store check result for JSON output
store_check_result() {
  local check_name="$1"
  local status="$2"
  local message="$3"
  local details="${4:-}"
  
  CHECK_RESULTS["${check_name}_status"]=$status
  CHECK_RESULTS["${check_name}_message"]="$message"
  [[ -n "$details" ]] && CHECK_RESULTS["${check_name}_details"]="$details"
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
  
  # Parse RTB snapshot timestamp (format: 2026-04-14__22-00-01 or 2026-04-12-163517)
  local rtb_timestamp
  if [[ "$latest_rtb_snapshot" == *__* ]]; then
    # Format with double underscore
    local rtb_date="${latest_rtb_snapshot%%__*}"
    local rtb_time="${latest_rtb_snapshot##*__}"
    rtb_time="${rtb_time//-/:}"
    rtb_timestamp="$rtb_date $rtb_time"
  else
    # Format with single dashes (e.g. 2026-04-12-163517)
    # Extract YYYY-MM-DD (first 10 chars) and HHMMSS
    local rtb_date="${latest_rtb_snapshot:0:10}"
    local rtb_time_raw="${latest_rtb_snapshot:11}"
    rtb_time_raw="${rtb_time_raw//-/}" # remove any remaining dashes
    # Insert colons for HH:MM:SS
    local rtb_time="${rtb_time_raw:0:2}:${rtb_time_raw:2:2}:${rtb_time_raw:4:2}"
    rtb_timestamp="$rtb_date $rtb_time"
  fi
  
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
  
  # Count RTB snapshots and pCloud manifests (validation)
  local snapshot_count=0
  local manifest_count=0
  
  snapshot_count=$(find "$RTB_SNAPSHOT_DIR" -maxdepth 1 -type d -name "20*" 2>/dev/null | wc -l || echo "0")
  
  local manifest_dir="${PCLOUD_ARCHIVE_DIR:-/srv/pcloud-archive}/manifests"
  if [[ -d "$manifest_dir" ]]; then
    manifest_count=$(find "$manifest_dir" -maxdepth 1 -type f -name "*.json" 2>/dev/null | wc -l || echo "0")
  fi
  
  [[ $VERBOSE -eq 1 ]] && echo "  RTB snapshots: $snapshot_count | pCloud manifests: $manifest_count"
  
  # Validate: manifest count should match snapshot count (or be close)
  if [[ $manifest_count -lt $snapshot_count ]]; then
    local missing_manifests=$((snapshot_count - manifest_count))
    [[ $VERBOSE -eq 1 ]] && echo -e "${YELLOW}  ⚠ Warning: $missing_manifests RTB snapshots lack corresponding pCloud manifests${NC}"
  elif [[ $manifest_count -gt $snapshot_count ]]; then
    local orphan_manifests=$((manifest_count - snapshot_count))
    [[ $VERBOSE -eq 1 ]] && echo -e "${YELLOW}  ⚠ Warning: $orphan_manifests pCloud manifests without corresponding RTB snapshots${NC}"
  else
    [[ $VERBOSE -eq 1 ]] && echo -e "${GREEN}  ✓ Manifest/snapshot count matches${NC}"
  fi
  
  # pCloud sync status from DB (if enabled)
  # IMPORTANT: Compare snapshot NAMES, not finished_at — catch-up uploads for older
  # snapshots finish later than newer ones and must not look like a GAP.
  local pcloud_backup_age_hours=999999
  local pcloud_snapshot=""
  local latest_rtb_uploaded=0
  local snap_escaped="${latest_rtb_snapshot//\'/\'\'}"
  
  if [[ "$PCLOUD_ENABLE_DB" == "1" ]]; then
    local rtb_db_age
    rtb_db_age=$(_mysql "SELECT TIMESTAMPDIFF(HOUR, finished_at, NOW()) FROM backup_runs WHERE snapshot_name='${snap_escaped}' AND status='SUCCESS' ORDER BY finished_at DESC LIMIT 1" 2>/dev/null || echo "")
    
    if [[ -n "$rtb_db_age" ]]; then
      latest_rtb_uploaded=1
      pcloud_snapshot="$latest_rtb_snapshot"
      pcloud_backup_age_hours="$rtb_db_age"
    fi
    
    # Newest SUCCESS by snapshot name (for gap display / fallback only)
    local newest_success
    newest_success=$(_mysql "SELECT snapshot_name, TIMESTAMPDIFF(HOUR, finished_at, NOW()) AS age_hours FROM backup_runs WHERE status='SUCCESS' ORDER BY snapshot_name DESC LIMIT 1" 2>/dev/null || echo "")
    
    if [[ $latest_rtb_uploaded -eq 0 && -n "$newest_success" ]]; then
      pcloud_snapshot=$(echo "$newest_success" | cut -f1)
      pcloud_backup_age_hours=$(echo "$newest_success" | cut -f2)
    fi
    
    if [[ -n "$pcloud_snapshot" ]]; then
      local pcloud_age_display
      local pcloud_age_days=$(( pcloud_backup_age_hours / 24 ))
      if [[ $pcloud_backup_age_hours -ge 24 ]]; then
        pcloud_age_display="${pcloud_age_days}d $((pcloud_backup_age_hours % 24))h ago"
      else
        pcloud_age_display="${pcloud_backup_age_hours}h ago"
      fi
      
      if [[ $latest_rtb_uploaded -eq 1 ]]; then
        [[ $VERBOSE -eq 1 ]] && echo "  Latest RTB snapshot on pCloud: $pcloud_snapshot ($pcloud_age_display)"
      else
        [[ $VERBOSE -eq 1 ]] && echo "  Newest pCloud backup (by snapshot): $pcloud_snapshot ($pcloud_age_display); latest RTB $latest_rtb_snapshot not uploaded yet"
      fi
    else
      [[ $VERBOSE -eq 1 ]] && echo "  No successful pCloud backups in database"
    fi
  else
    [[ $VERBOSE -eq 1 ]] && echo "  Database tracking disabled - cannot check pCloud backup age"
  fi
  
  # Gap Detection Logic (RTB is change-only backup!)
  # IMPORTANT: RTB only creates snapshots on changes, so age-based checks are WRONG
  # Correct approach: latest RTB snapshot must have SUCCESS in backup_runs
  if [[ "$PCLOUD_ENABLE_DB" == "1" ]]; then
    local running_for_latest=0
    running_for_latest=$(_mysql "SELECT COUNT(*) FROM backup_runs WHERE snapshot_name='${snap_escaped}' AND status='RUNNING'" 2>/dev/null || echo "0")
    if [[ $latest_rtb_uploaded -eq 1 ]]; then
      log_issue "OK" "pCloud in sync with RTB (both: $latest_rtb_snapshot)"
    elif [[ "${running_for_latest:-0}" -gt 0 ]]; then
      log_issue "WARNING" "Upload in progress for latest RTB snapshot ($latest_rtb_snapshot) — GAP expected until .upload_complete"
      set_status 1
    elif [[ -n "$pcloud_snapshot" ]]; then
      local pcloud_age_display
      local pcloud_age_days=$(( pcloud_backup_age_hours / 24 ))
      if [[ $pcloud_backup_age_hours -ge 24 ]]; then
        pcloud_age_display="${pcloud_age_days}d $((pcloud_backup_age_hours % 24))h ago"
      else
        pcloud_age_display="${pcloud_backup_age_hours}h ago"
      fi
      log_issue "CRITICAL" "Backup GAP detected! RTB has newer snapshot ($latest_rtb_snapshot, $rtb_age_display) but pCloud backup is older ($pcloud_snapshot, $pcloud_age_display)"
      set_status 2
    else
      log_issue "WARNING" "pCloud backup tracking not available (no successful backups in DB)"
      set_status 1
    fi
  else
    log_issue "WARNING" "pCloud backup tracking not available (enable PCLOUD_ENABLE_DB=1 for proper monitoring)"
    set_status 1
  fi
  
  # Store results for JSON output
  CHECK_RESULTS[rtb_snapshot]="$latest_rtb_snapshot"
  CHECK_RESULTS[rtb_age_hours]=$rtb_age_hours
  CHECK_RESULTS[snapshot_count]=$snapshot_count
  CHECK_RESULTS[manifest_count]=$manifest_count
  CHECK_RESULTS[pcloud_snapshot]="${pcloud_snapshot:-none}"
  CHECK_RESULTS[pcloud_age_hours]=${pcloud_backup_age_hours}
  CHECK_RESULTS[backup_age_status]=$GLOBAL_STATUS
  # Get the LAST (most recent) issue message for this check
  CHECK_RESULTS[backup_age_message]="$(echo "${ISSUES[@]}" | grep -E 'OK:|WARNING:|CRITICAL:' | tail -n1 || echo 'No status available')"
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
  
  # Convert to GB and TB (with decimal)
  local quota_free_gb=$((quota_free / 1073741824))
  local quota_total_gb=$((quota_total / 1073741824))
  local quota_used_gb=$((quota_used / 1073741824))
  
  # Calculate TB with one decimal place (using awk for floating point)
  local quota_total_tb=$(awk "BEGIN {printf \"%.1f\", $quota_total_gb / 1024}")
  local quota_used_tb=$(awk "BEGIN {printf \"%.1f\", $quota_used_gb / 1024}")
  local quota_free_tb=$(awk "BEGIN {printf \"%.1f\", $quota_free_gb / 1024}")
  
  [[ $VERBOSE -eq 1 ]] && echo "  Total: ${quota_total_tb} TB (${quota_total_gb} GB) | Used: ${quota_used_tb} TB (${quota_used_gb} GB) | Free: ${quota_free_tb} TB (${quota_free_gb} GB)"
  
  # Check thresholds
  if [[ $quota_free_gb -lt $QUOTA_CRITICAL_GB ]]; then
    log_issue "CRITICAL" "pCloud quota critically low: ${quota_free_tb} TB (${quota_free_gb} GB) free (threshold: ${QUOTA_CRITICAL_GB} GB)"
    set_status 2
  elif [[ $quota_free_gb -lt $QUOTA_WARNING_GB ]]; then
    log_issue "WARNING" "pCloud quota running low: ${quota_free_tb} TB (${quota_free_gb} GB) free (threshold: ${QUOTA_WARNING_GB} GB)"
    set_status 1
  else
    log_issue "OK" "pCloud quota healthy: ${quota_free_tb} TB (${quota_free_gb} GB) free"
  fi
  
  # Store results for JSON output
  CHECK_RESULTS[quota_total_gb]=${quota_total_gb:-0}
  CHECK_RESULTS[quota_used_gb]=${quota_used_gb:-0}
  CHECK_RESULTS[quota_free_gb]=${quota_free_gb:-0}
  local quota_status_before_this_check=$GLOBAL_STATUS
  CHECK_RESULTS[quota_status]=$quota_status_before_this_check
  CHECK_RESULTS[quota_message]="Healthy"
}

# =====================================================
# CHECK 3: Disk Space (RTB Source + Staging Pool)
# =====================================================
check_disk_space() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[3] Disk Space${NC}"
  
  local overall_status=0
  
  # --- RTB Source ---
  if [[ -d "/mnt/backup" ]]; then
    local rtb_info
    rtb_info=$(df -h /mnt/backup | tail -n 1)
    local rtb_used_pct=$(echo "$rtb_info" | awk '{print $5}' | tr -d '%')
    local rtb_avail=$(echo "$rtb_info" | awk '{print $4}')
    
    [[ $VERBOSE -eq 1 ]] && echo "  RTB Source (/mnt/backup): ${rtb_used_pct}% used, ${rtb_avail} available"
    
    if [[ $((100 - rtb_used_pct)) -lt 5 ]]; then
      log_issue "WARNING" "RTB source space low: ${rtb_used_pct}% used"
      overall_status=1
    fi
  fi
  
  # --- Staging Pool (mergerfs) ---
  if [[ -d "/srv/nas" ]]; then
    local staging_info
    staging_info=$(df -h /srv/nas | tail -n 1)
    local staging_used_pct=$(echo "$staging_info" | awk '{print $5}' | tr -d '%')
    local staging_avail=$(echo "$staging_info" | awk '{print $4}')
    
    [[ $VERBOSE -eq 1 ]] && echo "  Staging Pool (/srv/nas mergerfs 1:2): ${staging_used_pct}% used, ${staging_avail} available"
    
    # Check threshold for staging area
    local staging_free_pct=$((100 - staging_used_pct))
    if [[ $staging_free_pct -lt 5 ]]; then
      log_issue "CRITICAL" "Staging pool critically low: ${staging_free_pct}% free (${staging_avail} available)"
      set_status 2
      overall_status=2
    elif [[ $staging_free_pct -lt $DISK_WARNING_PERCENT ]]; then
      log_issue "WARNING" "Staging pool running low: ${staging_free_pct}% free (${staging_avail} available)"
      [[ $overall_status -lt 1 ]] && set_status 1 && overall_status=1
    fi
    
    # --- Individual SSDs with folder breakdown ---
    if [[ $VERBOSE -eq 1 ]]; then
      # SSD1 - show actual physical folders on this SSD
      if [[ -d "/mnt/ssd1" ]]; then
        local ssd1_info
        ssd1_info=$(df -h /mnt/ssd1 | tail -n 1)
        local ssd1_used_pct=$(echo "$ssd1_info" | awk '{print $5}' | tr -d '%')
        local ssd1_avail=$(echo "$ssd1_info" | awk '{print $4}')
        
        local ssd1_folders
        ssd1_folders=$(ls -1 /mnt/ssd1 2>/dev/null | head -n 5 | tr '\n' ', ' | sed 's/, $//')
        [[ -z "$ssd1_folders" ]] && ssd1_folders="(empty)"
        
        echo "    ├─ SSD1 (/mnt/ssd1): ${ssd1_used_pct}% used, ${ssd1_avail} available"
        echo "    │  Physical folders: $ssd1_folders"
      fi
      
      # SSD2 - show actual physical folders on this SSD
      if [[ -d "/mnt/ssd2" ]]; then
        local ssd2_info
        ssd2_info=$(df -h /mnt/ssd2 | tail -n 1)
        local ssd2_used_pct=$(echo "$ssd2_info" | awk '{print $5}' | tr -d '%')
        local ssd2_avail=$(echo "$ssd2_info" | awk '{print $4}')
        
        local ssd2_folders
        ssd2_folders=$(ls -1 /mnt/ssd2 2>/dev/null | head -n 5 | tr '\n' ', ' | sed 's/, $//')
        [[ -z "$ssd2_folders" ]] && ssd2_folders="(empty)"
        
        echo "    └─ SSD2 (/mnt/ssd2): ${ssd2_used_pct}% used, ${ssd2_avail} available"
        echo "       Physical folders: $ssd2_folders"
      fi
    fi
  fi
  
  # Final status if everything healthy
  if [[ $overall_status -eq 0 ]]; then
    log_issue "OK" "Disk space healthy on all volumes"
  fi
  
  # Store results for JSON output
  CHECK_RESULTS[rtb_used_pct]=${rtb_used_pct:-0}
  CHECK_RESULTS[rtb_avail]="${rtb_avail:-unknown}"
  CHECK_RESULTS[staging_used_pct]=${staging_used_pct:-0}
  CHECK_RESULTS[staging_avail]="${staging_avail:-unknown}"
  CHECK_RESULTS[disk_status]=$overall_status
  CHECK_RESULTS[disk_message]="$([[ $overall_status -eq 0 ]] && echo 'Healthy' || echo 'Check issues')"
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
  
  # Store results for JSON output
  local db_status_code=0
  [[ "$PCLOUD_ENABLE_DB" != "1" ]] && db_status_code=3
  CHECK_RESULTS[database_status]=$db_status_code
  CHECK_RESULTS[database_message]="$([[ "$PCLOUD_ENABLE_DB" == "1" ]] && echo 'Connected' || echo 'Disabled')"
}

# =====================================================
# CHECK 5: Catch-up / Incomplete Remotes / RUNNING-Zombies
# =====================================================
check_sync_backlog() {
  [[ $VERBOSE -eq 1 ]] && echo -e "\n${GREEN}[5] Catch-up / Incomplete / DB-RUNNING${NC}"

  local audit_py="${SCRIPT_DIR}/scripts/utilities/pool_audit_status.py"
  local pool_root="${PCLOUD_DEST:-/Backup/rtb_pool}"
  local rtb_root="${RTB:-${RTB_SNAPSHOT_DIR:-/mnt/backup/rtb_nas}}"
  local default_json='{"catchup_count":0,"incomplete_count":0,"running_count":0,"catchup":[],"incomplete_remote":[],"running":[]}'
  CHECK_RESULTS[sync_backlog_json]="$default_json"

  if [[ ! -f "$audit_py" ]]; then
    log_issue "WARNING" "pool_audit_status.py fehlt — Catch-up-Check übersprungen"
    set_status 1
    return
  fi

  local py_bin=""
  if [[ -x /opt/apps/pcloud-tools/venv/bin/python3 ]]; then
    py_bin=/opt/apps/pcloud-tools/venv/bin/python3
  elif [[ -x "${SCRIPT_DIR}/../venv/bin/python3" ]]; then
    py_bin="${SCRIPT_DIR}/../venv/bin/python3"
  else
    py_bin="$(command -v python3 || true)"
  fi
  if [[ -z "$py_bin" ]]; then
    log_issue "WARNING" "python3 fehlt — Catch-up-Check übersprungen"
    set_status 1
    return
  fi

  local audit_out rc=0
  set +e
  audit_out="$(
    MAIN_DIR="${SCRIPT_DIR}" "$py_bin" "$audit_py" \
      --json --env-file "$ENV_FILE" \
      --pool-root "$pool_root" \
      --rtb-root "$rtb_root" \
      2>/dev/null
  )"
  rc=$?
  set -e

  if [[ -z "$audit_out" || "$audit_out" != \{* ]]; then
    log_issue "WARNING" "Catch-up-Check fehlgeschlagen (API/Skript)"
    set_status 1
    return
  fi

  CHECK_RESULTS[sync_backlog_json]="$audit_out"

  local catchup_n incomplete_n running_n
  if command -v jq &>/dev/null; then
    catchup_n=$(echo "$audit_out" | jq -r '.catchup_count // 0')
    incomplete_n=$(echo "$audit_out" | jq -r '.incomplete_count // 0')
    running_n=$(echo "$audit_out" | jq -r '.running_count // 0')
  else
    catchup_n=0
    incomplete_n=0
    running_n=0
  fi

  local pipeline_state
  pipeline_state="$(systemctl is-active backup-pipeline.service 2>/dev/null || echo inactive)"
  local zombie_n=0
  if [[ "$pipeline_state" != "active" && "$pipeline_state" != "activating" ]]; then
    zombie_n=$running_n
  fi

  if command -v jq &>/dev/null; then
    CHECK_RESULTS[sync_backlog_json]=$(echo "$audit_out" | jq -c \
      --argjson z "${zombie_n:-0}" \
      --arg ps "$pipeline_state" \
      '. + {running_zombies: $z, pipeline: $ps}' 2>/dev/null || echo "$audit_out")
  fi

  [[ $VERBOSE -eq 1 ]] && echo "  Catch-up: ${catchup_n} | Incomplete remotes: ${incomplete_n} | RUNNING (DB): ${running_n} (Zombies: ${zombie_n})"

  if [[ "${incomplete_n:-0}" -gt 0 ]]; then
    local names
    names=$(echo "$audit_out" | jq -r '.incomplete_remote | join(", ")' 2>/dev/null || echo "")
    log_issue "CRITICAL" "Incomplete Remote-Ordner ohne .upload_complete (${incomplete_n}): ${names}"
    set_status 2
  elif [[ "${catchup_n:-0}" -gt 0 ]]; then
    local names
    names=$(echo "$audit_out" | jq -r '.catchup | join(", ")' 2>/dev/null || echo "")
    log_issue "WARNING" "Catch-up: ${catchup_n} lokale RTB-Snapshot(s) ohne .upload_complete: ${names}"
    set_status 1
  else
    log_issue "OK" "Catch-up leer — alle lokalen RTB-Snapshots complete auf pCloud"
  fi

  if [[ "${zombie_n:-0}" -gt 0 ]]; then
    local names
    names=$(echo "$audit_out" | jq -r '[.running[].snapshot_name] | join(", ")' 2>/dev/null || echo "")
    log_issue "WARNING" "backup_runs RUNNING=${zombie_n} obwohl Pipeline inactive: ${names}"
    set_status 1
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
check_sync_backlog

# =====================================================
# Output Summary
# =====================================================
if [[ $JSON_MODE -eq 1 ]]; then
  escape_json() {
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
      echo "$str"
    fi
  }
  
  # Build JSON manually (avoid jq dependency)
  echo "{"
  echo "  \"hostname\": \"$(hostname)\","
  echo "  \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"status_code\": $GLOBAL_STATUS,"
  echo "  \"status_text\": \"$(case $GLOBAL_STATUS in 0) echo "OK";; 1) echo "WARNING";; 2) echo "CRITICAL";; 3) echo "UNKNOWN";; esac)\","
  echo "  \"checks\": {"
  
  # Backup Age Check
  echo "    \"backup_age\": {"
  echo "      \"status\": ${CHECK_RESULTS[backup_age_status]:-3},"
  echo "      \"message\": \"$(escape_json "${CHECK_RESULTS[backup_age_message]:-Check not run}")\","
  echo "      \"snapshot_count\": ${CHECK_RESULTS[snapshot_count]:-0},"
  echo "      \"manifest_count\": ${CHECK_RESULTS[manifest_count]:-0},"
  echo "      \"rtb_snapshot\": \"$(escape_json "${CHECK_RESULTS[rtb_snapshot]:-unknown}")\","
  echo "      \"rtb_age_hours\": ${CHECK_RESULTS[rtb_age_hours]:-0},"
  echo "      \"pcloud_snapshot\": \"$(escape_json "${CHECK_RESULTS[pcloud_snapshot]:-unknown}")\","
  echo "      \"pcloud_age_hours\": ${CHECK_RESULTS[pcloud_age_hours]:-0}"
  echo "    },"
  
  # pCloud Quota Check
  echo "    \"pcloud_quota\": {"
  echo "      \"status\": ${CHECK_RESULTS[quota_status]:-3},"
  echo "      \"message\": \"$(escape_json "${CHECK_RESULTS[quota_message]:-Check not run}")\","
  echo "      \"total_gb\": ${CHECK_RESULTS[quota_total_gb]:-0},"
  echo "      \"used_gb\": ${CHECK_RESULTS[quota_used_gb]:-0},"
  echo "      \"free_gb\": ${CHECK_RESULTS[quota_free_gb]:-0}"
  echo "    },"
  
  # Disk Space Check
  echo "    \"disk_space\": {"
  echo "      \"status\": ${CHECK_RESULTS[disk_status]:-3},"
  echo "      \"message\": \"$(escape_json "${CHECK_RESULTS[disk_message]:-Check not run}")\","
  echo "      \"rtb_used_pct\": ${CHECK_RESULTS[rtb_used_pct]:-0},"
  echo "      \"rtb_avail\": \"$(escape_json "${CHECK_RESULTS[rtb_avail]:-unknown}")\","
  echo "      \"staging_used_pct\": ${CHECK_RESULTS[staging_used_pct]:-0},"
  echo "      \"staging_avail\": \"$(escape_json "${CHECK_RESULTS[staging_avail]:-unknown}")\""
  echo "    },"
  
  # Database Check
  echo "    \"database\": {"
  echo "      \"status\": ${CHECK_RESULTS[database_status]:-3},"
  echo "      \"message\": \"$(escape_json "${CHECK_RESULTS[database_message]:-Check not run}")\","
  echo "      \"enabled\": \"${PCLOUD_ENABLE_DB}\""
  echo "    },"
  echo "    \"sync_backlog\": ${CHECK_RESULTS[sync_backlog_json]}"
  
  echo "  },"
  echo "  \"issues\": ["
  
  # Output issues array
  issue_count=${#ISSUES[@]}
  i=0
  for issue in "${ISSUES[@]}"; do
    severity=$(echo "$issue" | cut -d: -f1)
    message=$(echo "$issue" | cut -d: -f2-)
    echo "    {"
    echo "      \"severity\": \"$severity\","
    echo "      \"message\": \"$(escape_json "$message")\""
    if [[ $((++i)) -lt $issue_count ]]; then
      echo "    },"
    else
      echo "    }"
    fi
  done
  
  echo "  ]"
  echo "}"

elif [[ $NAGIOS -eq 1 ]]; then
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
