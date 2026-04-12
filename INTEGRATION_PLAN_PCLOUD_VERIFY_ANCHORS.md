# Integration Plan: PCLOUD_VERIFY_ANCHORS=1

> **⚠️ STATUS: NOT IMPLEMENTED**
> 
> This feature was analyzed but **NOT implemented**. Reason: Post-upload verification via `pcloud_quick_delta.py` provides equivalent safety without performance overhead during uploads.
> 
> This document is kept for reference in case future requirements change.

---

## Kontext

**Aktuelles Verhalten (Index-driven Resume):**
- Upload-Entscheidung basiert ausschließlich auf `content_index.json`
- Check 1: Datei im Index für diesen Snapshot? → `resumed`
- Check 2: `anchor_path == None`? → `uploaded`
- **Keine API-Calls** während Resume-Entscheidung → sehr schnell
- **Risiko:** Wenn Index und Remote-Zustand divergieren (manueller Delete, Tamper), werden Dateien fälschlicherweise als "resumed" gezählt

**Geplantes Feature (PCLOUD_VERIFY_ANCHORS=1):**
- Optional: Für jeden Anchor `stat_file()` API-Call
- Verifiziert: Datei existiert wirklich auf pCloud
- Fail-safe bei Index-Inkonsistenzen
- **Kosten:** N API-Calls für N Anchors (bei 20k Dateien → 20k API-Calls)

---

## Design-Entscheidungen

### 1. **Wo integrieren?** → `pcloud_bin_lib.py` (EMPFOHLEN)

**Rationale:**
- ✅ Wiederverwendbar: Auch andere Tools (pcloud_repair_index.py, pcloud_integrity_check.py) profitieren
- ✅ Zentrale Wartung: Ein Ort für Verifikations-Logik
- ✅ Konsistenz: Alle Tools nutzen dieselbe Implementierung
- ❌ Overhead: Lib wird etwas größer (aktuell ~950 Zeilen)

**Alternative: pcloud_push_json_manifest_to_pcloud.py**
- ✅ Direkt am Use-Case
- ❌ Nicht wiederverwendbar
- ❌ Code-Duplikation bei späteren Tools

**Alternative: Separates Tool (pcloud_verify_anchors.py)**
- ❌ Overhead: Neues Skript, neue CLI
- ❌ Nicht passend: Ist keine eigenständige Operation, sondern Option

---

### 2. **API-Strategie** → Batch via `stat_file()` mit Retries

**Einzeln vs. Batch:**
```python
# Option A: Einzeln (EMPFOHLEN)
for anchor_path in anchors:
    stat = pc.stat_file(cfg, path=anchor_path, with_checksum=False)
    # Pro: Einfach, nutzt existierende Funktion
    # Con: N API-Calls (bei Limit 20 req/s → ~17min für 20k)

# Option B: Batch (Zukunft)
# pCloud Binary API hat KEINE Batch-stat_file Operation
# Müsste via listfolder + Path-Matching emuliert werden
# Pro: Schneller (weniger Requests)
# Con: Komplex, fehleranfällig
```

**Empfehlung:** Option A (Einzeln) mit parallelem Batch (Thread-Pool, max 5 parallel)

---

### 3. **Code-Umfang** → ~80-120 Zeilen

**Komponenten:**
1. Helper-Funktion in `pcloud_bin_lib.py`:
   ```python
   def verify_anchor_exists(cfg, anchor_path: str, *, cache: dict = None) -> bool:
       """
       Verifiziert ob anchor_path auf pCloud existiert.
       Nutzt stat_file mit with_checksum=False (schneller).
       
       Args:
           cfg: pCloud config
           anchor_path: Remote-Pfad zum Anchor
           cache: Optional cache {anchor_path: bool}
       
       Returns:
           True wenn Datei existiert, False sonst
       """
       if cache and anchor_path in cache:
           return cache[anchor_path]
       
       try:
           stat = stat_file(cfg, path=anchor_path, with_checksum=False)
           exists = stat.get("fileid") is not None
       except Exception:
           exists = False
       
       if cache is not None:
           cache[anchor_path] = exists
       
       return exists
   ```

2. Integration in `pcloud_push_json_manifest_to_pcloud.py`:
   ```python
   # In push_1to1_mode(), vor Upload-Loop
   verify_anchors = os.environ.get("PCLOUD_VERIFY_ANCHORS") == "1"
   anchor_verify_cache = {}
   
   # Im Upload-Loop für jede Datei
   if sha in items:
       node = items[sha]
       anchor_path = node.get("anchor_path")
       
       # Index-driven Resume
       if this_snapshot in holders:
           if verify_anchors and anchor_path:
               # Fail-safe: Prüfe ob Anchor wirklich existiert
               if not pc.verify_anchor_exists(cfg, anchor_path, cache=anchor_verify_cache):
                   print(f"[warn] Anchor fehlt: {anchor_path} (trotz Index-Eintrag)")
                   # Upload trotzdem durchführen
                   upload_file(...)
               else:
                   resumed += 1
                   continue
           else:
               resumed += 1
               continue
   ```

3. Parallelisierung (Optional, für Performance):
   ```python
   from concurrent.futures import ThreadPoolExecutor
   
   def verify_anchors_parallel(cfg, anchor_paths: list, max_workers=5) -> dict:
       """
       Verifiziert mehrere Anchors parallel.
       Returns: {anchor_path: bool}
       """
       results = {}
       with ThreadPoolExecutor(max_workers=max_workers) as executor:
           futures = {
               executor.submit(verify_anchor_exists, cfg, path): path
               for path in anchor_paths
           }
           for future in concurrent.futures.as_completed(futures):
               path = futures[future]
               results[path] = future.result()
       return results
   ```

**Geschätzte Größe:**
- Helper-Funktion: ~25 Zeilen
- Integration in push_1to1_mode: ~20 Zeilen
- Parallelisierung (optional): ~30 Zeilen
- Tests + Kommentare: ~40 Zeilen
- **Total: ~115 Zeilen**

---

## Performance-Impact

**Ohne PCLOUD_VERIFY_ANCHORS (aktuell):**
- Upload 19,808 Dateien (197 missing)
- Resume-Entscheidung: 0 API-Calls (nur Index-Lookup)
- Zeit: ~15min (nur Upload)

**Mit PCLOUD_VERIFY_ANCHORS=1 (geplant):**
- Upload 19,808 Dateien (197 missing)
- Verify für ~17,928 Anchors (alle existierenden)
- API-Calls: ~17,928 stat_file
- Zeit bei 20 req/s limit: ~17,928 / 20 = 896s ≈ **15min extra**
- **Total: ~30min** (2x so lang)

**Mit Parallelisierung (5 Threads):**
- Effektive Rate: ~100 req/s (5×20)
- Zeit: ~17,928 / 100 = 179s ≈ **3min extra**
- **Total: ~18min** (20% länger)

---

## Empfehlung

### Phase 1: Grundimplementierung (80 Zeilen)
1. **Helper in `pcloud_bin_lib.py`:**
   - `verify_anchor_exists(cfg, anchor_path, cache=None)`
   - Einzelne stat_file-Calls mit Cache

2. **Integration in `pcloud_push_json_manifest_to_pcloud.py`:**
   - Check vor Resume-Entscheidung
   - Bei Fehler: Upload statt Resume (fail-safe)

3. **Dokumentation:**
   - README.md: PCLOUD_VERIFY_ANCHORS Env-Variable
   - Performance-Hinweis: ~2x länger

### Phase 2: Optimierung (optional, +30 Zeilen)
- Thread-Pool für parallele Verifikation
- Reduziert Overhead auf ~20%

### Phase 3: Intelligente Strategie (Zukunft, +50 Zeilen)
- Nur erste N Anchors verifizieren (Stichprobe)
- Bei Fehler: Full-Verify
- Heuristik: 99% Vertrauen bei <1% Stichprobe

---

## Alternative: Post-Upload Verification (EINFACHER)

**Idee:** Statt während Upload zu verifizieren, danach via `pcloud_quick_delta.py`

**Vorteile:**
- ✅ Bereits implementiert (existierendes Tool)
- ✅ Kein Performance-Impact auf Upload
- ✅ Modular: Unabhängig vom Upload-Prozess
- ✅ **Jetzt bereits im Wrapper integriert!**

**Nachteile:**
- ❌ Fehler erst nach Upload erkannt
- ❌ Bei Problemen: Repair + Re-Upload nötig

**Empfehlung:** 
- **Kurzfristig:** Post-Upload Delta-Check via `pcloud_quick_delta.py` (bereits umgesetzt!)
- **Langfristig:** PCLOUD_VERIFY_ANCHORS=1 für kritische Umgebungen (z.B. Compliance)

---

## Implementation Checklist

### Wenn wir PCLOUD_VERIFY_ANCHORS implementieren:

- [ ] `pcloud_bin_lib.py`: Funktion `verify_anchor_exists()` hinzufügen
- [ ] `pcloud_push_json_manifest_to_pcloud.py`: Integration im Upload-Loop
- [ ] Tests: Verhalten bei fehlenden Anchors (trotz Index-Eintrag)
- [ ] Dokumentation: README.md mit Performance-Warnung
- [ ] .env.example: `PCLOUD_VERIFY_ANCHORS=0` (default)
- [ ] Optional: Thread-Pool für Parallelisierung
- [ ] Monitoring: Metrik `MET_ANCHOR_VERIFY_FAILED` hinzufügen

**Zeitschätzung:** 2-3h Implementierung + 1h Tests + 1h Doku = **4-5h total**

---

## Fazit

**Aktueller Zustand (nach heutigen Änderungen):**
- ✅ Post-Upload Verification via `pcloud_quick_delta.py` im Wrapper
- ✅ 0 missing anchors erkannt → System funktioniert
- ✅ Kein Performance-Overhead
- ✅ Modulare Architektur

**PCLOUD_VERIFY_ANCHORS=1:**
- ⏳ Noch nicht notwendig (Post-Upload Check reicht)
- ⏳ Nur bei wiederholten Index-Divergenz-Problemen relevant
- ⏳ Performance-Impact rechtfertigt aktuell nicht den Aufwand

**Empfehlung:** 
- **Jetzt:** Bleiben bei Post-Upload Delta-Check ✅
- **Zukunft:** PCLOUD_VERIFY_ANCHORS=1 implementieren falls:
  - Produktion zeigt Index-Inkonsistenzen
  - Compliance erfordert Fail-Safe während Upload
  - Performance-Overhead akzeptabel (z.B. nur nächtliche Backups)
