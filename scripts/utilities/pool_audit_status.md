# pool_audit_status.py — Backup-Status auf einen Blick

Schneller Triage-Report ohne rekursives `find`. Vergleicht drei direkte Listen:

| Quelle | Pfad (pi-nas) |
|--------|----------------|
| RTB-Snapshots | `/mnt/backup/rtb_nas/<snap>/` |
| Archiv-Manifeste | `/srv/pcloud-archive/manifests/<snap>.json` |
| pCloud-Ordner | `listfolder` auf `--pool-root/_snapshots/<snap>/` |
| pCloud complete | `stat` auf `…/<snap>/.upload_complete` |

MariaDB und `content_index.json` werden **nicht** für die Matrix gelesen (DB nur am Ende als Historie).

Pfade kommen aus `.env` (`PCLOUD_ARCHIVE_DIR`, `RTB`). **Nicht** `/srv/nas/pcloud-archive` — siehe `docs/STORAGE_PATHS.md`.

## Aufruf (pi-nas)

```bash
cd /opt/apps/pcloud-tools/main

MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_audit_status.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --rtb-root /mnt/backup/rtb_nas
```

Das Skript zeigt am Anfang `Storage:`-Zeilen (`df`) — erwartet `/dev/sdd1` für Archiv/Temp, nicht `mmcblk0`.

## Spalten in der Matrix

| Spalte | Bedeutung |
|--------|-----------|
| RTB | Snapshot-Ordner lokal unter RTB |
| Man | Archiv-Manifest unter `manifests/<snap>.json` |
| Pcl | Snapshot-Ordner existiert auf pCloud (`_snapshots/<snap>/`) |
| Cmp | `.upload_complete` gesetzt (Upload validiert & fertig) |

Nur **Abweichungen** werden einzeln gelistet; Zeile **Hinweis** erklärt die Aktion.

Vollständiger Triage-Workflow: [integrity-checks.md](../../../doku/Raspi/raspinas/ops/integrity-checks.md) (pi-nas Ops-Doku).

## Typische Befunde

| Befund | Bedeutung | Aktion |
|--------|-----------|--------|
| RTB ja, Pcl nein, Cmp nein | Noch nie hochgeladen | Catch-up / Wrapper |
| RTB ja, Pcl ja, Cmp nein | Unvollständiger Remote-Snapshot | Upload erneut (Pipeline wipet & neu) |
| RTB nein, Pcl ja, Cmp nein | Zombie (hängender Remote-Ordner) | `pcloud_pool_gc.py --delete-snapshots SNAP` (gezielt), **nicht** blind `--retention-apply` bei Zeit-Retention |
| Cmp ja, Man nein | Upload OK, Manifest fehlt lokal | Re-Upload oder Manifest archivieren |
| Cmp ja, RTB nein | Lokal per RTB-Retention gelöscht | Normal, Backup auf pCloud bleibt |
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
