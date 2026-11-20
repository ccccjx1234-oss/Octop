-- ERP reauthentication is separate from document consent and login state.
CREATE TABLE feishu_erp_reauth_state (
  state_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL REFERENCES sso_providers(id) ON DELETE CASCADE,
  session_hash TEXT NOT NULL, subject TEXT NOT NULL, client_id TEXT NOT NULL, region TEXT NOT NULL,
  config_fingerprint TEXT NOT NULL,
  verifier_blob BLOB NOT NULL, redirect_uri TEXT NOT NULL, generation INTEGER NOT NULL,
  expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE feishu_erp_reauth_proof (
  proof_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider_id INTEGER NOT NULL REFERENCES sso_providers(id) ON DELETE CASCADE,
  session_hash TEXT NOT NULL, subject TEXT NOT NULL, client_id TEXT NOT NULL, region TEXT NOT NULL,
  config_fingerprint TEXT NOT NULL,
  generation INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
UPDATE _schema_version SET version = 30;
