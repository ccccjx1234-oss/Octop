-- Schema v25: model pricing, message feedback, and eval suites.
-- model_pricing is name-keyed config (one row per model ref).
-- message_feedback keeps one vote per user per assistant message.

CREATE TABLE IF NOT EXISTS model_pricing (
  model                TEXT PRIMARY KEY,
  input_per_mtok       REAL NOT NULL DEFAULT 0,
  output_per_mtok      REAL NOT NULL DEFAULT 0,
  cache_read_per_mtok  REAL NOT NULL DEFAULT 0,
  currency             TEXT NOT NULL DEFAULT 'CNY',
  updated_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS message_feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id    TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  thread_id   TEXT NOT NULL,
  message_id  TEXT NOT NULL,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  rating      INTEGER NOT NULL,
  comment     TEXT NOT NULL DEFAULT '',
  excerpt     TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  UNIQUE (user_id, agent_id, thread_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_feedback_ts ON message_feedback(updated_at);
CREATE INDEX IF NOT EXISTS idx_message_feedback_agent ON message_feedback(agent_id, updated_at);

CREATE TABLE IF NOT EXISTS eval_cases (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  eval_case_id         TEXT NOT NULL UNIQUE,
  suite                TEXT NOT NULL DEFAULT 'default',
  prompt               TEXT NOT NULL,
  expect_contains      TEXT NOT NULL DEFAULT '[]',
  expect_not_contains  TEXT NOT NULL DEFAULT '[]',
  created_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_cases_suite ON eval_cases(suite);

CREATE TABLE IF NOT EXISTS eval_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  eval_run_id  TEXT NOT NULL UNIQUE,
  agent_id     TEXT NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
  suite        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'running',
  total        INTEGER NOT NULL DEFAULT 0,
  passed       INTEGER NOT NULL DEFAULT 0,
  error        TEXT NOT NULL DEFAULT '',
  started_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  started_at   INTEGER NOT NULL,
  finished_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_started ON eval_runs(started_at);

CREATE TABLE IF NOT EXISTS eval_results (
  eval_run_id   TEXT NOT NULL REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
  eval_case_id  TEXT NOT NULL,
  prompt        TEXT NOT NULL,
  passed        INTEGER NOT NULL,
  output        TEXT NOT NULL DEFAULT '',
  detail        TEXT NOT NULL DEFAULT '',
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (eval_run_id, eval_case_id)
);

UPDATE _schema_version SET version = 25;
