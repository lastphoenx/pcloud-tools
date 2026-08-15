# Pool-Index in SQLite (lokaler Arbeits-Index)

Diese Seite erklärt, warum auf pi-nas der Pool-Index während des Uploads in **SQLite** liegt — und was in pCloud weiterhin als **JSON** bleibt.

**Kurz:** Der Upload muss wissen, welche Datei-Hashes schon im Pool existieren und in welchen Snapshots sie vorkommen. Das ist der **Pool-Index**. Auf einem Pi mit 8 GB RAM kann der Index nicht als riesiges Dict im Speicher liegen → SQLite auf der SSD.

**Produktion pi-nas:** `PCLOUD_POOL_INDEX_DB=1` (seit 15.08.2026). Im Code ist der Default weiter `0`, damit Rollouts sicher bleiben.

Rollback-Tag falls nötig: `pre-c1-pool-index-db-2026-08-14` (`3ab8eea`).

---

## Was ist der Pool-Index?

| Begriff | Bedeutung |
|---------|-----------|
| **Pool** | Physische Dateien in pCloud, einmal pro SHA256-Hash |
| **Snapshot** | Verzeichnisstruktur als Stubs (kleine JSON-Dateien) |
| **Pool-Index** | Gesamtliste: Hash → wo die Datei liegt + in welchen Snapshots sie vorkommt |

Der Index ist groß (~700–960 MB als JSON). Beim **Turbo-Delta**-Upload wird er ständig aktualisiert (neue Dateien, geänderte Pfade, gelöschte Einträge).

---

## Zwei Speicherorte — nicht verwechseln

| Ort | Format | Zweck |
|-----|--------|--------|
| **Lokal auf pi-nas** (`pool_index.sqlite3`) | SQLite | Arbeits-Index **nur während** des Uploads |
| **In pCloud** (`_snapshots/_index/content_index.json`) | JSON (v2) | Offizielle Remote-Kopie, Chunk-Upload nach jedem Lauf |

SQLite ersetzt **nicht** das Cloud-Format. Nach jedem erfolgreichen Lauf exportiert das Tool den SQLite-Stand wieder als JSON und lädt ihn nach pCloud hoch — wie vorher.

**Weiterhin JSON (nicht SQLite):** Pool-GC, Restore, Full-Pool-Upload ohne Delta-Basis, Repair-Skripte. Wenn die lokale Master-JSON nach so einem Tool geändert wurde, kann ein Re-Import in SQLite nötig sein (siehe unten).

---

## Problem ohne SQLite

Alter Delta-Pfad (ohne `PCLOUD_POOL_INDEX_DB`):

1. Master-JSON (~900 MB) wird als Python-Dict geladen
2. Nach `copyfolder` wird **jede** Datei im Manifest einzeln registriert (`_register_snap`)
3. Bei ~100k–165k Dateien: hoher RAM-Bedarf, Risiko OOM auf 8 GB Pi

---

## Lösung mit SQLite

Mit `PCLOUD_POOL_INDEX_DB=1`:

1. Index liegt auf SSD2 als `pool_index.sqlite3` (mit WAL-Dateien `-wal` / `-shm`)
2. **Turbo-Delta** und **Full-Pool** nutzen SQLite (kein 900-MB-Dict im RAM)
3. Unveränderte Dateien vom Basis-Snapshot werden per SQL-Bulk-Merge übernommen (Delta)
4. Full-Pool: `register_batch` statt `_register_snap` auf Dict
5. Am Ende: Streaming-Export → JSON → bestehender Chunk-Upload nach pCloud

**Unverändert:** Integrity-Gate, parallele Uploads in Phase 4, JSON-Schema in der Cloud.

---

## Dateien auf pi-nas

| Pfad | Rolle |
|------|--------|
| `/srv/pcloud-archive/indexes/pool_index.sqlite3` | SQLite-Arbeitsindex |
| `/srv/pcloud-archive/indexes/pool_index.sqlite3-wal` / `-shm` | SQLite WAL (normal bei laufender DB) |
| `/srv/pcloud-archive/indexes/content_index_master.json` | Lokale Master-JSON (Dual-Track, nach jedem DB-Export) |
| pCloud `_snapshots/_index/content_index.json` | Remote-Master (Chunk-Upload) |
| pCloud `_index/archive/<snap>_index.json` | Snapshot-spezifischer Index-Auszug |

---

## Umgebungsvariablen

| Variable | Default | Was sie tut |
|----------|---------|-------------|
| `PCLOUD_POOL_INDEX_DB` | `0` | `1` = SQLite für Delta-Upload und Finalize |
| `PCLOUD_POOL_INDEX_DB_PATH` | `$PCLOUD_ARCHIVE_DIR/indexes/pool_index.sqlite3` | Pfad zur SQLite-Datei |
| `PCLOUD_POOL_INDEX_DB_AUTOIMPORT` | `1` | Master-JSON bei Bedarf automatisch in SQLite einlesen |
| `PCLOUD_POOL_INDEX_DB_SYNC_ON_GC` | `1` | SQLite nach GC/Retention anpassen |
| `PCLOUD_POOL_INDEX_DB_SYNC_MODE` | `auto` | `auto` = gelöschte Snapshots aus DB entfernen; `import` = voller Import; `skip` = nichts tun |

---

## Re-Import: wann SQLite die JSON neu einliest

### Das Problem (15.08.2026)

Pool-GC oder Retention schreibt `content_index_master.json` neu. Der **Inhalt** ist gleich, aber das **Datei-Datum** ändert sich. Alter Code: „Datei neu → voller Re-Import“ → **~15 min**, **~169k Hashes**, **~4 GB RAM-Spitze** — unnötig.

### Die Lösung

Vor dem Einlesen prüft `can_skip_master_reimport()` gespeicherte Fingerprints in der SQLite-`meta`-Tabelle:

| Prüfung | Dauer | Wenn positiv |
|--------|-------|--------------|
| Dateigröße + mtime | Millisekunden | Datei seit letztem Lauf nicht angefasst |
| `master_sha256` | ~1× Master lesen (~960 MB) | Bytes identisch, auch wenn GC nur mtime geändert hat |
| `master_content_digest` | DB-Scan | SQLite-Inhalt passt zum Master-JSON |

**Log bei Skip (gut):** `[index-db] Master unverändert (SHA256) — kein Re-Import (N SHAs)`

**Log bei echtem Import:** `[index-db] Master geändert → Re-Import`

Fingerprints werden nach erfolgreichem Import, Export und GC-Sync aktualisiert.

### Diagnose-Befehle

```bash
# DB-Größe, SHA-Counts, meta-Fingerprints, ob Re-Import übersprungen würde
python3 pool_index_db.py status

# Fingerprints nachziehen (nach Upgrade, ohne voller Re-Import)
python3 pool_index_db.py refresh-meta
```

Erwartung nach `refresh-meta`: `checks.can_skip_reimport: true`, `meta.master_sha256` gesetzt.

**Echter Re-Import nötig**, wenn GC den Master-Inhalt wirklich geändert hat. Mit `SYNC_ON_GC=1` und `SYNC_MODE=auto` reicht nach `delete-snapshots` meist `purge-snapshot` (schnell) statt vollem Import.

---

## Erst-Einrichtung / Migration (pi-nas)

Vorher: Heavy-Ops-Lock frei, Timer stoppen, ~3 GB frei auf SSD2.

```bash
sudo systemctl stop backup-pipeline.timer
systemctl is-active backup-pipeline.service   # sollte inactive sein

cp -a /srv/pcloud-archive/indexes/content_index_master.json \
      /srv/pcloud-archive/indexes/content_index_master.json.bak-pre-c1

cd /opt/apps/pcloud-tools/main
git pull origin main

/opt/apps/pcloud-tools/venv/bin/python tests/test_pool_index_db.py

flock /run/backup_pipeline.lock \
  /opt/apps/pcloud-tools/venv/bin/python pool_index_db.py import \
    --json /srv/pcloud-archive/indexes/content_index_master.json

flock /run/backup_pipeline.lock \
  /opt/apps/pcloud-tools/venv/bin/python pool_index_db.py verify \
    --json /srv/pcloud-archive/indexes/content_index_master.json
# Erwartung: ~165k SHAs, ok: true
```

In `.env` setzen: `PCLOUD_POOL_INDEX_DB=1`

Optional nach erstem Lauf oder Upgrade:

```bash
python3 pool_index_db.py refresh-meta
python3 pool_index_db.py status
```

---

## Testplan (ein Snapshot)

`PCLOUD_CATCHUP_MAX_PER_RUN=1` empfohlen.

1. `--finalize-only` auf fertigem Snap — Log `[index-db]`, Index-Upload
2. Kleiner Delta-Lauf — `Bulk-Merge: N … [tier=db]` in Sekunden; Gate ~20 s; Index-Upload ~1–2 min
3. `scripts/utilities/pcloud_verify_index_vs_manifests.py` + Archiv-Index plausibel
4. Flag `0` — Legacy-Pfad finalisiert noch (Rollback-Test)
5. Master neu schreiben mit gleichen Bytes (touch/`cp`) — **kein** Re-Import wenn `master_sha256` passt
6. Timer wieder aktivieren

---

## Rollback

Schrittweise (billig zuerst):

```bash
# 1. Flag aus — nächster Lauf nutzt wieder Dict im RAM
# in .env: PCLOUD_POOL_INDEX_DB=0

# 2. Code auf Stand vor SQLite
cd /opt/apps/pcloud-tools/main
git fetch --tags
git checkout pre-c1-pool-index-db-2026-08-14

# 3. Master-JSON zurück
cp -a /srv/pcloud-archive/indexes/content_index_master.json.bak-pre-c1 \
      /srv/pcloud-archive/indexes/content_index_master.json

# 4. Falls schlechter Export schon remote liegt:
#    _snapshots/_index/archive/content_index_prev.json → content_index.json

# 5. SQLite löschen (optional)
rm -f /srv/pcloud-archive/indexes/pool_index.sqlite3*
```

---

## CLI-Referenz

```bash
python pool_index_db.py import --json /srv/pcloud-archive/indexes/content_index_master.json
python pool_index_db.py export --out /tmp/content_index_export_check.json
python pool_index_db.py verify --json /srv/pcloud-archive/indexes/content_index_master.json
python pool_index_db.py status          # meta + can_skip_reimport
python pool_index_db.py refresh-meta    # Fingerprints ohne Import
python pool_index_db.py stats
python pool_index_db.py purge-snapshot 2026-07-16-040030
```

**Hinweis Export:** Semantisch identisch mit v2-JSON, aber nicht byte-identisch (sortierte Keys/Pfade). Vergleich über `verify` / `digest`, nicht `cmp`.

---

## Siehe auch

- [README — Speicher (RAM)](../../README.md#speicher-ram-auf-dem-raspberry-pi)
- [CHANGELOG_2026-08.md](CHANGELOG_2026-08.md)
