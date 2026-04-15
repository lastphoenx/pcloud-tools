# Apprise Setup Guide - Push-Benachrichtigungen für pCloud Backup

Dieses Dokument beschreibt die Installation und Konfiguration von **Apprise** für sofortige Push-Benachrichtigungen bei Backup-Problemen.

## 📋 Übersicht

**Apprise** ist eine universelle Benachrichtigungs-Bibliothek, die über 100 Dienste unterstützt:
- **Telegram** ✅ (Empfohlen - einfach, kostenlos, push)
- **Discord** ✅ (Gut für Teams)
- **ntfy.sh** ✅ (Open Source, self-hosted möglich)
- **Gotify** (Self-hosted)
- **Email, Slack, Matrix, Signal, WhatsApp**, uvm.

---

## 🚀 Installation (Raspberry Pi)

### Option A: Via apt (Empfohlen - Debian/Raspberry Pi OS)

```bash
# Apprise aus offiziellen Repos installieren
sudo apt update
sudo apt install -y apprise

# Test ob installiert
apprise --version
```

### Option B: Via pip (falls nicht im Repo)

```bash
# Python3 und pip installieren (falls nicht vorhanden)
sudo apt update
sudo apt install -y python3 python3-pip

# Apprise installieren
pip3 install --user apprise

# Test ob installiert
apprise --version
```

---

## 📁 Zentrale Konfiguration

Die Apprise-Config liegt **zentral** unter `/opt/apps/apprise.yml` und wird von **allen Monitoring-Tools** geteilt (pCloud-Tools, Entropy-Watcher, RTB, etc.).

### Erstmalige Einrichtung

```bash
# Template kopieren
sudo cp /opt/apps/pcloud-tools/main/apprise.yml.example /opt/apps/apprise.yml

# Bearbeiten (Telegram/Discord Token eintragen)
sudo nano /opt/apps/apprise.yml

# Berechtigungen setzen (nur root lesbar)
sudo chown root:root /opt/apps/apprise.yml
sudo chmod 600 /opt/apps/apprise.yml
```

**Warum `/opt/apps/`?**
- ✅ Zentral für alle Monitoring-Tools
- ✅ Eine Config → Alle Tools nutzen gleiche Benachrichtigungen
- ✅ Einfache Verwaltung
- ✅ Kein Git (Security!)

**Auto-Discovery:** Das `send_alert.sh` Script sucht automatisch in dieser Reihenfolge:
1. `/opt/apps/apprise.yml` (shared, empfohlen)
2. `~/.config/apprise/apprise.yml` (user-level)
3. `../apprise.yml` (tool-local fallback)
4. `/etc/apprise/apprise.yml` (system-level)

---

## 📱 Telegram Setup (Empfohlen)

Telegram ist kostenlos, einfach einzurichten und liefert zuverlässige Push-Benachrichtigungen.

### 1. Bot erstellen

1. Öffne Telegram und suche nach **@BotFather**
2. Sende `/newbot`
3. Wähle einen Namen (z.B. "Pi Backup Monitor")
4. Wähle einen Username (z.B. `pi_backup_bot`)
5. **Speichere den Bot Token** (sieht aus wie `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 2. Chat ID herausfinden

1. Sende eine Nachricht an deinen neu erstellten Bot (z.B. "Hello")
2. Öffne in einem Browser:
   ```
   https://api.telegram.org/bot<DEIN_BOT_TOKEN>/getUpdates
   ```
3. Suche nach `"chat":{"id":987654321` → Das ist deine **Chat ID**

### 3. Konfiguration

```bash
# Zentrale Config bearbeiten
sudo nano /opt/apps/apprise.yml
```

Ersetze die Telegram-Zeile mit deinen Werten:
```yaml
urls:
  - tgram://123456789:ABCdefGHIjklMNOpqrsTUVwxyz/987654321/:
      tag: telegram
```

**Testen:**
```bash
cd /opt/apps/pcloud-tools/main
./scripts/send_alert.sh --test
```

---

## 💬 Discord Setup (für Teams)

### 1. Webhook erstellen

1. Öffne deinen Discord Server
2. **Server Settings** → **Integrations** → **Webhooks** → **New Webhook**
3. Wähle einen Channel (z.B. `#backup-alerts`)
4. **Copy Webhook URL** (sieht aus wie `https://discord.com/api/webhooks/123.../abc...`)

### 2. Konfiguration

Die URL hat das Format: `https://discord.com/api/webhooks/{webhook_id}/{webhook_token}`

In `apprise.yml`:
```yaml
urls:
  - discord://1234567890123456789/AbCdEfGhIjKlMnOpQrStUvWxYz/:
      tag: discord
```

---

## 🔔 ntfy.sh Setup (Open Source)

**ntfy.sh** ist ein Open-Source Push-Service, der auch self-hosted werden kann.

### 1. App installieren

- **Android**: [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- **iOS**: [App Store](https://apps.apple.com/app/ntfy/id1625396347)
- **Web**: https://ntfy.sh

### 2. Topic wählen

Wähle einen **eindeutigen** Topic-Namen (z.B. `pi-nas-backup-alerts-xyz123`)

### 3. In App subscriben

Öffne die ntfy App und subscribe zum Topic: `pi-nas-backup-alerts-xyz123`

### 4. Konfiguration

```yaml
urls:
  - ntfy://pi-nas-backup-alerts-xyz123/:
      tag: ntfy
```

**Self-hosted ntfy**:
```yaml
urls:
  - ntfys://ntfy.yourdomain.de/backup-alerts/:
      tag: ntfy-private
```

---

## 🧪 Test der Konfiguration

### Test-Benachrichtigung senden

```bash
cd /opt/apps/pcloud-tools/main/scripts
chmod +x send_alert.sh
./send_alert.sh --test
```

Du solltest jetzt eine Test-Nachricht auf deinem Handy erhalten! 🎉

---

## ⚙️ Automatisierung

### Cron Job (alle 5 Minuten)

```bash
crontab -e
```

Füge hinzu:
```cron
*/5 * * * * /opt/apps/pcloud-tools/main/scripts/send_alert.sh >> /var/log/pcloud-alerts.log 2>&1
```

### Systemd Timer (präferiert)

Erstelle `/etc/systemd/system/pcloud-health-alert.service`:
```ini
[Unit]
Description=pCloud Health Check Alerting
After=network-online.target

[Service]
Type=oneshot
User=thomas
ExecStart=/opt/apps/pcloud-tools/main/scripts/send_alert.sh
StandardOutput=journal
StandardError=journal
```

Erstelle `/etc/systemd/system/pcloud-health-alert.timer`:
```ini
[Unit]
Description=Run pCloud health check every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
```

Aktivieren:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pcloud-health-alert.timer
sudo systemctl status pcloud-health-alert.timer
```

---

## 🔍 Wie es funktioniert

1. **Health Check** läuft (via Cron/Systemd)
2. **send_alert.sh** ruft `pcloud_health_check.sh --json` auf
3. **Status Code** wird mit letztem Status verglichen (gespeichert in `.status_last`)
4. **Bei Änderung** (OK→WARNING, WARNING→CRITICAL, etc.):
   - Apprise sendet Push-Benachrichtigung
   - Enthält: Status, Issues, Timestamp
5. **Kein Spam**: Solange Status gleich bleibt, keine weiteren Alerts

### Beispiel-Benachrichtigung

```
🚨 CRITICAL - pCloud Backup (pi-nas)

Status: CRITICAL (Code: 2)
Reason: Status changed: OK → CRITICAL

Issues:
  • Backup gap detected! RTB has new snapshot but pCloud backup old
  • Disk space critically low: 4% free

Timestamp: 2026-04-15 14:30:22
Run: ./pcloud_health_check.sh --verbose for details
```

---

## 📊 Alert-Typen

| Status Code | Text | Emoji | Farbe | Bedeutung |
|-------------|------|-------|-------|-----------|
| 0 | OK | ✅ | Grün | Alles funktioniert |
| 1 | WARNING | ⚠️ | Gelb | Degradiert, Aufmerksamkeit empfohlen |
| 2 | CRITICAL | 🚨 | Rot | Sofortiges Handeln erforderlich |
| 3 | UNKNOWN | ❓ | Grau | Check konnte nicht durchgeführt werden |

---

## 🛡️ Sicherheit

### apprise.yml Permissions

```bash
chmod 600 /opt/apps/pcloud-tools/main/apprise.yml
```

Die Datei enthält sensitive Tokens und sollte nur vom Owner lesbar sein.

### Telegram Bot-Token schützen

- Speichere Token **niemals** in öffentlichen Git-Repos
- `apprise.yml` ist in `.gitignore` (nur `.example` wird committed)

---

## 🎯 Multiple Services

Du kannst mehrere Dienste gleichzeitig nutzen:

```yaml
urls:
  # Telegram für sofortige Alerts
  - tgram://BOT_TOKEN/CHAT_ID/:
      tag: telegram, critical
      
  # Discord fürs Team
  - discord://WEBHOOK_ID/WEBHOOK_TOKEN/:
      tag: discord, all
      
  # Email als Backup
  - mailto://user:pass@smtp.gmail.com/:
      to: admin@example.com
      tag: email, critical
```

Filter nach Tags:
```bash
# Nur Telegram
apprise --config=apprise.yml --tag=telegram ...

# Nur für kritische Alerts
apprise --config=apprise.yml --tag=critical ...
```

---

## 🔧 Troubleshooting

### "apprise: command not found"

```bash
# Prüfe Installation
pip3 show apprise

# Füge User-bin zu PATH hinzu
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### "No notification services configured"

Prüfe `apprise.yml`:
```bash
apprise --config=/opt/apps/pcloud-tools/main/apprise.yml --details
```

### "Failed to send notification"

Test einzeln:
```bash
# Telegram
apprise -vv tgram://BOT_TOKEN/CHAT_ID -t "Test" -b "Body"

# Discord
apprise -vv discord://WEBHOOK_ID/TOKEN -t "Test" -b "Body"
```

### Logs prüfen

```bash
# Systemd
sudo journalctl -u pcloud-health-alert.service -f

# Cron
tail -f /var/log/pcloud-alerts.log
```

---

## 📚 Weitere Dienste

Apprise unterstützt 100+ Services. Beispiele:

### Pushover (iOS/Android paid app)
```yaml
- pover://USER_KEY@TOKEN/:
    tag: pushover
```

### Matrix
```yaml
- matrix://USER:PASSWORD@matrix.org/#room_id:
    tag: matrix
```

### Slack
```yaml
- slack://TOKEN_A/TOKEN_B/TOKEN_C/:
    tag: slack
```

Vollständige Liste: https://github.com/caronc/apprise/wiki

---

## 🎉 Fertig!

Du hast jetzt:
- ✅ Apprise installiert
- ✅ Telegram/Discord/ntfy konfiguriert
- ✅ Test-Benachrichtigung erfolgreich
- ✅ Automatische Alerts bei Status-Änderungen

**Next Steps:**
- Phase 2: Aggregator Script (sammelt alle Health-Checks)
- Phase 3: Dashboard (Web-UI für Status-Übersicht)

---

**Tipp**: Teste den Alerting mit:
```bash
# Simuliere CRITICAL Status
BACKUP_AGE_CRITICAL_HOURS=0 ./pcloud_health_check.sh --json > /dev/null
./send_alert.sh --force
```
