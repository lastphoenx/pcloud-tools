#!/usr/bin/env bash
# Periodische pcloud_backup DB-Bereinigung + Reports neu generieren
#
# Voraussetzung: backup-pipeline.service ist inactive
#
# Usage:
#   sudo ./scripts/maintenance_db_cleanup.sh
#   sudo ./scripts/maintenance_db_cleanup.sh --purge-90d

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SQL_FILE="$REPO_ROOT/sql/maintenance_db_cleanup.sql"
PURGE_90D=0

for arg in "$@"; do
  case "$arg" in
    --purge-90d) PURGE_90D=1 ;;
    -h|--help)
      echo "Usage: $0 [--purge-90d]"
      echo "  --purge-90d  Zusaetzlich backup_runs aelter als 90 Tage loeschen"
      exit 0
      ;;
    *)
      echo "Unbekanntes Argument: $arg" >&2
      exit 1
      ;;
  esac
done

if ! command -v mysql >/dev/null 2>&1; then
  echo "mysql nicht gefunden" >&2
  exit 1
fi

if systemctl is-active --quiet backup-pipeline.service 2>/dev/null; then
  echo "backup-pipeline.service laeuft noch — bitte warten oder abbrechen." >&2
  exit 1
fi

echo "==> SQL: $SQL_FILE"
mysql pcloud_backup < "$SQL_FILE"

if [[ "$PURGE_90D" -eq 1 ]]; then
  echo "==> Purge: backup_runs > 90 Tage"
  mysql pcloud_backup -e "
    SELECT COUNT(*) AS deleting_runs
    FROM backup_runs
    WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);
    DELETE FROM backup_runs
    WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);
  "
fi

echo "==> Reports neu generieren"
"$SCRIPT_DIR/generate_reports.sh"

if systemctl list-unit-files monitoring-dashboard.service >/dev/null 2>&1; then
  echo "==> Dashboard neu laden"
  systemctl restart monitoring-dashboard.service
fi

echo "Fertig."
