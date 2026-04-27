# pcloud_repair_index.py

## Kurzbeschreibung
Repariert den Content-Index nach Delta-Check. Entfernt Phantom-Einträge (Holder ohne reale Datei) basierend auf Delta-Report von pcloud_quick_delta.py.

## Parameter

- `--delta-report` (required): Pfad zum Delta-Report JSON (von pcloud_quick_delta.py)
- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--out-index`: Lokaler Pfad für bereinigten Index (default: /srv/pcloud-temp/pcloud_index_{snapshot}.json)
- `--dry-run`: Testlauf ohne Änderungen

## Beispielaufrufe

### Index-Reparatur nach Delta-Check
```bash
python scripts/utilities/pcloud_repair_index.py \
  --delta-report /tmp/delta_report.json \
  --dest-root /Backup/rtb_1to1
```
Lädt Remote-Index, entfernt Phantom-Einträge aus Delta-Report, speichert bereinigten Index lokal.

### Dry-Run (Test)
```bash
python scripts/utilities/pcloud_repair_index.py \
  --delta-report /tmp/delta_report.json \
  --dest-root /Backup/rtb_1to1 \
  --dry-run
```
Zeigt welche Einträge entfernt würden, ohne Index zu ändern.

### Custom Output-Pfad
```bash
python scripts/utilities/pcloud_repair_index.py \
  --delta-report /tmp/delta_report.json \
  --dest-root /Backup/rtb_1to1 \
  --out-index /backup/repaired_index.json
```
Speichert bereinigten Index am spezifischen Pfad (für manuelle Validierung).
