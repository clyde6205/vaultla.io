-- =============================================================================
-- Vaultla.io — 0002_billing_growth_enterprise.sql   (apply after 0001)
-- Billing state, webhook idempotency, referrals, teaser links, SSO config, data residency.
-- Not yet executed against a real Postgres: run it in a scratch database first.
-- =============================================================================
BEGIN;

-- System role for cross-tenant jobs that have no user session: Stripe webhooks, referral rewards.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vaultla_system') THEN
    CREATE ROLE vaultla_system LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- ---------- billing ------------------------------------------------------------
ALTER TABLE subscriptions
  ADD COLUMN provider              text    NOT NULL DEFAULT 'stripe' CHECK (provider IN ('stripe','paddle','invoice')),
  ADD COLUMN provider_customer_id  text,
  ADD COLUMN is_lifetime           boolean NOT NULL DEFAULT false,
  ADD COLUMN currency              char(3),
  ADD COLUMN cancel_at_period_end  boolean NOT NULL DEFAULT false,
  ADD COLUMN past_due_since        timestamptz;
CREATE INDEX ix_subscriptions_customer ON subscriptions (provider_customer_id)
  WHERE provider_customer_id IS NOT NULL;

-- Webhook idempotency: an event id is recorded exactly once.
CREATE TABLE billing_events (
  event_id     text PRIMARY KEY,
  event_type   text NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------- growth: referrals ----------------------------------------------------
CREATE TABLE referral_codes (
  tenant_id   uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
  code        char(8) NOT NULL UNIQUE CHECK (code ~ '^[A-HJKMNP-Z2-9]{8}$'),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE referrals (
  referee_tenant_id    uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,   -- one referral per account
  referrer_tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  status               text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','qualified','held','rejected')),
  referrer_email_hash  text NOT NULL,     -- salted hashes only; never raw emails
  referee_email_hash   text NOT NULL,
  referrer_device_hash text,
  referee_device_hash  text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  qualified_at         timestamptz,
  CHECK (referrer_tenant_id <> referee_tenant_id)
);
CREATE INDEX ix_referrals_referrer ON referrals (referrer_tenant_id, created_at DESC);

CREATE TABLE referral_bonus (
  tenant_id    uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE RESTRICT,
  bonus_bytes  bigint NOT NULL DEFAULT 0 CHECK (bonus_bytes BETWEEN 0 AND 21474836480)   -- 20 GiB cap
);

-- ---------- viral surface: shareable teaser links ------------------------------------
ALTER TABLE vaults
  ADD COLUMN teaser_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN teaser_message text CHECK (teaser_message IS NULL OR char_length(teaser_message) <= 140);

-- ---------- enterprise ---------------------------------------------------------------
ALTER TABLE tenants
  ADD COLUMN data_region       text    NOT NULL DEFAULT 'us-east-1'
      CHECK (data_region IN ('us-east-1','eu-central-1','ap-southeast-1','ap-south-1')),
  ADD COLUMN legal_hold        boolean NOT NULL DEFAULT false,      -- blocks deletion/revocation while true
  ADD COLUMN min_retention_years integer NOT NULL DEFAULT 0 CHECK (min_retention_years BETWEEN 0 AND 150);

-- SSO is bought, not built (WorkOS/Auth0/Cognito federation): we store only the connection reference.
CREATE TABLE sso_connections (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  protocol      text NOT NULL CHECK (protocol IN ('saml','oidc')),
  provider_ref  text NOT NULL,                   -- connection id at the SSO provider
  domains       text[] NOT NULL DEFAULT '{}',    -- verified email domains that must use SSO
  enforced      boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, provider_ref)
);
CREATE INDEX ix_sso_domains ON sso_connections USING gin (domains);

-- =============================================================================
-- RLS for the new tenant-scoped tables
-- =============================================================================
ALTER TABLE sso_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE sso_connections FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON sso_connections TO vaultla_app
  USING (tenant_id = app_tenant_id()) WITH CHECK (tenant_id = app_tenant_id());

ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;
ALTER TABLE referrals FORCE  ROW LEVEL SECURITY;
CREATE POLICY referral_party_read ON referrals FOR SELECT TO vaultla_app
  USING (referee_tenant_id = app_tenant_id() OR referrer_tenant_id = app_tenant_id());

ALTER TABLE referral_bonus ENABLE ROW LEVEL SECURITY;
ALTER TABLE referral_bonus FORCE  ROW LEVEL SECURITY;
CREATE POLICY bonus_self_read ON referral_bonus FOR SELECT TO vaultla_app USING (tenant_id = app_tenant_id());

-- referral_codes: only a code -> tenant mapping; readable for lookup at signup, owner writes its own.
ALTER TABLE referral_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE referral_codes FORCE  ROW LEVEL SECURITY;
CREATE POLICY code_lookup ON referral_codes FOR SELECT TO vaultla_app USING (true);
CREATE POLICY code_owner_write ON referral_codes FOR INSERT TO vaultla_app WITH CHECK (tenant_id = app_tenant_id());

-- System role: cross-tenant, narrowly scoped.
ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_events FORCE  ROW LEVEL SECURITY;
CREATE POLICY system_all ON billing_events TO vaultla_system USING (true) WITH CHECK (true);

CREATE POLICY system_subs   ON subscriptions TO vaultla_system USING (true) WITH CHECK (true);
CREATE POLICY system_refs   ON referrals     TO vaultla_system USING (true) WITH CHECK (true);
CREATE POLICY system_bonus  ON referral_bonus TO vaultla_system USING (true) WITH CHECK (true);
CREATE POLICY system_codes  ON referral_codes FOR SELECT TO vaultla_system USING (true);
CREATE POLICY system_audit_ins ON audit_log FOR INSERT TO vaultla_system WITH CHECK (true);
CREATE POLICY system_audit_sel ON audit_log FOR SELECT TO vaultla_system USING (true);  -- needed by the hash-chain trigger

-- ---------- grants ---------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON sso_connections TO vaultla_app;
GRANT SELECT ON referrals, referral_bonus, referral_codes TO vaultla_app;
GRANT INSERT ON referral_codes TO vaultla_app;

GRANT USAGE ON SCHEMA public TO vaultla_system;
GRANT SELECT, INSERT ON billing_events TO vaultla_system;
GRANT SELECT, INSERT, UPDATE ON subscriptions, referrals, referral_bonus TO vaultla_system;
GRANT SELECT ON referral_codes TO vaultla_system;
GRANT SELECT, INSERT ON audit_log TO vaultla_system;
GRANT USAGE, SELECT ON SEQUENCE audit_log_seq_seq TO vaultla_system;

COMMIT;
