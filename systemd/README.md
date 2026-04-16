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
- **Triggers after**: entropywatcher-nas, entropywatcher-nas-av, backup-pipeline
- **Fallback**: Every 15 minutes
- **Boot**: 2 minutes after system startup

### monitoring-dashboard.service
Persistent web server for the monitoring dashboard:
- **Port**: 8080
- **Protocol**: HTTP
- **Features**: No-cache headers, auto-refresh
- **User**: thomas (non-root for security)

## 🚀 Installation

### 1. Copy example files

```bash
# Copy service files to systemd directory
sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-update.service.example \
        /etc/systemd/system/monitoring-status-update.service

sudo cp /opt/apps/pcloud-tools/main/systemd/monitoring-status-update.timer.example \
        /etc/systemd/system/monitoring-status-update.timer

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
# Enable timer (will auto-start on boot)
sudo systemctl enable monitoring-status-update.timer

# Start timer immediately
sudo systemctl start monitoring-status-update.timer

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

The timer uses `OnUnitActivation` to run **immediately after** monitored services complete:

```
entropywatcher-nas.service completes → monitoring-status-update.service runs
backup-pipeline.service completes    → monitoring-status-update.service runs
```

**Benefits:**
- Dashboard shows fresh data seconds after key operations
- Reduced overhead (no polling)
- 15-minute fallback ensures regular updates even if services don't run

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
# Ensure thomas user can read dashboard files
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

- Dashboard service runs as **non-root user** (thomas)
- Read-only access to monitored directories
- Write access limited to /opt/apps/monitoring
- No new privileges allowed
- Private tmp directory
- Protected system paths

## 📝 Related Documentation

- [Main README](../README.md)
- [Dashboard Documentation](../dashboard/README.md)
- [Scripts Documentation](../scripts/README.md)
