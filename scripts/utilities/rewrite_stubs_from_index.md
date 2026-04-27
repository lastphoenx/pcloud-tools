# rewrite_stubs_from_index.py

## Kurzbeschreibung
Regeneriert ALLE Stub-Dateien eines Snapshots aus dem Content-Index. Nutzt enriched Index mit FileIDs als Authoritative Source.

## Parameter

- `--index-path` (required): Pfad zum enriched Index (lokal oder remote pCloud-Pfad)
- `--dest-root` (required): pCloud-Root-Pfad (z.B. `/Backup/rtb_1to1`)
- `--snapshot`: Spezifischer Snapshot (optional, sonst alle Holder im Index)
- `--batch-size`: Upload-Batch-Größe (default: 100)
- `--dry-run`: Testlauf ohne Upload

## Beispielaufrufe

### Alle Stubs neu schreiben
```bash
python scripts/utilities/rewrite_stubs_from_index.py \
  --index-path /Backup/rtb_1to1/_snapshots/_index/content_index.json \
  --dest-root /Backup/rtb_1to1
```
Lädt Index von pCloud, regeneriert ALLE Stub-Dateien, lädt sie hoch (Batch-Upload).

### Nur ein Snapshot
```bash
python scripts/utilities/rewrite_stubs_from_index.py \
  --index-path /srv/pcloud-archive/content_index.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot 2026-04-15-120000
```
Regeneriert nur Stubs für spezifischen Snapshot aus lokalem Index.

### Dry-Run mit kleiner Batch-Size
```bash
python scripts/utilities/rewrite_stubs_from_index.py \
  --index-path /Backup/rtb_1to1/_snapshots/_index/content_index.json \
  --dest-root /Backup/rtb_1to1 \
  --batch-size 50 \
  --dry-run
```
Zeigt was regeneriert würde, kleinere Batches für Testing.
