-- NULL means an older binding whose identity provenance is unknown.
ALTER TABLE youli_identity_binding ADD COLUMN had_independent_identity INTEGER;
UPDATE _schema_version SET version = 31;
