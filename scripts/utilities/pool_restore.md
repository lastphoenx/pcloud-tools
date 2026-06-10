# pool_restore.py – Pool-Mode Wiederherstellung

## Kurzbeschreibung

Stellt Dateien aus dem deduplizierten pCloud-Pool wieder her. Im Gegensatz zu `pcloud_restore.py` (Legacy 1to1 mit `anchor_path`) arbeitet dieses Tool mit dem **v2-Index** (`pool_refs`) und **Pool-Stubs** (`.meta.json`).

### Auflösungskette

```
Pfad + Snapshot  →  pool_refs[sha].snapshots[snap]  →  fileid  →  _pool/XX/sha256
Einzeldatei      →  Stub lesen  →  pool_fileid  →  Download
```

### Features

- Snapshot-Liste aus `pool_refs`
- Ganzer Snapshot oder `--filter` (Pfad-Präfix)
- Einzeldatei via `--relpath` (Stub-Weg)
- Paralleler Download kleiner Dateien (<50 MB, 16 Threads)
- SHA256-Verifikation (`--verify`)
- Resume: vorhandene Dateien mit korrektem SHA werden übersprungen
- Hardlink-Deduplizierung bei gleicher SHA im Snapshot

---

## Parameter

| Parameter | Beschreibung |
|-----------|--------------|
| `--env-file` | Pfad zur `.env` (required) |
| `--dest-root` | Remote Root, z.B. `/Backup/rtb_pool` (required) |
| `--list-snapshots` | Verfügbare Snapshots anzeigen |
| `--snapshot` | Snapshot-Name |
| `--relpath` | Einzelne Datei (relativer Pfad im Snapshot) |
| `--filter` | Nur Relpaths mit diesem Präfix |
| `--out-dir` | Lokales Zielverzeichnis (Snapshot wird als Unterordner angelegt) |
| `--download` | Download ausführen (ohne: nur Plan-Modus) |
| `--verify` | SHA256 nach Download prüfen |
| `--allow-incomplete` | Snapshot ohne `.upload_complete` erlauben |

### Umgebungsvariablen

- `PCLOUD_DOWNLOAD_THREADS` – parallele Downloads (Default: 16)
- `PCLOUD_DOWNLOAD_SMALL_THRESHOLD` – Grenze klein/groß in Bytes (Default: 50 MB)
- `MAIN_DIR` – Pfad zu `pcloud_bin_lib.py` (Default: `/opt/apps/pcloud-tools/main`)

---

## Beispiele

### Snapshots auflisten

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_restore.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --list-snapshots
```

### Plan-Modus (Vorschau, kein Download)

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --out-dir /tmp/restore
```

### Ganzer Snapshot mit Verifikation

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --out-dir /srv/restore \
  --download --verify
```

### Nur ein Ordner (Präfix-Filter)

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --filter "Gemeinsam/Rest/" \
  --out-dir /srv/restore \
  --download --verify
```

### Einzelne Datei (Stub-Weg)

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --relpath "Gemeinsam/Rest/dokument.pdf" \
  --out-dir /srv/restore \
  --download --verify
```

Ergebnis liegt unter: `/srv/restore/2026-05-28-120014/Gemeinsam/Rest/dokument.pdf`

---

## Voraussetzungen

1. **v2-Index mit Relpaths** – nach initialem Catch-up via `pool_rebuild_index_v2.py` (siehe SETUP.md §7)
2. **`.upload_complete`** – Snapshot muss vollständig hochgeladen sein (oder `--allow-incomplete`)
3. **Pool-Objekte** – physische Dateien unter `_pool/XX/sha256` müssen existieren

Integrität vorab prüfen:

```bash
python scripts/utilities/pool_verify_backup.py \
  --env-file .env --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests
```

---

## Unterschied zu pcloud_restore.py

| | `pcloud_restore.py` | `pool_restore.py` |
|---|---|---|
| Modus | Legacy 1to1 | Pool v2 |
| Index | `items` + `anchor_path` | `pool_refs` |
| Download-Quelle | `anchor_path` im Snapshot-Baum | `_pool/XX/sha256` via `fileid` |
| Snapshot-Liste | `holders` | `pool_refs[*].snapshots` |
