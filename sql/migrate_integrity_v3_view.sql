-- View-Fix: Integritaet auch anzeigen wenn nur RUNNING/FAILED-Lauf integrity hat
--   sudo mysql pcloud_backup < sql/migrate_integrity_v3_view.sql

USE pcloud_backup;

CREATE OR REPLACE VIEW v_snapshot_integrity_status AS
SELECT
    s.snapshot_name,
    br_succ.status AS backup_status,
    br_succ.started_at AS backup_at,
    COALESCE(br_int.integrity_status, br_succ.integrity_status) AS post_upload_status,
    COALESCE(br_int.integrity_checked_at, br_succ.integrity_checked_at) AS post_upload_at,
    COALESCE(br_int.integrity_issues_count, br_succ.integrity_issues_count, 0) AS post_upload_issues,
    COALESCE(br_int.integrity_report_path, br_succ.integrity_report_path) AS post_upload_report,
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
LEFT JOIN backup_runs br_succ ON br_succ.snapshot_name = s.snapshot_name
    AND br_succ.started_at = (
        SELECT MAX(br2.started_at) FROM backup_runs br2
        WHERE br2.snapshot_name = s.snapshot_name AND br2.status = 'SUCCESS'
    )
LEFT JOIN backup_runs br_int ON br_int.snapshot_name = s.snapshot_name
    AND br_int.integrity_checked_at = (
        SELECT MAX(br3.integrity_checked_at) FROM backup_runs br3
        WHERE br3.snapshot_name = s.snapshot_name AND br3.integrity_checked_at IS NOT NULL
    )
LEFT JOIN snapshot_integrity_checks ma
    ON ma.snapshot_name = s.snapshot_name AND ma.check_type = 'monthly_audit'
ORDER BY s.snapshot_name DESC;

CREATE OR REPLACE VIEW v_snapshot_dashboard AS
SELECT
    snapshot_name, backup_status, backup_at,
    post_upload_status AS integrity_status,
    post_upload_at AS integrity_at,
    post_upload_issues AS integrity_issues,
    monthly_audit_status, monthly_audit_at, audit_freshness
FROM v_snapshot_integrity_status;
