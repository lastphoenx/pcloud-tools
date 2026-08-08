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
FROM backup_runs br
WHERE status = 'FAILED'
  AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
  AND error_message IS NOT NULL
  AND TRIM(error_message) != ''
  AND NOT EXISTS (
      SELECT 1
      FROM backup_runs br_ok
      WHERE br_ok.snapshot_name = br.snapshot_name
        AND br_ok.status = 'SUCCESS'
        AND br_ok.started_at > br.started_at
  )
ORDER BY started_at DESC;
