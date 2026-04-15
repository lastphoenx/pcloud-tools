# Monitoring Dashboard

Web-basiertes Dashboard zur Überwachung aller Backup- und Monitoring-Services.

## Features

✅ **Echtzeit-Überwachung** - Auto-refresh alle 30 Sekunden  
📊 **Übersichtliche Status-Karten** - Systemd Services, RTB, pCloud  
🎨 **Responsive Design** - Funktioniert auf Desktop, Tablet, Mobile  
🚦 **Farb-codierte Status** - Grün (OK), Gelb (Warning), Rot (Critical)  
⚡ **Keine Abhängigkeiten** - Nur HTML/CSS/JavaScript (Vanilla)

## Installation

### 1. Dashboard deployen

```bash
# Dashboard nach /var/www/ kopieren
sudo mkdir -p /var/www/monitoring
sudo cp dashboard/index.html /var/www/monitoring/

# Status-JSON-Verzeichnis erstellen
sudo mkdir -p /opt/apps/monitoring
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

**Cron-Job für regelmäßige Updates:**

```bash
sudo crontab -e
```

Füge hinzu:
```cron
# Update monitoring status every 5 minutes
*/5 * * * * /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh > /dev/null 2>&1
```

**ODER: Systemd Timer (empfohlen):**

```bash
# /etc/systemd/system/monitoring-aggregator.service
[Unit]
Description=Backup Monitoring Status Aggregator
After=network.target

[Service]
Type=oneshot
ExecStart=/opt/apps/pcloud-tools/main/scripts/aggregate_status.sh
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/systemd/system/monitoring-aggregator.timer
[Unit]
Description=Run monitoring aggregator every 5 minutes
Requires=monitoring-aggregator.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=1s

[Install]
WantedBy=timers.target
```

**Aktivieren:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now monitoring-aggregator.timer
sudo systemctl status monitoring-aggregator.timer
```

### 4. Alerts konfigurieren (optional)

**Benachrichtigungen bei Status-Änderungen:**

```bash
sudo crontab -e
```

```cron
# Check aggregated status and send alerts on changes (every 5 minutes)
*/5 * * * * /opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh > /dev/null 2>&1
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
