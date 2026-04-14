-- =====================================================
-- pCloud Backup Run History Database Schema (SQLite)
-- =====================================================
-- Purpose: Track backup runs, metrics, performance, and errors
-- Usage: sqlite3 /var/lib/pcloud-backup/runs.db < init_pcloud_db.sql
-- =====================================================

-- Drop existing tables (for fresh init)
DROP TABLE IF EXISTS backup_runs;
DROP TABLE IF EXISTS backup_phases;
DROP TABLE IF EXISTS gap_backfills;

-- =====================================================
-- Main backup runs table
-- =====================================================
CREATE TABLE IF NOT EXISTS backup_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,              -- UUID for this run
    
    -- Timestamps
    start_time TIMESTAMP NOT NULL,            -- ISO 8601 format
    end_time TIMESTAMP,                       -- NULL if still running/failed
    duration_seconds INTEGER,                 -- Total runtime
    
    -- Status & Result
    status TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING/SUCCESS/FAILED/PARTIAL
    exit_code INTEGER,                        -- Shell exit code
    error_message TEXT,                       -- Error details if failed
    
    -- Snapshot Info
    snapshot_name TEXT NOT NULL,              -- e.g., 2026-04-14-183517
    snapshot_path TEXT,                       -- /mnt/backup/rtb_nas/...
    
    -- File Metrics
    files_total INTEGER DEFAULT 0,            -- Total files in snapshot
    files_uploaded INTEGER DEFAULT 0,         -- Newly uploaded (not dedup)
    files_stubbed INTEGER DEFAULT 0,          -- Deduplicated (stub JSON)
    files_failed INTEGER DEFAULT 0,           -- Failed uploads
    
    -- Byte Metrics
    bytes_uploaded INTEGER DEFAULT 0,         -- Actual bytes sent to pCloud
    bytes_total INTEGER DEFAULT 0,            -- Total snapshot size
    
    -- Performance Metrics
    manifest_duration_sec INTEGER,            -- Manifest generation time
    folders_duration_sec INTEGER,             -- Folder creation time
    upload_duration_sec INTEGER,              -- File/stub upload time
    verify_duration_sec INTEGER,              -- Delta check time
    
    -- Gap Backfilling
    gaps_detected INTEGER DEFAULT 0,          -- How many missing snapshots found
    gaps_backfilled INTEGER DEFAULT 0,        -- How many successfully uploaded
    
    -- pCloud API Stats
    api_calls_total INTEGER DEFAULT 0,        -- Total API requests
    api_errors INTEGER DEFAULT 0,             -- Failed API calls
    
    -- System Context
    retention_sync BOOLEAN DEFAULT 0,         -- Was retention cleaning triggered?
    bootstrap_mode BOOLEAN DEFAULT 0,         -- Was this a bootstrap (empty pCloud)?
    hostname TEXT,                            -- Which machine ran this
    
    -- Indexes for fast queries
    UNIQUE(run_id)
);

-- Index for common queries
CREATE INDEX idx_backup_runs_start_time ON backup_runs(start_time DESC);
CREATE INDEX idx_backup_runs_status ON backup_runs(status);
CREATE INDEX idx_backup_runs_snapshot ON backup_runs(snapshot_name);

-- =====================================================
-- Backup phases (for detailed timing breakdown)
-- =====================================================
CREATE TABLE IF NOT EXISTS backup_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,                     -- FK to backup_runs.run_id
    
    phase_name TEXT NOT NULL,                 -- manifest/folders/upload/verify/retention
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds INTEGER,
    status TEXT DEFAULT 'RUNNING',            -- RUNNING/SUCCESS/FAILED/SKIPPED
    
    -- Phase-specific metrics (JSON for flexibility)
    metrics TEXT,                             -- JSON: {"folders_created": 1101, ...}
    error_message TEXT,
    
    FOREIGN KEY (run_id) REFERENCES backup_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX idx_backup_phases_run_id ON backup_phases(run_id);

-- =====================================================
-- Gap backfill details (track each missing snapshot)
-- =====================================================
CREATE TABLE IF NOT EXISTS gap_backfills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,                     -- FK to backup_runs.run_id
    
    snapshot_name TEXT NOT NULL,              -- The missing snapshot being backfilled
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds INTEGER,
    status TEXT DEFAULT 'RUNNING',            -- RUNNING/SUCCESS/FAILED
    
    files_uploaded INTEGER DEFAULT 0,
    files_stubbed INTEGER DEFAULT 0,
    bytes_uploaded INTEGER DEFAULT 0,
    
    error_message TEXT,
    
    FOREIGN KEY (run_id) REFERENCES backup_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX idx_gap_backfills_run_id ON gap_backfills(run_id);
CREATE INDEX idx_gap_backfills_snapshot ON gap_backfills(snapshot_name);

-- =====================================================
-- Utility Views for common queries
-- =====================================================

-- Last 10 successful backups
CREATE VIEW IF NOT EXISTS v_recent_backups AS
SELECT 
    start_time,
    snapshot_name,
    duration_seconds,
    files_total,
    files_uploaded,
    files_stubbed,
    gaps_backfilled,
    ROUND(bytes_uploaded / 1024.0 / 1024.0, 2) AS mb_uploaded
FROM backup_runs
WHERE status = 'SUCCESS'
ORDER BY start_time DESC
LIMIT 10;

-- Failure history
CREATE VIEW IF NOT EXISTS v_failed_backups AS
SELECT 
    start_time,
    snapshot_name,
    error_message,
    exit_code
FROM backup_runs
WHERE status = 'FAILED'
ORDER BY start_time DESC;

-- Performance stats (last 30 days)
CREATE VIEW IF NOT EXISTS v_performance_stats AS
SELECT 
    COUNT(*) AS total_runs,
    SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) AS successful,
    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
    ROUND(AVG(duration_seconds), 1) AS avg_duration_sec,
    ROUND(AVG(files_total), 0) AS avg_files,
    ROUND(SUM(bytes_uploaded) / 1024.0 / 1024.0 / 1024.0, 2) AS total_gb_uploaded
FROM backup_runs
WHERE start_time >= datetime('now', '-30 days');

-- =====================================================
-- Sample Queries (for documentation)
-- =====================================================

-- Find all runs that took >1 hour:
-- SELECT start_time, snapshot_name, duration_seconds/60 AS minutes 
-- FROM backup_runs WHERE duration_seconds > 3600;

-- Total data uploaded per day (last 7 days):
-- SELECT DATE(start_time) AS day, SUM(bytes_uploaded)/1024/1024/1024 AS gb
-- FROM backup_runs WHERE start_time >= datetime('now', '-7 days')
-- GROUP BY day ORDER BY day;

-- Gap backfilling history:
-- SELECT b.start_time, g.snapshot_name, g.duration_seconds, g.files_stubbed
-- FROM gap_backfills g JOIN backup_runs b ON g.run_id = b.run_id
-- ORDER BY b.start_time DESC;

-- =====================================================
-- Version Tracking
-- =====================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_version (version) VALUES (1);

-- =====================================================
-- End of Schema
-- =====================================================
