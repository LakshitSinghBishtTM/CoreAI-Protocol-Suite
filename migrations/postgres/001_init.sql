-- CoreAI Protocol Suite
-- Migration 001: Initial schema

BEGIN;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";

-- Providers

CREATE TABLE providers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(64) NOT NULL UNIQUE,
    display_name    VARCHAR(128) NOT NULL,
    base_url        TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_providers_enabled ON providers(enabled);

INSERT INTO providers (name, display_name, base_url, priority) VALUES
    ('openai',    'OpenAI',    'https://api.openai.com/v1',          10),
    ('anthropic', 'Anthropic', 'https://api.anthropic.com/v1',       20),
    ('gemini',    'Google Gemini', 'https://generativelanguage.googleapis.com/v1beta', 30),
    ('grok',      'xAI Grok',  'https://api.x.ai/v1',                40),
    ('deepseek',  'DeepSeek',  'https://api.deepseek.com/v1',        50);

-- Models

CREATE TABLE models (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name            VARCHAR(128) NOT NULL,
    display_name    VARCHAR(128) NOT NULL,
    context_window  INTEGER NOT NULL,
    max_output      INTEGER NOT NULL,
    cost_per_1k_input_usd   NUMERIC(10, 6) NOT NULL DEFAULT 0,
    cost_per_1k_output_usd  NUMERIC(10, 6) NOT NULL DEFAULT 0,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider_id, name)
);

CREATE INDEX idx_models_provider ON models(provider_id);
CREATE INDEX idx_models_enabled  ON models(enabled);

-- Users

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           VARCHAR(320) NOT NULL UNIQUE,
    password_hash   VARCHAR(256) NOT NULL,
    plan            VARCHAR(32) NOT NULL DEFAULT 'free'
                        CHECK (plan IN ('free', 'pro', 'enterprise')),
    monthly_budget_usd  NUMERIC(10, 2),
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_email  ON users(email);
CREATE INDEX idx_users_active ON users(active);

-- API keys

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash        VARCHAR(256) NOT NULL UNIQUE,
    key_prefix      VARCHAR(12) NOT NULL,
    name            VARCHAR(128),
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    revoked         BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_api_keys_user    ON api_keys(user_id);
CREATE INDEX idx_api_keys_hash    ON api_keys(key_hash);
CREATE INDEX idx_api_keys_revoked ON api_keys(revoked);

-- Routing policies

CREATE TABLE routing_policies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(128) NOT NULL UNIQUE,
    description     TEXT,
    strategy        VARCHAR(64) NOT NULL
                        CHECK (strategy IN ('cost_aware', 'latency_first', 'round_robin', 'explicit')),
    config          JSONB NOT NULL DEFAULT '{}',
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO routing_policies (name, description, strategy, config) VALUES
    ('default',      'Cost-aware routing with latency fallback', 'cost_aware',
        '{"latency_threshold_ms": 2000, "cost_weight": 0.7, "latency_weight": 0.3}'),
    ('low_latency',  'Latency-optimized, cost secondary',        'latency_first',
        '{"max_latency_ms": 500, "fallback_strategy": "cost_aware"}'),
    ('round_robin',  'Equal distribution across providers',      'round_robin',
        '{"exclude_degraded": true}');

-- Agent definitions

CREATE TABLE agent_definitions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(128) NOT NULL,
    description     TEXT,
    type            VARCHAR(64) NOT NULL
                        CHECK (type IN ('research', 'code_execution', 'data_analysis', 'general')),
    system_prompt   TEXT,
    max_steps       INTEGER NOT NULL DEFAULT 200,
    timeout_seconds INTEGER NOT NULL DEFAULT 600,
    routing_policy_id UUID REFERENCES routing_policies(id),
    config          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Agent runs

CREATE TABLE agent_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL REFERENCES agent_definitions(id),
    user_id         UUID NOT NULL REFERENCES users(id),
    status          VARCHAR(32) NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'terminated')),
    steps_completed INTEGER NOT NULL DEFAULT 0,
    result          JSONB,
    error           TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_runs_user   ON agent_runs(user_id);
CREATE INDEX idx_agent_runs_status ON agent_runs(status);

-- Update triggers

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_providers_updated_at
    BEFORE UPDATE ON providers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_models_updated_at
    BEFORE UPDATE ON models
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_routing_policies_updated_at
    BEFORE UPDATE ON routing_policies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;