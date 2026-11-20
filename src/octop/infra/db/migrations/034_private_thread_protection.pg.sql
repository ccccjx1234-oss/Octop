ALTER TABLE threads ADD COLUMN private_knowledge_protected INTEGER NOT NULL DEFAULT 0;
UPDATE _schema_version SET version = 34;
