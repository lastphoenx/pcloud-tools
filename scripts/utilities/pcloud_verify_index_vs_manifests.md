# pcloud_verify_index_vs_manifests.py

## Kurzbeschreibung
Verifiziert Remote-Index gegen lokale Manifest-JSONs. Prüft Konsistenz zwischen pCloud Content-Index und Ground-Truth-Manifesten vom Backup-Source.

## Parameter

- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--manifest-dir` (required): Lokales Manifest-Verzeichnis (z.B. `/srv/pcloud-archive/manifests`)
- `--verbose`: Detaillierte Ausgabe
- `--out-json`: JSON-Report speichern

## Beispielaufrufe

### Vollständige Index-Validierung
```bash
python scripts/utilities/pcloud_verify_index_vs_manifests.py \
  --dest-root /Backup/rtb_1to1 \
  --manifest-dir /srv/pcloud-archive/manifests
```
Gleicht Remote-Index mit allen lokalen Manifesten ab, zeigt Inkonsistenzen.

### Mit JSON-Report
```bash
python scripts/utilities/pcloud_verify_index_vs_manifests.py \
  --dest-root /Backup/rtb_1to1 \
  --manifest-dir /srv/pcloud-archive/manifests \
  --out-json /tmp/index_validation.json \
  --verbose
```
Detaillierte Validierung mit JSON-Output für Monitoring/Automatisierung.

### Nach Index-Rekonstruktion
```bash
python scripts/utilities/pcloud_verify_index_vs_manifests.py \
  --dest-root /Backup/rtb_1to1 \
  --manifest-dir /srv/pcloud-archive/manifests
```
Prüft ob rekonstruierter Index vollständig und korrekt ist.
