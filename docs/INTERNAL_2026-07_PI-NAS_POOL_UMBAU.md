# Intern: pi-nas Pool-Pipeline Umbau (Juli 2026)

**Stand:** 2026-07-18  
**Betroffener Snapshot (Auslöser):** `2026-07-16-040030`  
**Host:** pi-nas (`/opt/apps/pcloud-tools/main`)  
**Remote:** `/Backup/rtb_pool`

---

## Kurzantwort: Ab welchem Commit?

| | Commit | Datum | Bedeutung |
|---|--------|-------|-----------|
| **Letzter Stand vor dem Umbau** | `1bcfcab` | 2026-07-03 | Nur Doku-Link in `pcloud_pool_gc.md` — **kein** Pool-Fix |
| **Erster Fix (Start des Umbaus)** | **`902e5fd`** | 2026-07-17 22:13 | RAM/OOM + Validation + Index-Upload |
| Aktuell (nach allen Fixes) | `cd7d128` | 2026-07-18 08:18 | inkl. Delta Phase-4-Progress |

**Ja:** `git pull` mit Meldung `Updating 1bcfcab..902e5fd` war der **erste** Schritt des großen Umbaus.  
Alle weiteren Fixes bauen direkt darauf auf (`4535f3c` → `d6c5426` → `cd7d128`).

```text
1bcfcab  docs: verlinke pi-nas cron-jobs …          ← letzter „alter“ Stand
902e5fd  fix(pool): RAM-sparende Batch-Validation   ← UMBAU START
4535f3c  fix(stubs): retry + progress Parent-FIDs
d6c5426  fix(api): API-Schonung, kein Full-Pool-Fallback
cd7d128  feat(delta): Fortschritts-Logging Phase 4
```

**Deploy auf pi-nas:** mindestens `cd7d128` (oder `git pull` auf `main`).

---

## Auslöser / Incident-Kette

| Zeitpunkt | Was passierte | Ursache (Code/Verhalten) |
|-----------|---------------|-------------------------|
| 2026-07-17 ~07:30 | OOM (~7,2 GB RSS), Prozess gekillt | Post-Upload-Validation: rekursives `listfolder` auf Pool + Snapshot; Index-Finalize via `json.dumps()` + In-Memory-Upload |
| 2026-07-17 ~23:38 | ~346 Stubs verloren, Validation „grün“, kein `.upload_complete` | `ensure_path` ohne Retry bei `socket closed`; Stub-Parent-FIDs fehlgeschlagen, Check übersprungen |
| 2026-07-18 ~00:37 | Full-Pool-Mode, 70 721 Ordner, ~173 GB geplant, Abbruch bei ~14 900 | Delta `copyfolder` fehlgeschlagen → Fallback Full-Pool; Pool-Scan fehlgeschlagen → alle SHAs als „neu“; 8 parallele Folder-Threads + kurze Retries → API-Hammering |
| 2026-07-18 ~07:27 | Delta-Lauf (Basis `2026-07-17-040029`, 97,5 %) | Nach `d6c5426`/`cd7d128`: kein Full-Pool-Fallback mehr bei Verbindungsfehlern |

**Kern-Erkenntnis:** `socket closed` war überwiegend **selbst verursacht** (zu viele parallele TLS-API-Calls + aggressive Retries), nicht pCloud-Ausfall.

---

## Commits im Detail

### `902e5fd` — fix(pool): RAM-sparende Batch-Validation und resumable Index-Upload

**Problem:** OOM auf 8-GB-Pi beim Finalize nach Upload.

**Umbau:**

| Bereich | Vorher | Nachher |
|---------|--------|---------|
| Post-Upload-Validation | Rekursives `listfolder` (gesamter Pool + Snapshot-Baum) | Batch-`stat()` in Parallel (`PCLOUD_VALIDATE_BATCH_SIZE`, `PCLOUD_VALIDATE_THREADS`) |
| Index-Upload | `json.dumps()` im RAM → direkt hochladen | Lokal auf SSD stagen → `upload_local_file_resumable()` |
| Finalize-Reihenfolge | Index vor Complete-Marker (riskant) | `gc.collect()` → `.upload_complete` → Index → Archiv |
| Lib (`pcloud_bin_lib.py`) | — | `get_resume_state_dir()`, `write_json_local_atomic()`, `upload_local_file_resumable()`, `write_json_to_folderid()` umgestellt |

**Dateien:** `pcloud_bin_lib.py`, `pcloud_push_json_pool_manifest_to_pcloud.py`, `.env.example`, `docs/ENV_VARIABLES.md`, `docs/DEVELOPER_GUIDE.md`, `docs/ARCHITECTURE.md`

**Neue ENV (Auszug):** `PCLOUD_VALIDATE_BATCH_SIZE`, `PCLOUD_VALIDATE_THREADS`, `PCLOUD_RESUME_CHUNK_MB`, `PCLOUD_VALIDATE_STUB_FULL`, …

---

### `4535f3c` — fix(stubs): retry + progress beim Parent-FolderID-Auflösen

**Problem:** Stub-Batch brach bei Parent-`folderid`-Auflösung ab (`socket closed`), Stubs ohne gültige Parents.

**Umbau:**

- `ensure_path` / `stat_folderid_fast` in Stub-Pipeline über `call_with_backoff`
- Fortschritt alle 100 Parents (`PCLOUD_STUB_FID_PROGRESS_EVERY`)
- Log bei übersprungenen Stubs nach Folder-ID-Fehler

**Dateien:** `pcloud_push_json_pool_manifest_to_pcloud.py`, `.env.example`, `docs/ENV_VARIABLES.md`

---

### `d6c5426` — fix(api): API-Schonung statt Full-Pool-Fallback bei Verbindungsfehlern

**Problem:** Transiente API-Fehler lösten Full-Pool-Mode aus (70k Ordner, Stunden/Tage Laufzeit).

**Umbau in `pcloud_bin_lib.py`:**

| Mechanismus | Default | Wirkung |
|-------------|---------|---------|
| `PCLOUD_API_META_CONCURRENCY` | 2 | Semaphore um `_rpc()` — max. gleichzeitige Binary-API-Verbindungen |
| `PCLOUD_API_META_DELAY` | 0,15 s | Pause nach jedem Meta-API-Call |
| `PCLOUD_CIRCUIT_BREAKER_ERRORS` | 12 | Abbruch nach N Verbindungsfehlern in Folge |
| `call_with_backoff` | — | Timestamp-Logs, längerer Backoff (2s→4s→8s…), `is_transient_api_error()` |
| `get_api_retry_count()` | — | Metrik am Laufende |

**Umbau in `pcloud_push_json_pool_manifest_to_pcloud.py`:**

- Delta `copyfolder` fehlgeschlagen + transient → **Abbruch** (kein Full-Pool-Fallback)
- Basis-Manifest fehlt → **Abbruch** (kein Full-Pool-Fallback)
- Preflight Pool-Scan fehlgeschlagen + transient → **Abbruch** (kein „alle SHAs neu“)
- Default `PCLOUD_FOLDER_THREADS` / `PCLOUD_POOL_FOLDER_THREADS`: 8/4 → **2**

---

### `cd7d128` — feat(delta): Fortschritts-Logging in Phase 4

**Problem:** Phase 4 wirkte „hängend“ (25+ Min ohne Log), obwohl tausende stille `stat()`-Calls liefen.

**Umbau:**

- Log nach Index-Laden: `[delta-mode] Phase 4: Index geladen (N pool_refs, lokal|remote) in X.Xs`
- Fortschritt wie `[folders]`/`[stubs]`: alle 100 Files + 10%-Sprünge + Ende  
  `[delta-mode] Phase 4: 500/2780 (18%) | uploaded=12 reused=488 failed=0 | ~25min verbleibend`
- ENV: `PCLOUD_DELTA_PROGRESS_EVERY` (Default 100)

---

## Empfohlene `.env` auf pi-nas (nach Umbau)

```bash
# --- Pool-Finalize (RAM-sparend) ---
PCLOUD_VALIDATE_BATCH_SIZE=5000
PCLOUD_VALIDATE_THREADS=8
PCLOUD_RESUME_CHUNK_MB=128
PCLOUD_CHUNK_DELAY=0.3

# --- API-Schonung (gegen socket closed) ---
PCLOUD_API_META_CONCURRENCY=2
PCLOUD_API_META_DELAY=0.15
PCLOUD_CIRCUIT_BREAKER_ERRORS=12

# --- Optional: Delta Phase 4 Sichtbarkeit ---
# PCLOUD_DELTA_PROGRESS_EVERY=100
```

Upload-/Folder-Threads bewusst **nicht** geändert, wenn sie bisher stabil waren — das API-Throttling ist der relevante Teil.

---

## API-Schnelltest (nach Deploy)

```bash
source /opt/apps/pcloud-tools/main/.env
python3 -c "
import pcloud_bin_lib as pc
cfg = pc.effective_config()
r = pc.listfolder(cfg, path='/Backup/rtb_pool/_pool', recursive=False, nofiles=True)
md = r.get('metadata') or {}
kids = [c['name'] for c in (md.get('contents') or []) if c.get('isfolder')]
print('result:', r['result'], '(0=OK)')
print('unterordner:', len(kids), '/ 256')
"
```

Erwartung: `result: 0`, `unterordner: 256`.

**Hinweis:** Config-Lader heißt `pc.effective_config()` — **nicht** `load_cfg`.

---

## Was der Umbau **nicht** ändert

- Delta-Mode-Grundlogik (Scout → `copyfolder` → Diff → Cleanup → Phase 4) bleibt
- Unvollständiger Snapshot wird weiter verworfen und per `copyfolder` neu geklont (kein Mid-Run-Resume über halbfertigen Snapshot)
- Pool-Dedup (`stat` auf Pool-Pfad) unverändert im Prinzip
- Upload-Parallelität (`PCLOUD_PARALLEL_UPLOAD_THREADS` etc.) nur indirekt gedrosselt via Meta-API-Semaphore

---

## Gesamt-Diff (1bcfcab → cd7d128)

```text
6 Dateien, +1085 / -597 Zeilen
  pcloud_bin_lib.py                           (+528 Zeilen netto)
  pcloud_push_json_pool_manifest_to_pcloud.py (großer Refactor Validation/Finalize/Delta)
  .env.example, docs/ENV_VARIABLES.md, DEVELOPER_GUIDE.md, ARCHITECTURE.md
```

---

## Referenzen

- Öffentliche Doku: [ENV_VARIABLES.md](ENV_VARIABLES.md) (Abschnitt Pool-Finalize), [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) (RAM-Strategie)
- Logs pi-nas: `/var/log/backup/pcloud_sync.log`, `/var/log/backup/rtb_wrapper.log`
- Wrapper: `sudo /opt/apps/rtb/rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-07-16-040030`
