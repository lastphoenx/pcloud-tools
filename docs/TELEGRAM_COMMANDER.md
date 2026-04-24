# Telegram Commander - Erweiterte Dokumentation

**Version:** 3.0 (Dashboard Parity - Akkurate Daten & Kompaktes Layout)  
**Letzte Aktualisierung:** 21. April 2026

---

## 📱 Überblick

Der Telegram Commander ist ein vollwertiges **Remote-Kontrollzentrum** für deine Backup-Pipeline. Nach dem Upgrade kannst du:

✅ **Backups starten** (mit Safety-Gate-Prüfung)  
✅ **Status abfragen** (RTB, pCloud, Services) → **NEU: Dashboard-akkurat!**  
✅ **Logs live ansehen** (ohne SSH-Login)  
✅ **Safety-Gate zurücksetzen** (mit Bestätigung)  
✅ **Interaktive Buttons nutzen** (kein Tippen nötig)  
✅ **Malware-Monitoring** (echte DB-Daten, nicht Cache) → **NEU in v3.0**

---

## 🆕 Was ist neu in v3.0? (April 2026)

**🎯 Hauptziel:** Bot zeigt jetzt **exakt die gleichen Daten wie das Dashboard**!

### 1. 🐛 MAJOR BUG FIX: Malware-Daten korrekt

**Vorher (v2.0):**
```
🧬 Malware & Integrität
✅ Aktiv: 0  🔴 Flagged: 0
⚠️ Missing: 0  ❌ Fehler: 0
```
❌ **FALSCH** - zeigte immer 0/0/0/0!

**Jetzt (v3.0):**
```
🚨 Malware & Integrität
━━━━━━━━━━━━━━━━━
✅ Aktiv: 5  🔴 Flagged: 1
⚠️ Missing: 8  ❌ Fehler: 0

Details pro Service:
  🔴 entropywatcher-nas: 1 flagged
  ⚠️ entropywatcher-os: 8 missing
```
✅ **KORREKT** - liest jetzt aus `reports.json` (echte DB-Daten)!

---

### 2. 📊 Dashboard-Style Status

**Kompaktes Layout** statt verbose Listen:

```
✅ OK
pi-nas
━━━━━━━━━━━━━━━━━

🔄 Backup-Status
RTB ✅  pCloud ✅  Safety 🟢 GREEN (live)

Letzter Lauf: 19.04.2026, 20:00
RTB Snapshots: 267
Nächstes Backup: ✓ Keine Änderungen (21:18)
Sync-Stand: 2026-04-18-004404

📊 Performance (30d)
Erfolgsrate: 100%  |  Ø Dauer: 57 min
Gesamt hochgeladen: 268.83 GB
```

**Neu hinzugekommen:**
- 🟢/🟡/🔴 **Emoji-Ampeln** für schnellen Überblick
- 📝 **Nächstes Backup** Forecast (aus dry_run_result)
- ☁️ **pCloud Sync-Stand** mit latest_snapshot
- 📊 **Performance Stats** (30d): Erfolgsrate, Durchschnittsdauer, GB uploaded
- 🔙 **Zurück-Button** überall vorhanden

---

### 3. 🗂️ Data Source Upgrade

**v2.0 las nur:** `status.json`  
**v3.0 liest:** `status.json` **+** `reports.json`

**Warum wichtig?**
- `status.json` = Live-Services-Status (kann falsche Malware-Zahlen haben)
- `reports.json` = Echte DB-Daten (EntropyWatcher Database Queries)

**Neue Daten verfügbar:**
- `reports.entropywatcher.flagged_files` → Welcher Service hat wie viele flagged files
- `reports.entropywatcher.last_scans` → Missing files Count
- `reports.performance_stats` → 30-Tage Statistiken
- `reports.recent_backups` → Letzte Backup-Läufe mit Details

---

### 4. 🔍 LIVE Safety-Gate

**v2.0:** Zeigte cached Safety-Gate (bis zu 15min alt)  
**v3.0:** Zeigt LIVE Safety-Gate wenn verfügbar

```
Safety-Gate (live): 🟢 GREEN
```

**Label erklärt:**
- `(live)` = Gerade eben geprüft (< 1 min alt)
- `(hist.)` = Historischer Wert (aus letztem Backup)
- Kein Label = Unbekannt

---

### 5. 🧬 Malware-Details Rewrite

**Vorher:**
```
🧬 Malware & Integrität
Status: OK - Alle Monitore laufen sauber
└ Aktive Monitore: 0   <-- FALSCH!
└ 🔴 Flagged: 0        <-- FALSCH!
└ ⚠️ Missing: 0        <-- FALSCH!
```

**Jetzt:**
```
🚨 Malware & Integrität
━━━━━━━━━━━━━━━━━
CRITICAL - Verdächtige Dateien gefunden!

Zusammenfassung:
✅ Aktiv: 5  🔴 Flagged: 1
⚠️ Missing: 8  ❌ Fehler: 0

Details pro Service:
  🔴 entropywatcher-nas: 1 flagged
  ⚠️ entropywatcher-os: 8 missing

Monitor-Status:
🟢 entropywatcher-nas
    nächster: 2026-04-19T21:22
🟢 entropywatcher-os
    nächster: 2026-04-20T03:44
```

**Zeigt jetzt:**
- Echte flagged/missing Counts
- Service-Breakdown (welcher Monitor hat Probleme)
- Next-Run Zeiten pro Monitor

---

### 6. 🎨 Konsistente Navigation

**Alle Commands zeigen jetzt:**
- 🔙 **Hauptmenü** Button (kein Zurück-Suchen mehr)
- 🌐 **Dashboard öffnen** Button (wenn dashboard_url verfügbar)

**Betrifft:**
- `/status`
- `/logs`
- `/malware` (NEU!)
- Safety-Gate Menüs
- Forecast

---

## 🆕 Was ist neu in v2.0?

### 1. 🎯 Inline Keyboard (Buttons)
Statt `/status` zu tippen, kannst du jetzt einfach auf **📊 Status** klicken.

**Haupt-Menü:**
```
┌─────────────────────┐
│  📊 Status          │
│  🔄 Backup starten  │
│  📜 Logs anzeigen   │
│  🔓 Safety-Gate     │
│  ❓ Hilfe           │
└─────────────────────┘
```

**Starten mit:** `/menu` oder `/start`

---

### 2. 📜 Log-Abfrage (ohne SSH)

Du kannst jetzt Logs direkt im Chat abrufen:

**Verfügbare Logs:**
- `backup-pipeline.service` — Haupt-Backup-Pipeline
- `rtb_wrapper.service` — RTB Wrapper (Änderungs-Erkennung)
- `pcloud-backup.service` — pCloud-Upload
- `telegram-commander.service` — Dieser Bot selbst

**Verwendung:**
1. Klick auf **📜 Logs anzeigen**
2. Wähle einen Service
3. Die letzten 30 Zeilen werden angezeigt

**Oder per Befehl:**
```
/logs
```

---

### 3. 🔓 Safety-Gate Reset (mit Bestätigung)

Wenn das Safety-Gate auf **RED** steht, kannst du es jetzt remote zurücksetzen:

**Ablauf:**
1. Klick auf **🔓 Safety-Gate** → **Reset zu GREEN**
2. Bot fragt: "Bist du sicher?"
3. Bestätige mit **✅ Ja, zurücksetzen**
4. Safety-Gate wird auf **GREEN** gesetzt

**⚠️ Warnung:** Nur nach manueller Prüfung durchführen!

**Oder per Befehl:**
```
/reset_safety_gate
```

---

## 📋 Alle Befehle

| Befehl | Beschreibung |
|--------|--------------|
| `/start` | Haupt-Menü mit Buttons |
| `/menu` | Haupt-Menü anzeigen |
| `/status` | System-Status (RTB, pCloud, Services) |
| `/backup` | Backup manuell starten (Safety-Gate-Check) |
| `/logs` | Service-Logs abrufen (30 Zeilen) |
| `/reset_safety_gate` | Safety-Gate auf GREEN setzen |
| `/help` | Befehls-übersicht |

---

## 🔧 Installation & Setup

### 1. Konfiguration

```bash
# Config kopieren (falls noch nicht vorhanden)
sudo cp /opt/apps/pcloud-tools/main/scripts/telegram_commander.conf.example \
       /etc/pcloud-tools/telegram_commander.conf

# Config bearbeiten
sudo nano /etc/pcloud-tools/telegram_commander.conf
```

**Mindest-Konfiguration:**
```bash
# Bot-Token von @BotFather
BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"

# Deine Chat-ID (mehrere mit Komma getrennt)
ALLOWED_CHAT_IDS="987654321"
```

**Chat-ID ermitteln:**
1. Sende `/start` an deinen Bot
2. Öffne: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Suche nach `"chat":{"id":987654321`

---

### 2. Berechtigungen (für Log-Abfrage & Safety-Gate Reset)

Der Bot muss als **root** laufen, um:
- `journalctl` aufzurufen (Logs lesen)
- `/opt/apps/monitoring/status.json` zu schreiben (Safety-Gate)

**Service-File prüfen:**
```bash
sudo nano /etc/systemd/system/telegram-commander.service
```

**Muss enthalten:**
```ini
[Service]
User=root
Group=root
```

**Falls nicht vorhanden:**
```bash
# Service-Datei kopieren
sudo cp /opt/apps/pcloud-tools/main/systemd/telegram-commander.service.example \
       /etc/systemd/system/telegram-commander.service

# Service aktivieren
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-commander.service
```

---

### 3. Service starten

```bash
# Service aktivieren & starten
sudo systemctl enable --now telegram-commander.service

# Status prüfen
sudo systemctl status telegram-commander.service

# Logs live verfolgen
sudo journalctl -u telegram-commander.service -f
```

**Erwartete Ausgabe:**
```
INFO Connected as @YourBotName
INFO Starting poll loop (offset=0)
```

---

## 🎮 Verwendung

### Szenario 1: Status prüfen
1. Sende `/menu` an den Bot
2. Klick auf **📊 Status**
3. Du siehst:
   - Overall-Status (✅ OK / ⚠️ WARNING / 🚨 CRITICAL)
   - RTB-Status & Safety-Gate
   - pCloud-Status
   - Fehlgeschlagene Services

---

### Szenario 2: Backup manuell starten
1. Klick auf **🔄 Backup starten**
2. Bot prüft Safety-Gate:
   - **RED**: ❌ Backup verweigert
   - **YELLOW**: ⚠️ Warnung, aber Start
   - **GREEN**: ✅ Backup gestartet
3. Nach 5 Minuten: `/status` für Ergebnis

---

### Szenario 3: Fehler diagnostizieren
**Problem:** Status zeigt `backup-pipeline: failed`

**Lösung:**
1. Klick auf **📜 Logs anzeigen**
2. Wähle **backup-pipeline**
3. Lies die letzten 30 Zeilen
4. Fehlerursache erkennbar!

**Ohne SSH-Login! 🎉**

---

### Szenario 4: Safety-Gate zurücksetzen
**Problem:** Safety-Gate steht auf RED nach Fehlalarm

**Lösung:**
1. Prüfe manuell (SSH): Kein echte Ransomware
2. Im Bot: **🔓 Safety-Gate** → **Reset zu GREEN**
3. Bestätige mit **✅ Ja, zurücksetzen**
4. Safety-Gate ist jetzt GREEN
5. Nächstes Backup läuft wieder

---

## 🔒 Sicherheit

### Whitelist-Prinzip
- Nur `ALLOWED_CHAT_IDS` werden akzeptiert
- Alle anderen werden **silent rejected** (Logs zeigen Warnung)
- Kein Open-Port (nur ausgehende Verbindungen)

### Bot-Token-Schutz
```bash
# Config-Datei nur für root lesbar
sudo chmod 600 /etc/pcloud-tools/telegram_commander.conf
sudo chown root:root /etc/pcloud-tools/telegram_commander.conf

# Verifizieren
ls -la /etc/pcloud-tools/telegram_commander.conf
# Zeigt: -rw------- 1 root root
```

### Mehrere Nutzer
```bash
# Mehrere Chat-IDs (Komma-getrennt)
ALLOWED_CHAT_IDS="123456789,987654321,555444333"
```

---

## 📊 Monitoring

### Service-Status prüfen
```bash
# Ist der Bot online?
sudo systemctl status telegram-commander.service

# Letzte Logs
sudo journalctl -u telegram-commander.service -n 50

# Live-Logs
sudo journalctl -u telegram-commander.service -f
```

### Bot-Verbindung testen
```bash
# API-Test (ersetze <TOKEN>)
curl -s "https://api.telegram.org/bot<TOKEN>/getMe" | jq .
```

**Erwartete Antwort:**
```json
{
  "ok": true,
  "result": {
    "id": 123456789,
    "is_bot": true,
    "username": "YourBotName"
  }
}
```

---

## 🐛 Troubleshooting

### Problem: Bot antwortet nicht

**Diagnose:**
```bash
# Service läuft?
sudo systemctl status telegram-commander.service

# Fehler in Logs?
sudo journalctl -u telegram-commander.service -n 50 --no-pager
```

**Mögliche Ursachen:**
1. **BOT_TOKEN falsch** → Logs zeigen: `API call getMe failed`
2. **ALLOWED_CHAT_IDS falsch** → Logs zeigen: `Rejected message from chat_id=...`
3. **Kein Internet** → Logs zeigen: `Connection error`
4. **ConditionPathExists schlägt fehl** → Service startet nie (siehe unten)

---

### Problem: Service startet nicht (ConditionPathExists)

**Symptom:** Service bleibt `inactive (dead)`, keine Logs

**Diagnose:**
```bash
sudo systemctl status telegram-commander.service
# Zeigt: Condition: start condition failed at...
#        ConditionPathExists=/etc/pcloud-tools/telegram_commander.conf was not met
```

**Ursache:** Alte systemd-Unit nutzte `ConditionPathExists` für Config-Check. Wenn Config nicht existiert → Service startet nie!

**Lösung:**

**Option 1: Config erstellen** (empfohlen)
```bash
# Config kopieren
sudo cp /opt/apps/pcloud-tools/main/scripts/telegram_commander.conf.example \
       /etc/pcloud-tools/telegram_commander.conf

# Token eintragen
sudo nano /etc/pcloud-tools/telegram_commander.conf

# Service starten
sudo systemctl start telegram-commander.service
```

**Option 2: Service-File aktualisieren** (wenn Config nicht benötigt)
```bash
# Neue Unit (ohne ConditionPathExists) holen
cd /opt/apps/pcloud-tools/main
git pull

# Neue Unit kopieren
sudo cp systemd/telegram-commander.service.example \
       /etc/systemd/system/telegram-commander.service

# Reload & Start
sudo systemctl daemon-reload
sudo systemctl start telegram-commander.service
```

**Warum wurde das geändert?**  
Seit v2.1 liest der Bot automatisch aus `apprise.yml` als Fallback. Config-File ist optional!

---

### Problem: Logs-Befehl funktioniert nicht

**Symptom:** Klick auf **📜 Logs** → "Keine Berechtigung"

**Lösung:**
```bash
# Service muss als root laufen
sudo nano /etc/systemd/system/telegram-commander.service

# Ändern zu:
User=root
Group=root

# Service neu starten
sudo systemctl daemon-reload
sudo systemctl restart telegram-commander.service
```

---

### Problem: Safety-Gate Reset schlägt fehl

**Symptom:** "Keine Schreibberechtigung"

**Lösung 1: Service als root laufen lassen** (siehe oben)

**Lösung 2: status.json-Berechtigung prüfen**
```bash
ls -la /opt/apps/monitoring/status.json
# Sollte writeable für den Service-User sein
```

---

### Problem: Button-Menü wird nicht angezeigt

**Ursache:** Alte Telegram-Version oder Bot-Cache

**Lösung:**
1. Telegram schließen & neu öffnen
2. Bot-Chat löschen & neu starten
3. `/start` erneut senden

---

## 🔄 Migration

### Von v2.0 → v3.0 (April 2026)

**Gute Nachricht:** Keine Breaking Changes! 🎉  
**Aber:** Du musst `reports.json` Generierung sicherstellen.

**Schritte:**

1. **Code aktualisieren:**
   ```bash
   cd /opt/apps/pcloud-tools/main
   git pull
   ```

2. **reports.json prüfen:**
   ```bash
   # Existiert die Datei?
   ls -la /opt/apps/monitoring/reports.json
   
   # Falls nicht:
   sudo /opt/apps/pcloud-tools/main/scripts/generate_reports.sh
   ```

3. **Service neu starten:**
   ```bash
   sudo systemctl restart telegram-commander.service
   ```

4. **Features testen:**
   - Sende `/status` → Sollte Performance Stats zeigen
   - Klick auf **🧬 Malware** → Sollte echte Zahlen zeigen (nicht mehr 0/0/0)

**Was ist jetzt besser?**
- ✅ Malware-Daten korrekt (liest aus DB)
- ✅ Kompaktes Dashboard-Layout
- ✅ Performance-Statistiken (30d)
- ✅ LIVE Safety-Gate Status

---

### Von v1.0 → v2.0

**Gute Nachricht:** Keine Breaking Changes! 🎉

**Schritte:**
1. **Code aktualisieren:**
   ```bash
   cd /opt/apps/pcloud-tools/main
   git pull
   ```

2. **Service neu starten:**
   ```bash
   sudo systemctl restart telegram-commander.service
   ```

3. **Features testen:**
   - Sende `/start` an den Bot
   - Buttons sollten erscheinen
   - Klick auf **📊 Status** zum Testen

**Alte Befehle funktionieren weiterhin:**
- `/status`, `/backup`, `/help` wie gewohnt

---

## 💡 Tipps & Best Practices

### 1. Regelmäßige Status-Checks
Gewöhne dir an, jeden Morgen `/status` zu checken. So siehst du Probleme sofort.

### 2. Alerts aktivieren
Der Bot ist **Command & Control**. Für proaktive Warnungen brauchst du:
- `send_alert.sh` (via Apprise)
- Siehe: [docs/TELEGRAM.md](TELEGRAM.md)

### 3. Log-Abfrage nutzen
Statt per SSH einzuloggen, nutze **📜 Logs** sofort im Chat.

### 4. Safety-Gate nur nach Prüfung resetten
**RED** bedeutet echtes Ransomware-Risiko! Prüfe immer manuell:
```bash
# SSH-Login
ssh user@backup-server

# EntropyWatcher-Logs prüfen
journalctl -u entropywatcher-*.service -n 100 --no-pager

# Nur bei Fehlalarm via Bot resetten
```

---

## 📚 Weiterführende Dokumentation

| Dokument | Link |
|----------|------|
| **Alert-Setup** | [docs/TELEGRAM.md](TELEGRAM.md) |
| **Apprise-Config** | [docs/APPRISE_SETUP.md](APPRISE_SETUP.md) |
| **Systemd-Services** | [systemd/README.md](../systemd/README.md) |
| **pCloud-Tools** | [README.md](../README.md) |
| **EntropyWatcher** | [entropy-watcher-und-clamav-scanner](https://github.com/lastphoenx/entropy-watcher-und-clamav-scanner) |

---

## 🤝 Support

**Problem melden:**
```bash
# Issue auf GitHub erstellen mit:
- /status Output
- journalctl -u telegram-commander.service -n 100
- /etc/pcloud-tools/telegram_commander.conf (ohne Token!)
```

**Feature-Request:**  
Neue Features? → GitHub Issue mit `[Feature]` Tag

---

## 📝 Changelog

### v2.0 (2026-04-19)
✨ **Neue Features:**
- Inline Keyboard-Menü (Buttons)
- Log-Abfrage (`/logs`)
- Safety-Gate Reset (`/reset_safety_gate`)
- Callback-Handler für Button-Klicks
- Mehrere Log-Services (backup-pipeline, RTB, pCloud, telegram-commander)

### v1.0 (2026-03-15)
🎉 **Initial Release:**
- `/status` — Status-Abfrage
- `/backup` — Backup-Trigger mit Safety-Gate-Check
- Whitelist-Sicherheit
- systemd-Service

---

## 📜 Lizenz

Siehe [LICENSE](../LICENSE) im Root-Verzeichnis.

**TL;DR:** Open Source, nutze & modifiziere frei.

---

**Viel Spaß mit deinem Telegram-Bot! 🤖**
