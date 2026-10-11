-- OPTIONAL — löscht FAILED-Zeilen auch wenn irgendwann SUCCESS existiert.
-- Verliert Forensik (Retry-Historie). Vorher mysqldump empfohlen.
--
-- Preview: mysql pcloud_backup < sql/maintenance_db_cleanup_failed_only_preview.sql
-- Nur wenn bewusst gewünscht:
--   mysqldump pcloud_backup backup_runs backup_phases > /tmp/backup_runs_before_superseded.sql
--   mysql pcloud_backup < sql/maintenance_db_cleanup_failed_superseded_optional.sql

USE pcloud_backup;

START TRANSACTION;

SELECT '=== DELETE (optional): FAILED obwohl SUCCESS existiert ===' AS '';

SELECT br.run_id, br.snapshot_name, br.started_at, LEFT(COALESCE(br.error_message,''),80) AS err
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  )
ORDER BY br.snapshot_name, br.started_at;

DELETE br
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  );

SELECT ROW_COUNT() AS deleted_superseded_failed;

COMMIT;

SELECT status, COUNT(*) AS cnt FROM backup_runs GROUP BY status;
