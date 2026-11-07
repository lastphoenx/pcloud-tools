-- =====================================================
-- Migration: RTB / PBS transfer metrics on backup_runs
-- =====================================================
-- Stores per-snapshot rsync stats from .rtb_backup_summary.json
-- Apply: mysql -u pcloud_backup -p pcloud_backup < sql/migrate_rtb_run_metrics.sql
-- =====================================================

USE pcloud_backup;

SET @col := (SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = 'pcloud_backup' AND table_name = 'backup_runs'
      AND column_name = 'rtb_bytes_transferred');
SET @sql := IF(@col = 0,
    'ALTER TABLE backup_runs
       ADD COLUMN rtb_bytes_transferred BIGINT UNSIGNED NOT NULL DEFAULT 0
         COMMENT ''Total rsync bytes (all RTB units)'' AFTER bytes_uploaded,
       ADD COLUMN rtb_files_transferred INT UNSIGNED NOT NULL DEFAULT 0 AFTER rtb_bytes_transferred,
       ADD COLUMN rtb_units_changed INT UNSIGNED NOT NULL DEFAULT 0 AFTER rtb_files_transferred,
       ADD COLUMN pbs_bytes_transferred BIGINT UNSIGNED NOT NULL DEFAULT 0
         COMMENT ''Bytes under Backup/pbs2/* units'' AFTER rtb_units_changed',
    'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
