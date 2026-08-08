#!/usr/bin/env bash
# Kompatibilitäts-Stub: Legacy-1to1-Wrapper liegt in legacy/ (seit 2026-08).
# Alte Aufrufe/Pfade (rtb_wrapper, Doku, manuelle Scripts) bleiben gültig.
set -euo pipefail
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${_HERE}/legacy/wrapper_pcloud_sync_1to1.sh" "$@"
