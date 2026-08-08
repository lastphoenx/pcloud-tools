# pcloud_pool_gc.py – Pool Garbage Collection

> Stand: Juni 2026 · Skript: `pcloud_pool_gc.py` (Repo-Root) · Doku: `docs/pcloud_pool_gc.md`

## Zweck

Der deduplizierte Pool (`_pool/XX/<sha256>`) wächst mit jedem neuen Dateiinhalt. Wenn Snapshots per Retention gelöscht werden oder Dateien in neueren Snapshots nicht mehr vorkommen, bleiben **verwaiste Pool-Objekte** auf pCloud liegen — sie belegen Speicher, werden aber von keinem Snapshot mehr referenziert.

`pcloud_pool_gc.py` findet und löscht diese Objekte sicher.

---

## Architektur

```
/Backup/rtb_pool/                    ← --pool-root
  _pool/
    ab/abc123...                     ← physische Dateien (dedupliziert)
    cd/cdef456...
  _snapshots/
    _index/
      content_index.json             ← Ground Truth: pool_refs Keys = lebende SHAs
  .gc_lock                           ← gesetzt während laufendem Backup-Upload
```

### GC-Formel (snapshot-aware)

```
remote_snaps = Snapshot-Ordner unter _snapshots/ (ohne _index)
referenced   = SHAs in pool_refs, die mindestens einen remote_snaps-Eintrag haben
pool_files   = alle Dateien unter _pool/ (rekursiv, 64-Hex-Dateinamen)
candidates   = pool_files − referenced
```

Nur `candidates` werden gelöscht — und nur wenn sie älter als die **Grace Period** sind.

Stale Index-Einträge (SHAs nur noch in gelöschten Snapshots referenziert) blockieren GC nicht mehr.

---

## Ablauf (3 Phasen)

### Phase 0: GC-Lock-Check

Während ein Backup läuft, setzt `pcloud_push_json_pool_manifest_to_pcloud.py` eine `.gc_lock` Datei:

```json
{
  "pid": 12345,
  "host": "pi-nas",
  "started_at": 1717000000.0,
  "snapshot": "2026-06-10-040013",
  "task": "push_pool_manifest"
}
```

| Lock-Alter | Verhalten |
|------------|-----------|
| < 48h (Default `PCLOUD_GC_STALE_LOCK_HOURS`) | **Abbruch** — Backup läuft vermutlich noch |
| ≥ 48h (stale) | GC fährt fort (Backup vermutlich abgestürzt) |
| Kein Lock | GC fährt fort |

### Phase 1: Referenzen laden (Index-basiert, ~8s)

Lädt `_snapshots/_index/content_index.json` und ermittelt **aktive** SHAs: nur solche, deren `pool_refs`-Eintrag mindestens einen noch existierenden Remote-Snapshot referenziert.

**Performance:** ~0,1–10s statt Stunden bei Stub-Scan.

Beispiel-Log:

```
[gc] PHASE 1 DONE: 101159 active SHA256s (49 remote snaps, 0 stale index keys) (11.80s)
```

- **active SHA256s** — SHAs in `pool_refs`, die mindestens einen noch existierenden Remote-Snapshot referenzieren
- **remote snaps** — Snapshot-Ordner unter `_snapshots/` (ohne `_index`)
- **stale index keys** — Index-Einträge ohne Remote-Snapshot (blockieren GC nicht)

### Phase 2: Pool scannen (~5s)

Ein rekursiver `listfolder` über `_pool/` — findet alle physischen Pool-Dateien.

```
[gc] PHASE 2 DONE: 100904 pool files found (5.7s)
```

### Phase 3: Löschen (nur Unreferenzierte + Grace Period)

Vergleicht Pool-Set mit Referenz-Set. Dateien die im Index fehlen **und** älter als `--grace-hours` sind → löschen.

- **Pfad:** `pcloud_bin_lib.pool_file_remote_path()` wenn `listfolder` kein `path` liefert (rekursiv ohne `showpath`)
- **Delete:** `pcloud_bin_lib.delete_file()` per **REST** (nicht Binary-RPC), bevorzugt per **`fileid`** aus `listfolder`
- **Timeout:** `delete_file(..., size_bytes=N)` skaliert REST-Timeout für große Pool-Objekte (bis 600s)
- **Ablauf:** sequentiell (keine parallelen Deletes — stabiler bei wenigen großen Kandidaten)

```
[gc] Check complete: 4 to delete, 101159 to keep
[dry] delete: /Backup/rtb_pool/_pool/29/29b2b100... (343898684 bytes)
[gc] PHASE 3 DONE: 4 files deleted, 0.43 GB freed (0.1s)
```

---

## Dein Dry-Run (gesundes System)

```
Unique SHA256 refs: 101159
Pool files found:   101159
Pool files kept:    101159
Pool files deleted: 0
```

**Interpretation:** Jede physische Pool-Datei ist im Index referenziert. Kein Speicherleck, nichts zu löschen. Das ist der **Idealfall**.

Wenn `pool files found` > `unique refs` (z.B. 101163 vs. 101159), gibt es **4 Kandidaten** — mit Default `--grace-hours 24` werden sie oft noch **behalten** (Race-Protection). Zum Anzeigen: `--dry-run --grace-hours 0`.

---

## Parameter

| Parameter | Beschreibung |
|-----------|--------------|
| `--env-file` | `.env` mit pCloud-Credentials (Default: `.env`) |
| `--pool-root` | Remote Pool-Root auf pCloud, z.B. `/Backup/rtb_pool` |
| `--dest-root` | **(deprecated)** Alias für `--pool-root` |
| `--dry-run` | Zeigt was gelöscht würde, führt keine Löschungen aus |
| `--grace-hours` | Mindestalter in Stunden bevor Löschung (Default: 24, `0` = deaktiviert) |
| `--audit-mode` | Deep-Audit: validiert Index gegen physische Stubs (langsam!) |
| `--verbose` | Detailliertes Logging pro Datei |
| `--retention-forecast` | Retention-Simulation (read-only): RTB vs. Remote, geschätzte Pool-Einsparung |
| `--retention-apply` | Retention scharf: Remote-Snapshots ohne lokales RTB löschen + Index bereinigen |
| `--rtb-root` | Lokales RTB-Verzeichnis (Default: `RTB` aus `.env` oder `/mnt/backup/rtb_nas`) |
| `--run-gc` | Nach `--retention-apply` direkt Pool-GC ausführen (nur ohne `--dry-run`) |

### Umgebungsvariablen

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `PCLOUD_GC_STALE_LOCK_HOURS` | `48` | Ab wann ein `.gc_lock` als veraltet gilt |
| `PCLOUD_TIMEOUT` | `30`–`60` | Basis-REST-Timeout; `delete_file(size_bytes=…)` skaliert für große Objekte |

`PCLOUD_GC_WORKERS` wird nicht mehr verwendet (Deletes sequentiell).

---

## Beispiele

### Dry-Run (empfohlen vor erstem Produktionslauf)

```bash
cd /opt/apps/pcloud-tools/main

python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --dry-run --verbose
```

### Orphans nach fehlgeschlagenem Upload (typisch Juni 2026)

Wenn Pool-Upload validiert, aber Index zurückgerollt wurde, bleiben physische `_pool/`-Objekte ohne `pool_refs`-Eintrag (~450 MB). **tamper-detect** meldet sie als Hinweis, kein Backup-Defekt.

```bash
# Kandidaten sofort anzeigen (kein Backup laufen lassen):
python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env \
  --dry-run --grace-hours 0

# Aufräumen:
python pcloud_pool_gc.py --pool-root /Backup/rtb_pool --env-file .env --grace-hours 0
```

Mit Default `--grace-hours 24` erscheinen frische Orphans als `0 to delete` — das ist **korrekt**, nicht ein Bug.

### Produktions-GC (Standard, Grace 24h)

```bash
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --grace-hours 24
```

### Mit längerer Grace Period (48h)

```bash
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --grace-hours 48
```

### Audit-Mode (Index vs. Stubs validieren — langsam)

```bash
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --audit-mode \
  --dry-run --verbose
```

### Retention Forecast (read-only)

```bash
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --retention-forecast \
  --rtb-root /mnt/backup/rtb_nas
```

Zeigt: Remote-Snapshots ohne lokales RTB-Pendant, simulierte Pool-GC-Kandidaten und geschätzte GB-Einsparung.

### Retention Apply (scharf)

```bash
# Erst Dry-Run
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --retention-apply --dry-run

# Dann produktiv inkl. Pool-GC
python pcloud_pool_gc.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --retention-apply --run-gc --grace-hours 24
```

**Ablauf `--retention-apply`:**
1. Retention-Modus aus `.env` (`PCLOUD_REMOTE_RETENTION_DAYS_FULL` > 0 → **Zeit-Retention** 62d + Wochen-Tier; sonst rtb-spiegel)
2. **Sicherheitsabbruch**, wenn rtb-spiegel aktiv und RTB nicht gemountet oder 0 lokale Snapshots
3. Kandidaten löschen → `content_index.json` bereinigen
4. Optional `--run-gc`: verwaiste Pool-Dateien entfernen

**Wichtig:** Zeit-Retention liest `PCLOUD_REMOTE_RETENTION_*` aus der `--env-file` (nicht nur aus der Shell-Umgebung). Ohne das würde Cron fälschlich rtb-spiegel nutzen.

Der Wrapper ruft **kein** `--retention-sync` mehr auf — Retention gehört ins GC-Skript.

### Cron (wöchentlich / monatlich)

Siehe auch pi-nas-Übersicht: [Doku/cron-jobs.md](https://github.com/lastphoenx/Doku/blob/main/Raspi/raspinas/ops/cron-jobs.md) (falls Doku-Repo verfügbar).

```cron
# Nur Pool-GC (wenn Retention separat)
0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py \
  --env-file .env --pool-root /Backup/rtb_pool --grace-hours 24 \
  >> /var/log/backup/pool_gc.log 2>&1

# Retention + GC (z.B. monatlich)
0 4 1 * * cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py \
  --env-file .env --pool-root /Backup/rtb_pool \
  --retention-apply --run-gc --grace-hours 24 \
  >> /var/log/backup/pool_retention.log 2>&1
```

---

## Wann ausführen?

| Situation | GC sinnvoll? |
|-----------|--------------|
| Nach lokaler RTB-Retention | **Ja** — `--retention-apply --run-gc` |
| Nach Retention (Snapshots gelöscht) | **Ja** — freigibt Pool-Speicher |
| Wöchentlich per Cron | **Ja** — präventiv |
| Platzbudget knapp | **Ja** — nach Dry-Run |
| Während laufendem Backup | **Nein** — `.gc_lock` blockiert (oder Grace Period abwarten) |
| Direkt nach erstem Upload | Meist unnötig — noch keine Verwaisten |

---

## GC-Kandidaten vorab anzeigen (ohne Löschen)

`pool_verify_backup.py` zeigt aus anderer Perspektive Kandidaten an:

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_verify_backup.py \
  --env-file .env \
  --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests
```

| Metrik in `pool_verify_backup` | Bedeutung |
|--------------------------------|-----------|
| `gc_candidates` | Pool-SHAs ohne lokales Manifest (informativ) |
| `pool_not_in_index` | Pool-SHAs nicht in `pool_refs` → GC würde löschen |

**Unterschied:** `pcloud_pool_gc.py` nutzt den **Remote-Index** (`pool_refs`) als Ground Truth. `pool_verify_backup.py` vergleicht zusätzlich mit **lokalen Manifesten**.

---

## Sicherheitsmechanismen

1. **Index-basiert** — nur SHAs die nicht in `pool_refs` stehen, sind Kandidaten
2. **Grace Period** — frisch erstellte Pool-Dateien (z.B. während parallelem Upload) werden nicht sofort gelöscht; `modified` wird als Unix-Zahl oder HTTP-Datum geparst (`pcloud_bin_lib.parse_metadata_modified_ts`)
3. **GC-Lock** — laufende Backups werden erkannt und respektiert
4. **Dry-Run** — jederzeit testbar ohne Datenverlust
5. **REST-Delete mit Backoff** — `pcloud_bin_lib.delete_file()` (wie `delete_folder`), robust gegen Timeouts bei großen Objekten

---

## Exit-Codes

| Code | Bedeutung |
|------|-----------|
| `0` | Erfolg (auch Dry-Run mit 0 Löschungen) |
| `1` | Fehler beim Löschen (`errors > 0`) oder interner Abbruch (`result["error"]`) |
| `2` | Abbruch wegen GC-Lock (Backup läuft) |

Bei Lock-Abbruch (`backup_in_progress`) erscheint:

```
[gc] ❌ ABBRUCH: Backup läuft (Lock < 48h alt)
```

→ Warten bis Upload fertig ist, dann erneut versuchen.

---

## Troubleshooting

### `PHASE 1 DONE: 0 unique SHA256s`

→ Index leer oder falscher Pfad. Prüfen: `/Backup/rtb_pool/_snapshots/_index/content_index.json` existiert?

### `pool files found` > refs, aber `0 to delete`

→ Orphans innerhalb der **Grace Period** (Default 24h). `--dry-run --grace-hours 0` zum Anzeigen.

### Viele `to delete` im Dry-Run unerwartet

→ Erst `--dry-run --grace-hours 0 --verbose`, dann mit `pool_verify_backup.py` Index vs. Pool prüfen. Nicht blind löschen.

### `[dry] delete:  (bytes)` oder `API error 2010: Invalid path`

→ Veraltete Version: Pfad muss aus SHA256 konstruiert werden (`pool_file_remote_path`). `git pull` in `pcloud-tools`.

### `TimeoutError` beim Löschen

→ Veraltete Binary-`deletefile`-API oder parallele Deletes. Aktuell: REST-`delete_file` mit `size_bytes`-Timeout-Skalierung.

### GC löscht nichts trotz Retention

→ Grace Period noch nicht abgelaufen. `--grace-hours 0` nur wenn kein Upload läuft und Dry-Run geprüft.

### `--dest-root` Warnung

→ Auf `--pool-root` umstellen.

---

## Performance (Referenzwerte pi-nas, ~100k Pool-Objekte)

| Phase | Dauer |
|-------|-------|
| Index laden | ~11s |
| Pool scannen | ~5s |
| Vergleich + Dry-Run | <1s |
| **Gesamt Dry-Run** | **~17s** |
| **4 Deletes (Orphans)** | **<1s** (REST, sequentiell) |

Löschungen hängen von der Anzahl und Größe der Kandidaten ab; große Objekte (>50 MB) nutzen längere REST-Timeouts automatisch.

---

## Siehe auch

- `scripts/utilities/pool_restore.md` — Daten aus dem Pool wiederherstellen
- `docs/SETUP.md` §9 — Kurzanleitung GC
- `docs/DEVELOPER_GUIDE.md` — GC-Logik und `.gc_lock`
- `scripts/utilities/pool_verify_backup.py` — Integritätscheck vor/nach GC
