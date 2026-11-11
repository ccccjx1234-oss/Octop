-- Schema v26: Youli (经贝 MCN) handoff sessions, private agent bindings and
-- business-template whitelist. Tokens are stored encrypted by the caller.

CREATE TABLE IF NOT EXISTS youli_login_request (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id    TEXT NOT NULL UNIQUE,
  state_hash    TEXT NOT NULL,
  client_id     TEXT NOT NULL,
  audience      TEXT NOT NULL,
  callback_url  TEXT NOT NULL,
  verifier_hash TEXT NOT NULL,
  verifier_blob BLOB,
  browser_hash  TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  expires_at    INTEGER NOT NULL,
  consumed_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_youli_login_request_expires
  ON youli_login_request(expires_at);

CREATE TABLE IF NOT EXISTS youli_session (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  sid               TEXT NOT NULL UNIQUE,
  user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_user_id   INTEGER NOT NULL,
  account_username  TEXT NOT NULL,
  company_id        INTEGER NOT NULL,
  employee_id       INTEGER NOT NULL,
  industry          INTEGER,
  token_blob        BLOB,
  token_expires_at  INTEGER NOT NULL,
  abs_expires_at    INTEGER NOT NULL,
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        INTEGER NOT NULL,
  revoked_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_youli_session_user ON youli_session(user_id);
CREATE INDEX IF NOT EXISTS idx_youli_session_owner
  ON youli_session(company_id, employee_id);

CREATE TABLE IF NOT EXISTS youli_agent_binding (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id         INTEGER NOT NULL,
  employee_id        INTEGER NOT NULL,
  published_expert_id INTEGER NOT NULL,
  user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  agent_id           TEXT NOT NULL,
  youli_business     INTEGER NOT NULL DEFAULT 1,
  created_at         INTEGER NOT NULL,
  UNIQUE (company_id, employee_id, published_expert_id)
);
CREATE INDEX IF NOT EXISTS idx_youli_agent_binding_agent
  ON youli_agent_binding(agent_id);

CREATE TABLE IF NOT EXISTS youli_business_template (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  published_expert_id INTEGER NOT NULL UNIQUE,
  name               TEXT NOT NULL DEFAULT '',
  created_at         INTEGER NOT NULL
);

UPDATE _schema_version SET version = 26;

