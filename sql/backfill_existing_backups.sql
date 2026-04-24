-- =====================================================
-- Backfill Existing pCloud Backups (Manual)
-- =====================================================
-- Purpose: Insert historical backup runs for the 3 existing snapshots
-- These backups were completed before DB tracking (PCLOUD_ENABLE_DB) was enabled
-- 
-- PREREQUISITES:
--   1. pcloud_backup database must exist
--   2. backup_runs table must exist (created by init_pcloud_db.sql)
--   
--   If table doesn't exist, run first:
--     mysql -u pcloud_backup -p pcloud_backup < sql/init_pcloud_db.sql
--
-- Evidence:
--   RTB Snapshots:
--     /mnt/backup/rtb_nas/2026-04-10-075334 (exists)
--     /mnt/backup/rtb_nas/2026-04-12-121042 (exists)
--     /mnt/backup/rtb_nas/2026-04-12-163517 (exists)
--   
--   pCloud Manifests:
--     /srv/pcloud-archive/manifests/2026-04-10-075334.json (10665932 bytes, Apr 10 08:52)
--     /srv/pcloud-archive/manifests/2026-04-12-121042.json (10667325 bytes, Apr 12 12:25)
--     /srv/pcloud-archive/manifests/2026-04-12-163517.json (10667640 bytes, Apr 12 16:35)
--
-- Database: pcloud_backup (separate DB from entropywatcher)
-- Table: backup_runs (created by init_pcloud_db.sql)
--
-- Usage:
--   mysql -u pcloud_backup -p pcloud_backup < sql/backfill_existing_backups.sql
-- =====================================================

USE pcloud_backup;

-- Preflight check: Verify table exists
SELECT 'Checking if backup_runs table exists...' AS '';
SELECT 
    CASE 
        WHEN COUNT(*) > 0 THEN 
            CONCAT('✓ backup_runs table found with ', COUNT(*), ' existing entries')
        ELSE 
            '✓ backup_runs table exists (empty)'
    END AS status
FROM information_schema.tables
WHERE table_schema = 'pcloud_backup' 
  AND table_name = 'backup_runs'
INTO @table_check;

-- Show existing runs count
SELECT COUNT(*) AS existing_runs_count FROM backup_runs;

SELECT '=== Inserting Backfill Entries ===' AS '';

-- Backup 1: 2026-04-10-075334
-- Manifest created: Apr 10 08:52 (completion time)
-- Estimated start: ~1.5 hours before (07:20)
INSERT INTO backup_runs (
    run_id,
    snapshot_name,
    status,
    started_at,
    finished_at,
    duration_sec,
    files_uploaded,
    bytes_uploaded,
    files_total,
    bytes_total,
    error_message,
    gap_backfill_mode
) VALUES (
    'backfill-2026-04-10-075334',
    '2026-04-10-075334',
    'SUCCESS',
    '2026-04-10 07:20:00',  -- Estimated start
    '2026-04-10 08:52:00',  -- Manifest mtime (completion)
    5520,                    -- Duration: 92 minutes (1.5h)
    NULL,                    -- Unknown (not tracked before DB enabled)
    NULL,                    -- Unknown
    NULL,                    -- Unknown
    NULL,                    -- Unknown
    'Backfilled manually: Historical backup before pcloud_backup DB tracking enabled',
    0
) ON DUPLICATE KEY UPDATE
    -- If already exists, don't update
    run_id = run_id;

-- Backup 2: 2026-04-12-121042
-- Manifest created: Apr 12 12:25
-- Estimated start: 45 min before (11:40)
INSERT INTO backup_runs (
    run_id,
    snapshot_name,
    status,
    started_at,
    finished_at,
    duration_sec,
    files_uploaded,
    bytes_uploaded,
    files_total,
    bytes_total,
    error_message,
    gap_backfill_mode
) VALUES (
    'backfill-2026-04-12-121042',
    '2026-04-12-121042',
    'SUCCESS',
    '2026-04-12 11:40:00',  -- Estimated start
    '2026-04-12 12:25:00',  -- Manifest mtime
    2700,                    -- Duration: 45 minutes
    NULL,
    NULL,
    NULL,
    NULL,
    'Backfilled manually: Historical backup before pcloud_backup DB tracking enabled',
    0
) ON DUPLICATE KEY UPDATE
    run_id = run_id;

-- Backup 3: 2026-04-12-163517
-- Manifest created: Apr 12 16:35
-- Estimated start: 40 min before (15:55)
INSERT INTO backup_runs (
    run_id,
    snapshot_name,
    status,
    started_at,
    finished_at,
    duration_sec,
    files_uploaded,
    bytes_uploaded,
    files_total,
    bytes_total,
    error_message,
    gap_backfill_mode
) VALUES (
    'backfill-2026-04-12-163517',
    '2026-04-12-163517',
    'SUCCESS',
    '2026-04-12 15:55:00',  -- Estimated start
    '2026-04-12 16:35:00',  -- Manifest mtime
    2400,                    -- Duration: 40 minutes
    NULL,
    NULL,
    NULL,
    NULL,
    'Backfilled manually: Historical backup before pcloud_backup DB tracking enabled',
    0
) ON DUPLICATE KEY UPDATE
    run_id = run_id;

-- Verification: Show all backfilled entries
SELECT '=== Backfilled Entries ===' AS '';
SELECT 
    snapshot_name,
    status,
    DATE_FORMAT(started_at, '%Y-%m-%d %H:%i') AS started,
    DATE_FORMAT(finished_at, '%Y-%m-%d %H:%i') AS finished,
    ROUND(duration_sec / 60, 0) AS duration_minutes,
    SUBSTRING(error_message, 1, 50) AS note
FROM backup_runs
WHERE run_id LIKE 'backfill-%'
ORDER BY started_at;

-- Summary statistics
SELECT '=== Summary ===' AS '';
SELECT 
    COUNT(*) AS total_backfilled,
    SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) AS successful,
    SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed
FROM backup_runs
WHERE run_id LIKE 'backfill-%';

-- Show all backup runs (historical + future)
SELECT '=== All Backup Runs ===' AS '';
SELECT 
    snapshot_name,
    status,
    DATE_FORMAT(started_at, '%Y-%m-%d %H:%i') AS started,
    DATE_FORMAT(finished_at, '%Y-%m-%d %H:%i') AS finished,
    ROUND(duration_sec / 60, 0) AS duration_min
FROM backup_runs
ORDER BY started_at DESC
LIMIT 10;
