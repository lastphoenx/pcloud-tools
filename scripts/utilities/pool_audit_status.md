# pool_audit_status.py — Backup-Status auf einen Blick

Schneller Triage-Report ohne rekursives `find`. Vergleicht drei direkte Listen:

| Quelle | Pfad |
|--------|------|
| RTB-Snapshots | `--rtb-root` (Default: `/mnt/backup/rtb_nas`) |
| Archiv-Manifeste | `--manifests-dir` (Default: `$PCLOUD_ARCHIVE_DIR/manifests`) |
| pCloud complete | `--pool-root` + `.upload_complete` |

Optional: MariaDB `backup_runs` (letzter Lauf = FAILED pro Snapshot).

## Aufruf (pi-nas)

```bash
cd /opt/apps/pcloud-tools/main

PCLOUD_ARCHIVE_DIR=/srv/nas/pcloud-archive \
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_audit_status.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool
```

## Spalten in der Matrix

| Spalte | Bedeutung |
|--------|-----------|
| RTB | Snapshot-Ordner lokal unter `/mnt/backup/rtb_nas/` |
| Man | Archiv-Manifest unter `manifests/<snap>.json` |
| Rdy | `.upload_complete` auf pCloud |

Nur **Abweichungen** werden einzeln gelistet.

## Typische Befunde

| Befund | Bedeutung | Aktion |
|--------|-----------|--------|
| RTB ja, Rdy nein | Catch-up nötig | `./wrapper_pcloud_pool_sync_1to1.sh <snap>` |
| Man ja, Rdy nein | Anomalie (selten) | Log prüfen, Re-Upload |
| Rdy ja, Man nein | Upload OK, Manifest fehlt/ defekt | Manifest regenerieren oder leere Datei löschen |
| Rdy ja, RTB nein | Lokal per Retention gelöscht | Normal, Backup auf pCloud bleibt |
| DB FAILED → COMPLETE | Alter Fehler, inzwischen nachgeholt | Keine Aktion |

## Danach: Vollständiger Integritätscheck

```bash
PCLOUD_ARCHIVE_DIR=/srv/nas/pcloud-archive \
python scripts/utilities/pool_verify_backup.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --manifests-dir /srv/nas/pcloud-archive/manifests \
  --stub-sample 50
```

## Defekte Manifeste finden

```bash
for f in /srv/nas/pcloud-archive/manifests/*.json; do
  [ -s "$f" ] || echo "LEER: $f"
done
```
