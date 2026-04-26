# Dashboard - Web-UI Monitoring

**Version:** 2.0 (mit Timer-Status & Horizontal Scrolling)  
**Letzte Aktualisierung:** 21. April 2026

---

## 📊 Überblick

Das **Monitoring Dashboard** ist eine vollständig selbst-gehostete Web-UI für deine Backup-Pipeline. Es zeigt:

✅ **Echtzeit-Status** aller Services (RTB, pCloud, EntropyWatcher)  
✅ **Safety-Gate** Ampel (🟢 GREEN / 🟡 YELLOW / 🔴 RED)  
✅ **Malware & Integrität** Monitoring mit DB-Daten  
✅ **Performance-Statistiken** (30 Tage)  
✅ **Timer-Status** aller systemd-Timer  
✅ **Responsive Design** (Desktop & Smartphone)

**Vorteile:**
- 🚀 **Kein Framework** - Vanilla HTML/JS (lädt in < 1 Sekunde)
- 🔒 **Lokal gehostet** - Keine externen Services
- 📱 **Mobile-First** - Sieht auf dem Smartphone besser aus als am PC
- 🔄 **Auto-Refresh** - Alle 30 Sekunden aktualisiert

---

## 🖼️ Screenshot-Tour

### Desktop-Ansicht
```
┌──────────────────────────────────────────────────────┐
│ Backup & Monitoring Dashboard      pi-nas  18:56:00 │
├──────────────────────────────────────────────────────┤
│ ✅ OK - System läuft stabil                          │
├──────────────────────────────────────────────────────┤
│  🔄 Backup-Status          📊                        │
│  ✅ RTB    ✅ pCloud    🟢 GREEN (live)             │
│  267 Snapshots  •  Erfolgsrate: 100%                │
│  [▼ Letzte Backup-Läufe]                            │
├──────────────────────────────────────────────────────┤
│  🛡️ Malware & Integrität   ⚠️                       │
│  🟢 5 Aktiv  🔴 1 Flagged  ⚠️ 8 Missing            │
│  Entropywatcher Nas  •  nächster: 21:22            │
├──────────────────────────────────────────────────────┤
│  ⏰ Timer-Status                                     │
│  [Tabelle mit allen systemd-Timern scrollbar]       │
└──────────────────────────────────────────────────────┘
```

### Smartphone-Ansicht
```
┌────────────────────┐
│ Dashboard          │
│ ✅ OK - Alles gut  │
├────────────────────┤
│ 🔄 Backup          │
│ RTB ✅  pCloud ✅  │
│ Safety 🟢 GREEN    │
│                    │
│ Snapshots: 267     │
│ Erfolg: 100%       │
│ [▼ Details]        │
├────────────────────┤
│ 🛡️ Malware ⚠️     │
│ 1 Flagged          │
│ 8 Missing          │
└────────────────────┘
```

---

## 🆕 Was ist neu in v2.0? (April 2026)

### 1. ⏰ Timer-Status Tabelle

**NEU:** Übersicht aller systemd-Timer mit LastRun & NextRun:

```
Unit                              | Enabled | Active | LastRun              | NextRun
----------------------------------+---------+--------+----------------------+----------------------
entropywatcher-nas.timer          | enabled | active | 2026-04-19 20:21:47  | 2026-04-19 21:22:18
                                  |         |        | (00d 00h 16m)        | (00d 00h 44m)
entropywatcher-nas-av.timer       | enabled | active | 2026-04-19 02:05:22  | 2026-04-20 02:07:21
                                  |         |        | (00d 18h 32m)        | (00d 05h 29m)
backup-pipeline.timer             | enabled | active | 2026-04-19 20:00:00  | 2026-04-20 04:00:00
                                  |         |        | (00d 00h 38m)        | (00d 07h 22m)
```

**Features:**
- Zeigt alle EntropyWatcher + Backup-Pipeline Timer
- LastRun mit Delta (vor wie vielen Stunden/Minuten)
- NextRun mit Delta (in wie vielen Stunden/Minuten)
- Status-Badges (🟢 enabled/active, 🔴 disabled/failed)

**Daten-Quelle:** `status.json` → `timers[]` Array

---

### 2. ↔️ Horizontales Scrollen für Tabellen

**Problem (v1.0):** Lange Tabellen waren auf Smartphone abgeschnitten

**Lösung (v2.0):** Automatisches horizontales Scrollen!

```css
.tile-detail-inner {
  overflow-x: auto;  /* Horizontal scrolling */
}

.report-table {
  min-width: 600px;  /* Ensures scroll activates */
}
```

**Betrifft:**
- 📋 **Letzte Backup-Läufe** (kleine Tabelle in Tile)
- 📊 **Recent Backups** (große Detail-Tabelle)
- ⏰ **Timer-Status** (neue Tabelle)

**Auf Smartphone:** Wische horizontal um alle Spalten zu sehen!

---

### 3. 🌐 Dashboard URL Auto-Detection

**NEU:** Dashboard kennt seine eigene URL:

```json
{
  "dashboard_url": "http://192.168.141.140:8080"
}
```

**Verwendet von:**
- Telegram Bot → "🌐 Dashboard öffnen" Button
- Alerting → Link in Benachrichtigungen
- API-Aufrufe → Selbst-Referenzierung

**Auto-Detect Logik:**
```bash
# In aggregate_status.sh
DASHBOARD_URL="${DASHBOARD_URL:-http://$(hostname -I | awk '{print $1}'):8080}"
```

---

### 4. 📦 Malware Summary Aggregation

**NEU:** Dashboard zeigt aggregierte Malware-Statistiken:

```json
"malware_summary": {
  "active_monitors": 5,
  "flagged": 1,
  "missing": 8,
  "errors": 0
}
```

**Berechnung:** `aggregate_status.sh` summiert alle entropywatcher-Services:
- `entropywatcher-nas` → flagged: 1, missing: 0
- `entropywatcher-os` → flagged: 0, missing: 8
- `entropywatcher-nas-av` → flagged: 0, missing: 0
- ...
- **Total:** flagged: 1, missing: 8

**Dashboard zeigt:** 🔴 1 Flagged, ⚠️ 8 Missing in kompakter Kachel-Ansicht

---

## 🔧 Installation & Setup

### 1. Dashboard-Server starten

```bash
# Aktivieren & Starten
sudo systemctl enable --now monitoring-dashboard.service

# Status prüfen
sudo systemctl status monitoring-dashboard.service

# Logs
sudo journalctl -u monitoring-dashboard.service -f
```

**Erwartete Ausgabe:**
```
Dashboard server started on port 8080
```

---

### 2. Dashboard aufrufen

**Im Browser:**
```
http://<Pi-IP>:8080
```

**Beispiel:**
```
http://192.168.141.140:8080
```

**Oder lokal auf dem Pi:**
```
http://localhost:8080
```

---

### 3. Nginx Reverse-Proxy (Optional)

Wenn du das Dashboard über Port 80/443 erreichbar machen willst:

```nginx
# /etc/nginx/sites-available/monitoring-dashboard
server {
    listen 80;
    server_name monitoring.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # JSON-Endpoints ohne Caching
    location ~ \.(json)$ {
        proxy_pass http://127.0.0.1:8080;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }
}
```

**Aktivieren:**
```bash
sudo ln -s /etc/nginx/sites-available/monitoring-dashboard \
           /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 📊 Daten-Quellen

Das Dashboard liest zwei JSON-Dateien:

### 1. status.json
**Pfad:** `/opt/apps/monitoring/status.json`  
**Generiert von:** `aggregate_status.sh` (alle 30s)  
**Enthält:**
- Live-Service-Status (systemd units)
- RTB Wrapper Status (letzte Backup-Details)
- pCloud Status (sync-status, pending files)
- Safety-Gate Status (LIVE + historisch)
- Malware Summary (aggregiert)
- Timer Status (alle systemd-Timer)
- Overall Status (OK/WARNING/CRITICAL)

**Beispiel:**
```json
{
  "timestamp": "2026-04-19T20:38:00Z",
  "hostname": "pi-nas",
  "dashboard_url": "http://192.168.141.140:8080",
  "overall_status": "OK",
  "services": {
    "entropywatcher-nas": {
      "status": "active",
      "next_run": "2026-04-19T21:22:18"
    }
  },
  "scripts": {
    "rtb_wrapper": {
      "status": "success",
      "snapshot_count": 267,
      "live_safety_gate": "GREEN"
    }
  },
  "malware_summary": {
    "flagged": 1,
    "missing": 8
  },
  "timers": [
    {
      "unit": "entropywatcher-nas.timer",
      "enabled": "enabled",
      "active": "active",
      "last_run": "2026-04-19 20:21:47",
      "next_run": "2026-04-19 21:22:18"
    }
  ]
}
```

---

### 2. reports.json
**Pfad:** `/opt/apps/monitoring/reports.json`  
**Generiert von:** `generate_reports.sh` (täglich)  
**Enthält:**
- EntropyWatcher DB-Daten (echte flagged/missing files)
- Performance-Statistiken (30d)
- Recent Backups (letzte 10 Läufe)
- Failed Backups (letzte 7 Tage)
- Phase-Statistiken (Durchschnitts-Dauer pro Phase)

**Beispiel:**
```json
{
  "timestamp": "2026-04-19T05:00:00Z",
  "entropywatcher": {
    "flagged_files": {
      "entropywatcher-nas": 1,
      "entropywatcher-os": 0
    },
    "last_scans": [
      {
        "service_name": "entropywatcher-os",
        "missing_count": 8,
        "scan_timestamp": "2026-04-19 03:40:00"
      }
    ]
  },
  "performance_stats": {
    "total_runs": 30,
    "successful_runs": 30,
    "avg_duration_min": 57.33,
    "total_gb_uploaded": 268.83
  },
  "recent_backups": [
    {
      "snapshot": "2026-04-19-20h00m00s",
      "status": "success",
      "started_at": "2026-04-19 20:00:02",
      "finished_at": "2026-04-19 20:57:35",
      "duration_sec": 3453,
      "gb_uploaded": 8.92,
      "files_uploaded": 1247
    }
  ]
}
```

---

## 🎨 Dashboard-Komponenten

### Tile 1: Backup-Status

**Zeigt:**
- 🔄 RTB Status (✅ Success / ❌ Failed / ⛔ Blocked / ⏭️ Skipped)
- ☁️ pCloud Status (✅ OK / ⚠️ Warning / ❌ Critical)
- 🟢/🟡/🔴 Safety-Gate (LIVE wenn verfügbar)
- 📊 Snapshot-Count
- 📈 Erfolgsrate (30d)
- ⏱️ Durchschnitts-Dauer
- ☁️ Gesamt GB uploaded (30d)

**Expandierbar:** Klick auf "▼ Letzte Backup-Läufe" zeigt Tabelle mit letzten 7 Backups

---

### Tile 2: Malware & Integrität

**Zeigt:**
- 🟢 Anzahl aktiver Monitore
- 🔴 Flagged Files (verdächtige Dateien)
- ⚠️ Missing Files (gelöschte/fehlende Dateien)
- ❌ Fehlerhafte Monitore

**Service-Liste:**
- `entropywatcher-nas` → 🟢 aktiv, nächster: 21:22
- `entropywatcher-os` → 🟢 aktiv, nächster: 03:44
- `entropywatcher-nas-av` → 🟢 aktiv, nächster: 02:07
- `honeyfile-monitor` → 🟢 aktiv

---

### Tile 3: System-Gesundheit

**Zeigt:**
- `cleanup-samba-recycle` → Status
- `backup-pipeline` → Status (mit Safety-Gate LIVE wenn blockiert)
- ⚠️ Letzte Fehler (7 Tage)

---

### Detail-Sektion: Timer-Status

**Neue Tabelle** (scrollbar auf Smartphone):
- Unit-Name
- Enabled (enabled/disabled)
- Active (active/inactive)
- LastRun (mit Delta "vor X Stunden")
- NextRun (mit Delta "in X Stunden")

**Auto-Update:** Alle 30 Sekunden mit neuem status.json

---

## 🔄 Auto-Refresh Mechanismus

**Dashboard aktualisiert sich automatisch:**

```javascript
// Alle 30 Sekunden
setInterval(() => {
  fetch('/monitoring/status.json?t=' + Date.now())
    .then(r => r.json())
    .then(data => render(data));
}, 30000);
```

**Cache-Control Headers:**
```python
# In server.py
def end_headers(self):
    self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
    self.send_header('Pragma', 'no-cache')
    self.send_header('Expires', '0')
```

**Countdown-Anzeige:** "Nächstes Update in 25s"

---

## 📱 Responsive Design

**Desktop (> 1280px):**
- 3-Spalten-Grid für Tiles
- Große Detail-Karten

**Tablet (1001px - 1280px):**
- 2-Spalten-Grid
- Kompaktere Darstellung

**Smartphone (< 1000px):**
- 1-Spalten-Layout
- Alle Tiles untereinander
- Horizontales Scrollen für Tabellen
- Touch-optimiert

---

## 🐛 Troubleshooting

### Problem: Dashboard zeigt keine Daten

**Diagnose:**
```bash
# status.json existiert?
ls -la /opt/apps/monitoring/status.json

# Inhalt valide?
cat /opt/apps/monitoring/status.json | jq .

# Letztes Update?
stat /opt/apps/monitoring/status.json
```

**Lösung:**
```bash
# aggregate_status.sh manuell ausführen
sudo /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh --verbose

# Timer prüfen
sudo systemctl status monitoring-status-update.timer
```

---

### Problem: Timer-Tabelle leer

**Ursache:** `collect_timer_status()` nicht in aggregate_status.sh

**Lösung:**
```bash
# Code aktualisieren
cd /opt/apps/pcloud-tools/main
git pull

# aggregate_status.sh neu ausführen
sudo /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh

# status.json prüfen
cat /opt/apps/monitoring/status.json | jq '.timers'
# Sollte jetzt Array mit Timer-Daten enthalten
```

---

### Problem: Horizontales Scrollen funktioniert nicht

**Ursache:** Alte CSS-Version im Cache

**Lösung:**
```bash
# Hard-Refresh im Browser
Ctrl + Shift + R  (Windows/Linux)
Cmd + Shift + R   (macOS)

# Oder Browser-Cache leeren
```

---

### Problem: Malware zeigt 0/0/0

**Ursache:** reports.json fehlt oder veraltet

**Lösung:**
```bash
# reports.json generieren
sudo /opt/apps/pcloud-tools/main/scripts/generate_reports.sh

# Prüfen
cat /opt/apps/monitoring/reports.json | jq '.entropywatcher.flagged_files'

# Dashboard neu laden (Ctrl+Shift+R)
```

---

## 💡 Tipps & Best Practices

### 1. Bookmark anlegen
Speichere `http://<Pi-IP>:8080` als Lesezeichen auf deinem Smartphone → Schneller Zugriff!

### 2. Als App speichern (iOS/Android)
**iOS:** Safari → Teilen → Zum Home-Bildschirm → Dashboard-Icon auf Homescreen!  
**Android:** Chrome → Menü → Zum Startbildschirm hinzufügen

### 3. Kiosk-Mode für Monitoring-Bildschirm
```bash
# Chromium im Vollbild-Modus
chromium-browser --kiosk --app=http://localhost:8080
```

### 4. Dark Mode ist Standard
Dashboard nutzt dunkle Farben (`--bg: #0f172a`) → Augenschonend für 24/7 Monitoring!

---

## 📚 Weiterführende Dokumentation

- [ARCHITECTURE.md](ARCHITECTURE.md) → Dashboard-Architektur Details
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) → Anpassungen & Erweiterungen
- [TELEGRAM_COMMANDER.md](TELEGRAM_COMMANDER.md) → Mobile Bot-Alternative
- [SETUP.md](SETUP.md) → Initial-Setup Anleitung

---

## 🔗 Related Projects

**Dashboard basiert auf:**
- `aggregate_status.sh` → JSON-Generator
- `generate_reports.sh` → DB-Report Generator
- `server.py` → Minimaler HTTP-Server (Python 3)

**Verwendet von:**
- Telegram Bot → Dashboard URL Button
- Alerting → Status-Checks
- Monitoring → Zentrale Daten-Quelle
