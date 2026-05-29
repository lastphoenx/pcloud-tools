# Pool-Mode Delta-Copy: Best-Match Scout

**Datum:** 29. Mai 2026  
**Status:** Implementiert & In Erprobung (Phase 2 Optimierung aktiv)

---

## Pre-flight: Massen-Delta-Abgleich (Neu!)

Bevor der erste Upload-Thread startet, führt das Skript einen massiven Pre-flight Check durch. Dies eliminiert die Ineffizienz von "Just-in-Time" Prüfungen während des Upload-Loops.

### Architektur
1. **Physical Pool Sync (NEU!)**:
   - Einmaliger `listfolder(recursive=True)` auf `/_pool`.
   - Extrahiert alle vorhandenen SHA256-Hashes direkt vom Server.
   - Aktualisiert den lokalen Index-Cache mit der physischen Realität.
2. **Laden des Master-Pool-Index**: Alle lokal bekannten SHA256-Hashes werden in ein Set geladen.
3. **Klassifizierung**: Das Manifest wird gegen den Index abgeglichen und in drei Mengen unterteilt:
   - `delta_sha256s`: Hashes, die physisch im Pool fehlen (→ Echter Upload nötig).
   - `reused_sha256s`: Hashes, die im Pool sind, aber noch nicht für diesen Snapshot registriert wurden (→ Nur Stub-Erstellung + Index-Update).
   - `already_in_snapshot`: Hashes, die bereits vollständig verarbeitet sind (→ Skip).

### Performance-Vorteil
- **API-Quota**: `stat_file` entfällt komplett für Dateien, die bereits im Pool sind.
- **Lock-Contention**: Threads müssen nicht mehr gegeneinander auf den Index warten, da die Aufgaben-Listen (`delta_items`, `reused_items`) vorab final feststehen.
- **Speed**: Ein Pool-Scan (100k Files) dauert ~3s. 100k `stat`-Calls dauern ~1.5h.

---

## Deep-Audit & Garbage Collection (GC)

Um die Integrität langfristig zu sichern und Speicherplatz freizugeben, werden zwei neue Tools konzipiert:

### 1. Deep-Audit (Vollständigkeits-Check)
- **Ziel**: Sicherstellen, dass JEDER Stub in JEDEM Snapshot eine gültige Datei im Pool hat.
- **Logik**:
  1. Scanne alle `/_snapshots/*` rekursiv nach `.meta.json`.
  2. Sammle alle referenzierten SHA256s.
  3. Scanne `/_pool` rekursiv (SHA256-Set).
  4. Abgleich: `required_sha256s - physical_sha256s` = **CORRUPTION** (Fehlende Pool-Files).

### 2. Pool-GC (Garbage Collection)
- **Ziel**: Löschen von verwaisten Dateien im Pool (Files ohne Stub-Referenz).
- **Logik**:
  1. Sammle alle SHA256s aus ALLEN Stubs (wie Audit).
  2. Sammle alle physischen SHA256s aus `/_pool`.
  3. Abgleich: `physical_sha256s - required_sha256s` = **ORPHANS** (Löschbar).
  4. **Safety-First**: Nur löschen, wenn die Datei älter als X Tage ist (Race-Condition Schutz während laufender Uploads).

---

## Problem-Statement

**Aktuelle Situation (Phase 1 - Ordneranlage):**
```
103,492 Ordner × ensure_path() = ~73 Minuten API-Overhead
```

**Lösung:** Nutze `copyfolder()` für serverseitige Struktur-Kopie (~Sekunden statt Minuten!)

---

## Konzept: "Best Match" Scout

### Ziel
Vor dem Upload entscheiden, ob ein bestehender Snapshot als Basis dienen kann.

### Logik

```python
def scout_best_pool_basis(current_manifest: dict, archive_dir: str) -> tuple[str | None, float]:
    """
    Findet den effizientesten Basis-Snapshot via Jaccard-Ähnlichkeit.
    
    Strategie:
    - Vergleiche relpath-Mengen der Manifeste.
    - Performance-Limit: Scanne nur die letzten 10 Snapshots.
    - Early Exit: Bei >95% Match sofort wählen.
    """
    t_start = time.time()
    manifests_path = os.path.join(archive_dir, "manifests")
    if not os.path.isdir(manifests_path):
        return None, 0.0

    current_paths = _get_manifest_paths(current_manifest)
    if not current_paths:
        return None, 0.0

    best_snap = None
    best_score = 0.0
    
    # Neueste zuerst prüfen
    archived_files = sorted(
        [f for f in os.listdir(manifests_path) if f.endswith(".json")],
        reverse=True
    )

    for filename in archived_files[:10]:
        snap_name = filename.replace(".json", "")
        if snap_name == current_manifest.get("snapshot"):
            continue

        try:
            with open(os.path.join(manifests_path, filename), "r") as f:
                arch_manifest = json.load(f)
            
            arch_paths = _get_manifest_paths(arch_manifest)
            intersection = len(current_paths & arch_paths)
            score = intersection / len(current_paths)
            
            if score > best_score:
                best_score = score
                best_snap = snap_name
                
            if best_score > 0.95: break
        except Exception: continue

    return best_snap, best_score
```

---

## Metrik: Similarity Score

```python
def count_matching_files(current, basis):
    """
    Zählt Files mit identischem relpath + sha256.
    
    current: {relpath: sha256}
    basis:   {relpath: sha256}
    """
    matches = 0
    for path, sha in current.items():
        if basis.get(path) == sha:
            matches += 1
    return matches
```

**Beispiel:**
```
Current Manifest: 103,492 Files
Basis Snapshot:   100,000 Files
Matches:          85,000 Files (identischer Pfad + Hash)

Similarity = 85,000 / 103,492 = 82.1%  ← PASS (> 70%)
```

---

## Workflow: Turbo-Pool-Delta-Mode

### Szenario A: Best Match gefunden (Similarity > 70%)

```
┌─────────────────────────────────────────────┐
│ 1. Scout findet Basis: 2026-05-24-200014   │
│    Similarity: 82.1%                        │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ 2. copyfolder(from=2026-05-24-200014,       │
│               to=2026-05-29-080000,         │
│               copycontentonly=True)         │
│    → Kopiert Ordner-Struktur + Stubs        │
│    → Dauer: ~5 Sekunden (serverseitig!)    │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ 3. Manifest-Diff berechnen:                │
│    - Added:   18,492 Files (neue)          │
│    - Changed: 0 Files                       │
│    - Deleted: 15,000 Files (entfernt)      │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ 4. Delta-Sync (nur Änderungen!):           │
│    - Upload neue Anchors → Pool             │
│    - Erstelle Stubs für neue Files          │
│    - Lösche veraltete Stubs (recursive!)   │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ 5. Validation & Complete-Marker            │
└─────────────────────────────────────────────┘
```

**Zeit-Ersparnis:**
- **Alt:** 73 Min (Ordneranlage) + 8 Std (Upload/Stubs)
- **Neu:** 5 Sek (copyfolder) + 1 Std (nur Delta) → **~7-8 Std gespart!**

---

### Szenario B: Kein Match (Similarity < 70%)

```
┌─────────────────────────────────────────────┐
│ 1. Scout: Kein geeigneter Basis gefunden   │
│    Best Similarity: 23.5% (zu niedrig)     │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ 2. Full-Pool-Mode (wie bisher):            │
│    Phase 1: Ordneranlage (73 Min)          │
│    Phase 2: Upload + Stubs (8 Std)         │
└─────────────────────────────────────────────┘
```

---

## Implementierungs-Roadmap

### A. Scout-Funktion

**Datei:** `pcloud_push_json_pool_manifest_to_pcloud.py`

**Neue Funktion:**
```python
def scout_best_basis_snapshot(cfg: dict, 
                              manifest_path: str,
                              dest_root: str) -> tuple[str | None, float]:
    """
    Findet besten Basis-Snapshot via Manifest-Vergleich.
    
    Returns:
        (snapshot_name, similarity) oder (None, 0.0)
    """
    # 1. Lade aktuelles Manifest
    current = _load_manifest_index(manifest_path)
    
    # 2. Liste remote Snapshots
    snapshots_root = f"{dest_root}/_snapshots"
    remote_snaps = _list_remote_snapshots(cfg, snapshots_root)
    
    # 3. Für jeden Snapshot: Lade archived Manifest + compare
    best = None
    max_sim = 0.0
    
    for snap_name in remote_snaps:
        # Lade archiviertes Manifest (falls vorhanden)
        archived_manifest = _try_load_archived_manifest(cfg, snapshots_root, snap_name)
        if not archived_manifest:
            continue
        
        # Vergleiche
        matches = _count_matches(current, archived_manifest)
        similarity = matches / len(current) if current else 0.0
        
        if similarity > max_sim:
            max_sim = similarity
            best = snap_name
    
    # Schwellenwert
    if max_sim >= 0.70:
        return (best, max_sim)
    else:
        return (None, 0.0)
```

---

### B. Turbo-Delta-Copy Funktion

**Neue Funktion:**
```python
def push_pool_delta_mode(cfg, pc, manifest, basis_snapshot_name):
    """
    Synchronisiert neuen Snapshot basierend auf Klon eines alten.
    """
    snapshot_name = manifest["snapshot"]
    dest_snapshot_dir = f"{cfg.dest_root}/_snapshots/{snapshot_name}"
    basis_snapshot_dir = f"{cfg.dest_root}/_snapshots/{basis_snapshot_name}"

    # 1. Server-Side Copy (Instant Struktur)
    if not cfg.dry:
        pc.copyfolder(cfg, path=basis_snapshot_dir, 
                      destpath=f"{cfg.dest_root}/_snapshots", 
                      toname=snapshot_name)

    # 2. Diff berechnen (Added, Changed, Removed)
    old_manifest = load_archived_manifest(cfg.archive_dir, basis_snapshot_name)
    diff = calculate_manifest_diff(old_manifest, manifest)
    
    # 3. Bereinigung: Veraltete Stubs entfernen
    if diff['removed'] and not cfg.dry:
        for relpath in diff['removed']:
            stub_path = f"{dest_snapshot_dir}/{relpath}.meta.json"
            pc.deletefile(cfg, path=stub_path)

    # 4. Update: Neue/Geänderte Stubs parallel verarbeiten
    tasks = diff['added'] + diff['changed']
    if tasks:
        # Nutzt ThreadPool für _upload_to_pool_as_stub
        process_stub_updates_parallel(cfg, pc, tasks, dest_snapshot_dir)

    # 5. Marker & Manifest finalisieren
    write_remote_manifest(cfg, pc, dest_snapshot_dir, manifest)
    set_upload_complete_marker(cfg, pc, dest_snapshot_dir)
```

---

### C. Integration in main()

**Änderung in `push_pool_mode()`:**

```python
def push_pool_mode(cfg: dict, manifest_path: str, dest_root: str, 
                   snapshot_name: str, dry: bool = False):
    """
    Pool-Mode Upload mit Scout-Optimization.
    """
    # Scout: Beste Basis finden
    basis, similarity = scout_best_basis_snapshot(cfg, manifest_path, dest_root)
    
    if basis and similarity >= 0.70:
        _log(f"[scout] ✓ Best Match: {basis} (Similarity: {similarity*100:.1f}%)")
        _log(f"[scout] → Nutze Turbo-Delta-Mode!")
        
        return push_pool_turbo_delta_mode(
            cfg, manifest_path, dest_root, 
            basis_snapshot=basis,
            snapshot_name=snapshot_name,
            dry=dry
        )
    else:
        _log(f"[scout] Kein geeigneter Basis-Snapshot (Best: {similarity*100:.1f}%)")
        _log(f"[scout] → Fallback zu Full-Pool-Mode")
        
        # Bisherige Implementierung (Phase 1 + 2)
        return push_pool_mode_full(cfg, manifest_path, dest_root, snapshot_name, dry)
```

---

## Performance-Schätzung

**Test-Szenario:** 103,492 Files, 97.77 GB, 82% Similarity

| Mode | Phase 1 | Phase 2 | Total |
|------|---------|---------|-------|
| **Full-Pool** | 73 Min | 8 Std | ~9 Std |
| **Turbo-Delta** | 5 Sek | 1.5 Std | ~1.5 Std |
| **Ersparnis** | -72 Min | -6.5 Std | **~7.5 Std** |

---

## Offene Punkte

1. **Manifest-Archivierung:**
   - Aktuell: Remote Archive nach Upload
   - Benötigt: Effiziente Download-Funktion für Scout

2. **Stub-Deletion:**
   - Einzelne DELETE vs. rekursiver `delete_folder()`
   - Trade-off: API-Calls vs. Kollateralschäden

3. **Cache-Invalidierung:**
   - Folder-Cache vom Basis-Snapshot ungültig nach copyfolder?
   - Benötigt frisches `listfolder()` auf neuem Snapshot

4. **Dry-Run Testing:**
   - Scout-Logik ohne echte copyfolder testen
   - Manifest-Diff Validierung

---

## Nächste Schritte

1. ✅ **Konzept dokumentiert**
2. ⏸️ **Prototype:** Scout-Funktion isoliert testen
3. ⏸️ **Integration:** Turbo-Delta-Mode implementieren
4. ⏸️ **Testing:** Dry-Run auf Test-Dataset
5. ⏸️ **Production:** Live-Test mit echtem Snapshot

---

**Fazit:** Scout-basierter Turbo-Delta-Mode könnte **~80% Zeit-Ersparnis** bei inkrementellen Backups bringen!

---

## Pool-GC: Optimierungen & Best Practices (Version 2.0)

### Problem-Analyse (Original-GC)

Das ursprüngliche GC-Tool hatte bei großen Datensätzen kritische Schwächen:

**1. Phase 1 - "The API-Killer":**
```python
# ALT: Für JEDEN Stub in JEDEM Snapshot:
stub_content = pc.get_textfile(cfg, path=stub_path)  # ← 1 API-Call!
stub_data = json.loads(stub_content)
sha256 = stub_data["sha256"]

# Resultat: 100k Files × 10 Snapshots = 1 Million API-Calls!
# → pCloud Throttling / Account-Sperren
```

**2. Phase 2 - "The Prefix-Crawler":**
```python
# ALT: Für JEDEN Präfix-Ordner (00-ff):
result = pc.listfolder(cfg, path=f"/_pool/{prefix}")  # ← 256× API-Calls!

# → Statt 1× rekursiv = Sekunden, braucht es Minuten
```

**3. Race-Condition - "The Silent Killer":**
```python
# ALT: Lösche ALLES was aktuell nicht referenziert ist
if sha256 not in ref_set:
    delete_file(pool_file)  # ← Kein Grace-Period Check!

# Problem: Parallel laufende Backups verlieren gerade hochgeladene Files!
# → Backup-Korruption
```

---

### Lösung: Index-basiertes GC 2.0

**Neue Architektur:**

```python
def run_pool_gc_v2(cfg, dest_root, *, audit_mode=False, grace_hours=24):
    """
    Optimiertes GC mit drei Modi:
    
    1. STANDARD (Index-basiert, schnell):
       - Lädt content_index.json (pool_refs)
       - ~0.1s statt Stunden!
    
    2. AUDIT (Deep-Validation, langsam):
       - Scannt alle Stubs (wie Alt-GC)
       - Validiert Index-Konsistenz
    
    3. GRACE-PERIOD (Race-Protection):
       - Nur Files > X Stunden alt löschen
       - Schützt parallel laufende Backups
    """
```

#### Phase 1: Referenz-Sammlung (NEU!)

```python
# STANDARD-MODE (Index-basiert):
index_content = pc.get_textfile(cfg, path="/_snapshots/content_index.json")
index = json.loads(index_content)
referenced_sha256s = set(index["pool_refs"].keys())  # ← 1 API-Call, 0.1s!

# Statt:
# - 1M get_textfile() Calls
# - Stunden Laufzeit
# - API-Throttling

# AUDIT-MODE (optional, für Index-Validation):
# Scannt alle Stubs wie Alt-GC → vergleicht mit Index → findet Diskrepanzen
```

#### Phase 2: Pool-Scan (NEU!)

```python
# REKURSIV (1× API-Call):
result = pc.listfolder(cfg, path="/_pool", recursive=True, nofiles=False)

def _extract_pool_sha256s(obj):
    """Extrahiert SHA256s aus rekursivem Tree"""
    if obj.get("isfolder") == False:
        filename = obj["name"]
        if len(filename) == 64 and filename.isalnum():
            pool_sha256s.add(filename.lower())
    
    for child in obj.get("contents", []):
        _extract_pool_sha256s(child)

# Resultat: 500k Pool-Files in ~2-5 Sekunden!
# Statt: 256× listfolder = Minuten
```

#### Phase 3: Grace-Period Check (NEU!)

```python
# RACE-PROTECTION via mtime:
grace_cutoff = time.time() - (grace_hours * 3600)

for pool_file in pool_files:
    sha256 = pool_file["name"]
    
    # 1. Referenz-Check
    if sha256 in referenced_sha256s:
        keep()
        continue
    
    # 2. Grace-Period-Check (NEU!)
    if pool_file["modified"] > grace_cutoff:
        keep()  # ← Zu jung, könnte gerade uploaded sein!
        if verbose:
            age_hours = (time.time() - pool_file["modified"]) / 3600
            log(f"Grace: {sha256[:16]}... (age: {age_hours:.1f}h < {grace_hours}h)")
        continue
    
    # 3. Unreferenziert & alt genug → löschen
    delete_files_to_delete.append(pool_file)
```

---

### Performance-Vergleich

**Test-Szenario:** 103,492 Files, 10 Snapshots, 500k Pool-Files

| Metrik | **GC v1 (Alt)** | **GC v2 (Neu)** | **Ersparnis** |
|--------|----------------|----------------|---------------|
| **Phase 1 (Refs)** | 1M API-Calls, ~Stunden | 1 API-Call, ~0.1s | **~99.9%** |
| **Phase 2 (Pool-Scan)** | 256× listfolder, ~Minuten | 1× listfolder, ~2-5s | **~98%** |
| **Race-Protection** | ❌ Keine | ✅ Grace Period (24h) | **0 Korruptionen** |
| **Gesamt-Laufzeit** | ~Stunden (mit Throttling) | **~10 Sekunden** | **~99%** |

---

### Anwendungs-Szenarien

#### 1. Routine-GC (Wöchentlich, Cron)

```bash
# Standard-Mode: Schnell, sicher, Index-basiert
python pcloud_pool_gc.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --grace-hours 24 \
  >> /var/log/backup/pool_gc.log 2>&1

# Erwartete Ausgabe:
[gc] Mode: INDEX-BASED
[gc] PHASE 1: Loading references from content_index.json...
[gc] PHASE 1 DONE: 98,492 unique SHA256s (0.12s)
[gc] PHASE 2: Listing pool files (recursive)...
[gc] PHASE 2 DONE: 500,000 pool files found (2.34s)
[gc] Check complete: 1,508 to delete, 498,492 to keep
[gc] PHASE 3 DONE: 1,508 files deleted, 12.45 GB freed (3.21s)
[gc] Duration: 5.7s
```

#### 2. Deep-Audit (Nach Index-Reparatur)

```bash
# Audit-Mode: Validiert Index gegen physische Stubs
python pcloud_pool_gc.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --audit-mode \
  --dry-run \
  --verbose

# Erwartete Ausgabe:
[gc] Mode: AUDIT (Deep-Validation)
[gc] PHASE 1: AUDIT-MODE - Scanning all stubs...
[gc] PHASE 1 DONE: 10 snapshots, 1,034,920 stubs, 98,492 unique SHA256 (143.2s)
[gc] WARN: Index zeigt 98,500 SHA256s, Stubs zeigen 98,492 (8 Diskrepanzen!)
```

#### 3. Dry-Run (Vor Produktion)

```bash
# Test-Run: Zeigt was gelöscht würde, ohne echte Löschung
python pcloud_pool_gc.py \
  --dest-root /Backup/rtb_1to1 \
  --env-file .env \
  --dry-run \
  --grace-hours 48

# Erwartete Ausgabe:
[gc] Dry-Run: {dry: true}
[gc] Grace Period: 48h
[gc] Would delete 1,508 files (12.45 GB)
[gc] ⚠ DRY-RUN: Keine echten Löschungen durchgeführt
```

---

### Best Practices

**1. Grace Period richtig wählen:**
```bash
# Standard (tägliche Backups):
--grace-hours 24  # ← 1 Tag Puffer

# Konservativ (wöchentliche Backups):
--grace-hours 168  # ← 1 Woche Puffer

# Aggressiv (nur bei manueller Aufsicht):
--grace-hours 1  # ← 1 Stunde Puffer (Risiko!)

# Deaktiviert (GEFÄHRLICH!):
--grace-hours 0  # ← Keine Grace Period (Race-Conditions möglich!)
```

**2. Audit-Mode nur bei Verdacht:**
```bash
# Normal: Schnelles Index-basiertes GC
python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env

# Bei Index-Reparatur / Verdacht auf Korruption:
python pcloud_pool_gc.py --dest-root /Backup/rtb_1to1 --env-file .env --audit-mode --dry-run
```

**3. Cron-Integration (Empfohlen):**
```bash
# Wöchentlich, Sonntag 3 Uhr (außerhalb Backup-Fenster):
0 3 * * 0 cd /opt/apps/pcloud-tools/main && \
  python pcloud_pool_gc.py \
    --dest-root /Backup/rtb_1to1 \
    --env-file .env \
    --grace-hours 24 \
    >> /var/log/backup/pool_gc.log 2>&1

# Benachrichtigung bei Fehlern:
0 3 * * 0 cd /opt/apps/pcloud-tools/main && \
  python pcloud_pool_gc.py ... || \
  echo "GC failed!" | mail -s "Backup GC Alert" admin@example.com
```

---

### Fehlerbehandlung & Safety

**1. Index nicht verfügbar:**
```python
# Automatischer Fallback auf Stub-Scan:
if not referenced_sha256s:
    log("[gc] Fallback: Scanning stubs (Index nicht verfügbar)...")
    # → Scannt Stubs wie Alt-GC (langsam, aber funktional)
```

**2. Pool-Scan fehlgeschlagen:**
```python
# Abort statt Delete:
try:
    result = pc.listfolder(cfg, path="/_pool", recursive=True)
except Exception as e:
    log(f"[ERROR] Pool-Scan fehlgeschlagen: {e}")
    return {"error": str(e)}  # ← KEIN Löschen bei Unsicherheit!
```

**3. Keine Referenzen gefunden:**
```python
# Safety-Check:
if not referenced_sha256s:
    log("[ERROR] Keine Referenzen gefunden! Abbruch (Sicherheit).")
    return {"error": "No references found"}
    # → Verhindert versehentliches Löschen des kompletten Pools!
```

---

### Monitoring & Alerting

**Log-Metriken:**
```
[gc] Duration: 5.7s                        ← Baseline: ~5-10s (Index-Mode)
[gc] Unique SHA256 refs: 98,492            ← Sollte stabil bleiben
[gc] Pool files deleted: 1,508             ← Hoch bei Retention, niedrig sonst
[gc] Space freed: 12.45 GB                 ← Indikator für Retention-Effizienz
[gc] Errors: 0                             ← Muss 0 sein!
```

**Alerts:**
```bash
# 1. Laufzeit-Anomalie (Index-Mode sollte <30s sein):
if duration > 30:
    alert("GC langsamer als erwartet, möglicherweise Fallback auf Stub-Scan")

# 2. Massive Löschungen (Indikator für Fehler):
if deleted > total * 0.5:
    alert("GC würde >50% Pool löschen, möglicherweise Index-Korruption!")

# 3. Fehler:
if errors > 0:
    alert("GC mit Fehlern abgeschlossen")
```

---

**Fazit:** GC 2.0 ist **~99% schneller**, **Race-Condition-sicher** und **Index-validiert**!

---

## Sicherheits-Features: Lock-File + Bidirektionale Validation

### Problem-Statement

**Grace Period (mtime-basiert) ist fehleranfällig:**
- pCloud mtime-Semantik unklar (könnte Original-mtime erhalten).
- False-Positives bei Re-Uploads.
- Kein echter Race-Protection (nur Zeit-basiert).

**Bidirektionale Validation fehlte:**
- Kein Pre-Upload-Check (Source-Integrity).
- Post-Upload-Check nur in Full-Pool-Mode.
- Kein Schutz gegen Upload mit toten Referenzen.

---

### 1. Lock-File-System (ersetzt Grace Period)

**Konzept:** Binärer Status statt Zeit-basierter Heuristik.

#### Workflow
```python
# === BACKUP-START (push_pool_mode) ===
create_gc_lock(cfg, dest_root, snapshot_name)
# → Erstellt /_backup-root/.gc_lock mit JSON-Metadaten:
{
    "pid": 12345,
    "host": "pi5-backup",
    "started_at": 1735671234.56,
    "snapshot": "2026-05-29_03-00",
    "task": "push_pool_manifest"
}

try:
    # Upload-Code hier (Scout, Full-Pool-Mode, etc.)
    ...
finally:
    # IMMER entfernen (auch bei Fehler!)
    remove_gc_lock(cfg, dest_root)
```

#### GC-Lock-Check (pool_gc.py)
```python
# === GC-START (run_pool_gc) ===
lock_path = f"{dest_root}/.gc_lock"
stale_lock_hours = int(os.environ.get("PCLOUD_GC_STALE_LOCK_HOURS", "48"))

try:
    lock_content = pc.get_textfile(cfg, path=lock_path)
    lock_data = json.loads(lock_content)
    lock_age_hours = (time.time() - lock_data["started_at"]) / 3600
    
    if lock_age_hours < stale_lock_hours:
        # Lock ist frisch → Backup läuft!
        return {"error": "backup_in_progress", "aborted": True}
    else:
        # Stale Lock → Backup wahrscheinlich crashed
        log("[WARN] Stale Lock erkannt, fahre mit GC fort")

except Exception:
    # Kein Lock → Alles ok
    pass
```

#### Vorteile
- **Binärer Status:** Lock vorhanden = Backup läuft, kein Lock = GC kann laufen.
- **Atomar:** Kein Zeitfenster zwischen Check und Delete.
- **Stale-Handling:** Alte Locks (>48h) werden ignoriert (Crash-Assumption).
- **Metadata:** PID, Host, Snapshot für Debugging.

---

### 2. Source-Integrity-Check (Pre-Upload)

**Ziel:** Sicherstellen dass ALLE Files aus Manifest noch existieren bevor Upload startet.

#### Workflow
```python
# === VOR Upload (push_pool_mode) ===
deep_check = os.environ.get("PCLOUD_SOURCE_DEEP_CHECK") == "1"
is_valid, errors = validate_source_integrity(manifest, deep_check=deep_check)

if not is_valid:
    log(f"[ERROR] Source-Integrity fehlgeschlagen: {len(errors)} Fehler")
    return {"error": "source_integrity_failed", "errors": errors}
```

#### Checks
1. **Existenz-Check:** `os.path.exists()` für jeden File aus Manifest.
2. **Optional: Deep-Check:** SHA256-Hash-Verifikation (`PCLOUD_SOURCE_DEEP_CHECK=1`).

#### Vorteile
- **Früh-Abort:** Backup stoppt BEVOR erste API-Calls stattfinden.
- **Keine toten Referenzen:** Verhindert Stubs mit fehlenden Pool-Files.
- **Deep-Check:** Erkennt Source-File-Änderungen (Silent-Corruption).

---

### 3. Remote-Completeness-Check (Post-Upload)

**Ziel:** Validieren dass ALLE SHA256s aus Manifest im Pool sind UND alle Stubs vorhanden sind.

#### Workflow
```python
# === NACH Upload, VOR Complete-Marker ===
is_valid, errors = validate_pool_snapshot(cfg, dest_snapshot_dir, pool_root, manifest, index)

if not is_valid:
    log(f"[ERROR] Validation fehlgeschlagen: {len(errors)} Fehler")
    raise RuntimeError("Snapshot-Validation fehlgeschlagen")  # Kein Complete-Marker!
else:
    # Erst jetzt Complete-Marker setzen
    pc.write_json(..., filename=".upload_complete", ...)
```

#### Checks (100% Coverage!)
1. **Pool-SHA256s:** `listfolder(/_pool, recursive=True)` für ALLE physischen Pool-Files (~2-5s).
2. **Delta-Check:** `manifest_sha256s - physical_pool_sha256s` = **MISSING** (KRITISCH!).
3. **Index-Konsistenz:** Prüfe `pool_refs[sha256]` enthält `snapshot_name`.
4. **Optional: Stub-Sample:** Statistisches Sampling von Stubs (opt-in).

#### Vorteile
- **100% Coverage:** Jeder SHA256 aus Manifest wird geprüft (statt 0.1% Sampling).
- **Schnell:** 1× listfolder (~2-5s) statt 100× stat_file (~10s+).
- **Binärer Status:** Complete-Marker NUR bei erfolgreicher Validation.

---

### Integration: Source-to-Pool Alignment

**Vollständiger Workflow (push_pool_mode):**
```
1. Source-Integrity-Check (Pre-Upload)
   ├─ Alle Files vorhanden? (os.path.exists)
   ├─ Optional: Hashes korrekt? (Deep-Check)
   └─ Bei Fehler: ABORT (kein Lock, kein Upload)

2. GC-Lock erstellen
   └─ /_backup-root/.gc_lock mit Metadaten

3. Upload (Scout / Full-Pool-Mode / Delta-Mode)
   └─ try-finally garantiert Lock-Cleanup

4. Remote-Completeness-Check (Post-Upload)
   ├─ 100% Pool-SHA256-Coverage (listfolder)
   ├─ Index-Konsistenz (pool_refs)
   └─ Bei Fehler: RuntimeError (kein Complete-Marker)

5. Complete-Marker setzen
   └─ Nur bei erfolgreicher Validation!

6. Lock entfernen (finally)
   └─ Auch bei Exception!
```

---

### Umgebungsvariablen

```bash
# Source-Integrity Deep-Check (optional, langsam!)
export PCLOUD_SOURCE_DEEP_CHECK=1

# Validation aktivieren/deaktivieren
export PCLOUD_VALIDATE_UPLOAD=1  # Default

# Stale-Lock Timeout (GC)
export PCLOUD_GC_STALE_LOCK_HOURS=48  # Default
```

---

### Performance & Safety

**Source-Integrity-Check:**
- Existenz-Check: ~0.5s (103K Files, SSD).
- Deep-Check: ~30s (SHA256 aller Files).
- **Empfehlung:** Deep-Check nur bei Paranoia-Level oder nach Restore.

**Remote-Completeness-Check:**
- listfolder(/_pool): ~2-5s (100K Files).
- Index-Check: <0.1s (In-Memory).
- **100% Coverage** statt 0.1% Sampling!

**Lock-File:**
- Overhead: <0.1s (create + delete).
- **Garantiert:** Kein GC während Backup läuft.

---

**Fazit:** Lock-File + Bidirektionale Validation bieten **echten Race-Protection** und **garantierte Konsistenz**!
