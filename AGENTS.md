# pcloud-tools — Hinweise für KI-Assistenten

## pi-nas — Produktions-Layout (nicht raten)

Git-Checkout und Python-venv liegen **nebeneinander**, nicht im Repo-Root:

| Pfad | Bedeutung |
|------|-----------|
| `/opt/apps/pcloud-tools/main/` | **Arbeitskopie** (`git pull` hier). `.env`, `pcloud_pool_gc.py`, `scripts/` |
| `/opt/apps/pcloud-tools/venv` | **Symlink** auf die aktive venv, z. B. `venv-20260424-1410/` |
| `/opt/apps/pcloud-tools/venv-YYYYmmdd-HHMM/` | Datierte venvs (Rotation, siehe `docs/VENV_MANAGEMENT.md`) |

**Falsch auf pi-nas (nur Dev/Windows):** `source .venv/bin/activate` oder `main/.venv` — existiert in Produktion **nicht**.

**Richtig — Interpreter immer so (auch ohne `activate`):**

```bash
PY=/opt/apps/pcloud-tools/venv/bin/python
PIP=/opt/apps/pcloud-tools/venv/bin/pip
cd /opt/apps/pcloud-tools/main
```

Wrapper, Integrity-Audit und Health-Check nutzen dieselbe Konvention (`/opt/apps/pcloud-tools/venv/bin/python`), Fallback nur `python3` ohne venv.

**Zwei SQLite-Dateien (nicht eine DB mit „extra Tabellen“):** Backup = `…/indexes/pool_index.sqlite3`; gc-engine = `…/indexes/pool_index_gc.sqlite3` (`PCLOUD_GC_INDEX_DB_PATH`). Getrennte Dateien = kein Re-Import in die Backup-DB. gc-engine: Dry **und** scharf gleicher SHA-Fast-Path (`checksumfile`), kein `digest()`-Marathon auf Ops-DB.

**Integrity-Audit (separat):** `integrity-audit-next.py` — pro Snapshot **Subprocess** (kein `PoolRemoteCache` im Parent), Batch `INTEGRITY_AUDIT_MAX` (Doku/systemd).

**Nach `requirements.txt`-Änderung auf `main`:**

```bash
cd /opt/apps/pcloud-tools/main && git pull
/opt/apps/pcloud-tools/venv/bin/pip install -r requirements.txt
# optional Verifikation: /opt/apps/pcloud-tools/venv/bin/python -c "import ijson"
```

Größere Dependency-Umstellungen: `venv_rotate.sh` (neue datierte venv + Symlink) — `docs/VENV_MANAGEMENT.md`, `docs/SETUP.md`. Betrieb/Timer-Details: privates Doku-Repo.

**Beispiel (Pool-GC / delete-snapshots):**

```bash
cd /opt/apps/pcloud-tools/main
# --pool-root = PCLOUD_DEST aus .env (z. B. /Backup/rtb_pool); CLI-Flag optional wenn PCLOUD_DEST gesetzt
/opt/apps/pcloud-tools/venv/bin/python pcloud_pool_gc.py --env-file .env \
  --pool-root /Backup/rtb_pool --dry-run --delete-snapshots SNAP1,SNAP2
```

Agent: **kein SSH** auf pi-nas — Befehle als Copy-Paste mit obigen Pfaden liefern, nie `pip`/`python3` systemweit (PEP 668).

## Versions-Wahrheit

Kanonisch: `requirements.txt` auf `main`. Deploy auf pi-nas: `git pull` in `main/`, dann `venv/bin/pip install -r requirements.txt` (siehe Tabelle oben).

## Dependabot

Security updates in GitHub aktivieren. Version-PRs: `.github/dependabot.yml` (weekly, gruppiert, semver-major ignoriert).

## Git

**`main`**. Commit/Push nur auf Nutzeranweisung. Kein Agent-SSH auf pi-nas.
