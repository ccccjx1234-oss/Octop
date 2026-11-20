-- SQLite schema runs as a single transaction.
BEGIN IMMEDIATE;
-- Octop control database, migration runner. Adds configurable minute-level sync interval for shared Feishu sources.
-- Rollback: column is additive with default 0; legacy code ignores it and uses schedule ('manual' / 'daily').
ALTER TABLE kb_feishu_sources ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 39;
COMMIT;
