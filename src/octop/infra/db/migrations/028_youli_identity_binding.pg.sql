-- Authenticated, one-to-one employee binding and its current delegation.
CREATE TABLE IF NOT EXISTS youli_identity_binding (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  company_id BIGINT NOT NULL,
  employee_id BIGINT NOT NULL,
  feishu_subject TEXT NOT NULL,
  sid TEXT NOT NULL REFERENCES youli_session(sid),
  UNIQUE (company_id, employee_id)
);
CREATE TABLE IF NOT EXISTS youli_binding_generation (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL DEFAULT 0
);
UPDATE _schema_version SET version = 28;
