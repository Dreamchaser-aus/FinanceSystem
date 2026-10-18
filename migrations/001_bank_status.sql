-- BankFlow V1: idempotent, non-destructive PostgreSQL migration.
-- Back up PostgreSQL before applying. Run on the existing application database.
BEGIN;
SELECT pg_advisory_xact_lock(7261001);
ALTER TABLE bank ADD COLUMN IF NOT EXISTS status VARCHAR(30);
ALTER TABLE bank ADD COLUMN IF NOT EXISTS status_remark TEXT;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS status_updated_at TIMESTAMPTZ;
ALTER TABLE bank ADD COLUMN IF NOT EXISTS status_updated_by VARCHAR(36);
UPDATE bank SET status=CASE WHEN active THEN 'active' ELSE 'closed' END WHERE status IS NULL;
ALTER TABLE bank ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE bank ALTER COLUMN status SET NOT NULL;
CREATE TABLE IF NOT EXISTS bank_status_history (
    id VARCHAR(36) PRIMARY KEY,
    bank_id VARCHAR(36) NOT NULL REFERENCES bank(id),
    actor_id VARCHAR(36) NOT NULL REFERENCES "user"(id),
    old_status VARCHAR(30) NOT NULL,
    new_status VARCHAR(30) NOT NULL,
    remark TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_bank_status_history_bank_id ON bank_status_history(bank_id);
COMMIT;
