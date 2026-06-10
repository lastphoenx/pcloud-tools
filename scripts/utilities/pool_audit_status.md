# pool_audit_status.py — Backup-Status auf einen Blick

Schneller Triage-Report ohne rekursives `find`. Vergleicht drei direkte Listen:

| Quelle | Pfad (pi-nas) |
|--------|----------------|
| RTB-Snapshots | `/mnt/backup/rtb_nas/<snap>/` |
| Archiv-Manifeste | `/srv/pcloud-archive/manifests/<snap>.json` |
| pCloud complete | `--pool-root` + `.upload_complete` |

Pfade kommen aus `.env` (`PCLOUD_ARCHIVE_DIR`, `RTB`). **Nicht** `/srv/nas/pcloud-archive` — siehe `docs/STORAGE_PATHS.md`.

## Aufruf (pi-nas)

```bash
cd /opt/apps/pcloud-tools/main

MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_audit_status.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool
```

Das Skript zeigt am Anfang `Storage:`-Zeilen (`df`) — erwartet `/dev/sdd1` für Archiv/Temp, nicht `mmcblk0`.

## Spalten in der Matrix

| Spalte | Bedeutung |
|--------|-----------|
| RTB | Snapshot-Ordner lokal unter RTB |
| Man | Archiv-Manifest unter `manifests/<snap>.json` |
| Rdy | `.upload_complete` auf pCloud |

Nur **Abweichungen** werden einzeln gelistet.

## Typische Befunde

| Befund | Bedeutung | Aktion |
|--------|-----------|--------|
| RTB ja, Rdy nein | Catch-up nötig | `./wrapper_pcloud_pool_sync_1to1.sh <snap>` |
| Man ja, Rdy nein | Anomalie (selten) | Log prüfen, Re-Upload |
| Rdy ja, Man nein | Upload OK, Manifest fehlt/defekt | Manifest regenerieren oder leere Datei löschen |
| Rdy ja, RTB nein | Lokal per Retention gelöscht | Normal, Backup auf pCloud bleibt |
| DB FAILED → COMPLETE | Alter Fehler, inzwischen nachgeholt | Keine Aktion |

## Danach: Vollständiger Integritätscheck

```bash
python scripts/utilities/pool_verify_backup.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests \
  --stub-sample 50
```

## Defekte Manifeste finden

```bash
for f in /srv/pcloud-archive/manifests/*.json; do
  [ -s "$f" ] || echo "LEER: $f"
done
```

## Siehe auch

- `docs/STORAGE_PATHS.md` — Bind-Mounts, mergerfs, was **nicht** Pipeline-Pfad ist
