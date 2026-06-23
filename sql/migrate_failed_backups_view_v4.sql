-- v_failed_backups: nur echte Fehler (mit error_message), keine DB-Zombie-Eintraege
--   sudo mysql pcloud_backup < sql/migrate_failed_backups_view_v4.sql

USE pcloud_backup;

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
  AND error_message IS NOT NULL
  AND TRIM(error_message) != ''
ORDER BY started_at DESC;
