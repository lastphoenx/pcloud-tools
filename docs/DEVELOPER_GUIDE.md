# Developer Guide: pCloud Backup-Pipeline (Pool-Mode)

> Lebende Systemdokumentation für Entwickler und Betrieb.
> Bei strukturellen Änderungen bitte aktualisieren.
>
> **Stand:** Juni 2026 · **Modus:** Pool-only
> Architekturübersicht: [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 📋 Übersicht

| # | Komponente | Datei | Zweck |
|---|---|---|---|
| 1 | **Binary-API** | `pcloud_bin_lib.py` | pCloud HTTP-API, Streaming, Chunked Upload, Connection-Pooling |
| 2 | **Manifest-Generator** | `pcloud_json_pool_manifest.py` | Snapshot scannen → SHA256, mtime, inode → Manifest v4 |
| 3 | **Pool-Upload** | `pcloud_push_json_pool_manifest_to_pcloud.py` | Scout, Turbo-Delta, Full-Pool, Validation, Index-Management |
| 4 | **tamper-detect** | `pcloud_quick_delta.py` | Pool-Index v2 vs. Remote (fileid, hash, size, Stubs) |
| 5 | **Verifikation** | `scripts/utilities/pool_verify_backup.py` | Vollständiger Integritätscheck: Manifest→Pool→Stubs |
| 6 | **GC** | `pcloud_pool_gc.py` | Verwaiste Pool-Objekte entfernen |
| 7 | **Orchestrator** | `wrapper_pcloud_pool_sync_1to1.sh` | Catch-up-Loop, MariaDB, Preflight, tamper-detect |

**Pipeline-Aufruf:** `rtb_pool_wrapper.sh` → `rsync_tmbackup.sh` → `wrapper_pcloud_pool_sync_1to1.sh`

**RTB ↔ Pipeline (Kurz):** Zwei Exclude-Schichten — `excludes.txt` (nie ins Snapshot: `__pycache__`, `/restore/`, …) und `rtb_check_excludes.sh` (nur Check: `pcloud-archive/`, `pcloud-temp/` triggern nicht, werden aber mitgesichert). Post-Filter in `rtb_check_only_delta.py`. Dashboard: Backup-Trigger vs. Pipeline vs. Exclude-Policy. Details: [STORAGE_PATHS.md](./STORAGE_PATHS.md) § RTB vs. Pipeline, [DASHBOARD.md](./DASHBOARD.md) § Backup-Trigger.

---

## 🧱 Säule 1: Das Fundament — pcloud_bin_lib.py

### Warum eine eigene Binary-API?

pCloud bietet zwei APIs: JSON-API (einfach, langsam, Dateilimit) und Binary-API (schnell, streaming-fähig, kein Limit). Das Tool nutzt ausschließlich die Binary-API über HTTPS mit persistenten TCP-Verbindungen.

### Kritische Funktion: `read_json_at_path()`

Alle Metadata-Operationen gehen durch diese Funktion. Sie handled:
- Automatische Retry-Logik bei transienten Fehlern
- Timeout-Handling (konfigurierbar via `PCLOUD_COPYFOLDER_TIMEOUT`)
- Response-Validation (pCloud gibt manchmal `{}` statt Fehler zurück)

### Drei Stufen des Datei-Downloads

1. **`get_textfile()`** — Kleine Dateien (<1 MB): lädt komplett in RAM
2. **`download_binaryfile_to()`** — RAM-schonender Streaming-Download für beliebige Größen (chunked, 4 MB Chunks)
3. **`checksumfile()`** — Nur SHA256/pCloud-Hash anfordern, ohne Dateiinhalt zu laden

### Chunked Upload mit Resume (große Dateien >5 GB)

```python
# Konfiguration via .env:
PCLOUD_RESUME_THRESHOLD_GB=5    # ab wann Chunked-Upload
PCLOUD_RESUME_CHUNK_MB=128      # Chunk-Größe
```

**Mechanismus:**
1. `_upload_file_smart()`: wählt automatisch zwischen normalem und Chunked-Upload
2. `_upload_file_resumable()`: Initiiert Upload-Session, schreibt Resume-State nach jedem Chunk
3. Bei Unterbrechung: `server_offset` aus pCloud API gelesen, Upload ab diesem Byte fortgesetzt
4. Resume-State wird in `/srv/pcloud-archive/resume/` persistiert (Dateiname = `<safe_path>_<sha256>.state.json`)

### Parallele Uploads (kleine Dateien)

```python
PCLOUD_UPLOAD_THREADS=4          # Default: 4 Threads
PCLOUD_SMALL_FILE_THRESHOLD_MB=50  # Dateien < 50 MB → parallel
```

Kleine Dateien (<50 MB) werden mit 4 Threads parallel hochgeladen. Große Dateien laufen sequenziell (Chunked-Resume nicht thread-safe).

### Keep-Alive Session & DNS-Cache

```python
# Globale Session (Modul-Level, einmalig initialisiert)
_SESSION = requests.Session()
_SESSION.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=8))
```

Verhindert TCP-Reconnects zwischen API-Calls. Spart ~50 ms pro Call bei stabiler Verbindung.

---

## 🗃️ Säule 2: Pool-Struktur & v2-Index

### Pool-Struktur auf pCloud

```
/Backup/rtb_pool/
  _pool/
    00/ … FF/           ← 256 Ordner (erster Byte von SHA256)
      <sha256>          ← physische Datei (Originalinhalt, einmal gespeichert)

  _snapshots/
    <snap>/
      <relpath>.meta.json   ← Stub (Zeiger auf Pool-Objekt)
      .upload_started        ← Upload-Marker: läuft
      .upload_complete       ← Upload-Marker: fertig + validiert

    _index/
      content_index.json         ← Master-Index v2
      archive/
        <snap>_index.json        ← gefilterte per-Snapshot-Kopie
```

### v2-Index-Format (content_index.json)

Der Index ist **SHA256-keyed** (Pool ist sha-nativ: ein SHA = eine Datei, N Referenzen). `snapshots` ist eine **Map** `{snap → [relpaths]}`, nicht mehr eine Liste:

```json
{
  "version": 2,
  "pool_refs": {
    "f153611a35...9886": {
      "fileid": 96720201405,
      "hash": 5016324286669844513,
      "size": 805637,
      "snapshots": {
        "2026-04-27-173201": ["Gemeinsam/Rest/dokument.pdf"],
        "2026-05-15-120009": [
          "Gemeinsam/Rest/dokument.pdf",
          "Gemeinsam/Haus/Solar/dokument.pdf"
        ]
      }
    }
  }
}
```

**Felder:**
- `fileid`: pCloud-interne Datei-ID (für direkten Download ohne Pfad-Lookup)
- `hash`: pCloud-interner CRC/Hash (für Tamper-Detection via `listfolder`)
- `size`: Dateigröße in Bytes
- `snapshots`: Map `{snapshot_name → [relpaths_in_diesem_snapshot]}`
  - Eine SHA kann in einem Snapshot an mehreren Pfaden liegen (Dedup: z.B. leere Placeholder-Dateien)
  - `snapshots.keys()` = alle Snapshots die diese Datei enthalten (GC-Basis)

**Warum SHA-keyed statt Pfad-keyed?**
Der Pool ist sha-nativ: jede SHA wird physisch genau einmal gespeichert, egal wie viele Snapshots darauf zeigen. Pfade sind Metadaten — sie stehen in den Stubs und im `snapshots`-Map des Index.

### Stubs (.meta.json)

Jeder Stub unter `_snapshots/<snap>/<relpath>.meta.json` enthält:
```json
{
  "format_version": 1,
  "kind": "stub",
  "type": "pool_stub",
  "sha256": "f153611a35...",
  "pcloud_hash": 5016324286669844513,
  "size": 805637,
  "mtime": 1714220400.0,
  "relpath": "Gemeinsam/Rest/dokument.pdf",
  "pool_path": "/Backup/rtb_pool/_pool/f1/f153611a35...",
  "pool_fileid": 96720201405,
  "snapshot": "2026-04-27-173201"
}
```

**Restore-Logik:** Nutzer nennt Pfad → Stub lesen → `pool_fileid` → `download_binaryfile_to(fileid=...)`. Implementiert in `scripts/utilities/pool_restore.py` (Index-bulk oder `--relpath` via Stub).

**Versions-Historie (`--all-versions`):** `pool_refs[sha].snapshots` quer über alle Snapshots auswerten → Timeline (`changed`/`same` pro Snapshot). Download nach `out-dir/_versions/<relpath>/<snapshot>/`. Optional `--only-changed` lädt nur Einträge mit neuem SHA. Umbenennungen/Verschiebungen: noch nicht (siehe legacy `pcloud_file_history.sh`).

### Lokale Artefakte (`/srv/pcloud-archive/`)

```
manifests/                      ← Snapshot-Manifeste (je Snapshot ein JSON)
  <snap>.json                   ← relpath, sha256, mtime, size, inode, source_path
indexes/
  content_index_master.json     ← lokaler Spiegel des Remote-Index
deltas/
  delta_verify_<snap>.json      ← tamper-detect Reports
```

---

## 🚀 Säule 3: Pool-Upload-Engine

### Phase 1: Manifest-Erstellung (`pcloud_json_pool_manifest.py`)

```bash
pcloud_json_pool_manifest.py \
  --root /mnt/backup/rtb_nas/<snap> \
  --snapshot <snap> \
  --out /srv/pcloud-temp/pcloud_mani.<snap>.json \
  --hash sha256 \
  [--ref-manifest /srv/pcloud-archive/manifests/<prev>.json]
```

**Smart-Mode** (`--ref-manifest` oder `--auto-ref-manifest`): SHA256-Cache via mtime/size+inode. Auto-Pick wählt nach dem Scan bis zu 6 chronologisch nächste Archiv-Manifeste und nimmt die beste Deckung — **ein Walk**, kein separates Vorab-Scoring.

`ReferenceCache.lookup()`:
1. Gleicher `relpath` + gleiche `mtime` + gleiche `size` → SHA aus Cache
2. Gleicher `inode` (Hardlink) → SHA aus Cache
3. Sonst: frisch berechnen

### Scout: Best-Match-Suche (`scout_best_pool_basis`)

Bevor der Upload startet, sucht der Scout den besten Basis-Snapshot für Turbo-Delta:

```python
# Jaccard-Similarity: |current_shas ∩ basis_shas| / |current_shas ∪ basis_shas|
similarity = len(current_sha_set & basis_sha_set) / len(current_sha_set | basis_sha_set)
```

Kandidaten = Remote-Snapshots ∩ lokale Manifeste (ohne current). Scout wählt den mit höchster Similarity.

- **≥ 70%** → Turbo-Delta-Mode
- **< 70%** → Full-Pool-Mode

### Turbo-Delta-Mode (`push_pool_delta_mode`)

1. **Wipe incomplete**: besteht ein unvollständiger Remote-Snapshot (kein `.upload_complete`), wird er gelöscht und aus `pool_refs` entfernt
2. **`copyfolder(basis → neu)`**: Server-seitiger Klon (ein API-Call, ~50s für 20k Dateien)
3. **Manifest-Diff**: `current_paths - basis_paths` = added, deleted, changed
4. **Phase 3**: veraltete Stubs aus dem Klon löschen
5. **Phase 4**: neue/geänderte Dateien in `_pool` hochladen, Stubs schreiben (Resume nur wenn Remote-Stub existiert; `pool_refs` allein zählt nicht)
6. **Post-Upload-Validation**
7. **Index persistieren**: `content_index.json` erst **nach** erfolgreicher Validation (parallel zu `.upload_complete`)

**Fail-fast:** Kann eine Datei nicht in den Pool geladen werden → `failed`-Liste → `RuntimeError` vor Stubs/Index/Marker.

**Wipe + Bereinigung:** Beim Wipe eines unvollständigen Snapshots wird `snapshot_name` aus allen `pool_refs`-Einträgen entfernt. Zusätzlich bereinigt Phase 4 `pool_refs` vor dem Schreiben (wichtig nach manuellem Remote-Delete).

**Resume in Phase 4:** Nur wenn der Remote-Stub (`.meta.json`) existiert — nicht wenn der Eintrag nur in `pool_refs` steht. Geänderte Pfade (`changed_paths`) werden immer neu geschrieben.

**Index-Timing:** Ohne `.upload_complete` wird der Snapshot nicht in den persistierten `pool_refs` geschrieben. Verhindert „Index sagt fertig, Stubs fehlen“ beim Retry.

### Full-Pool-Mode (`push_pool_mode`)

1. **Preflight**: `listfolder(_pool)` → alle vorhandenen SHAs → Delta = Manifest-SHAs - Pool-SHAs
2. **Ordnerstruktur**: 256 `_pool/XX`-Ordner anlegen (idempotent)
3. **Upload**: Delta-SHAs hochladen (4 Threads für kleine, sequenziell für große)
4. **Stubs**: für alle Manifest-Dateien schreiben
5. **Index aktualisieren**
6. **Post-Upload-Validation**

### Post-Upload-Validation

Läuft nach **jedem** Modus. Ohne erfolgreiche Validation kein `.upload_complete`:

```python
# 1. Pool-SHA-Check (batch): stat() pro Manifest-SHA via _pool_object_present()
missing_in_pool = _validate_pool_shas_batched(...)

# 2. Index-Konsistenz: alle SHAs in pool_refs mit korrektem Snapshot?
for sha in manifest_sha256s:
    assert snapshot_name in pool_refs[sha]['snapshots']

# 3. Stub-Vollcheck (batch, 100%): stat() pro Manifest-relpath (.meta.json)
missing_stubs = _validate_stubs_batched(...)
```

**RAM-Strategie (Juli 2026):** Statt `listfolder(_pool)` + `listfolder(snapshot_dir)` (Millionen-Knoten-Bäume, OOM auf pi-nas) werden Pool-SHAs und Stubs in Batches mit parallelem `stat()` geprüft. Konfiguration: `PCLOUD_VALIDATE_BATCH_SIZE` (Default 5000), `PCLOUD_VALIDATE_THREADS` (Default 8). Siehe [ENV_VARIABLES.md](ENV_VARIABLES.md#pool-finalize-ram-sparend).

**Pool-Objekt-Lookup:** `_pool_object_present()` prüft zuerst `by_fileid` (aus `pool_refs`), dann Pfad in `by_path`.

**Pool-Backfill:** Fehlen wenige SHA256s im Pool (z.B. nach GC), lädt `validate_pool_snapshot` die Quelldateien aus dem Manifest nach (`PCLOUD_VALIDATE_POOL_BACKFILL_MAX`, Default 50). Deaktivieren: `PCLOUD_VALIDATE_POOL_BACKFILL_MAX=0`.

**Finalize-Reihenfolge (Delta):** `gc.collect()` → `.upload_complete` → Index-Upload (lokal + resumable) → Manifest archivieren.

---

## ✅ Säule 4: Verifikation & Integritätscheck

### tamper-detect (`pcloud_quick_delta.py`, Pool-Modus)

Auto-erkennung via `index["version"] >= 2 and pool_refs`:

```
listfolder(_pool + _snapshots, recursive)  →  1 API-Call, ~5s
listfolder gibt: fileid, pcloud_hash, size pro Objekt
```

Pro `pool_refs[sha]`:
- Pool-Objekt vorhanden? (`fileid` in `by_fileid` oder Pfad in `by_path`)
- `fileid` == `pool_refs[sha].fileid`?
- `pcloud_hash` == `pool_refs[sha].hash`?
- `size` == `pool_refs[sha].size`?
- Alle Stubs aus `snapshots`-Map vorhanden?

**Orphan Pool-Objekte (Juni 2026):** SHAs physisch in `_pool/`, aber nicht in `pool_refs` (z.B. nach fehlgeschlagenem Upload mit Index-Rollback) → **GC-Hinweis**, kein kritischer Backup-Fehler. Exit-Code bleibt 0; Dashboard/Wrapper werten Orphans nicht als CRITICAL.

### Vollständiger Integritätscheck (`pool_verify_backup.py`)

Manifest-getriebener Check: **lokale Manifeste als Ground Truth** × Remote-Zustand.

```
Phase 0 (parallel, ~5s):
  Thread 1: listfolder(_pool)      → SHA-Set (Dateiname = SHA256)
  Thread 2: listfolder(_snapshots) → Stub-Pfad-Set
  Thread 3: content_index.json     → pool_refs

Phase 1 (RAM, <1s):
  A) Für jede Manifest-SHA: in Pool-SHA-Set?
     → GC-Hinweis: Pool-SHAs ohne Manifest-Referenz
  B) Für jeden Manifest-relpath: Stub-Pfad im Stub-Set?

Phase 2 optional --stub-sample N:
  N zufällige Stubs lesen (8 Threads parallel)
  stub.sha256 == manifest.sha256?
  stub.pool_fileid == pool_refs[sha].fileid?
```

**Laufzeit:** ~5s für alle 4 Snapshots (~80k Dateien), +2s für 100 Stub-Sample.

```bash
# Standardlauf:
MAIN_DIR=/opt/apps/pcloud-tools/main python scripts/utilities/pool_verify_backup.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests

# Mit Stub-Inhalt-Probe:
... --stub-sample 100
```

### GC (`pcloud_pool_gc.py`)

Snapshot-aware: nur SHAs in `pool_refs` mit mindestens einem **existierenden** Remote-Snapshot zählen als referenziert.

```bash
python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file "$ENV_FILE" --dry-run
```

| Mechanismus | Implementierung |
|-------------|-----------------|
| Pool-Pfad aus SHA | `pcloud_bin_lib.pool_file_remote_path()` |
| Grace Period (`modified`) | `pcloud_bin_lib.parse_metadata_modified_ts()` |
| Löschen | `pcloud_bin_lib.delete_file()` REST, bevorzugt `fileid`, `size_bytes` skaliert Timeout |
| Lock | `.gc_lock` während Push |

**GC-Lock:** `.gc_lock`-Datei verhindert Race-Condition zwischen laufendem Upload und GC.

Siehe `docs/pcloud_pool_gc.md` für Retention-Apply, Grace-Period-Triage und Troubleshooting.

### `pcloud_bin_lib` — Pool-relevante Helfer (Juni 2026)

| Funktion | Zweck |
|----------|--------|
| `pool_file_remote_path(pool_root, sha256)` | `_pool/XX/<sha>` wenn `listfolder` kein `path` liefert |
| `parse_metadata_modified_ts(modified)` | Grace Period: Unix oder RFC-2822-`modified` |
| `delete_file(..., size_bytes=N)` | REST-`deletefile`, Timeout skaliert bei großen Objekten |
| `delete_folder(..., recursive=True)` | REST-`deletefolderrecursive` (Retention, Push-Cleanup) |

Skripte sollen diese API nutzen — kein dupliziertes Binary-RPC-Delete in Einzeltools.

**Backlog (vorbereitet, noch offen):** Pool-Pfad-Duplikate in Push/Delta/Restore → `docs/backlog_pool_lib_consolidation.md`

---

## 🔧 Konfiguration (`.env`)

```bash
# pCloud API
PCLOUD_TOKEN=...
PCLOUD_HOST=eapi.pcloud.com        # EU: eapi, US: api

# Pfade
PCLOUD_TEMP_DIR=/srv/pcloud-temp
PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive
RTB=/mnt/backup/rtb_nas            # RTB Snapshot-Root
PCLOUD_DEST=/Backup/rtb_pool       # Remote Pool-Root

# MariaDB
PCLOUD_DB_HOST=localhost
PCLOUD_DB_NAME=pcloud_backup
PCLOUD_DB_USER=pcloud_backup
PCLOUD_DB_PASS=...
PCLOUD_ENABLE_DB=1

# Performance
PCLOUD_UPLOAD_THREADS=4
PCLOUD_SMALL_FILE_THRESHOLD_MB=50
PCLOUD_RESUME_THRESHOLD_GB=5
PCLOUD_RESUME_CHUNK_MB=128
PCLOUD_COPYFOLDER_TIMEOUT=300      # Meta-Operationen: 300s statt 30s

# Scout
PCLOUD_SCOUT_THRESHOLD=0.70        # Mindest-Similarity für Turbo-Delta

# Validation
PCLOUD_VALIDATE_UPLOAD=0           # Default: listfolder-Gate im Push; 1 = legacy stat (~40min)

# Logging
PCLOUD_LOG=/var/log/backup/pcloud_sync.log
```

---

## 📈 Metriken & Monitoring

### MariaDB (`pcloud_backup`)

```sql
-- Lauf-Übersicht
SELECT snapshot_name, status, started_at, duration_sec, files_uploaded
FROM backup_runs ORDER BY started_at DESC LIMIT 10;

-- Phasen eines Laufs
SELECT phase_name, status, duration_sec
FROM backup_phases WHERE run_id = '...' ORDER BY started_at;

-- Pool-Wachstum (pro Lauf)
SELECT snapshot_name, files_uploaded, bytes_uploaded/1073741824 AS gb
FROM backup_runs WHERE status='SUCCESS' ORDER BY started_at;
```

### [metrics]-Zeile im Log

```
[metrics] uploaded_files=316 pool_reused=284 stubs_written=632 ...
```

- `uploaded_files`: echte neue Pool-Objekte hochgeladen
- `pool_reused`: Pool-Objekte die schon existierten (Dedup-Treffer)
- `stubs_written`: immer 2× `uploaded_files` + reused (Delta + alle Manifest-Dateien)

---

## 🔐 Sicherheitsaspekte

### Daten-Integrität

- Post-Upload-Validation verhindert `.upload_complete` bei inkonsistentem Snapshot
- Fail-fast bei Upload-Fehlern: kein Stub/Index ohne Pool-Objekt
- GC-Lock verhindert Race zwischen Upload und GC

### Wipe-Schutz

Unvollständige Snapshots werden beim Neustart automatisch erkannt und bereinigt. Stammdaten (`pool_refs`) werden vor dem Neuaufbau konsistent gehalten: `snapshot_name` wird aus allen Einträgen entfernt.

### Concurrency

Thread-Safety im Delta-Mode via `threading.Lock()` (`_state_lock`):
- Alle Schreibzugriffe auf `pool_refs` und Counter-Updates
- `_upload_to_pool()` kann parallel aufgerufen werden; Pool-Check intern serialisiert

---

## 🧪 Wichtige Testszenarien

```bash
# 1. Dry-Run eines bestehenden Snapshots
./wrapper_pcloud_pool_sync_1to1.sh 2026-05-15-120009 --dry-run

# 2. Vollständiger Integritätscheck
python scripts/utilities/pool_verify_backup.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests --stub-sample 100

# 3. Pool-Check remote (quick)
python scripts/utilities/pool_check_remote.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --snapshot 2026-05-15-120009

# 4. tamper-detect
python legacy/pcloud_quick_delta.py --dest-root /Backup/rtb_pool --env-file "$ENV_FILE"

# 5. Undefined-Names-Check (vor Deployment)
python scripts/utilities/check_undefined_names.py \
  pcloud_push_json_pool_manifest_to_pcloud.py \
  pcloud_json_pool_manifest.py pcloud_quick_delta.py

# 6. Simulate Wipe+Restart (manuell)
# .upload_complete löschen → nächster Lauf wipet + startet sauber neu
```

---

## 📚 Verwandte Dokumentation

| Datei | Inhalt |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | System-Übersicht, Datenstrukturen, Modi |
| [SETUP.md](./SETUP.md) | Ersteinrichtung, Abhängigkeiten, Pool-Bootstrap |
| [ENV_VARIABLES.md](./ENV_VARIABLES.md) | Vollständige ENV-Variablen-Referenz |
| [BACKUP_RETENTION_DEEP_DIVE.md](./BACKUP_RETENTION_DEEP_DIVE.md) | RTB + Pool Retention-Strategie |
| [VENV_MANAGEMENT.md](./VENV_MANAGEMENT.md) | Python-venv Setup |
