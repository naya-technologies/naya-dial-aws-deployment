CREATE SCHEMA IF NOT EXISTS dial_licensing;

CREATE TABLE IF NOT EXISTS dial_licensing.activations (
    id text PRIMARY KEY,
    tier_name text NOT NULL,
    status text NOT NULL,
    expiration_timestamp bigint,
    encrypted_record bytea NOT NULL
);

CREATE TABLE IF NOT EXISTS dial_licensing.apply_operations (
    id text PRIMARY KEY,
    record jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS dial_licensing.request_receipts (
    id text PRIMARY KEY,
    tier_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS dial_licensing.active_users (
    id text PRIMARY KEY,
    expires_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS active_users_expiry ON dial_licensing.active_users (expires_at);

CREATE TABLE IF NOT EXISTS dial_licensing.active_api_keys (
    id text PRIMARY KEY,
    expires_at bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS active_api_keys_expiry ON dial_licensing.active_api_keys (expires_at);
