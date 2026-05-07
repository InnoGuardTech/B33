-- ════════════════════════════════════════════════════════════════════
-- Hydra V14 — Migration: per-account proxy support
-- ════════════════════════════════════════════════════════════════════
-- Adds the proxy_url column on the `accounts` table so each Webook
-- account can be routed through its OWN exit IP (proxy-per-account).
--
-- The column is nullable: when NULL the booking layer falls back to
-- the global PROXY_SERVER env var (or no proxy at all).
--
-- This script is idempotent — safe to run multiple times.
-- It is also automatically applied at runtime by storage._ensure_event_v12_columns()
-- on first boot. This file exists as a single source of truth for DBAs.
-- ════════════════════════════════════════════════════════════════════

BEGIN;

SAVEPOINT v14_account_proxy;

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS proxy_url TEXT;

-- Optional helper index: speeds up "find next available account WITHOUT proxy"
-- queries used during distribution. WHERE clause prunes the index size.
CREATE INDEX IF NOT EXISTS idx_accounts_proxy_null
    ON accounts(status)
    WHERE proxy_url IS NULL;

RELEASE SAVEPOINT v14_account_proxy;

COMMIT;

-- Verification:
--     SELECT column_name, data_type, is_nullable
--     FROM information_schema.columns
--     WHERE table_name = 'accounts' AND column_name = 'proxy_url';
-- Expected: ('proxy_url', 'text', 'YES')
