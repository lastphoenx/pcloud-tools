# pCloud-Tools Scripts

Helper scripts for monitoring, alerting, and maintenance.

## 📁 Verzeichnisstruktur

- **`/scripts/`** → Produktions-Scripts (Monitoring & Alerting)
- **`/scripts/utilities/`** → Developer-Tools & Wartung ([→ README](utilities/README.md))
- **`/scripts/archiv/`** → Obsolete Scripts ([→ README](archiv/README.md))
- **`/scripts/testing/`** → Test-Scripts & PoC

## ⚠️ Wichtig: NICHT VERSCHIEBEN

Folgende Scripts werden aktiv von systemd-Services verwendet:
- `aggregate_status.sh` → monitoring-status-update.service
- `send_aggregated_alert.sh` → monitoring-alert.service
- `send_alert.sh` → Legacy Apprise (noch dokumentiert)
- `telegram_commander.py` → telegram-commander.service
- `generate_reports.sh` → monitoring-reports.service
- Config-Dateien: `telegram_commander.conf.example`, `sudoers-telegram-commander.example`

## Production Scripts

### `aggregate_status.sh` ⭐ NEW

Collects monitoring data from all backup and monitoring services into a unified JSON file.

**Monitored Components:**
- **Systemd Services**: entropy-watcher (nas/os), clamav, honeyfile-monitor, cleanup-samba-recycle, backup-pipeline
- **RTB Wrapper**: Parses `/var/log/backup/rtb_wrapper.log` for last run status
- **pCloud Backup**: Uses `pcloud_health_check.sh --json`

**Output:** `/opt/apps/monitoring/status.json` (configurable)

**Usage:**
```bash
# Run aggregation (silent)
./aggregate_status.sh

# Verbose mode (shows progress)
./aggregate_status.sh --verbose

# Custom output location
MONITORING_OUTPUT=/tmp/status.json ./aggregate_status.sh
```

**Exit Codes:**
- `0`: All OK
- `1`: Warnings detected (z.B. failed systemd service, yellow Safety-Gate)
- `2`: Critical issues found (z.B. backup failed, red Safety-Gate)

**Wichtig:** Exit Codes 1 und 2 sind **normale Monitoring-Ergebnisse**, keine Script-Fehler!

Systemd Service muss daher `SuccessExitStatus=0 1 2` setzen:

```ini
[Service]
Type=oneshot
ExecStart=/opt/apps/pcloud-tools/main/scripts/aggregate_status.sh
SuccessExitStatus=0 1 2  # Alle Exit Codes sind "erfolgreich"
```

Ohne diese Zeile zeigt systemd "FAILURE" bei Exit 1/2, obwohl das Script korrekt funktioniert.

**Automation:**
```bash
# Quick: alle 5 min | Full: alle 15 min + nach backup-pipeline — siehe systemd/README.md
sudo systemctl enable --now monitoring-status-quick.timer
sudo systemctl enable --now monitoring-status-update.timer
```

**Modi:** `AGGREGATE_MODE=quick` (Default im quick-Service) überspringt RTB `--check-only` und reused pCloud-Health aus letztem Full-Lauf.

---

### `send_aggregated_alert.sh` ⭐ NEW

Sends push notifications based on aggregated system status (all services combined).

**Features:**
- Uses `aggregate_status.sh` to collect status from all services
- Sends alerts only when overall status changes (OK → WARNING → CRITICAL)
- State tracking in `.aggregated_status_last`
- Supports multi-service notifications (Telegram, Discord, ntfy)

**Usage:**
```bash
# Normal mode (only alerts on status change)
./send_aggregated_alert.sh

# Send test notification with current status
./send_aggregated_alert.sh --test

# Force alert even if status unchanged
./send_aggregated_alert.sh --force
```

**Automation:**
```bash
# Wird automatisch via monitoring-status-update.timer ausgelöst.
# Kein separater Cronjob nötig.
sudo systemctl status monitoring-status-update.timer
```

**Example Alert:**
```
🚨 CRITICAL - System Monitoring (pi-nas)

Overall Status: CRITICAL
Reason: Status changed: OK → CRITICAL

Summary:
  • Failed Services: 2
  • Inactive Services: 1

Timestamp: 2026-04-15 14:30:22

View detailed status:
  cat /opt/apps/monitoring/status.json
```

---

### `telegram_commander.py` ⭐ NEW

**Remote backup control via Telegram bot** with interactive inline keyboard.

**Features:**
- 🎯 **Inline Keyboard** — Buttons instead of typing commands
- 📊 **Status Monitoring** — Check backup status remotely
- 🔄 **Manual Backups** — Trigger backups with Safety-Gate protection
- 📜 **Live Log Viewing** — Read systemd logs without SSH
- 🔓 **Safety-Gate Reset** — Reset RED gates with confirmation

**Commands:**
- `/menu` — Show main menu with buttons
- `/status` — Display system status (RTB, pCloud, services)
- `/backup` — Trigger manual backup (refuses if Safety-Gate is RED)
- `/logs` — View systemd service logs (last 30 lines)
- `/reset_safety_gate` — Reset Safety-Gate to GREEN (with confirmation)
- `/help` — Show available commands

**Interactive Menus:**
```
Main Menu:
├── 📊 Status       → Show system status
├── 🔄 Backup       → Start backup-pipeline
├── 📜 Logs         → View service logs
├── 🔓 Safety-Gate  → Check/reset gate
└── ❓ Help         → Command overview

Logs Menu:
├── backup-pipeline
├── RTB Wrapper
├── pCloud Backup
└── Telegram Commander

Safety-Gate Menu:
├── Status prüfen
└── Reset zu GREEN (requires confirmation)
```

**Setup:**

1. **Create Telegram Bot:**
   ```bash
   # Via @BotFather on Telegram
   /newbot
   # Save the token: 123456789:ABCdef...
   ```

2. **Get Chat ID:**
   ```bash
   # Send /start to your bot, then visit:
   https://api.telegram.org/bot<TOKEN>/getUpdates
   # Find: "chat":{"id":987654321}
   ```

3. **Configure:**
   ```bash
   sudo cp telegram_commander.conf.example /etc/pcloud-tools/telegram_commander.conf
   sudo nano /etc/pcloud-tools/telegram_commander.conf
   ```
   
   ```bash
   BOT_TOKEN="123456789:ABCdef..."
   ALLOWED_CHAT_IDS="987654321"
   ```

4. **Install Service:**
   ```bash
   sudo cp ../systemd/telegram-commander.service.example \
          /etc/systemd/system/telegram-commander.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now telegram-commander.service
   ```

5. **Test:**
   ```bash
   # In Telegram, send to your bot:
   /start
   # You should see the button menu appear!
   ```

**Security:**
- ✅ Whitelist-only (ALLOWED_CHAT_IDS)
- ✅ No open ports (outbound-only API calls)
- ✅ Config file encrypted (600 permissions)
- ✅ Runs as root (for log access & status.json write)

**Permissions:**
```bash
# Service must run as root for:
# - journalctl access (logs)
# - status.json write (Safety-Gate reset)

sudo nano /etc/systemd/system/telegram-commander.service
# Ensure: User=root, Group=root
```

**Monitoring:**
```bash
# Service status
sudo systemctl status telegram-commander.service

# Live logs
sudo journalctl -u telegram-commander.service -f
```

**Full Documentation:** [../docs/TELEGRAM_COMMANDER.md](../docs/TELEGRAM_COMMANDER.md)

---

### `send_alert.sh`

Intelligent alerting script for **pCloud-specific** status with change detection using Apprise.

**Features:**
- Runs health check in JSON mode
- Compares with previous status (stored in `.status_last`)
- Sends push notification only on status changes (no spam!)
- Supports --test mode for testing configuration
- Supports --force mode to send alert regardless of status

**Usage:**
```bash
# Normal mode (only alerts on status change)
./send_alert.sh

# Send test notification
./send_alert.sh --test

# Force alert even if status unchanged
./send_alert.sh --force
```

**Setup:**
1. Install Apprise: `pip3 install --user apprise`
2. Configure: Copy `../apprise.yml.example` to `../apprise.yml` and edit
3. Test: `./send_alert.sh --test`
4. Automate: systemd Timer — siehe [systemd/README.md](../systemd/README.md)

See [../docs/APPRISE_SETUP.md](../docs/APPRISE_SETUP.md) for detailed setup instructions.

---

## Automation

Alle Scripts werden via systemd Timer automatisiert — kein Cronjob nötig:

```bash
sudo systemctl enable --now monitoring-status-update.timer
sudo systemctl enable --now monitoring-dashboard.service
```

Siehe [systemd/README.md](../systemd/README.md) für vollständige Installations-Anleitung.

---

## State Tracking

The `.status_last` file stores the last known status code:
- `-1`: First run (no previous state)
- `0`: OK
- `1`: WARNING
- `2`: CRITICAL
- `3`: UNKNOWN

This prevents "notification fatigue" - you only get alerted when the status **changes**.

---

## Alert Format

Example notification:

```
🚨 CRITICAL - pCloud Backup (pi-nas)

Status: CRITICAL (Code: 2)
Reason: Status changed: OK → CRITICAL

Issues:
  • Backup gap detected! RTB has new snapshot
  • pCloud quota critically low: 150 GB free

Timestamp: 2026-04-15 14:30:22
Run: ./pcloud_health_check.sh --verbose for details
```

---

## Supported Notification Services

Via Apprise (100+ services):
- ✅ **Telegram** (recommended)
- ✅ **Discord**
- ✅ **ntfy.sh**
- Gotify, Pushover, Slack, Matrix, Email, and many more

See [Apprise documentation](https://github.com/caronc/apprise/wiki) for full list.

---

## Virtual Environment Setup

### Development (Local `.venv`)

```bash
# Linux/macOS
./scripts/setup_venv.sh

# Windows
.\scripts\setup_venv.ps1

# Aktivieren
source .venv/bin/activate     # Linux/macOS
.\.venv\Scripts\Activate.ps1   # Windows
```

### Production (Datierte venvs mit Rotation)

```bash
# Erstelle datierte venv mit Rotation (behält letzte 3)
sudo /opt/apps/venv_rotate.sh --keep 3 /opt/apps/pcloud-tools

# Schnell in Projekt wechseln + venv aktivieren
source venv_switch.sh pcloud-tools
```

**Siehe:** [../docs/VENV_MANAGEMENT.md](../docs/VENV_MANAGEMENT.md) für Details.
