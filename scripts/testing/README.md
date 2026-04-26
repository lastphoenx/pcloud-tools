# PoC: Persistent Chunked Resume - Test-Anleitung

## 🎯 Ziel

Testen der Chunk-Level-Resume-Funktionalität isoliert, bevor wir sie in den produktiven Code integrieren.

**Test-Fragen:**
1. ✅ Funktioniert chunked upload mit `upload_create` → `upload_write` → `upload_save`?
2. ⏱️ Wie lange bleibt eine `uploadid` gültig nach Abbruch?
3. 🔄 Kann man nach Stunden/Tagen mit derselben `uploadid` fortsetzen?
4. 📊 Welche Responses gibt die API zurück (für jeden Chunk)?

---

## 📁 Files & Locations

| Datei | Beschreibung |
|-------|--------------|
| `poc_chunked_resume.py` | Haupt-Test-Skript |
| `~/.pcloud_poc_state/*.state.json` | Persistent State (uploadid, offset, etc.) |
| `~/.pcloud_poc_state/*.responses.jsonl` | Alle API-Responses (JSONL-Format) |
| `/Backup/rtb_1to1/testing_purpose/` | Zielordner in pCloud |

---

## 🚀 Test-Szenarien

### **Szenario 1: Normaler Upload (ohne Abbruch)**

```bash
# Große Test-Datei erstellen (z.B. 50 MB)
dd if=/dev/urandom of=/tmp/testfile.bin bs=1M count=50

# Upload starten
python poc_chunked_resume.py --file /tmp/testfile.bin --mode normal
```

**Erwartet:**
- Alle Chunks erfolgreich hochgeladen
- State-File wird am Ende gelöscht
- Response-Log zeigt alle `result: 0`

---

### **Szenario 2: Abbruch + Resume (kurze Pause)**

```bash
# Upload starten, nach 3 Chunks abbrechen
python poc_chunked_resume.py --file /tmp/testfile.bin --mode abort-after --abort-after-chunks 3

# State prüfen
cat ~/.pcloud_poc_state/testfile_bin.state.json

# Resume nach wenigen Sekunden
python poc_chunked_resume.py --file /tmp/testfile.bin --mode resume
```

**Erwartet:**
- ✅ Resume funktioniert
- ✅ uploadid ist noch gültig
- ✅ Upload setzt bei Chunk 4 fort (nicht von vorne!)
- Output: `[RESUME] uploadid gültig → Upload wird fortgesetzt`

---

### **Szenario 3: Timeout-Test (uploadid-Gültigkeit nach Pause)**

```bash
# Upload mit 10-Minuten-Pause nach Chunk 1
python poc_chunked_resume.py --file /tmp/testfile.bin --mode timeout-test --timeout-minutes 10

# Oder: Manueller Test mit Ctrl+C
python poc_chunked_resume.py --file /tmp/testfile.bin --mode normal
# Nach Chunk 1: Drücke Ctrl+C
# Warte 10 Minuten, dann:
python poc_chunked_resume.py --file /tmp/testfile.bin --mode resume
```

**Test-Varianten:**
| Pause | Erwartung |
|-------|-----------|
| 1 Minute | ✅ uploadid gültig |
| 10 Minuten | ✅ / ❌ (zu testen!) |
| 60 Minuten | ❌ Wahrscheinlich abgelaufen |
| 24 Stunden | ❌ Definitiv abgelaufen |

**Bei abgelaufener uploadid:**
```
[RESUME] uploadid abgelaufen → FALLBACK: Neuer Upload
[INIT] Erstelle neue Upload-Session (upload_create)...
```

---

### **Szenario 4: Datei-Änderung während Abbruch**

```bash
# Upload abbrechen
python poc_chunked_resume.py --file /tmp/testfile.bin --mode abort-after --abort-after-chunks 2

# Datei ändern
echo "modified" >> /tmp/testfile.bin

# Resume versuchen
python poc_chunked_resume.py --file /tmp/testfile.bin --mode resume
```

**Erwartet:**
```
FEHLER: Datei wurde seit Abbruch geändert (Hash-Mismatch)!
```

---

### **Szenario 5: Sehr große Datei (echte Bedingungen)**

```bash
# 1 GB Test-Datei erstellen
dd if=/dev/urandom of=/tmp/bigfile.bin bs=1M count=1024

# Upload mit kleinen Chunks (schnelleres Testen)
export POC_CHUNK_SIZE=$((5 * 1024 * 1024))  # 5 MB Chunks

# Abbruch nach 100 MB
python poc_chunked_resume.py --file /tmp/bigfile.bin --mode abort-after --abort-after-chunks 20

# Resume nach Minuten/Stunden
sleep 600  # 10 Minuten warten
python poc_chunked_resume.py --file /tmp/bigfile.bin --mode resume
```

---

## 📊 Response-Analyse

Alle API-Responses werden in `~/.pcloud_poc_state/*.responses.jsonl` geloggt:

```bash
# Alle upload_write Responses anschauen
cat ~/.pcloud_poc_state/testfile_bin.responses.jsonl | jq 'select(.response.step == "upload_write")'

# Durchschnittliche Chunk-Upload-Zeit
cat ~/.pcloud_poc_state/testfile_bin.responses.jsonl | jq -r 'select(.response.step == "upload_write") | .response.duration_s' | awk '{sum+=$1; n++} END {print sum/n " Sekunden"}'

# Prüfen ob result immer 0 war
cat ~/.pcloud_poc_state/testfile_bin.responses.jsonl | jq '.response.response.result' | sort | uniq -c
```

---

## 🔬 Was wir herausfinden

### **1. uploadid-Timeout-Fenster**

| Pause | Gültig? | Notizen |
|-------|---------|---------|
| 1 Min | ✅ | (zu testen) |
| 5 Min | ? | (zu testen) |
| 10 Min | ? | (zu testen) |
| 30 Min | ? | (zu testen) |
| 60 Min | ? | (zu testen) |
| 24h | ❌ | (zu testen) |

**Protokollieren in:** `/Backup/rtb_1to1/testing_purpose/timeout_test_results.txt`

---

### **2. API-Response-Struktur**

Dokumentiere die exakte Response-Struktur für:
- `upload_create` → Was kommt zurück außer `uploadid`?
- `upload_write` → Gibt es Bestätigungen? Progress-Info?
- `upload_save` → Metadaten, Hash, FileID?

---

### **3. Fehler-Codes**

Welche `result`-Codes können auftreten?
- `0` = Erfolg
- `1900` = Upload not found (abgelaufene uploadid)
- `?` = Andere Fehler?

---

## 🧹 Cleanup

```bash
# State-Files anschauen
ls -lh ~/.pcloud_poc_state/

# Alte Tests löschen
rm -rf ~/.pcloud_poc_state/*

# Test-Dateien in pCloud löschen
# (manuell über Web-UI oder via pCloud-API)
```

---

## 📈 Nächste Schritte

Nach erfolgreichen Tests:

1. ✅ **Timeout-Fenster dokumentieren** → Wissen wir wie lange uploadid gültig bleibt
2. ✅ **Fallback-Strategie validieren** → Bei abgelaufener uploadid neu starten funktioniert
3. ✅ **Integration in `pcloud_bin_lib.py`** → Persistent State in produktiven Code
4. ✅ **ENV-Flags** → `PCLOUD_ENABLE_CHUNK_RESUME=1` für Opt-in

---

## 💡 Tipps

### **Schnelle Test-Iterationen**

```bash
# Kleine Chunks für schnellere Tests
export POC_CHUNK_SIZE=$((1 * 1024 * 1024))  # 1 MB

# Test-Datei wiederverwenden
export TESTFILE=/tmp/testfile.bin
dd if=/dev/urandom of=$TESTFILE bs=1M count=10

# Loop für mehrere Tests
for i in {1..5}; do
  echo "=== Test $i ==="
  python poc_chunked_resume.py --file $TESTFILE --mode abort-after --abort-after-chunks 2
  sleep 5
  python poc_chunked_resume.py --file $TESTFILE --mode resume
  rm ~/.pcloud_poc_state/*
done
```

### **Verbose Logging**

```bash
# Detaillierte pCloud-API-Logs
export PCLOUD_VERBOSE=1
python poc_chunked_resume.py --file /tmp/testfile.bin --mode normal
```

### **Verschiedene pCloud-Accounts testen**

```bash
# Profile nutzen (falls mehrere .env-Files)
python poc_chunked_resume.py --file /tmp/testfile.bin --env-file ~/pcloud_test.env --mode normal
```

---

## 🐛 Bekannte Limitierungen (PoC)

1. **Kein Multi-Threading** → Chunks werden sequentiell hochgeladen
2. **Kein Retry pro Chunk** → Bei Fehler bricht Skript ab (für Testing okay)
3. **State-Dir ist fix** → `~/.pcloud_poc_state/` (nicht konfigurierbar)

Diese sind bewusst einfach gehalten für klare Test-Ergebnisse!

---

## 📝 Test-Protokoll-Vorlage

```markdown
# Test-Protokoll: Chunked Resume PoC

**Datum:** 2026-04-26
**Tester:** [Name]
**Test-Datei:** /tmp/testfile.bin (50 MB)
**Chunk-Größe:** 2 MB (25 Chunks total)

## Szenario: Abbruch nach 3 Chunks + Resume nach 10 Min

### Upload-Phase 1 (Abbruch)
- ✅ Chunks 1-3 erfolgreich hochgeladen
- ✅ State gespeichert: uploadid=12345678, offset=6291456
- ✅ Abbruch wie erwartet

### Wartezeit
- ⏱️ 10 Minuten Pause

### Upload-Phase 2 (Resume)
- ✅ State geladen
- ✅ Hash-Validierung OK
- ❓ uploadid-Test: [GÜLTIG / ABGELAUFEN]
- ✅ Chunks 4-25 erfolgreich hochgeladen
- ✅ upload_save erfolgreich
- ✅ FileID: 1729212

### Erkenntnisse
- uploadid ist nach 10 Min noch gültig: [JA / NEIN]
- Fallback funktioniert: [JA / NEIN / N/A]
- Durchschnittliche Chunk-Speed: [X.X MB/s]

### Logs
- State-File: ~/.pcloud_poc_state/testfile_bin.state.json
- Response-Log: ~/.pcloud_poc_state/testfile_bin.responses.jsonl
```
