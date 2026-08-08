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

## 9. Commit-Referenzen (Auswahl)

| Thema | Commit-Bereich (main) |
|-------|------------------------|
| Phase 3 parallel | `28e7fd0` |
| OOM-Schutz Pipeline | `70dcd2b` |
| `pool_delta_plan.py` | `35fb5ad` |
| Scout chronologisch | `6c57336` |
| Catch-up-Simulation | `15c108e` |
| Circuit Breaker Cooldown | `adde572` |
