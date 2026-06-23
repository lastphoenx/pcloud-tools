-- =====================================================
-- Migration v2: Integrity on backup_runs (not append-only log)
-- =====================================================
-- post_upload / manual  -> backup_runs.integrity_* (am Upload-Lauf)
-- monthly_audit         -> max. 1 Zeile pro Snapshot (UPSERT)
--
-- Apply: sudo mysql pcloud_backup < sql/migrate_integrity_v2.sql
-- =====================================================

USE pcloud_backup;

-- 1) Integrity am Upload-Lauf
SET @col := (SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = 'pcloud_backup' AND table_name = 'backup_runs'
      AND column_name = 'integrity_status');
SET @sql := IF(@col = 0,
    'ALTER TABLE backup_runs
       ADD COLUMN integrity_status ENUM(''OK'', ''FAILED'', ''SKIPPED'') NULL
         COMMENT ''Post-upload Integritaetscheck'' AFTER status,
       ADD COLUMN integrity_issues_count INT UNSIGNED NULL AFTER integrity_status,
       ADD COLUMN integrity_checked_at DATETIME NULL AFTER integrity_issues_count,
       ADD COLUMN integrity_report_path VARCHAR(512) NULL AFTER integrity_checked_at',
    'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2) Manuelle Historie -> letzter SUCCESS backup_run (vor Bereinigung)
UPDATE backup_runs br
INNER JOIN (
    SELECT ic.snapshot_name, ic.status, ic.issues_count, ic.finished_at, ic.report_path
    FROM snapshot_integrity_checks ic
    INNER JOIN (
        SELECT snapshot_name, MAX(finished_at) AS max_fin
        FROM snapshot_integrity_checks
        WHERE check_type = 'manual' AND finished_at IS NOT NULL
        GROUP BY snapshot_name
    ) latest ON ic.snapshot_name = latest.snapshot_name
            AND ic.finished_at = latest.max_fin
    WHERE ic.check_type = 'manual'
) mig ON br.snapshot_name = mig.snapshot_name
INNER JOIN (
    SELECT snapshot_name, MAX(started_at) AS max_started
    FROM backup_runs WHERE status = 'SUCCESS'
    GROUP BY snapshot_name
) lr ON br.snapshot_name = lr.snapshot_name AND br.started_at = lr.max_started
SET br.integrity_status = mig.status,
    br.integrity_issues_count = mig.issues_count,
    br.integrity_checked_at = mig.finished_at,
    br.integrity_report_path = mig.report_path
WHERE br.integrity_status IS NULL;

-- 3) Alte Log-Zeilen entfernen (post_upload/manual nicht mehr in dieser Tabelle)
DELETE FROM snapshot_integrity_checks WHERE check_type IN ('post_upload', 'manual');

-- 4) Duplikate monthly_audit bereinigen
DELETE sic FROM snapshot_integrity_checks sic
INNER JOIN (
    SELECT snapshot_name, check_type, MAX(started_at) AS max_started
    FROM snapshot_integrity_checks
    GROUP BY snapshot_name, check_type
) keep ON sic.snapshot_name = keep.snapshot_name
       AND sic.check_type = keep.check_type
       AND sic.started_at < keep.max_started;

-- 5) UNIQUE: ein Eintrag pro Snapshot + check_type
SET @uq := (SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = 'pcloud_backup'
      AND table_name = 'snapshot_integrity_checks'
      AND index_name = 'uq_snapshot_check_type');
SET @sql_uq := IF(@uq = 0,
    'ALTER TABLE snapshot_integrity_checks ADD UNIQUE KEY uq_snapshot_check_type (snapshot_name, check_type)',
    'SELECT 1');
PREPARE stmt FROM @sql_uq; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 6) Views
CREATE OR REPLACE VIEW v_snapshot_integrity_latest AS
SELECT * FROM snapshot_integrity_checks WHERE check_type = 'monthly_audit';

CREATE OR REPLACE VIEW v_snapshot_integrity_status AS
SELECT
    s.snapshot_name,
    br.status AS backup_status,
    br.started_at AS backup_at,
    br.integrity_status AS post_upload_status,
    br.integrity_checked_at AS post_upload_at,
    COALESCE(br.integrity_issues_count, 0) AS post_upload_issues,
    br.integrity_report_path AS post_upload_report,
    ma.status AS monthly_audit_status,
    ma.finished_at AS monthly_audit_at,
    COALESCE(ma.issues_count, 0) AS monthly_audit_issues,
    ma.error_summary AS monthly_audit_summary,
    ma.report_path AS monthly_audit_report,
    CASE
        WHEN ma.finished_at IS NULL THEN 'UNKNOWN'
        WHEN ma.finished_at < DATE_SUB(NOW(), INTERVAL 35 DAY) THEN 'STALE'
        WHEN ma.status = 'FAILED' THEN 'FAILED'
        WHEN ma.status = 'OK' THEN 'OK'
        ELSE 'UNKNOWN'
    END AS audit_freshness
FROM (
    SELECT DISTINCT snapshot_name FROM backup_runs
    UNION
    SELECT DISTINCT snapshot_name FROM snapshot_integrity_checks
) s
LEFT JOIN backup_runs br ON br.snapshot_name = s.snapshot_name
    AND br.started_at = (
        SELECT MAX(br2.started_at) FROM backup_runs br2
        WHERE br2.snapshot_name = s.snapshot_name AND br2.status = 'SUCCESS'
    )
LEFT JOIN snapshot_integrity_checks ma
    ON ma.snapshot_name = s.snapshot_name AND ma.check_type = 'monthly_audit'
ORDER BY s.snapshot_name DESC;

CREATE OR REPLACE VIEW v_snapshot_dashboard AS
SELECT
    snapshot_name,
    backup_status,
    backup_at,
    post_upload_status AS integrity_status,
    post_upload_at AS integrity_at,
    post_upload_issues AS integrity_issues,
    monthly_audit_status,
    monthly_audit_at,
    audit_freshness
FROM v_snapshot_integrity_status;
