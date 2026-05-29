# Kritische Analyse: Grace Period & Bidirektionale Validation

**Datum:** 29. Mai 2026  
**Status:** ANALYSE & EMPFEHLUNGEN (Nicht implementiert)

---

## A) GRACE PERIOD (mtime-Check) - KRITISCHE SCHWÄCHEN

### 1. Problem-Statement: Race-Condition

**Original-Motivation:**
```
Timeline:
10:00 Uhr: Backup startet, uploaded File "abc123..." → /_pool/ab/abc123...
10:05 Uhr: GC startet parallel
10:05 Uhr: GC sieht: "abc123 hat keinen Stub → unreferenziert"
10:05 Uhr: GC löscht "abc123..."
10:10 Uhr: Backup will Stub erstellen → Pool-File fehlt → KORRUPTION!
```

**Lösung (aktuell):** Grace Period via mtime-Check
```python
grace_cutoff = time.time() - (grace_hours * 3600)
if pool_file["modified"] > grace_cutoff:
    keep()  # File zu jung, könnte parallel uploaded sein
```

---

### 2. KRITISCHE SCHWÄCHE #1: pCloud mtime-Semantik unklar

#### Problem: Was ist "modified" genau?

**Szenario A: uploadfile() MIT mtime-Preservation**
```python
# Falls pCloud API mtime von Original-File übernimmt:
pc.uploadfile(cfg, path="/_pool/ab/abc123...", 
              local_file="/backup/rtb/2026-05-29/old_file.txt")

# Original-File: 2024-01-15 (vor 1.5 Jahren!)
# → pool_file["modified"] = 2024-01-15
# → Grace-Check: 2024-01-15 > (2026-05-29 - 24h) → FALSE!
# → File wird GELÖSCHT obwohl gerade uploaded! ❌
```

**Szenario B: uploadfile() OHNE mtime-Preservation**
```python
# Falls pCloud API Upload-Zeit als mtime setzt:
# → pool_file["modified"] = 2026-05-29 10:00
# → Grace-Check: 2026-05-29 10:00 > (2026-05-29 10:05 - 24h) → TRUE!
# → File wird GESCHÜTZT ✓
```

**Szenario C: copyfolder() in Turbo-Delta-Mode**
```python
# Was passiert bei serverseitigem Copy?
pc.copyfolder(from="/_snapshots/2026-05-28-200014",
              to="/_snapshots/2026-05-29-080000")

# Werden mtimes kopiert oder neu gesetzt?
# → UNKLAR aus pCloud API Dokumentation!
# → Potenzielles Risiko bei Scout-basierten Backups
```

#### Aktueller Code-Status

**Unser Upload-Code (pcloud_push_json_pool_manifest_to_pcloud.py):**
```python
# Zeile 604-750: _upload_file_smart()
def _upload_file_smart(cfg, local_path, remote_path, *, dry=False):
    return pc.call_with_backoff(pc.upload_file, cfg,
                                local_path=local_path,
                                remote_path=remote_path)
    # ← Nutzt pc.upload_file() aus pcloud_bin_lib.py
    # ← Keine explizite mtime-Parameter sichtbar
```

**Mögliche Szenarien:**
1. **pCloud API preserviert mtime standardmäßig** → Grace Period versagt!
2. **pCloud API ignoriert mtime standardmäßig** → Grace Period funktioniert
3. **pCloud API verhalten ist API-Version-abhängig** → Unvorhersehbar

---

### 3. KRITISCHE SCHWÄCHE #2: Timezone-Probleme

```python
# Server-Zeit (pCloud):
pool_file["modified"] = 1716961200  # Unix-Timestamp (UTC?)

# Client-Zeit (Raspberry Pi):
grace_cutoff = time.time() - (24 * 3600)  # Python time.time() (UTC?)

# Problem: Sind beide wirklich UTC?
# - pCloud API Doku sagt: "Unix timestamp" (implizit UTC)
# - Python time.time() ist UTC (POSIX-Standard)
# - ABER: Bei 24h Grace meist egal, bei 1h Grace kritisch!
```

**Clock-Skew Risiko:**
```
Server-Zeit: 2026-05-29 10:00:00 UTC
Client-Zeit: 2026-05-29 10:05:00 UTC (5 Min voraus)

Grace: 1 Stunde
File uploaded: Server-Zeit 10:00
GC prüft: Client-Zeit 10:05
Cutoff: 10:05 - 1h = 09:05
File-mtime: 10:00 > 09:05 → geschützt ✓

# Aber bei 5 Min Grace:
Cutoff: 10:05 - 5min = 10:00
File-mtime: 10:00 = 10:00 → EDGE-CASE!
# → Abhängig von Rounding/Precision
```

---

### 4. KRITISCHE SCHWÄCHE #3: False-Positives

**Szenario: Re-Upload nach Retention**
```
Tag 1: 
  - File "abc123..." uploaded
  - Stub erstellt
  - Snapshot vollständig

Tag 30: 
  - Retention löscht Snapshot (inkl. Stub!)
  - Pool-File "abc123..." bleibt (noch referenced von anderem Snapshot)

Tag 60:
  - Letzter Snapshot auch gelöscht
  - Pool-File "abc123..." ist jetzt Orphan
  - ABER: File wird re-uploaded (weil in neuer Source wieder vorhanden)
  - mtime = Tag 60 (aktuell!)

Tag 61:
  - GC läuft
  - File "abc123..." ist unreferenziert (Stub fehlt)
  - Grace-Check: mtime (Tag 60) > cutoff (Tag 60) → GESCHÜTZT!
  - Obwohl legitim löschbar!
```

**Folge:** Files bleiben unnötig im Pool (Speicherplatz-Leak)

---

### 5. KRITISCHE SCHWÄCHE #4: Kein echter Race-Protection

**Problem:** Grace Period schützt nur "junge" Files, aber:

```
Race-Condition-Window:

10:00:00  Backup: Upload File → Pool
10:00:05  GC: Scan Pool (File bereits da!)
10:00:10  GC: Load Index (File NOCH NICHT im Index!)
10:00:15  Backup: Update Index + Stub
10:00:20  GC: Check File (unreferenziert) + Grace (mtime aktuell) → GESCHÜTZT ✓

# Funktioniert!

ABER:

10:00:00  Backup: Upload File → Pool (mtime = 09:00, alte Datei!)
10:00:05  GC: Scan Pool (File bereits da!)
10:00:10  GC: Load Index (File NOCH NICHT im Index!)
10:00:15  GC: Check File (unreferenziert) + Grace (mtime = 09:00) → NICHT GESCHÜTZT! ❌
10:00:20  GC: DELETE File
10:00:25  Backup: Update Index + Stub → Pool-File FEHLT → KORRUPTION!
```

---

## VORSCHLÄGE: Robuste Alternativen zur Grace Period

### **OPTION 1: Upload-Complete-Marker Check (EMPFOHLEN)**

**Konzept:** GC prüft ob Snapshots vollständig sind

```python
def get_safe_orphans(cfg, dest_root, referenced_sha256s):
    """
    Findet Orphans, aber schützt incomplete Snapshots.
    """
    snapshots_root = f"{dest_root}/_snapshots"
    
    # 1. Finde alle Snapshots
    snapshots = list_snapshots(cfg, snapshots_root)
    
    # 2. Identifiziere incomplete Snapshots
    protected_sha256s = set()
    
    for snapshot in snapshots:
        marker_path = f"{snapshots_root}/{snapshot}/.upload_complete"
        
        if not exists(cfg, marker_path):
            # Snapshot ist incomplete → ALLE Pool-Files dieses Snapshots schützen!
            manifest = load_snapshot_manifest(cfg, snapshot)
            snapshot_sha256s = {item["sha256"] for item in manifest["items"]}
            protected_sha256s.update(snapshot_sha256s)
            
            log(f"[gc-protect] Snapshot {snapshot} incomplete, protecting {len(snapshot_sha256s)} SHA256s")
    
    # 3. Delta: Nur löschen was weder referenziert noch protected ist
    pool_sha256s = scan_pool_files(cfg, dest_root)
    deletable = pool_sha256s - referenced_sha256s - protected_sha256s
    
    return deletable

# Vorteil:
# - Expliziter Check auf Upload-Status
# - Keine mtime-Abhängigkeit
# - 100% sicher für parallel laufende Backups
```

---

### **OPTION 2: Lock-File Mechanismus (EINFACHST)**

**Konzept:** Backup erstellt Lock, GC prüft Lock

```python
# In pcloud_push_json_pool_manifest_to_pcloud.py:
def push_pool_mode(...):
    lock_path = f"{dest_root}/_snapshots/.gc_lock"
    
    try:
        # Lock erstellen (atomic)
        pc.write_json_at_path(cfg, lock_path, {
            "snapshot": snapshot_name,
            "started": time.time(),
            "pid": os.getpid()
        })
        
        # Upload durchführen...
        
    finally:
        # Lock entfernen
        pc.delete_file(cfg, path=lock_path)


# In pcloud_pool_gc.py:
def run_pool_gc(cfg, dest_root, ...):
    lock_path = f"{dest_root}/_snapshots/.gc_lock"
    
    # Lock-Check
    if exists(cfg, lock_path):
        lock_data = load_json(cfg, lock_path)
        age_seconds = time.time() - lock_data["started"]
        
        if age_seconds < 86400:  # Lock < 24h alt
            abort(f"Backup läuft (Snapshot: {lock_data['snapshot']}), GC abgebrochen!")
        else:
            # Stale Lock (> 24h alt) → Backup wahrscheinlich crashed
            log(f"[warn] Stale lock detected (age: {age_seconds/3600:.1f}h), proceeding...")
    
    # GC durchführen...

# Vorteile:
# - Sehr einfach zu implementieren
# - Explizite Koordination zwischen Backup & GC
# - Keine mtime-Abhängigkeit

# Nachteile:
# - Backup-Crash hinterlässt Stale Lock (muss manuell geprüft werden)
```

---

### **OPTION 3: Transaction-Log (ROBUST, aber KOMPLEX)**

**Konzept:** Backup schreibt Log, GC liest Log

```python
# In pcloud_push_json_pool_manifest_to_pcloud.py:
def push_pool_mode(...):
    log_path = f"{dest_root}/_snapshots/.upload_log.jsonl"
    
    # Start-Event
    append_to_log(cfg, log_path, {
        "event": "backup_started",
        "snapshot": snapshot_name,
        "timestamp": time.time()
    })
    
    # Während Upload: Track uploaded Files
    for file in files_to_upload:
        pool_sha256 = upload_to_pool(file)
        
        append_to_log(cfg, log_path, {
            "event": "pool_uploaded",
            "snapshot": snapshot_name,
            "sha256": pool_sha256,
            "timestamp": time.time()
        })
    
    # Complete-Event
    append_to_log(cfg, log_path, {
        "event": "backup_completed",
        "snapshot": snapshot_name,
        "timestamp": time.time()
    })


# In pcloud_pool_gc.py:
def run_pool_gc(cfg, dest_root, ...):
    log_path = f"{dest_root}/_snapshots/.upload_log.jsonl"
    
    # Parse Log
    log_entries = load_jsonl(cfg, log_path)
    
    # Finde in-progress Backups
    in_progress_snapshots = {}
    
    for entry in log_entries:
        snapshot = entry["snapshot"]
        
        if entry["event"] == "backup_started":
            in_progress_snapshots[snapshot] = {
                "started": entry["timestamp"],
                "protected_sha256s": set()
            }
        
        elif entry["event"] == "pool_uploaded":
            if snapshot in in_progress_snapshots:
                in_progress_snapshots[snapshot]["protected_sha256s"].add(entry["sha256"])
        
        elif entry["event"] == "backup_completed":
            in_progress_snapshots.pop(snapshot, None)
    
    # Sammle alle protected SHA256s
    protected_sha256s = set()
    for snapshot, data in in_progress_snapshots.items():
        age = time.time() - data["started"]
        
        if age < 86400:  # Backup < 24h alt
            protected_sha256s.update(data["protected_sha256s"])
            log(f"[gc-protect] In-progress: {snapshot}, protecting {len(data['protected_sha256s'])} SHA256s")
        else:
            log(f"[warn] Stale backup: {snapshot} (age: {age/3600:.1f}h), not protecting")
    
    # GC mit Protected SHA256s...

# Vorteile:
# - Sehr robust
# - Granular tracking
# - Kann auch für Monitoring/Debugging genutzt werden

# Nachteile:
# - Komplexer zu implementieren
# - Log kann groß werden (braucht Rotation)
# - Append-to-remote-file in pCloud nicht trivial (download → append → upload)
```

---

### **OPTION 4: Index-basierte Protection (HYBRID)**

**Konzept:** Kombiniere Index-Check mit Grace-Period-Fallback

```python
def get_deletable_files(cfg, dest_root, grace_hours=24):
    # 1. Lade Index (pool_refs)
    index = load_content_index(cfg, f"{dest_root}/_snapshots")
    referenced_sha256s = set(index.get("pool_refs", {}).keys())
    
    # 2. Scanne Pool
    pool_files = scan_pool_files_with_metadata(cfg, f"{dest_root}/_pool")
    
    # 3. Klassifiziere
    deletable = []
    grace_cutoff = time.time() - (grace_hours * 3600)
    
    for pool_file in pool_files:
        sha256 = pool_file["name"]
        
        # Check 1: Referenziert im Index?
        if sha256 in referenced_sha256s:
            continue  # Keep
        
        # Check 2: Upload-Complete-Marker vorhanden?
        # (Implizit: Wenn Index vollständig ist, sind alle Snapshots complete)
        # → Index-Check reicht eigentlich!
        
        # Check 3: FALLBACK Grace-Period (nur als Safety-Net)
        if grace_hours > 0 and pool_file["modified"] > grace_cutoff:
            log(f"[gc-grace] Fallback-Protection: {sha256[:16]}... (age < {grace_hours}h)")
            continue  # Keep
        
        # Unreferenziert & alt genug → löschen
        deletable.append(pool_file)
    
    return deletable

# Vorteil:
# - Index-Check ist primär (robust)
# - Grace-Period nur als Fallback (Safety-Net für Edge-Cases)
# - Kombiniert beste aus beiden Welten
```

---

## B) BIDIREKTIONALE VALIDATION - KRITISCHE LÜCKE!

### 1. Problem-Statement: Einseitige Validation

**AKTUELLER ZUSTAND:**
```python
# validate_pool_snapshot() prüft:
✓ Pool-Files physisch vorhanden?
✓ Stubs physisch vorhanden?
✓ Index-Konsistenz (pool_refs)?

# Pool-GC prüft:
✓ Unreferenzierte Pool-Files löschen?

# ABER: Was fehlt?
❌ Sind ALLE Source-Files remote vorhanden?
❌ Ist Remote identisch mit Source?
❌ Können Files wirklich restored werden?
```

---

### 2. KRITISCHE SZENARIEN

#### **Szenario 1: Partial Upload Failure**

```
Timeline:
10:00  Backup startet: 1000 Files zu uploaden
10:30  File 500: Netzwerk-Fehler → Upload abgebrochen
10:30  Prozess crasht → .upload_complete Marker wird NICHT gesetzt
11:00  Nächster Backup-Run: Sieht Snapshot als "incomplete" → SKIP oder RETRY?
       → Aktuell: Keine Logik dafür!
       → 500 Files fehlen dauerhaft!
```

**Auswirkung:**
- Snapshot sieht vollständig aus (500 Files sind da)
- ABER: 500 Files fehlen
- Bei Restore: 50% der Daten nicht wiederherstellbar!

---

#### **Szenario 2: Silent Server-Side Corruption**

```
Timeline:
10:00  Upload erfolgreich: 1000 Files → Pool
10:05  Stubs erstellt: 1000 Stubs
10:10  .upload_complete Marker gesetzt
11:00  pCloud Server-Fehler: 50 Pool-Files verschwinden (Bug, Disk-Fehler, etc.)

Folge:
- Stubs zeigen auf nicht-existente Pool-Files
- validate_pool_snapshot() würde das erkennen (listfolder-Check)
- ABER: Wird nicht automatisch ausgeführt!
- Bei Restore: 50 Files nicht wiederherstellbar!
```

---

#### **Szenario 3: Index-Desync (Preflight-Fix hat das teilweise gelöst)**

```
Vor Preflight-Fix:
- Pool-File existiert physisch
- ABER: Nicht im Index (pool_refs fehlt Eintrag)
- Snapshot-Validation: "Pool-File da" → ✓
- GC: "Nicht im Index" → DELETE! ❌

Nach Preflight-Fix:
- Preflight scannt Pool physisch
- Index-Reparatur: Fehlendes pool_refs wird hinzugefügt ✓

ABER: Was wenn GC VOR Preflight läuft?
- GC: Sieht "nicht im Index" → DELETE!
- Preflight: Sieht "Pool-File fehlt" → Upload!
- → Unnötiger Re-Upload (Bandbreite verschwendet)
```

---

### 3. FEHLENDE VALIDIERUNGEN

#### **Validation #1: Source-Integrity-Check (Pre-Upload)**

**Problem:** Manifest könnte auf nicht-existente Source-Files zeigen

```python
def validate_source_integrity(manifest_path: str) -> tuple[bool, list]:
    """
    Prüft ob alle Files aus dem Manifest noch in der Source existieren.
    
    WICHTIG: Vor Upload-Start ausführen!
    
    Returns:
        (is_valid, errors)
    """
    manifest = load_manifest(manifest_path)
    errors = []
    
    for item in manifest.get("items", []):
        if item.get("type") != "file":
            continue
        
        source_path = item.get("source_path")
        expected_sha256 = item.get("sha256")
        
        # Check 1: File existiert?
        if not os.path.exists(source_path):
            errors.append(f"Source-File fehlt: {source_path}")
            continue
        
        # Check 2: Hash stimmt überein?
        # (Optional, langsam bei großen Files!)
        if os.environ.get("PCLOUD_VALIDATE_SOURCE_HASH") == "1":
            actual_sha256 = hash_file(source_path)
            if actual_sha256 != expected_sha256:
                errors.append(f"Source-File geändert seit Manifest: {source_path}")
    
    return (len(errors) == 0, errors)


# Verwendung in push_pool_mode():
is_valid, errors = validate_source_integrity(manifest_path)
if not is_valid:
    log("[ERROR] Source-Integrity-Check fehlgeschlagen!")
    for err in errors:
        log(f"  - {err}")
    abort("Backup abgebrochen (Source inkonsistent)")
```

**Wann ausführen:** Vor jedem Upload-Start

---

#### **Validation #2: Remote-Completeness-Check (Post-Upload)**

**Problem:** Upload könnte unvollständig sein (Netzwerk-Fehler, Crash, etc.)

```python
def validate_remote_completeness(cfg: dict, manifest: dict, dest_root: str) -> tuple[bool, list]:
    """
    Prüft ob ALLE Files aus dem Manifest remote vorhanden sind.
    
    ZWEI-STUFEN-CHECK:
    1. Pool-Files physisch vorhanden?
    2. Stubs physisch vorhanden?
    
    Returns:
        (is_complete, errors)
    """
    errors = []
    snapshot_name = manifest["snapshot"]
    
    # 1. Pool-Check (physisch)
    manifest_sha256s = {
        item["sha256"] for item in manifest["items"] 
        if item.get("type") == "file" and item.get("sha256")
    }
    
    # Scanne Pool (via listfolder, wie in validate_pool_snapshot)
    pool_root = f"{dest_root}/_pool"
    result = pc.listfolder(cfg, path=pool_root, recursive=True, nofiles=False)
    
    pool_sha256s = set()
    def _extract_sha256s(obj):
        if not obj.get("isfolder") and obj.get("name"):
            filename = obj["name"]
            if len(filename) == 64 and all(c in "0123456789abcdef" for c in filename):
                pool_sha256s.add(filename.lower())
        for child in obj.get("contents", []):
            _extract_sha256s(child)
    
    _extract_sha256s(result.get("metadata", {}))
    
    missing_in_pool = manifest_sha256s - pool_sha256s
    if missing_in_pool:
        for sha in list(missing_in_pool)[:10]:
            errors.append(f"Pool-File fehlt: {sha[:16]}...")
        if len(missing_in_pool) > 10:
            errors.append(f"... und {len(missing_in_pool)-10} weitere")
    
    # 2. Stub-Check (physisch)
    snapshot_dir = f"{dest_root}/_snapshots/{snapshot_name}"
    
    for item in manifest["items"]:
        if item.get("type") != "file":
            continue
        
        relpath = item["relpath"]
        stub_path = f"{snapshot_dir}/{relpath}.meta.json"
        
        # Check via stat_file (schnell)
        if not stat_file_safe(cfg, path=stub_path):
            errors.append(f"Stub fehlt: {relpath}")
    
    return (len(errors) == 0, errors)


# Verwendung in push_pool_mode():
is_complete, errors = validate_remote_completeness(cfg, manifest, dest_root)
if not is_complete:
    log("[ERROR] Remote-Completeness-Check fehlgeschlagen!")
    for err in errors[:20]:  # Erste 20 zeigen
        log(f"  - {err}")
    
    # RETRY-Logik (optional):
    if os.environ.get("PCLOUD_RETRY_INCOMPLETE") == "1":
        log("[retry] Versuche fehlende Files erneut hochzuladen...")
        # Re-Upload nur fehlende Files...
    else:
        abort("Backup unvollständig! (Siehe Fehler oben)")
```

**Wann ausführen:** Nach jedem Upload, VOR .upload_complete Marker

---

#### **Validation #3: Restore-Test (Paranoia-Level)**

**Problem:** Files könnten remote existieren, aber korrupt sein

```python
def validate_restorability(cfg: dict, manifest: dict, dest_root: str, sample_size: int = 100) -> tuple[bool, list]:
    """
    Stichproben-Test: Können Files wirklich restored werden?
    
    ULTIMATIVER TEST: Download + Hash-Vergleich
    
    WARNUNG: Langsam! Nur für kritische Validierung.
    
    Returns:
        (is_restorable, errors)
    """
    errors = []
    snapshot_name = manifest["snapshot"]
    snapshot_dir = f"{dest_root}/_snapshots/{snapshot_name}"
    
    # Sample auswählen (zufällig)
    manifest_files = [it for it in manifest["items"] if it.get("type") == "file"]
    sample = random.sample(manifest_files, min(sample_size, len(manifest_files)))
    
    log(f"[restore-test] Teste {len(sample)} Files (Stichprobe)...")
    
    for item in sample:
        relpath = item["relpath"]
        expected_sha256 = item["sha256"]
        
        try:
            # 1. Lade Stub
            stub_path = f"{snapshot_dir}/{relpath}.meta.json"
            stub_content = pc.get_textfile(cfg, path=stub_path)
            stub = json.loads(stub_content)
            
            pool_fileid = stub.get("pool_fileid")
            if not pool_fileid:
                errors.append(f"Stub korrupt (kein pool_fileid): {relpath}")
                continue
            
            # 2. Download Pool-File (via FileID)
            # WARNUNG: Große Files können lange dauern!
            file_size = item.get("size", 0)
            
            if file_size > 100 * 1024 * 1024:  # > 100 MB
                log(f"[restore-test] Skip large file: {relpath} ({file_size/1024**2:.1f} MB)")
                continue
            
            pool_content = pc.download_file_by_id(cfg, fileid=pool_fileid)
            
            # 3. Hash-Vergleich
            actual_sha256 = hashlib.sha256(pool_content).hexdigest().lower()
            
            if actual_sha256 != expected_sha256:
                errors.append(f"CORRUPTION: {relpath} (hash mismatch!)")
                errors.append(f"  Expected: {expected_sha256}")
                errors.append(f"  Actual:   {actual_sha256}")
        
        except Exception as e:
            errors.append(f"Restore-Test fehlgeschlagen für {relpath}: {e}")
    
    return (len(errors) == 0, errors)


# Verwendung (optional, nur bei wichtigen Backups):
if os.environ.get("PCLOUD_PARANOID_VALIDATION") == "1":
    is_restorable, errors = validate_restorability(cfg, manifest, dest_root, sample_size=100)
    if not is_restorable:
        log("[ERROR] Restore-Test fehlgeschlagen!")
        for err in errors:
            log(f"  - {err}")
        alert("CRITICAL: Backup nicht wiederherstellbar!")
```

**Wann ausführen:** 
- Nach Upload (optional, langsam!)
- Periodisch via Cron (z.B. wöchentlich auf neuesten Snapshot)
- Vor Retention (um sicherzustellen dass alte Snapshots ok sind)

---

#### **Validation #4: Differential Sync Check**

**Problem:** Source und Remote können divergieren (gelöschte Files, geänderte Files)

```python
def validate_source_vs_remote(source_path: str, dest_root: str, snapshot_name: str, cfg: dict) -> dict:
    """
    Vergleicht lokale Source mit Remote-Snapshot.
    
    FINDET:
    - Missing Remote: Files in Source aber nicht Remote
    - Orphaned Remote: Files Remote aber nicht in Source
    - Hash Mismatches: Files mit gleichem Path aber unterschiedlichem Hash
    
    Returns:
        {
            "missing_remote": [...],
            "orphaned_remote": [...],
            "mismatches": [...]
        }
    """
    log(f"[diff-check] Vergleiche Source vs. Remote...")
    
    # 1. Scanne Source (lokal, schnell!)
    source_files = {}
    
    for root, dirs, files in os.walk(source_path):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, source_path)
            
            # Hash berechnen (kann lange dauern!)
            sha256 = hash_file(abs_path)
            source_files[rel_path] = sha256
    
    log(f"[diff-check] Source: {len(source_files)} Files")
    
    # 2. Scanne Remote (via Stubs)
    remote_files = {}
    snapshot_dir = f"{dest_root}/_snapshots/{snapshot_name}"
    
    # Liste alle Stubs rekursiv
    result = pc.listfolder(cfg, path=snapshot_dir, recursive=True, nofiles=False)
    
    stub_paths = []
    def _collect_stubs(obj):
        if not obj.get("isfolder") and obj.get("name", "").endswith(".meta.json"):
            stub_paths.append(obj["path"])
        for child in obj.get("contents", []):
            _collect_stubs(child)
    
    _collect_stubs(result.get("metadata", {}))
    
    log(f"[diff-check] Remote: {len(stub_paths)} Stubs gefunden")
    
    # Parallel Stubs laden (kann lange dauern!)
    def _load_stub(stub_path):
        try:
            stub_content = pc.get_textfile(cfg, path=stub_path)
            stub = json.loads(stub_content)
            relpath = stub.get("relpath")
            sha256 = stub.get("sha256")
            if relpath and sha256:
                return (relpath, sha256)
        except Exception:
            pass
        return None
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = ex.map(_load_stub, stub_paths)
        for result in results:
            if result:
                relpath, sha256 = result
                remote_files[relpath] = sha256
    
    log(f"[diff-check] Remote: {len(remote_files)} Files (aus Stubs)")
    
    # 3. Differential
    source_set = set(source_files.keys())
    remote_set = set(remote_files.keys())
    
    missing_remote = source_set - remote_set
    orphaned_remote = remote_set - source_set
    
    # 4. Hash-Vergleich (nur gemeinsame Paths)
    common = source_set & remote_set
    mismatches = []
    
    for path in common:
        if source_files[path] != remote_files[path]:
            mismatches.append({
                "path": path,
                "source_sha": source_files[path],
                "remote_sha": remote_files[path]
            })
    
    log(f"[diff-check] Missing Remote: {len(missing_remote)}")
    log(f"[diff-check] Orphaned Remote: {len(orphaned_remote)}")
    log(f"[diff-check] Mismatches: {len(mismatches)}")
    
    return {
        "missing_remote": list(missing_remote),
        "orphaned_remote": list(orphaned_remote),
        "mismatches": mismatches
    }


# Verwendung (periodisch via Cron):
diff = validate_source_vs_remote(
    source_path="/mnt/backup/rtb/latest",
    dest_root="/Backup/rtb_1to1",
    snapshot_name="2026-05-29-080000",
    cfg=cfg
)

if diff["missing_remote"]:
    alert(f"WARNING: {len(diff['missing_remote'])} files missing in remote!")
    # Optional: Trigger re-upload

if diff["mismatches"]:
    alert(f"CRITICAL: {len(diff['mismatches'])} hash mismatches!")
    # Optional: Trigger investigation

if diff["orphaned_remote"]:
    log(f"INFO: {len(diff['orphaned_remote'])} orphaned files in remote (wurden lokal gelöscht)")
    # Optional: Trigger cleanup
```

**Wann ausführen:**
- Periodisch via Cron (z.B. täglich auf neuesten Snapshot)
- Nach großen Retention-Cleanups
- Vor wichtigen Restores

---

## ZUSAMMENFASSUNG & EMPFEHLUNGEN

### A) Grace Period - NICHT EMPFOHLEN als primäre Methode

**Probleme:**
1. ❌ mtime-Semantik unklar (pCloud API)
2. ❌ False-Positives bei Re-Uploads
3. ❌ Kein echter Race-Protection
4. ❌ Timezone/Clock-Skew Risiken

**Empfehlung:** 
- **PRIMÄR:** Option 1 (Upload-Complete-Marker Check) oder Option 2 (Lock-File)
- **FALLBACK:** Grace Period nur als Safety-Net (nicht als Haupt-Protection)

**Priorität:** 🔴 HOCH (Grace Period kann in Edge-Cases versagen)

---

### B) Bidirektionale Validation - KRITISCHE LÜCKE

**Fehlende Validierungen:**
1. ❌ Source-Integrity-Check (Pre-Upload)
2. ❌ Remote-Completeness-Check (Post-Upload)
3. ❌ Restore-Test (Paranoia-Level)
4. ❌ Differential Sync Check (Source vs. Remote)

**Empfehlung:**
1. **SOFORT implementieren:** Validation #1 (Source-Integrity) + #2 (Remote-Completeness)
2. **Mittelfristig:** Validation #4 (Differential Sync) als Cron-Job
3. **Optional:** Validation #3 (Restore-Test) nur für kritische Backups

**Priorität:** 🔴 KRITISCH (Silent Corruption kann unbemerkt bleiben)

---

## NÄCHSTE SCHRITTE

### Phase 1: Grace Period ersetzen (Prio: HOCH)
1. Implementiere Lock-File Mechanismus (einfachste Lösung)
2. Oder: Upload-Complete-Marker Check (robuster)
3. Behalte Grace Period als Fallback (Safety-Net)

### Phase 2: Bidirektionale Validation (Prio: KRITISCH)
1. Implementiere Source-Integrity-Check (Pre-Upload)
2. Implementiere Remote-Completeness-Check (Post-Upload)
3. Integriere in push_pool_mode() Workflow

### Phase 3: Monitoring & Alerting (Prio: MITTEL)
1. Differential Sync Check als Cron-Job
2. Alerting bei Diskrepanzen
3. Dashboard für Validation-Status

---

**FAZIT:** Grace Period ist fehleranfällig, bidirektionale Validation fehlt komplett. Beide Punkte sollten dringend adressiert werden!
