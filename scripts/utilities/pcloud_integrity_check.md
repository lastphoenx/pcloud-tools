# pcloud_integrity_check.py

## Kurzbeschreibung
Vollständiger Integritäts-Check für pCloud-Backups. Prüft Konsistenz zwischen Index, Stubs, Anchors, FileIDs und SHA256-Hashes auf 7 Ebenen.

## Parameter

- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--checks`: Komma-separierte Liste (z.B. `anchors,checksums,holders`)
  - `anchors`: Index → pCloud Anchors & FileIDs
  - `checksums`: SHA256-Stichprobe
  - `holders`: Holder → Snapshot-Existenz (Waisen)
  - `stubs-index`: Stubs → Index-Verweise
  - `stubs-pcloud`: Stubs → pCloud-Anchors
  - `timeline`: Anchor-Zeitlinien-Konsistenz
  - `stubs-combined`: Kombinierter Stub-Check (1 Pass)
- `--sample-size`: Anzahl Items für Checksum-Stichprobe (default: 50)
- `--stubs-mode`: Stub-Check-Modus: `separate` | `combined` | `both`
- `--out-json`: JSON-Report ausgeben (Pfad)

## Beispielaufrufe

### Vollständiger Check (alle Ebenen)
```bash
python scripts/utilities/pcloud_integrity_check.py \
  --dest-root /Backup/rtb_1to1
```
Führt alle 7 Checks durch, zeigt Zusammenfassung und Details.

### Nur Anchor-Check (schnell)
```bash
python scripts/utilities/pcloud_integrity_check.py \
  --dest-root /Backup/rtb_1to1 \
  --checks anchors
```
Prüft nur ob alle Index-Anchors auf pCloud existieren und FileIDs stimmen.

### Checksum-Stichprobe (200 Files)
```bash
python scripts/utilities/pcloud_integrity_check.py \
  --dest-root /Backup/rtb_1to1 \
  --checks checksums \
  --sample-size 200
```
Verifiziert SHA256-Hashes von 200 zufälligen Dateien gegen pCloud.

### Mit JSON-Report
```bash
python scripts/utilities/pcloud_integrity_check.py \
  --dest-root /Backup/rtb_1to1 \
  --out-json /tmp/integrity_report.json
```
Speichert detaillierten Report als JSON (für Automatisierung/Monitoring).
