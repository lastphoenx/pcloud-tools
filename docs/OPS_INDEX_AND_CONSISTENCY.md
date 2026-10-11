# Ops-Index, Konsistenz, Geister-Snapshots

Kurzüberblick nach dem RAM-sparenden Umbau (Backup-SQLite + separate **gc-engine** / Ops-SQLite). Details: [POOL_INDEX_DB.md](POOL_INDEX_DB.md), [pcloud_pool_gc.md](pcloud_pool_gc.md), Runbook [pool-index-gap-runbook.md](pool-index-gap-runbook.md).

## Zwei SQLite-Dateien

| Datei | Zweck |
|-------|--------|
| `pool_index.sqlite3` | **Backup-Pipeline** — Upload, Finalize, Integrity; wird bei jedem Lauf geschrieben. |
| `pool_index_gc.sqlite3` | **Ops-DB** — Kopie des Remote-Masters für GC/Retention; Voll-Reimport hier, Backup-DB bleibt unberührt. |

Remote-Wahrheit für den Pool-Inhalt: pCloud `_snapshots/_index/content_index.json` (lokal oft `content_index_master.json`).

## Wann wird die Ops-DB aktualisiert?

- **Automatisch:** Jeder Lauf von `pcloud_pool_gc.py` (Forecast, Retention, Pool-GC) startet Phase 1 mit `open_ops_db_for_queries` → bei geändertem Remote-SHA: Download + Streaming-Import (wie `pool_ops_index_sync.py`).
- **Nicht automatisch:** Nach normalem Backup/Finalize (nur Backup-DB + Master-Upload).
- **Manuell / optional:** `scripts/utilities/pool_ops_index_sync.py` — gleicher Import, ohne GC-Orphan-Scan.

**Geplant / nicht im Repo:** systemd „Sync-if-needed“ nach Pipeline (nur wenn Remote-SHA ≠ Ops-SHA). Monatlicher scharfer GC holt den Stand ohnehin nach; separater Job dient vor allem Monitoring (`pool_consistency_check`) zwischen Finalize und GC.

## `pool_consistency_check.py`

Lesender Vergleich (Exit 0 / 1 / 2):

| Signal | Bedeutung |
|--------|-----------|
| `live_not_in_ops_db` | **Kritisch** — Snapshot-Ordner auf pCloud, fehlt in Ops-DB (nach Finalize bis Sync/GC). |
| `live_not_in_backup_db` | **Kritisch** — Live-Snap fehlt in Backup-DB. |
| `stale_ops_snapshots` | **Warnung** — Name noch in Index (`snapshots`-Tabelle / Master), **kein** Remote-Ordner mehr. |
| `stale_local_manifests` | **Warnung** — lokales Manifest ohne Remote-Ordner (nach `--retention-apply` / `--delete-snapshots` werden betroffene Dateien automatisch entfernt). |
| `pool_files_missing` (`--pool`) | **Kritisch** — SHA aus **Manifesten** lebender Snaps fehlt im Pool-Listing. |
| `pool_index_listing_gaps` (`--pool-index`) | **Warnung** — Ops-Index-SHA vs. Listing (oft Phantom-Refs, siehe `pool_gap_report.py`). |

Optional langsam: `--pool` (Monitoring), bei Verdacht zusätzlich `--pool-index`. Ohne Flags: kein Pool-API-Scan.

WARN ≠ „Sync kaputt“. Nach erfolgreichem Ops-Sync kann `stale_ops_*` bleiben (Geister-Eintrag im Master).

## Wie entsteht ein Geister-Snapshot?

Typische Kette:

1. Remote-Snapshot-Ordner wurde gelöscht (Retention, manuell, abgebrochenes Lauf).
2. Der **Index** (`content_index.json`) wurde **nicht** zum gleichen Zeitpunkt bereinigt — z. B. Abbruch vor Export/Upload, `upload_pending` noch offen, alter Lauf ohne gc-engine-Purge, oder nur Ordner gelöscht ohne `--retention-apply`-Index-Pfad.

Ergebnis: **99** Ordner mit `.upload_complete`, **100** Namen im Master — `pool_consistency_check` meldet `stale_ops_snapshots`.

`pool_ops_index_sync` importiert den Master **ehrlich** — der Geist bleibt sichtbar, bis der Master repariert ist.

## Geister einmalig beheben (Beispiel)

Ersetze `GHOST=2026-07-23-040044` durch den Namen aus `stale_ops_snapshots`.

```bash
cd /opt/apps/pcloud-tools/main
set -a && . ./.env && set +a
PY=/opt/apps/pcloud-tools/venv/bin/python3
SNAPS_ROOT="${PCLOUD_DEST%/}/_snapshots"
BACKUP_DB="${PCLOUD_POOL_INDEX_DB_PATH:-/srv/pcloud-archive/indexes/pool_index.sqlite3}"
GHOST=2026-07-23-040044

systemctl is-active backup-pipeline.service   # → inactive

# 1) Ordner auf pCloud wirklich weg? (sollte nicht in remote_complete sein)
$PY scripts/utilities/pool_audit_status.py --env-file .env --json \
  | jq -r '.remote_complete[]' | grep -x "$GHOST" && echo "NOCH DA — STOP" || echo "OK: nicht complete"

# 2) Referenzen in Backup-DB
sqlite3 "$BACKUP_DB" "
SELECT s.name, COUNT(r.sha_id) AS refs
FROM snapshots s LEFT JOIN snap_refs r ON r.snap_id=s.id
WHERE s.name='$GHOST' GROUP BY s.id;"

# 3) Purge (purge_snapshot entfernt snap_refs + snapshots-Zeile)
$PY pool_index_db.py --db "$BACKUP_DB" purge-snapshot "$GHOST"

# 4) Master exportieren + Chunk-Upload nach pCloud (Pipeline-Code)
$PY -c "
import os, pcloud_bin_lib as pc, pool_index_db as pidb
import pcloud_push_json_pool_manifest_to_pcloud as push
cfg = pc.effective_config(env_file='.env', env_dir='.')
dest = os.environ['PCLOUD_DEST'].rstrip('/')
with pidb.open_db('$BACKUP_DB') as db:
    push.save_content_index_from_db(cfg, dest + '/_snapshots', db)
"

# 5) Ops-DB an neuen Remote-Stand (schnell wenn SHA gleich; sonst ~20–35 min Import)
$PY scripts/utilities/pool_ops_index_sync.py --env-file .env

$PY scripts/pool_consistency_check.py --env-file .env
```

Erwartung: `live_count` = `ops_db_count` = `backup_db_count` = `manifest_count` (99), keine `stale_ops_snapshots`.

## MariaDB-Kosmetik

- Standard: `scripts/maintenance_db_cleanup.sh` (superseded FAILED, RUNNING-Zombies, …).
- Preview: `./scripts/maintenance_db_cleanup.sh --preview-failed` oder `sql/maintenance_db_cleanup_failed_only_preview.sql`.
- Snapshots **ohne je SUCCESS**: `--failed-only` / `sql/maintenance_db_cleanup_failed_only.sql`.
- FAILED trotz SUCCESS (optional, Forensik weg): `--failed-superseded` / `sql/maintenance_db_cleanup_failed_superseded_optional.sql`.
- Danach: `scripts/generate_reports.sh`

## RAM / Dauer

Ops-Streaming-Import ~300k SHAs: auf pi-nas typisch **~20–35 min**, ~1–2 GB RAM, eine CPU hoch. Kein Import nötig, wenn Remote-SHA unverändert (Sekunden).

Siehe auch [ram-acceptance-pi.md](ram-acceptance-pi.md).
