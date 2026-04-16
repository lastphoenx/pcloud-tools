# Gap-Handling Quick Start Guide

**Ziel:** Gap-Handling auf `pi-nas` produktiv einsetzen - in 5 Minuten.

---

## ⚡ Express-Setup (3 Schritte)

### 1️⃣ Feature-Branch aktivieren

```bash
cd /opt/apps/pcloud-tools/main
git fetch origin
git checkout feature/delta-copy-poc
git pull origin feature/delta-copy-poc
```

### 2️⃣ Konfiguration anpassen

```bash
# .env editieren
sudo nano /opt/apps/pcloud-tools/main/.env

# Diese Zeilen hinzufügen/ändern:
PCLOUD_USE_DELTA_COPY=1
PCLOUD_GAP_STRATEGY=optimistic
PCLOUD_ENABLE_JSONL=1
```

### 3️⃣ Ersten Backup-Run starten

```bash
# Test-Run (Conservative = Safe)
sudo PCLOUD_GAP_STRATEGY=conservative \
     bash /opt/apps/rtb/rtb_wrapper.sh

# Bei Erfolg: Wechsel zu Optimistic
sudo PCLOUD_GAP_STRATEGY=optimistic \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

**Fertig!** 🎉

---

## 🎯 Strategie-Wahl

**Für 99% der Use-Cases:**
```bash
PCLOUD_GAP_STRATEGY=optimistic  # ← Empfohlen!
```

**Nur verwenden wenn:**
- `conservative`: Erste Tests, maximale Sicherheit
- `aggressive`: Nach schweren Integritäts-Problemen

---

## 📊 Status prüfen

### Log-Check
```bash
# Aktuelle Logs
tail -50 /var/log/backup/pcloud_sync.log

# Gap-Events finden
grep "Gap detected" /var/log/backup/pcloud_sync.log | tail -10
```

### Dashboard (Web-UI)
```
http://pi-nas.local:5000/dashboard
→ Tab: "Gap History"
```

### DB-Check
```bash
mysql -u backup_pipeline -p -e "
  SELECT 
    run_id,
    gaps_synced,
    new_snapshots,
    rebuilt_snapshots,
    run_status
  FROM pcloud_run_history 
  ORDER BY run_start DESC 
  LIMIT 5"
```

---

## ⚠️ Troubleshooting

### Problem: "Gap detected in conservative mode"

**Quick-Fix:**
```bash
# Wechsel zu Optimistic (repariert automatisch)
sudo PCLOUD_GAP_STRATEGY=optimistic \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

### Problem: "MISSING_MANIFEST"

**Quick-Fix:**
```bash
# Aggressive Rebuild
sudo PCLOUD_GAP_STRATEGY=aggressive \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

### Problem: Rebuild dauert zu lange

**Analysis:**
```bash
# Check: Wie viele Snapshots werden rebuilt?
grep "rebuilt_snapshots" /var/log/backup/pcloud_sync.log | tail -5

# Falls > 5: Evtl. Scenario A (Broken Chain) → korrekt!
# Falls = 0: Scenario B → optimal!
```

---

## 📈 Performance-Erwartung

| Scenario | Snapshots | Upload-Zeit | Strategie |
|----------|-----------|-------------|-----------|
| **Neuer Snapshot** | 1 | ~2-7h | Alle gleich |
| **Gap (Intact)** | 1 | ~2-7h | Optimistic ⚡ |
| **Gap (Intact)** | 3 | ~18-21h | Aggressive 🐌 |
| **Gap (Broken)** | 3 | ~18-21h | Alle gleich |

**➡️ Optimistic = Beste Performance bei Scenario B!**

---

## 🔐 Sicherheits-Check

**Nach jedem Gap-Event:**
```bash
# 1. Delta-Report prüfen
ls -lh /srv/pcloud-archive/deltas/delta_verify_*.json | tail -5

# 2. Letzten Report analysieren
LAST_DELTA=$(ls -t /srv/pcloud-archive/deltas/*.json | head -1)
jq '.status' "$LAST_DELTA"
# Erwartet: "OK"

# 3. Missing Anchors?
jq '.missing_anchors | length' "$LAST_DELTA"
# Erwartet: 0
```

---

## 🎓 Weiterführende Docs

- **Vollständige Dokumentation:** [GAP_HANDLING.md](GAP_HANDLING.md)
- **Workflow-Diagramme:** [GAP_HANDLING_WORKFLOWS.md](GAP_HANDLING_WORKFLOWS.md)
- **FAQ:** [GAP_HANDLING_FAQ.md](GAP_HANDLING_FAQ.md)

---

**🚀 Ready to go!**
