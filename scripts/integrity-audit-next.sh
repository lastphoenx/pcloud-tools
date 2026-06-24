#!/usr/bin/env bash
# integrity-audit-next.sh — Batch-Integritaets-Audit (Pool-Cache, mehrere Snapshots/Lauf)
#
# Prioritaet pro Snapshot: nie -> FAILED -> STALE (>=35d) -> aeltester Audit
# INTEGRITY_AUDIT_MAX: Snapshots pro Lauf (default 10)
#
# systemd: integrity-audit.service + integrity-audit.timer (3x nach Backup +105min)

set -euo pipefail

MAIN_DIR=${MAIN_DIR:-/opt/apps/pcloud-tools/main}
ENV_FILE=${ENV_FILE:-${MAIN_DIR}/.env}
INTEGRITY_AUDIT_MAX=${INTEGRITY_AUDIT_MAX:-10}

if [[ -x "/opt/apps/pcloud-tools/venv/bin/python" ]]; then
  PY="/opt/apps/pcloud-tools/venv/bin/python"
else
  PY="${PY:-python3}"
fi

export MAIN_DIR ENV_FILE
exec "$PY" "${MAIN_DIR}/scripts/integrity-audit-next.py" --max "$INTEGRITY_AUDIT_MAX"
