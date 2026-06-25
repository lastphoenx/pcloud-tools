# pCloud-Tools – Umgebungsvariablen Referenz

Vollständige Dokumentation aller ENV-Variablen zur Performance-Optimierung und Konfiguration der pCloud-Tools.

---

## 📋 Inhaltsverzeichnis

- [Threading & Parallelität](#threading--parallelität)
- [File Size Thresholds](#file-size-thresholds)
- [Chunking & Resume](#chunking--resume)
- [Connection & Timeouts](#connection--timeouts)
- [Caching & Pfade](#caching--pfade)
- [RAM & Integritaet](#-ram--integritaet)
- [Debugging & Features](#debugging--features)
- [Empfohlene Konfigurationen](#empfohlene-konfigurationen)

---

## ⚡ Threading & Parallelität

### `PCLOUD_UPLOAD_THREADS`
**Beschreibung:** Anzahl paralleler Threads für File-Uploads (kleine Dateien < 50MB).  
**Default:** `4`  
**Empfohlen:** `16` (für moderne Server mit guter Anbindung)  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_UPLOAD_THREADS=16
```

### `PCLOUD_DOWNLOAD_THREADS`
**Beschreibung:** Anzahl paralleler Threads für File-Downloads (kleine Dateien).  
**Default:** `16`  
**Empfohlen:** `16-32` (abhängig von Netzwerk)  
**Verwendet in:** `pcloud_restore.py`

```bash
PCLOUD_DOWNLOAD_THREADS=16
```

### `PCLOUD_FOLDER_THREADS`
**Beschreibung:** Anzahl paralleler Threads für Ordner-Operationen (`copyfolder`, `delete_folder`).  
**Default:** `4`  
**Empfohlen:** `8` (zu viele Threads können zu API-Rate-Limits führen)  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_FOLDER_THREADS=8
```

### `PCLOUD_STUB_THREADS`
**Beschreibung:** Anzahl paralleler Threads für Stub-Writes (JSON-Metadaten).  
**Default:** `4`  
**Empfohlen:** `8` (Balance zwischen Speed und API-Stabilität)  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_STUB_THREADS=8
```

---

## 📏 File Size Thresholds

### `PCLOUD_SMALL_FILE_THRESHOLD_MB`
**Beschreibung:** Upload – Ab welcher Größe (MB) ein File als "groß" gilt und sequentiell verarbeitet wird.  
**Default:** `50` MB  
**Empfohlen:** `50` MB  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_SMALL_FILE_THRESHOLD_MB=50
```

### `PCLOUD_DOWNLOAD_SMALL_THRESHOLD`
**Beschreibung:** Download – Ab welcher Größe (Bytes!) ein File als "groß" gilt.  
**Default:** `52428800` (50 MB in Bytes)  
**Empfohlen:** `52428800` (50 MB)  
**Verwendet in:** `pcloud_restore.py`

```bash
PCLOUD_DOWNLOAD_SMALL_THRESHOLD=52428800
```

### `PCLOUD_RESUME_THRESHOLD_GB`
**Beschreibung:** Ab welcher Dateigröße (GB) Chunked-Resume aktiv wird.  
**Default:** `5` GB  
**Empfohlen:** `5-10` GB (abhängig von Stabilität)  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_RESUME_THRESHOLD_GB=5
```

---

## 🔄 Chunking & Resume

### `PCLOUD_CHUNK_THRESHOLD`
**Beschreibung:** Ab welcher Dateigröße (Bytes) Chunking verwendet wird.  
**Default:** `104857600` (100 MB)  
**Empfohlen:** `104857600` (100 MB)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_CHUNK_THRESHOLD=104857600
```

### `PCLOUD_CHUNK_SIZE`
**Beschreibung:** Chunk-Größe für Upload/Download (Bytes).  
**Default:** `5242880` (5 MB)  
**Empfohlen:** `5242880-10485760` (5-10 MB)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_CHUNK_SIZE=5242880
```

### `PCLOUD_RESUME_CHUNK_MB`
**Beschreibung:** Chunk-Größe (MB) für SHA256-Verifikation bei Resume.  
**Default:** `128` MB  
**Empfohlen:** `128` MB  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_RESUME_CHUNK_MB=128
```

### `PCLOUD_CHUNK_RETRIES`
**Beschreibung:** Anzahl Wiederholungsversuche pro Chunk bei Fehler.  
**Default:** `8`  
**Empfohlen:** `8-12` (höher bei instabiler Verbindung)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_CHUNK_RETRIES=8
```

### `PCLOUD_CHUNK_DELAY`
**Beschreibung:** Delay (Sekunden) zwischen Chunks (Rate-Limiting).  
**Default:** `0.15` s  
**Empfohlen:** `0.1-0.2` s  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_CHUNK_DELAY=0.15
```

### `PCLOUD_RESUME_CLEANUP`
**Beschreibung:** Automatisches Löschen alter Resume-States aktivieren.  
**Default:** `1` (aktiviert)  
**Empfohlen:** `1`  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_RESUME_CLEANUP=1
```

### `PCLOUD_RESUME_CLEANUP_DAYS`
**Beschreibung:** Alter (Tage) ab dem Resume-States gelöscht werden.  
**Default:** `7` Tage  
**Empfohlen:** `7-14` Tage  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_RESUME_CLEANUP_DAYS=7
```

---

## 🌐 Connection & Timeouts

### `PCLOUD_TIMEOUT`
**Beschreibung:** API-Timeout in Sekunden (einzelner REST/Binary-Request).  
**Default:** `30` s (Lib), oft `60` in `.env`  
**Empfohlen:** `60-120` s (höher bei großen Operationen)  
**Verwendet in:** `pcloud_bin_lib.py`, `pcloud_push_json_manifest_to_pcloud.py`

`delete_file(..., size_bytes=N)` skaliert den Timeout für große Löschungen automatisch (bis 600s) — kein separates GC-Tuning nötig.

```bash
PCLOUD_TIMEOUT=60
```

### `PCLOUD_HOST`
**Beschreibung:** pCloud API-Server (überschreibt .env).  
**Default:** Aus `.env` Profil  
**Empfohlen:** `api.pcloud.com` oder `eapi.pcloud.com` (EU)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_HOST=api.pcloud.com
```

### `PCLOUD_PORT`
**Beschreibung:** pCloud API-Port (überschreibt .env).  
**Default:** Aus `.env` Profil  
**Empfohlen:** `443` (HTTPS)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_PORT=443
```

### `PCLOUD_TOKEN`
**Beschreibung:** pCloud Access Token (überschreibt .env).  
**Default:** Aus `.env` Profil  
**Empfohlen:** In `.env` speichern (nicht in ENV setzen)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
# Besser in .env speichern!
PCLOUD_TOKEN=yourtoken
```

### `PCLOUD_DEVICE`
**Beschreibung:** pCloud Device-ID (überschreibt .env).  
**Default:** Aus `.env` Profil  
**Empfohlen:** In `.env` speichern  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_DEVICE=yourdevice
```

### `PCLOUD_PROFILE`
**Beschreibung:** Profil-Name aus `.env` (z.B. `[backup]`).  
**Default:** `[default]`  
**Empfohlen:** Separate Profile für verschiedene Accounts  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_PROFILE=backup
```

### `PCLOUD_ENV_FILE`
**Beschreibung:** Pfad zur `.env` Datei.  
**Default:** `./.env`  
**Empfohlen:** Absoluter Pfad bei systemd-Services  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_ENV_FILE=/opt/apps/pcloud-tools/main/.env
```

---

## 📁 Caching & Pfade

### `PCLOUD_ARCHIVE_DIR`
**Beschreibung:** Archiv-Verzeichnis für Manifests, Master-Index, Deltas, Resume.  
**Default:** `/srv/pcloud-archive`  
**pi-nas (verifiziert):** Bind-Mount auf `/dev/sdd1` (SSD2) — nicht Micro-SD, nicht `/srv/nas/pcloud-archive`  
**Verwendet in:** Wrapper, `pcloud_push_json_manifest_to_pcloud.py`, Verify/Audit-Tools

```bash
PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive
```

Siehe `docs/STORAGE_PATHS.md`.

### `PCLOUD_DEST`
**Beschreibung:** Remote Pool-Root auf pCloud (`/_pool`, `/_snapshots` darunter).  
**Default (Pool-Wrapper):** `/Backup/rtb_pool` — wird im Wrapper gesetzt, fehlt oft in `.env`  
**Wichtig:** Bei manuellem `source .env` ohne Wrapper **explizit setzen**, sonst landen Delta-Checks unter `/_snapshots` (API-Fehler 2002)  
**Verwendet in:** `wrapper_pcloud_pool_sync_1to1.sh`, `pcloud_quick_delta.py`, `pool_verify_backup.py`

```bash
PCLOUD_DEST=/Backup/rtb_pool
```

### `PCLOUD_TEMP_DIR`
**Beschreibung:** Temp-Verzeichnis für Manifeste und Index-Checkpoints während Upload.  
**Default (Wrapper):** `/tmp` falls unset; **pi-nas `.env`:** `/srv/pcloud-temp` (Bind-Mount SSD2)  
**Verwendet in:** Wrapper, `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_TEMP_DIR=/srv/pcloud-temp
```

### `PCLOUD_MANIFEST_SKIP_GLOBS`
**Beschreibung:** Komma-getrennte Glob-Patterns — beim Pool-Manifest-Scan (`pcloud_json_pool_manifest.py`) werden passende Dateien übersprungen (nicht in Manifest/Index).  
**Default:** `**/._*` (AppleDouble)  
**Optional pi-nas:** `**/__pycache__/**,**/*.pyc` — analog `rtb/excludes.txt`, reduziert Manifest-Größe  
**Verwendet in:** `pcloud_path_compat.py`, `pcloud_json_pool_manifest.py`

```bash
# PCLOUD_MANIFEST_SKIP_GLOBS=**/__pycache__/**,**/*.pyc
```

Siehe `docs/STORAGE_PATHS.md` § RTB vs. Pipeline (Manifest-Scan-Zeile).

### `PCLOUD_FOLDERID_CACHE`
**Beschreibung:** Cache-Datei für FolderID-Lookups (beschleunigt `ensure_parent_dirs`).  
**Default:** `/tmp/pcloud_folderid_cache.json`  
**Empfohlen:** Default OK (wird automatisch aufgebaut)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_FOLDERID_CACHE=/tmp/pcloud_folderid_cache.json
```

### `PCLOUD_FIDCACHE_TTL`
**Beschreibung:** Cache-TTL (Sekunden) für FolderID-Cache.  
**Default:** `3600` (1 Stunde)  
**Empfohlen:** `3600-7200` (1-2 Stunden)  
**Verwendet in:** `pcloud_bin_lib.py`

```bash
PCLOUD_FIDCACHE_TTL=3600
```

---

## 🧠 RAM & Integritaet

### `PCLOUD_POST_UPLOAD_INTEGRITY`
**Beschreibung:** Post-Upload-Integritaetscheck via `pool_integrity_run.py` (ein Snapshot, DB-Tracking).  
**Default:** `1` (aktiv)  
**Werte:** `1` / `skip` / `0` / `off`  
**Verwendet in:** `wrapper_pcloud_pool_sync_1to1.sh`

Ersetzt den früheren `pcloud_quick_delta`-Lauf nach Upload (OOM-Risiko auf 8GB-Pi).

```bash
PCLOUD_POST_UPLOAD_INTEGRITY=1
# Notfall:
# PCLOUD_POST_UPLOAD_INTEGRITY=skip
```

### `PCLOUD_POST_UPLOAD_DELTA` (deprecated)
Nicht mehr vom Wrapper verwendet. Tamper-Detect nur noch manuell:

```bash
python pcloud_quick_delta.py --dest-root /Backup/rtb_pool --snapshots SNAP
```

---

## 🐛 Debugging & Features

### `PCLOUD_VERBOSE`
**Beschreibung:** Verbose-Modus aktivieren (detaillierte Logs).  
**Default:** `0` (deaktiviert)  
**Empfohlen:** `1` nur zum Debuggen (viel Output!)  
**Verwendet in:** `pcloud_bin_lib.py`, `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_VERBOSE=1
```

### `PCLOUD_TIMING`
**Beschreibung:** Timing-Stats nach Upload ausgeben.  
**Default:** `0` (deaktiviert)  
**Empfohlen:** `1` für Performance-Analyse  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_TIMING=1
```

### `PCLOUD_PRETTY_JSON`
**Beschreibung:** JSON-Dateien formatiert (human-readable) schreiben.  
**Default:** `0` (kompakt)  
**Empfohlen:** `0` (spart Platz), `1` nur zum Debuggen  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_PRETTY_JSON=0
```

### `PCLOUD_STUB_PROGRESS_INTERVAL`
**Beschreibung:** Progress-Meldung bei Stub-Writes (alle N Stubs).  
**Default:** `500`  
**Empfohlen:** `500-1000`  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
PCLOUD_STUB_PROGRESS_INTERVAL=500
```

### `PCLOUD_API_RETRIES`
**Beschreibung:** Metrik-Zähler für API-Retries (nur Logging).  
**Default:** `0`  
**Empfohlen:** `0` (automatisch)  
**Verwendet in:** `pcloud_push_json_manifest_to_pcloud.py`

```bash
# Automatisch gesetzt, nicht manuell ändern
PCLOUD_API_RETRIES=0
```

---

## 🎯 Empfohlene Konfigurationen

### **Produktion (Server mit guter Anbindung)**
Optimiert für **Geschwindigkeit** und **Stabilität**.

```bash
# === Threading & Parallelität ===
PCLOUD_UPLOAD_THREADS=16
PCLOUD_DOWNLOAD_THREADS=16
PCLOUD_FOLDER_THREADS=8
PCLOUD_STUB_THREADS=8

# === Thresholds ===
PCLOUD_SMALL_FILE_THRESHOLD_MB=50
PCLOUD_DOWNLOAD_SMALL_THRESHOLD=52428800
PCLOUD_RESUME_THRESHOLD_GB=10

# === Connection ===
PCLOUD_TIMEOUT=120
PCLOUD_FIDCACHE_TTL=7200

# === Pfade ===
PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive
PCLOUD_TEMP_DIR=/tmp

# === Debugging (aus) ===
PCLOUD_VERBOSE=0
PCLOUD_TIMING=0
PCLOUD_PRETTY_JSON=0

# === Resume ===
PCLOUD_RESUME_CLEANUP=1
PCLOUD_RESUME_CLEANUP_DAYS=7
```

---

### **Entwicklung / Testing**
Optimiert für **Debugging** und **Nachvollziehbarkeit**.

```bash
# === Threading (reduziert für bessere Logs) ===
PCLOUD_UPLOAD_THREADS=4
PCLOUD_DOWNLOAD_THREADS=4
PCLOUD_FOLDER_THREADS=2
PCLOUD_STUB_THREADS=2

# === Debugging (an) ===
PCLOUD_VERBOSE=1
PCLOUD_TIMING=1
PCLOUD_PRETTY_JSON=1

# === Connection ===
PCLOUD_TIMEOUT=60

# === Resume ===
PCLOUD_RESUME_CLEANUP=0  # Manuelles Cleanup für Analyse
```

---

### **Instabile Verbindung / Remote**
Optimiert für **Robustheit** bei schlechter Netzwerkqualität.

```bash
# === Threading (konservativ) ===
PCLOUD_UPLOAD_THREADS=8
PCLOUD_DOWNLOAD_THREADS=8
PCLOUD_FOLDER_THREADS=4
PCLOUD_STUB_THREADS=4

# === Thresholds (kleinere Files parallel) ===
PCLOUD_SMALL_FILE_THRESHOLD_MB=20
PCLOUD_RESUME_THRESHOLD_GB=2

# === Chunking (robuster) ===
PCLOUD_CHUNK_SIZE=5242880  # 5MB
PCLOUD_CHUNK_RETRIES=12
PCLOUD_CHUNK_DELAY=0.2

# === Connection (längere Timeouts) ===
PCLOUD_TIMEOUT=180

# === Resume (aktiver Cleanup) ===
PCLOUD_RESUME_CLEANUP=1
PCLOUD_RESUME_CLEANUP_DAYS=3
```

---

## 📊 Performance-Tipps

### **Upload-Optimierung**
1. **Viele kleine Dateien**: `PCLOUD_UPLOAD_THREADS=16-32`
2. **Große Dateien**: Threads reduzieren, Chunking optimieren
3. **Folder-Template**: `PCLOUD_FOLDER_THREADS=8` (nicht höher!)

### **Download-Optimierung**
1. **Bandbreite voll ausnutzen**: `PCLOUD_DOWNLOAD_THREADS=32`
2. **RAM schonen**: `PCLOUD_DOWNLOAD_SMALL_THRESHOLD` erhöhen
3. **Resume-Logik**: Größen-Check ist schneller als SHA256

### **Cache-Optimierung**
1. **FolderID-Cache**: `PCLOUD_FIDCACHE_TTL=7200` (2h) spart API-Calls
2. **Archive-Dir**: Auf SSD/NVMe für schnelle Manifest-Lookups
3. **Temp-Dir**: SSD für Index-Updates während Upload

---

## ⚠️ Wichtige Hinweise

1. **Thread-Limits**: Zu viele Threads → pCloud API-Rate-Limit!  
   → Empfohlen: Max. 32 Threads gesamt über alle Operationen

2. **Byte-Werte**: `PCLOUD_DOWNLOAD_SMALL_THRESHOLD` erwartet **Bytes**, nicht MB!  
   → 50 MB = `52428800` Bytes

3. **Token-Sicherheit**: `PCLOUD_TOKEN` **nie** in systemd-Units hardcoden!  
   → Immer in `.env` speichern und via `PCLOUD_ENV_FILE` laden

4. **Resume-Cleanup**: Bei instabilen Verbindungen kann `PCLOUD_RESUME_CLEANUP=0` helfen  
   → Manuelle Analyse möglich, aber Platz im Temp-Dir!

5. **Verbose-Modus**: `PCLOUD_VERBOSE=1` produziert **sehr viel** Output  
   → Nur temporär zum Debuggen aktivieren

---

## 🔗 Siehe auch

- [pcloud_restore.md](../scripts/utilities/pcloud_restore.md) – Download-Tool Dokumentation
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) – Architektur & Code-Struktur
- [CONFIG.md](CONFIG.md) – .env Profil-Konfiguration

---

**Letzte Aktualisierung:** April 2026  
**Maintainer:** pCloud-Tools Team
