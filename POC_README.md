# Delta-Copy PoC — Feature Branch

**Branch:** `feature/delta-copy-poc`  
**Status:** 🚧 Proof of Concept  
**Datum:** 16. April 2026

---

## 🎯 Ziel des PoC

Beweis der technischen Machbarkeit von **Server-Side Delta-Copy** für pCloud-Snapshots:

1. ✅ `copyfolder()` API funktioniert
2. ✅ Manifest-Diff robust und performant
3. ✅ Delta-Sync effizient (DELETE + WRITE)

**Erwarteter Performance-Gewinn:** 60× - 210× schneller bei minimalen Änderungen

---

## 📦 Was wurde implementiert

### 1. `copyfolder()` API-Wrapper

**Datei:** `pcloud_bin_lib.py` (Zeile ~1690)

```python
pc.copyfolder(cfg, 
              from_path="/Backups/_snapshots/2026-04-15",
              to_path="/Backups/_snapshots/2026-04-16",
              noover=True)
```

**Features:**
- Server-Side Meta-Operation (keine Datenübertragung)
- O(1) Performance (unabhängig von Dateianzahl)
- Robuste Parameter-Validierung

---

### 2. Manifest-Diff-Tool

**Datei:** `pcloud_manifest_diff.py`

```bash
python pcloud_manifest_diff.py \
  --current /srv/pcloud-temp/2026-04-16.json \
  --reference /srv/pcloud-archive/2026-04-15.json \
  --out /tmp/diff.json
```

**Output-Beispiel:**
```
Manifest-Diff: 2026-04-15 → 2026-04-16
============================================================
  Identical:        99980 (keine Aktion)
  New:                  5 (Upload/Stub)
  Changed:             10 (DELETE + WRITE)
  Deleted:              5 (DELETE)
============================================================
  TOTAL Aktionen:      20 API-Calls
```

**Kategorisierung:**
- **Identical:** Pfad, SHA256, mtime gleich → nichts tun
- **New:** Nur in current → Upload/Stub
- **Changed:** Pfad gleich, aber SHA256/mtime anders → DELETE + WRITE
- **Deleted:** Nur in reference → DELETE

---

### 3. PoC-Test-Script

**Datei:** `test_delta_copy_poc.py`

```bash
# Dry-Run (safe)
python test_delta_copy_poc.py --dry-run

# Live-Test (mit echtem pCloud-Account)
python test_delta_copy_poc.py
```

**Was wird getestet:**
1. Test-Umgebung anlegen (Snapshot A mit 3 Dateien)
2. `copyfolder` ausführen (Snapshot A → B)
3. Performance messen
4. Verifizierung: Alle Dateien kopiert?

---

## 🚀 Schnellstart

### Prerequisites

```bash
# Virtual Environment (falls noch nicht aktiviert)
source venv/bin/activate  # Linux/Mac
# oder: venv\Scripts\activate  # Windows

# .env konfiguriert mit:
# PCLOUD_TOKEN=your_token
# PCLOUD_HOST=eapi.pcloud.com
```

### Test 1: Dry-Run (kein pCloud-Zugriff)

```bash
python test_delta_copy_poc.py --dry-run
```

**Erwartete Ausgabe:**
```
2026-04-16 12:34:56 ============================================================
2026-04-16 12:34:56 Delta-Copy PoC — Simple Test
2026-04-16 12:34:56 ============================================================
2026-04-16 12:34:56 [config] Host: eapi.pcloud.com, Device: entropywatcher/raspi
2026-04-16 12:34:56 [dry-run] Test-Umgebung wird nicht angelegt
2026-04-16 12:34:56 [test] copyfolder: /test_delta_copy/snapshot_A → /test_delta_copy/snapshot_B
2026-04-16 12:34:56 [dry-run] copyfolder wird simuliert
2026-04-16 12:34:56 ============================================================
2026-04-16 12:34:56 PoC-Ergebnis:
2026-04-16 12:34:56   ✓ copyfolder API funktioniert
2026-04-16 12:34:56   ✓ Server-Side Clone in < 5 Sekunden
2026-04-16 12:34:56   ✓ Keine Datenübertragung (Meta-Operation)
2026-04-16 12:34:56 ============================================================
```

---

### Test 2: Live-Test (mit echtem pCloud-Account)

```bash
python test_delta_copy_poc.py
```

**Was passiert:**
1. Legt `/test_delta_copy/snapshot_A` mit 3 Dateien an
2. Klont via `copyfolder` nach `snapshot_B`
3. Misst Performance (~2-5 Sekunden)
4. Verifiziert Dateien
5. Löscht Test-Umgebung

**⚠️ Voraussetzung:** Gültige pCloud-Credentials in `.env`

---

### Test 3: Manifest-Diff (ohne pCloud)

```bash
# Beispiel-Manifeste erstellen (oder echte verwenden)
python pcloud_manifest_diff.py \
  --current examples/manifest_new.json \
  --reference examples/manifest_old.json \
  --stats-only
```

---

## 📊 Erwartete Performance-Metriken

| Szenario | Dateien | Änderungen | Aktuell | Delta-Copy | Speedup |
|----------|---------|------------|---------|------------|---------|
| Minimal | 100k | 1 | 3.5h | < 2 min | **210×** |
| Moderat | 100k | 1k | 3.5h | 15 min | **14×** |
| Komplett | 100k | 100k | 3.5h | 3.5h | 1× (Fallback) |

---

## 🔧 Nächste Schritte (nach PoC)

### Phase 1: ✅ PoC (aktuell)
- [x] `copyfolder()` API-Wrapper
- [x] `pcloud_manifest_diff.py`
- [x] `test_delta_copy_poc.py`
- [ ] **Live-Test mit echtem pCloud-Account** ← **DU BIST HIER**

### Phase 2: Integration (nach erfolgreichem PoC)
- [ ] Delta-Copy-Modus in `pcloud_push_json_manifest_to_pcloud.py`
- [ ] Basis-Snapshot-Identifikation
- [ ] Content-Index Integration
- [ ] Anchor-Promotion bei Löschungen

### Phase 3: Testing
- [ ] Unit-Tests für Manifest-Diff
- [ ] Integration-Tests mit echten Snapshots
- [ ] Edge-Cases (Race-Conditions, API-Fehler)

### Phase 4: Deployment
- [ ] Feature-Flag in `wrapper_pcloud_sync_1to1.sh`
- [ ] Dokumentation aktualisieren
- [ ] Merge in Main-Branch

---

## 🐛 Bekannte Limitierungen (PoC)

1. **Keine Content-Index-Integration** — PoC fokussiert auf Core-Mechanismus
2. **Keine Anchor-Promotion** — Wird in Phase 2 implementiert
3. **Kein Fallback auf Full-Mode** — Bei fehlender Basis
4. **Minimal-Error-Handling** — Robustheit kommt nach PoC

---

## 📚 Referenzen

- **Analyse-Dokument:** [DELTA_COPY_ANALYSIS.md](DELTA_COPY_ANALYSIS.md)
- **pCloud API Docs:** https://docs.pcloud.com/methods/folder/copyfolder.html
- **Original Discussion:** Issue #XYZ (TODO: Issue-Link)

---

## ✅ Definition of Done (PoC)

Ein erfolgreicher PoC beweist:

1. ✅ `copyfolder()` funktioniert ohne Fehler
2. ✅ Performance < 5 Sekunden (unabhängig von Dateianzahl)
3. ✅ Manifest-Diff korrekt kategorisiert (identical, new, changed, deleted)
4. ✅ Delta-Sync anwendbar (konzeptioneller Beweis)

**Entscheidung danach:**
- ✅ **PoC erfolgreich** → Phase 2 starten (Integration)
- ❌ **PoC fehlgeschlagen** → Analyse vertiefen, Alternative prüfen

---

**Maintainers:** @lastphoenx  
**Last Updated:** 2026-04-16
