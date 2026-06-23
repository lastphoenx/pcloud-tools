-- =====================================================
-- Migration: snapshot_integrity_checks
-- =====================================================
-- Tracks per-snapshot integrity verification (separate from backup_runs).
-- Apply after init_pcloud_db.sql:
--   mysql -u pcloud_backup -p pcloud_backup < sql/migrate_integrity_checks.sql
-- =====================================================

USE pcloud_backup;

CREATE TABLE IF NOT EXISTS snapshot_integrity_checks (
    check_id CHAR(36) NOT NULL PRIMARY KEY COMMENT 'UUID v4',
    snapshot_name VARCHAR(255) NOT NULL,
    check_type ENUM('post_upload', 'monthly_audit', 'manual') NOT NULL,
    status ENUM('RUNNING', 'OK', 'FAILED') NOT NULL DEFAULT 'RUNNING',
    started_at DATETIME NOT NULL,
    finished_at DATETIME NULL,
    duration_sec INT UNSIGNED NULL,
    issues_count INT UNSIGNED DEFAULT 0 COMMENT 'Total integrity issues found',
    report_path VARCHAR(512) NULL COMMENT 'JSON report under PCLOUD_ARCHIVE_DIR/integrity/',
    error_summary TEXT NULL COMMENT 'Short summary for dashboard',
    backup_run_id CHAR(36) NULL COMMENT 'Optional link to upload run (post_upload)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_snapshot (snapshot_name),
    INDEX idx_type (check_type),
    INDEX idx_status (status),
    INDEX idx_started (started_at),
    INDEX idx_snapshot_type_started (snapshot_name, check_type, started_at),
    FOREIGN KEY (backup_run_id) REFERENCES backup_runs(run_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Per-snapshot integrity checks (manifest vs pool vs stubs)';

-- Latest integrity result per snapshot and check_type
CREATE OR REPLACE VIEW v_snapshot_integrity_latest AS
SELECT ic.*
FROM snapshot_integrity_checks ic
INNER JOIN (
    SELECT snapshot_name, check_type, MAX(started_at) AS max_started
    FROM snapshot_integrity_checks
    WHERE status IN ('OK', 'FAILED')
    GROUP BY snapshot_name, check_type
) latest
  ON ic.snapshot_name = latest.snapshot_name
 AND ic.check_type = latest.check_type
 AND ic.started_at = latest.max_started;

-- Dashboard: one row per snapshot with post_upload + monthly_audit columns
CREATE OR REPLACE VIEW v_snapshot_integrity_status AS
SELECT
    s.snapshot_name,
    pu.status AS post_upload_status,
    pu.started_at AS post_upload_at,
    pu.issues_count AS post_upload_issues,
    pu.error_summary AS post_upload_summary,
    ma.status AS monthly_audit_status,
    ma.started_at AS monthly_audit_at,
    ma.issues_count AS monthly_audit_issues,
    ma.error_summary AS monthly_audit_summary,
    CASE
        WHEN ma.started_at IS NULL THEN 'UNKNOWN'
        WHEN ma.started_at < DATE_SUB(NOW(), INTERVAL 35 DAY) THEN 'STALE'
        WHEN ma.status = 'FAILED' THEN 'FAILED'
        WHEN ma.status = 'OK' THEN 'OK'
        ELSE 'UNKNOWN'
    END AS audit_freshness
FROM (
    SELECT DISTINCT snapshot_name FROM snapshot_integrity_checks
    UNION
    SELECT DISTINCT snapshot_name FROM backup_runs
) s
LEFT JOIN v_snapshot_integrity_latest pu
  ON pu.snapshot_name = s.snapshot_name AND pu.check_type = 'post_upload'
LEFT JOIN v_snapshot_integrity_latest ma
  ON ma.snapshot_name = s.snapshot_name AND ma.check_type = 'monthly_audit'
ORDER BY s.snapshot_name DESC;
