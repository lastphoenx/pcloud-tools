-- Migration: Pool-Mode Phasen in backup_phases
-- Ausfuehren auf bestehender pcloud_backup DB (einmalig):
--   sudo mysql pcloud_backup < sql/migrate_pool_phases.sql
--
-- Hintergrund: Pool-Wrapper loggt manifest/upload/verify — kein retention_sync mehr.
-- Retention/GC laeuft separat (pcloud_pool_gc.py); ENUM erweitert fuer kuenftiges Logging.

USE pcloud_backup;

ALTER TABLE backup_phases
  MODIFY COLUMN phase_name ENUM(
    'manifest', 'upload', 'verify',
    'pool_retention', 'pool_gc',
    'folder_creation', 'retention_sync'
  ) NOT NULL;
