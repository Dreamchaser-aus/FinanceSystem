-- BankFlow V6: run ONCE against PostgreSQL after a verified database backup,
-- and BEFORE deploying V6 app.py. Preserves all existing rows.
BEGIN;
CREATE TABLE IF NOT EXISTS platform (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(120) NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_platform_access (
    user_id VARCHAR(36) NOT NULL REFERENCES "user"(id),
    platform_id VARCHAR(36) NOT NULL REFERENCES platform(id),
    PRIMARY KEY (user_id, platform_id)
);
CREATE TABLE IF NOT EXISTS user_permission (
    user_id VARCHAR(36) NOT NULL REFERENCES "user"(id),
    permission VARCHAR(60) NOT NULL,
    PRIMARY KEY (user_id, permission)
);
-- Existing platform tables created by SQLAlchemy may lack server-side defaults.
-- Repair defaults for future direct SQL inserts without overwriting existing values.
ALTER TABLE platform ALTER COLUMN active SET DEFAULT TRUE;
ALTER TABLE platform ALTER COLUMN created_at SET DEFAULT NOW();
INSERT INTO platform (id,name,active,created_at) VALUES ('00000000-0000-4000-8000-000000000001','Legacy Platform',TRUE,NOW())
ON CONFLICT (id) DO NOTHING;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS platform_id VARCHAR(36);
ALTER TABLE "transaction" ADD COLUMN IF NOT EXISTS platform_id VARCHAR(36);
ALTER TABLE dealer ADD COLUMN IF NOT EXISTS platform_id VARCHAR(36);
UPDATE bank SET platform_id='00000000-0000-4000-8000-000000000001' WHERE platform_id IS NULL;
UPDATE "transaction" SET platform_id='00000000-0000-4000-8000-000000000001' WHERE platform_id IS NULL;
UPDATE dealer SET platform_id='00000000-0000-4000-8000-000000000001' WHERE platform_id IS NULL;
ALTER TABLE bank ALTER COLUMN platform_id SET NOT NULL;
ALTER TABLE "transaction" ALTER COLUMN platform_id SET NOT NULL;
ALTER TABLE dealer ALTER COLUMN platform_id SET NOT NULL;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_bank_platform_v6') THEN ALTER TABLE bank ADD CONSTRAINT fk_bank_platform_v6 FOREIGN KEY(platform_id) REFERENCES platform(id); END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_transaction_platform_v6') THEN ALTER TABLE "transaction" ADD CONSTRAINT fk_transaction_platform_v6 FOREIGN KEY(platform_id) REFERENCES platform(id); END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_dealer_platform_v6') THEN ALTER TABLE dealer ADD CONSTRAINT fk_dealer_platform_v6 FOREIGN KEY(platform_id) REFERENCES platform(id); END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_bank_platform_id ON bank(platform_id);
CREATE INDEX IF NOT EXISTS ix_transaction_platform_id ON "transaction"(platform_id);
CREATE INDEX IF NOT EXISTS ix_dealer_platform_id ON dealer(platform_id);
-- Existing non-admin users retain no access until admin explicitly grants platforms + permissions.
COMMIT;
