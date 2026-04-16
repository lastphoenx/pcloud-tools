# Telegram Notification Setup Guide

Complete guide for setting up Telegram notifications for the Monitoring Dashboard.

## 📱 Overview

The monitoring system uses **Apprise** to send notifications to Telegram (and other services). Alerts are sent when:
- Overall system status changes (OK → WARNING → CRITICAL)
- You manually trigger with `--force` flag
- You run test mode with `--test` flag

## 🤖 Create Telegram Bot

### Step 1: Open BotFather

1. Open Telegram on your phone or desktop
2. Search for: `@BotFather`
3. Start a chat with BotFather

### Step 2: Create New Bot

Send this command to BotFather:
```
/newbot
```

BotFather will ask for:
1. **Bot name**: Choose a friendly name (e.g., "Pi NAS Monitor")
2. **Bot username**: Must be unique and end with `bot` (e.g., "pi_nas_monitor_bot")

### Step 3: Save Bot Token

BotFather will reply with your bot token:
```
Done! Congratulations on your new bot. You will find it at t.me/pi_nas_monitor_bot.

Use this token to access the HTTP API:
123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567

Keep your token secure and store it safely...
```

**Save this token!** You'll need it in the config file.

## 👤 Get Your Chat ID

You need your Chat ID so the bot knows where to send messages.

### Method 1: Using @userinfobot (Easiest)

1. Search for `@userinfobot` on Telegram
2. Start a chat with it
3. It will immediately reply with your user ID:
   ```
   Id: 987654321
   First name: Thomas
   Username: @yourname
   ```
4. Your Chat ID is the `Id` value: `987654321`

### Method 2: Using getUpdates API

1. Search for your bot on Telegram (search for the username you created)
2. Start a chat and send any message (e.g., "Hello")
3. Open this URL in your browser (replace `<YOUR_BOT_TOKEN>` with your actual token):
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
4. Look for `"chat":{"id":987654321` in the JSON response
5. Your Chat ID is the `id` value: `987654321`

### Method 3: Group Chat (Optional)

To send notifications to a group:

1. Create a Telegram group
2. Add your bot to the group (use the bot username)
3. Send a message in the group
4. Visit: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
5. Look for `"chat":{"id":-1234567890` (negative number for groups)
6. Your Chat ID is: `-1234567890`

## ⚙️ Configure Apprise

### Step 1: Copy Example Config

```bash
sudo cp /opt/apps/pcloud-tools/main/apprise.yml.example /opt/apps/apprise.yml
```

### Step 2: Edit Config

```bash
sudo nano /opt/apps/apprise.yml
```

### Step 3: Add Telegram Credentials

Replace the placeholder values:

```yaml
urls:
  # Telegram (Personal or Group)
  - tgram://YOUR_BOT_TOKEN/YOUR_CHAT_ID/:
      tag: telegram
      format: markdown
```

**Example (with fake values):**
```yaml
urls:
  # Telegram
  - tgram://123456789:ABCdefGHIjklMNOpqrsTUVwxyz/987654321/:
      tag: telegram
      format: markdown
```

### Step 4: Secure Config File

```bash
# Set ownership to root
sudo chown root:root /opt/apps/apprise.yml

# Set permissions (only root can read/write)
sudo chmod 600 /opt/apps/apprise.yml

# Verify
ls -la /opt/apps/apprise.yml
# Should show: -rw------- 1 root root
```

## ✅ Test Notifications

### Test 1: Aggregated Alert Test

```bash
/opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh --test
```

**Expected output:**
```
Using config: /opt/apps/apprise.yml
Running aggregator to get current status...
Sending test notification to all configured services...
  → Sending to: telegram
Test notifications sent!
```

**Check Telegram** - you should receive:
```
🧪 Test Alert - System Monitoring (pi-nas)

Current System Status: ✅ OK

This is a test notification showing your current backup/monitoring status.
View full details at: /opt/apps/monitoring/status.json

Run 'aggregate_status.sh --verbose' for detailed output.
```

### Test 2: pCloud-Specific Alert Test

```bash
/opt/apps/pcloud-tools/main/scripts/send_alert.sh --test
```

### Test 3: Force Alert (Real Status)

```bash
/opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh --force
```

## 🔧 Configure Multiple Services (Optional)

You can send notifications to multiple services simultaneously:

```yaml
urls:
  # Telegram
  - tgram://123456789:ABCdefGHI/987654321/:
      tag: telegram
      format: markdown
  
  # Discord (optional)
  - discord://WEBHOOK_ID/WEBHOOK_TOKEN/:
      tag: discord
      username: Backup Monitor
  
  # ntfy.sh (optional)
  - ntfy://pi-nas-alerts/:
      tag: ntfy
```

## 📊 Alert Examples

### ✅ Success Alert (OK)
```
✅ OK - System Monitoring (pi-nas)

Overall Status: OK
Reason: Status changed: WARNING → OK

Summary:
  • Failed Services: 0
  • Inactive Services: 0

Timestamp: 2026-04-16 14:30:00
```

### ⚠️ Warning Alert
```
⚠️ WARNING - System Monitoring (pi-nas)

Overall Status: WARNING
Reason: Status changed: OK → WARNING

Summary:
  • Failed Services: 0
  • Inactive Services: 1

Timestamp: 2026-04-16 14:35:00
```

### 🚨 Critical Alert
```
🚨 CRITICAL - System Monitoring (pi-nas)

Overall Status: CRITICAL
Reason: Status changed: WARNING → CRITICAL

Summary:
  • Failed Services: 2
  • Inactive Services: 0

Timestamp: 2026-04-16 14:40:00
```

## 🔍 Troubleshooting

### "No Apprise config found"

**Solution:**
```bash
# Check if file exists
ls -la /opt/apps/apprise.yml

# If not, copy example
sudo cp /opt/apps/pcloud-tools/main/apprise.yml.example /opt/apps/apprise.yml
```

### "apprise is not installed"

**Solution:**
```bash
# Install apprise
sudo apt update
sudo apt install python3-apprise

# Or install via pip
pip3 install apprise
```

### Notifications not received

**Check 1: Verify config syntax**
```bash
# Test with verbose output
apprise --config=/opt/apps/apprise.yml \
        --tag=telegram \
        --title="Test" \
        --body="Test message" \
        --verbose
```

**Check 2: Verify bot token**
```bash
# Test API access (replace YOUR_BOT_TOKEN)
curl https://api.telegram.org/botYOUR_BOT_TOKEN/getMe
```

Expected response:
```json
{"ok":true,"result":{"id":123456789,"is_bot":true,"first_name":"Pi NAS Monitor"}}
```

**Check 3: Verify chat ID**
- Did you start a chat with your bot? (send at least one message)
- Is the chat_id correct? (positive for users, negative for groups)

**Check 4: Check logs**
```bash
# Check alert service logs
journalctl -u monitoring-alert.service -n 50

# Check script execution
sudo /opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh --test
```

### Wrong status reported

**Check status.json:**
```bash
cat /opt/apps/monitoring/status.json | jq .
```

**Run aggregator manually:**
```bash
/opt/apps/pcloud-tools/main/scripts/aggregate_status.sh --verbose
```

## 📝 Customization

### Change Notification Frequency

Edit timer: `/etc/systemd/system/monitoring-alert.timer`

```ini
[Timer]
# Change from 30min to 1 hour
OnCalendar=*:0/60
```

Then reload:
```bash
sudo systemctl daemon-reload
sudo systemctl restart monitoring-alert.timer
```

### Change Alert Tags

Edit: `/opt/apps/pcloud-tools/main/scripts/send_aggregated_alert.sh`

```bash
# Line 27: Add or remove tags
NOTIFICATION_TAGS=("telegram" "discord" "ntfy")
```

### Disable Specific Services

Remove or comment out services in `/opt/apps/apprise.yml`:

```yaml
urls:
  # Telegram - enabled
  - tgram://123456789:ABCdefGHI/987654321/:
      tag: telegram
  
  # Discord - disabled (commented out)
  # - discord://WEBHOOK_ID/TOKEN/:
  #     tag: discord
```

## 🔗 Related Documentation

- [Apprise GitHub](https://github.com/caronc/apprise)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [systemd README](../systemd/README.md)
- [Scripts README](../scripts/README.md)
