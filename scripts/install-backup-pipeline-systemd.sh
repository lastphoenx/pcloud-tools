#!/usr/bin/env bash
# Deploy backup-pipeline.service + .timer from repo examples to /etc/systemd/system.
# Entfernt veraltetes memory-limit Drop-in (MemoryMax war kein RAM-Schutz, nur Verwirrung).
set -euo pipefail

MAIN_DIR="${MAIN_DIR:-/opt/apps/pcloud-tools/main}"
SVC_SRC="${MAIN_DIR}/systemd/backup-pipeline.service.example"
TMR_SRC="${MAIN_DIR}/systemd/backup-pipeline.timer.example"
DROPIN="/etc/systemd/system/backup-pipeline.service.d/memory-limit.conf"
DROPIN_DIR="/etc/systemd/system/backup-pipeline.service.d"

for f in "$SVC_SRC" "$TMR_SRC"; do
  if [[ ! -f "$f" ]]; then
    echo "Fehlt: $f — bitte zuerst: cd ${MAIN_DIR} && git pull" >&2
    exit 1
  fi
done

echo "→ /etc/systemd/system/backup-pipeline.service"
sudo cp "$SVC_SRC" /etc/systemd/system/backup-pipeline.service

echo "→ /etc/systemd/system/backup-pipeline.timer"
sudo cp "$TMR_SRC" /etc/systemd/system/backup-pipeline.timer

if [[ -f "$DROPIN" ]]; then
  echo "→ Entferne veraltetes Drop-in: $DROPIN"
  sudo rm -f "$DROPIN"
fi
if [[ -d "$DROPIN_DIR" ]] && [[ -z "$(ls -A "$DROPIN_DIR" 2>/dev/null)" ]]; then
  sudo rmdir "$DROPIN_DIR" 2>/dev/null || true
fi

sudo systemctl daemon-reload

echo ""
echo "OK. Prüfen (sollte KEIN MemoryMax / KEIN StandardOutput=append zeigen):"
systemctl cat backup-pipeline.service
