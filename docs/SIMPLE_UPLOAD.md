# pCloud Simple Upload - Universal Upload Tool

**Ein Schweizer-Sackmesser für pCloud-Uploads** 🔧

## 🎯 Zweck

Ad-hoc-Uploads ohne komplexes Setup:
- **Einzelne Dateien** oder **ganze Ordner** rekursiv hochladen
- Minimale Dependencies (Python 3.7+, pcloud_bin_lib.py, .env)
- Auf **jedem Debian/Linux-System** nutzbar
- **Keine Manifest-Verwaltung**, **keine Deduplizierung** → einfach 1:1 Upload

---

## 🚀 Features

### ✅ Auto-Download von pcloud_bin_lib.py
- **Erste Ausführung:** Library wird automatisch von GitHub geladen
- **Zielort:** Gleicher Ordner wie `pcloud_simple_upload.py`
- **Deaktivieren:** `--no-auto-download` Parameter

### ✅ Performance-Optimierungen (aus pcloud_bin_lib.py)
- **DNS-Caching:** Hostname-Lookups werden gecacht (weniger DNS-Queries)
- **Keep-Alive:** HTTP-Connections werden wiederverwendet (weniger TLS-Handshakes)
- **Connection-Pooling:** `requests.Session()` pooled Connections

### ✅ Intelligente Upload-Strategie
- **Kleine Dateien (<5 GB):** Direkter Upload (`putfile`)
- **Große Dateien (≥5 GB):** Chunked Upload mit **Resume-Mechanismus**
- Automatische Entscheidung basierend auf Dateigröße
- **Konfigurierbar via .env:**
  - `PCLOUD_RESUME_THRESHOLD_GB=5` (Default: 5 GB)
  - `PCLOUD_RESUME_CHUNK_MB=128` (Default: 128 MB Chunks)

### ✅ Paralleles Threading
- **PCLOUD_UPLOAD_THREADS:** Parallele File-Uploads (aus .env)
- **PCLOUD_FOLDER_THREADS:** Parallele Ordner-Erstellung (aus .env)
- Performance-Tuning für Raspberry Pi 5 oder x86-Systeme

### ✅ Crash-Resistant Resume
- **State-Files** in `/srv/pcloud-archive/resume/` oder `~/.pcloud_resume/`
- Nach Crash: **Automatischer Resume** ab letztem vollständigen Chunk
- Server-Status-Abfrage via `upload_info()` für Offset-Synchronisation

### ✅ Verifikation & Integrität
- **SHA256-Check** nach jedem Upload (lokaler Hash vs. pCloud-Hash)
- **Delta-Check** am Ende: Vergleicht lokal vs. pCloud rekursiv
- Report: `OK`, `MISSING`, `SIZE MISMATCH`, `EXTRA`

### ✅ Robust & Production-Ready
- **Retry-Logik:** 12 Versuche für File-Uploads (wie in `pcloud_push_json_manifest_to_pcloud.py`)
- **Exponential Backoff** bei temporären Fehlern
- **Thread-safe Statistiken** für Live-Monitoring

---

## 📦 Installation

### 1. Dependencies

```bash
# Python 3.7+ (meist vorinstalliert auf Debian/Ubuntu)
python3 --version

# Tool herunterladen
wget https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_simple_upload.py
chmod +x pcloud_simple_upload.py
```

**Wichtig:** `pcloud_bin_lib.py` wird **automatisch heruntergeladen** beim ersten Start!

**Manueller Download (optional):**
```bash
# Falls Auto-Download nicht gewünscht
wget https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_bin_lib.py
```

### 2. .env erstellen

```bash
# Beispiel .env
cat > .env << 'EOF'
# pCloud API-Config (ZWINGEND!)
PCLOUD_TOKEN=your_pcloud_access_token_here
PCLOUD_API_HOST=binapi.pcloud.com
PCLOUD_API_PORT=8398

# Optional: Threading-Tuning
PCLOUD_UPLOAD_THREADS=16
PCLOUD_FOLDER_THREADS=8
EOF

# Permissions setzen
chmod 600 .env
```

**Token generieren:**
1. Gehe zu https://my.pcloud.com → Settings → Security → Apps
2. Erstelle "App Password" oder "OAuth Token"
3. Kopiere Token in `.env`

---

## 📖 Usage

### Basis-Syntax

```bash
python3 pcloud_simple_upload.py \
    --env-file /path/to/.env \
    --source /local/path \
    --destination /pCloud/path
```

### Beispiel 1: Einzelne Datei

```bash
python3 pcloud_simple_upload.py \
    --env-file /opt/apps/pcloud-tools/main/.env \
    --source /data/backup.tar.gz \
    --destination /Backup/archives/
```

**Resultat:**
- Datei wird nach `/Backup/archives/backup.tar.gz` hochgeladen
- SHA256-Check erfolgt automatisch
- Delta-Check bestätigt Upload

### Beispiel 2: Ganzer Ordner rekursiv

```bash
python3 pcloud_simple_upload.py \
    --env-file ~/.pcloud.env \
    --source /data/photos/ \
    --destination /Backup/photos/
```

**Resultat:**
- Alle Dateien in `/data/photos/` werden rekursiv hochgeladen
- Ordner-Struktur wird 1:1 repliziert
- 16 parallele Upload-Threads (falls in .env konfiguriert)

### Beispiel 3: Mit custom lib-path

```bash
python3 pcloud_simple_upload.py \
    --env-file /etc/pcloud/.env \
    --lib-path /usr/local/lib/pcloud_bin_lib.py \
    --source /var/backups/db_dump.sql \
    --destination /Backup/databases/
```

### Beispiel 4: Delta-Check überspringen

```bash
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /data/huge_folder/ \
    --destination /Backup/data/ \
    --no-delta-check
```

**Wann sinnvoll?**
- Sehr große Uploads (>100k Dateien)
- Delta-Check dauert zu lange
- Upload wird manuell verifiziert

---

## ⚙️ Konfiguration (.env)

### Pflicht-Parameter

| Variable | Beschreibung | Beispiel |
|----------|--------------|----------|
| `PCLOUD_TOKEN` | Access Token (von pCloud) | `ABCabc123...` |
| `PCLOUD_API_HOST` | API-Hostname | `binapi.pcloud.com` |
| `PCLOUD_API_PORT` | API-Port (TLS: 8398, Plain: 8388) | `8398` |

### Optional: Performance-Tuning

**Threading:**

| Variable | Default | Empfohlen (RPi 5) | Empfohlen (x86) | Beschreibung |
|----------|---------|-------------------|-----------------|--------------|
| `PCLOUD_UPLOAD_THREADS` | 4 | 16 | 32 | Parallele File-Uploads |
| `PCLOUD_FOLDER_THREADS` | 4 | 8 | 16 | Parallele Ordner-Erstellung |

**Chunked-Upload (für sehr große Dateien):**

| Variable | Default | Beschreibung |
|----------|---------|------------|
| `PCLOUD_RESUME_THRESHOLD_GB` | 5 | Ab dieser Dateigröße wird Chunked Upload verwendet |
| `PCLOUD_RESUME_CHUNK_MB` | 128 | Chunk-Größe (MB) beim Chunked Upload |

**Tuning-Tipps:**
- **Raspberry Pi 5:** 16 Upload-Threads, 8 Folder-Threads (getestet!)
- **x86 Server:** 32-64 Threads möglich (CPU/Bandbreite abhängig)
- **Bottleneck:** Meist pCloud API-Rate-Limits, nicht lokale CPU

### Vollständiges .env-Beispiel

```bash
# pCloud API-Config
PCLOUD_TOKEN=Your_Access_Token_Here_123456789abcdef
PCLOUD_API_HOST=binapi.pcloud.com
PCLOUD_API_PORT=8398

# Threading-Tuning (RPi 5 optimiert)
PCLOUD_UPLOAD_THREADS=16
PCLOUD_FOLDER_THREADS=8

# Chunked-Upload-Konfiguration (optional)
PCLOUD_RESUME_THRESHOLD_GB=5   # Chunked ab 5 GB (default)
PCLOUD_RESUME_CHUNK_MB=128     # 128 MB Chunks (default)

# Optional: Debug-Logging (falls implementiert)
# PCLOUD_DEBUG=1
```

---

## 🔄 Resume-Mechanismus

### Wie funktioniert Resume?

**Bei großen Dateien (≥50 MB):**
1. **Upload-Session erstellen:** `upload_create()` → `uploadid` erhalten
2. **Chunks hochladen:** Jeweils 10 MB Chunks via `upload_write()`
3. **State speichern:** Nach jedem Chunk → `/srv/pcloud-archive/resume/*.state.json`
4. **Bei Crash:** State-File lädt `uploadid` + `offset` → Resume ab letztem Chunk

### State-File-Format

```json
{
  "uploadid": 123456789,
  "offset": 52428800,
  "file_size": 104857600,
  "local_path": "/data/bigfile.bin",
  "remote_path": "/Backup/bigfile.bin",
  "updated_at": 1714234567.89
}
```

### Resume nach Crash

```bash
# Upload startet
python3 pcloud_simple_upload.py --env-file .env \
    --source /data/bigfile.bin --destination /Backup/

# ... Crash bei 50% ...

# Einfach nochmal starten - Resume erfolgt automatisch!
python3 pcloud_simple_upload.py --env-file .env \
    --source /data/bigfile.bin --destination /Backup/
```

**Output:**
```
2026-04-27 21:30:15 [INFO ] ⬆ Upload: bigfile.bin (100.00 MB)
2026-04-27 21:30:15 [INFO ]   → Chunked Upload (Datei ≥50MB)
2026-04-27 21:30:15 [WARN ]   ⚠ Offset-Korrektur: Local 52428800 → Server 52428800
2026-04-27 21:30:15 [INFO ]   Progress: 50% (50.0/100.0 MB)
2026-04-27 21:30:20 [INFO ]   Progress: 60% (60.0/100.0 MB)
...
```

### Server-Status-Synchronisation

**Problem:** State-File sagt "50% hochgeladen", aber Server hat nur 40% erhalten (z.B. Netzwerk-Fehler).

**Lösung:** Tool fragt Server via `upload_info(uploadid)` nach **echtem Offset**:
```python
server_info = pc.upload_info(cfg, uploadid)
server_offset = server_info.get("size", 0)

if server_offset != upload_offset:
    # Korrigiere lokal auf Server-Status
    upload_offset = server_offset
```

**Resultat:** Verlorene Chunks werden neu gesendet, keine Datenkorruption!

### uploadid-Ablauf

**Frage:** Wie lange bleibt `uploadid` gültig?

**Antwort (empirisch):** Ca. 10-30 Minuten nach letztem `upload_write()`.

**Fallback:** Wenn `uploadid` abgelaufen:
```
2026-04-27 21:45:00 [WARN ]   ⚠ uploadid abgelaufen, starte neu
2026-04-27 21:45:00 [INFO ]   → Erstelle neue Upload-Session
```

Tool erkennt automatisch und startet neu von 0%.

---

## 📊 Output & Logging

### Normaler Upload (kleine Datei)

```
2026-04-27 20:00:00 [INFO ] === pCloud Simple Upload ===
2026-04-27 20:00:00 [INFO ] Source: /data/test.txt
2026-04-27 20:00:00 [INFO ] Destination: /Backup/test/
2026-04-27 20:00:00 [INFO ] Upload Threads: 16
2026-04-27 20:00:00 [INFO ] Folder Threads: 8
2026-04-27 20:00:00 [INFO ] API Host: binapi.pcloud.com:8398
2026-04-27 20:00:01 [INFO ] Source: Einzelne Datei (1.23 MB)
2026-04-27 20:00:01 [OK  ] ✓ 0/0 Ordner erstellt
2026-04-27 20:00:01 [INFO ] Starte Upload: 1 Dateien mit 16 Threads...
2026-04-27 20:00:01 [INFO ] ⬆ Upload: test.txt (1.23 MB)
2026-04-27 20:00:02 [OK  ] ✓ test.txt (1.2s)
2026-04-27 20:00:02 [INFO ] 
2026-04-27 20:00:02 [INFO ] === Upload abgeschlossen ===
2026-04-27 20:00:02 [OK  ] Dateien: 1/1 erfolgreich
2026-04-27 20:00:02 [INFO ] Fehler: 0
2026-04-27 20:00:02 [INFO ] Bytes: 1.23 MB / 1.23 MB
2026-04-27 20:00:02 [INFO ] Ordner: 0 erstellt
2026-04-27 20:00:02 [INFO ] Dauer: 2.1s (0.59 MB/s)
2026-04-27 20:00:03 [INFO ] === Delta-Check: Verifikation ===
2026-04-27 20:00:03 [INFO ] Scanne pCloud-Ordner: /Backup/test/
2026-04-27 20:00:04 [OK  ] ✓ 1 Dateien auf pCloud gefunden
2026-04-27 20:00:04 [INFO ] Delta-Check Ergebnisse:
2026-04-27 20:00:04 [OK  ]   ✓ OK: 1/1 Dateien
2026-04-27 20:00:04 [OK  ] ✓ Delta-Check OK - Alle Dateien vollständig auf pCloud!
```

### Ordner-Upload mit Fehlern

```
2026-04-27 20:10:00 [INFO ] Scanne Ordner rekursiv: /data/photos/
2026-04-27 20:10:02 [OK  ] ✓ 247 Dateien gefunden (1234.56 MB total)
2026-04-27 20:10:02 [INFO ] Erstelle 15 Ordner parallel (8 Threads)...
2026-04-27 20:10:03 [OK  ] ✓ 15/15 Ordner erstellt
2026-04-27 20:10:03 [INFO ] Starte Upload: 247 Dateien mit 16 Threads...
2026-04-27 20:10:05 [INFO ] ⬆ Upload: 2024/IMG_001.jpg (3.45 MB)
2026-04-27 20:10:07 [OK  ] ✓ 2024/IMG_001.jpg (2.1s)
2026-04-27 20:10:08 [INFO ] ⬆ Upload: 2024/IMG_002.jpg (4.12 MB)
2026-04-27 20:10:09 [ERROR] ✗ 2024/IMG_002.jpg: Network timeout
...
2026-04-27 20:15:30 [INFO ] === Upload abgeschlossen ===
2026-04-27 20:15:30 [WARN ] Dateien: 245/247 erfolgreich
2026-04-27 20:15:30 [ERROR] Fehler: 2
2026-04-27 20:15:30 [ERROR] Fehler-Details:
2026-04-27 20:15:30 [ERROR]   ✗ 2024/IMG_002.jpg: Network timeout
2026-04-27 20:15:30 [ERROR]   ✗ 2025/IMG_099.jpg: API error 5000: Internal server error
```

### Chunked Upload mit Progress

```
2026-04-27 21:00:00 [INFO ] ⬆ Upload: bigfile.bin (8.50 GB)
2026-04-27 21:00:00 [INFO ]   → Chunked Upload (Datei ≥5.0 GB, Chunk-Size: 128 MB)
2026-04-27 21:00:05 [INFO ]   Progress: 10% (0.85/8.50 GB)
2026-04-27 21:00:10 [INFO ]   Progress: 20% (1.70/8.50 GB)
2026-04-27 21:00:15 [INFO ]   Progress: 30% (2.55/8.50 GB)
...
2026-04-27 21:05:00 [INFO ]   Progress: 100% (8.50/8.50 GB)
2026-04-27 21:05:01 [INFO ] Berechne SHA256-Hash (8.50 GB)...
2026-04-27 21:05:35 [OK  ] ✓ bigfile.bin (335.2s)
```

---

## ✅ Delta-Check

### Was prüft der Delta-Check?

Nach jedem Upload:
1. **Rekursiver Scan** von pCloud-Zielordner
2. **Vergleich** mit lokalen Source-Dateien
3. **Report:**
   - **OK:** Dateien identisch (Size-Match)
   - **MISSING:** Auf pCloud fehlt Datei
   - **SIZE MISMATCH:** Datei vorhanden, aber Größe falsch
   - **EXTRA:** Datei auf pCloud, aber nicht lokal

### Beispiel-Output

```
=== Delta-Check: Verifikation ===
Scanne pCloud-Ordner: /Backup/photos/
✓ 247 Dateien auf pCloud gefunden

Delta-Check Ergebnisse:
  ✓ OK: 245/247 Dateien
  ✗ MISSING auf pCloud: 2 Dateien
    - 2024/IMG_002.jpg
    - 2025/IMG_099.jpg
  ⚠ EXTRA auf pCloud: 1 Dateien (nicht in Source)
    - old_backup/file.txt

⚠ WARNUNG: Upload unvollständig! Fehlende oder inkonsistente Dateien erkannt.
```

**Exit-Code:** `1` (Fehler) bei MISSING/MISMATCH, sonst `0` (OK)

### Delta-Check überspringen

Bei sehr großen Uploads (>100k Dateien) kann Delta-Check lange dauern:

```bash
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /huge/dataset/ \
    --destination /Backup/data/ \
    --no-delta-check
```

**Manueller Delta-Check später:**
```bash
# TODO: Separates Tool erstellen
python3 pcloud_delta_check.py \
    --env-file .env \
    --local /huge/dataset/ \
    --remote /Backup/data/
```

---

## 🔬 SHA256-Verifikation

### Automatischer Hash-Check

Nach jedem erfolgreichen Upload:
1. **Lokaler Hash:** Berechnet vor/nach Upload
2. **Remote Hash:** Via `checksumfile(fileid)` von pCloud abgerufen
3. **Vergleich:** Beide Hashes müssen identisch sein

### Beispiel-Output

```
2026-04-27 20:00:05 [INFO ] Berechne SHA256-Hash (123.4 MB)...
2026-04-27 20:00:10 [OK  ] ✓ bigfile.bin (5.2s)
```

**Bei Mismatch:**
```
2026-04-27 20:00:10 [ERROR] ✗ bigfile.bin: SHA256 Mismatch! 
                            Local: abc123def456... 
                            Remote: xyz789ghi012...
```

**Ursachen für Mismatch:**
- Netzwerk-Korruption während Upload
- pCloud-Server-Fehler (selten)
- Datei wurde lokal während Upload geändert

**Lösung:** Upload wiederholen!

---

## 🛠️ Troubleshooting

### Problem: "FEHLER: pcloud_bin_lib.py nicht gefunden!"

**Ursache:** Tool kann Library nicht finden und Auto-Download ist deaktiviert oder gescheitert.

**Lösung:**
```bash
# Option 1: Auto-Download nutzen (default)
python3 pcloud_simple_upload.py --env-file .env --source /data --destination /Backup/
# → Library wird automatisch heruntergeladen

# Option 2: Manueller Download
wget https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_bin_lib.py

# Option 3: lib-path explizit übergeben
python3 pcloud_simple_upload.py \
    --lib-path /pfad/zu/pcloud_bin_lib.py \
    --env-file .env --source /data --destination /Backup/

# Option 4: PYTHONPATH setzen
export PYTHONPATH=/opt/apps/pcloud-tools/main:$PYTHONPATH
python3 pcloud_simple_upload.py --env-file .env --source /data --destination /Backup/
```

### Problem: Auto-Download schlägt fehl (Firewall, kein Internet)

**Ursache:** Kein Zugriff auf GitHub oder `raw.githubusercontent.com` geblockt.

**Lösung:**
```bash
# Auto-Download deaktivieren + Library von anderem System kopieren
scp user@server:/opt/apps/pcloud-tools/main/pcloud_bin_lib.py .

python3 pcloud_simple_upload.py --no-auto-download \
    --env-file .env --source /data --destination /Backup/
```

### Problem: "FEHLER: PCLOUD_TOKEN nicht gesetzt in .env"

**Ursache:** .env fehlt Token.

**Lösung:**
```bash
# Token in .env eintragen
echo "PCLOUD_TOKEN=Your_Token_Here" >> .env

# Oder: Token von anderem System kopieren
scp user@server:/opt/apps/pcloud-tools/main/.env .
```

### Problem: Upload langsam (nur 1-2 MB/s)

**Ursache:** Zu wenig Threads.

**Lösung:**
```bash
# .env bearbeiten
nano .env

# Threads erhöhen
PCLOUD_UPLOAD_THREADS=16
PCLOUD_FOLDER_THREADS=8

# Erneut starten
python3 pcloud_simple_upload.py --env-file .env --source /data --destination /Backup/
```

**Erwartet (RPi 5):** 4-6 MB/s mit 16 Threads

### Problem: "Offset-Korrektur: Local 50000000 → Server 40000000"

**Was bedeutet das?**
- Tool dachte: 50% hochgeladen
- Server hat aber nur 40% empfangen
- Automatische Korrektur: 10% werden neu gesendet

**Ursache:** Netzwerk-Fehler oder timeout während Chunk-Upload.

**Aktion:** Keine! Tool korrigiert automatisch.

### Problem: Delta-Check zeigt MISSING-Files trotz erfolgreicher Uploads

**Ursache 1:** Upload-Fehler wurden nicht geloggt.

**Lösung:**
```bash
# Fehler-Log prüfen
grep "ERROR" upload.log

# Fehlende Dateien nochmal hochladen
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /data/IMG_002.jpg \
    --destination /Backup/photos/2024/
```

**Ursache 2:** pCloud-Replikations-Delay (sehr selten).

**Lösung:** 5 Minuten warten, Delta-Check wiederholen.

### Problem: "uploadid abgelaufen, starte neu" nach jeder Pause

**Ursache:** uploadid ist nur 10-30 Minuten gültig.

**Verhalten:** Normal! Tool startet automatisch neu.

**Optimierung:** Bei sehr großen Dateien (>10 GB):
- Chunk-Size erhöhen (weniger API-Calls)
- Schnellere Internetverbindung nutzen

---

## 🎯 Use Cases

### Use Case 1: Manuelle Backups

```bash
# Tägliches Backup von Dokumenten
python3 pcloud_simple_upload.py \
    --env-file ~/.pcloud.env \
    --source ~/Documents/ \
    --destination /Backup/Documents-$(date +%Y%m%d)/
```

### Use Case 2: Ad-hoc-Upload auf fremdem System

```bash
# Auf neuem Debian-Server (nur Python 3 vorhanden)
wget https://raw.githubusercontent.com/lastphoenx/pcloud-tools/main/pcloud_simple_upload.py

# .env mit Token erstellen
cat > .env << 'EOF'
PCLOUD_TOKEN=Your_Token_Here
PCLOUD_API_HOST=binapi.pcloud.com
PCLOUD_API_PORT=8398
EOF

# Upload starten - pcloud_bin_lib.py wird automatisch heruntergeladen!
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /var/backups/db_dump.sql.gz \
    --destination /Backup/databases/
```

**Output:**
```
2026-04-27 20:00:00 [WARN ] ⚠ pcloud_bin_lib.py nicht gefunden in:
2026-04-27 20:00:00 [WARN ]   - /root/pcloud_bin_lib.py
2026-04-27 20:00:00 [INFO ] Lade pcloud_bin_lib.py herunter von GitHub...
2026-04-27 20:00:00 [INFO ]   URL: https://raw.githubusercontent.com/...
2026-04-27 20:00:00 [INFO ]   Ziel: /root/pcloud_bin_lib.py
2026-04-27 20:00:01 [OK  ] ✓ Download erfolgreich!
2026-04-27 20:00:01 [OK  ] ✓ pcloud_bin_lib geladen: /root/pcloud_bin_lib.py
```

### Use Case 3: Große Datei mit Resume

```bash
# 50 GB Datei hochladen
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /data/huge_archive.tar.gz \
    --destination /Backup/archives/

# ... bei 30% crashed ...

# Einfach nochmal starten - Resume erfolgt automatisch ab 30%!
python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /data/huge_archive.tar.gz \
    --destination /Backup/archives/
```

### Use Case 4: Testing auf RPi

```bash
# Performance-Test mit verschiedenen Thread-Counts
for threads in 4 8 16 32; do
  echo "Testing with $threads threads..."
  
  # .env anpassen
  sed -i "s/PCLOUD_UPLOAD_THREADS=.*/PCLOUD_UPLOAD_THREADS=$threads/" .env
  
  # Test-Upload
  time python3 pcloud_simple_upload.py \
    --env-file .env \
    --source /data/test_500mb/ \
    --destination /Backup/test_$threads/
done
```

---

## 📈 Performance-Tuning

### Raspberry Pi 5

**Empfohlene Config (.env):**
```bash
PCLOUD_UPLOAD_THREADS=16
PCLOUD_FOLDER_THREADS=8
```

**Erwartete Performance:**
- Kleine Dateien (<1 MB): **4-6 MB/s** (Bottleneck: pCloud API)
- Große Dateien (>50 MB): **8-12 MB/s** (Chunked, Netzwerk-limitiert)
- CPU-Last: **~30-40%** (Load Average ~1.5)
- RAM: **~200-400 MB**

### x86 Server (z.B. Intel Xeon)

**Empfohlene Config (.env):**
```bash
PCLOUD_UPLOAD_THREADS=32
PCLOUD_FOLDER_THREADS=16
```

**Erwartete Performance:**
- Kleine Dateien: **10-20 MB/s**
- Große Dateien: **20-50 MB/s** (Bandbreite-limitiert)
- CPU-Last: **~10-20%**
- RAM: **~500 MB - 1 GB**

### Tuning-Matrix

| Hardware | Upload-Threads | Folder-Threads | Erwartete Speed |
|----------|----------------|----------------|-----------------|
| RPi 4 (4 GB) | 8 | 4 | 2-4 MB/s |
| RPi 5 (8 GB) | 16 | 8 | 4-6 MB/s |
| x86 (2 Cores) | 16 | 8 | 8-15 MB/s |
| x86 (4+ Cores) | 32 | 16 | 15-30 MB/s |
| x86 (8+ Cores, 1 Gbit) | 64 | 32 | 30-60 MB/s |

**Wichtig:** pCloud hat Account-basierte Rate-Limits! Bei >64 Threads oft keine weiteren Speedups.

---

## 🔒 Security & Best Practices

### .env-Permissions

```bash
# Nur Owner kann lesen/schreiben
chmod 600 .env

# Nie in Git committen!
echo ".env" >> .gitignore
```

### Token-Rotation

```bash
# Regelmäßig neuen Token generieren (alle 6-12 Monate)
# Alten Token widerrufen: pCloud Settings → Security → Apps → Revoke
```

### Production-Setup

```bash
# .env in geschütztem Verzeichnis
sudo mkdir -p /etc/pcloud
sudo cp .env /etc/pcloud/
sudo chmod 600 /etc/pcloud/.env
sudo chown root:root /etc/pcloud/.env

# Tool mit absolutem Pfad
python3 /opt/apps/pcloud-tools/pcloud_simple_upload.py \
    --env-file /etc/pcloud/.env \
    --source /data/ \
    --destination /Backup/
```

---

## 🆚 Unterschied zu anderen Tools

| Feature | `pcloud_simple_upload.py` | `pcloud_push_json_manifest_to_pcloud.py` |
|---------|----------------------------|-------------------------------------------|
| **Zweck** | Ad-hoc Uploads | Production Backups mit Deduplizierung |
| **Manifest** | ❌ Nein | ✅ Ja (für Gap-Handling) |
| **Deduplizierung** | ❌ Nein | ✅ Ja (via Index) |
| **Resume** | ✅ Ja (Chunked) | ✅ Ja (Chunked + Manifest) |
| **Threading** | ✅ Ja | ✅ Ja |
| **Delta-Check** | ✅ Ja (nach Upload) | ✅ Ja (via Index) |
| **Dependencies** | Minimal | Komplex (MariaDB, Manifest-Tools) |
| **Use Case** | Testing, manuelle Backups | Automated daily backups |

**Wann welches Tool?**
- **`pcloud_simple_upload.py`:** Ad-hoc, Testing, fremde Systeme
- **`pcloud_push_json_manifest_to_pcloud.py`:** Production, Daily Backups, Gap-Handling

---

## 📚 Siehe auch

- [DEVELOPER_GUIDE.md](./DEVELOPER_GUIDE.md) - Vollständige Architektur-Dokumentation
- [pcloud_bin_lib.py](../pcloud_bin_lib.py) - API-Library
- [TESTING.md](./TESTING.md) - Test-Strategie

---

## 🐛 Known Issues

**Issue 1: Delta-Check bei >100k Dateien langsam**
- **Status:** Known Limitation
- **Workaround:** `--no-delta-check` verwenden

**Issue 2: uploadid-Timeout bei sehr langsamen Verbindungen**
- **Status:** pCloud API-Limitation (10-30 Min)
- **Workaround:** Tool startet automatisch neu

---

## 🚀 Roadmap

**Geplante Features:**
- [ ] `--parallel-chunked` für mehrere große Dateien gleichzeitig
- [ ] `--bandwidth-limit` für Rate-Limiting
- [ ] `--exclude` Pattern für File-Filtering
- [ ] `--dry-run` für Test ohne Upload
- [ ] JSON-Report-Export für Monitoring

---

## 📝 Changelog

### v1.0.0 (2026-04-27)
- ✅ Initial Release
- ✅ Chunked Upload mit Resume
- ✅ Threading (UPLOAD_THREADS, FOLDER_THREADS)
- ✅ SHA256-Verifikation
- ✅ Delta-Check
- ✅ Production-ready

---

**Happy Uploading!** 🚀
