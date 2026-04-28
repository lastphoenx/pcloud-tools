# pcloud_restore.py – Universelles pCloud Recovery Tool

## **Kurzbeschreibung**
Das ultimative Tool zur Wiederherstellung von Daten aus pCloud. Es unterstützt sowohl das **Snapshot-basierte Restore** (historische Backups) als auch den **Direct-Download** von aktuellen Dateien/Ordnern.

### **Key Features**
- 🚀 **Parallel Downloads**: Kleine Dateien (<50MB) werden mit bis zu 16 Threads gleichzeitig geladen.
- 📉 **Deduplizierung**: Nutzt den `content_index.json`, um identische Dateien via Hardlinks lokal nur einmal zu speichern.
- 🔄 **Smart Resume (Stage 1)**: Überspringt bereits vorhandene Dateien (via SHA256 im Snapshot-Mode oder via Size-Check im Direct-Mode).
- 🎯 **Direct Mode**: Download via Pfad oder ID (`folderid`/`fileid`) ohne Index-Zwang.
- 🛡️ **Integrity**: Automatische SHA256-Verifikation nach jedem Download (wenn Hashes verfügbar sind).

---

## **Parameter-Übersicht**

### **1. Quelle (Eins von diesen erforderlich)**
- `--snapshot <name>`: Stellt einen spezifischen Snapshot aus dem Backup-System wieder her.
- `--folder <path>`: Lädt einen pCloud-Ordner rekursiv herunter (Live-Daten).
- `--folderid <id>`: Lädt einen Ordner via ID rekursiv herunter.
- `--file <path>`: Lädt eine einzelne Datei via Pfad herunter.
- `--fileid <id>`: Lädt eine einzelne Datei via ID herunter.

### **2. Ziel & Modus**
- `--out-dir <path>` (Required): Lokales Zielverzeichnis.
- `--mode {flat, object-store}`: 
  - `flat` (Default): Klassische Ordnerstruktur.
  - `object-store`: Baut einen lokalen Snapshot-Baum mit Hardlinks auf (für Server-Architekturen).
- `--download`: Führt den Restore tatsächlich aus (ohne dies nur Plan-Modus).

### **3. Filter & Verifikation**
- `--filter <prefix>`: Beschränkt den Restore auf Dateien, die mit diesem Pfad beginnen.
- `--verify`: Aktiviert die SHA256-Prüfung beim Download.
- `--verify-only`: Prüft nur bereits geladene Dateien im Zielverzeichnis (kein Download).

---

## **Beispielaufrufe**

### **A. Snapshot-Wiederherstellung (Klassisch)**
```bash
# 1. Verfügbare Snapshots auflisten
python pcloud_restore.py --manifest pcloud --list-snapshots

# 2. Snapshot wiederherstellen (mit 16 Threads & Verifikation)
python pcloud_restore.py \
  --manifest pcloud \
  --snapshot 2026-04-15-120000 \
  --out_dir /home/user/restore \
  --download --verify
```

### **B. Direct-Download (Der "Game Changer")**
```bash
# Einen spezifischen Ordner rekursiv laden (ohne Backup-Index)
python pcloud_restore.py \
  --manifest pcloud \
  --folder "/Backup/rtb_1to1/Wichtig/Projekte" \
  --out_dir ./downloads \
  --download

# Eine einzelne Datei via ID retten
python pcloud_restore.py \
  --manifest pcloud \
  --fileid 18273645 \
  --out_dir . \
  --download
```

### **C. Resume-Funktion nutzen**
Wenn ein Download abgebrochen wurde, einfach den gleichen Befehl erneut starten. Das Skript erkennt:
1. Im **Snapshot-Mode**: Stimmt der SHA256 der lokalen Datei? -> Skip.
2. Im **Direct-Mode**: Stimmt die Dateigröße exakt überein? -> Skip.

---

## **Performance-Tuning**
Die Parallelität kann über Umgebungsvariablen gesteuert werden:
- `PCLOUD_DOWNLOAD_THREADS`: Anzahl der Threads (Default: 16).
- `PCLOUD_DOWNLOAD_SMALL_THRESHOLD`: Ab welcher Größe eine Datei als "groß" gilt und sequentiell geladen wird (Default: 50MB).
