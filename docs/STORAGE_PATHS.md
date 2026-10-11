# Lokale Speicherpfade (pi-nas, verifiziert)

> **Stand:** Juni 2026 · Quelle: `df`/`findmnt` auf pi-nas, `.env`, laufende Pipeline  
> **Regel:** Das laufende System hat recht — diese Doku beschreibt den **Ist-Zustand**, nicht eine Alternative.

---

## Pipeline-Pfade (aktiv)

| Variable | Pfad | Physisches FS | Verwendung |
|----------|------|---------------|------------|
| `PCLOUD_ARCHIVE_DIR` | `/srv/pcloud-archive` | `/dev/sdd1` (SSD2) | Manifeste, Master-Index, Deltas, Resume |
| `PCLOUD_TEMP_DIR` | `/srv/pcloud-temp` | `/dev/sdd1` (SSD2) | Temp-Manifeste, Index-Checkpoints während Upload |
| `RTB` | `/mnt/backup/rtb_nas` | (RTB-Quelle) | Lokale Snapshot-Quelle für Backup |
| `PCLOUD_DEST` | `/Backup/rtb_pool` | pCloud remote | Pool-Root auf pCloud |

**Micro-SD (`/` = `/dev/mmcblk0p2`):** Pipeline schreibt **nicht** dorthin — nur Code, OS, Logs.

Verifizierung:

```bash
df -h /srv/pcloud-archive /srv/pcloud-temp /
grep -E 'PCLOUD_ARCHIVE|PCLOUD_TEMP' /opt/apps/pcloud-tools/main/.env
```

Erwartete Ausgabe (pi-nas):

```
/dev/sdd1  ...  /srv/pcloud-archive
/dev/sdd1  ...  /srv/pcloud-temp
/dev/mmcblk0p2  ...  /
```

---

## Bind-Mounts (SSD2)

`/srv/pcloud-archive` und `/srv/pcloud-temp` sind **keine Symlinks**, sondern Bind-Mounts auf SSD2:

```
/mnt/ssd2/pcloud-archive  ──bind──►  /srv/pcloud-archive   ← Pipeline
/mnt/ssd2/pcloud-temp     ──bind──►  /srv/pcloud-temp      ← Pipeline
```

Unterverzeichnisse (von Pipeline/Wrapper angelegt):

```
/srv/pcloud-archive/
  manifests/              ← ein Manifest pro erfolgreichem Upload: <snap>.json
  indexes/
    content_index_master.json
    pool_index.sqlite3        ← C1 Delta-Arbeitsindex (optional, Flag default aus)
  deltas/
    delta_verify_<snap>.json
  resume/                 ← Chunked-Upload Resume-State
  staging/json/           ← Upload-Scratch (pro folderid) — **nicht** ins RTB (`excludes.txt`)
```

**RTB / Offsite:** `/srv/pcloud-archive` liegt auf **SSD2 (`sdd1`)**, nicht unter `SRC=/srv/nas`. Manifeste, Deltas, Resume und die lokale `pool_index.sqlite3` sind **nicht** im RTB-Snapshot und **nicht** im pCloud-Pool-Backup der Nutzdaten-Snapshots (Manifest-Test: nur Nutzdaten + Spiegel-Master-JSON). Offsite-Index-Spiegel: ausschließlich `Backup/raspi5nas/pcloud-archive-indexes/content_index_master.json` (wenn ein RTB-Lauf mitzieht). `staging/` ist zusätzlich in `excludes.txt` ausgeschlossen.

---

## NICHT für die Pipeline verwenden

| Pfad | Was es ist | Warum nicht |
|------|------------|-------------|
| `/srv/nas/pcloud-archive` | Ordner unter mergerfs (`1:2`, SSD1+SSD2) | **Separater** Baum, nicht der Pipeline-Pfad; per Samba `veto files` ausgeblendet |
| `/srv/nas/pcloud-temp` | Ebenfalls mergerfs | Dito |
| `/tmp` | RAM/Disk je nach Konfiguration | Nur Fallback wenn `PCLOUD_TEMP_DIR` nicht gesetzt |

Falls `/srv/nas/pcloud-archive` existiert: Legacy oder manuelle Kopie. Die Pipeline liest/schreibt **`/srv/pcloud-archive`** (laut `.env`).

Prüfen ob Duplikat abweicht:

```bash
diff -rq /srv/pcloud-archive/manifests /srv/nas/pcloud-archive/manifests
```

Keine Ausgabe = identisch. Unterschiede = zwei getrennte Bäume — nur `/srv/pcloud-archive` ist maßgeblich.

---

## Restore-Pfad (getrennt von Archiv)

```
/srv/restore  →  Symlink auf  /srv/nas/restore
```

Restore-Ziel für `pool_restore.py --out-dir /srv/restore` liegt unter mergerfs in `/srv/nas/`, **nicht** unter `pcloud-archive`.

---

## Alle Tools: korrekte Pfade

```bash
# Audit-Status
python scripts/utilities/pool_audit_status.py \
  --env-file .env --pool-root /Backup/rtb_pool

# Integrität (Manifest-Pfad aus .env oder explizit)
python scripts/utilities/pool_verify_backup.py \
  --env-file .env \
  --pool-root /Backup/rtb_pool \
  --manifests-dir /srv/pcloud-archive/manifests

# Wrapper (liest .env selbst)
./wrapper_pcloud_pool_sync_1to1.sh
```

`--manifests-dir` muss auf `/srv/pcloud-archive/manifests` zeigen — **nicht** `/srv/nas/pcloud-archive/manifests`.

---

## Referenz: mergerfs vs. Bind-Mount

```
/srv/nas/          mergerfs 1:2  (ssd1 + ssd2)  ← Samba, RTB
                     Pin-Map: SSD1 = User-Shares + Paperless/media; SSD2 = Fotos, Videos, Backup
                     Policy nach Bereinigung: category.create=epmfs — docs/NAS_SSD_PIN.md
/srv/pcloud-archive/  bind → sdd1 only (1.8T)         ← Pipeline (kanonisch)
/srv/pcloud-temp/     bind → sdd1 only                 ← Pipeline-Temp
/                     mmcblk0p2 (15G)                    ← OS nur
```

---

## RTB vs. Pipeline-Artefakte (Juni 2026)

**Ziel:** Reine Pipeline-Änderungen (Upload, Manifest, Temp) und **replizierte Backup-Stores** (`Backup/pbs2/`, `Backup/pve2/` — täglich von PBS/pve2 gepusht) sollen **kein** RTB triggern. Sobald **Nutzerdaten** oder **Config-Backups** (z. B. Paperless) ein Backup auslösen, landen diese Pfade **mit** im Snapshot.

| Schicht | Datei / Mechanismus | Pipeline + `Backup/pbs2` `Backup/pve2` | `__pycache__/` etc. |
|---------|---------------------|----------------------------------------|---------------------|
| Delta-Check (`signature`/`hybrid`) | `rtb_check_excludes.sh` + Sub-Buckets | **triggert nicht** | excluded (via excludes.txt in Check-Liste) |
| Echtes RTB (`rsync_tmbackup`) | `excludes.txt` | **mitgesichert** wenn Backup läuft | **nie** im Snapshot |
| Pool-Manifest-Scan | `PCLOUD_MANIFEST_SKIP_GLOBS` in Python | skipped (keine Pool-Userdateien) | optional in `.env` |

**Post-Filter:** `rtb_check_only_delta.py --analyze` trennt echte Trigger-Deltas von reinen Pipeline-Pfaden (rsync-Exclude ist nicht immer 100 % zuverlässig). Pre-Backup-Check im Wrapper nutzt dieselbe Logik.

**Dashboard:** `aggregate_status.sh` parst `[RTB Delta JSON]`, `[RTB PipelineOnly JSON]`, `[RTB BackupScope JSON]`, `[RTB ExcludePolicy JSON]` → siehe `docs/DASHBOARD.md`.

**Deploy excludes.txt:** Änderungen nur im Repo `rtb`, auf pi-nas `git pull` — nicht `/opt/apps/rtb/excludes.txt` per Hand editieren (blockiert sonst `git pull`).

**raspi5nas-Spiegel (rtb-Repo):** `raspi5nas_backup.sh` legt täglich u. a. `Backup/raspi5nas/pcloud-archive-indexes/content_index_master.json` ab (~1–2 GB). **`pool_index.sqlite3` wird nicht gespiegelt** (rebuild per Pipeline-Streaming oder `pool_ops_index_sync.py`; RTB-`excludes.txt` schließt eine Altdatei im Spiegel aus). Spart ~7 GB neues Pool-Objekt pro Tageskopie. Siehe `docs/POOL_INDEX_DB.md` § Offsite-Spiegel.

**Notfall / Was wo liegt:**

| Daten | Lokal SSD2 | pCloud (Pool/Snapshots) | RTB-Offsite (NAS → pCloud) |
|-------|------------|-------------------------|----------------------------|
| Nutzer-Snapshots (RTB) | — | Stubs + Pool-Hashes | ja (Kern-Backup) |
| `content_index_master.json` | ja | ja (Remote-Master) | ja (Spiegel `raspi5nas/…`) |
| `manifests/*.json` (~97×) | ja | nein | nein |
| `pool_index.sqlite3` (Arbeits-DB) | ja | nein | nein (rebuildbar) |
| Deltas / Resume | ja | nein | nein |

Manifeste neu erzeugen ist **Aufwand** (RTB-Re-Scan), kein Datenverlust der Nutzer-Backups. **Optional (rtb-Repo):** `pcloud_manifests_usb_backup.sh` + `pcloud-manifests-backup.service` (Zielpfad in `Environment=` der Unit) + Timer — `rsync` auf Backup-HDD **außerhalb** `/srv/nas`. Täglich inkrementell.

**Pi-nas Einmal-Setup (Manifest-Spiegel, öffentliche Skripte):**

1. `git pull` in `/opt/apps/rtb` — Pfade in `pcloud-manifests-backup.service` (`Environment=PCLOUD_MANIFESTS_BACKUP_DEST=…`) anpassen; Ziel **nicht** unter `/srv/nas`.
2. `sudo cp /opt/apps/rtb/pcloud-manifests-backup.{service,timer} /etc/systemd/system/`
3. `sudo systemctl daemon-reload && sudo systemctl enable --now pcloud-manifests-backup.timer` (Standard: 10:30 + 22:15)
4. Manuell testen: `sudo systemctl start pcloud-manifests-backup.service` (nicht das Skript ohne Unit — `Environment=` fehlt sonst)
5. Pipeline-Tools: `/opt/apps/pcloud-tools/venv/bin/python3` (ijson etc.)

Log: `/var/log/backup/pcloud_manifests_backup.log`. Details: `rtb/README.md`.

**Unvollständiger pCloud-Upload — Retry-Entscheidung:**

| Situation | Befehl | Was passiert |
|-----------|--------|--------------|
| Pool + Stubs **remote OK**, nur Verify/`.upload_complete` fehlten (z. B. OOM im Integrity-Gate) | `rtb_pool_wrapper.sh --finalize-only /mnt/backup/rtb_nas/SNAPSHOT` | Nur Integrity-Gate + Marker + Index (~Minuten) |
| Remote-Snapshot **unvollständig** (kein Phase-4-Erfolg, Stubs fehlen) | `rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/SNAPSHOT` | Löscht unvollständigen Remote-Ordner, Delta neu (copyfolder + Upload) |

`--upload-only` braucht **kein** manuelles Löschen — `pcloud_push` erkennt fehlendes `.upload_complete` und startet sauber neu.

**Prüfen vor Retry:** In `pcloud_sync.log` nach `Phase 4: … failed=0` und `Stubs erfolgreich` suchen → dann `--finalize-only` statt `--upload-only`.

Siehe auch: `rtb/README.md` (Excludes), `doku/Raspi/raspinas/ops/betrieb.md` §9.

---

## Siehe auch

- `docs/SETUP.md` §3 — `.env` Einrichtung
- `docs/ENV_VARIABLES.md` — `PCLOUD_ARCHIVE_DIR`, `PCLOUD_TEMP_DIR`
- `doku/Raspi/raspinas/samba/smb-permissions.md` — NAS-Filesystem-Übersicht (externes Repo)
