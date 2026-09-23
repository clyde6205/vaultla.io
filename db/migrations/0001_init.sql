-- =============================================================================
-- Vaultla.io — 0001_init.sql
-- PostgreSQL 15+ (Amazon RDS). Multi-tenant, RLS enforced on every tenant table.
--
-- Conventions
--   * Every tenant-owned table carries tenant_id and has FORCE ROW LEVEL SECURITY.
--   * The API connects as `vaultla_app` and MUST run, per transaction:
--         SELECT set_config('app.tenant_id', '<uuid>', true);
--     If it is unset, current_setting(...) is NULL and NO rows match (fail closed).
--   * The Chronos scanner connects as `vaultla_chronos`: cross-tenant read of
--     sealed vaults, and column-restricted UPDATE (state fields only).
--   * Plaintext never exists server-side. Columns named *_sealed are KMS-encrypted
--     shares; *_wrapped are client-wrapped blobs the server cannot open.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- ---------- roles (passwords are set out-of-band via Secrets Manager) ---------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vaultla_app') THEN
    CREATE ROLE vaultla_app LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vaultla_chronos') THEN
    CREATE ROLE vaultla_chronos LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- ---------- enums --------------------------------------------------------------
CREATE TYPE tenant_kind    AS ENUM ('personal', 'enterprise');
CREATE TYPE user_role      AS ENUM ('owner', 'admin', 'member', 'guest');
CREATE TYPE trigger_type   AS ENUM ('fixed_date', 'progressive', 'liveness');
CREATE TYPE vault_state    AS ENUM ('draft', 'sealed', 'matured', 'released', 'revoked');
CREATE TYPE storage_class  AS ENUM ('STANDARD', 'STANDARD_IA', 'DEEP_ARCHIVE');
CREATE TYPE plan_tier      AS ENUM ('free', 'premium', 'lifetime', 'enterprise');

-- ---------- helper: current tenant (NULL when unset => no rows visible) --------
CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$;

-- ---------- tenants -------------------------------------------------------------
CREATE TABLE tenants (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind               tenant_kind NOT NULL,
  slug               citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name       text   NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
  -- White-label: {logo_key, theme:{...}, custom_domain}. Validated in the API layer.
  branding           jsonb  NOT NULL DEFAULT '{}'::jsonb,
  stripe_customer_id text UNIQUE,
  max_sub_capsules   integer NOT NULL DEFAULT 1 CHECK (max_sub_capsules BETWEEN 1 AND 50000),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_tenants_touch BEFORE UPDATE ON tenants
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------- users ---------------------------------------------------------------
CREATE TABLE users (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  email        citext NOT NULL,
  role         user_role NOT NULL DEFAULT 'member',
  idp_subject  text NOT NULL,                   -- subject claim from the IdP (Cognito etc.)
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email),
  UNIQUE (tenant_id, idp_subject),
  UNIQUE (tenant_id, id)                        -- target for composite FKs
);
CREATE TRIGGER trg_users_touch BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------- subscriptions (Stripe-mirrored) ------------------------------------
CREATE TABLE subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id              uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  tier                   plan_tier NOT NULL DEFAULT 'free',
  stripe_subscription_id text UNIQUE,
  status                 text NOT NULL DEFAULT 'active',
  storage_quota_bytes    bigint NOT NULL DEFAULT 1073741824 CHECK (storage_quota_bytes >= 0), -- 1 GiB free
  current_period_end     timestamptz,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_subscriptions_tenant ON subscriptions (tenant_id);
CREATE TRIGGER trg_subscriptions_touch BEFORE UPDATE ON subscriptions
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ---------- vaults --------------------------------------------------------------
CREATE TABLE vaults (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  owner_id           uuid NOT NULL,
  parent_vault_id    uuid,                                  -- B2B sub-capsules point at a program vault
  state              vault_state NOT NULL DEFAULT 'draft',
  -- Non-secret metadata (per spec: size, unlock date, owner):
  total_size_bytes   bigint NOT NULL DEFAULT 0 CHECK (total_size_bytes >= 0),
  unlock_at          timestamptz,                           -- fixed_date / progressive start
  trigger            trigger_type NOT NULL,
  trigger_config     jsonb NOT NULL,                        -- validated by app/chronos/triggers.py
  -- Scheduler bookkeeping: earliest instant the trigger could possibly change state.
  next_eval_at       timestamptz,
  releasable_bps     integer NOT NULL DEFAULT 0 CHECK (releasable_bps BETWEEN 0 AND 10000),
  last_checkin_at    timestamptz,
  envelope_version   smallint NOT NULL DEFAULT 1,           -- crypto agility for 100-year retention
  storage_class      storage_class NOT NULL DEFAULT 'STANDARD',
  restore_requested_at timestamptz,
  sealed_at          timestamptz,
  matured_at         timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, owner_id)        REFERENCES users  (tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, parent_vault_id) REFERENCES vaults (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT ck_fixed_needs_unlock CHECK (trigger <> 'fixed_date' OR unlock_at IS NOT NULL),
  CONSTRAINT ck_sealed_needs_eval  CHECK (state <> 'sealed' OR next_eval_at IS NOT NULL)
);
CREATE TRIGGER trg_vaults_touch BEFORE UPDATE ON vaults
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Required indexes on unlock dates + tenant mapping
CREATE INDEX ix_vaults_tenant_unlock  ON vaults (tenant_id, unlock_at);
CREATE INDEX ix_vaults_tenant_state   ON vaults (tenant_id, state);
CREATE INDEX ix_vaults_tenant_owner   ON vaults (tenant_id, owner_id);
CREATE INDEX ix_vaults_parent         ON vaults (tenant_id, parent_vault_id) WHERE parent_vault_id IS NOT NULL;
-- The Chronos hot path: tiny partial index, only sealed vaults, ordered by due time.
CREATE INDEX ix_vaults_chronos_due    ON vaults (next_eval_at) WHERE state = 'sealed';

-- ---------- vault items (each is one encrypted blob in S3) ---------------------
CREATE TABLE vault_items (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id           uuid NOT NULL,
  vault_id            uuid NOT NULL,
  release_rank        integer NOT NULL CHECK (release_rank >= 0),  -- progressive releases unlock by rank
  size_bytes          bigint  NOT NULL CHECK (size_bytes > 0),
  storage_key         text    NOT NULL,
  ciphertext_sha256   bytea   NOT NULL CHECK (octet_length(ciphertext_sha256) = 32),
  upload_confirmed_at timestamptz,
  -- Server-held Shamir share, sealed under KMS with encryption context (tenant, vault, item).
  -- One share is information-theoretically useless alone; server can never reconstruct the key.
  server_share_sealed bytea   NOT NULL,
  released_at         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (vault_id, release_rank),
  UNIQUE (storage_key),
  FOREIGN KEY (tenant_id, vault_id) REFERENCES vaults (tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_items_tenant_vault ON vault_items (tenant_id, vault_id, release_rank);

-- ---------- wrapped key material (opaque to server) ----------------------------
CREATE TABLE vault_key_envelopes (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL,
  vault_id      uuid NOT NULL,
  item_id       uuid NOT NULL,
  holder        text NOT NULL CHECK (holder IN ('owner', 'guardian', 'beneficiary')),
  holder_user_id uuid,
  wrapped_share bytea NOT NULL CHECK (octet_length(wrapped_share) BETWEEN 32 AND 4096),
  kdf_params    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, item_id)  REFERENCES vault_items (tenant_id, id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, vault_id) REFERENCES vaults      (tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_envelopes_item ON vault_key_envelopes (tenant_id, item_id);

-- ---------- liveness -----------------------------------------------------------
CREATE TABLE liveness_checkins (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL,
  vault_id      uuid NOT NULL,
  source        text NOT NULL CHECK (source IN ('user', 'webhook', 'trustee')),
  attested_by   text,                      -- webhook id / trustee id
  trusted_time  timestamptz NOT NULL,      -- Chronos time, never client time
  created_at    timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, vault_id) REFERENCES vaults (tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_checkins_vault_time ON liveness_checkins (tenant_id, vault_id, trusted_time DESC);

-- ---------- B2B provisioning & events ------------------------------------------
CREATE TABLE provisioning_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  total_rows    integer NOT NULL CHECK (total_rows BETWEEN 1 AND 50000),
  created_rows  integer NOT NULL DEFAULT 0,
  error_report_key text,
  created_by    uuid NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX ix_prov_tenant ON provisioning_jobs (tenant_id, created_at DESC);

CREATE TABLE event_pages (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL,
  vault_id          uuid NOT NULL,
  slug              citext NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{3,62}$'),
  qr_token_hash     bytea NOT NULL CHECK (octet_length(qr_token_hash) = 32),  -- store hash only
  allow_anonymous   boolean NOT NULL DEFAULT true,
  opens_at          timestamptz NOT NULL,
  closes_at         timestamptz NOT NULL,
  max_guest_bytes   bigint NOT NULL DEFAULT 2147483648 CHECK (max_guest_bytes > 0),
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (closes_at > opens_at),
  FOREIGN KEY (tenant_id, vault_id) REFERENCES vaults (tenant_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_event_pages_tenant ON event_pages (tenant_id, opens_at);

-- ---------- Chronos state + tamper-evident audit chain -------------------------
CREATE TABLE chronos_state (
  id                   boolean PRIMARY KEY DEFAULT true CHECK (id),   -- singleton row
  last_trusted_time    timestamptz NOT NULL,
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  seq           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  vault_id      uuid,
  actor         text NOT NULL,
  event_type    text NOT NULL,
  trusted_time  timestamptz NOT NULL,
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash     bytea,
  row_hash      bytea NOT NULL
);
CREATE INDEX ix_audit_tenant_seq ON audit_log (tenant_id, seq DESC);
CREATE INDEX ix_audit_vault      ON audit_log (tenant_id, vault_id, seq DESC);

-- Hash-chain PER TENANT: each row commits to the previous row of the same tenant (so the
-- chain is computable under RLS, where a session only sees its own tenant). Append-only.
CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE prev bytea;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('vaultla_audit:' || NEW.tenant_id::text));
  SELECT row_hash INTO prev FROM audit_log
   WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
  NEW.prev_hash := prev;
  NEW.row_hash  := digest(
      coalesce(encode(prev, 'hex'), '') || NEW.tenant_id::text || coalesce(NEW.vault_id::text, '') ||
      NEW.actor || NEW.event_type || NEW.trusted_time::text || NEW.payload::text, 'sha256');
  RETURN NEW;
END $$;
CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_chain();

CREATE OR REPLACE FUNCTION audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$;
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_immutable();

-- =============================================================================
-- ROW-LEVEL SECURITY
-- =============================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'users','subscriptions','vaults','vault_items','vault_key_envelopes',
    'liveness_checkins','provisioning_jobs','event_pages','audit_log'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I TO vaultla_app
         USING (tenant_id = app_tenant_id())
         WITH CHECK (tenant_id = app_tenant_id())', t);
  END LOOP;
END $$;

-- tenants: a session sees only its own tenant row.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants TO vaultla_app
  USING (id = app_tenant_id()) WITH CHECK (id = app_tenant_id());

-- Chronos: cross-tenant, but read + narrowly-scoped update on sealed vaults only.
ALTER TABLE chronos_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY chronos_scan_read   ON vaults FOR SELECT TO vaultla_chronos USING (state = 'sealed');
CREATE POLICY chronos_scan_write  ON vaults FOR UPDATE TO vaultla_chronos
  USING (state = 'sealed') WITH CHECK (state IN ('sealed','matured'));
CREATE POLICY chronos_items_read  ON vault_items FOR SELECT TO vaultla_chronos USING (true);
CREATE POLICY chronos_items_write ON vault_items FOR UPDATE TO vaultla_chronos USING (true) WITH CHECK (true);
CREATE POLICY chronos_checkins    ON liveness_checkins FOR SELECT TO vaultla_chronos USING (true);
CREATE POLICY chronos_audit_ins   ON audit_log FOR INSERT TO vaultla_chronos WITH CHECK (true);
CREATE POLICY chronos_audit_sel   ON audit_log FOR SELECT TO vaultla_chronos USING (true);
CREATE POLICY chronos_state_all   ON chronos_state TO vaultla_chronos USING (true) WITH CHECK (true);

-- ---------- grants (least privilege) --------------------------------------------
GRANT USAGE ON SCHEMA public TO vaultla_app, vaultla_chronos;
GRANT SELECT, INSERT, UPDATE ON tenants, users, subscriptions, vaults, vault_items,
      vault_key_envelopes, provisioning_jobs, event_pages TO vaultla_app;
GRANT SELECT, INSERT ON liveness_checkins, audit_log TO vaultla_app;
GRANT USAGE, SELECT ON SEQUENCE audit_log_seq_seq TO vaultla_app, vaultla_chronos;

GRANT SELECT ON vaults, vault_items, liveness_checkins TO vaultla_chronos;
-- Chronos may change ONLY these columns; it cannot touch owners, sizes, or key material.
GRANT UPDATE (state, matured_at, next_eval_at, releasable_bps, updated_at, restore_requested_at) ON vaults TO vaultla_chronos;
GRANT UPDATE (released_at) ON vault_items TO vaultla_chronos;
GRANT SELECT, INSERT ON audit_log TO vaultla_chronos;
GRANT SELECT, INSERT, UPDATE ON chronos_state TO vaultla_chronos;

-- Deliberately NO DELETE granted to either role: deletion is an audited admin procedure.

COMMIT;
