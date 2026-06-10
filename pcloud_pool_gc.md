# pcloud_pool_gc.py – Pool Garbage Collection

> Stand: Juni 2026 · Skript: `pcloud_pool_gc.py` (Repo-Root)

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

### GC-Formel

```
pool_files  = alle Dateien unter _pool/ (rekursiv, 64-Hex-Dateinamen)
referenced  = Keys von pool_refs in content_index.json
candidates  = pool_files − referenced
```

Nur `candidates` werden gelöscht — und nur wenn sie älter als die **Grace Period** sind.

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

Lädt `_snapshots/_index/content_index.json` und extrahiert alle SHA256-Keys aus `pool_refs`.

**Performance:** ~0,1–10s statt Stunden bei Stub-Scan.

Beispiel-Log:

```
[gc] PHASE 1 DONE: 100904 unique SHA256s, 1852385 total refs (8.80s)
```

- **100904 unique SHA256s** — verschiedene Dateiinhalte im Index referenziert
- **1852385 total refs** — Summe aller Snapshot-Pfad-Zuordnungen (eine SHA kann in vielen Snapshots/Pfaden vorkommen)

### Phase 2: Pool scannen (~5s)

Ein rekursiver `listfolder` über `_pool/` — findet alle physischen Pool-Dateien.

```
[gc] PHASE 2 DONE: 100904 pool files found (5.7s)
```

### Phase 3: Löschen (nur Unreferenzierte + Grace Period)

Vergleicht Pool-Set mit Referenz-Set. Dateien die im Index fehlen **und** älter als `--grace-hours` sind → löschen.

```
[gc] Check complete: 0 to delete, 100904 to keep
[gc] PHASE 3 SKIPPED: No unreferenced files found
```

---

## Dein Dry-Run (gesundes System)

```
Unique SHA256 refs: 100904
Pool files found:   100904
Pool files kept:    100904
Pool files deleted: 0
```

**Interpretation:** Jede physische Pool-Datei ist im Index referenziert. Kein Speicherleck, nichts zu löschen. Das ist der **Idealfall**.

Wenn nach Retention-Löschungen Zahlen divergieren (z.B. 100904 refs, 101200 pool files), würde GC die 296 Differenz-Dateien nach Ablauf der Grace Period entfernen.

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

### Umgebungsvariablen

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `PCLOUD_GC_STALE_LOCK_HOURS` | `48` | Ab wann ein `.gc_lock` als veraltet gilt |
| `PCLOUD_GC_WORKERS` | `8` | Parallele Delete-Threads |

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

### Produktions-GC

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

### Cron (wöchentlich, Sonntag 03:00)

```cron
0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py \
  --env-file .env --pool-root /Backup/rtb_pool --grace-hours 24 \
  >> /var/log/backup/pool_gc.log 2>&1
```

---

## Wann ausführen?

| Situation | GC sinnvoll? |
|-----------|--------------|
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
2. **Grace Period** — frisch erstellte Pool-Dateien (z.B. während parallelem Upload) werden nicht sofort gelöscht
3. **GC-Lock** — laufende Backups werden erkannt und respektiert
4. **Dry-Run** — jederzeit testbar ohne Datenverlust
5. **Parallel-Delete mit Backoff** — robust gegen transiente pCloud-API-Fehler

---

## Exit-Codes

| Code | Bedeutung |
|------|-----------|
| `0` | Erfolg (auch Dry-Run mit 0 Löschungen) |
| `1` | Fehler beim Löschen (`errors > 0`) oder Backup-Lock-Abbruch |

Bei Lock-Abbruch (`backup_in_progress`) erscheint:

```
[gc] ❌ ABBRUCH: Backup läuft (Lock < 48h alt)
```

→ Warten bis Upload fertig ist, dann erneut versuchen.

---

## Troubleshooting

### `PHASE 1 DONE: 0 unique SHA256s`

→ Index leer oder falscher Pfad. Prüfen: `/Backup/rtb_pool/_snapshots/_index/content_index.json` existiert?

### Viele `to delete` im Dry-Run unerwartet

→ Erst `--verbose --dry-run`, dann mit `pool_verify_backup.py` Index vs. Pool prüfen. Nicht blind löschen.

### GC löscht nichts trotz Retention

→ Grace Period noch nicht abgelaufen. `--grace-hours 0` nur mit Vorsicht (Race-Risiko während Uploads).

### `--dest-root` Warnung

→ Auf `--pool-root` umstellen.

---

## Performance (Referenzwerte pi-nas, ~100k Pool-Objekte)

| Phase | Dauer |
|-------|-------|
| Index laden | ~8s |
| Pool scannen | ~6s |
| Vergleich + Dry-Run | <1s |
| **Gesamt Dry-Run** | **~15s** |

Löschungen hängen von der Anzahl der Kandidaten ab (parallel, 8 Workers).

---

## Siehe auch

- `scripts/utilities/pool_restore.md` — Daten aus dem Pool wiederherstellen
- `docs/SETUP.md` §9 — Kurzanleitung GC
- `docs/DEVELOPER_GUIDE.md` — GC-Logik und `.gc_lock`
- `scripts/utilities/pool_verify_backup.py` — Integritätscheck vor/nach GC
