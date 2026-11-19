-- Octop control database, migration runner. Registers explicitly shared Feishu imports.
-- Rollback: keep additive tables until a pre-migration backup can be restored.
CREATE TABLE kb_feishu_connections (
  id TEXT PRIMARY KEY,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL,
  subject TEXT NOT NULL,
  label TEXT NOT NULL,
  token_blob BYTEA,
  expires_at BIGINT NOT NULL,
  refreshing INTEGER NOT NULL DEFAULT 0,
  UNIQUE (owner_user_id, provider_id, subject)
);
CREATE TABLE kb_feishu_connection_states (
  state_hash TEXT PRIMARY KEY,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL,
  session_hash TEXT NOT NULL,
  verifier_blob BYTEA NOT NULL,
  redirect_uri TEXT NOT NULL,
  label TEXT NOT NULL,
  client_id TEXT NOT NULL,
  expires_at BIGINT NOT NULL
);
CREATE INDEX idx_kb_feishu_connection_states_expiry ON kb_feishu_connection_states(expires_at);
CREATE TABLE kb_feishu_sources (
  id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id) ON DELETE CASCADE,
  connection_id TEXT NOT NULL REFERENCES kb_feishu_connections(id) ON DELETE CASCADE,
  approved_by INTEGER NOT NULL,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('wiki_space', 'wiki_node', 'drive_folder')),
  root_token TEXT NOT NULL,
  space_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  include_children INTEGER NOT NULL,
  auto_add INTEGER NOT NULL,
  schedule TEXT NOT NULL CHECK (schedule IN ('manual', 'daily')),
  approved_at BIGINT NOT NULL,
  approved_document_ids TEXT NOT NULL,
  approval_visibility TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'queued',
  next_sync BIGINT NOT NULL DEFAULT 0,
  last_sync BIGINT,
  last_error TEXT,
  pending_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT,
  lease_until BIGINT NOT NULL DEFAULT 0,
  UNIQUE (kb_id, connection_id, scope_kind, space_id, root_token)
);
CREATE INDEX idx_kb_feishu_sources_due ON kb_feishu_sources(active, next_sync);
CREATE INDEX idx_kb_feishu_sources_connection ON kb_feishu_sources(connection_id);
ALTER TABLE knowledge_documents ADD COLUMN feishu_source_id TEXT REFERENCES kb_feishu_sources(id) ON DELETE CASCADE;
ALTER TABLE knowledge_documents ADD COLUMN feishu_document_id TEXT;
ALTER TABLE knowledge_documents ADD COLUMN feishu_origin TEXT;
CREATE UNIQUE INDEX idx_kb_feishu_document_identity ON knowledge_documents(feishu_source_id, feishu_document_id);
CREATE TABLE kb_feishu_blob_cleanup (
  document_id TEXT PRIMARY KEY,
  kb_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  available_at BIGINT NOT NULL
);
UPDATE _schema_version SET version = 37;
