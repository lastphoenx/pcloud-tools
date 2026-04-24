# pCloud Backup Database Scripts

## Overview

These scripts manage the **pcloud_backup** database, which tracks backup runs, phases, and metrics for the pCloud backup pipeline.

**Important**: The `pcloud_backup` database is **separate** from the `entropywatcher` database. They serve different purposes and should not be merged.

---

## Database Architecture

- **Database**: `pcloud_backup` (standalone)
- **User**: `pcloud_backup` (dedicated user with limited permissions)
- **Tables**:
  - `backup_runs`: Main run tracking (one row per backup execution)
  - `backup_phases`: Phase-level tracking (manifest, upload, verify, etc.)
  - `gap_backfills`: Tracks backfilled gaps (missing snapshots)
- **Views**: Analytics views for dashboards and monitoring

---

## Initial Setup (First Time)

### 1. Create Database and User

```bash
# Connect as root/admin
mysql -u root -p

# Create database
CREATE DATABASE IF NOT EXISTS pcloud_backup CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

# Create user (replace PASSWORD with actual password)
CREATE USER IF NOT EXISTS 'pcloud_backup'@'localhost' IDENTIFIED BY 'YOUR_SECURE_PASSWORD';

# Grant permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON pcloud_backup.* TO 'pcloud_backup'@'localhost';
FLUSH PRIVILEGES;

EXIT;
```

### 2. Create Tables and Views

```bash
# Run schema initialization (idempotent - safe to run multiple times)
mysql -u pcloud_backup -p pcloud_backup < /opt/apps/pcloud-tools/main/sql/init_pcloud_db.sql
```

**Expected output**:
- Creates 3 tables: `backup_runs`, `backup_phases`, `gap_backfills`
- Creates 3 views: `v_recent_backups`, `v_failed_backups`, `v_performance_stats`

### 3. Configure Wrapper to Use Database

Edit `/opt/apps/pcloud-tools/main/.env`:

```bash
# Enable database tracking
PCLOUD_ENABLE_DB=1

# Database credentials
PCLOUD_DB_HOST=localhost
PCLOUD_DB_PORT=3306
PCLOUD_DB_NAME=pcloud_backup
PCLOUD_DB_USER=pcloud_backup
PCLOUD_DB_PASS=YOUR_SECURE_PASSWORD
```

### 4. Backfill Historical Data (Optional)

If you have existing backups before DB tracking was enabled:

```bash
mysql -u pcloud_backup -p pcloud_backup < /opt/apps/pcloud-tools/main/sql/backfill_existing_backups.sql
```

**Note**: Edit `backfill_existing_backups.sql` to match your actual backup snapshots and dates.

---

## Scripts Reference

### `init_pcloud_db.sql`

**Purpose**: Initialize database schema (tables and views)  
**Idempotent**: ✅ Yes (safe to run multiple times)  
**Usage**:
```bash
mysql -u pcloud_backup -p pcloud_backup < sql/init_pcloud_db.sql
```

**Creates**:
- Tables with `CREATE TABLE IF NOT EXISTS`
- Views with `CREATE OR REPLACE VIEW`
- Foreign key constraints
- Indexes for performance

### `backfill_existing_backups.sql`

**Purpose**: Insert historical backup runs that occurred before DB tracking was enabled  
**Prerequisites**: Requires `backup_runs` table to exist (run `init_pcloud_db.sql` first)  
**Idempotent**: ✅ Yes (uses `ON DUPLICATE KEY UPDATE`)  
**Usage**:
```bash
mysql -u pcloud_backup -p pcloud_backup < sql/backfill_existing_backups.sql
```

**Verification**:
The script outputs a summary showing:
- Number of backfilled entries
- Status of each backup
- Duration statistics

---

## Verification Queries

### Check if DB Tracking is Working

```sql
USE pcloud_backup;

-- Show recent backup runs
SELECT 
    snapshot_name,
    status,
    started_at,
    finished_at,
    ROUND(duration_sec / 60, 1) AS duration_min
FROM backup_runs
ORDER BY started_at DESC
LIMIT 10;

-- Show phase breakdown for last run
SELECT 
    br.snapshot_name,
    bp.phase_name,
    bp.status,
    bp.started_at,
    bp.finished_at,
    bp.duration_sec
FROM backup_phases bp
JOIN backup_runs br ON bp.run_id = br.run_id
WHERE br.run_id = (SELECT run_id FROM backup_runs ORDER BY started_at DESC LIMIT 1)
ORDER BY bp.started_at;
```

### Check Table Structure

```sql
USE pcloud_backup;

-- List all tables
SHOW TABLES;

-- Show backup_runs schema
DESCRIBE backup_runs;

-- Show backup_phases schema
DESCRIBE backup_phases;
```

---

## Integration with Monitoring

The `pcloud_health_check.sh` script queries this database to:
- Determine last successful backup age
- Detect backup gaps (RTB snapshot exists but pCloud backup is old)
- Display backup statistics in dashboard

Example query used by health check:

```sql
SELECT 
    snapshot_name,
    TIMESTAMPDIFF(HOUR, finished_at, NOW()) AS age_hours
FROM backup_runs
WHERE status = 'SUCCESS'
ORDER BY finished_at DESC
LIMIT 1;
```

---

## Troubleshooting

### Error: Table 'pcloud_backup.backup_runs' doesn't exist

**Solution**: Run `init_pcloud_db.sql` first:
```bash
mysql -u pcloud_backup -p pcloud_backup < sql/init_pcloud_db.sql
```

### Error: Access denied for user 'pcloud_backup'

**Solution**: Check credentials in `.env` file and verify user permissions:
```sql
SHOW GRANTS FOR 'pcloud_backup'@'localhost';
```

### Wrapper not writing to database

**Check**:
1. `PCLOUD_ENABLE_DB=1` in `.env`
2. Database credentials correct
3. Connection test: `mysql -u pcloud_backup -p -e "USE pcloud_backup; SELECT 1;"`

### Backfill script fails with duplicate key error

**Explanation**: Entries already exist (idempotent protection working)  
**Solution**: This is expected behavior. Check existing entries:
```sql
SELECT * FROM backup_runs WHERE run_id LIKE 'backfill-%';
```

---

## Maintenance

### Clean Old Entries (>90 days)

```sql
USE pcloud_backup;

-- Preview what would be deleted
SELECT COUNT(*) AS old_runs
FROM backup_runs
WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);

-- Delete old runs (cascade deletes phases and backfills)
DELETE FROM backup_runs
WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);
```

### Optimize Tables

```sql
USE pcloud_backup;
OPTIMIZE TABLE backup_runs;
OPTIMIZE TABLE backup_phases;
OPTIMIZE TABLE gap_backfills;
```

---

## Schema Updates

If you need to add columns or modify the schema:

1. **DON'T** delete existing tables
2. **DO** use `ALTER TABLE ADD COLUMN IF NOT EXISTS` (MariaDB 10.0+)
3. **TEST** on a copy of the database first
4. **UPDATE** `init_pcloud_db.sql` with new schema

Example safe column addition:
```sql
-- Add new column if it doesn't exist
SET @col_exists = (
    SELECT COUNT(*) 
    FROM information_schema.COLUMNS 
    WHERE TABLE_SCHEMA = 'pcloud_backup' 
      AND TABLE_NAME = 'backup_runs' 
      AND COLUMN_NAME = 'new_column'
);

SET @sql = IF(@col_exists = 0, 
    'ALTER TABLE backup_runs ADD COLUMN new_column VARCHAR(255) NULL', 
    'SELECT "Column already exists" AS notice'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
```

---

## Related Documentation

- **Wrapper Script**: `wrapper_pcloud_sync_1to1.sh` (database integration code)
- **Health Check**: `pcloud_health_check.sh` (queries this database)
- **Dashboard**: `dashboard/index.html` (displays backup statistics)
- **Main README**: `../README.md` (overall project documentation)
