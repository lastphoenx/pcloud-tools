# pCloud Backup Tools - Setup Guide (Pool-Mode)

> **Zielgruppe:** Ersteinrichtung auf Raspberry Pi / Debian-NAS mit bestehenden RTB-Snapshots.
> **Modus:** Pool-only (`wrapper_pcloud_pool_sync_1to1.sh`)
> **Stand:** Juni 2026

---

## Voraussetzungen

- Debian/Ubuntu Linux (Raspberry Pi OS 12+)
- Python 3.9+ mit venv
- Bash 4.0+
- RTB-Snapshots in `/mnt/backup/rtb_nas/`
- pCloud-Account (EU-Region: `eapi.pcloud.com`)
- pCloud OAuth2-Token → [Token erneuern via rclone](./RCLONE_TOKEN_REFRESH.md)

---

## 1. Abhängigkeiten installieren

```bash
sudo apt update && sudo apt install -y \
  mariadb-server mariadb-client \
  python3 python3-pip python3-venv \
  curl jq uuid-runtime git

# Python venv erstellen
cd /opt/apps/pcloud-tools/main
python3 -m venv /opt/apps/pcloud-tools/venv
source /opt/apps/pcloud-tools/venv/bin/activate
pip install requests
```

---

## 2. MariaDB einrichten

```bash
sudo mysql_secure_installation  # Root-Passwort setzen, Anonymous Users entfernen

sudo mysql -e "
CREATE DATABASE IF NOT EXISTS pcloud_backup CHARACTER SET utf8mb4;
CREATE USER 'pcloud_backup'@'localhost' IDENTIFIED BY 'STRONG_PASSWORD';
GRANT ALL ON pcloud_backup.* TO 'pcloud_backup'@'localhost';
FLUSH PRIVILEGES;
"

# Schema initialisieren
sudo mysql pcloud_backup < /opt/apps/pcloud-tools/main/sql/init_pcloud_db.sql

# Prüfen
sudo mysql pcloud_backup -e "SHOW TABLES;"
# Erwartet: backup_runs, backup_phases, gap_backfills + Views
```

---

## 3. .env konfigurieren

```bash
cp /opt/apps/pcloud-tools/main/.env.example /opt/apps/pcloud-tools/main/.env
nano /opt/apps/pcloud-tools/main/.env
```

**Minimale Konfiguration:**

```bash
# pCloud API (EU-Region)
PCLOUD_TOKEN=dein_oauth2_token_hier
PCLOUD_HOST=eapi.pcloud.com

# Remote Pool-Root auf pCloud
PCLOUD_DEST=/Backup/rtb_pool

# Lokale Pfade
PCLOUD_TEMP_DIR=/srv/pcloud-temp
PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive
RTB=/mnt/backup/rtb_nas

# MariaDB
PCLOUD_DB_HOST=localhost
PCLOUD_DB_NAME=pcloud_backup
PCLOUD_DB_USER=pcloud_backup
PCLOUD_DB_PASS=STRONG_PASSWORD
PCLOUD_ENABLE_DB=1
```

```bash
# Verzeichnisse anlegen (auf SSD2, dann Bind-Mount — pi-nas Ist-Zustand)
sudo mkdir -p /mnt/ssd2/pcloud-archive/{manifests,indexes,deltas,resume}
sudo mkdir -p /mnt/ssd2/pcloud-temp
sudo mkdir -p /srv/pcloud-archive /srv/pcloud-temp
# fstab-Einträge (Beispiel pi-nas, /dev/sdd1 = SSD2):
# /mnt/ssd2/pcloud-archive  /srv/pcloud-archive  none  bind  0  0
# /mnt/ssd2/pcloud-temp     /srv/pcloud-temp     none  bind  0  0
sudo mount -a
sudo chown -R $USER:$USER /mnt/ssd2/pcloud-archive /mnt/ssd2/pcloud-temp /var/log/backup

# Verifizieren: Pipeline-Pfade auf SSD, nicht Micro-SD
df -h /srv/pcloud-archive /srv/pcloud-temp /
```

Ausführlich: `docs/STORAGE_PATHS.md` (pi-nas verifiziert). **Nicht** `/srv/nas/pcloud-archive` für die Pipeline verwenden.

---

## 4. Script-Berechtigungen setzen

```bash
cd /opt/apps/pcloud-tools/main
chmod +x wrapper_pcloud_pool_sync_1to1.sh pcloud_pool_gc.py
chmod +x scripts/utilities/*.py
```

---

## 5. Erster Lauf: Initialer Pool-Upload (Bootstrap)

Beim ersten Mal gibt es noch keine Remote-Snapshots. Der erste Upload läuft im Full-Pool-Mode:

```bash
# .env laden
export ENV_FILE=/opt/apps/pcloud-tools/main/.env

# Syntax-Check vor erstem Lauf
bash -n wrapper_pcloud_pool_sync_1to1.sh && echo "OK"
python scripts/utilities/check_undefined_names.py \
  pcloud_push_json_pool_manifest_to_pcloud.py && echo "OK"

# Dry-Run des ältesten Snapshots (read-only, kein Upload)
./wrapper_pcloud_pool_sync_1to1.sh 2026-04-27-173201 --dry-run

# Produktionslauf: ältesten Snapshot zuerst
./wrapper_pcloud_pool_sync_1to1.sh 2026-04-27-173201
```

Erwarteter Ablauf:
1. Manifest erzeugt (Full-Hash aller Dateien, dauert je nach Datenmenge)
2. Pool-Preflight: `listfolder(_pool)` → 0 SHA256s (Pool leer)
3. Alle Dateien hochladen (~45 MB/s je nach Bandbreite)
4. 19808 Stubs schreiben
5. Post-Upload-Validation: Pool-SHA-Check + Stub-100%-Check
6. `.upload_complete` gesetzt

---

## 6. Catch-up: alle weiteren Snapshots hochladen

```bash
# Alle fehlenden Snapshots automatisch der Reihe nach hochladen:
./wrapper_pcloud_pool_sync_1to1.sh

# Der Wrapper ermittelt: remote vorhanden vs. lokal vorhanden → Differenz
# Lädt chronologisch hoch; Scout wählt für jeden besten Basis-Snapshot
# Scout ≥ 70% Similarity → Turbo-Delta (~2 Min/Snapshot)
# Scout < 70% → Full-Pool-Mode (bei neu hinzugekommenen Geräten)
```

Fortschritt beobachten:
```bash
tail -f /var/log/backup/rtb_wrapper.log
```

---

## 7. v2-Index aufbauen (nach initialem Catch-up)

Nach dem initialen Catch-up enthält `pool_refs` nur SHA256-Keys. Der **v2-Index mit Relpaths** (für Restore-by-Pfad) wird via Rebuild aufgebaut:

```bash
# 1. Fehlende Delta-Manifeste regenerieren (falls nicht archiviert)
RTB=/mnt/backup/rtb_nas; MD=/srv/pcloud-archive/manifests
REF="$MD/<ältester_snap>.json"
for s in <snap1> <snap2> ...; do
  python pcloud_json_pool_manifest.py \
    --root "$RTB/$s" --snapshot "$s" \
    --out "$MD/$s.json" --hash sha256 --ref-manifest "$REF"
done

# 2. v2-Index lokal aufbauen (Read-only, zur Inspektion)
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_rebuild_index_v2.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --out /srv/pcloud-temp/content_index_v2.json

# Stichprobe prüfen:
python -c "
import json
d=json.load(open('/srv/pcloud-temp/content_index_v2.json'))
k=next(iter(d['pool_refs']))
print(k[:16], json.dumps(d['pool_refs'][k]['snapshots'], indent=2)[:200])
"

# 3. v2-Index auf pCloud schreiben (nach Prüfung)
# → --upload hält Backup des alten Index unter _index/archive/
python scripts/utilities/pool_rebuild_index_v2.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --out /srv/pcloud-temp/content_index_v2.json \
  --upload
```

---

## 8. Integrität verifizieren

```bash
# Vollständiger Check (Manifest→Pool + Manifest→Stubs, ~5s):
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_verify_backup.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests

# Mit Stub-Inhalt-Probe (sha256 + fileid gegengeprüft):
... --stub-sample 100

# Erwartete Ausgabe:
# ✓ ALLE CHECKS OK — Backup vollstaendig integr (5.1s)
```

---

## 9. Pool Garbage Collection

Der Pool wächst durch Deduplizierung — verwaiste SHA256-Objekte (nicht mehr im Index referenziert) werden periodisch gelöscht.

**Wann ausführen:** nach Retention-Löschungen, wöchentlich per Cron — **nicht** während laufender Backups (`.gc_lock` schützt davor).

```bash
# Dry-Run: zeigt Kandidaten ohne zu löschen
python pcloud_pool_gc.py \
  --env-file "$ENV_FILE" \
  --pool-root /Backup/rtb_pool \
  --dry-run --verbose

# Produktions-GC (Grace Period 24h, Index-basiert)
python pcloud_pool_gc.py \
  --env-file "$ENV_FILE" \
  --pool-root /Backup/rtb_pool \
  --grace-hours 24

# Wöchentlich per Cron (Sonntag 03:00):
# 0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py \
#   --env-file .env --pool-root /Backup/rtb_pool >> /var/log/backup/pool_gc.log 2>&1
```

Ausführliche Doku: `pcloud_pool_gc.md`

GC-Kandidaten vorab anzeigen (ohne Löschen):

```bash
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_verify_backup.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests
# → Zeile "gc_candidates" = Pool-Objekte ohne Manifest-Referenz
```

---

## 10. Daten wiederherstellen (Pool Restore)

Dateien aus dem Pool wiederherstellen — per Snapshot, Pfad-Filter oder Einzeldatei.

**Voraussetzung:** v2-Index mit Relpaths (§7) und `.upload_complete` am Snapshot.

```bash
# Verfügbare Snapshots
MAIN_DIR=/opt/apps/pcloud-tools/main \
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --list-snapshots

# Plan-Modus (Vorschau, kein Download)
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --out-dir /srv/restore

# Ganzer Snapshot mit SHA256-Verifikation
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --out-dir /srv/restore \
  --download --verify

# Nur ein Ordner
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --filter "Gemeinsam/Playmobil_Youtube/mein_ordner/" \
  --out-dir /srv/restore \
  --download --verify

# Einzelne Datei (Stub-Weg)
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --snapshot 2026-05-28-120014 \
  --relpath "home/user/datei.txt" \
  --out-dir /srv/restore \
  --download --verify

# Versions-Timeline (alle Snapshots, nur Anzeige)
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --all-versions \
  --relpath "Gemeinsam/Rest/dokument.pdf"

# Alle Versionen einer Datei laden (nur geänderte SHA)
python scripts/utilities/pool_restore.py \
  --env-file "$ENV_FILE" --pool-root /Backup/rtb_pool \
  --all-versions \
  --relpath "Gemeinsam/Rest/dokument.pdf" \
  --out-dir /srv/restore \
  --download --verify --only-changed
```

Einzel-Snapshot: `/srv/restore/<snapshot>/<relpath>`.  
Alle Versionen: `/srv/restore/_versions/<relpath>/<snapshot>/<dateiname>`.  
Ausführliche Doku: `scripts/utilities/pool_restore.md`.

---

## 11. systemd-Service umstellen und aktivieren

```bash
# Service auf Pool-Wrapper umstellen:
sudo systemctl edit --full backup-pipeline.service
# → ExecStart=/opt/apps/rtb/rtb_pool_wrapper.sh

# Prüfen ob richtig:
systemctl cat backup-pipeline.service | grep ExecStart

# Timer aktivieren:
sudo systemctl enable --now backup-pipeline.timer
sudo systemctl status backup-pipeline.timer
```

---

## Troubleshooting

### pCloud-Token abgelaufen
```bash
curl -s "https://eapi.pcloud.com/userinfo?auth=$PCLOUD_TOKEN" | python -m json.tool
# "error": 1000 → Token ungültig → neuen Token via rclone holen
# Siehe RCLONE_TOKEN_REFRESH.md
```

### MariaDB-Verbindungsfehler
```bash
sudo mysql pcloud_backup -e "SELECT 1"  # kein Passwort nötig (root via Unix-Socket)
sudo mysql pcloud_backup -e "SHOW TABLES;"
```

### Snapshot lädt nicht hoch (FAILED im Log)
```bash
# Letzten Fehler sehen:
sudo mysql pcloud_backup -e \
  "SELECT snapshot_name, status, error_message FROM backup_runs ORDER BY started_at DESC LIMIT 5;"

# Unvollständigen Remote-Snapshot neu starten:
# Der Wrapper erkennt fehlendes .upload_complete und startet automatisch neu.
./wrapper_pcloud_pool_sync_1to1.sh <snapshot>
```

### Pool-Objekt fehlt in Validation
```bash
# SHA identifizieren:
python scripts/utilities/pool_check_remote.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --snapshot <snap>

# Vollständiger Check:
python scripts/utilities/pool_verify_backup.py \
  --env-file "$ENV_FILE" --dest-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests --stub-sample 50
```

### Disk full (/srv/pcloud-temp)
```bash
df -h /srv
# Alte Temp-Dateien bereinigen (nur wenn kein Backup läuft!):
find /srv/pcloud-temp -type f -mtime +7 -delete
```
