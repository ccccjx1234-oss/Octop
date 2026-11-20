CREATE TABLE feishu_kb_private_index_versions (
  collection_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, user_id INTEGER NOT NULL,
  digest TEXT NOT NULL, config_version TEXT NOT NULL, generation INTEGER NOT NULL,
  ready INTEGER NOT NULL DEFAULT 0
);
UPDATE _schema_version SET version = 33;
