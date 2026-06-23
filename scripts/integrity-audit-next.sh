#!/usr/bin/env bash
# integrity-audit-next.sh — Naechsten Snapshot fuer monatlichen Integritaets-Audit waehlen und pruefen.
#
# Ein Snapshot pro Lauf (RAM-schonend). Prioritaet:
#   1) Nie monthly_audit
#   2) FAILED monthly_audit
#   3) Aeltester monthly_audit (>35 Tage STALE bevorzugt)
#
# systemd: integrity-audit.service + integrity-audit.timer

set -euo pipefail

MAIN_DIR=${MAIN_DIR:-/opt/apps/pcloud-tools/main}
ENV_FILE=${ENV_FILE:-${MAIN_DIR}/.env}

if [[ -x "/opt/apps/pcloud-tools/venv/bin/python" ]]; then
  PY="/opt/apps/pcloud-tools/venv/bin/python"
else
  PY="${PY:-python3}"
fi

export MAIN_DIR ENV_FILE
exec "$PY" "${MAIN_DIR}/scripts/integrity-audit-next.py"
