-- Private staging only; these records never enter the global knowledge index.
CREATE TABLE feishu_kb_state (
  state_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL REFERENCES sso_providers(id) ON DELETE CASCADE,
  session_hash TEXT NOT NULL, verifier_blob BLOB NOT NULL, redirect_uri TEXT NOT NULL,
  subject TEXT NOT NULL, expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE feishu_kb_grant (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL REFERENCES sso_providers(id) ON DELETE CASCADE,
  subject TEXT NOT NULL, token_blob BLOB NOT NULL, expires_at INTEGER NOT NULL,
  refreshing INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, provider_id)
);
CREATE TABLE feishu_kb_source (
  source_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider_id INTEGER NOT NULL,
  document_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  interval_seconds INTEGER NOT NULL DEFAULT 900 CHECK (interval_seconds >= 900),
  next_sync INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', digest TEXT, content TEXT,
  FOREIGN KEY (user_id, provider_id) REFERENCES feishu_kb_grant(user_id, provider_id) ON DELETE CASCADE,
  UNIQUE (user_id, provider_id, document_id)
);
CREATE INDEX feishu_kb_due ON feishu_kb_source(enabled, next_sync);
UPDATE _schema_version SET version = 29;
