# C1: Pool-Index SQLite (Hybrid)

Lokaler Arbeitsindex für den **Delta-Pfad**. Remote bleibt `content_index.json` (v2).  
GC, Restore und Full-Pool-Upload lesen weiter den JSON-Master — nicht die SQLite.

**Produktion pi-nas:** `PCLOUD_POOL_INDEX_DB=1` (seit 15.08.2026). Flag-Default im Code weiterhin `0` für sichere Rollouts.

Rollback-Tag: `pre-c1-pool-index-db-2026-08-14` (`3ab8eea`).

---

## Was C1 ändert

Vorher: nach `copyfolder` registrierte Delta-Mode **jede** Manifest-Datei per `_register_snap` (~100k–165k Aufrufe auf einem 913-MB-Dict).

Jetzt (wenn Flag an):

1. SQLite auf SSD2 (`pool_index.sqlite3`)
2. Bulk-Merge per SQL-JOIN gegen die Basis (`(sha, relpath)` nur wenn unverändert)
3. Phase 4 schreibt nur added/changed/reused per `register_batch`
4. Finalize: streaming Export → bestehender Chunk-Upload

Unverändert: Integrity-Gate (Subprozess, `manifest_scoped`), Phase-4-Upload-Parallelität, Remote-JSON-Schema.

---

## Pfade

| Datei | Rolle |
|-------|--------|
| `/srv/pcloud-archive/indexes/pool_index.sqlite3` | Arbeitsindex (WAL: `.sqlite3-wal` / `-shm`) |
| `/srv/pcloud-archive/indexes/content_index_master.json` | Dual-Track, nach jedem DB-Export |
| Remote `_snapshots/_index/content_index.json` | unverändert, Chunk-Upload |
| Remote `_index/archive/<snap>_index.json` | unverändert, aus DB gefiltert |

---

## ENV

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `PCLOUD_POOL_INDEX_DB` | `0` | `1` = Delta/finalize-only nutzen SQLite |
| `PCLOUD_POOL_INDEX_DB_PATH` | `$PCLOUD_ARCHIVE_DIR/indexes/pool_index.sqlite3` | DB-Pfad |
| `PCLOUD_POOL_INDEX_DB_AUTOIMPORT` | `1` | Bei stale Fingerprint Master neu einlesen |
| `PCLOUD_POOL_INDEX_DB_SYNC_ON_GC` | `1` | Nach `delete-snapshots` / `retention-apply` SQLite anpassen |
| `PCLOUD_POOL_INDEX_DB_SYNC_MODE` | `auto` | `auto`=purge gelöschte Snaps; `import`=voller Import; `skip` |

### Re-Import: wann SQLite neu aus JSON gelesen wird

**Problem (15.08.2026):** GC/Retention schreibt `content_index_master.json` neu → **mtime** ändert sich → alter Code startete **~15 Min / ~169k SHAs** Re-Import obwohl Bytes identisch (~4 GB RAM-Spitze auf 8 GB Pi).

**Lösung:** In SQLite-`meta` werden Fingerprints gespeichert; vor `import_from_json` läuft `can_skip_master_reimport()`.

| Prüfung | Geschwindigkeit | Bedeutung |
|--------|-----------------|-----------|
| `master_mtime_ns` + `master_size` | ms | Datei unverändert seit letztem Lauf |
| `master_sha256` | ~1× Master lesen (~960 MB) | Byte-identisch trotz neuem mtime (GC-Rewrite) |
| `master_content_digest` | DB-Scan | SQLite-Inhalt passt zum Master-JSON |

**Log bei Skip:** `[index-db] Master unverändert (SHA256) — kein Re-Import (N SHAs)`

**Log bei echtem Import:** `[index-db] Master geändert → Re-Import`

```bash
# Status inkl. meta + Skip-Checks
python3 pool_index_db.py status

# Einmalig Hashes nachziehen (kein Re-Import; ~Master lesen + digest)
python3 pool_index_db.py refresh-meta
```

Nach jedem erfolgreichen `import_from_json`, Export (`save_content_index_from_db`) und GC-Sync (`purge-snapshot` + `refresh_master_metadata`) werden die Meta-Keys aktualisiert.

Stale ohne Skip: GC schreibt Master mit **geändertem Inhalt** → voller Import nötig. Mit `SYNC_ON_GC=1` und `SYNC_MODE=auto` reicht nach `delete-snapshots` meist `purge-snapshot` (schnell), kein Import.

---

## Migration (pi-nas)

Heavy-Ops-Lock, Timer aus, ~3 GB frei auf SSD2.

```bash
sudo systemctl stop backup-pipeline.timer
systemctl is-active backup-pipeline.service   # inactive

cp -a /srv/pcloud-archive/indexes/content_index_master.json \
      /srv/pcloud-archive/indexes/content_index_master.json.bak-pre-c1

cd /opt/apps/pcloud-tools/main
git pull origin main   # C1 + Re-Import-Skip auf main

/opt/apps/pcloud-tools/venv/bin/python tests/test_pool_index_db.py

flock /run/backup_pipeline.lock \
  /opt/apps/pcloud-tools/venv/bin/python pool_index_db.py import \
    --json /srv/pcloud-archive/indexes/content_index_master.json

flock /run/backup_pipeline.lock \
  /opt/apps/pcloud-tools/venv/bin/python pool_index_db.py verify \
    --json /srv/pcloud-archive/indexes/content_index_master.json
# expect ~165725 SHAs, ok: true
```

Dann in `.env`: `PCLOUD_POOL_INDEX_DB=1`

Nach erstem Lauf mit neuem Code oder manuell:

```bash
python3 pool_index_db.py refresh-meta
python3 pool_index_db.py status
# checks.can_skip_reimport: true, meta.master_sha256 gesetzt
```

### Testplan (ein Snap)

`PCLOUD_CATCHUP_MAX_PER_RUN=1`

1. `--finalize-only` auf fertigem Snap — Log `[index-db]`, Index-Upload
2. Kleiner Delta — `Bulk-Merge: N … [tier=db]` in Sekunden; Gate ~20 s; Index ~48 s; Upload ~77 s
3. `scripts/utilities/pcloud_verify_index_vs_manifests.py` + Archiv ~30 MB
4. Flag `0` — Legacy-Pfad finalisiert noch
5. GC-Rewrite Master (touch/`cp` gleiche Bytes) — **kein** Re-Import wenn `master_sha256` passt; `refresh-meta` nach altem Lauf
6. Timer wieder an

---

## Rollback

Billig zuerst:

```bash
# 1. Flag aus (nächster Lauf = alter Dict-Pfad)
# in .env: PCLOUD_POOL_INDEX_DB=0

# 2. Code
cd /opt/apps/pcloud-tools/main
git fetch --tags
git checkout pre-c1-pool-index-db-2026-08-14

# 3. Master
cp -a /srv/pcloud-archive/indexes/content_index_master.json.bak-pre-c1 \
      /srv/pcloud-archive/indexes/content_index_master.json

# 4. Falls ein schlechter Export schon remote liegt:
#    _snapshots/_index/archive/content_index_prev.json → content_index.json

# 5. DB verwerfen (optional, vor Re-Import)
rm -f /srv/pcloud-archive/indexes/pool_index.sqlite3*
```

---

## CLI

```bash
python pool_index_db.py import --json /srv/pcloud-archive/indexes/content_index_master.json
python pool_index_db.py export --out /tmp/content_index_export_check.json
python pool_index_db.py verify --json /srv/pcloud-archive/indexes/content_index_master.json
python pool_index_db.py status          # meta + can_skip_reimport
python pool_index_db.py refresh-meta    # Hashes backfill ohne Import
python pool_index_db.py stats
python pool_index_db.py purge-snapshot 2026-07-16-040030
```

Export ist **semantisch** v2 (nicht byte-identisch: SHA-Key-Reihenfolge, sortierte Relpaths). Vergleich über `verify`/`digest`, nicht `cmp`.

---

## Nicht in der DB

`pcloud_pool_gc.py`, `pool_restore.py`, Full-Pool-Loop, Repair-Scripts — unverändert JSON. Nach denen Fingerprint mismatch → Auto-Reimport.
