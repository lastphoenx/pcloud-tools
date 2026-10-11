#!/usr/bin/env bash
# Periodische pcloud_backup DB-Bereinigung + Reports neu generieren
#
# Voraussetzung: backup-pipeline.service ist inactive
#
# Usage:
#   sudo ./scripts/maintenance_db_cleanup.sh
#   sudo ./scripts/maintenance_db_cleanup.sh --purge-90d
#   sudo ./scripts/maintenance_db_cleanup.sh --preview-failed
#   sudo ./scripts/maintenance_db_cleanup.sh --failed-only
#   sudo ./scripts/maintenance_db_cleanup.sh --failed-superseded   # Forensik weg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SQL_FILE="$REPO_ROOT/sql/maintenance_db_cleanup.sql"
PURGE_90D=0
PREVIEW_FAILED=0
FAILED_ONLY=0
FAILED_SUPERSEDED=0

for arg in "$@"; do
  case "$arg" in
    --purge-90d) PURGE_90D=1 ;;
    --preview-failed) PREVIEW_FAILED=1 ;;
    --failed-only) FAILED_ONLY=1 ;;
    --failed-superseded) FAILED_SUPERSEDED=1 ;;
    -h|--help)
      echo "Usage: $0 [--purge-90d] [--preview-failed] [--failed-only] [--failed-superseded]"
      echo "  --purge-90d           backup_runs aelter als 90 Tage loeschen"
      echo "  --preview-failed      nur SELECTs (failed_only_preview.sql)"
      echo "  --failed-only         DELETE nur FAILED ohne je SUCCESS"
      echo "  --failed-superseded   optional: FAILED trotz SUCCESS (mysqldump empfohlen)"
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

if [[ "$PREVIEW_FAILED" -eq 1 ]]; then
  echo "==> Preview: failed_only_preview.sql"
  mysql pcloud_backup < "$REPO_ROOT/sql/maintenance_db_cleanup_failed_only_preview.sql"
fi

if [[ "$FAILED_ONLY" -eq 1 ]]; then
  echo "==> DELETE: failed_only.sql (nur FAILED ohne je SUCCESS)"
  mysql pcloud_backup < "$REPO_ROOT/sql/maintenance_db_cleanup_failed_only.sql"
fi

if [[ "$FAILED_SUPERSEDED" -eq 1 ]]; then
  echo "==> DELETE: failed_superseded_optional.sql"
  mysql pcloud_backup < "$REPO_ROOT/sql/maintenance_db_cleanup_failed_superseded_optional.sql"
fi

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
