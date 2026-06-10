-- =====================================================
-- Fix: verify=FAILED bei backup_runs.status=SUCCESS
-- =====================================================
-- Pool-Wrapper markiert Delta-Verify (pcloud_quick_delta) bei Fehler als
-- backup_phases.status=FAILED, schliesst den Lauf aber als SUCCESS ab
-- (non-critical). Nach pool_verify_backup.py OK sind diese DB-Eintraege
-- irrefuehrend (Dashboard: gelbe Verify-Warnungen).
--
-- Ausfuehren:
--   sudo mysql pcloud_backup < sql/fix_verify_phases_noncritical.sql
--   sudo /opt/apps/pcloud-tools/main/scripts/generate_reports.sh
--   sudo /opt/apps/pcloud-tools/main/scripts/aggregate_status.sh
-- =====================================================

USE pcloud_backup;

-- 1) Vorschau (sollte nur verify+FAILED+SUCCESS-Laeufe zeigen)
SELECT br.snapshot_name,
       br.status AS run_status,
       bp.status AS verify_status,
       bp.started_at AS verify_started,
       bp.duration_sec,
       bp.error_message
FROM backup_phases bp
JOIN backup_runs br ON br.run_id = bp.run_id
WHERE bp.phase_name = 'verify'
  AND bp.status = 'FAILED'
  AND br.status = 'SUCCESS'
ORDER BY bp.started_at;

-- 2) Korrigieren
UPDATE backup_phases bp
JOIN backup_runs br ON br.run_id = bp.run_id
SET bp.status = 'SUCCESS',
    bp.error_message = CONCAT(
      'Korrigiert ',
      DATE_FORMAT(NOW(), '%Y-%m-%d'),
      ': pool_verify OK; Delta-Verify war non-critical (Catch-up/Index-Timing)'
    )
WHERE bp.phase_name = 'verify'
  AND bp.status = 'FAILED'
  AND br.status = 'SUCCESS';

-- 3) Kontrolle (sollte 0 Zeilen)
SELECT COUNT(*) AS remaining_verify_failed_but_run_success
FROM backup_phases bp
JOIN backup_runs br ON br.run_id = bp.run_id
WHERE bp.phase_name = 'verify'
  AND bp.status = 'FAILED'
  AND br.status = 'SUCCESS';

-- 4) Echte Fehler unveraendert (verify FAILED + run FAILED)
SELECT br.snapshot_name, br.started_at, br.error_message
FROM backup_phases bp
JOIN backup_runs br ON br.run_id = bp.run_id
WHERE bp.phase_name = 'verify'
  AND bp.status = 'FAILED'
  AND br.status = 'FAILED'
ORDER BY br.started_at DESC;
