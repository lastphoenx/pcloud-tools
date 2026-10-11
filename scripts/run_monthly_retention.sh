#!/usr/bin/env bash
# Monatliche Remote-Retention (Zeit-Modus aus .env) + optional Pool-GC.
# Voraussetzung: backup-pipeline.service inactive (oder Wartezeit); .env mit DAYS_FULL>0.
#
# Usage:
#   sudo ./scripts/run_monthly_retention.sh
#   sudo ./scripts/run_monthly_retention.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="${PCLOUD_PYTHON:-/opt/apps/pcloud-tools/venv/bin/python}"
ENV_FILE="${PCLOUD_ENV_FILE:-$REPO_ROOT/.env}"
WAIT_PIPELINE_SEC="${WAIT_PIPELINE_SEC:-10800}"
DRY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run]"
      echo "  WAIT_PIPELINE_SEC=${WAIT_PIPELINE_SEC} (Warten auf backup-pipeline.service)"
      exit 0
      ;;
    *)
      echo "Unbekanntes Argument: $arg" >&2
      exit 1
      ;;
  esac
done

_wait_pipeline_idle() {
  local elapsed=0
  while systemctl is-active --quiet backup-pipeline.service 2>/dev/null; do
    if [[ "$elapsed" -ge "$WAIT_PIPELINE_SEC" ]]; then
      echo "backup-pipeline.service noch aktiv nach ${WAIT_PIPELINE_SEC}s — Retention abgebrochen." >&2
      return 1
    fi
    echo "Warte auf backup-pipeline.service (${elapsed}s / ${WAIT_PIPELINE_SEC}s)…" >&2
    sleep 60
    elapsed=$((elapsed + 60))
  done
  return 0
}

_wait_pipeline_idle

cd "$REPO_ROOT"
FORECAST_ARGS=(--env-file "$ENV_FILE" --retention-forecast --rtb-root "${RTB:-/mnt/backup/rtb_nas}")
"$PY" pcloud_pool_gc.py "${FORECAST_ARGS[@]}"

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
