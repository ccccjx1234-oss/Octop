ALTER TABLE feishu_kb_source ADD COLUMN retrieval_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE feishu_kb_source ADD COLUMN retrieval_generation INTEGER NOT NULL DEFAULT 0;
CREATE TABLE feishu_kb_private_index (
  source_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL,
  digest TEXT, config_version TEXT, dirty INTEGER NOT NULL DEFAULT 1
);
UPDATE _schema_version SET version = 32;
