# pool_restore.py – Pool-Mode Wiederherstellung

> Stand: Juni 2026 · Skript: `scripts/utilities/pool_restore.py`

## Zweck

Stellt Dateien aus dem **deduplizierten pCloud-Pool** wieder her. Im Pool-Mode liegen echte Dateiinhalte nur einmal unter `_pool/XX/<sha256>`. Die Snapshot-Ordner unter `_snapshots/<snap>/` enthalten ausschließlich **Stubs** (`.meta.json`) — kleine JSON-Verweise, keine Videos oder Fotos.

`pool_restore.py` löst Pfade aus dem v2-Index (`pool_refs`) oder einzelnen Stubs auf und lädt den echten Inhalt aus dem Pool herunter.

---

## Architektur: Wo liegt was?

```
pCloud (Quelle — Parameter --pool-root)          Lokal (Ziel — Parameter --out-dir)
────────────────────────────────────────         ─────────────────────────────────
/Backup/rtb_pool/                                /srv/nas/restore/
  _pool/                                           2026-06-10-040013/
    f1/f153611a...          ← echter Inhalt              Gemeinsam/.../video.mp4
  _snapshots/
    _index/
      content_index.json    ← v2-Index (pool_refs)
    2026-06-10-040013/
      Gemeinsam/.../video.mp4.meta.json  ← Stub (Verweis)
```

### Auflösungskette

**Bulk (Snapshot / Filter):**

```
--snapshot + --filter  →  pool_refs[sha].snapshots[snap]  →  fileid  →  _pool/XX/sha256
```

**Einzeldatei:**

```
--relpath  →  Stub lesen (.meta.json)  →  pool_fileid  →  Download
```

---

## Parameter

### Pflicht / Quelle & Ziel

| Parameter | Beschreibung |
|-----------|--------------|
| `--env-file` | Pfad zur `.env` mit pCloud-Credentials (required) |
| `--pool-root` | **Quelle:** Remote Pool-Root auf pCloud, z.B. `/Backup/rtb_pool` |
| `--dest-root` | **(deprecated)** Alias für `--pool-root` — weiterhin akzeptiert, gibt Warnung aus |
| `--out-dir` | **Ziel:** Lokales Verzeichnis, in das restored wird |

> **Hinweis:** `--pool-root` bezeichnet die Backup-Quelle auf pCloud, **nicht** das lokale Ziel. Das lokale Ziel ist immer `--out-dir`.

### Restore-Steuerung

| Parameter | Beschreibung |
|-----------|--------------|
| `--list-snapshots` | Alle Snapshots aus `pool_refs` auflisten und beenden |
| `--snapshot` | Snapshot-Name, z.B. `2026-06-10-040013` |
| `--filter` | Nur Relpaths mit diesem **Präfix** (Ordner-Restore) |
| `--relpath` | Einzelne Datei (relativer Pfad im Snapshot, Stub-Weg) |
| `--download` | Download ausführen (ohne: nur Plan-Modus) |
| `--verify` | SHA256 nach jedem Download prüfen |
| `--allow-incomplete` | Snapshot ohne `.upload_complete` trotzdem erlauben |

### Umgebungsvariablen

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `MAIN_DIR` | `/opt/apps/pcloud-tools/main` | Pfad zu `pcloud_bin_lib.py` |
| `PCLOUD_DOWNLOAD_THREADS` | `16` | Parallele Downloads für Dateien < 50 MB |
| `PCLOUD_DOWNLOAD_SMALL_THRESHOLD` | `52428800` (50 MB) | Grenze parallel vs. sequentiell |

---

## Ausgabepfad

Dateien landen **immer** unter:

```
<out-dir>/<snapshot>/<relpath>
```

**Beispiel:**

```bash
--out-dir /srv/nas/restore \
--snapshot 2026-06-10-040013 \
--filter "Gemeinsam/Playmobil_Youtube/2026_05_25_Short_Nea_übt_mit_Leona_Airtrack/"
```

Ergebnis:

```
/srv/nas/restore/2026-06-10-040013/Gemeinsam/Playmobil_Youtube/2026_05_25_Short_Nea_übt_mit_Leona_Airtrack/
  Short-Nea-übt-mit-Leona-Airtrack.mp4
  20260523_122649.jpg
  ...
```

---

## Beispiele

### Snapshots auflisten

```bash
cd /opt/apps/pcloud-tools/main

MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_restore.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --list-snapshots
```

### Plan-Modus (Vorschau, kein Download)

Zeigt Dateiliste, Größen und SHA-Präfixe — ideal vor dem ersten echten Restore:

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_restore.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --snapshot 2026-06-10-040013 \
  --filter "Gemeinsam/Playmobil_Youtube/2026_05_25_Short_Nea_übt_mit_Leona_Airtrack/" \
  --out-dir /srv/nas/restore
```

### Ordner wiederherstellen (mit Verifikation)

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_restore.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --snapshot 2026-06-10-040013 \
  --filter "Gemeinsam/Playmobil_Youtube/2026_05_25_Short_Nea_übt_mit_Leona_Airtrack/" \
  --out-dir /srv/nas/restore \
  --download --verify
```

### Einzelne Datei (Stub-Weg)

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --snapshot 2026-06-10-040013 \
  --relpath "Gemeinsam/Rest/dokument.pdf" \
  --out-dir /srv/nas/restore \
  --download --verify
```

### Ganzer Snapshot

```bash
python scripts/utilities/pool_restore.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --snapshot 2026-06-10-040013 \
  --out-dir /srv/nas/restore \
  --download --verify
```

---

## Verhalten im Detail

### Plan-Modus vs. Download

| Modus | Flag | Verhalten |
|-------|------|-----------|
| Plan | *(kein `--download`)* | Listet Items, keine API-Downloads |
| Download | `--download` | Lädt Dateien nach `<out-dir>/<snapshot>/` |

### Download-Strategie

1. **Primär:** Download via `fileid` aus `pool_refs` (schnell, zuverlässig)
2. **Fallback:** Wenn `fileid` fehlt → `stat_file` auf `_pool/XX/sha256` → Download via Pfad
3. **Parallel:** Dateien < 50 MB mit bis zu 16 Threads gleichzeitig
4. **Sequentiell:** Dateien ≥ 50 MB einzeln (RAM-schonend)
5. **Resume:** Bereits vorhandene Dateien mit korrektem SHA256 werden übersprungen (`--verify`)
6. **Dedup:** Gleiche SHA im selben Lauf → Hardlink statt erneutem Download

### `.upload_complete`-Prüfung

Vor dem Restore wird geprüft, ob der Snapshot vollständig hochgeladen wurde (Marker `.upload_complete` auf pCloud). Unvollständige Snapshots werden abgelehnt — außer mit `--allow-incomplete`.

### AppleDouble-Dateien (`._*`)

macOS legt beim Kopieren auf SMB/NFS/exFAT oft Begleitdateien `._dateiname` an (4 KB Metadaten, **kein** Duplikat des Originals). Wenn diese im Backup sind, erscheinen sie auch im Restore-Plan. Auf Linux meist unnötig:

```bash
find /srv/nas/restore -name '._*' -delete
```

---

## Voraussetzungen

1. **v2-Index mit Relpaths** — nach initialem Catch-up via `pool_rebuild_index_v2.py` (SETUP.md §7)
2. **Pool-Objekte vorhanden** — physische Dateien unter `_pool/XX/sha256`
3. **Gültige `.env`** — pCloud-Token mit Lesezugriff

### Integrität vorab prüfen

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_verify_backup.py \
  --env-file .env \
  --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests
```

---

## Exit-Codes

| Code | Bedeutung |
|------|-----------|
| `0` | Erfolg (Plan oder Download ohne Fehler) |
| `1` | Download-Fehler (mindestens eine Datei fehlgeschlagen) |
| `2` | Konfigurations-/Nutzungsfehler (fehlende Parameter, Index nicht lesbar) |

---

## Troubleshooting

### `Index ist kein v2 Pool-Index`

→ `pool_rebuild_index_v2.py --upload` ausführen (SETUP.md §7).

### `Snapshot hat kein .upload_complete`

→ Upload noch nicht abgeschlossen, oder Snapshot abgebrochen. Mit `--allow-incomplete` auf eigenes Risiko fortfahren.

### `Keine Dateien fuer Snapshot`

→ `--filter` prüfen (Präfix muss exakt zum Relpath im Index passen, ohne führenden Slash). Plan mit `--list-snapshots` und ohne Filter testen.

### Download schlägt fehl, SHA-Mismatch

→ Pool-Objekt beschädigt oder Index veraltet. `pool_verify_backup.py --stub-sample 50` ausführen.

### `--dest-root` Warnung

→ Auf `--pool-root` umstellen. `--dest-root` bleibt aus Kompatibilitätsgründen vorerst verfügbar.

---

## Unterschied zu `pcloud_restore.py`

| | `pcloud_restore.py` | `pool_restore.py` |
|---|---|---|
| Modus | Legacy 1to1 | Pool v2 |
| Index | `items` + `anchor_path` | `pool_refs` |
| Download-Quelle | Echte Datei im Snapshot-Baum | `_pool/XX/sha256` via `fileid` |
| Remote-Parameter | `--dest-root` | `--pool-root` (empfohlen) |
| Snapshot-Liste | `holders` | `pool_refs[*].snapshots` |

---

## Siehe auch

- `pcloud_pool_gc.md` — Pool Garbage Collection
- `docs/SETUP.md` §10 — Kurzanleitung Restore
- `docs/DEVELOPER_GUIDE.md` — Datenstrukturen (Stubs, pool_refs)
