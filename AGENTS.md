# pcloud-tools — Hinweise für KI-Assistenten

## Versions-Wahrheit

Kanonisch: `requirements.txt` auf `main`. Deploy auf pi-nas — nach Dependency-Änderung venv/`pip install -r requirements.txt` (Betreiber, siehe private `doku/`).

## Dependabot

Security updates in GitHub aktivieren. Version-PRs: `.github/dependabot.yml` (weekly, gruppiert, semver-major ignoriert).

## Git

**`main`**. Commit/Push nur auf Nutzeranweisung. Kein Agent-SSH auf pi-nas.
