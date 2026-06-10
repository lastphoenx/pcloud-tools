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
  deltas/
    delta_verify_<snap>.json
  resume/                 ← Chunked-Upload Resume-State
```

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
/srv/nas/          mergerfs 1:2  (ssd1 + ssd2, 3.6T)  ← Samba [nas]-Share
/srv/pcloud-archive/  bind → sdd1 only (1.8T)         ← pCloud-Backup-Artefakte
/srv/pcloud-temp/     bind → sdd1 only                 ← pCloud-Temp
/                     mmcblk0p2 (15G)                    ← OS nur
```

---

## Siehe auch

- `docs/SETUP.md` §3 — `.env` Einrichtung
- `docs/ENV_VARIABLES.md` — `PCLOUD_ARCHIVE_DIR`, `PCLOUD_TEMP_DIR`
- `doku/Raspi/raspinas/samba/smb-permissions.md` — NAS-Filesystem-Übersicht (externes Repo)
