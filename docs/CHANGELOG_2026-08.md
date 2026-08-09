# Changelog August 2026 — pi-nas Catch-up & Robustheit

> Operative Zusammenfassung der Änderungen an **pcloud-tools**, **rtb** und **entropy-watcher** während des Pool-Catch-ups (Juli/August 2026).  
> Architektur-Details (Pool-Modell): Doku-Repo `doku/Raspi/raspinas/pcloud-tools/POOL_MODE_2026_REBUILD.md`  
> Cross-Repo-Betrieb (Locks, Timer): `doku/Raspi/raspinas/pcloud-tools/OPERATIONS_2026-08.md`

---

## 1. NAS Heavy-Ops-Lock (rsync / pCloud / ClamAV / Entropy)

**Problem:** RTB-Backup (`rsync_tmbackup`), pCloud-Upload und ClamAV-/Entropy-Scans griffen gleichzeitig auf `/srv/nas` zu → I/O-Stürme, OOM-Risiko, instabile pCloud-API.

**Lösung (Repo `rtb`):**

| Komponente | Datei |
|------------|-------|
| Gemeinsamer Lock | `rtb/nas_heavy_ops_lock.sh` — Lockfile `/run/backup_pipeline.lock` |
| Wrapper-Helfer | `rtb/with_nas_heavy_ops_lock.sh` |
| RTB Pool-Pipeline | `rtb/rtb_pool_wrapper.sh` — `nas_heavy_ops_acquire` vor Backup + pCloud |
| RTB Legacy | `rtb/rtb_wrapper.sh` — gleiches Muster |
| Exclude-Check | `rtb/rtb_check_excludes.sh` — kein rsync-Dry-Run während Lock aktiv |

**EntropyWatcher (Repo `entropy-watcher-und-clamav-scanner`):**

- systemd-Units (`entropywatcher-nas.service`, `nas-av`, `nas-av-weekly`) starten Scans via `with_nas_heavy_ops_lock.sh`
- `Conflicts=` zwischen sich gegenseitig ausschließenden Units (z. B. daily AV vs. weekly full)
- `safety_gate.sh` prüft vor RTB/pCloud, ob NAS-Scans aktiv sind

**pCloud-Wrapper:** `wrapper_pcloud_pool_sync_1to1.sh` nutzt dieselbe Lib (`NAS_HEAVY_OPS_LIB`), sofern nicht `BACKUP_PIPELINE_LOCKED=1` (innerhalb `rtb_pool_wrapper`).

---

## 2. Wrapper-Logging & OOM-Schutz

`wrapper_pcloud_pool_sync_1to1.sh`:

- Strukturiertes Logging (`_log INFO/WARN/ERROR`), MariaDB-Phasen (`manifest`, `upload`, `verify`)
- Catch-up: **latest zuerst**, dann Backlog; fehlgeschlagener älterer Snapshot blockiert jüngere nicht
- Harte Prüfung: Upload ohne `.upload_complete` → `FAILED`
- `PCLOUD_OOM_SCORE_ADJ` (Default `-500`) — Upload-Prozess bei OOM geschützt
- Preflight mit Retries (`PCLOUD_PREFLIGHT_RETRIES`, `PCLOUD_PREFLIGHT_RETRY_DELAY_SEC`)

---

## 3. Turbo-Delta: Scout-Fix & Phase 3 parallel

### Scout (`scout_pool_basis` in `pcloud_bin_lib.py`)

**Bug:** Scout wählte den höchsten Jaccard-Match **ohne Chronologie** → oft ein **neuerer** Remote-Snap als Basis für einen **älteren** Ziel-Snap → massives Phase-3-Cleanup (tausende Löschungen).

**Fix:** Priorität:

1. Chronologischer Vorgänger (`name < target`) mit Similarity ≥ `PCLOUD_SCOUT_THRESHOLD`
2. Bester Jaccard unter **nur älteren** Remote-Snaps
3. **Nie** einen chronologisch neueren Snap als Basis

### Phase 3 Cleanup parallel

- `PCLOUD_DELTA_CLEANUP_THREADS` (Default = `PCLOUD_UPLOAD_THREADS`)
- `PCLOUD_DELTA_CLEANUP_PROGRESS_EVERY` (Default `500`) — Fortschritts-Logs während Bereinigung

---

## 4. Full-Pool-Mode Reparatur

**Bug:** Delta-Fehler → Fallback rief erneut Scout auf → wechselseitige Rekursion / Hänger.

**Fix:** `push_pool_mode(..., use_scout=False)` beim Full-Fallback — ein echter Full-Upload ohne erneutes Scouting. Siehe auch `POOL_MODE_2026_REBUILD.md` §7.

---

## 5. API Circuit Breaker & Stub-Schonung

**Problem:** Bei Netzwerk-Blips (`socket closed`) öffnete der Circuit Breaker **dauerhaft** im Prozess → tausende sofortige Fehler in der Stub-/FID-Phase, kein Recovery im selben Lauf.

**Fix (`pcloud_bin_lib.py`):**

| Zustand | Verhalten |
|---------|-----------|
| **CLOSED** | Normal |
| **OPEN** | Pause (`PCLOUD_CIRCUIT_BREAKER_COOLDOWN_SEC`, Default 60 s), Threads runter |
| **HALF-OPEN** | Ein Probe-Call; bei Erfolg → CLOSED |
| **Ramp-up** | Nach `PCLOUD_CIRCUIT_RECOVERY_SUCCESSES` (Default 30) Erfolgen → Parallelitäts-Tier hoch |

Parallelitäts-Tiers (`PCLOUD_CIRCUIT_PARALLELISM_TIERS=1,0.75,0.5`):

- Bei 16 konfigurierten Threads: **16 → 12 → 8**

Betrifft: `PCLOUD_API_META_CONCURRENCY`, `PCLOUD_STUB_FID_THREADS`, `PCLOUD_STUB_THREADS`.

**Retry-Pausen (unverändert, zur Einordnung):**

| Mechanismus | Pause |
|-------------|-------|
| `PCLOUD_API_META_DELAY` | 30 ms nach jedem RPC |
| `call_with_backoff` | 2 s, 4 s, 8 s, 16 s … (max. 30–60 s), 5 Versuche |
| Circuit-Breaker-Cooldown | 60 s (+ länger bei wiederholten Trips) |

→ Vollständige ENV-Liste: [ENV_VARIABLES.md](ENV_VARIABLES.md)

---

## 6. Planungstool `pool_delta_plan.py`

Offline-Schätzung **Delta vs. Full** pro Snapshot (Phase-3-Löschungen, Phase-4-Uploads, Scout-Basis).

```bash
cd /opt/apps/pcloud-tools/main
/opt/apps/pcloud-tools/venv/bin/python scripts/utilities/pool_delta_plan.py \
  --env-file .env --missing-only --simulate-catchup
```

- `--simulate-catchup` — Reihenfolge-Effekt: jeder geplante Upload erweitert die Remote-Basis für den nächsten
- `--compare-static` — Gegenüberstellung statisch vs. Catch-up-Simulation

→ [scripts/utilities/pool_delta_plan.md](../scripts/utilities/pool_delta_plan.md)

---

## 7. Deployment auf pi-nas

```bash
cd /opt/apps/pcloud-tools/main && git pull origin main
cd /opt/apps/rtb && git pull origin main
cd /opt/apps/entropywatcher/main && git pull origin main   # falls Scan-Units aktualisiert

# Optional: neue ENV aus .env.example übernehmen (Circuit Breaker)
# systemctl daemon-reload  # nur bei geänderten systemd-Units
```

---

## 8. Wrapper: `.upload_complete`-Check vs. API-Ausfall

**Problem:** Kurzer DNS-/API-Ausfall nach erfolgreichem Upload → `remote_snapshot_exists` gab `NO` zurück → fälschlich „`.upload_complete` fehlt“ und Exit 1, obwohl Marker gesetzt war.

**Lösung:** `YES` / `NO` / `ERR:…` unterscheiden, Retries (`PCLOUD_MARKER_VERIFY_RETRIES`), danach weicher OK mit Warnung statt False-FAIL.

---

## 9. Manifest Smart-Ref: mtime/size-Deckung

**Problem:** Catch-up wählte global neuestes Archiv-Manifest (z. B. Aug 6) als Referenz für ältere Snapshots (Jul 12) → unnötiges Re-Hashing.

**Lösung:** Auto-Pick per mtime/size-Deckung; max. **6** chronologisch nächste Kandidaten; Scoring im **gleichen** Manifest-Scan (kein zweiter Walk, kein Laden aller Archiv-Manifeste).

---

## 10. Integrity-Gate (listfolder, Default)

**Problem:** Drei redundante Validierungsschichten — legacy `stat()` (~40 Min), listfolder-Verify (~60 s), Wrapper-Integrity — bei jedem Upload.

**Lösung:** Ein hartes Gate im Push-Skript **vor** `.upload_complete`:

| Schicht | Wo | Default | Dauer |
|---------|-----|---------|-------|
| **Integrity-Gate** | `pcloud_push_json_pool_manifest_to_pcloud.py` | **an** | ~30–60 s (`listfolder`) |
| Legacy stat-Validation | gleiches Skript (`PCLOUD_VALIDATE_UPLOAD=1`) | **aus** | ~40 Min |
| Wrapper-Integrity | `wrapper_pcloud_pool_sync_1to1.sh` | **skip** | redundant |

**Ablauf:**
```
Stubs → [integrity-gate] pool_verify_backup (listfolder)
      → optional Pool-Backfill (max PCLOUD_VALIDATE_POOL_BACKFILL_MAX)
      → pool_integrity_run (post_upload → MariaDB)
      → .upload_complete → Index-Upload
```

**ENV:** `PCLOUD_VALIDATE_UPLOAD=0`, `PCLOUD_POST_UPLOAD_INTEGRITY=skip`, `PCLOUD_VALIDATE_POOL_BACKFILL_MAX=50`

---

## 11. Pipeline RAM: Logging + Manifest-Streaming (August 2026)

**Problem:** Backup-Pipeline auf pi-nas (8 GB) lief in OOM/Swap — u. a. `tee`-Pipes unter systemd, rsync `--itemize-changes`, Manifest mit `all_paths` + Finalize-Spike im RAM.

**Lösung (Repos `rtb` + `pcloud-tools`):**

| Bereich | Änderung |
|---------|----------|
| RTB Trigger | FD-Kollision Lock vs. Check behoben (`rtb_backup_trigger_run_locked`) |
| RTB/rsync | Kein `--itemize-changes` in Produktion; stdout → Temp-Datei statt Parent-`tee` |
| RTB Wrapper | Unter systemd: `exec >>log` statt `tee`-Pipe |
| pCloud Wrapper | Gleiches Logging-Muster; Integrity-Ausgabe über Temp-Datei |
| Manifest | Scan/Finalize in `pcloud_bin_lib.py` (TSV + `sort`, JSONL, streaming JSON) |
| systemd | `install-backup-pipeline-systemd.sh` — kanonische Unit ohne `MemoryMax`/`StandardOutput=append` |

**Nicht breaking:** Manifest-Schema v4 unverändert; JSON-Ausgabe weiterhin gültiges JSON (`jq -e '.items'`). Nur intern weniger RAM, etwas mehr SSD-I/O.

**Offen:** `pcloud_push_json_pool_manifest_to_pcloud.py` lädt Manifest noch per `json.load()` — nächster RAM-Kandidat nach RTB.

---

## 12. RTB: Batch-Fork revertiert + OOM-Opfer entfernt (9. Aug 2026)

**Was schiefging (Commits `55e469c`–`d987261`, kurz auf pi-nas):**

- `rsync_tmbackup.sh` wurde mit `--batch-top-level` geforkt (21 rsync-Läufe pro Snapshot) — **gegen Architektur-Regel**.
- `d987261` übersprang `pcloud-archive`/`pcloud-temp` im Batch → Snapshot **ohne** Pipeline-Pfade, obwohl Policy „Mitläufer“ verlangt.
- Lauf ~18:39: rsync auf `pcloud-archive` → **OOM-Kill** (`anon-rss` ~3,5 GB, `oom_score_adj=500`).
- Lauf 20:00 mit `d987261`: „erfolgreich“ in 27 s, weil pcloud-Pfade **übersprungen** — kein echter Policy-Erfolg.

**Fix (Commits `e86a96b` rtb, `8c3367c` pcloud-tools):**

| Änderung | Warum |
|----------|--------|
| `rsync_tmbackup.sh` wieder Upstream | Kein Batch-Fork; Clone des Originals bleibt gültig |
| `excludes.txt` ohne pcloud-Pfade | Zwei-Schichten: Mitläufer nur in `rtb_check_excludes.sh` |
| Wrapper: `--rsync-set-flags` ohne itemize | Kein stdout/Log-RAM; Upstream unverändert |
| `RTB_OOM_SCORE_ADJ` / `OOMScoreAdjust=500` entfernt | rsync nicht mehr gezielt opfern |

**Deploy pi-nas:** `git pull` rtb + pcloud-tools → `install-backup-pipeline-systemd.sh` → `backup.inprogress` löschen falls halb.

---

## 13. Manifest: leeres `items[]` verhindert (9. Aug 2026, Abend)

**Symptom:** Manifest-Scan 116k Dateien, Push liest `0 Files` → `ZeroDivisionError` → `upload_failed`.

**Ursache:** JSONL-Streaming schrieb Items in `.tmp.jsonl`, Finalize fehlte/fiel durch → Fallback schrieb `items: []`. Wrapper akzeptierte leeres Array (`jq -e '.items'`).

**Fix:**

| Datei | Änderung |
|-------|----------|
| `pcloud_json_pool_manifest.py` | Fehler wenn JSONL fehlt oder Finalize 0 Items |
| `wrapper_pcloud_pool_sync_1to1.sh` | Manifest gültig nur wenn `items \| length > 0` |
| `pcloud_push_json_pool_manifest_to_pcloud.py` | Klare Meldung statt `ZeroDivisionError` bei 0 Files |

---

## 13. Dashboard / DB-Wartung

**Problem:** Dashboard „Letzte Fehler (7d)“ zeigte alte FAILED-Einträge, obwohl Snapshots später erfolgreich waren.

**Lösung:** `sql/maintenance_db_cleanup.sql` + `scripts/maintenance_db_cleanup.sh` — superseded FAILED entfernen, RUNNING-Zombies bereinigen, `v_failed_backups` (7-Tage-Filter) aktualisieren.

```bash
sudo ./scripts/maintenance_db_cleanup.sh
sudo ./scripts/generate_reports.sh
```

---

## 14. RTB Staged Backup — OOM auf pi-nas (9.–10. Aug 2026)

**Problem:** Monolithischer rsync über `/srv/nas` (~240k Einträge) → OOM auf 8 GB-Pi. `pcloud-archive/staging/json` (~47k Scratch-Ordner) erzeugte bei fehlerhaftem Tree-Split **~47k rsync-Läufe**.

**Lösung:**

| Repo | Änderung |
|------|----------|
| `rtb` | `rtb_staged_backup.sh` — ~32 Einheiten, Resume, Excludes in Unit-Liste (`d653c61`), Top-Level-Dateien (`33c8d1d`) |
| `rtb` | `excludes.txt`: `/pcloud-archive/staging/` |
| `rtb` | Wrapper: Delta-Check skip bei offenem Staged-Resume |
| `pcloud-tools` | `RTB_STAGED=1` in `backup-pipeline.service.example` |
| `pcloud-tools` | `write_json_to_folderid()` entfernt leere Scratch-Ordner nach Upload |
| `pcloud-tools` | `scripts/restore-pipeline-services.sh` — Timer nach Wartung |

**Nach manuellem RTB:** `--upload-only` für pCloud, dann `restore-pipeline-services.sh`.

Details: `rtb/README.md`, `doku/Raspi/raspinas/pcloud-tools/OPERATIONS_2026-08.md` §11.

---

## 15. Manifest JSONL gelöscht vor Finalize (10. Aug 2026)

**Symptom:** `[manifest] 98%` → `[stats]` → `JSONL-Checkpoint fehlt` → `FileNotFoundError` auf `.json`.

**Ursache:** `scan_base=jsonl_tmp` — `manifest_cleanup_scan_files()` löschte nach dem Walk die JSONL-Checkpoint-Datei (gleicher Pfad).

**Fix:** `scan_base=f"{jsonl_tmp}.scan"` (separate TSV-Temp-Dateien).

```bash
cd /opt/apps/pcloud-tools/main && git pull
rm -f /srv/pcloud-temp/pcloud_mani.2026-08-09-224716.json*
sudo /opt/apps/rtb/rtb_pool_wrapper.sh --upload-only /mnt/backup/rtb_nas/2026-08-09-224716
```

---

## 16. Commit-Referenzen (Auswahl)

| Thema | Commit-Bereich (main) |
|-------|------------------------|
| Phase 3 parallel | `28e7fd0` |
| OOM-Schutz Pipeline | `70dcd2b` |
| `pool_delta_plan.py` | `35fb5ad` |
| Scout chronologisch | `6c57336` |
| Catch-up-Simulation | `15c108e` |
| Circuit Breaker Cooldown | `adde572` |
| Integrity-Gate (listfolder) | `76441b7` |
| Manifest Smart-Ref (max 6) | `f7f440c`, `53d7fce`, `4da0d21` |
| Pipeline RAM / streaming | `c8513c6` (rtb), `046e42d` (rtb), `fd09a17` + Manifest-Lib (pcloud-tools) |
| RTB Batch revert + OOM-Opfer weg | `e86a96b` (rtb), `8c3367c` (pcloud-tools) |
| Marker-Verify API-Ausfall | `0a24f12` |
| DB-Wartung Dashboard | `79e5430` |
| RTB staged backup | `bdf93b7`–`33c8d1d` (rtb), `51228b0` + `1f0a1c9` (pcloud-tools) |
