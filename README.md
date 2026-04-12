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

Diagnostic & Repair Tools:
  pcloud_quick_delta.py                      Fast tamper detection (2s for 20k files)
  pcloud_repair_index.py                     Clean phantom holders from index

Helper Tools:
  pcloud_bin_lib.py                          Binary client library for pCloud API
  wrapper_pcloud_sync_1to1.sh                Shell wrapper for backup automation

Umgebungsvariablen (.env):
  PCLOUD_USER                                pCloud username
  PCLOUD_PASS                                pCloud password
  PCLOUD_REGION                              Region (eu|us)
  LOCAL_BACKUP_ROOT                          Local source directory
  PCLOUD_BACKUP_ROOT                         Cloud destination directory
  PCLOUD_TEMP_DIR                            Temporary files (index, manifests, delta reports)
                                             Default: /tmp (recommend: /srv/pcloud-temp on SSD)
  PCLOUD_ARCHIVE_DIR                         Long-term manifest archive after successful upload
                                             Default: /srv/pcloud-archive
  PCLOUD_ARCHIVE_INDEX                       Archive index files (set to "1" to enable)
```

## Features

* **JSON-Manifest-Architektur** - Snapshots bestehen aus Metadaten + Verweise auf File-Pool

* **Content-based Deduplication** - SHA256-basiert: gleiche Datei wird nur einmal gespeichert

* **Space Efficiency** - Wie `rsync --hard-links`, aber in der Cloud

* **Full Restore** - Rekonstruiert komplette Backups aus Manifests + File-Pool

* **Integrity Check** - Verifiziert Cloud-Backups gegen SHA256-Hashes

* **Fast Tamper Detection** - 2-second delta check via single API call (vs. hours with traditional methods)

* **Automated Index Repair** - Clean phantom holders after interrupted uploads

* **Incremental Uploads** - Nur neue/geänderte Dateien werden hochgeladen

* **Python Standard Library** - Keine externen API-Wrapper nötig

* **Automation-Ready** - Shell-Wrapper für systemd-Timer Integration

## Technical Improvements (April 2026)

Recent optimizations have significantly improved reliability, performance, and robustness:

### Network & Upload Stability
* **DNS Caching** - Reduces DNS resolution overhead for repeated API calls
* **HTTP Keep-Alive Sessions** - Persistent connections reduce TCP handshake latency
* **Chunked Upload for Large Files** - Files >100MB use multi-part upload (5MB chunks by default)
  - Automatic retry per chunk (8 attempts with exponential backoff)
  - Session refresh on connection errors (prevents stale socket issues)
  - Proper `Content-Type: application/octet-stream` headers
* **Correct pCloud API Usage**:
  - `upload_create`: Only `access_token` parameter
  - `upload_write`: Binary data with proper headers
  - `upload_save`: Uses `folderid` + `name` (not `path`)

### Resume & Recovery
* **Index-Driven Skip Logic** - Maintains local index (`/tmp/pcloud_index_<snapshot>.json`) with successfully uploaded files
  - On restart: Automatically skips already-uploaded files
  - Safe for multi-TB uploads over days/weeks
  - Configurable index save interval (default: every 100 files via `--index-save-interval`)
* **Incremental Index Updates** - Index saved during upload (not just at end)
* **Upload to pCloud on Success** - Index uploaded to snapshot folder after successful completion

### Security & Privacy
* **Token Scrubbing** - Access tokens automatically removed from exception messages (regex-based)
  - Prevents accidental token leaks in logs/error reports
  - Applied to HTTP errors and connection exceptions

### Performance Tuning
* **Configurable Timeouts** - Adjustable via `PCLOUD_TIMEOUT` or `PCLOUD_TIMEOUT_SECS` env vars
* **Chunk Parameters**:
  - `PCLOUD_CHUNK_SIZE` - Chunk size in bytes (default: 5MB)
  - `PCLOUD_CHUNK_RETRIES` - Retry attempts per chunk (default: 8)
  - `PCLOUD_CHUNK_DELAY` - Delay between chunks in seconds (default: 0.15s)
  - `PCLOUD_CHUNK_THRESHOLD` - File size threshold for chunked upload (default: 100MB)
* **Batch Folder Creation** - Minimizes API calls when creating directory structures:
  - Single recursive `listfolder()` call fetches all existing remote folders
  - Diff against manifest folders to identify only missing directories
  - Creates only missing folders (sorted by depth for parent-first creation)
  - `PCLOUD_FOLDER_CREATE_SLEEP` - Rate limiting between folder creation (default: 0.05s)
* **Directory Caching** - In-memory cache (`_KNOWN_DIRS`) prevents redundant folder existence checks

### Resume Capability
* **Automatic Resume on Restart** - Local index file `/tmp/pcloud_index_<snapshot>.json` tracks uploaded files
  - On restart: Automatically detects and resumes incomplete uploads
  - Skips already uploaded files (no re-upload waste)
  - Safe for multi-TB uploads over days/weeks
* **Index Save Intervals**:
  - `PCLOUD_INDEX_SAVE_INTERVAL=100` - Save index every N files (default: 100)
  - `PCLOUD_INDEX_SAVE_INTERVAL_TIME=300` - OR save every N seconds (default: 300 = 5 min)
  - Whichever trigger fires first wins (hybrid approach)
* **Command Line Options**:
  ```bash
  # Resume: Just re-run the same command
  python pcloud_push_json_manifest_to_pcloud.py \
    --manifest /path/to/snapshot.json \
    --dest-root /Backup/rtb_1to1 \
    --snapshot-mode 1to1 \
    --env-file .env
  
  # Optional: Clean up old snapshots before upload (1to1 mode only)
  --retention-sync
  
  # Test run without actual upload
  --dry-run
  ```

### Logging & Visibility
* **Progress Indicators** - Real-time upload progress for large files
* **Verbose Mode** - `PCLOUD_VERBOSE=1` shows per-chunk progress
* **Better Error Messages** - Clearer diagnostics for network/API failures

### Example Configuration
```bash
# .env or environment
PCLOUD_CHUNK_SIZE=$((5 * 1024 * 1024))    # 5MB chunks
PCLOUD_CHUNK_RETRIES=8                     # 8 retry attempts
PCLOUD_CHUNK_DELAY=0.15                    # 150ms between chunks
PCLOUD_TIMEOUT=300                         # 5min timeout
PCLOUD_VERBOSE=1                           # Show detailed logs
```

**Real-world Impact:** Successfully uploaded 19,808 files / 89.66 GB including 910MB video files that previously failed with connection errors.

### April 2026 Hardening & Production-Readiness (Commits: bfdd8b3, aa1cdcb)

Recent hardening improvements ensure robust long-term operation:

#### **Logging & Observability**
* **RTB-Style Timestamps** - All log output now includes timestamps (`2026-04-12 16:35:19 [push] ...`)
  - Applies to: `pcloud_json_manifest.py`, `pcloud_push_json_manifest_to_pcloud.py` 
  - `_log()` helper function replaces raw `print()` calls
  - **Progress Tracking** - Upload progress now timestamped for post-mortem analysis
    - Before: `[push] 4902/19811 (25%) | 13.08/89.66 GB (15%) | ~34min verbleibend`
    - After: `2026-04-12 17:00:17 [push] 4902/19811 (25%) | 13.08/89.66 GB (15%) | ~34min verbleibend`
  - **Why:** Essential for debugging long-running uploads ("When exactly did the rate drop?")

#### **Robustness Improvements**
* **Retry Logic for Stub-Writes** - Graceful failure handling with 5 retry attempts (exponential backoff)
  - `call_with_backoff()` wraps stub JSON writes (30s max sleep between attempts)
  - Continues on failure (logs error, counts in stats)
  - Final output: `19810/19810 Stubs erfolgreich (100.0%)`
* **Folder Creation 2004-Handling** - Parallel folder creation no longer crashes on "already exists" errors
  - Fallback to `stat_folderid_fast()` when `ensure_path()` throws API Error 2004
  - Allows 4-thread parallelism without race conditions
  - ~4× speedup (1,101 folders in ~5min 20s)
* **Timeout Protection** - Default timeout increased from 30s → 60s for mass-upload scenarios
  - Configurable via `cfg["timeout"]` in `pcloud_push_json_manifest_to_pcloud.py`
* **Stub Write Error Statistics** - Thread-safe counter tracks failed stub writes
  - Displayed at end: `[stubs] Fehler-Statistik: 19810/19810 (100.00%)` (graceful degradation)

#### **Bug Fixes (Critical)**
* **Manifest Write Bug** (Commit: dab62de) - `json.dump()` was missing after refactoring
  - **Symptom:** `FileNotFoundError: pcloud_mani.*.json` (manifest created in RAM but never written to disk)
  - **Fix:** Restored `json.dump(payload, f)` after stats output
* **total_files Scope Bug** (Commit: bfdd8b3) - Variable only defined in `if ref_cache:` block
  - **Symptom:** `NameError: name 'total_files' is not defined` in Full-Mode
  - **Fix:** Calculate `total_files` before if-block, stats output only when `ref_cache` exists
* **trap RETURN Trap** (Commit: bfdd8b3) - Shell trap deleted manifests on any Python error
  - **Symptom:** Manifest deleted immediately after Python crash → `FileNotFoundError` in push script
  - **Fix:** Removed `trap 'rm -f "$mani"' RETURN`, replaced with explicit cleanup at function end
* **Latest-Symlink Bug** (Commit: b661514) - Cleanup script pointed to deleted snapshot instead of previous
  - **Symptom:** `Latest → 2026-04-12-141849` (snapshot being deleted) instead of `2026-04-12-121042`
  - **Fix:** `grep -v "^$SNAPSHOT_NAME$"` filters deleted snapshot from list before `tail -1`

#### **Operational Improvements**
* **Paritäts-Cleanup for Manifests** (Commit: aa1cdcb) - Automatic manifest deletion during retention sync
  - **Problem:** RTB retention deletes old snapshots → Remote snapshot deleted via `retention_sync` → Local manifest orphaned in `/srv/pcloud-archive/manifests/`
  - **Solution:** `retention_sync_1to1()` now deletes manifest immediately after `pc.delete_folder()`
  - **Code:**
    ```python
    pc.delete_folder(cfg, path=rmpath, recursive=True)
    manifest_file = f"{PCLOUD_ARCHIVE_DIR}/manifests/{snapshot}.json"
    if os.path.exists(manifest_file):
        os.remove(manifest_file)
        print(f"[retention] Manifest gelöscht: {snapshot}.json")
    ```
  - **Why:** 1:1 parity (snapshot deleted = manifest deleted), no orphans, no extra cronjob, Smart-Mode safety
* **Intelligent Gap-Backfilling** (Commit: 84d907d) - Automatic upload of missing snapshots
  - **Problem:** Old logic only uploaded `latest` snapshot → If pCloud blocked for days, intermediate snapshots never uploaded → Gaps in cloud history after RTB retention
  - **Solution:** Wrapper now iterates ALL local snapshots, checks each against remote, uploads missing ones chronologically (old → new)
  - **Self-Healing:** Gaps automatically filled on next successful run (e.g., pCloud down 4 days → 4 snapshots backfilled)
  - **Efficient:** Smart-Mode deduplication → subsequent snapshots only upload stubs (~5min each vs. 20min full upload)
  - **Example Output:**
    ```bash
    [check] Prüfe auf fehlende Snapshots...
    [gap] Snapshot 2026-04-08-120000 fehlt remote – hole nach...
    [gap] Snapshot 2026-04-09-120000 fehlt remote – hole nach...
    [gap] Snapshot 2026-04-10-120000 fehlt remote – hole nach...
    [done] 3 Snapshot(s) hochgeladen
    ```
  - **Why:** Ensures complete cloud history, immune to temporary pCloud outages/blockages

#### **Smart-Manifest Performance** (Commit: 8b16b1c)
* **Schema v3 with mtime/size Cache** - 600× speedup via reference manifest
  - Full-Mode: 20min (19,811 SHA256 calculations)
  - Smart-Mode: ~2s (19,810 cached, 1 new file)
  - Cache hit rate: 99.99% for incremental backups
* **Parallel Folder Creation** (Commit: bd7f4eb) - 4 threads per depth level
  - Sequential: ~55s for 1,101 folders
  - Parallel: ~14s (4× speedup)
  - Depth-based sorting prevents parent-before-child errors

**Production Status:** All critical bugs fixed, hardening features active, tested with 19,810 files / 89.66 GB uploads.

## Diagnostic & Repair Tools

### Fast Tamper Detection (`pcloud_quick_delta.py`)

**Purpose:** Verify cloud backup integrity in seconds using a single API call.

**How it works:**
- Fetches remote folder structure via one recursive `listfolder` call (2-3s for 20k files)
- Compares against local `content_index.json` (hash-based deduplication index)
- Detects missing files, hash mismatches, size discrepancies, and unknown files

**Use cases:**
- Quick tamper detection after upload (verify all files reached pCloud)
- Pre-backup validation (ensure remote state matches local index)
- Identify phantom index entries (files marked as uploaded but missing on remote)

**Example:**
```bash
# Quick delta check
python pcloud_quick_delta.py \
  --snapshot 2026-04-10-075334 \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --json-out /srv/pcloud-temp/delta.json

# Output:
# ✅ 15,028 files OK (hash + fileid + size match)
# ❌ 2,900 missing anchors (in index but not on pCloud)
# ⚠️ 0 hash gaps (file exists but no pcloud_hash in index)
# 🔍 2 unknown files (on pCloud but not in index)
```

**Performance:** ~2 seconds for 20k files (vs. traditional checksumfile verification: hours)

**Output formats:**
- Console summary (colored diff report)
- JSON export (`--json-out`) for automation/repair workflows

---

### Index Repair (`pcloud_repair_index.py`)

**Purpose:** Clean phantom holder entries from local index based on delta report.

**How it works:**
- Reads delta report from `pcloud_quick_delta.py`
- Removes holders for missing files (phantom entries from interrupted uploads)
- Preserves valid holders for existing files
- Saves cleaned index to `/srv/pcloud-temp/pcloud_index_<snapshot>.json`

**Use cases:**
- Fix index after interrupted upload (resume upload will skip valid files, re-upload missing)
- Clean up after partial upload failures
- Prepare index for re-run without re-uploading entire snapshot

**Example:**
```bash
# Step 1: Detect issues
python pcloud_quick_delta.py \
  --snapshot 2026-04-10-075334 \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --json-out /srv/pcloud-temp/delta.json

# Step 2: Repair index
python pcloud_repair_index.py \
  --delta-report /srv/pcloud-temp/delta.json \
  --env-file .env

# Output:
# 🔧 Processing 17,928 index nodes...
# ❌ Removed 2,900 phantom holders
# 🗑️ Deleted 2,703 nodes with no remaining holders
# ✅ Cleaned index: 15,225 nodes, 16,908 holders
# 💾 Saved to: /srv/pcloud-temp/pcloud_index_2026-04-10-075334.json

# Step 3: Resume upload with cleaned index
python pcloud_push_json_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/pcloud_mani.2026-04-10-075334.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode 1to1 \
  --env-file .env
  
# Upload will automatically use cleaned index from /srv/pcloud-temp/
# Result: uploaded=2,703 (missing files), resumed=16,908 (valid files)
```

**Safety:**
- Operates on local copy (original index on pCloud untouched)
- Dry-run available via `--dry` flag
- Cleaned index written to separate file for review before upload

---

### Diagnostic Workflow

**Scenario:** Upload reports success but integrity check shows missing files

```bash
# 1. Quick delta check (2s)
python pcloud_quick_delta.py \
  --snapshot 2026-04-10-075334 \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --json-out /srv/pcloud-temp/delta.json

# 2. Repair index (removes phantom holders)
python pcloud_repair_index.py \
  --delta-report /srv/pcloud-temp/delta.json \
  --env-file .env

# 3. Resume upload (only missing files uploaded)
python pcloud_push_json_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/pcloud_mani.2026-04-10-075334.json \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode 1to1 \
  --env-file .env
```

**Key metrics:**
- Delta check: 2 seconds (single API call)  
- Index repair: 274 lines, instant execution
- Upload resume: Skips 16,908 valid files, uploads 2,703 missing (90% time saved)

**Comparison to alternatives:**
| Method | Time | API Calls | Accuracy |
|--------|------|-----------|----------|
| `pcloud_quick_delta.py` | 2s | 1 | Hash + Fileid + Size |
| Traditional checksumfile | 2+ hours | 20,000+ | SHA256 only |
| Manual index rebuild | N/A | N/A | Loses dedup info |

---

## Maintenance Scripts

### Cleanup Orphaned Manifests (`cleanup_orphaned_manifests.sh`)

**Purpose:** Find and delete "orphaned" manifests - JSON files without corresponding RTB snapshots. Useful after manual snapshot deletions or as a safety-net for retention issues.

**When to use:**
- After manual RTB interventions (snapshots deleted by hand)
- After retention runs (monthly sanity check)
- Debugging (when manifest counts look suspicious)
- Migration/cleanup (one-time removal of old test data)

**Note:** As of commit `aa1cdcb`, parity cleanup runs automatically during `retention_sync`. This script is only needed for:
- Legacy cleanup (manifests created before automatic parity cleanup)
- Manual interventions (snapshots deleted outside of normal retention)
- Debugging (verify manifest-snapshot parity)

**How it works:**
1. Lists all `.json` files in `/srv/pcloud-archive/manifests/`
2. For each manifest: checks if RTB snapshot exists in `/mnt/backup/rtb_nas/<TIMESTAMP>/`
3. If snapshot missing → manifest is "orphaned" → delete (or dry-run)
4. Reports: Total manifests, valid (snapshot exists), orphaned (snapshot missing)

**Advantages:**
- ✅ One-time cleanup for "zombie manifests"
- ✅ Ad-hoc maintenance after manual changes
- ✅ Safety-net (debugging tool)
- ✅ Dry-run mode (safe testing with `--dry-run`)
- ✅ Shows which manifests are affected

**Disadvantages:**
- ⚠️ Extra script to maintain
- ⚠️ Can mask problems (better: fix root cause)
- ⚠️ One-time solution (not for automation)

**Usage:**
```bash
# Test run (shows what would be deleted)
./cleanup_orphaned_manifests.sh --dry-run

# Actually delete orphaned manifests
./cleanup_orphaned_manifests.sh
```

**Example output:**
```
═══════════════════════════════════════════════════════════
Orphaned Manifest Cleanup
═══════════════════════════════════════════════════════════
RTB-Snapshots:  /mnt/backup/rtb_nas
Manifeste:      /srv/pcloud-archive/manifests

  ✓ Gelöscht: 2026-04-05-120000.json (orphan)
  ✓ Gelöscht: 2026-04-08-143000.json (orphan)

═══════════════════════════════════════════════════════════
✓ Cleanup abgeschlossen
═══════════════════════════════════════════════════════════
Gesamt:   5 Manifeste
Valid:    3 (RTB-Snapshot existiert)
Orphaned: 2 (RTB-Snapshot fehlt)

✓ 2 orphaned Manifeste gelöscht
```

**Recommended frequency:** 
- Monthly (after retention runs)
- Ad-hoc (after manual interventions)
- Not needed for normal operation (automatic parity cleanup handles this)

---

### Cleanup Aborted Upload (`cleanup_aborted_upload.sh`)

**Purpose:** Clean up artifacts after a failed or aborted backup upload (e.g., Ctrl+C during upload, script crash).

**What it cleans:**
- RTB snapshot directory (`/mnt/backup/rtb_nas/<SNAPSHOT>/`)
- Latest symlink (resets to previous successful snapshot)
- Manifest file (if created in `/srv/pcloud-archive/manifests/`)
- Index cache (if created in `/srv/pcloud-temp/`)
- Remote snapshot (optional with `--remote` flag)

**Usage:**
```bash
# Test run (shows what would be deleted)
./cleanup_aborted_upload.sh 2026-04-12-161214 --dry-run

# Local cleanup only (keeps remote snapshot for manual inspection)
./cleanup_aborted_upload.sh 2026-04-12-161214

# Full cleanup (includes remote pCloud snapshot)
./cleanup_aborted_upload.sh 2026-04-12-161214 --remote
```

**Key features:**
- ✅ Mandatory snapshot name argument (no dangerous defaults)
- ✅ `--dry-run` mode (safety first)
- ✅ `--remote` flag (opt-in remote deletion)
- ✅ Latest-symlink handling (critical bug fix in `b661514`)
- ✅ Verbose output (shows what's being cleaned)

**Safety:** Snapshot name is required (no hardcoded defaults). After 3 weeks, you'd have to remember the exact name - prevents accidental deletions.

---

## Production Workflow: Real-World Delta-Check & Repair

This section documents a complete real-world scenario that occurred during production use, including troubleshooting steps and lessons learned.

### Scenario: 197 Missing Files After "Successful" Upload

**Symptom:**
- Upload reported success: `uploaded=2703, resumed=16908, stubs=0`
- But integrity check revealed **197 missing files**
- Files existed in earlier snapshot, had `holders=2` (multi-holder scenario)

**Root Cause:**
```
Missing anchor: /snap1/fileA
Index state:
  holders = [
    {snapshot: "2026-04-10-075334", relpath: "your/path/to/file.pdf"},
    {snapshot: "2026-03-15-120000", relpath: "your/path/to/file.pdf"}
  ]
  anchor_path = "/Backup/rtb_1to1/_snapshots/2026-04-10-075334/your/path/to/file.pdf"

Problem:
1. Upload interrupted → anchor physically deleted from pCloud
2. First repair removed holder for 2026-04-10-075334
3. BUT kept anchor_path (because 2026-03-15 holder still existed)
4. Push tool saw anchor_path → assumed file exists → skipped upload
```

**The Fix (committed 71ff563):**
```python
# pcloud_repair_index.py now checks:
if anchor_path in missing_anchors:
    del node["anchor_path"]
    del node["fileid"]
    del node["pcloud_hash"]
# Independent of remaining holder count!
```

---

### Complete Repair Workflow (Step-by-Step)

**Step 1: Delta Check (Detect Issues)**
```bash
python pcloud_quick_delta.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --json-out /srv/pcloud-temp/delta.json
```

**Real Output:**
```
=== ERGEBNIS: tamper-detect ===
  Geprüfte Index-Nodes:    17,928
  Davon OK:                17,731
  Fehlende Anchors:        197      ← PROBLEM!

  [MISSING] /Backup/.../your/patth/to/your.pdf (fid=None, holders=2)
  [MISSING] /Backup/.../your/patth/with_folders/.../your.jpg (fid=13....56, holders=2)
  ... und 195 weitere

✗ 199 ABWEICHUNG(EN) GEFUNDEN
[timing] Gesamtlaufzeit: 1.9s
```

**Analysis:**
- `fid=None` → File was NEVER uploaded
- `fid=92438268051` → File exists somewhere, but NOT at anchor_path
- `holders=2` → Multi-holder scenario (same content in 2 snapshots)

---

**Step 2: Verify Index Repair Plan**
```bash
# Check how many nodes will lose anchor_path
python -c "
import json
with open('/srv/pcloud-temp/delta.json') as f:
    delta = json.load(f)
    
missing = len(delta.get('missing_anchors', []))
print(f'Files to repair: {missing}')
print('Sample missing files:')
for m in delta['missing_anchors'][:3]:
    print(f\"  {m['anchor_path']}\")
"
```

**Output:**
```
Files to repair: 197
Sample missing files:
  /Backup/rtb_1to1/_snapshots/2026-04-10-075334/your/folder/document.pdf
  /Backup/rtb_1to1/_snapshots/2026-04-10-075334/your/path/to/file.pdf
  /Backup/rtb_1to1/_snapshots/2026-04-10-075334/your/subfolder/.../image.jpg
```

---

**Step 3: Repair Index**
```bash
python pcloud_repair_index.py \
  --delta-report /srv/pcloud-temp/delta.json \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env
```

**Real Output:**
```
=== pcloud_repair_index ===
[phase 1] 197 fehlende Anchors gefunden
[phase 2] Index geladen: 17,928 Nodes

[phase 3] Repariere Index...
[phase 3] Holders entfernt:     197
[phase 3] Nodes bereinigt:      197    ← anchor_path deleted!
[phase 3] Nodes komplett gelöscht: 0   ← Nodes preserved (SHA256 key)

[phase 4] Speichere reparierten Index lokal...
[phase 4] Index gespeichert: /srv/pcloud-temp/pcloud_index_2026-04-10-075334.json

[summary] Nodes vorher: 17,928
[summary] Nodes nachher: 17,928
[summary] Delta: 0 Nodes entfernt

✓ Reparatur abgeschlossen
```

**Critical Verification:**
```bash
# Verify anchor_paths were actually deleted
python -c "
import json
with open('/srv/pcloud-temp/pcloud_index_2026-04-10-075334.json') as f:
    idx = json.load(f)

items = idx.get('items', {})
null_anchors = sum(1 for node in items.values() if node.get('anchor_path') is None)
total_nodes = len(items)

print(f'Nodes mit anchor_path=None: {null_anchors}/{total_nodes}')

# Sample: Show 3 nodes without anchor_path
count = 0
for sha, node in items.items():
    if node.get('anchor_path') is None and count < 3:
        print(f'  SHA: {sha[:12]}... holders={len(node.get(\"holders\", []))} anchor={node.get(\"anchor_path\")}')
        count += 1
"
```

**Output:**
```
Nodes mit anchor_path=None: 197/17,928  ← PERFECT!
  SHA: f4ed29b081a7... holders=1 anchor=None
  SHA: 06be446b2cc6... holders=1 anchor=None
  SHA: d4cbd79fbcb5... holders=1 anchor=None
```

**Why holders=1?** One holder was removed (the missing anchor), the other holder remains (from older snapshot).

---

**Step 4: Delete Upload Markers (Important!)**
```bash
python -c "
import sys
sys.path.insert(0, '/opt/apps/pcloud-tools/main')
import pcloud_bin_lib as pc

cfg = pc.effective_config(env_file='.env')
pc.delete_file(cfg, path='/Backup/rtb_1to1/_snapshots/2026-04-10-075334/.upload_complete')
pc.delete_file(cfg, path='/Backup/rtb_1to1/_snapshots/2026-04-10-075334/.upload_started')
print('✓ Marker gelöscht')
"
```

**Why?** `.upload_complete` blocks re-runs even if files are missing!

---

**Step 5: Resume Upload (With Repaired Index)**
```bash
MANI=$(ls -t /srv/pcloud-temp/pcloud_mani.2026-04-10-075334.*.json | head -1)
echo "Using manifest: $MANI"

python pcloud_push_json_manifest_to_pcloud.py \
  --manifest "$MANI" \
  --dest-root /Backup/rtb_1to1 \
  --snapshot-mode 1to1 \
  --env-file .env
```

**Real Output:**
```
[resume] Lade lokalen Index: /srv/pcloud-temp/pcloud_index_2026-04-10-075334.json
[push] Starte Upload: 19,808 Dateien, 89.66 GB

[push] 2,898/19,808 (15%) | uploaded=156 resumed=2,741 stubs=0
[push] 3,004/19,808 (15%) | uploaded=189 resumed=2,814 stubs=0

[timing] index_write_ms=1053
[archive] Manifest archiviert: /srv/pcloud-archive/manifests/2026-04-10-075334.json
[success] Upload-Complete-Marker gesetzt

1to1: uploaded=197 resumed=19,611 stubs=0 (snapshot=2026-04-10-075334)
```

**Common Question:** *"Why uploaded=156 at 15% if only 197 total?"*

**Answer:** Upload processes ALL 19,808 files sequentially! The manifest is sorted alphabetically:
- Position 2,898: 15% through manifest (files starting with A-G)
- 197 missing files are spread across entire alphabet
- At 15%, only 156 of the 197 have been encountered
- Remaining (~41) will be uploaded as upload continues to end

**Final Statistics:**
```
uploaded=197      ← Exactly the missing files!
resumed=19,611    ← Already uploaded files skipped
stubs=0          ← No hardlinks
Total: 19,808    ← 100% complete
```

---

**Step 6: Final Verification**
```bash
python pcloud_quick_delta.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --json-out /srv/pcloud-temp/delta_verify.json
```

**Success Output:**
```
=== ERGEBNIS: tamper-detect ===
  Geprüfte Index-Nodes:    17,928
  Davon OK:                17,928    ← 100%!

  Fehlende Anchors:        0         ← SUCCESS!
  FileID-Abweichungen:     0
  Hash-Abweichungen:       0
  Size-Abweichungen:       0
  pcloud_hash Lücken:      0

  Unbekannte Dateien:      2
    [UNBEKANNT] .upload_complete  (size=115)
    [UNBEKANNT] .upload_started   (size=84)

✗ 2 ABWEICHUNG(EN) GEFUNDEN — manuelle Prüfung empfohlen

[timing] Gesamtlaufzeit: 1.8s
```

**Note:** The 2 "unknown" files are marker files (metadata, not backup data). This is normal and OK.

---

### Troubleshooting FAQ

**Q: Why does repair show "Nodes komplett gelöscht: 0" but holders were removed?**

A: Nodes are keyed by SHA256 hash (for deduplication). Even if all holders are removed, the node itself remains until anchor_path is also deleted. This preserves deduplication info for future uploads.

**Q: Why `uploaded=189` at 15% when only 197 total missing?**

A: The upload iterates through ALL manifest files (19,808) sequentially in alphabetical order. Missing files are distributed across the alphabet. At 15% progress, 189 of the 197 have been encountered and uploaded. The remaining ~8 will be uploaded as the process continues.

**Q: Can I skip the verification step after repair?**

A: **NO!** Always verify `anchor_path=None` count matches expected missing files before starting upload. If verification fails, the repair didn't work and upload will skip files again.

**Q: What if I have thousands of missing files?**

A: The workflow scales efficiently:
- Delta check: Still 2-3 seconds (single API call)
- Repair: Linear with node count (~1s per 10k nodes)
- Upload: Only missing files uploaded (resume skips valid ones)

Example: 5,000 missing files from 50k total:
- Delta: 3s
- Repair: 5s
- Upload: ~50min (only 5k files uploaded, 45k skipped)

**Q: Is the stat_file-check expensive?**

A: The current implementation does NOT use stat_file checks! All resume logic is index-driven:
- Check 1: File in index for this snapshot? → resumed
- Check 2: anchor_path == None? → uploaded
- No API calls during resume decision → very fast
- Post-upload verification via `pcloud_quick_delta.py` ensures integrity without slowing down uploads

---

### Performance Metrics (Real Data)

**Scenario:** 19,808 files (89.66 GB), 197 missing after interrupted upload

| Phase | Time | API Calls | Notes |
|-------|------|-----------|-------|
| Delta Check | 1.9s | 1 | Single recursive listfolder |
| Repair Index | <1s | 2 | Load remote index + save local |
| Upload (197 files) | 15min | ~600 | Only missing files uploaded |
| Final Verification | 1.8s | 1 | Confirm 0 missing |
| **Total** | **~17min** | **~604** | vs. full re-upload: 2+ hours |

**Without Resume (full re-upload):**
- Time: 2-3 hours
- API calls: 20,000+
- Bandwidth: 89.66 GB

**With Delta-Check-Repair:**
- Time: ~17 minutes
- API calls: ~604
- Bandwidth: ~4 GB (only missing files)

**Time saved: 95%** | **Bandwidth saved: 96%**

---

## Examples

* **JSON-Manifest aus lokalem Backup erstellen:**

```bash
python pcloud_json_manifest.py \
  --source /mnt/backup/latest \
  --output /srv/pcloud-temp/manifest_$(date +%Y%m%d).json
```

* **Deduplizierter Upload in pCloud:**

```bash
python pcloud_push_json_manifest_to_pcloud.py \
  --manifest /srv/pcloud-temp/manifest_20251214.json \
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

* **SSD-Pfade nutzen** - `PCLOUD_TEMP_DIR=/srv/pcloud-temp` auf SSD statt `/tmp` (Micro-SD wear-leveling!)
  - Index-Dateien: Mehrere MB bei 20k+ Dateien
  - Manifeste: 50+ MB bei großen Snapshots
  - Delta-Reports: Wird bei jedem Integrity-Check geschrieben
  - Archivierung: `PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive` für erfolgreiche Uploads

* **Verzeichnisstruktur einrichten:**
  ```bash
  mkdir -p /srv/pcloud-temp /srv/pcloud-archive/{manifests,indexes}
  chmod 750 /srv/pcloud-temp /srv/pcloud-archive
  ```

* **Manifest-Archivierung** - Erfolgreiche Manifeste werden automatisch nach `/srv/pcloud-archive/manifests/` verschoben
  - Wertvoll für Forensik, Audits, Delta-Checks zwischen Snapshots
  - Optional: Index auch archivieren mit `PCLOUD_ARCHIVE_INDEX=1`

* **Cleanup alter Temp-Files** - `/srv/pcloud-temp` regelmäßig aufräumen (z.B. >7 Tage alte Dateien)

* **Manifest-Naming** - Zeitstempel verwenden: `manifest_$(date +%Y%m%d_%H%M%S).json`

* **Pool-Cleanup** - Alte SHA256-Dateien nur löschen, wenn kein Manifest mehr darauf verweist

* **Integrity Checks** - Regelmäßig nach Upload ausführen (wöchentlich empfohlen)
  ```bash
  python pcloud_quick_delta.py --snapshot <name> --dest-root /Backup/rtb_1to1 --json-out /srv/pcloud-temp/delta.json
  ```

* **Bandwidth** - Bei großen Uploads: `--rate-limit` in pCloud-API nutzen

* **Restore-Tests** - Monatliche Test-Restores in Staging-Umgebung

* **Region** - `PCLOUD_REGION=eu` für EU-Datacenter (DSGVO-Compliance)
