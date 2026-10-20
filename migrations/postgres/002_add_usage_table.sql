-- CoreAI Protocol Suite
-- Migration 002: API usage tracking

BEGIN;

-- Request log

CREATE TABLE api_requests (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID NOT NULL REFERENCES users(id),
    api_key_id          UUID REFERENCES api_keys(id),
    provider_id         UUID NOT NULL REFERENCES providers(id),
    model_id            UUID NOT NULL REFERENCES models(id),
    routing_policy_id   UUID REFERENCES routing_policies(id),
    agent_run_id        UUID REFERENCES agent_runs(id),

    -- Request metadata
    request_id          VARCHAR(128) UNIQUE,
    status_code         INTEGER NOT NULL,
    tokens_input        INTEGER NOT NULL DEFAULT 0,
    tokens_output       INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER,

    -- Cost
    cost_usd            NUMERIC(12, 8) NOT NULL DEFAULT 0,

    -- Routing
    was_failover        BOOLEAN NOT NULL DEFAULT false,
    original_provider_id UUID REFERENCES providers(id),

    -- Timestamps
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_api_requests_user        ON api_requests(user_id);
CREATE INDEX idx_api_requests_provider    ON api_requests(provider_id);
CREATE INDEX idx_api_requests_requested   ON api_requests(requested_at DESC);
CREATE INDEX idx_api_requests_agent       ON api_requests(agent_run_id) WHERE agent_run_id IS NOT NULL;

-- Daily usage aggregates (materialised for billing and dashboards)

CREATE TABLE usage_daily (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID NOT NULL REFERENCES users(id),
    provider_id         UUID NOT NULL REFERENCES providers(id),
    date                DATE NOT NULL,

    request_count       INTEGER NOT NULL DEFAULT 0,
    error_count         INTEGER NOT NULL DEFAULT 0,
    tokens_input        BIGINT NOT NULL DEFAULT 0,
    tokens_output       BIGINT NOT NULL DEFAULT 0,
    total_cost_usd      NUMERIC(12, 4) NOT NULL DEFAULT 0,
    avg_latency_ms      NUMERIC(8, 2),

    UNIQUE(user_id, provider_id, date)
);

CREATE INDEX idx_usage_daily_user ON usage_daily(user_id, date DESC);
CREATE INDEX idx_usage_daily_date ON usage_daily(date DESC);

-- Provider health snapshots

CREATE TABLE provider_health (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL REFERENCES providers(id),
    status          VARCHAR(32) NOT NULL
                        CHECK (status IN ('healthy', 'degraded', 'unavailable')),
    latency_p50_ms  INTEGER,
    latency_p99_ms  INTEGER,
    error_rate      NUMERIC(5, 4),
    circuit_breaker VARCHAR(16) NOT NULL DEFAULT 'CLOSED'
                        CHECK (circuit_breaker IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
    sampled_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_provider_health_provider ON provider_health(provider_id, sampled_at DESC);

-- Billing records

CREATE TABLE billing_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id),
    type            VARCHAR(64) NOT NULL
                        CHECK (type IN ('charge', 'credit', 'refund', 'threshold_warning', 'budget_exceeded')),
    amount_usd      NUMERIC(12, 4) NOT NULL DEFAULT 0,
    description     TEXT,
    stripe_event_id VARCHAR(128) UNIQUE,
    period_start    DATE,
    period_end      DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_billing_events_user ON billing_events(user_id, created_at DESC);

-- Materialised view: per-user cost summary

CREATE MATERIALIZED VIEW mv_user_cost_summary AS
SELECT
    u.id                                        AS user_id,
    u.email,
    u.plan,
    COALESCE(SUM(ud.total_cost_usd), 0)         AS total_cost_usd,
    COALESCE(SUM(ud.request_count), 0)          AS total_requests,
    MAX(ud.date)                                AS last_active_date
FROM users u
LEFT JOIN usage_daily ud ON ud.user_id = u.id
GROUP BY u.id, u.email, u.plan
WITH DATA;

CREATE UNIQUE INDEX idx_mv_user_cost_user ON mv_user_cost_summary(user_id);

-- Refresh job (called by coreai.billing on schedule)
COMMENT ON MATERIALIZED VIEW mv_user_cost_summary IS 'Refreshed hourly by coreai.billing worker';

COMMIT;