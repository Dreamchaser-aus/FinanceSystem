BEGIN;
CREATE TABLE IF NOT EXISTS unclaimed_deposit (
 id VARCHAR(36) PRIMARY KEY,
 platform_id VARCHAR(36) NOT NULL REFERENCES platform(id),
 bank_id VARCHAR(36) NOT NULL REFERENCES bank(id),
 amount NUMERIC(18,2) NOT NULL CHECK (amount > 0),
 reference VARCHAR(120) NOT NULL,
 remark TEXT,
 status VARCHAR(20) NOT NULL DEFAULT 'unclaimed',
 transaction_id VARCHAR(36) REFERENCES "transaction"(id),
 created_by VARCHAR(36) NOT NULL REFERENCES "user"(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CONSTRAINT uq_unclaimed_bank_reference UNIQUE (bank_id, reference)
);
CREATE INDEX IF NOT EXISTS ix_unclaimed_platform_id ON unclaimed_deposit(platform_id);
CREATE INDEX IF NOT EXISTS ix_unclaimed_bank_id ON unclaimed_deposit(bank_id);
COMMIT;
