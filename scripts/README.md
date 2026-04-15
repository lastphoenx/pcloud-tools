# pCloud-Tools Scripts

Helper scripts for monitoring and alerting.

## Available Scripts

### `send_alert.sh`

Intelligent alerting script with status change detection using Apprise.

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
4. Automate: Add to cron or systemd timer

See [../docs/APPRISE_SETUP.md](../docs/APPRISE_SETUP.md) for detailed setup instructions.

---

## Automation Examples

### Cron (every 5 minutes)

```bash
crontab -e
```

Add:
```cron
*/5 * * * * /opt/apps/pcloud-tools/main/scripts/send_alert.sh >> /var/log/pcloud-alerts.log 2>&1
```

### Systemd Timer

See [APPRISE_SETUP.md](../docs/APPRISE_SETUP.md#-automatisierung) for systemd unit files.

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
