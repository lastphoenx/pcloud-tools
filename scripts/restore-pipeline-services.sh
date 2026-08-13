#!/usr/bin/env bash
# Re-enable pi-nas pipeline timers/services after manual maintenance (OOM recovery, staged resume, upload-only).
set -euo pipefail

TIMERS=(
  backup-pipeline.timer
  monitoring-status-update.timer
  monitoring-status-quick.timer
  monitoring-alert.timer
  entropywatcher-nas.timer
  entropywatcher-nas-av.timer
  entropywatcher-nas-av-weekly.timer
)

SERVICES=(
  monitoring-dashboard.service
)

echo "=== Pipeline-Services wieder aktivieren ==="

for unit in "${TIMERS[@]}"; do
  if systemctl list-unit-files "$unit" &>/dev/null; then
    echo "→ enable --now $unit"
    sudo systemctl enable --now "$unit"
  else
    echo "⚠ übersprungen (nicht installiert): $unit"
  fi
done

for unit in "${SERVICES[@]}"; do
  if systemctl list-unit-files "$unit" &>/dev/null; then
    echo "→ enable --now $unit"
    sudo systemctl enable --now "$unit"
  else
    echo "⚠ übersprungen (nicht installiert): $unit"
  fi
done

echo ""
echo "=== Status ==="
systemctl list-timers backup-pipeline monitoring-status-update monitoring-alert entropywatcher-nas --no-pager 2>/dev/null || true
systemctl is-active monitoring-dashboard.service backup-pipeline.timer 2>/dev/null || true

echo ""
echo "Optional: Status-JSON aktualisieren"
echo "  sudo systemctl start monitoring-status-update.service"
