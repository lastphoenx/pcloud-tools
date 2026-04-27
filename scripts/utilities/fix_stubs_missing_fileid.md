# fix_stubs_missing_fileid.py

## Kurzbeschreibung
Repariert Stub-Dateien (.meta.json) und den Content-Index nach API-Fehlern. Fetcht fehlende FileIDs via pCloud-API und schreibt betroffene Stubs neu.

## Parameter

- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--dry-run`: Testlauf ohne Änderungen
- `--verbose`: Detaillierte Logging-Ausgabe
- `--rewrite-all`: Rewrite-All-Modus (Index OK, aber Stubs kaputt)
- `--check-index N`: Prüft N zufällige Items im Index

## Beispielaufrufe

### Standard-Reparatur (fehlende FileIDs im Index)
```bash
python scripts/utilities/fix_stubs_missing_fileid.py \
  --dest-root /Backup/rtb_1to1 \
  --dry-run
```
Findet Items ohne FileID, fetcht sie via stat_file(), aktualisiert Index und Stubs.

### Rewrite-All-Modus (Index OK, Stubs kaputt)
```bash
python scripts/utilities/fix_stubs_missing_fileid.py \
  --dest-root /Backup/rtb_1to1 \
  --rewrite-all \
  --verbose
```
Nutzen wenn Content-Index FileIDs hat, aber Stub-Dateien korrupt sind. Schreibt ALLE Stubs neu.

### Index-Check (Stichprobe)
```bash
python scripts/utilities/fix_stubs_missing_fileid.py \
  --dest-root /Backup/rtb_1to1 \
  --check-index 20
```
Prüft 20 zufällige Index-Items auf Konsistenz (FileID-Validierung).
