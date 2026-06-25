# Monitoring Dashboard

Web-basiertes Dashboard zur Überwachung aller Backup- und Monitoring-Services.

## Features

✅ **Echtzeit-Überwachung** - Auto-refresh alle 30 Sekunden  
📊 **Übersichtliche Status-Karten** - Systemd Services, RTB, pCloud  
🎨 **Responsive Design** - Funktioniert auf Desktop, Tablet, Mobile  
🚦 **Farb-codierte Status** - Grün (OK), Gelb (Warning), Rot (Critical)  
⚡ **Keine Abhängigkeiten** - Nur HTML/CSS/JavaScript (Vanilla)

## Installation

### 1. Dashboard Service einrichten

**WICHTIG: Multi-Repo Setup (April 2026 Update)**

Seit dem Dashboard-Update vom April 2026 verwendet das System **absolute Pfade** (`/pcloud-tools/dashboard/`, `/entropy-watcher-und-clamav-scanner/docs/`). Der Webserver muss daher im **gemeinsamen Parent-Verzeichnis** laufen (normalerweise `/opt/apps/`).

**Option A: Standalone Python-Server (empfohlen)**

```bash
# 1. Systemd Service installieren
sudo cp main/systemd/monitoring-dashboard.service.example /etc/systemd/system/monitoring-dashboard.service

# 2. Service-Datei anpassen (nur User ändern)
sudo nano /etc/systemd/system/monitoring-dashboard.service
# Ersetze: YOUR_USER durch tatsächlichen Benutzer (z.B. pi, thomas)
# Die Pfade sollten bereits korrekt sein:
#   WorkingDirectory=/opt/apps
#   ExecStart=/usr/bin/python3 /opt/apps/pcloud-tools/main/monitoring-dashboard-server.py
#   ReadOnlyPaths=/opt/apps/pcloud-tools/main
#   ReadOnlyPaths=/opt/apps/entropywatcher/main

# 3. Service starten
sudo systemctl daemon-reload
sudo systemctl enable --now monitoring-dashboard.service

# 5. Verify
systemctl status monitoring-dashboard.service
curl http://localhost:8080/pcloud-tools/dashboard/index.html
```

**URLs nach dem Update:**
- Dashboard: `http://localhost:8080/pcloud-tools/dashboard/index.html`
- Dokumentation: `http://localhost:8080/entropy-watcher-und-clamav-scanner/docs/index.html`
- API: `http://localhost:8080/opt/apps/monitoring/status.json` (via symlink oder mapping)

**Legacy `dashboard/server.py`:**  
Der alte `dashboard/server.py` funktioniert weiterhin für **lokale Entwicklung** im `dashboard/` Verzeichnis, aber die absoluten Links zu anderen Repos funktionieren nicht. Für Production verwende `monitoring-dashboard-server.py` im Parent-Verzeichnis.

**Cache-Control Headers:**
```
Cache-Control: no-store, no-cache, must-revalidate
Pragma: no-cache
Expires: 0
```

**Option B: Nginx (für Produktivumgebung)**

```bash
# Dashboard und Docs im Webroot verfügbar machen
sudo mkdir -p /var/www/monitoring
sudo ln -s /opt/apps/pcloud-tools/dashboard /var/www/monitoring/pcloud-tools/dashboard
sudo ln -s /opt/apps/entropy-watcher-und-clamav-scanner/docs /var/www/monitoring/entropy-watcher-und-clamav-scanner/docs

# Status-JSON-Verzeichnis mapping
sudo ln -s /opt/apps/monitoring /var/www/monitoring/opt/apps/monitoring
sudo chown www-data:www-data /opt/apps/monitoring
```

### 2. Nginx konfigurieren

```nginx
server {
    listen 80;
    server_name monitoring.yourdomain.de;
    
    root /var/www/monitoring;
    index index.html;
    
    # Serve dashboard
    location / {
        try_files $uri $uri/ =404;
    }
    
    # Serve status JSON
    location /monitoring/status.json {
        alias /opt/apps/monitoring/status.json;
        add_header Cache-Control "no-cache, must-revalidate";
        add_header Content-Type "application/json";
    }
    
    # Optional: Basic Auth
    # auth_basic "Restricted";
    # auth_basic_user_file /etc/nginx/.htpasswd;
}
```

**Nginx neu laden:**
```bash
sudo nginx -t
sudo systemctl reload nginx
```

### 3. Aggregator einrichten

**Systemd Timer (event-getriggert + 15-min Fallback):**

```bash
sudo cp main/systemd/monitoring-status-update.service.example /etc/systemd/system/monitoring-status-update.service
sudo cp main/systemd/monitoring-status-update.timer.example /etc/systemd/system/monitoring-status-update.timer
sudo cp main/systemd/monitoring-dashboard.service.example /etc/systemd/system/monitoring-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now monitoring-status-update.timer
sudo systemctl enable --now monitoring-dashboard.service
sudo systemctl status monitoring-status-update.timer
```

Der Timer läuft automatisch nach `backup-pipeline.service` und `entropywatcher-nas.service`
sowie alle 15 Minuten als Fallback — kein Cronjob nötig.

### 4. Alerts konfigurieren (optional)

**Benachrichtigungen bei Status-Änderungen:**

```bash
# Alerts werden vom monitoring-status-update.service nach jedem Aggregator-Lauf ausgelöst.
# Kein separater Cronjob nötig — der Timer übernimmt das Scheduling.
sudo systemctl status monitoring-status-update.timer
```

## Integration mit Authentik

**Reverse Proxy mit Authentik SSO:**

```nginx
server {
    listen 443 ssl http2;
    server_name monitoring.yourdomain.de;
    
    ssl_certificate /etc/letsencrypt/live/yourdomain.de/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.de/privkey.pem;
    
    # Authentik Forward Auth
    location / {
        auth_request /outpost.goauthentik.io/auth/nginx;
        error_page 401 = @goauthentik_proxy_signin;
        auth_request_set $auth_cookie $upstream_http_set_cookie;
        add_header Set-Cookie $auth_cookie;
        
        root /var/www/monitoring;
        index index.html;
        try_files $uri $uri/ =404;
    }
    
    location /monitoring/status.json {
        auth_request /outpost.goauthentik.io/auth/nginx;
        
        alias /opt/apps/monitoring/status.json;
        add_header Cache-Control "no-cache, must-revalidate";
        add_header Content-Type "application/json";
    }
    
    # Authentik endpoints
    location /outpost.goauthentik.io {
        proxy_pass https://authentik.yourdomain.de/outpost.goauthentik.io;
        proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
    }
    
    location @goauthentik_proxy_signin {
        internal;
        add_header Set-Cookie $auth_cookie;
        return 302 /outpost.goauthentik.io/start?rd=$request_uri;
    }
}
```

## Überwachte Komponenten

### Systemd Services
- **entropywatcher-nas** - Entropy-Check für NAS-Verzeichnis
- **entropywatcher-os** - Entropy-Check für OS-Verzeichnis
- **entropywatcher-nas-av** - NAS + ClamAV Scan
- **entropywatcher-os-av** - OS + ClamAV Scan
- **honeyfile-monitor** - Honeyfile Überwachung
- **cleanup-samba-recycle** - Samba Recycle-Bin Cleanup
- **backup-pipeline** - Backup-Pipeline Orchestrierung

### Backup Scripts
- **RTB Wrapper** - rsync time-backup Snapshots
- **pCloud Backup** - Cloud-Sync Status

## Status-Codes

| Status | Bedeutung | Farbe |
|--------|-----------|-------|
| **OK** | Alle Services laufen normal | 🟢 Grün |
| **WARNING** | Einzelne Services haben Probleme | 🟡 Gelb |
| **CRITICAL** | Kritische Fehler, sofortiges Handeln erforderlich | 🔴 Rot |
| **RUNNING** | Backup läuft gerade | 🔵 Blau |

## Fehlerbehebung

### Dashboard zeigt "Failed to load"

**Prüfe ob status.json existiert:**
```bash
ls -lh /opt/apps/monitoring/status.json
```

**Generiere manuell:**
```bash
/opt/apps/pcloud-tools/main/scripts/aggregate_status.sh --verbose
```

**Prüfe Nginx-Config:**
```bash
sudo nginx -t
curl http://localhost/monitoring/status.json
```

### Status JSON ist leer/veraltet

**Prüfe Cron/Timer:**
```bash
sudo systemctl status monitoring-aggregator.timer
sudo journalctl -u monitoring-aggregator.service -n 20
```

**Teste Aggregator manuell:**
```bash
cd /opt/apps/pcloud-tools/main
sudo ./scripts/aggregate_status.sh --verbose
cat /opt/apps/monitoring/status.json | jq .
```

### Services zeigen "not_installed"

**Installiere fehlende Services:**
```bash
# Entropy-Watcher
cd /opt/apps/entropywatcher/main
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable entropywatcher-nas.service
```

## Anpassung

### Eigene Services hinzufügen

**In aggregate_status.sh:**
```bash
SYSTEMD_SERVICES=(
  "entropywatcher-nas"
  "mein-eigener-service"  # ← Hier hinzufügen
)
```

### Auto-Refresh-Interval ändern

**In dashboard/index.html:**
```javascript
const REFRESH_INTERVAL = 60000; // 60 Sekunden statt 30
```

### Timer-Intervall anpassen

**Für schnellere Updates (jede Minute statt alle 2 Minuten):**

```bash
# Editiere /etc/systemd/system/monitoring-status-update.timer
[Timer]
OnCalendar=*:*:00  # Jede Minute (12:00:00, 12:01:00, 12:02:00, ...)
# Statt: OnCalendar=*:0/2  # Alle 2 Minuten (nur gerade Minuten)

sudo systemctl daemon-reload
sudo systemctl restart monitoring-status-update.timer
```

**Wichtig:** Kürzere Intervalle erhöhen die CPU-Last. Für Production sind 2-5 Minuten empfohlen.

---

## Wichtige Konzepte

### Safety-Gate Status-Timing

**Das Dashboard zeigt zwei verschiedene Safety-Gate Werte:**

1. **`live_safety_gate`** - Aktueller Echtzeit-Status (wird bei jedem aggregate_status.sh Run neu abgefragt)
2. **`safety_gate` (letzter Lauf)** - Historischer Wert vom letzten Backup-Run

**YELLOW-Status nach Scan:**

Nach jedem EntropyWatcher-Scan wechselt der Status für **10 Minuten** auf YELLOW mit Grund `"too_fresh_to_trust"`:

```json
{
  "status": "yellow",
  "reasons": ["too_fresh_to_trust"],
  "counters": {"age_min": 2.5, "safeage_min": 10}
}
```

**Warum?** Anti-Ransomware-Schutz: Wenn eine Infektion **während** des Scans passiert, wird sie erst beim **nächsten** Scan erkannt. Die 10-Minuten-Wartezeit gibt dem System Zeit, verdächtige Aktivitäten zu erkennen.

**Timeline-Beispiel:**
```
14:20:00 - EntropyWatcher Scan läuft
14:22:25 - Scan fertig, Status: YELLOW (age_min: 0)
14:30:00 - Timer-Update, Status: YELLOW (age_min: 7.5)
14:32:25 - 10 Minuten erreicht, Status: GREEN (age_min: 10+)
```

**Implikation fürs Dashboard:**
- Nach jedem EW-Scan: Dashboard zeigt YELLOW für 10 Minuten (normal!)
- Live Safety-Gate ≠ historischer Safety-Gate vom Backup-Lauf
- Timer-Intervall sollte < 10 Minuten sein, sonst miss man Status-Änderungen
```

### Farben anpassen

**CSS-Variablen in index.html:**
```css
.status-badge.ok {
  background: #eigene-farbe;
  color: #text-farbe;
}
```

## Sicherheit

⚠️ **Wichtig:**
- Dashboard enthält sensitive Informationen über Backup-Status
- **Immer** mit Authentifizierung absichern (nginx Basic Auth ODER Authentik)
- Nur über HTTPS bereitstellen (Let's Encrypt)
- Keine öffentliche Exposition ohne Auth

**Empfohlene Setup:**
- Authentik SSO (Single Sign-On)
- HTTPS mit Let's Encrypt
- Firewall-Regeln (nur aus lokalem Netz)
- VPN-Gateway für externen Zugriff

## API-Referenz

### GET /monitoring/status.json

**Response:**
```json
{
  "timestamp": "2026-04-15T15:50:00Z",
  "hostname": "pi-nas",
  "overall_status": "OK",
  "exit_code": 0,
  "services": {
    "entropywatcher-nas": {
      "status": "active",
      "enabled": "yes",
      "last_start": "2026-04-15T14:30:00Z",
      "exit_code": "0",
      "message": "Backup completed successfully"
    }
  },
  "scripts": {
    "rtb_wrapper": {
      "status": "success",
      "last_run": "2026-04-15 14:00:00",
      "snapshot_count": 12,
      "dry_run_result": "no_changes",
      "dry_run_pipeline_only": { "kind": "pipeline_only", "count": 2, "samples": ["pcloud-temp/..."] },
      "dry_run_backup_scope": { "kind": "backup_scope", "count": 2 },
      "exclude_policy": {
        "trigger_only": ["/pcloud-archive/", "/pcloud-temp/"],
        "never_backup": ["__pycache__/", "/restore/", "..."]
      },
      "message": "[success] Backup complete"
    },
    "pcloud_backup": {
      "status_code": 0,
      "status_text": "OK",
      "hostname": "pi-nas",
      "checks": { ... }
    }
  }
}
```

## Lizenz

Siehe Hauptprojekt LICENSE
