# Gap-Handling Dokumentation - Index

**Feature:** Intelligentes Gap-Handling für pCloud-Backups  
**Version:** 1.0.0  
**Branch:** `feature/delta-copy-poc`  
**Status:** Production-Ready

---

## 📚 Dokumentations-Übersicht

| Dokument | Beschreibung | Zielgruppe | Lesezeit |
|----------|--------------|------------|----------|
| **[GAP_HANDLING.md](GAP_HANDLING.md)** | Vollständige technische Dokumentation | DevOps, Entwickler | 45 min |
| **[GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md)** | Express-Setup in 5 Minuten | Alle | 5 min |
| **[GAP_HANDLING_FAQ.md](GAP_HANDLING_FAQ.md)** | Häufig gestellte Fragen | Alle | 20 min |
| **[GAP_HANDLING_WORKFLOWS.md](GAP_HANDLING_WORKFLOWS.md)** | Visuelle Workflow-Diagramme | Architekten, DevOps | 15 min |

---

## 🚀 Schnelleinstieg

**Neu hier? Start here! →** [GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md)

**3-Schritte-Setup:**
1. Feature-Branch aktivieren
2. `.env` konfigurieren (`PCLOUD_GAP_STRATEGY=optimistic`)
3. Ersten Backup-Run durchführen

**Fertig in 5 Minuten!**

---

## 📖 Detaillierte Inhalte

### [GAP_HANDLING.md](GAP_HANDLING.md) - Hauptdokumentation

**Kapitel:**
- Executive Summary
- Problemstellung (Scenario A vs. B)
- Architektur & Design
- Gap-Strategien (Conservative, Optimistic, Aggressive)
- Implementierung (Funktionen, API)
- Testing & Validation
- Performance-Analyse
- Best Practices
- Troubleshooting
- Technische Referenz

**Umfang:** ~2500 Zeilen, vollständig, produktionsreif

---

### [GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md) - Express-Anleitung

**Inhalte:**
- ⚡ 3-Schritte-Setup
- 🎯 Strategie-Wahl
- 📊 Status prüfen
- ⚠️ Troubleshooting (Quick-Fixes)
- 📈 Performance-Erwartung

**Ziel:** Produktiv-Einsatz in unter 5 Minuten

---

### [GAP_HANDLING_FAQ.md](GAP_HANDLING_FAQ.md) - Häufige Fragen

**Kategorien:**
- Allgemeine Fragen (Was ist ein Gap? Warum problematisch?)
- Strategien (Welche wählen? Unterschiede?)
- Scenarios (A vs. B, Erkennung)
- Fehlerbehandlung (MISSING_MANIFEST, BROKEN_CHAIN)
- Performance (Wie viel schneller? Overhead?)
- Monitoring (Alerts, Logs, Metriken)
- Sicherheit (Datenverlust? Rollback?)
- Integration (rtb_wrapper, systemd)

**Format:** ❓ Frage → Antwort (konkret, actionable)

---

### [GAP_HANDLING_WORKFLOWS.md](GAP_HANDLING_WORKFLOWS.md) - Visuelle Diagramme

**Diagramme (Mermaid):**
1. **Hauptworkflow** (Gap-Detection & Handling)
2. **Integritäts-Validierung** (validate_snapshot_integrity)
3. **Remote-Snapshot-Löschung** (delete_remote_snapshot)
4. **Scenario-Vergleich** (A vs. B Side-by-Side)
5. **Strategie-Entscheidungsbaum**
6. **Performance-Timeline** (Gantt-Chart)
7. **Build & Push Sequence** (Sequenz-Diagramm)
8. **Gap-Erkennung** (Algorithmus)
9. **State-Machine**
10. **Metrics Update Flow**
11. **Testing-Workflows** (3 Tests)
12. **RTB-Integration**

**Total:** 12 interaktive Mermaid-Diagramme

---

## 🎯 Use-Case-Matrix

**Welche Dokumentation für welchen Use-Case?**

| Use-Case | Empfohlene Doku |
|----------|-----------------|
| **First-Time-Setup** | QUICKSTART.md → FAQ.md |
| **Produktiv-Deployment** | GAP_HANDLING.md (Kapitel: Best Practices) |
| **Troubleshooting** | FAQ.md → GAP_HANDLING.md (Kapitel: Troubleshooting) |
| **Architektur-Review** | GAP_HANDLING.md → WORKFLOWS.md |
| **Code-Review** | GAP_HANDLING.md (Kapitel: Implementierung) |
| **Performance-Optimierung** | GAP_HANDLING.md (Kapitel: Performance) |
| **Monitoring-Setup** | FAQ.md (Monitoring) → GAP_HANDLING.md (Metriken) |
| **Testing** | GAP_HANDLING.md (Kapitel: Testing) → WORKFLOWS.md (Testing) |

---

## 🔍 Wichtige Konzepte

### Scenario A: Broken Chain

**Problem:** Gap durch Upload-Fehler → Hardlink-Chain unterbrochen

**Lösung:** Rebuild (DELETE spätere Snapshots + Re-Upload)

**Details:** [GAP_HANDLING.md#scenario-a](GAP_HANDLING.md#szenario-a-broken-chain-upload-fehler)

---

### Scenario B: Intact Chain

**Problem:** Gap durch versehentliches Löschen → Chain intakt

**Lösung:** Nur Gap füllen (keine Re-Uploads)

**Performance:** 3x-10x schneller als Rebuild

**Details:** [GAP_HANDLING.md#scenario-b](GAP_HANDLING.md#szenario-b-intact-chain-versehentliches-löschen)

---

### Gap-Strategien

| Strategie | Modus | Use-Case |
|-----------|-------|----------|
| **Conservative** | Manual | PoC-Testing, First-Run |
| **Optimistic** ⭐ | Smart Auto-Repair | Produktiv (Empfohlen) |
| **Aggressive** | Force-Rebuild | Nach Disaster-Recovery |

**Details:** [GAP_HANDLING.md#gap-strategien](GAP_HANDLING.md#-gap-strategien)

---

## 🛠️ File-Struktur

```
pcloud-tools/
├── docs/
│   ├── GAP_HANDLING.md                  (Haupt-Doku, 2500 Zeilen)
│   ├── GAP_HANDLING_QUICKSTART.md       (Express-Setup, 200 Zeilen)
│   ├── GAP_HANDLING_FAQ.md              (FAQ, 800 Zeilen)
│   ├── GAP_HANDLING_WORKFLOWS.md        (Mermaid-Diagramme, 600 Zeilen)
│   └── GAP_HANDLING_INDEX.md            (Dieser Index)
│
├── wrapper_pcloud_sync_1to1.sh          (Implementation)
│   ├── validate_snapshot_integrity()    (Line 312-354)
│   ├── delete_remote_snapshot()         (Line 356-371)
│   └── Gap-Detection-Loop               (Line 565-695)
│
├── pcloud_quick_delta.py                (Integrity-Check)
├── pcloud_manifest_diff.py              (Delta-Copy)
└── pcloud_repair_index.py               (Index-Repair)
```

---

## 📊 Metriken & Monitoring

**MariaDB-Tabelle:** `pcloud_run_history`

**Neue Spalten:**
- `gaps_synced` - Anzahl gefüllter Gaps
- `new_snapshots` - Anzahl neuer Snapshots
- `rebuilt_snapshots` - Anzahl rebuilder Snapshots

**Query-Beispiele:** [GAP_HANDLING.md#monitoring](GAP_HANDLING.md#-metriken--monitoring)

---

## 🧪 Testing

**3 Test-Szenarien:**

1. **Conservative-Abort** (Sicherheit verifizieren)
2. **Optimistic Scenario B** (Performance-Gewinn)
3. **Optimistic Scenario A** (Chain-Reparatur)

**Details:** [GAP_HANDLING.md#testing](GAP_HANDLING.md#-testing--validation)

---

## 🔗 Verwandte Dokumentation

- [DELTA_COPY_ANALYSIS.md](../DELTA_COPY_ANALYSIS.md) - Delta-Copy-Technologie
- [INTEGRATION_PLAN_PCLOUD_VERIFY_ANCHORS.md](../INTEGRATION_PLAN_PCLOUD_VERIFY_ANCHORS.md) - Anchor-Verification
- [POC_README.md](../POC_README.md) - PoC-Dokumentation
- [README.md](../README.md) - Projekt-Übersicht

---

## 💡 Empfohlener Lernpfad

### Für Operators/Admins:
1. **QUICKSTART.md** (5 min) - Setup verstehen
2. **FAQ.md** (20 min) - Häufige Fragen
3. **GAP_HANDLING.md** - Kapitel: Best Practices, Troubleshooting
4. **WORKFLOWS.md** - Visuelles Verständnis

### Für Entwickler:
1. **GAP_HANDLING.md** - Vollständige Doku (45 min)
2. **WORKFLOWS.md** - Architektur-Diagramme
3. **wrapper_pcloud_sync_1to1.sh** - Source-Code
4. **FAQ.md** - Implementation-Details

### Für Architekten:
1. **GAP_HANDLING.md** - Architektur & Design
2. **WORKFLOWS.md** - Komplette Diagramme
3. **GAP_HANDLING.md** - Performance-Analyse
4. **FAQ.md** - Erweiterte Features

---

## 📝 Changelog

### v1.0.0 (2026-04-16) - Initial Release

**Dokumentation:**
- ✅ GAP_HANDLING.md (Haupt-Doku)
- ✅ GAP_HANDLING_QUICKSTART.md
- ✅ GAP_HANDLING_FAQ.md
- ✅ GAP_HANDLING_WORKFLOWS.md (12 Mermaid-Diagramme)
- ✅ GAP_HANDLING_INDEX.md

**Features:**
- ✅ Scenario A vs. B Detection
- ✅ 3 Gap-Strategien
- ✅ Integritäts-Validierung
- ✅ MariaDB-Metriken
- ✅ JSONL-Logging

**Commit:** `cf8af0f`  
**Branch:** `feature/delta-copy-poc`

---

## 🎓 Glossar (Quick-Reference)

- **Gap:** Fehlender Snapshot in Chain
- **Broken Chain:** ref_snapshot fehlt remote
- **Intact Chain:** Alle Referenzen valide
- **Scenario A:** Gap durch Upload-Fehler
- **Scenario B:** Gap durch Löschen
- **ref_snapshot:** Basisversion für inkrementelles Backup
- **Manifest:** JSON-Metadaten eines Snapshots
- **Stub:** Placeholder mit Verweis auf echte Datei

**Vollständiges Glossar:** [GAP_HANDLING.md#glossar](GAP_HANDLING.md#-glossar)

---

## 👥 Support & Contribution

**Bei Fragen:**
1. **FAQ.md** durchsuchen
2. Logs prüfen (`/var/log/backup/pcloud_sync.log`)
3. GitHub-Issue erstellen

**Contribution:**
- Feature-Requests: GitHub Issues
- Bug-Reports: Mit Logs + DB-Metriken
- Dokumentation: Pull-Requests willkommen

---

## 📍 Quick-Links

| Link | Beschreibung |
|------|--------------|
| [Setup](GAP_HANDLING_QUICKSTART.md#️-express-setup-3-schritte) | 3-Schritte-Installation |
| [Strategien](GAP_HANDLING.md#-gap-strategien) | Conservative vs. Optimistic vs. Aggressive |
| [Scenarios](GAP_HANDLING.md#-scenario-matrix) | A vs. B Comparison |
| [Testing](GAP_HANDLING.md#-testing--validation) | 3 Test-Workflows |
| [Troubleshooting](GAP_HANDLING_FAQ.md#fehlerbehandlung) | Fehler-Lösungen |
| [Performance](GAP_HANDLING.md#-performance-analyse) | 3x-10x Speedup |
| [Diagramme](GAP_HANDLING_WORKFLOWS.md) | 12 Mermaid-Grafiken |

---

**🎯 Start here:** [GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md) → Produktiv in 5 Minuten!

---

*Dokumentation erstellt: 2026-04-16*  
*Letzte Aktualisierung: 2026-04-16*  
*Version: 1.0.0*
