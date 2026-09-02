-- Periodische DB-/Dashboard-Bereinigung (pcloud_backup)
--
-- Vorher: backup-pipeline darf nicht laufen
--   systemctl is-active backup-pipeline.service   # -> inactive
--
-- Ausfuehrung:
--   sudo mysql pcloud_backup < sql/maintenance_db_cleanup.sql
--   sudo /opt/apps/pcloud-tools/main/scripts/generate_reports.sh
--   sudo systemctl restart monitoring-dashboard.service
--
-- Oder: scripts/maintenance_db_cleanup.sh

USE pcloud_backup;

SELECT '=== PREVIEW ===' AS '';

SELECT 'RUNNING (soll 0 sein wenn Pipeline inactive)' AS '';
SELECT run_id, snapshot_name, started_at
FROM backup_runs
WHERE status = 'RUNNING'
ORDER BY started_at DESC
LIMIT 20;

SELECT 'RUNNING mit vorhandenem SUCCESS (werden geloescht, nicht zu FAILED)' AS '';
SELECT br_run.run_id, br_run.snapshot_name, br_run.started_at AS running_at, br_ok.started_at AS success_at
FROM backup_runs br_run
INNER JOIN backup_runs br_ok
  ON br_ok.snapshot_name = br_run.snapshot_name
  AND br_ok.status = 'SUCCESS'
WHERE br_run.status = 'RUNNING'
ORDER BY br_run.started_at DESC
LIMIT 20;

SELECT 'FAILED gesamt / letzte 7d' AS '';
SELECT
  SUM(status = 'FAILED') AS failed_total,
  SUM(status = 'FAILED' AND started_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)) AS failed_7d
FROM backup_runs;

SELECT 'FAILED mit spaeterem SUCCESS (werden entfernt)' AS '';
SELECT br_old.run_id, br_old.snapshot_name, br_old.started_at AS failed_at, br_ok.started_at AS success_at
FROM backup_runs br_old
INNER JOIN backup_runs br_ok
  ON br_ok.snapshot_name = br_old.snapshot_name
  AND br_ok.status = 'SUCCESS'
  AND br_ok.started_at > br_old.started_at
WHERE br_old.status = 'FAILED'
ORDER BY br_old.started_at DESC
LIMIT 50;

SELECT 'FAILED + integrity_status=OK (werden SUCCESS)' AS '';
SELECT run_id, snapshot_name, started_at, integrity_status
FROM backup_runs
WHERE status = 'FAILED'
  AND integrity_status = 'OK'
ORDER BY started_at DESC
LIMIT 20;

SELECT 'Runs aelter als 90 Tage (optional purge)' AS '';
SELECT COUNT(*) AS runs_older_90d
FROM backup_runs
WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);

-- =====================================================
-- 1) RUNNING-Zombies (Crash/Abbruch)
--     a) Snapshot hat bereits SUCCESS → Zeile loeschen.
--        Sonst wird RUNNING zu einem NEUEREN FAILED als SUCCESS
--        und bleibt im Dashboard (7d) + Audit „letzter Lauf = FAILED“.
--     b) Nie SUCCESS → FAILED (echter Abbruch).
-- =====================================================
DELETE br_run
FROM backup_runs br_run
INNER JOIN backup_runs br_ok
  ON br_ok.snapshot_name = br_run.snapshot_name
  AND br_ok.status = 'SUCCESS'
WHERE br_run.status = 'RUNNING';

UPDATE backup_runs
SET status = 'FAILED',
    finished_at = COALESCE(finished_at, NOW()),
    error_message = CONCAT(
        COALESCE(NULLIF(TRIM(error_message), ''), 'upload_aborted'),
        ' | stale RUNNING bereinigt (Crash/Abbruch)'
    )
WHERE status = 'RUNNING';

-- FAILED der neuer ist als SUCCESS (alte Cleanup-Artefakte / Abbruch nach Erfolg)
DELETE br_fail
FROM backup_runs br_fail
INNER JOIN backup_runs br_ok
  ON br_ok.snapshot_name = br_fail.snapshot_name
  AND br_ok.status = 'SUCCESS'
  AND br_ok.started_at < br_fail.started_at
WHERE br_fail.status = 'FAILED'
  AND (
    br_fail.error_message LIKE '%stale RUNNING bereinigt%'
    OR br_fail.error_message LIKE '%upload_aborted%'
  );

-- =====================================================
-- 2) Artefakte aus alter Bereinigung aus error_message
-- =====================================================
UPDATE backup_runs
SET error_message = NULLIF(TRIM(BOTH ' | ' FROM
    REPLACE(
        REPLACE(COALESCE(error_message, ''), ' | stale RUNNING bereinigt (Crash/Abbruch)', ''),
        'stale RUNNING bereinigt (Crash/Abbruch)', ''
    )
), '')
WHERE error_message LIKE '%stale RUNNING bereinigt%';

-- =====================================================
-- 3) Upload war OK, nur DB-Zombie
-- =====================================================
UPDATE backup_runs
SET status = 'SUCCESS',
    finished_at = COALESCE(finished_at, integrity_checked_at, started_at),
    error_message = NULL
WHERE status = 'FAILED'
  AND integrity_status = 'OK';

-- =====================================================
-- 4) Alte Fehlversuche: spaeterer SUCCESS fuer gleichen Snapshot
--    (Phasen werden per CASCADE mitgeloescht)
-- =====================================================
DELETE br_old
FROM backup_runs br_old
INNER JOIN backup_runs br_ok
  ON br_ok.snapshot_name = br_old.snapshot_name
  AND br_ok.status = 'SUCCESS'
  AND br_ok.started_at > br_old.started_at
WHERE br_old.status = 'FAILED';

-- =====================================================
-- 5) Views aktualisieren (7d-Filter + keine superseded Fehler)
-- =====================================================
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

-- =====================================================
-- 6) OPTIONAL: Historie >90 Tage loeschen (auskommentiert)
-- =====================================================
-- DELETE FROM backup_runs
-- WHERE started_at < DATE_SUB(NOW(), INTERVAL 90 DAY);

SELECT '=== NACH BEREINIGUNG ===' AS '';

SELECT status, COUNT(*) AS cnt
FROM backup_runs
GROUP BY status;

SELECT 'Letzte Fehler (7d, Dashboard)' AS '';
SELECT snapshot_name, started_at, LEFT(COALESCE(error_message, ''), 80) AS error_message
FROM v_failed_backups
ORDER BY started_at DESC
LIMIT 10;
