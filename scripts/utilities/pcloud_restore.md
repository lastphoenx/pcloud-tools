# pcloud_restore.py

## Kurzbeschreibung
Stellt pCloud-Snapshots wieder her (Notfall-Recovery-Tool). Lädt Snapshots von pCloud herunter mit automatischer SHA256-Verifikation und Deduplizierung über den Content-Index.

## Parameter

- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--snapshot` (required): Snapshot-Name (z.B. `2026-04-15-120000`)
- `--out-dir` (required): Lokales Zielverzeichnis für den Download
- `--dry-run`: Testlauf ohne tatsächlichen Download
- `--verify-only`: Nur SHA256-Verifikation (Download überspringen)
- `--skip-verification`: Download ohne SHA256-Check (schneller, unsicherer)

## Beispielaufrufe

### Vollständiger Snapshot-Download mit Verifikation
```bash
python scripts/utilities/pcloud_restore.py \
  --dest-root /Backup/rtb_1to1 \
  --snapshot 2026-04-15-120000 \
  --out-dir /tmp/restore/
```
Lädt kompletten Snapshot herunter, nutzt Content-Index für Deduplizierung, verifiziert SHA256-Hashes.

### Dry-Run (Test ohne Download)
```bash
python scripts/utilities/pcloud_restore.py \
  --dest-root /Backup/rtb_1to1 \
  --snapshot 2026-04-15-120000 \
  --out-dir /tmp/restore/ \
  --dry-run
```
Zeigt was heruntergeladen würde, ohne Dateien zu übertragen.

### Nur SHA256-Verifikation
```bash
python scripts/utilities/pcloud_restore.py \
  --dest-root /Backup/rtb_1to1 \
  --snapshot 2026-04-15-120000 \
  --out-dir /tmp/restore/ \
  --verify-only
```
Prüft Integrität bereits heruntergeladener Dateien gegen Content-Index.
