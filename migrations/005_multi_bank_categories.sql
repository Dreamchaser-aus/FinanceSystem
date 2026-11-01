-- V7.9.6.7: Allow many category assignments per bank, preserving existing assignments.
BEGIN;
ALTER TABLE bank_label_assignment DROP CONSTRAINT IF EXISTS bank_label_assignment_pkey;
ALTER TABLE bank_label_assignment ADD CONSTRAINT bank_label_assignment_pkey PRIMARY KEY (bank_id,label_id);
COMMIT;
