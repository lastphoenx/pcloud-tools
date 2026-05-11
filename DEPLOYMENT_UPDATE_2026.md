# Dashboard Update April 2026 - Deployment Guide

## 🚨 Breaking Change: Absolute Pfade

Das Dashboard verwendet jetzt absolute Pfade für alle internen Links:
- `/pcloud-tools/dashboard/index.html`
- `/entropy-watcher-und-clamav-scanner/docs/MONITORING.html`

**Konsequenz:** Der Webserver muss im **Parent-Verzeichnis** laufen, nicht mehr im `dashboard/` Verzeichnis.

---

## 🔄 Update auf bestehenden Systemen

### Schritt 1: Code aktualisieren

```bash
# Auf dem Produktions-Server
cd /opt/apps

# Git pull in beiden Repos
cd pcloud-tools
git pull origin main

cd ../entropy-watcher-und-clamav-scanner  
git pull origin main

# Fertig! Der Server-Script liegt jetzt in pcloud-tools/monitoring-dashboard-server.py
# Kein Kopieren nötig - der Service zeigt direkt darauf
```

### Schritt 2: Systemd Service aktualisieren

```bash
# Service stoppen
sudo systemctl stop monitoring-dashboard.service

# Neue Service-Datei kopieren
sudo cp pcloud-tools/systemd/monitoring-dashboard.service.example \
        /etc/systemd/system/monitoring-dashboard.service

# Service-Datei anpassen
sudo nano /etc/systemd/system/monitoring-dashboard.service

# Ändere nur:
# - User=YOUR_USER → User=pi (oder dein Benutzer)
# - Group=YOUR_USER → Group=pi
#
# Die Pfade sollten bereits korrekt sein:
# - WorkingDirectory=/opt/apps
# - ExecStart=/usr/bin/python3 /opt/apps/pcloud-tools/monitoring-dashboard-server.py
# - ReadOnlyPaths=/opt/apps/pcloud-tools
# - ReadOnlyPaths=/opt/apps/entropy-watcher-und-clamav-scanner

# Daemon reload
sudo systemctl daemon-reload
```

### Schritt 3: Service neu starten

```bash
# Service starten
sudo systemctl start monitoring-dashboard.service

# Status prüfen
sudo systemctl status monitoring-dashboard.service

# Logs prüfen
sudo journalctl -u monitoring-dashboard.service -f
```

### Schritt 4: Funktionstest

```bash
# Dashboard testen
curl http://localhost:8080/pcloud-tools/dashboard/index.html

# Dokumentation testen  
curl http://localhost:8080/entropy-watcher-und-clamav-scanner/docs/index.html

# Status-JSON testen
curl http://localhost:8080/opt/apps/monitoring/status.json
```

**Im Browser öffnen:**
- Dashboard: `http://<server-ip>:8080/pcloud-tools/dashboard/index.html`
- Alle Navigation-Links sollten funktionieren

---

## 📁 Erwartete Verzeichnisstruktur

```
/opt/apps/
├── monitoring/                              ← Status-JSON Output
│   ├── status.json
│   └── reports.json
├── pcloud-tools/
│   ├── monitoring-dashboard-server.py      ← Webserver (läuft von hier, WorkDir=/opt/apps)
│   ├── dashboard/
│   │   ├── index.html                      ← Dashboard UI
│   │   └── server.py                       ← Legacy (nur für lokale Dev)
│   └── systemd/
│       └── monitoring-dashboard.service.example
└── entropy-watcher-und-clamav-scanner/
    └── docs/
        ├── index.html                       ← Dokumentations-Hub
        ├── MONITORING.html
        └── ...
```

---

## 🔍 Troubleshooting

### Problem: 404 bei `/entropy-watcher-und-clamav-scanner/docs/`

**Ursache:** Server läuft noch im alten `dashboard/` Verzeichnis

**Lösung:**
```bash
# Prüfe WorkingDirectory im Service
systemctl cat monitoring-dashboard.service | grep WorkingDirectory

# Sollte sein: WorkingDirectory=/opt/apps
# Falls nicht: Service-Datei anpassen und neu starten
```

### Problem: Links funktionieren nicht

**Ursache:** Browser cached alte HTML-Seiten

**Lösung:**
```bash
# Hard-Refresh im Browser
# Chrome/Firefox: Ctrl+Shift+R
# Oder: Private/Incognito Fenster öffnen
```

### Problem: Server startet nicht

```bash
# Logs prüfen
sudo journalctl -u monitoring-dashboard.service -n 50

# Häufige Fehler:
# - Python3 nicht gefunden → ExecStart-Pfad prüfen
# - Permission denied → User/Group in Service-Datei prüfen  
# - Port bereits belegt → `sudo lsof -i :8080`
```

---

## 🧪 Lokaler Test (vor Deployment)

Auf dem **Entwicklungs-PC** (Windows/Linux):

```bash
# Im Workspace-Root (github_code/)
cd c:\Users\tsant\OneDrive\Dokumente\vsc\github_code

# Server starten
python monitoring-dashboard-server.py

# Browser öffnen
# http://localhost:8080/pcloud-tools/dashboard/index.html
```

**Erwartetes Output:**
```
============================================================
🚀 Monitoring Dashboard Server
============================================================
📁 Document Root: C:\Users\tsant\OneDrive\Dokumente\vsc\github_code
🌐 Port: 8080
🔗 Dashboard: http://localhost:8080/pcloud-tools/dashboard/index.html
📚 Docs: http://localhost:8080/entropy-watcher-und-clamav-scanner/docs/
============================================================
Press Ctrl+C to stop the server
```

---

## 📊 Nginx Alternative

Falls Nginx verwendet wird:

```nginx
server {
    listen 80;
    server_name monitoring.yourdomain.de;
    
    root /opt/apps;
    index index.html;
    
    location /pcloud-tools/dashboard/ {
        alias /opt/apps/pcloud-tools/dashboard/;
    }
    
    location /entropy-watcher-und-clamav-scanner/docs/ {
        alias /opt/apps/entropy-watcher-und-clamav-scanner/docs/;
    }
    
    location /opt/apps/monitoring/ {
        alias /opt/apps/monitoring/;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }
}
```

---

## ✅ Changelog

**v2.0 (April 2026)**
- ✨ Absolute Pfade für Multi-Repo Navigation
- ✨ Kontextuelle Dokumentations-Links bei YELLOW/RED Status
- ✨ Unified Dark Theme (#0a0e1a)
- ✨ LIVE_SG_DETAILS Dokumentation
- ✨ 11 Navigation-Links im Dashboard
- 🔧 monitoring-dashboard-server.py für Workspace-Root
- 🔧 Systemd Service WorkingDirectory Update
- 📚 HTML-Wrapper für MONITORING.md und DASHBOARD.md
