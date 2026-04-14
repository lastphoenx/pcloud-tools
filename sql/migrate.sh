#!/usr/bin/env bash
# =====================================================
# pCloud Backup DB Migration Tool
# =====================================================
# Purpose: Apply schema migrations in correct order
# Usage:
#   ./migrate.sh                     # Apply all pending migrations
#   ./migrate.sh --check             # Check current version
#   ./migrate.sh --force-recreate    # DROP + recreate (DESTRUCTIVE!)
# =====================================================

set -euo pipefail

# Config
PCLOUD_DB="${PCLOUD_DB:-/var/lib/pcloud-backup/runs.db}"
MIGRATIONS_DIR="$(dirname "$0")/migrations"
SCRIPT_DIR="$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Check if sqlite3 is available
if ! command -v sqlite3 &>/dev/null; then
  log_error "sqlite3 not found. Install with: sudo apt install sqlite3"
  exit 1
fi

# Get current schema version
get_current_version() {
  if [[ ! -f "$PCLOUD_DB" ]]; then
    echo "0"
    return
  fi
  
  # Check if schema_version table exists
  local has_table; has_table=$(sqlite3 "$PCLOUD_DB" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_version';" 2>/dev/null || echo "0")
  
  if [[ "$has_table" == "0" ]]; then
    echo "0"
    return
  fi
  
  # Get max version
  sqlite3 "$PCLOUD_DB" "SELECT COALESCE(MAX(version), 0) FROM schema_version;" 2>/dev/null || echo "0"
}

# Apply a migration
apply_migration() {
  local migration_file="$1"
  local version="$2"
  local description="$3"
  
  log_info "Applying migration v${version}: $description"
  
  # Apply in transaction
  sqlite3 "$PCLOUD_DB" <<SQL
BEGIN TRANSACTION;

-- Apply migration
$(cat "$migration_file")

-- Record version
INSERT INTO schema_version (version, description) VALUES (${version}, '${description}');

COMMIT;
SQL

  if [[ $? -eq 0 ]]; then
    log_info "✓ Migration v${version} applied successfully"
    return 0
  else
    log_error "✗ Migration v${version} failed!"
    return 1
  fi
}

# Force recreate database (DESTRUCTIVE!)
force_recreate() {
  log_warn "⚠️  FORCE RECREATE: This will DELETE ALL DATA!"
  read -p "Type 'YES' to confirm: " confirm
  
  if [[ "$confirm" != "YES" ]]; then
    log_info "Aborted"
    exit 0
  fi
  
  log_warn "Backing up existing DB..."
  if [[ -f "$PCLOUD_DB" ]]; then
    cp "$PCLOUD_DB" "${PCLOUD_DB}.backup.$(date +%Y%m%d-%H%M%S)"
    log_info "Backup created: ${PCLOUD_DB}.backup.*"
  fi
  
  log_warn "Dropping database..."
  rm -f "$PCLOUD_DB"
  
  log_info "Creating fresh schema..."
  sqlite3 "$PCLOUD_DB" < "${SCRIPT_DIR}/init_pcloud_db.sql"
  
  log_info "✓ Database recreated successfully"
}

# Check mode
check_version() {
  local version; version=$(get_current_version)
  
  log_info "Database: $PCLOUD_DB"
  log_info "Current schema version: ${version}"
  
  if [[ "$version" == "0" ]]; then
    log_warn "Database not initialized or legacy schema detected"
  fi
  
  # List available migrations
  if [[ -d "$MIGRATIONS_DIR" ]]; then
    log_info "Available migrations:"
    for migration in "$MIGRATIONS_DIR"/*.sql; do
      [[ -f "$migration" ]] || continue
      local basename; basename=$(basename "$migration")
      local ver; ver="${basename%%_*}"
      ver="${ver#0}"  # Remove leading zero
      echo "  - v${ver}: $(basename "$migration" .sql)"
    done
  fi
}

# Main migration logic
run_migrations() {
  local current_version; current_version=$(get_current_version)
  
  log_info "Current schema version: ${current_version}"
  
  # If DB doesn't exist or version is 0, run initial schema
  if [[ ! -f "$PCLOUD_DB" || "$current_version" == "0" ]]; then
    log_info "Initializing database with base schema..."
    
    # Create DB directory
    mkdir -p "$(dirname "$PCLOUD_DB")"
    
    # Apply init schema (safe, uses CREATE IF NOT EXISTS)
    sqlite3 "$PCLOUD_DB" < "${SCRIPT_DIR}/init_pcloud_db.sql"
    
    log_info "✓ Database initialized (schema v1)"
    return 0
  fi
  
  # If migrations dir exists, apply pending ones
  if [[ -d "$MIGRATIONS_DIR" ]]; then
    local applied=0
    
    for migration in "$MIGRATIONS_DIR"/*.sql; do
      [[ -f "$migration" ]] || continue
      
      local basename; basename=$(basename "$migration")
      local ver; ver="${basename%%_*}"
      ver="${ver#0}"  # Remove leading zero (01 → 1)
      local desc; desc="${basename#*_}"
      desc="${desc%.sql}"
      
      # Skip if already applied
      if (( ver <= current_version )); then
        continue
      fi
      
      # Apply migration
      apply_migration "$migration" "$ver" "$desc" || {
        log_error "Migration failed, rolling back..."
        exit 1
      }
      
      applied=$((applied + 1))
    done
    
    if [[ $applied -eq 0 ]]; then
      log_info "✓ Database is up to date (v${current_version})"
    else
      log_info "✓ Applied $applied migration(s)"
    fi
  else
    log_info "✓ Database is up to date (no migrations directory)"
  fi
}

# Parse args
case "${1:-}" in
  --check)
    check_version
    ;;
  --force-recreate)
    force_recreate
    ;;
  *)
    run_migrations
    ;;
esac
