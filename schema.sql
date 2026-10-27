-- Idempotent: runs on every app startup.
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid() on PG < 13

CREATE TABLE IF NOT EXISTS accounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(255) NOT NULL CHECK (length(btrim(name)) > 0),
    balance     NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS transactions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    type          VARCHAR(6) NOT NULL CHECK (type IN ('credit', 'debit')),
    amount        NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    description   VARCHAR(500),
    reference_id  VARCHAR(100) NOT NULL CHECK (length(btrim(reference_id)) > 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT uq_transactions_account_reference UNIQUE (account_id, reference_id)
);

-- Serves history queries: WHERE account_id [AND type] ORDER BY created_at DESC.
CREATE INDEX IF NOT EXISTS ix_transactions_account_created
    ON transactions (account_id, created_at DESC, id DESC);

-- Ledger entries are append-only, even for direct SQL access.
CREATE OR REPLACE FUNCTION forbid_transaction_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'transactions are immutable (% not allowed)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_transactions_immutable
    BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION forbid_transaction_change();
