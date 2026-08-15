# Systemd Services & Timers

This directory contains systemd service and timer units for the Monitoring Dashboard.

## 📋 Available Units

### monitoring-status-update.service
Oneshot service that aggregates system status (runs aggregate_status.sh).
- Collects data from systemd services
- Checks RTB wrapper status
- Runs pCloud health checks
- Writes output to `/opt/apps/monitoring/status.json`

### monitoring-status-update.timer
Event-triggered timer with fallback schedule:
- **Triggers after**: backup-pipeline completes (`OnUnitActivation`)
- **Fallback**: Every 15 minutes (full aggregate: RTB --check-only + pCloud health)
- **Boot**: 10 minutes after system startup

### monitoring-status-quick.service / monitoring-status-quick.timer
Lightweight status refresh every **5 minutes** (`AGGREGATE_MODE=quick`):
- Services, RTB log tail, live Safety-Gate, timers — **no** RTB `--check-only` (~1–3 min saved)
- Reuses cached pCloud health from last full run
- Skips if another aggregate is already running (`flock`)

### monitoring-alert.service
Oneshot service that sends notifications on status changes:
- Runs send_aggregated_alert.sh
- Compares current status with previous run
- Sends Telegram/Discord/ntfy alerts only on changes
- Uses Apprise for multi-service notifications

### monitoring-alert.timer
Event-triggered timer for change notifications:
- **Triggers after**: monitoring-status-update.service
- **Fallback**: Every 30 minutes
- **Boot**: 3 minutes after system startup (after status update)

### monitoring-dashboard.service
Persistent web server for the monitoring dashboard:
- **Port**: 8080
- **Protocol**: HTTP
- **Features**: No-cache headers, auto-refresh
- **User**: YOUR_USER (non-root for security)

### backup-pipeline.service / backup-pipeline.timer
RTB staged backup + pCloud pool sync (`rtb_pool_wrapper.sh`).

**Timer:** 04:00, 12:00, 20:00 daily.

**Timeout (15.08.2026):** `TimeoutStartSec=12h` in `backup-pipeline.service.example` (war `4h`).  
Catch-up, große Turbo-Deltas (PBS2-Chunks, viele Stubs) und erster C1-Import können **>4 h** dauern; bei `4h` killt systemd den Push → Snap ohne `.upload_complete`.

```bash
sudo /opt/apps/pcloud-tools/main/scripts/install-backup-pipeline-systemd.sh
systemctl show backup-pipeline.service -p TimeoutUSec
```

## 🚀 Installation

### 0. Setup Telegram Notifications (Required for monitoring-alert)

Before enabling the alert service, configure Telegram notifications:

```bash
# 1. Copy example config
sudo cp /opt/apps/pcloud-tools/main/apprise.yml.example /opt/apps/apprise.yml

# 2. Edit config with your Telegram bot token and chat ID
sudo nano /opt/apps/apprise.yml

# 3. Secure the config file
sudo chown root:root /opt/apps/apprise.yml
sudo chmod 600 /opt/apps/apprise.yml

# 4. Test notifications
/opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh --test
```

**Telegram Bot Setup:**
1. Open Telegram, search for `@BotFather`
2. Send: `/newbot`
3. Follow prompts to create bot and get token (format: `123456789:ABCdefGHI...`)
4. Get your chat_id:
   - Method 1: Search for `@userinfobot` on Telegram, it will show your user ID
   - Method 2: Send a message to your bot, then visit:
     ```
     https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
     ```
5. Add token and chat_id to `/opt/apps/apprise.yml`:
   ```yaml
   urls:
     - tgram://YOUR_BOT_TOKEN/YOUR_CHAT_ID/:
         tag: telegram
   ```

See [apprise.yml.example](../apprise.yml.example) for full configuration options.

### 1. Copy example files

```bash
# Copy service files to systemd directory
sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-update.service.example \
        /etc/systemd/system/monitoring-status-update.service

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-update.timer.example \
        /etc/systemd/system/monitoring-status-update.timer

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-quick.service.example \
        /etc/systemd/system/monitoring-status-quick.service

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-quick.timer.example \
        /etc/systemd/system/monitoring-status-quick.timer

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-reports.service.example \
        /etc/systemd/system/monitoring-reports.service

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-reports.timer.example \
        /etc/systemd/system/monitoring-reports.timer

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-alert.service.example \
        /etc/systemd/system/monitoring-alert.service

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-alert.timer.example \
        /etc/systemd/system/monitoring-alert.timer

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-dashboard.service.example \
        /etc/systemd/system/monitoring-dashboard.service
```

### 2. Customize paths (if needed)

Edit the service files if your installation paths differ:

```bash
sudo nano /etc/systemd/system/monitoring-status-update.service
# Adjust: WorkingDirectory, ExecStart, ReadWritePaths, ReadOnlyPaths

sudo nano /etc/systemd/system/monitoring-dashboard.service
# Adjust: WorkingDirectory, User, Group
```

### 3. Reload systemd

```bash
sudo systemctl daemon-reload
```

### 4. Enable and start services

```bash
# Enable timers (will auto-start on boot)
sudo systemctl enable monitoring-status-quick.timer
sudo systemctl enable monitoring-status-update.timer
sudo systemctl enable monitoring-reports.timer
sudo systemctl enable monitoring-alert.timer

# Start timers immediately
sudo systemctl start monitoring-status-quick.timer
sudo systemctl start monitoring-status-update.timer
sudo systemctl start monitoring-reports.timer
sudo systemctl start monitoring-alert.timer

# Enable dashboard webserver
sudo systemctl enable monitoring-dashboard.service

# Start dashboard webserver
sudo systemctl start monitoring-dashboard.service
```

## 🔍 Verification

### Check timer status

```bash
# List all monitoring timers
systemctl list-timers monitoring-*

# Check timer details
systemctl status monitoring-status-update.timer
```

Expected output:
```
● monitoring-status-update.timer - Monitoring Status Update Timer
     Loaded: loaded (/etc/systemd/system/monitoring-status-update.timer; enabled)
     Active: active (waiting) since ...
    Trigger: Thu 2026-04-16 11:45:00 CEST; 12min left
```

### Check service status

```bash
# Dashboard webserver
systemctl status monitoring-dashboard.service

# Status update (oneshot - may show inactive when not running)
systemctl status monitoring-status-update.service
```

### Test manual run

```bash
# Manually trigger status update
sudo systemctl start monitoring-status-update.service

# Check output
cat /opt/apps/monitoring/status.json | jq .
```

### View logs

```bash
# Dashboard webserver logs
journalctl -u monitoring-dashboard.service -f

# Status update logs
journalctl -u monitoring-status-update.service -n 50

# Timer activation logs
journalctl -u monitoring-status-update.timer -f
```

## 🎯 Event Triggering

**status.json (full):** `monitoring-status-update.timer` mit `OnUnitActivation=backup-pipeline.service` — Full-Aggregate direkt nach Backup-Ende.

**status.json (quick):** `monitoring-status-quick.timer` — alle **5 Minuten** (ohne RTB `--check-only`).

**reports.json:** `backup-pipeline.service` → `OnSuccess=monitoring-reports.service` plus `monitoring-reports.timer` alle **15 Minuten**.

```
backup-pipeline.service completes
  → monitoring-reports.service (reports.json)
  → monitoring-status-update.service (full status.json, via timer OnUnitActivation)
```

**Benefits:**
- Dashboard-Daten nach Backup ohne 15-Min-Wartezeit (reports + full status)
- Quick-Timer hält Header-Datum und Service-Status alle 5 min frisch
- 15-Minuten-Fallback wenn keine Pipeline läuft

## 🛠️ Troubleshooting

### Timer not activating after services

**Check dependencies:**
```bash
systemctl show monitoring-status-update.timer | grep -i after
```

**Verify service completion triggers timer:**
```bash
# Watch timer activation
journalctl -u monitoring-status-update.timer -f
```

### Dashboard not accessible

**Check if port is listening:**
```bash
sudo ss -tulpn | grep :8080
```

**Check for permission issues:**
```bash
# Ensure YOUR_USER can read dashboard files
ls -la /opt/apps/pcloud-tools/main/dashboard/
```

### Status.json not updating

**Check write permissions:**
```bash
ls -la /opt/apps/monitoring/status.json
# Should be writable by root (service runs as root)
```

**Test manual execution:**
```bash
sudo /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh
echo $?  # Should be 0
```

### Service won't start (ConditionPathExists issue)

**Symptom:** Service fails to start with message like:
```
● telegram-commander.service
   Loaded: loaded (/etc/systemd/system/telegram-commander.service; enabled)
   Active: inactive (dead)
Condition: start condition failed at ...
          ConditionPathExists=/etc/pcloud-tools/telegram_commander.conf was not met
```

**Root Cause:** 

Older service files used `ConditionPathExists` to ensure configuration files exist before starting. This was **removed in April 2026** because services now have **fallback mechanisms**:

- **telegram-commander.service**: Falls back to `/opt/apps/apprise.yml` if config missing
- **Other services**: Use sane defaults or skip optional features

**Why was ConditionPathExists removed?**

1. **Breaking Change:** If config file was accidentally deleted/moved, service would silently refuse to start
2. **No Error Visibility:** systemd shows "condition not met" instead of actual error, making debugging harder
3. **Fallback Support:** Services can now use alternative config sources or defaults

**Diagnosis:**

```bash
# Check if old service file still has ConditionPathExists
systemctl cat telegram-commander.service | grep ConditionPathExists

# Check service status (will show "Condition: start condition failed")
systemctl status telegram-commander.service
```

**Solution Option 1: Update Service File (Recommended)**

```bash
# Remove the .example files and reinstall from latest version
cd /opt/apps/pcloud-tools/main
git pull

# Copy updated service file (WITHOUT ConditionPathExists)
sudo cp systemd/telegram-commander.service.example \
        /etc/systemd/system/telegram-commander.service

# Reload and restart
sudo systemctl daemon-reload
sudo systemctl restart telegram-commander.service

# Verify it starts successfully
systemctl status telegram-commander.service
```

**Solution Option 2: Manual Edit (Quick Fix)**

```bash
# Edit service file
sudo nano /etc/systemd/system/telegram-commander.service

# Remove/Comment this line:
# ConditionPathExists=/etc/pcloud-tools/telegram_commander.conf

# Save, then reload
sudo systemctl daemon-reload
sudo systemctl restart telegram-commander.service
```

**Post-Fix Verification:**

```bash
# Service should now start even without config file
systemctl status telegram-commander.service
# Expected: Active: active (running)

# Check logs to see fallback behavior
journalctl -u telegram-commander.service -n 20
# Expected: "Config not found, falling back to apprise.yml"
```

**Architectural Note:**

Services with optional configs should **fail loudly at runtime** (with clear error messages in logs) rather than silently refusing to start at systemd level. This makes troubleshooting easier and allows graceful degradation.

See also: [TELEGRAM_COMMANDER.md Troubleshooting](../docs/TELEGRAM_COMMANDER.md#-troubleshooting) for more details on this breaking change.

## 🔄 Maintenance

### Restart services

```bash
# Restart timer (will reschedule)
sudo systemctl restart monitoring-status-update.timer

# Restart dashboard webserver
sudo systemctl restart monitoring-dashboard.service
```

### Disable services

```bash
# Stop and disable timer
sudo systemctl stop monitoring-status-update.timer
sudo systemctl disable monitoring-status-update.timer

# Stop and disable dashboard
sudo systemctl stop monitoring-dashboard.service
sudo systemctl disable monitoring-dashboard.service
```

### Update service files

```bash
# After editing service files
sudo systemctl daemon-reload
sudo systemctl restart monitoring-status-update.timer
sudo systemctl restart monitoring-dashboard.service
```

### Restore after manual maintenance

After OOM recovery, staged resume, or `--upload-only`, re-enable timers:

```bash
sudo /opt/apps/pcloud-tools/main/scripts/restore-pipeline-services.sh
```

Re-enables: `backup-pipeline.timer`, monitoring timers, `monitoring-dashboard.service`, EntropyWatcher timers.

## 📊 Monitoring

### Dashboard URL

```
http://<your-server-ip>:8080/index.html
```

### Status JSON API

```
http://<your-server-ip>:8080/monitoring/status.json
```

### Timer schedule

```bash
# View next scheduled activations
systemctl list-timers monitoring-*

# Detailed timer info
systemctl show monitoring-status-update.timer
```

## 🔒 Security Notes

- Dashboard service runs as **non-root user** (YOUR_USER)
- Read-only access to monitored directories
- Write access limited to /opt/apps/monitoring
- No new privileges allowed
- Private tmp directory
- Protected system paths

## 📝 Related Documentation

- [Main README](../README.md)
- [Dashboard Documentation](../dashboard/README.md)
- [Scripts Documentation](../scripts/README.md)
