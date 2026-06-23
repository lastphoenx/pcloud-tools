-- Einmalige Dashboard-/DB-Bereinigung (pi-nas 2026-06-23)
--
-- 1) "stale RUNNING bereinigt" aus error_message entfernen (Letzte Fehler-Liste)
-- 2) FAILED-Laeufe mit integrity_status=OK -> SUCCESS (Upload war fertig, nur DB-Zombie)
--
-- Vorher: systemctl is-active backup-pipeline.service -> inactive
--
--   sudo mysql pcloud_backup < sql/cleanup_dashboard_oneoff.sql
--   sudo /opt/apps/pcloud-tools/main/scripts/generate_reports.sh
--   sudo systemctl restart monitoring-dashboard.service

USE pcloud_backup;

-- --- 1) Fehlertext bereinigen ---
UPDATE backup_runs
SET error_message = NULLIF(TRIM(BOTH ' | ' FROM
    REPLACE(
        REPLACE(COALESCE(error_message, ''), ' | stale RUNNING bereinigt (Crash/Abbruch)', ''),
        'stale RUNNING bereinigt (Crash/Abbruch)', ''
    )
), '')
WHERE error_message LIKE '%stale RUNNING bereinigt%';

SELECT 'Nach Fehlertext-Bereinigung (FAILED 7d):' AS '';
SELECT snapshot_name, LEFT(COALESCE(error_message, ''), 80) AS error_message
FROM backup_runs
WHERE status = 'FAILED'
  AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
ORDER BY started_at DESC
LIMIT 10;

-- --- 2) Reconcile: Integritaet OK => Upload war erfolgreich ---
UPDATE backup_runs
SET status = 'SUCCESS',
    finished_at = COALESCE(finished_at, integrity_checked_at, started_at),
    error_message = NULL
WHERE status = 'FAILED'
  AND integrity_status = 'OK';

SELECT 'Nach Reconcile (letzte SUCCESS):' AS '';
SELECT snapshot_name, status, integrity_status, finished_at
FROM backup_runs
WHERE status = 'SUCCESS'
ORDER BY finished_at DESC
LIMIT 5;
