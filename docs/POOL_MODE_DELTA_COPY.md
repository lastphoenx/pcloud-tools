# Pool-Mode Delta-Copy: Best-Match Scout

**Datum:** 29. Mai 2026  
**Status:** Design-Konzept (nicht implementiert)

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
def scout_best_basis_snapshot(current_manifest_path, archive_dir):
    """
    Findet den besten Basis-Snapshot für Delta-Copy.
    
    Returns:
        (basis_snapshot_name, similarity_score) oder (None, 0.0)
    """
    # 1. Lade aktuelles Manifest
    current_files = load_manifest_index(current_manifest_path)
    # {relpath: sha256} Dictionary für schnellen Lookup
    
    # 2. Iteriere über archivierte Manifeste
    archive_manifests = list_archived_manifests(archive_dir)
    
    best_match = None
    max_similarity = 0.0
    
    for archived in archive_manifests:
        # Vergleiche SHA256-Hashes für identische Pfade
        matches = count_matching_files(current_files, archived)
        similarity = matches / len(current_files) if current_files else 0.0
        
        if similarity > max_similarity:
            max_similarity = similarity
            best_match = archived["snapshot_name"]
    
    # 3. Schwellenwert-Prüfung
    THRESHOLD = 0.70  # 70% Übereinstimmung
    
    if max_similarity >= THRESHOLD:
        return (best_match, max_similarity)
    else:
        return (None, 0.0)
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
def push_pool_turbo_delta_mode(cfg: dict,
                                manifest_path: str,
                                dest_root: str,
                                basis_snapshot: str,
                                snapshot_name: str,
                                dry: bool = False) -> dict:
    """
    Delta-Copy mit copyfolder() als Basis.
    
    Workflow:
    1. copyfolder(basis → new, copycontentonly=True)
    2. Manifest-Diff berechnen
    3. Nur Delta uploaden/updaten
    """
    snapshots_root = f"{dest_root}/_snapshots"
    basis_path = f"{snapshots_root}/{basis_snapshot}"
    new_path = f"{snapshots_root}/{snapshot_name}"
    
    # 1. Struktur kopieren (serverseitig!)
    _log(f"[turbo-delta] Kopiere Struktur von {basis_snapshot}...")
    t0 = time.time()
    
    new_fid = pc.ensure_path(cfg, new_path)
    pc.copyfolder(cfg, from_path=basis_path, to_folderid=new_fid, copycontentonly=True)
    
    elapsed = time.time() - t0
    _log(f"[turbo-delta] ✓ Struktur kopiert ({elapsed:.1f}s)")
    
    # 2. Manifest-Diff
    current = load_manifest(manifest_path)
    basis_manifest = _load_archived_manifest(cfg, snapshots_root, basis_snapshot)
    
    added, changed, deleted = _compute_manifest_diff(current, basis_manifest)
    
    _log(f"[turbo-delta] Diff: +{len(added)} ~{len(changed)} -{len(deleted)}")
    
    # 3. Delta-Sync
    uploaded = 0
    stubs_written = 0
    
    # Added/Changed: Upload + Stub-Update
    for file in (added + changed):
        sha = file["sha256"]
        relpath = file["relpath"]
        
        # Upload to Pool (falls nicht vorhanden)
        if not _anchor_exists_in_index(sha):
            _upload_to_pool(cfg, file, dest_root)
            uploaded += 1
        
        # Update Stub im Snapshot
        _write_stub(cfg, new_path, relpath, file, dry=dry)
        stubs_written += 1
    
    # Deleted: Stubs entfernen (recursive für Ordner!)
    for file in deleted:
        stub_path = f"{new_path}/{file['relpath']}.meta.json"
        _delete_file_safe(cfg, stub_path)
    
    return {
        "uploaded": uploaded,
        "stubs_written": stubs_written,
        "deleted": len(deleted),
        "duration": time.time() - t0
    }
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
