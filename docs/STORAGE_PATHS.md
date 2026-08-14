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

**RTB:** Manifeste/Index/Deltas werden mitgesichert (Mergerfs-Spiegel `/srv/nas/pcloud-archive/`). Nur `staging/` ist Scratch — in `rtb/excludes.txt` ausgeschlossen. Seit `1f0a1c9` werden leere `staging/json/<id>/` nach Upload entfernt.

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

**Kein separater Pipeline-Export:** `raspi5nas_backup.sh` kopiert **nicht** mehr nach `Backup/raspi5nas/pcloud-*`. Offsite-Manifeste/Temp kommen über RTB, wenn ein Snapshot fällig ist.

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
