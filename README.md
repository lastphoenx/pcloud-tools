# pCloud-Tools

Deduplizierte Cloud-Backups mit JSON-Manifest-Architektur für die pCloud-API. Ermöglicht platzsparende Snapshots ähnlich wie `rsync --hard-links`, aber in der Cloud.

Funktioniert auf Linux/Debian. Hauptvorteil: **Content-based Deduplication** (SHA256) - gleiche Dateien werden nur einmal hochgeladen, Snapshots bestehen aus JSON-Metadaten + Verweisen auf File-Pool. Vollständige Restore-Funktion rekonstruiert Backups aus Manifests.

---

## 📚 Table of Contents

- [🏗️ Projekt-Übersicht](#️-projekt-übersicht-secure-nas--backup-ecosystem)
  - [📦 Repositories](#-repositories)
  - [🎯 Die Entstehungsgeschichte](#-die-entstehungsgeschichte)
  - [🔗 Zusammenspiel der Komponenten](#-zusammenspiel-der-komponenten)
- [🛠️ Technologie-Stack](#️-technologie-stack)
- [Installation](#installation)
- [Usage](#usage)
- [Features](#features)
- [Examples](#examples)
- [How It Works](#how-it-works)
- [Integration with Backup Pipeline](#integration-with-backup-pipeline)
- [Best Practices](#best-practices)
- [Contributing](#contributing)
- [License](#license)

---

# 🏗️ Projekt-Übersicht: Secure NAS & Backup Ecosystem

## 📦 Repositories

Dieses Projekt besteht aus mehreren zusammenhängenden Komponenten:

- **[EntropyWatcher & ClamAV Scanner](https://github.com/lastphoenx/entropy-watcher-und-clamav-scanner)** - Pre-Backup Security Gate mit Intrusion Detection
- **[pCloud-Tools](https://github.com/lastphoenx/pcloud-tools)** - Deduplizierte Cloud-Backups mit JSON-Manifest
- **[RTB Wrapper](https://github.com/lastphoenx/rtb)** - Delta-Detection für Rsync Time Backup
- **[Rsync Time Backup](https://github.com/laurent22/rsync-time-backup)** (Original) - Hardlink-basierte lokale Backups

---

## 🎯 Die Entstehungsgeschichte

### Von proprietären NAS-Systemen zu Debian

Die Reise begann mit Frustration: **QNAP** (TS-453 Pro, TS-473A, TS-251+) und **LaCie 5big NAS Pro** waren zwar funktional, aber sobald man mehr als die Standard-Features wollte, wurde es zum Gefrickel. Autostart-Scripts, limitierte Shell-Umgebungen, fehlende Packages - man kam einfach nicht ans Ziel.

**Die Lösung:** Wechsel auf ein vollwertiges **Debian-System**. Hardware: **Raspberry Pi 5** mit **Radxa Penta SATA HAT** (5x 2.5" SATA-SSDs), Samba-Share mit Recycling-Bin. Volle Kontrolle, Standard-Tools, keine Vendor-Lock-ins.

### Der Weg zur vollautomatisierten Backup-Pipeline

#### 1️⃣ **RTB Wrapper** - Delta-gesteuerte Backups

Ziel: Automatisierte lokale Backups mit Deduplizierung über Standard-Debian-Tools.

Ich entschied mich für [Rsync Time Backup](https://github.com/laurent22/rsync-time-backup) - ein cleveres Script, das `rsync --hard-links` nutzt, um platzsparende Snapshots zu erstellen. **Problem:** Das Script lief immer, auch wenn keine Änderungen vorlagen.

**Lösung:** Der [RTB Wrapper](https://github.com/lastphoenx/rtb) prüft vorher ob überhaupt ein Delta existiert (via `rsync --dry-run`). Nur bei echten Änderungen wird das Backup ausgeführt.

#### 2️⃣ **EntropyWatcher + ClamAV** - Pre-Backup Security Gate

Eine Erkenntnis: **Backups von infizierten Dateien sind wertlos.** Schlimmer noch - sie verbreiten Malware in die Backup-Historie und Cloud.

**Lösung:** [EntropyWatcher & ClamAV Scanner](https://github.com/lastphoenx/entropy-watcher-und-clamav-scanner) analysiert `/srv/nas` (und optional das OS) auf:
- **Entropy-Anomalien** (verschlüsselte/komprimierte verdächtige Dateien)
- **Malware-Signaturen** (ClamAV)
- **Safety-Gate-Mechanismus:** Backups werden nur bei grünem Status ausgeführt

Später erweitert auf das gesamte Betriebssystem (`/`, `/boot`, `/home`).

#### 3️⃣ **Honeyfiles** - Intrusion Detection mit Ködern

Der **Shai-Hulud 2.0 npm Worm** zeigte: Moderne Malware sucht aktiv nach Credentials (`~/.aws/credentials`, `.git-credentials`, `.env`-Dateien).

**Gegenmaßnahme:** **Honeyfiles** - 7 randomisiert benannte Köder-Dateien, überwacht durch **auditd** auf Kernel-Ebene:
- **Tier 1:** Zugriff auf Honeyfile = sofortiger Alarm + Backup-Blockade
- **Tier 2:** Zugriff auf Honeyfile-Config = verdächtig
- **Tier 3:** Manipulation an auditd = kritischer Alarm

#### 4️⃣ **pCloud-Tools** - Deduplizierte Cloud-Backups

Mit funktionierender lokaler Backup- und Security-Pipeline kam die Frage: **Wie bekomme ich das sicher in die Cloud?**

**Anforderung:** Deduplizierung wie bei `rsync --hard-links` (Inode-Prinzip), aber `rclone` konnte das nicht.

**Lösung:** [pCloud-Tools](https://github.com/lastphoenx/pcloud-tools) mit **JSON-Manifest-Architektur**:
- **JSON-Stub-System:** Jedes Backup speichert nur Metadaten + Verweise auf echte Files
- **Inhalts-basierte Deduplizierung:** Gleicher SHA256-Hash = gleiche Datei = kein Upload
- **Restore-Funktion:** Rekonstruiert komplette Backups aus Manifests + File-Pool

---

## 🔗 Zusammenspiel der Komponenten

```
┌─────────────────────────────────────────────────────────────┐
│  1. EntropyWatcher + ClamAV (Safety Gate)                   │
│     ↓ GREEN = Sicher | YELLOW = Warnung | RED = STOP        │
└─────────────────────────────────────────────────────────────┘
                            ↓ (nur bei GREEN)
┌─────────────────────────────────────────────────────────────┐
│  2. RTB Wrapper prüft: Hat sich was geändert?               │
│     ↓ JA = Delta erkannt | NEIN = Skip Backup               │
└─────────────────────────────────────────────────────────────┘
                            ↓ (nur bei Delta)
┌─────────────────────────────────────────────────────────────┐
│  3. Rsync Time Backup (lokale Snapshots mit Hard-Links)     │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  4. pCloud-Tools (deduplizierter Upload in Cloud)           │
└─────────────────────────────────────────────────────────────┘

       [Honeyfiles überwachen parallel das gesamte System]
```

---

## 🛠️ Technologie-Stack

- **OS:** Debian Bookworm (Raspberry Pi 5)
- **Storage:** 5x 2.5" SATA SSD (Radxa Penta SATA HAT)
- **File Sharing:** Samba mit Recycling-Bin
- **Security:** auditd, ClamAV, Python-basierte Entropy-Analyse
- **Backup:** rsync, JSON-Manifests, pCloud API
- **Automation:** Bash, systemd-timer, Git-Workflow

---

## Installation

```bash
git clone https://github.com/lastphoenx/pcloud-tools
cd pcloud-tools

# Python Virtual Environment erstellen
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Konfiguration
cp .env.example .env
# Edit .env with your pCloud credentials
```

**Abhängigkeiten:**  
Alle Skripte nutzen Python-Standardbibliothek. Optional: [`python-dotenv`](https://pypi.org/project/python-dotenv/) für `.env`-Support.

## Usage

```
Primary Tools:
  pcloud_json_manifest.py                    Create JSON manifests from local backups
  pcloud_push_json_manifest_to_pcloud.py     Upload manifests + deduplicated files
  pcloud_restore.py                          Restore backups from manifests
  pcloud_integrity_check.py                  Verify cloud backup integrity

Helper Tools:
  pcloud_bin_lib.py                          Binary client library for pCloud API
  wrapper_pcloud_sync_1to1.sh                Shell wrapper for backup automation

Umgebungsvariablen (.env):
  PCLOUD_USER                                pCloud username
  PCLOUD_PASS                                pCloud password
  PCLOUD_REGION                              Region (eu|us)
  LOCAL_BACKUP_ROOT                          Local source directory
  PCLOUD_BACKUP_ROOT                         Cloud destination directory
```

## Features

* **JSON-Manifest-Architektur** - Snapshots bestehen aus Metadaten + Verweise auf File-Pool

* **Content-based Deduplication** - SHA256-basiert: gleiche Datei wird nur einmal gespeichert

* **Space Efficiency** - Wie `rsync --hard-links`, aber in der Cloud

* **Full Restore** - Rekonstruiert komplette Backups aus Manifests + File-Pool

* **Integrity Check** - Verifiziert Cloud-Backups gegen SHA256-Hashes

* **Incremental Uploads** - Nur neue/geänderte Dateien werden hochgeladen

* **Python Standard Library** - Keine externen API-Wrapper nötig

* **Automation-Ready** - Shell-Wrapper für systemd-Timer Integration

## Examples

* **JSON-Manifest aus lokalem Backup erstellen:**

```bash
python pcloud_json_manifest.py \
  --source /mnt/backup/latest \
  --output /tmp/manifest_$(date +%Y%m%d).json
```

* **Deduplizierter Upload in pCloud:**

```bash
python pcloud_push_json_manifest_to_pcloud.py \
  --manifest /tmp/manifest_20251214.json \
  --pool-dir /pCloudBackups/file_pool \
  --manifest-dir /pCloudBackups/manifests
```

* **Backup wiederherstellen:**

```bash
python pcloud_restore.py \
  --manifest /pCloudBackups/manifests/manifest_20251214.json \
  --pool-dir /pCloudBackups/file_pool \
  --output /mnt/restore/2024-12-14
```

* **Integritäts-Check:**

```bash
python pcloud_integrity_check.py \
  --manifest /pCloudBackups/manifests/manifest_20251214.json \
  --pool-dir /pCloudBackups/file_pool
```

* **Automatisierung via Wrapper:**

```bash
# wrapper_pcloud_sync_1to1.sh ruft die Tools in korrekter Reihenfolge auf
bash wrapper_pcloud_sync_1to1.sh /mnt/backup/latest
```

## How It Works

**Architektur:**

```
1. JSON-Manifest erstellen (lokal)
   ├─ Scannt Backup-Verzeichnis
   ├─ Berechnet SHA256 für jede Datei
   └─ Speichert Metadaten (path, size, mtime, sha256) in JSON

2. Deduplizierter Upload
   ├─ Prüft für jede Datei: SHA256 bereits im Pool?
   ├─ JA → Nur Manifest-Verweis, kein Upload
   └─ NEIN → Upload in file_pool/<first_2_chars_of_sha256>/<sha256>

3. Restore
   ├─ Liest Manifest
   ├─ Für jeden Eintrag: Download aus file_pool/<sha256>
   └─ Rekonstruiert Original-Verzeichnisstruktur
```

**File-Pool-Struktur:**

```
/pCloudBackups/
├─ file_pool/
│  ├─ a7/
│  │  └─ a7f3e9d8c2b1... (Datei mit SHA256 = a7f3e9...)
│  ├─ b8/
│  │  └─ b8g2h1k9f3c4...
│  └─ ...
└─ manifests/
   ├─ manifest_20251201.json
   ├─ manifest_20251208.json
   └─ manifest_20251214.json
```

**Deduplizierung:**
- Datei `photo.jpg` in 10 Snapshots → 1x im file_pool, 10x Verweis im Manifest
- Platzersparnis: ~90% bei typischen Backup-Historien

## Integration with Backup Pipeline

Dieses Tool ist **Stufe 4** in der automatisierten Backup-Pipeline:

1. **EntropyWatcher + ClamAV** (Safety Gate) → EXIT 0 = GREEN
2. **RTB Wrapper** prüft Delta → JA = Änderungen erkannt
3. **Rsync Time Backup** erstellt lokalen Snapshot
4. **pCloud-Tools** (dieser Repo) → deduplizierter Cloud-Upload

**Wrapper-Integration:**

```bash
# In rtb_wrapper.sh (nach erfolgreichem rsync):
if [ $RSYNC_EXIT -eq 0 ]; then
  bash wrapper_pcloud_sync_1to1.sh "$BACKUP_LATEST_DIR"
fi
```

## Best Practices

* **Manifest-Naming** - Zeitstempel verwenden: `manifest_$(date +%Y%m%d_%H%M%S).json`

* **Pool-Cleanup** - Alte SHA256-Dateien nur löschen, wenn kein Manifest mehr darauf verweist

* **Integrity Checks** - Regelmäßig nach Upload ausführen (wöchentlich empfohlen)

* **Bandwidth** - Bei großen Uploads: `--rate-limit` in pCloud-API nutzen

* **Restore-Tests** - Monatliche Test-Restores in Staging-Umgebung

* **Region** - `PCLOUD_REGION=eu` für EU-Datacenter (DSGVO-Compliance)
