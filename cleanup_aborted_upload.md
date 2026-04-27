# cleanup_aborted_upload.sh

## Kurzbeschreibung
Räumt abgebrochene pCloud-Uploads auf und bereitet Neustart vor. Funktioniert für Full-Mode und Delta-Copy-Mode. Kann optional den geklonten Snapshot auf pCloud löschen.

## Parameter

- `--snapshot SNAPSHOT_NAME` (required): Snapshot-Name (z.B. `2026-04-15-120000`)
  - **Alternative:** Positional argument (backwards-kompatibel): `./cleanup_aborted_upload.sh 2026-04-15-120000`
- `--remote`: Löscht auch den pCloud-Snapshot (Delta-Copy-Clone)
- `--dry-run`: Testlauf ohne tatsächliche Änderungen

**Hinweis:** Reihenfolge der Parameter ist egal.

## Beispielaufrufe

### Nur lokaler Cleanup (Standard)
```bash
./cleanup_aborted_upload.sh --snapshot 2026-04-15-120000
```
Löscht lokale Manifeste, Deltas, Resume-States. pCloud-Snapshot bleibt erhalten (manuell prüfen via UI).

### Mit Remote-Löschung (Delta-Copy)
```bash
./cleanup_aborted_upload.sh --snapshot 2026-04-15-120000 --remote
```
Zusätzlich: Löscht geklonten Snapshot auf pCloud (bei abgebrochenem Delta-Copy-Upload).

### Dry-Run (Test ohne Änderungen)
```bash
./cleanup_aborted_upload.sh --snapshot 2026-04-15-120000 --dry-run
```
Zeigt was gelöscht würde, ohne tatsächlich Dateien zu löschen (sicher für Testing).

### Vollständiger Dry-Run mit Remote
```bash
./cleanup_aborted_upload.sh --snapshot 2026-04-15-120000 --remote --dry-run
```
Zeigt kompletten Cleanup inkl. pCloud-Löschung (ohne Ausführung).

### Backwards-kompatible Syntax (positional)
```bash
./cleanup_aborted_upload.sh 2026-04-15-120000 --remote
```
Funktioniert auch ohne `--snapshot` Flag (Legacy-Support).
