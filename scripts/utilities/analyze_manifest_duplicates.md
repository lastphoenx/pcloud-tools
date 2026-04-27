# analyze_manifest_duplicates.py

## Kurzbeschreibung
Analysiert JSON-Manifeste auf SHA256-Duplikate und erstellt Excel-Reports. Identifiziert doppelte Dateien im Backup (Hardlinks, Kopien).

## Parameter

- `--manifest` (required): Pfad zum JSON-Manifest
- `--output` (required): Pfad zur Excel-Ausgabedatei (.xlsx)
- `--min-duplicates`: Minimale Duplikat-Anzahl für Aufnahme (default: 2)
- `--sort-by`: Sortierung: `count` | `size` | `space_saved` (default: space_saved)

## Beispielaufrufe

### Standard-Analyse
```bash
python scripts/utilities/analyze_manifest_duplicates.py \
  --manifest /srv/pcloud-archive/manifests/2026-04-15-120000.json \
  --output /tmp/duplicates.xlsx
```
Erstellt Excel mit allen Duplikaten (≥2 Vorkommen), sortiert nach eingesparter Space.

### Nur große Duplikate (≥5 Kopien)
```bash
python scripts/utilities/analyze_manifest_duplicates.py \
  --manifest /srv/pcloud-archive/manifests/2026-04-15-120000.json \
  --output /tmp/major_duplicates.xlsx \
  --min-duplicates 5
```
Zeigt nur Dateien mit 5+ Duplikaten (z.B. Log-Dateien, Cache).

### Nach Häufigkeit sortiert
```bash
python scripts/utilities/analyze_manifest_duplicates.py \
  --manifest /srv/pcloud-archive/manifests/2026-04-15-120000.json \
  --output /tmp/duplicates_by_count.xlsx \
  --sort-by count
```
Sortiert nach Anzahl Duplikate (statt Space-Savings).
