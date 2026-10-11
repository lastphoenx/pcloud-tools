-- FAILED-Zeilen für Snapshots, die NIE einen SUCCESS-Lauf hatten (Kosmetik / Audit).
--
-- Voraussetzung: backup-pipeline.service inactive
--   systemctl is-active backup-pipeline.service
--
-- Ausführung auf pi-nas (Pipeline inactive):
--   mysql pcloud_backup < sql/maintenance_db_cleanup_failed_only_preview.sql   # nur lesen
--   mysqldump pcloud_backup backup_runs backup_phases > /tmp/backup_runs_$(date +%F).sql
--   mysql pcloud_backup < sql/maintenance_db_cleanup_failed_only.sql
--
-- FAILED trotz SUCCESS (Forensik weg): nur bewusst —
--   sql/maintenance_db_cleanup_failed_superseded_optional.sql

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

SELECT '=== PREVIEW: alle FAILED-Runs ohne je SUCCESS (werden gelöscht) ===' AS '';

SELECT br.run_id, br.snapshot_name, br.started_at, LEFT(COALESCE(br.error_message, ''), 120) AS err
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  )
ORDER BY br.snapshot_name, br.started_at;

SELECT COUNT(*) AS rows_to_delete
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  );

-- =====================================================
-- DELETE (Phasen-CASCADE löscht backup_phases mit)
-- =====================================================
DELETE br
FROM backup_runs br
WHERE br.status = 'FAILED'
  AND NOT EXISTS (
    SELECT 1 FROM backup_runs o
    WHERE o.snapshot_name = br.snapshot_name AND o.status = 'SUCCESS'
  );

SELECT '=== NACH DELETE (nur nie-SUCCESS FAILED) ===' AS '';

SELECT status, COUNT(*) AS cnt FROM backup_runs GROUP BY status;

SELECT 'Snapshots mit letztem Lauf FAILED (soll leer oder nur echte Offene sein)' AS '';
SELECT br.snapshot_name, br.started_at, br.error_message
FROM backup_runs br
INNER JOIN (
  SELECT snapshot_name, MAX(started_at) AS mx FROM backup_runs GROUP BY snapshot_name
) t ON br.snapshot_name = t.snapshot_name AND br.started_at = t.mx
WHERE br.status = 'FAILED'
ORDER BY br.started_at DESC
LIMIT 20;
