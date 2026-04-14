-- =====================================================
-- pCloud Backup Run History - Migration Framework
-- =====================================================
-- Purpose: Version-aware migrations for schema upgrades
-- Usage: ./sql/migrate.sh (auto-detects version, applies missing migrations)
-- =====================================================

-- Schema version tracking table (created first!)
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

-- Migration v1 → v1 (initial schema, safe idempotent version)
-- Only runs if schema_version table is empty OR version 1 doesn't exist

-- Check current version (will be 0 if fresh DB)
-- Insert base version if this is first run
INSERT OR IGNORE INTO schema_version (version, description) 
VALUES (0, 'Base schema (pre-migration framework)');
