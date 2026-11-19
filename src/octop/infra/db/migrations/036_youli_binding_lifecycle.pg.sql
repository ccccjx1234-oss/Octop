-- Octop control database, migration runner. Rollback leaves the additive column in place.
ALTER TABLE youli_identity_binding ADD COLUMN unlinked INTEGER NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 36;
