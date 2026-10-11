#!/usr/bin/env bash
# Monatliche Remote-Retention (Zeit-Modus aus .env) + optional Pool-GC.
# Voraussetzung: backup-pipeline.service inactive; .env mit DAYS_FULL>0.
#
# Usage:
#   sudo ./scripts/run_monthly_retention.sh
#   sudo ./scripts/run_monthly_retention.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="${PCLOUD_PYTHON:-/opt/apps/pcloud-tools/venv/bin/python}"
ENV_FILE="${PCLOUD_ENV_FILE:-$REPO_ROOT/.env}"
DRY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run]"
      exit 0
      ;;
    *)
      echo "Unbekanntes Argument: $arg" >&2
      exit 1
      ;;
  esac
done

if systemctl is-active --quiet backup-pipeline.service 2>/dev/null; then
  echo "backup-pipeline.service läuft — Retention abbrechen." >&2
  exit 1
fi

cd "$REPO_ROOT"
GC_ARGS=(--env-file "$ENV_FILE" --retention-forecast --rtb-root "${RTB:-/mnt/backup/rtb_nas}")
"$PY" pcloud_pool_gc.py "${GC_ARGS[@]}"

APPLY_ARGS=(
  --env-file "$ENV_FILE"
  --retention-apply
  --run-gc
  --grace-hours 24
  --rtb-root "${RTB:-/mnt/backup/rtb_nas}"
)
if [[ "$DRY" -eq 1 ]]; then
  APPLY_ARGS+=(--dry-run)
fi

"$PY" pcloud_pool_gc.py "${APPLY_ARGS[@]}"
