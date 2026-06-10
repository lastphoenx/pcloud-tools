# Pool-Mode Test-Anleitung

## a) TEST MIT EINZELNEM SNAPSHOT

### Variante 1: Kompletter Snapshot (empfohlen für echten Test)

```bash
# Auf pi-nas Server:
cd /opt/apps/pcloud-tools/main

# 1. Wähle einen existierenden Snapshot
SNAP=/mnt/backup/rtb_nas/2026-05-24-200014
# oder neuesten:
# SNAP=$(readlink -f /mnt/backup/rtb_nas/latest)

# 2. Manifest generieren (Pool-Mode, Schema v4)
python pcloud_json_pool_manifest.py \
  --root "$SNAP" \
  --snapshot $(basename "$SNAP") \
  --out /srv/pcloud-temp/test_pool.json \
  --hash sha256

# 3. Upload DRY-RUN (zeigt was passieren würde)
python pcloud_push_json_pool_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/test_pool.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode pool \
  --env-file .env \
  --dry-run

# 4. Echte Upload (wenn dry-run OK)
python pcloud_push_json_pool_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/test_pool.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode pool \
  --env-file .env

# 5. Verifiziere im pCloud Web-Interface:
#    https://my.pcloud.com/#page=filemanager&folder=...
#    Suche: /Backup/rtb_1to1/_pool/ und /Backup/rtb_1to1/_snapshots/
```

### Variante 2: Test-Snapshot mit einzelnem Ordner (minimalistisch)

```bash
# Erstelle Test-Snapshot mit nur einem Ordner
mkdir -p /tmp/test_snapshot/home/user
cp -a /mnt/backup/rtb_nas/latest/home/user/Documents /tmp/test_snapshot/home/user/
# oder ein kleineres Verzeichnis:
cp -a /mnt/backup/rtb_nas/latest/etc /tmp/test_snapshot/

# Manifest + Upload wie oben
python pcloud_json_pool_manifest.py \
  --root /tmp/test_snapshot \
  --snapshot test-$(date +%Y-%m-%d-%H%M%S) \
  --out /srv/pcloud-temp/test_mini.json \
  --hash sha256

python pcloud_push_json_pool_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/test_mini.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode pool \
  --env-file .env \
  --dry-run

# Cleanup nach Test
rm -rf /tmp/test_snapshot
```

### Variante 3: Upload-Only mit existierendem Snapshot (RTB überspringen)

```bash
# Nutze rtb_pool_wrapper.sh mit --upload-only
bash /opt/apps/rtb/rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-05-24-200014
```

---

## b) REMOTE-SPEICHERORTE (pCloud)

### Basis-Root: `/Backup/rtb_1to1`

### 1. POOL (deduplizierte Files)
```
📁 /Backup/rtb_1to1/_pool/
   📁 00/
      📄 0000123abc456def789... (SHA256 = Filename, echte Datei)
      📄 0001456789abcdef012...
   📁 01/
      📄 0100789abcdef123456...
   ...
   📁 ff/
      📄 ff89abcdef012345678...
```

**Struktur:**
- Prefix-Ordner: `00` bis `ff` (ersten 2 Zeichen des SHA256)
- Filename: Kompletter SHA256-Hash (64 Zeichen Hex)
- Inhalt: ECHTE Datei (kein Stub!)
- Deduplizierung: Nur 1× pro SHA256, egal wie viele Snapshots

**Beispiel:**
```
Datei: /home/user/document.txt (SHA256: abc123def456...)
Pool: /Backup/rtb_1to1/_pool/ab/abc123def456...
```

### 2. SNAPSHOTS (Stub-Ordnerstruktur)
```
📁 /Backup/rtb_1to1/_snapshots/
   📁 2026-05-24-200014/
      📄 .upload_started
      📄 .upload_complete
      📁 home/
         📁 user/
            📄 document.txt.meta.json    ← STUB (1 KB JSON)
            📄 photo.jpg.meta.json       ← STUB
      📁 etc/
         📄 hosts.meta.json              ← STUB
   📁 2026-05-25-120426/
      📄 .upload_started
      📄 .upload_complete
      📁 home/
         📁 user/
            📄 document.txt.meta.json    ← STUB (zeigt auf Pool-File)
            📄 newfile.txt.meta.json     ← STUB
```

**Struktur:**
- Snapshot-Name = Ordner-Name (z.B. `2026-05-24-200014`)
- Original-Ordnerstruktur erhalten (readable!)
- Files = `.meta.json` Stubs (zeigen auf Pool-File)
- Marker: `.upload_started`, `.upload_complete`

**Stub-Inhalt (document.txt.meta.json):**
```json
{
  "type": "pool_stub",
  "sha256": "abc123def456...",
  "pcloud_hash": "...",
  "size": 12345678,
  "mtime": 1717000000.0,
  "relpath": "home/user/document.txt",
  "pool_path": "/_pool/ab/abc123def...",
  "pool_fileid": 87654321,
  "snapshot": "2026-05-24-200014"
}
```

### 3. CONTENT-INDEX (Metadaten)
```
📁 /Backup/rtb_1to1/
   📄 content_index.json    ← Master-Index (200-400 MB)
```

**Content-Index Struktur (Schema v4):**
```json
{
  "schema": 4,
  "mode": "pool",
  "updated_at": 1717000000.0,
  "updated_by": "push_pool_mode",
  "snapshots": [
    "2026-05-24-200014",
    "2026-05-25-120426",
    "2026-05-26-040014"
  ],
  "pool_refs": {
    "abc123def456...": ["2026-05-24-200014", "2026-05-25-120426"],
    "cdef9876543...": ["2026-05-24-200014"]
  },
  "nodes": {
    "abc123def456...": {
      "sha256": "abc123def456...",
      "pcloud_hash": "...",
      "size": 12345678,
      "holders": [
        {"snapshot": "2026-05-24-200014", "relpath": "home/user/document.txt"},
        {"snapshot": "2026-05-25-120426", "relpath": "home/user/document.txt"}
      ]
    }
  }
}
```

### QUOTA-VERBRAUCH (Beispiel 20 Snapshots, 100 GB Daten)

**1to1-Mode (ALT):**
```
100 GB × 20 Snapshots = 2000 GB Quota (!)
Problem: Jeder Snapshot = Full Copy
```

**Pool-Mode (NEU):**
```
100 GB (Pool) + 20 MB (Stubs) = 100.02 GB Quota
Benefit: Nur 1× pro File, 20× weniger Quota!
```

---

## c) RTB-WRAPPER MIT POOL-PIPELINE

### Neue Datei: `rtb_pool_wrapper.sh`

**Erstellt:** ✅ `c:\Users\tsant\OneDrive\Dokumente\vsc\github_code\rtb\rtb_pool_wrapper.sh`

**Änderungen:**
1. Header dokumentiert Pool-Mode
2. `PCLOUD_WRAPPER` zeigt auf `wrapper_pcloud_pool_sync_1to1.sh`
3. Log-Messages erwähnen "POOL-MODE"

**Deployment auf pi-nas:**
```bash
# 1. Code holen
cd /opt/apps/pcloud-tools/main
git pull  # Holt d78b4b9 (Performance-Fixes)

# 2. RTB-Wrapper kopieren
sudo cp rtb_pool_wrapper.sh /opt/apps/rtb/
sudo chmod +x /opt/apps/rtb/rtb_pool_wrapper.sh

# 3. Test (dry-run implizit via --check-only)
sudo /opt/apps/rtb/rtb_pool_wrapper.sh --check-only

# 4. Upload-Only Test (nutzt existierenden Snapshot)
sudo /opt/apps/rtb/rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-05-24-200014

# 5. Voller Backup-Lauf (RTB + Pool-Upload)
sudo /opt/apps/rtb/rtb_pool_wrapper.sh
```

### SYSTEMD-TIMER (für automatische Pool-Backups)

**Option A: Neue Service/Timer-Einheit (empfohlen für Koexistenz)**
```bash
# Erstelle neue Systemd-Unit
sudo nano /etc/systemd/system/backup-pipeline-pool.service
```

```ini
[Unit]
Description=Backup Pipeline POOL-MODE (RTB + pCloud Pool-Upload)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/opt/apps/rtb/rtb_pool_wrapper.sh
StandardOutput=journal
StandardError=journal
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

**Timer:**
```bash
sudo nano /etc/systemd/system/backup-pipeline-pool.timer
```

```ini
[Unit]
Description=Backup Pipeline POOL-MODE Timer (4× täglich)

[Timer]
OnCalendar=*-*-* 04:00:14
OnCalendar=*-*-* 12:04:26
OnCalendar=*-*-* 20:00:14
Persistent=true

[Install]
WantedBy=timers.target
```

**Aktivierung:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable backup-pipeline-pool.timer
sudo systemctl start backup-pipeline-pool.timer

# Status prüfen
sudo systemctl status backup-pipeline-pool.timer
sudo systemctl list-timers backup-pipeline-pool.timer
```

**Option B: Alte Unit ersetzen (nur Pool-Mode nutzen)**
```bash
# Stoppe alte Pipeline
sudo systemctl stop backup-pipeline.timer
sudo systemctl disable backup-pipeline.timer

# Update Service-Unit
sudo nano /etc/systemd/system/backup-pipeline.service
# Ändere: ExecStart=/opt/apps/rtb/rtb_pool_wrapper.sh

sudo systemctl daemon-reload
sudo systemctl enable backup-pipeline.timer
sudo systemctl start backup-pipeline.timer
```

### MIGRATION: 1to1-Mode → Pool-Mode

**⚠️ WICHTIG: Beide Modi können PARALLEL laufen!**

**Szenario 1: Fresh Start (nur Pool-Mode)**
```bash
# 1. Alte 1to1-Snapshots löschen (optional)
# WARNUNG: Nur wenn du sicher bist!
# pCloud Web-Interface: /Backup/rtb_1to1/_snapshots/* löschen

# 2. Starte Pool-Pipeline
sudo /opt/apps/rtb/rtb_pool_wrapper.sh
```

**Szenario 2: Parallel-Betrieb (Test-Phase)**
```bash
# 1to1-Mode läuft weiter: backup-pipeline.timer (alt)
# Pool-Mode parallel: backup-pipeline-pool.timer (neu)
# 
# Beide schreiben in unterschiedliche Ordner:
# - 1to1: /Backup/rtb_1to1/_snapshots/  (alte Struktur)
# - Pool: /Backup/rtb_1to1/_pool/ + /_snapshots/ (neue Struktur)
#
# Quota: Verdoppelt während Test-Phase!
# Nach Test: 1to1-Snapshots löschen
```

**Szenario 3: Schrittweise Migration**
```bash
# 1. Pool-Mode Test mit neuen Snapshots
sudo /opt/apps/rtb/rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/latest

# 2. Wenn stabil: alte 1to1-Snapshots schrittweise löschen
# pCloud Web: Älteste Snapshots erst löschen

# 3. Nach X Tagen: Alle 1to1-Snapshots weg, nur noch Pool
```

---

## CHECKLISTE: ERSTER POOL-TEST

### Pre-Test (Vorbereitung)
- [ ] `git pull` auf pi-nas (holt d78b4b9)
- [ ] `.env` prüfen (PCLOUD_USER, PCLOUD_PASS gesetzt)
- [ ] MariaDB läuft (für DB-Tracking)
- [ ] Backup-Lock frei (`/run/backup_pipeline.lock`)

### Test-Durchlauf
- [ ] Snapshot wählen: `/mnt/backup/rtb_nas/2026-05-24-200014`
- [ ] Manifest generieren: `pcloud_json_pool_manifest.py`
- [ ] Upload DRY-RUN: `--dry-run` (zeigt Plan)
- [ ] Upload ECHT: ohne `--dry-run`

### Verification (pCloud Web)
- [ ] Pool-Files existieren: `/Backup/rtb_1to1/_pool/XX/[sha256]`
- [ ] Stub-Snapshot existiert: `/Backup/rtb_1to1/_snapshots/2026-05-24-200014/`
- [ ] Marker vorhanden: `.upload_complete`
- [ ] Stub-Inhalt prüfen: Download `.meta.json` → JSON valid

### Performance-Check
- [ ] Upload-Rate: ~10-15 files/s (mit 8 Workers)
- [ ] Log zeigt Parallel-Upload: "8 Workers"
- [ ] Progress alle 5s im Log
- [ ] Keine Errors/Timeouts

### Post-Test
- [ ] DB-Eintrag: `SELECT * FROM backup_runs WHERE snapshot_name='2026-05-24-200014'`
- [ ] Status = SUCCESS
- [ ] Delta-Check: `pcloud_quick_delta.py` (optional)

### Gap-Backfill (nach erfolgreichem Test)
- [ ] Verbleibende 6 Snapshots uploaden
- [ ] Wrapper nutzen: `wrapper_pcloud_pool_sync_1to1.sh`
- [ ] Oder: rtb_pool_wrapper.sh mit `--upload-only`

---

## ⚠️ SICHERHEIT: KOLLISIONS-VERMEIDUNG

### Marker-basierte Sicherheit

**Beide Modi (1to1 + Pool) prüfen `.upload_complete` Marker:**
- ✅ Wenn Marker existiert → Upload wird übersprungen
- ✅ Verhindert Doppel-Uploads für denselben Snapshot

**ABER: Marker dokumentiert NICHT den Mode!**

### Risiko-Szenario

```
# 1to1-Snapshot existiert
/_snapshots/2026-05-24-200014/
  ├─ .upload_complete      ← 1to1 Marker (ohne Mode-Info!)
  └─ home/user/file.txt    ← Echte Datei

# Wenn Marker gelöscht wird + Pool-Mode läuft:
/_snapshots/2026-05-24-200014/
  ├─ file.txt              ← Echte Datei (1to1)
  └─ file.txt.meta.json    ← Stub (Pool) = CHAOS!
```

### ✅ EMPFOHLENE VORGEHENSWEISE

**Option 1: Separate Dest-Roots (SICHERSTE)**
```bash
# 1to1-Mode (alt, läuft aus)
--dest-root /Backup/rtb_1to1_legacy

# Pool-Mode (neu, produktiv)
--dest-root /Backup/rtb_1to1_pool

# KEIN Konflikt möglich! ✓
```

**Option 2: Sequentielle Migration**
```bash
# Phase 1: Nur 1to1-Mode (Status Quo)
# Phase 2: Stop 1to1-Timer
sudo systemctl stop backup-pipeline.timer
# Phase 3: Start Pool-Timer
sudo systemctl start backup-pipeline-pool.timer
# Phase 4: Alte 1to1-Snapshots löschen (nach X Tagen)
```

**Option 3: Marker mit Mode-Indikator (Code-Fix)**
```python
# Started-Marker bereits hat Mode-Info:
{
  "snapshot": "2026-05-24-200014",
  "started_at": 1717000000.0,
  "mode": "pool"  # ← Pool-Mode schreibt dies!
}

# TODO: Complete-Marker sollte auch Mode haben
# TODO: Beim Prüfen auch Mode validieren
```

### 🔐 ZUSÄTZLICHE SICHERHEIT

**1. Lock-Mechanismus** (bereits vorhanden)
```bash
# Beide Wrapper nutzen dasselbe Lock
/run/backup_pipeline.lock

# Verhindert: 1to1 + Pool gleichzeitig!
```

**2. DB-Status prüfen** (rtb_wrapper.sh macht das)
```sql
SELECT status FROM backup_runs 
WHERE snapshot_name='2026-05-24-200014' 
ORDER BY run_id DESC LIMIT 1;

# Wenn SUCCESS → Skip Upload
```

**3. Snapshot-Name Convention**
```bash
# Optional: Mode-Prefix im Snapshot-Namen
1to1:  2026-05-24-200014         (Status Quo)
Pool:  pool-2026-05-24-200014    (Neue Convention)

# Verhindert Name-Kollisionen!
```

### ✅ AKTUELLE EMPFEHLUNG FÜR DICH

**Für ersten Test (minimales Risiko):**
```bash
# 1. Wähle Snapshot der NICHT auf pCloud ist
SNAP=/mnt/backup/rtb_nas/2026-05-24-200014

# 2. Prüfe ob auf pCloud existiert
python pcloud_quick_delta.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env | grep 2026-05-24-200014

# 3. Wenn NICHT auf pCloud → Safe zum Testen!
python pcloud_push_json_pool_manifest_to_pcloud.py ...
```

**Für Produktiv-Betrieb:**
```bash
# Nutze separaten Dest-Root (SICHERSTE Option)
export PCLOUD_DEST=/Backup/rtb_1to1_pool

# Oder: Stop 1to1-Mode komplett
sudo systemctl stop backup-pipeline.timer
```

---

## TROUBLESHOOTING

### Problem: "pcloud_bin_lib konnte nicht importiert werden"
```bash
# Python-Path prüfen
export PYTHONPATH=/opt/apps/pcloud-tools/main:$PYTHONPATH
python -c "import pcloud_bin_lib; print('OK')"
```

### Problem: "Lock timeout"
```bash
# Lock-File prüfen
ls -l /run/backup_pipeline.lock
# Falls blockiert:
sudo flock -u /run/backup_pipeline.lock  # oder neustarten
```

### Problem: Upload sehr langsam (<5 files/s)
```bash
# Worker erhöhen (mehr Parallelität)
export PCLOUD_POOL_WORKERS=16
python pcloud_push_json_pool_manifest_to_pcloud.py ...
```

### Problem: API Rate-Limit (429 Errors)
```bash
# Worker reduzieren
export PCLOUD_POOL_WORKERS=4
python pcloud_push_json_pool_manifest_to_pcloud.py ...
```

### Problem: Stub-Datei fehlt nach Upload
```bash
# Verbose-Log aktivieren
export PCLOUD_VERBOSE=1
python pcloud_push_json_pool_manifest_to_pcloud.py ...

# Prüfe Log: [stub] ✓ Einträge
```

---

## NEXT STEPS

1. **✅ Test durchführen** (siehe oben)
2. **🚀 Pool-GC einrichten** (wöchentlich)
   ```bash
   # Crontab
   0 3 * * 0 cd /opt/apps/pcloud-tools/main && python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env >> /var/log/backup/pool_gc.log 2>&1
   ```
3. **📊 Monitoring** (Dashboard Integration)
4. **🔄 Migration planen** (1to1 → Pool)
5. **✅ Restore-Script** (`scripts/utilities/pool_restore.py` — siehe `pool_restore.md` + SETUP.md §10)
