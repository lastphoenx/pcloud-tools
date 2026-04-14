-- =====================================================
-- pCloud Backup Run History - MariaDB Schema
-- =====================================================
-- Purpose: Track backup runs, phases, gaps, and metrics
-- Database: Separate pcloud_backup schema (standalone mode)
-- Usage:
--   1. CREATE DATABASE pcloud_backup;
--   2. CREATE USER 'pcloud_backup'@'localhost' IDENTIFIED BY 'PASSWORD';
--   3. GRANT ALL ON pcloud_backup.* TO 'pcloud_backup'@'localhost';
--   4. mysql -u pcloud_backup -p pcloud_backup < init_pcloud_db.sql
-- =====================================================

CREATE DATABASE IF NOT EXISTS pcloud_backup CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE pcloud_backup;

-- =====================================================
-- TABLE: backup_runs
-- =====================================================
-- Tracks individual backup runs (one per snapshot upload)
CREATE TABLE IF NOT EXISTS backup_runs (
    run_id CHAR(36) NOT NULL PRIMARY KEY COMMENT 'UUID v4',
    snapshot_name VARCHAR(255) NOT NULL COMMENT 'RTB snapshot identifier (e.g., 2026-04-14__22-00-01)',
    
    status ENUM('RUNNING', 'SUCCESS', 'FAILED') NOT NULL DEFAULT 'RUNNING',
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    duration_sec INT UNSIGNED NULL COMMENT 'Total run duration in seconds',
    
    files_uploaded INT UNSIGNED DEFAULT 0,
    bytes_uploaded BIGINT UNSIGNED DEFAULT 0,
    files_total INT UNSIGNED DEFAULT 0,
    bytes_total BIGINT UNSIGNED DEFAULT 0,
    
    error_message TEXT NULL,
    gap_backfill_mode TINYINT(1) DEFAULT 0 COMMENT '1 if this run backfilled gaps',
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_status (status),
    INDEX idx_started (started_at),
    INDEX idx_snapshot (snapshot_name),
    INDEX idx_duration (duration_sec)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Backup run tracking';

-- =====================================================
-- TABLE: backup_phases
-- =====================================================
-- Tracks individual phases within a backup run (manifest, upload, verify)
CREATE TABLE IF NOT EXISTS backup_phases (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    
    phase_name ENUM('manifest', 'folder_creation', 'upload', 'verify', 'retention_sync') NOT NULL,
    status ENUM('RUNNING', 'SUCCESS', 'FAILED') NOT NULL DEFAULT 'RUNNING',
    
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    duration_sec INT UNSIGNED NULL,
    
    files_processed INT UNSIGNED DEFAULT 0,
    bytes_processed BIGINT UNSIGNED DEFAULT 0,
    
    error_message TEXT NULL,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (run_id) REFERENCES backup_runs(run_id) ON DELETE CASCADE,
    INDEX idx_run_phase (run_id, phase_name),
    INDEX idx_status (status),
    INDEX idx_duration (duration_sec)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Phase-level tracking';

-- =====================================================
-- TABLE: gap_backfills
-- =====================================================
-- Tracks gaps backfilled (missing snapshots uploaded via intelligent loop)
CREATE TABLE IF NOT EXISTS gap_backfills (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    run_id CHAR(36) NOT NULL,
    
    gap_snapshot_name VARCHAR(255) NOT NULL COMMENT 'Snapshot that was missing',
    backfilled_at DATETIME NOT NULL,
    files_uploaded INT UNSIGNED DEFAULT 0,
    bytes_uploaded BIGINT UNSIGNED DEFAULT 0,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (run_id) REFERENCES backup_runs(run_id) ON DELETE CASCADE,
    INDEX idx_run (run_id),
    INDEX idx_snapshot (gap_snapshot_name),
    INDEX idx_backfilled (backfilled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Gap backfilling history';

-- =====================================================
-- VIEWS: Analytics and Dashboards
-- =====================================================

-- Recent backups (last 30 days)
CREATE OR REPLACE VIEW v_recent_backups AS
SELECT 
    run_id,
    snapshot_name,
    status,
    started_at,
    finished_at,
    duration_sec,
    files_uploaded,
    ROUND(bytes_uploaded / 1024 / 1024 / 1024, 2) AS gb_uploaded,
    gap_backfill_mode,
    error_message
FROM backup_runs
WHERE started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
ORDER BY started_at DESC;

-- Failed backups (last 7 days)
CREATE OR REPLACE VIEW v_failed_backups AS
SELECT 
    run_id,
    snapshot_name,
    started_at,
    finished_at,
    duration_sec,
    error_message
FROM backup_runs
WHERE status = 'FAILED'
  AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
ORDER BY started_at DESC;

-- Performance statistics (last 30 days)
CREATE OR REPLACE VIEW v_performance_stats AS
SELECT 
    COUNT(*) AS total_runs,
    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful_runs,
    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_runs,
    ROUND(AVG(duration_sec) / 60, 2) AS avg_duration_min,
    ROUND(SUM(bytes_uploaded) / 1024 / 1024 / 1024, 2) AS total_gb_uploaded,
    ROUND(AVG(bytes_uploaded) / 1024 / 1024 / 1024, 2) AS avg_gb_per_run,
    SUM(gap_backfill_mode) AS gap_backfill_count
FROM backup_runs
WHERE started_at >= DATE_SUB(NOW(), INTERVAL 30 DAY);

-- =====================================================
-- INITIAL DATA
-- =====================================================
-- (None - tables start empty)

-- =====================================================
-- PERMISSIONS (Reminder)
-- =====================================================
-- Run manually after sourcing this file:
-- GRANT SELECT, INSERT, UPDATE, DELETE ON pcloud_backup.* TO 'pcloud_backup'@'localhost';
-- FLUSH PRIVILEGES;

