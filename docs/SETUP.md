# pCloud Backup Tools - Complete Setup Guide

> **Target Audience:** This guide is for setting up pCloud-Tools on a Raspberry Pi 5 (or similar Debian-based system) with existing RTB (rsync-time-backup) snapshots.

---

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [MariaDB Installation & Setup](#mariadb-installation--setup)
3. [pCloud-Tools Installation](#pcloud-tools-installation)
4. [Configuration (.env)](#configuration-env)
5. [Database Initialization](#database-initialization)
6. [First Backup Test](#first-backup-test)
7. [Health Check Setup](#health-check-setup)
8. [Automation (Cron/Systemd)](#automation-cronsystemd)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### System Requirements
- Debian/Ubuntu-based Linux (Raspberry Pi OS 11+)
- Python 3.9+
- Bash 4.0+
- 200GB+ free space for temporary uploads (SSD recommended)
- Internet connection with stable bandwidth

### Existing Setup (Required)
- RTB snapshots in `/mnt/backup/rtb_nas/` (or similar)
- pCloud account with 2TB+ storage
- pCloud OAuth2 token ([get one here](https://docs.pcloud.com/))

### Install Dependencies

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install MariaDB server
sudo apt install -y mariadb-server mariadb-client

# Install Python dependencies
sudo apt install -y python3 python3-pip python3-venv

# Install utilities
sudo apt install -y curl jq uuid-runtime

# Optional: Install logrotate (if not already present)
sudo apt install -y logrotate
```

---

## MariaDB Installation & Setup

### 1. Secure MariaDB Installation

```bash
sudo mysql_secure_installation
```

**Prompts:**
- `Enter current password for root:` → Press Enter (no password yet)
- `Set root password? [Y/n]` → **Y**, then enter strong password
- `Remove anonymous users? [Y/n]` → **Y**
- `Disallow root login remotely? [Y/n]` → **Y**
- `Remove test database? [Y/n]` → **Y**
- `Reload privilege tables? [Y/n]` → **Y**

### 2. Create Database and User

```bash
# Login as root
sudo mysql -u root -p
```

**Execute in MySQL prompt:**

```sql
-- Create database for pCloud backup tracking
CREATE DATABASE IF NOT EXISTS pcloud_backup CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Create dedicated user (replace PASSWORD with strong password!)
CREATE USER 'pcloud_backup'@'localhost' IDENTIFIED BY 'YOUR_STRONG_PASSWORD_HERE';

-- Grant all privileges on pcloud_backup database
GRANT ALL PRIVILEGES ON pcloud_backup.* TO 'pcloud_backup'@'localhost';

-- Apply changes
FLUSH PRIVILEGES;

-- Verify database exists
SHOW DATABASES;

-- Exit
EXIT;
```

**Security Note:** Store the password securely - you'll need it for `.env` configuration.

### 3. Test Database Connection

```bash
# Test login with new user
mysql -u pcloud_backup -p pcloud_backup

# Should prompt for password, then show MySQL prompt
# Type: EXIT;
```

---

## pCloud-Tools Installation

### 1. Clone Repository

```bash
# Choose installation directory
sudo mkdir -p /opt/apps/pcloud-tools
cd /opt/apps/pcloud-tools

# Clone repo
sudo git clone https://github.com/YOUR_USERNAME/pcloud-tools.git main

# Set ownership (replace 'thomas' with your username)
sudo chown -R thomas:thomas /opt/apps/pcloud-tools/main

# Enter directory
cd main
```

### 2. Create Virtual Environment (Optional, for Python scripts)

```bash
# Create venv
python3 -m venv venv

# Activate
source venv/bin/activate

# Install Python dependencies (if requirements.txt exists)
pip install -r requirements.txt
```

### 3. Set Script Permissions

```bash
chmod +x wrapper_pcloud_sync_1to1.sh
chmod +x pcloud_status.sh
chmod +x pcloud_health_check.sh
```

---

## Configuration (.env)

### 1. Copy Example Config

```bash
cp .env.example .env
```

### 2. Edit Configuration

```bash
nano .env
```

### 3. Required Settings

**Minimum configuration to edit:**

```bash
#########################################
# pCloud API Configuration (REQUIRED)
#########################################
PCLOUD_TOKEN=YOUR_PCLOUD_ACCESS_TOKEN_HERE

# API region (eapi.pcloud.com for EU, api.pcloud.com for US)
PCLOUD_HOST="eapi.pcloud.com"

# Remote folder ID in pCloud (0 = root, or specific folder ID)
PCLOUD_DEFAULT_FOLDERID="0"

#########################################
# Paths (REQUIRED)
#########################################
# RTB snapshot source directory
RTB_SNAPSHOT_DIR=/mnt/backup/rtb_nas

# Temporary upload directory (MUST be on fast SSD!)
PCLOUD_TEMP_DIR=/srv/pcloud-temp

# Archive directory for successful manifests
PCLOUD_ARCHIVE_DIR=/srv/pcloud-archive

#########################################
# MariaDB Configuration (REQUIRED for tracking)
#########################################
PCLOUD_DB_HOST=localhost
PCLOUD_DB_PORT=3306
PCLOUD_DB_NAME=pcloud_backup
PCLOUD_DB_USER=pcloud_backup
PCLOUD_DB_PASS=YOUR_STRONG_PASSWORD_HERE  # ← From step 2.2

# Enable database tracking (0=disabled, 1=enabled)
PCLOUD_ENABLE_DB=1

#########################################
# Logging (OPTIONAL)
#########################################
PCLOUD_LOG=/var/log/backup/pcloud_sync.log

# Enable JSON Lines logging (for log aggregation)
PCLOUD_ENABLE_JSONL=0
PCLOUD_JSONL_LOG=/var/log/backup/pcloud_sync.jsonl

#########################################
# Health Check Thresholds (OPTIONAL)
#########################################
# Alert if last backup older than X hours
BACKUP_AGE_WARNING_HOURS=48
BACKUP_AGE_CRITICAL_HOURS=72

# Alert if pCloud quota below X GB
QUOTA_WARNING_GB=500   # 5% of 10TB
QUOTA_CRITICAL_GB=200  # 2% of 10TB

# Alert if disk space below X percent
DISK_WARNING_PERCENT=10
```

### 4. Create Required Directories

```bash
# Create log directory
sudo mkdir -p /var/log/backup

# Create temp directory (ensure it's on SSD!)
sudo mkdir -p /srv/pcloud-temp

# Create archive directory
sudo mkdir -p /srv/pcloud-archive

# Set permissions (replace 'thomas' with your username)
sudo chown -R thomas:thomas /srv/pcloud-temp /srv/pcloud-archive
sudo chown -R thomas:thomas /var/log/backup
```

---

## Database Initialization

### 1. Import Schema

```bash
cd /opt/apps/pcloud-tools/main

# Initialize database schema
mysql -u pcloud_backup -p pcloud_backup < sql/init_pcloud_db.sql
```

**Enter password when prompted** (the one from step 2.2)

### 2. Verify Schema

```bash
# Login to database
mysql -u pcloud_backup -p pcloud_backup

# Check tables
SHOW TABLES;
```

**Expected output:**
```
+-------------------------+
| Tables_in_pcloud_backup |
+-------------------------+
| backup_phases           |
| backup_runs             |
| gap_backfills           |
| v_failed_backups        |
| v_performance_stats     |
| v_recent_backups        |
+-------------------------+
```

```sql
-- Check table structure
DESCRIBE backup_runs;

-- Exit
EXIT;
```

### 3. Test Database Connection (via script)

```bash
# Source .env
source .env

# Test pcloud_status.sh
./pcloud_status.sh --stats
```

**Expected output:**
```
pCloud Backup Statistics (Last 30 Days)
========================================

  Total Runs: 0
  Successful: 0
  Failed: 0
  Success Rate: N/A

  Average Duration: 0.00 minutes
  Total Data: 0.00 GB
  Average per Run: 0.00 GB
  Gap Backfills: 0
```

If you see this, **database connection is working!** ✅

---

## First Backup Test

### 1. Identify Latest RTB Snapshot

```bash
# List snapshots
ls -lah /mnt/backup/rtb_nas/

# Should show directories like:
# 2026-04-14__22-00-01/
# 2026-04-13__22-00-01/
# ...
```

### 2. Run First Backup (Dry-Run Recommended)

```bash
cd /opt/apps/pcloud-tools/main

# Activate venv if using Python scripts
source venv/bin/activate

# Dry-run mode (no actual upload)
# NOTE: Check if wrapper has --dry-run flag, if not, skip this

# Actual backup (replace with your latest snapshot)
./wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/2026-04-14__22-00-01 /Backups/NAS
```

**What happens:**
1. Script generates manifest of local files
2. Queries pCloud for existing files (delta detection)
3. Creates folder structure in pCloud
4. Uploads only new/changed files
5. Logs run to database (if `PCLOUD_ENABLE_DB=1`)

### 3. Monitor Progress

**Open second terminal:**

```bash
# Watch logs in real-time
tail -f /var/log/backup/pcloud_sync.log
```

**Check database (third terminal):**

```bash
./pcloud_status.sh --current
```

### 4. Verify After Completion

```bash
# Check backup status
./pcloud_status.sh recent

# Expected output:
# Run ID: abc-123-def-456
#   Snapshot: 2026-04-14__22-00-01
#   Status: SUCCESS
#   Started: 2026-04-14 22:05:00
#   Finished: 2026-04-14 23:15:00
#   Duration: 1h 10m 0s
#   Files: 45678
#   Bytes: 125.3 GB
```

---

## Health Check Setup

### 1. Test Health Check Manually

```bash
cd /opt/apps/pcloud-tools/main

# Run health check (verbose)
./pcloud_health_check.sh --verbose
```

**Expected output:**
```
=== pCloud Backup Health Check ===

[1] Backup Age & Gap Detection
  Latest RTB snapshot: 2026-04-14__22-00-01 (2h ago)
  Latest pCloud backup: 2026-04-14__22-00-01 (2h ago)
  ✓ OK: Backup age healthy (2h ago)

[2] pCloud Quota
  Total: 10240 GB | Used: 150 GB | Free: 10090 GB
  ✓ OK: pCloud quota healthy: 10090 GB free

[3] Disk Space (/srv/pcloud-temp)
  Usage: 5% | Available: 1.9T
  ✓ OK: Disk space healthy: 95% free (1.9T available)

[4] Database Connectivity
  ✓ OK: Database connection healthy

========================================
✓ Status: HEALTHY
========================================
```

### 2. Check Exit Code

```bash
./pcloud_health_check.sh
echo $?  # Should be 0 (healthy)
```

---

## Automation (Cron/Systemd)

### Option A: Cron (Simple)

```bash
# Edit crontab
crontab -e
```

**Add entries:**

```cron
# pCloud Backup: Run every day at 23:00 (after RTB completes)
0 23 * * * /opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/latest /Backups/NAS >> /var/log/backup/pcloud_cron.log 2>&1

# Health Check: Every 15 minutes
*/15 * * * * /opt/apps/pcloud-tools/main/pcloud_health_check.sh || logger -t pcloud_health "Health check failed: exit code $?"
```

### Option B: Systemd Timer (Advanced)

**Create service file:**

```bash
sudo nano /etc/systemd/system/pcloud-backup.service
```

```ini
[Unit]
Description=pCloud Backup Upload
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
Type=oneshot
User=thomas
WorkingDirectory=/opt/apps/pcloud-tools/main
ExecStart=/opt/apps/pcloud-tools/main/wrapper_pcloud_sync_1to1.sh /mnt/backup/rtb_nas/latest /Backups/NAS
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pcloud-backup

[Install]
WantedBy=multi-user.target
```

**Create timer file:**

```bash
sudo nano /etc/systemd/system/pcloud-backup.timer
```

```ini
[Unit]
Description=pCloud Backup Timer
Requires=pcloud-backup.service

[Timer]
OnCalendar=daily
OnCalendar=23:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Enable and start:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable pcloud-backup.timer
sudo systemctl start pcloud-backup.timer

# Check status
sudo systemctl status pcloud-backup.timer
```

---

## Troubleshooting

### Database Connection Failed

```bash
# Check if MariaDB is running
sudo systemctl status mariadb

# Test connection manually
mysql -u pcloud_backup -p pcloud_backup

# Check credentials in .env
grep PCLOUD_DB_ .env
```

**Fix:** Verify password, user, and database name in `.env`

---

### Permission Denied Errors

```bash
# Fix script permissions
chmod +x /opt/apps/pcloud-tools/main/*.sh

# Fix directory permissions
sudo chown -R thomas:thomas /srv/pcloud-temp /srv/pcloud-archive /var/log/backup
```

---

### pCloud API Errors

```bash
# Test token manually
curl -s "https://eapi.pcloud.com/userinfo?auth=YOUR_TOKEN" | jq .

# Expected: JSON with quota, email, userid
# If error: Token expired or invalid - get new one from pCloud dashboard
```

---

### Disk Space Full

```bash
# Check disk usage
df -h /srv

# Clean old temp files (safe - only if no backup running!)
find /srv/pcloud-temp -type f -mtime +7 -delete

# Check archive folder size
du -sh /srv/pcloud-archive
```

**Config:** Reduce `RETENTION_COUNT` in `.env` to keep fewer snapshots

---

### Health Check Always Reports Critical

```bash
# Run verbose to see exact issue
./pcloud_health_check.sh --verbose

# Adjust thresholds in .env if needed:
BACKUP_AGE_WARNING_HOURS=96   # Increase if RTB runs infrequently
BACKUP_AGE_CRITICAL_HOURS=168
```

---

### Database Schema Mismatch (after updates)

```bash
# Check current schema version
mysql -u pcloud_backup -p pcloud_backup -e "DESCRIBE backup_runs;"

# If columns missing: Re-import schema (safe - uses CREATE IF NOT EXISTS)
mysql -u pcloud_backup -p pcloud_backup < sql/init_pcloud_db.sql
```

---

## Next Steps

- 📊 **Monitoring Dashboard:** Generate HTML: `./pcloud_status.sh html /var/www/html/pcloud.html`
- 🔔 **Alerting:** Set up Telegram/Discord webhooks (see docs/ALERTING.md - coming soon)
- 📈 **Grafana Integration:** Export metrics to Prometheus (see docs/METRICS.md - coming soon)
- 🔐 **Encrypted Backups:** Use pCloud Crypto folders for sensitive data

---

## Support

- **Issues:** https://github.com/YOUR_USERNAME/pcloud-tools/issues
- **Documentation:** https://github.com/YOUR_USERNAME/pcloud-tools/docs
- **Changelog:** https://github.com/YOUR_USERNAME/pcloud-tools/blob/main/CHANGELOG.md

---

**Last Updated:** April 14, 2026  
**Version:** 1.0.0 (MariaDB Edition)
