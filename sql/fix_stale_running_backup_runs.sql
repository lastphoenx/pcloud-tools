-- Bereinigt backup_runs-Zombies (status=RUNNING ohne laufenden Prozess).
-- Vorher prüfen: systemctl is-active backup-pipeline.service → inactive
--
--   sudo mysql pcloud_backup < sql/fix_stale_running_backup_runs.sql

USE pcloud_backup;

UPDATE backup_runs
SET status = 'FAILED',
    finished_at = COALESCE(finished_at, NOW()),
    error_message = CONCAT(
        COALESCE(error_message, ''),
        IF(COALESCE(error_message, '') = '', '', ' | '),
        'stale RUNNING bereinigt (Crash/Abbruch)'
    )
WHERE status = 'RUNNING';

SELECT status, COUNT(*) AS cnt FROM backup_runs GROUP BY status;
