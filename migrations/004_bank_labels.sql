-- V7.2 optional bank tags, safe on existing databases
BEGIN;
CREATE TABLE IF NOT EXISTS bank_label (
 id VARCHAR(36) PRIMARY KEY,
 platform_id VARCHAR(36) NOT NULL REFERENCES platform(id),
 name VARCHAR(60) NOT NULL,
 CONSTRAINT uq_bank_label_platform_name UNIQUE (platform_id,name)
);
CREATE INDEX IF NOT EXISTS ix_bank_label_platform_id ON bank_label(platform_id);
CREATE TABLE IF NOT EXISTS bank_label_assignment (
 bank_id VARCHAR(36) PRIMARY KEY REFERENCES bank(id),
 label_id VARCHAR(36) NOT NULL REFERENCES bank_label(id)
);
CREATE INDEX IF NOT EXISTS ix_bank_label_assignment_label_id ON bank_label_assignment(label_id);
COMMIT;
