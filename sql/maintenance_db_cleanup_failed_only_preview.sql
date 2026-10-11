-- PREVIEW ONLY — keine DELETEs.
-- Anwenden: mysql pcloud_backup < sql/maintenance_db_cleanup_failed_only_preview.sql
-- Löschen (nie SUCCESS): sql/maintenance_db_cleanup_failed_only.sql

USE pcloud_backup;

SELECT '=== PREVIEW: letzter Lauf = FAILED, nie SUCCESS für diesen Namen ===' AS '';

SELECT br.snapshot_name, br.started_at, br.error_message
FROM backup_runs br
INNER JOIN (
  SELECT snapshot_name, MAX(started_at) AS mx
  FROM backup_runs
  GROUP BY snapshot_name
) t ON br.snapshot_name = t.snapshot_name AND br.started_at = t.mx
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  )
ORDER BY br.snapshot_name;

SELECT '=== PREVIEW: alle FAILED-Runs ohne je SUCCESS (Ziel von failed_only.sql) ===' AS '';

SELECT br.run_id, br.snapshot_name, br.started_at, LEFT(COALESCE(br.error_message, ''), 120) AS err
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  )
ORDER BY br.snapshot_name, br.started_at;

SELECT COUNT(*) AS rows_to_delete_failed_only
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  );

SELECT '=== PREVIEW: FAILED trotz SUCCESS (nur optional, siehe failed_superseded_optional.sql) ===' AS '';

SELECT br.run_id, br.snapshot_name, br.started_at, LEFT(COALESCE(br.error_message,''),80) AS err
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  )
ORDER BY br.snapshot_name, br.started_at;
