# Gap-Handling: Häufig gestellte Fragen (FAQ)

---

## Allgemeine Fragen

### ❓ Was ist ein "Gap"?

**Antwort:** Ein Gap (Lücke) ist ein fehlender Snapshot in der Backup-Chain.

**Beispiel:**
```
Lokal:  2026-04-13, 2026-04-14, 2026-04-15, 2026-04-16
Remote: 2026-04-13, 2026-04-14,           , 2026-04-16
                                    ↑
                                   Gap!
```

Der Snapshot `2026-04-15` existiert lokal, aber nicht auf pCloud.

---

### ❓ Warum ist ein Gap problematisch?

**Antwort:** Weil spätere Snapshots auf den fehlenden Snapshot referenzieren könnten.

**Technisch:**
```json
// 2026-04-16/manifest.json
{
  "ref_snapshot": "2026-04-15",  // ← Referenz auf fehlenden Snapshot!
  "items": [
    {
      "relpath": "file.txt",
      "hash": "abc123...",
      "stub_target": "/2026-04-15/_files/abc123..."  // ← Broken Link!
    }
  ]
}
```

**Resultat:** Restore schlägt fehl, weil referenzierte Dateien nicht gefunden werden.

---

### ❓ Wie entstehen Gaps?

**Hauptursachen:**

1. **Upload-Fehler** (Scenario A)
   - Netzwerk-Timeout während Upload
   - Quota überschritten
   - Script-Crash (Kill-Signal, OOM)
   - API-Fehler

2. **Manuelles Löschen** (Scenario B)
   - Cleanup via pCloud Web-UI
   - Versehentliches `rclone delete`
   - API-Fehler (Ghost-Deletion)

3. **Inkonsistente Sync-States**
   - Interrupted Runs
   - Concurrency-Probleme (2 Backups parallel)

---

## Strategien

### ❓ Welche Strategie soll ich wählen?

**Quick-Answer:**
```bash
PCLOUD_GAP_STRATEGY=optimistic  # ← 99% aller Fälle
```

**Entscheidungsbaum:**

```
Bin ich beim ersten Test?
├─ Ja  → conservative (safe start)
└─ Nein → Produktiv?
          ├─ Ja  → optimistic (beste Balance)
          └─ Nein → Nach Disaster?
                    └─ Ja → aggressive (paranoid rebuild)
```

---

### ❓ Was macht "Conservative"?

**Antwort:** Absolute Sicherheit - bei Gap sofortiger Abbruch.

**Workflow:**
```
Gap detected → ERROR → EXIT → Manuelle Intervention nötig
```

**Vorteil:** Keine automatischen Änderungen  
**Nachteil:** Hoher manueller Aufwand

**Use-Case:** PoC-Testing, erste Produktiv-Tests

---

### ❓ Was macht "Optimistic"? ⭐

**Antwort:** Intelligente Entscheidung basierend auf Integritäts-Check.

**Workflow:**
```
Gap detected
  → Validiere alle späteren Snapshots
    → Alle OK?
      ├─ Ja  → Scenario B (nur Gap füllen) ⚡
      └─ Nein → Scenario A (Rebuild)
```

**Vorteil:** Performance-Optimierung bei Scenario B (3x-10x schneller)  
**Nachteil:** Etwas komplexere Logik

**Use-Case:** Produktionssysteme (Standard)

---

### ❓ Was macht "Aggressive"?

**Antwort:** Maximum Paranoia - immer rebuilden.

**Workflow:**
```
Gap detected
  → Keine Validierung!
  → DELETE alle späteren Snapshots
  → UPLOAD Gap + alle späteren
```

**Vorteil:** Garantiert korrekte Chain (100% safe)  
**Nachteil:** Ineffizient bei Scenario B (unnötige Re-Uploads)

**Use-Case:** Nach schweren Integritäts-Problemen

---

## Scenarios

### ❓ Was ist "Scenario A" (Broken Chain)?

**Antwort:** Gap durch Upload-Fehler → Hardlink-Chain unterbrochen.

**Beispiel:**
```
1. Upload von 2026-04-15 startet
2. Manifest lokal gespeichert ✓
3. Upload zu pCloud fehlschlägt ✗ (Netzwerk-Timeout)
4. Nächster Lauf: 2026-04-16 wird uploaded
   → ref_snapshot=2026-04-15 (existiert nicht remote!)
   → Broken Chain!
```

**Lösung:** DELETE 2026-04-16 + späteren, UPLOAD Gap + Rebuild

---

### ❓ Was ist "Scenario B" (Intact Chain)?

**Antwort:** Gap durch versehentliches Löschen → Chain war intakt.

**Beispiel:**
```
1. 2026-04-15 wurde korrekt uploaded ✓
2. 2026-04-16 wurde basierend auf 2026-04-15 erstellt ✓
3. Später: 2026-04-15 versehentlich gelöscht (Web-UI) ✗
4. Gap entsteht, aber Chain war mal korrekt!
```

**Lösung:** Nur Gap re-uploaden → Chain repariert

**Performance-Gewinn:** 3x-10x schneller als Rebuild!

---

### ❓ Wie erkenne ich welches Scenario vorliegt?

**Antwort:** Optimistic-Strategie macht das automatisch!

**Manuelle Prüfung:**
```bash
# 1. Manifest lokal vorhanden?
ls /srv/pcloud-archive/manifests/2026-04-15.json

# 2. Referenz-Snapshot auslesen
REF=$(jq -r '.ref_snapshot' /srv/pcloud-archive/manifests/2026-04-15.json)
echo "Referenz: $REF"

# 3. Referenz remote vorhanden?
# ... (siehe validate_snapshot_integrity Funktion)

# 4. Spätere Snapshots validieren
validate_snapshot_integrity "2026-04-16"
→ OK = Scenario B
→ BROKEN_CHAIN = Scenario A
```

---

## Fehlerbehandlung

### ❓ Fehler: "Gap detected in conservative mode"

**Ursache:** Conservative-Strategie aktiv, Gap erkannt.

**Lösungen:**

**Option 1: Wechsel zu Optimistic (empfohlen)**
```bash
sudo PCLOUD_GAP_STRATEGY=optimistic \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

**Option 2: Manuelle Reparatur**
```bash
# Nur Gap füllen (wenn sicher dass Scenario B)
build_and_push /mnt/backup/rtb_nas/2026-04-15
```

**Option 3: Aggressive Rebuild**
```bash
sudo PCLOUD_GAP_STRATEGY=aggressive \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

---

### ❓ Fehler: "MISSING_MANIFEST"

**Ursache:** Lokales Manifest fehlt oder korrupt.

**Diagnose:**
```bash
ls /srv/pcloud-archive/manifests/2026-04-15.json
# Datei fehlt?

# Falls vorhanden: JSON-Syntax prüfen
jq '.' /srv/pcloud-archive/manifests/2026-04-15.json
# Parse-Error?
```

**Lösungen:**

**Option 1: Manifest wiederherstellen**
```bash
# Falls Backup existiert
cp /srv/pcloud-archive/manifests.backup/2026-04-15.json \
   /srv/pcloud-archive/manifests/
```

**Option 2: Neues Manifest generieren**
```bash
cd /opt/apps/pcloud-tools/main
python pcloud_json_manifest.py \
  --src /mnt/backup/rtb_nas/2026-04-15 \
  --out /srv/pcloud-archive/manifests/2026-04-15.json \
  --ref-manifest /srv/pcloud-archive/manifests/2026-04-14.json
```

**Option 3: Aggressive Rebuild**
```bash
# Löscht alles ab Gap und rebuilt
sudo PCLOUD_GAP_STRATEGY=aggressive \
     bash /opt/apps/rtb/rtb_wrapper.sh
```

---

### ❓ Fehler: "BROKEN_CHAIN"

**Ursache:** Referenz-Snapshot fehlt remote.

**Diagnose:**
```bash
# Welche Referenz fehlt?
REF=$(jq -r '.ref_snapshot' /srv/pcloud-archive/manifests/2026-04-15.json)
echo "Referenz: $REF"

# Remote-Check
remote_snapshot_exists "$REF"
# Output: NO → Referenz fehlt!
```

**Lösung:**

**Optimistic-Strategie repariert automatisch:**
```bash
sudo PCLOUD_GAP_STRATEGY=optimistic \
     bash /opt/apps/rtb/rtb_wrapper.sh

# Workflow:
# 1. Erkennt BROKEN_CHAIN
# 2. DELETE 2026-04-15 + spätere
# 3. UPLOAD Gap (2026-04-14) falls auch fehlt
# 4. UPLOAD 2026-04-15 (rebuild)
# 5. UPLOAD spätere (rebuild)
```

---

### ❓ Fehler: Gap-Repair dauert ewig

**Ursache:** Viele spätere Snapshots → Rebuild nötig (Scenario A).

**Analyse:**
```bash
# Wie viele Snapshots werden rebuilt?
grep "rebuilt_snapshots" /var/log/backup/pcloud_sync.log | tail -1

# Beispiel-Output:
# rebuilt_snapshots = 10  ← 10 Snapshots re-uploaden!
```

**Ist das korrekt?**
```bash
# Prüfe ob wirklich Scenario A (Broken Chain)
for s in 2026-04-16 2026-04-17 2026-04-18; do
  echo "=== $s ==="
  validate_snapshot_integrity "$s"
done

# Falls ALLE "OK" → Scenario B → Bug!
# Falls mind. 1 "BROKEN_CHAIN" → Scenario A → korrekt (Rebuild nötig)
```

**False-Positive? (Bug)**
```bash
# Falls Scenario B aber trotzdem rebuildet → Debug
_log ERROR "Bug detected: Scenario B incorrectly handled as A!"

# Workaround: Manuell nur Gap füllen
build_and_push /mnt/backup/rtb_nas/2026-04-15
```

---

## Performance

### ❓ Wie viel schneller ist Optimistic bei Scenario B?

**Antwort:** **3x-10x schneller** (abhängig von Snapshot-Größe).

**Beispiel-Rechnung:**
- Gap: 150 GB (7h Upload)
- Later: 2x 150 GB (je 7h = 14h)

**Aggressive/Conservative:**
```
Upload: 150 GB + 150 GB + 150 GB = 21 Stunden
```

**Optimistic (Scenario B):**
```
Validation: 10 Sekunden
Upload: 150 GB = 7 Stunden
Total: 7 Stunden (3x faster!)
```

---

### ❓ Gibt es Performance-Overhead bei Validierung?

**Antwort:** Minimal (10-30 Sekunden für 10 Snapshots).

**Details:**
- **Pro Snapshot:** 1 API-Call (`listfolder`) + 1 `jq`-Aufruf
- **Caching:** `remote_snapshot_names()` wird nur 1x aufgerufen
- **Parallelisierung:** Aktuell sequential, könnte optimiert werden

**Trade-Off:** 30 Sekunden Validierung vs. 14 Stunden falsche Entscheidung → **lohnt sich immer!**

---

### ❓ Kann ich Validierung überspringen?

**Antwort:** Ja, mit Aggressive-Strategie.

**Aber:**
- ❌ Keine Scenario-Unterscheidung
- ❌ Immer Rebuild (auch wenn unnötig)
- ✅ Nur sinnvoll nach Disaster Recovery

**Empfehlung:** Nutze Optimistic (Validierung ist minimal).

---

## Monitoring

### ❓ Wie überwache ich Gap-Events?

**Methode 1: JSONL-Logs**
```bash
# Alle Gap-Events finden
jq -r 'select(.message | contains("Gap detected"))' \
  /var/log/backup/pcloud_sync.jsonl

# Nur Scenario A (Rebuild)
jq -r 'select(.message | contains("rebuilt chain"))' \
  /var/log/backup/pcloud_sync.jsonl
```

**Methode 2: MariaDB**
```sql
-- Alle Runs mit Gaps
SELECT 
  run_id,
  run_start,
  gaps_synced,
  rebuilt_snapshots,
  run_status
FROM pcloud_run_history
WHERE gaps_synced > 0
ORDER BY run_start DESC;

-- Performance-Check
SELECT 
  AVG(rebuilt_snapshots) as avg_rebuilds,
  SUM(gaps_synced) as total_gaps
FROM pcloud_run_history
WHERE run_start > NOW() - INTERVAL 30 DAY;
```

**Methode 3: Web-Dashboard**
```
http://pi-nas.local:5000/dashboard
→ Tab: "Gap History"
→ Filter: "Last 30 days"
```

---

### ❓ Wie setze ich Alerts für Gaps?

**Methode 1: Log-Monitor**
```bash
# /etc/logwatch/scripts/services/pcloud-gaps
#!/bin/bash
tail -100 /var/log/backup/pcloud_sync.log | \
  grep "Gap detected" | \
  mail -s "pCloud Gap Alert" admin@example.com
```

**Methode 2: DB-Trigger**
```sql
CREATE TRIGGER gap_alert
AFTER UPDATE ON pcloud_run_history
FOR EACH ROW
BEGIN
  IF NEW.gaps_synced > 0 THEN
    -- Send notification (via stored procedure)
    CALL send_notification('Gap detected', NEW.run_id);
  END IF;
END;
```

**Methode 3: Prometheus/Grafana**
```yaml
# prometheus.yml
- job_name: 'pcloud-metrics'
  static_configs:
    - targets: ['pi-nas.local:9104']
  
# Alert-Rule
- alert: PCloudGapDetected
  expr: pcloud_gaps_synced > 0
  for: 5m
  annotations:
    summary: "Gap in pCloud backup chain"
```

---

## Sicherheit

### ❓ Kann ich Daten verlieren durch Gap-Handling?

**Antwort:** **Nein** (bei korrekter Konfiguration).

**Schutz-Mechanismen:**

1. **Read-Only Validierung**
   - `validate_snapshot_integrity()` ändert nichts
   
2. **Explizite Deletion**
   - Nur bei confirmed BROKEN_CHAIN
   - Nur spätere Snapshots (nie frühere)

3. **Lokale Manifeste bleiben**
   - Remote-Deletion löscht nicht lokal
   - Jederzeit re-uploadbar

4. **All-or-Nothing**
   - Bei Upload-Fehler → Rollback (exit 1)
   - Keine partiellen States

**Worst-Case:**
```
Aggressive-Strategie + Scenario B
→ Unnötige Re-Uploads (langsam)
→ ABER: Kein Datenverlust!
```

---

### ❓ Was passiert wenn Validierung fehlschlägt?

**Antwort:** Safe-Fallback zu Scenario A (Rebuild).

**Pseudo-Code:**
```bash
for later in "${later_snaps[@]}"; do
  status=$(validate_snapshot_integrity "$later")
  
  if [[ "$status" != "OK" ]]; then
    # Conservative-Bias: Lieber rebuilden als Risiko
    needs_rebuild=1
    break
  fi
done
```

**Resultat:**
- Validierung fehlgeschlagen → Annahme: Scenario A
- Rebuild wird durchgeführt
- **Sicher** (aber vlt. langsamer als nötig)

---

### ❓ Kann ich Rollback machen?

**Antwort:** Ja, lokale Snapshots bleiben unberührt.

**Rollback-Strategie:**
```bash
# Falls Gap-Repair fehlschlägt:
1. Lokale Snapshots sind intakt (/mnt/backup/rtb_nas)
2. Manifeste archiviert (/srv/pcloud-archive/manifests)
3. Einfach nochmal laufen lassen:
   bash /opt/apps/rtb/rtb_wrapper.sh
```

**Manuelle Intervention:**
```bash
# Falls wirklich nötig: Manuell uploaden
for s in 2026-04-15 2026-04-16 2026-04-17; do
  build_and_push /mnt/backup/rtb_nas/$s
done
```

---

## Erweiterte Features

### ❓ Was ist "Deep Validation"?

**Antwort:** Erweiterte Integritäts-Prüfung via `pcloud_quick_delta`.

**Standard-Validierung:**
```bash
1. Manifest existiert lokal?
2. ref_snapshot korrekt?
3. Referenz remote vorhanden?
```

**Deep-Validierung:**
```bash
1-3. (wie oben)
4. pcloud_quick_delta → Vergleich Index vs. LIVE
   → Missing Anchors?
   → Hash-Mismatches?
   → Unknown Files?
```

**Aktivierung:**
```bash
export PCLOUD_DEEP_GAP_VALIDATION=1
bash /opt/apps/rtb/rtb_wrapper.sh
```

**Nachteil:** Langsamer (API-Calls für jede Datei)

---

### ❓ Kann ich Gap-Handling deaktivieren?

**Antwort:** Nicht direkt, aber mit Conservative + Manual-Intervention.

**Workflow:**
```bash
export PCLOUD_GAP_STRATEGY=conservative

# Bei Gap → Abbruch
# Dann: Manuelle Prüfung
# Dann: Manuelle Upload-Entscheidung
```

**Warum deaktivieren?**
- Legacy-Kompatibilität
- Sehr altes Backup-Set
- Spezielle Compliance-Anforderungen

**Empfehlung:** Nutze conservative statt Deaktivierung.

---

## Integration

### ❓ Funktioniert Gap-Handling mit rtb_wrapper?

**Antwort:** Ja, vollständig integriert!

**Workflow:**
```bash
rtb_wrapper.sh
  ├─ rsync_tmbackup.sh (Snapshot erstellen)
  └─ wrapper_pcloud_sync_1to1.sh
       └─ Gap-Detection + Handling
```

**Keine Änderungen an `rtb_wrapper.sh` nötig!**

---

### ❓ Funktioniert Gap-Handling mit Systemd-Timern?

**Antwort:** Ja, setze Env-Vars in Service-File.

**Beispiel:**
```ini
# /etc/systemd/system/backup-pipeline.service
[Service]
ExecStart=/opt/apps/rtb/rtb_wrapper.sh
Environment="PCLOUD_GAP_STRATEGY=optimistic"
Environment="PCLOUD_USE_DELTA_COPY=1"
```

**Reload + Restart:**
```bash
sudo systemctl daemon-reload
sudo systemctl restart backup-pipeline.service
```

---

## Versioning

### ❓ Ab welcher Version ist Gap-Handling verfügbar?

**Antwort:** Ab v1.0.0 (Feature-Branch `feature/delta-copy-poc`).

**Commit:** `cf8af0f`

**Verfügbarkeit:**
- ✅ Branch: `feature/delta-copy-poc`
- ⏳ Branch: `main` (nach Testing)

---

### ❓ Ist Gap-Handling abwärtskompatibel?

**Antwort:** Ja, vollständig!

**Kompatibilität:**
- ✅ Alte Manifeste (ohne `ref_snapshot`) → OK
- ✅ Alte Backups → Kann nahtlos weiterlaufen
- ✅ Alte Scripts → Neue Features optional (Env-Vars)

**Migration:** Keine Migration nötig!

---

**📚 Weitere Hilfe:**
- [GAP_HANDLING.md](GAP_HANDLING.md) - Vollständige Doku
- [GAP_HANDLING_QUICKSTART.md](GAP_HANDLING_QUICKSTART.md) - Quick-Start
- [GAP_HANDLING_WORKFLOWS.md](GAP_HANDLING_WORKFLOWS.md) - Workflow-Diagramme

---

*Letzte Aktualisierung: 2026-04-16*
