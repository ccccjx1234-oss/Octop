ALTER TABLE users ADD COLUMN mobile TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_mobile_unique ON users(mobile) WHERE mobile IS NOT NULL;
UPDATE _schema_version SET version = 35;
