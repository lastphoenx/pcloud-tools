# NAS: ein Ordner = eine SSD (Pin-Map)

> **Stand:** Juni 2026 · pi-nas  
> **Problem:** mergerfs mit `category.create=mfs` legt Dateien global auf die SSD mit **meistem freien Platz** — unabhängig vom Share.  
> **Konzept:** Ein Mount (`/srv/nas` via mergerfs), aber jeder Top-Level-Share physisch **nur auf einer SSD**.

---

## Soll-Zuordnung

| SSD | Ordner |
|-----|--------|
| **SSD1** | user-a, Eltern, Gemeinsam, user-b, user-c, restore, admin, av-quarantine, **Paperless** (Media) |
| **SSD2** | Fotos, Videos, **Backup** (inkl. `Backup/Paperless/` — DB-Dumps, Config) |

**Nicht über mergerfs (separat):**

| Pfad | SSD |
|------|-----|
| `/srv/pcloud-archive`, `/srv/pcloud-temp` | SSD2 (Pipeline-Bind-Mounts) |
| `/srv/nas/pcloud-*` | dieselben Daten wie Bind-Mount (über mergerfs sichtbar); **SSD2 nicht löschen** |

**Paperless:** Live-Media und NFS-Export liegen bewusst auf **`/mnt/ssd1/Paperless/media`** (kein mergerfs-Split). Backups laufen über **`/mnt/ssd2/Backup/Paperless/`** — das ist „Paperless auf SSD2“ im Sinne von Backup, nicht die PDFs.

---

## Warum user-a 44 / 267 / 311?

| Zähler | Bedeutung |
|--------|-----------|
| ssd1 | Dateien nur auf `/mnt/ssd1/user-a` |
| ssd2 | Dateien nur auf `/mnt/ssd2/user-a` (user-a gehört auf SSD1 → **falsch**) |
| mergerfs | Vereinigte Sicht `/srv/nas/user-a` |

`mfs` ignoriert die Share-Grenze. Nach Konsolidierung + `epmfs` landen neue Dateien unter `user-a/` wieder auf SSD1, weil der Pfad nur dort existiert.

---

## mergerfs-Policy (nach einmaliger Bereinigung)

```
category.create=epmfs,category.search=epmfs,category.action=epmfs
```

- **`epmfs`** = *existing path, most free space* — Create im Branch, der den Ordnerpfad schon hat.  
- **Nicht** `mfs` (global freie Platte) und **nicht** `ff` (alles SSD1).

Voraussetzung: Jeder Share existiert physisch **nur** auf seiner SSD (einmalig `migrate` + `purge`).

---

## Skript

```bash
cd /opt/apps/pcloud-tools/main
./scripts/nas-ssd-pin.sh show-pin
```

| Befehl | Zweck |
|--------|--------|
| `analyze` | Spalte `wrong?` zeigt Dateien auf falscher SSD |
| `conflicts` | Gleicher Pfad, unterschiedliche MD5 |
| `migrate` | Rsync **zur** kanonischen SSD (ssd2→ssd1 für user-a, ssd1→ssd2 für Fotos, …) |
| `verify` | Alles auf falscher SSD identisch auf kanonischer SSD? |
| `purge` | Falscher Branch wird gelöscht |
| `apply-fstab` | `epmfs` statt `mfs` |

---

## Ablauf

```bash
systemctl stop smbd nmbd   # empfohlen

./scripts/nas-ssd-pin.sh analyze
./scripts/nas-ssd-pin.sh conflicts

./scripts/nas-ssd-pin.sh migrate      # ggf. --checksum
./scripts/nas-ssd-pin.sh verify
./scripts/nas-ssd-pin.sh purge --yes

./scripts/nas-ssd-pin.sh cleanup-dupes   # nur Streuer auf SSD1 löschbar; /mnt/ssd2/pcloud-* = Pipeline

./scripts/nas-ssd-pin.sh apply-fstab
umount /srv/nas && mount /srv/nas

systemctl start smbd nmbd
./scripts/nas-ssd-pin.sh analyze   # wrong? leer für alle PIN-Ordner
```

**user-a:** migrate zieht von SSD2 → SSD1, purge löscht `/mnt/ssd2/user-a`.  
**Fotos/Videos/Backup:** migrate zieht von SSD1 → SSD2 (falls dort Split), purge löscht auf SSD1.

---

## Verifikation

```bash
# user-a nur SSD1
test ! -d /mnt/ssd2/user-a || diff -rq /mnt/ssd1/user-a /mnt/ssd2/user-a
diff -rq /mnt/ssd1/user-a /srv/nas/user-a

# Fotos nur SSD2
test ! -d /mnt/ssd1/Fotos || echo "Fotos noch auf ssd1 — purge fehlt"
```

---

## Siehe auch

- `docs/STORAGE_PATHS.md`
- `doku/Raspi/raspinas/samba/smb-permissions.md`
- `doku/pve2/vm/121-paperless/Doku/docs/ct121-nfs-fix.md`
