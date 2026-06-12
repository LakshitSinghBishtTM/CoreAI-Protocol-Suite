-- CoreAI Protocol Suite
-- PostgreSQL dump
-- Host: prod-db.r7Kp2x.us-east-1.rds.amazonaws.com
-- Database: coreai_production
-- Dumped: 2026-05-22 03:14:07 UTC
-- pg_dump version 15.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA public;

SET search_path = public, pg_catalog;
SET default_tablespace = '';
SET default_table_access_method = heap;

-- ============================================================
-- SCHEMA
-- ============================================================

CREATE TABLE public.providers (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    name            CHARACTER VARYING(64) NOT NULL,
    display_name    CHARACTER VARYING(128) NOT NULL,
    base_url        TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    priority        INTEGER NOT NULL DEFAULT 100,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT providers_pkey PRIMARY KEY (id),
    CONSTRAINT providers_name_key UNIQUE (name)
);

CREATE TABLE public.models (
    id                      UUID NOT NULL DEFAULT uuid_generate_v4(),
    provider_id             UUID NOT NULL,
    name                    CHARACTER VARYING(128) NOT NULL,
    display_name            CHARACTER VARYING(128) NOT NULL,
    context_window          INTEGER NOT NULL,
    max_output              INTEGER NOT NULL,
    cost_per_1k_input_usd   NUMERIC(10,6) NOT NULL DEFAULT 0,
    cost_per_1k_output_usd  NUMERIC(10,6) NOT NULL DEFAULT 0,
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    created_at              TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT models_pkey PRIMARY KEY (id),
    CONSTRAINT models_provider_name_key UNIQUE (provider_id, name),
    CONSTRAINT models_provider_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE CASCADE
);

CREATE TABLE public.users (
    id                  UUID NOT NULL DEFAULT uuid_generate_v4(),
    email               CHARACTER VARYING(320) NOT NULL,
    password_hash       CHARACTER VARYING(256) NOT NULL,
    plan                CHARACTER VARYING(32) NOT NULL DEFAULT 'free',
    monthly_budget_usd  NUMERIC(10,2),
    active              BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT users_pkey PRIMARY KEY (id),
    CONSTRAINT users_email_key UNIQUE (email),
    CONSTRAINT users_plan_check CHECK (plan IN ('free', 'pro', 'enterprise'))
);

CREATE TABLE public.api_keys (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    key_hash        CHARACTER VARYING(256) NOT NULL,
    key_prefix      CHARACTER VARYING(12) NOT NULL,
    name            CHARACTER VARYING(128),
    last_used_at    TIMESTAMP WITH TIME ZONE,
    expires_at      TIMESTAMP WITH TIME ZONE,
    revoked         BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT api_keys_pkey PRIMARY KEY (id),
    CONSTRAINT api_keys_hash_key UNIQUE (key_hash),
    CONSTRAINT api_keys_user_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE
);

CREATE TABLE public.routing_policies (
    id          UUID NOT NULL DEFAULT uuid_generate_v4(),
    name        CHARACTER VARYING(128) NOT NULL,
    description TEXT,
    strategy    CHARACTER VARYING(64) NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}',
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT routing_policies_pkey PRIMARY KEY (id),
    CONSTRAINT routing_policies_name_key UNIQUE (name),
    CONSTRAINT routing_policies_strategy_check CHECK (strategy IN ('cost_aware','latency_first','round_robin','explicit'))
);

CREATE TABLE public.agent_definitions (
    id                  UUID NOT NULL DEFAULT uuid_generate_v4(),
    name                CHARACTER VARYING(128) NOT NULL,
    description         TEXT,
    type                CHARACTER VARYING(64) NOT NULL,
    system_prompt       TEXT,
    max_steps           INTEGER NOT NULL DEFAULT 200,
    timeout_seconds     INTEGER NOT NULL DEFAULT 600,
    routing_policy_id   UUID,
    config              JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_definitions_pkey PRIMARY KEY (id),
    CONSTRAINT agent_definitions_policy_fkey FOREIGN KEY (routing_policy_id) REFERENCES public.routing_policies(id),
    CONSTRAINT agent_definitions_type_check CHECK (type IN ('research','code_execution','data_analysis','general'))
);

CREATE TABLE public.agent_runs (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    agent_id        UUID NOT NULL,
    user_id         UUID NOT NULL,
    status          CHARACTER VARYING(32) NOT NULL DEFAULT 'queued',
    steps_completed INTEGER NOT NULL DEFAULT 0,
    result          JSONB,
    error           TEXT,
    started_at      TIMESTAMP WITH TIME ZONE,
    completed_at    TIMESTAMP WITH TIME ZONE,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_runs_pkey PRIMARY KEY (id),
    CONSTRAINT agent_runs_agent_fkey FOREIGN KEY (agent_id) REFERENCES public.agent_definitions(id),
    CONSTRAINT agent_runs_user_fkey  FOREIGN KEY (user_id)  REFERENCES public.users(id),
    CONSTRAINT agent_runs_status_check CHECK (status IN ('queued','running','completed','failed','terminated'))
);

CREATE TABLE public.api_requests (
    id                      UUID NOT NULL DEFAULT uuid_generate_v4(),
    user_id                 UUID NOT NULL,
    api_key_id              UUID,
    provider_id             UUID NOT NULL,
    model_id                UUID NOT NULL,
    routing_policy_id       UUID,
    agent_run_id            UUID,
    request_id              CHARACTER VARYING(128),
    status_code             INTEGER NOT NULL,
    tokens_input            INTEGER NOT NULL DEFAULT 0,
    tokens_output           INTEGER NOT NULL DEFAULT 0,
    latency_ms              INTEGER,
    cost_usd                NUMERIC(12,8) NOT NULL DEFAULT 0,
    was_failover            BOOLEAN NOT NULL DEFAULT false,
    original_provider_id    UUID,
    requested_at            TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMP WITH TIME ZONE,
    CONSTRAINT api_requests_pkey       PRIMARY KEY (id),
    CONSTRAINT api_requests_req_id_key UNIQUE (request_id),
    CONSTRAINT api_requests_user_fkey     FOREIGN KEY (user_id)     REFERENCES public.users(id),
    CONSTRAINT api_requests_provider_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id),
    CONSTRAINT api_requests_model_fkey    FOREIGN KEY (model_id)    REFERENCES public.models(id)
) PARTITION BY RANGE (requested_at);

CREATE TABLE public.api_requests_2025_04 PARTITION OF public.api_requests
    FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00');
CREATE TABLE public.api_requests_2025_05 PARTITION OF public.api_requests
    FOR VALUES FROM ('2026-05-01 00:00:00+00') TO ('2026-06-01 00:00:00+00');
CREATE TABLE public.api_requests_2025_06 PARTITION OF public.api_requests
    FOR VALUES FROM ('2026-06-01 00:00:00+00') TO ('2026-07-01 00:00:00+00');

CREATE TABLE public.usage_daily (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    provider_id     UUID NOT NULL,
    date            DATE NOT NULL,
    request_count   INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    tokens_input    BIGINT NOT NULL DEFAULT 0,
    tokens_output   BIGINT NOT NULL DEFAULT 0,
    total_cost_usd  NUMERIC(12,4) NOT NULL DEFAULT 0,
    avg_latency_ms  NUMERIC(8,2),
    CONSTRAINT usage_daily_pkey PRIMARY KEY (id),
    CONSTRAINT usage_daily_unique UNIQUE (user_id, provider_id, date),
    CONSTRAINT usage_daily_user_fkey     FOREIGN KEY (user_id)     REFERENCES public.users(id),
    CONSTRAINT usage_daily_provider_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id)
);

CREATE TABLE public.provider_health (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    provider_id     UUID NOT NULL,
    status          CHARACTER VARYING(32) NOT NULL,
    latency_p50_ms  INTEGER,
    latency_p99_ms  INTEGER,
    error_rate      NUMERIC(5,4),
    circuit_breaker CHARACTER VARYING(16) NOT NULL DEFAULT 'CLOSED',
    sampled_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT provider_health_pkey PRIMARY KEY (id),
    CONSTRAINT provider_health_status_check  CHECK (status IN ('healthy','degraded','unavailable')),
    CONSTRAINT provider_health_cb_check      CHECK (circuit_breaker IN ('CLOSED','OPEN','HALF_OPEN')),
    CONSTRAINT provider_health_provider_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id)
);

CREATE TABLE public.billing_events (
    id              UUID NOT NULL DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    type            CHARACTER VARYING(64) NOT NULL,
    amount_usd      NUMERIC(12,4) NOT NULL DEFAULT 0,
    description     TEXT,
    stripe_event_id CHARACTER VARYING(128),
    period_start    DATE,
    period_end      DATE,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT billing_events_pkey            PRIMARY KEY (id),
    CONSTRAINT billing_events_stripe_key      UNIQUE (stripe_event_id),
    CONSTRAINT billing_events_user_fkey       FOREIGN KEY (user_id) REFERENCES public.users(id),
    CONSTRAINT billing_events_type_check      CHECK (type IN ('charge','credit','refund','threshold_warning','budget_exceeded'))
);

-- ============================================================
-- INDEXES
-- ============================================================

CREATE INDEX idx_providers_enabled          ON public.providers(enabled);
CREATE INDEX idx_models_provider            ON public.models(provider_id);
CREATE INDEX idx_models_enabled             ON public.models(enabled);
CREATE INDEX idx_users_email                ON public.users(email);
CREATE INDEX idx_users_active               ON public.users(active);
CREATE INDEX idx_api_keys_user              ON public.api_keys(user_id);
CREATE INDEX idx_api_keys_hash              ON public.api_keys(key_hash);
CREATE INDEX idx_api_keys_revoked           ON public.api_keys(revoked);
CREATE INDEX idx_agent_runs_user            ON public.agent_runs(user_id);
CREATE INDEX idx_agent_runs_status          ON public.agent_runs(status);
CREATE INDEX idx_api_requests_user          ON public.api_requests(user_id);
CREATE INDEX idx_api_requests_provider      ON public.api_requests(provider_id);
CREATE INDEX idx_api_requests_requested     ON public.api_requests(requested_at DESC);
CREATE INDEX idx_api_requests_agent         ON public.api_requests(agent_run_id) WHERE (agent_run_id IS NOT NULL);
CREATE INDEX idx_usage_daily_user           ON public.usage_daily(user_id, date DESC);
CREATE INDEX idx_usage_daily_date           ON public.usage_daily(date DESC);
CREATE INDEX idx_provider_health_provider   ON public.provider_health(provider_id, sampled_at DESC);
CREATE INDEX idx_billing_events_user        ON public.billing_events(user_id, created_at DESC);

-- ============================================================
-- FUNCTIONS & TRIGGERS
-- ============================================================

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_providers_updated_at
    BEFORE UPDATE ON public.providers
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_routing_policies_updated_at
    BEFORE UPDATE ON public.routing_policies
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_agent_definitions_updated_at
    BEFORE UPDATE ON public.agent_definitions
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================
-- DATA
-- ============================================================

COPY public.providers (id, name, display_name, base_url, enabled, priority, created_at, updated_at) FROM stdin;
a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	openai	OpenAI	https://api.openai.com/v1	t	10	2024-12-01 09:00:00+00	2026-05-21 14:32:11+00
b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	anthropic	Anthropic	https://api.anthropic.com/v1	t	20	2024-12-01 09:00:00+00	2026-05-20 08:17:44+00
c3d7e2f5-9a8b-4c03-d5f4-7e0a1b2c3d4f	gemini	Google Gemini	https://generativelanguage.googleapis.com/v1beta	t	30	2024-12-01 09:00:00+00	2026-04-14 11:02:58+00
d4c6f3a6-1b9c-4d14-e6a5-6f1b2c3d4e5a	grok	xAI Grok	https://api.x.ai/v1	t	40	2026-01-15 14:22:00+00	2026-05-18 19:44:07+00
e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	deepseek	DeepSeek	https://api.deepseek.com/v1	t	50	2026-01-15 14:22:00+00	2026-05-22 01:08:33+00
\.

COPY public.models (id, provider_id, name, display_name, context_window, max_output, cost_per_1k_input_usd, cost_per_1k_output_usd, enabled, created_at) FROM stdin;
f1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	gpt-4o	GPT-4o	128000	16384	0.005000	0.015000	t	2026-01-15 14:22:00+00
f2b3c4d5-e6f7-4a81-b2c3-d4e5f6a7b8c9	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	gpt-4o-mini	GPT-4o Mini	128000	16384	0.000150	0.000600	t	2026-01-15 14:22:00+00
f3c4d5e6-f7a8-4b92-c3d4-e5f6a7b8c9d0	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	o3-mini	o3-mini	200000	100000	0.001100	0.004400	t	2026-02-03 09:15:00+00
f4d5e6f7-a8b9-4c03-d4e5-f6a7b8c9d0e1	b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	claude-opus-4	Claude Opus 4	200000	32000	0.015000	0.075000	t	2026-03-12 10:00:00+00
f5e6f7a8-b9c0-4d14-e5f6-a7b8c9d0e1f2	b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	claude-sonnet-4	Claude Sonnet 4	200000	64000	0.003000	0.015000	t	2026-03-12 10:00:00+00
f6f7a8b9-c0d1-4e25-f6a7-b8c9d0e1f2a3	c3d7e2f5-9a8b-4c03-d5f4-7e0a1b2c3d4f	gemini-2.0-flash	Gemini 2.0 Flash	1048576	8192	0.000100	0.000400	t	2026-02-19 08:30:00+00
f7a8b9c0-d1e2-4f36-a7b8-c9d0e1f2a3b4	c3d7e2f5-9a8b-4c03-d5f4-7e0a1b2c3d4f	gemini-2.5-pro	Gemini 2.5 Pro	1048576	65536	0.001250	0.010000	t	2026-03-25 11:00:00+00
f8b9c0d1-e2f3-4a47-b8c9-d0e1f2a3b4c5	d4c6f3a6-1b9c-4d14-e6a5-6f1b2c3d4e5a	grok-3	Grok 3	131072	16384	0.003000	0.015000	t	2026-02-18 16:45:00+00
f9c0d1e2-f3a4-4b58-c9d0-e1f2a3b4c5d6	e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	deepseek-chat	DeepSeek Chat	65536	8192	0.000140	0.000280	t	2026-01-15 14:22:00+00
fad1e2f3-a4b5-4c69-d0e1-f2a3b4c5d6e7	e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	deepseek-reasoner	DeepSeek Reasoner	65536	8192	0.000550	0.002190	t	2026-01-28 09:00:00+00
\.

COPY public.users (id, email, password_hash, plan, monthly_budget_usd, active, created_at, updated_at) FROM stdin;
u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	lakshit@coreai.dev	$2b$12$nW3vKx9mQrL2pTwY8dHsF.uZeAoGiNjE7lPsBtYhDfCkW6qMnRxV	enterprise	\N	t	2024-12-01 09:00:00+00	2026-05-22 02:41:07+00
u2b3c4d5-e6f7-4a81-b2c3-d4e5f6a7b8c9	svc-agent-runtime@internal	$2b$12$T8vQw3nR7mKj2xPyL9dHs.4cBuZeAoGiNtVlEpMrXqWsYhDfCkP	enterprise	\N	t	2024-12-01 09:00:00+00	2026-05-22 03:00:01+00
u3c4d5e6-f7a8-4b92-c3d4-e5f6a7b8c9d0	svc-billing@internal	$2b$12$mN7vKx2pRqL8wTjY4dHsF.cBuZeAoGiNtVlEpMrXqWsYh9DfCkP	enterprise	\N	t	2024-12-01 09:00:00+00	2026-05-22 03:00:01+00
\.

-- 3,844 user rows omitted

COPY public.routing_policies (id, name, description, strategy, config, active, created_at, updated_at) FROM stdin;
r1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	default	Cost-aware routing with latency fallback	cost_aware	{"cost_weight": 0.7, "latency_weight": 0.3, "latency_threshold_ms": 2000}	t	2024-12-01 09:00:00+00	2026-05-01 11:22:34+00
r2b3c4d5-e6f7-4a81-b2c3-d4e5f6a7b8c9	low_latency	Latency-optimized, cost secondary	latency_first	{"max_latency_ms": 500, "fallback_strategy": "cost_aware"}	t	2026-01-15 14:22:00+00	2026-03-08 09:47:12+00
r3c4d5e6-f7a8-4b92-c3d4-e5f6a7b8c9d0	round_robin	Equal distribution across providers	round_robin	{"exclude_degraded": true}	t	2026-01-15 14:22:00+00	2026-01-15 14:22:00+00
r4d5e6f7-a8b9-4c03-d4e5-f6a7b8c9d0e1	high_quality	Route to highest capability model	explicit	{"provider": "anthropic", "model": "claude-opus-4", "fallback_chain": ["openai"]}	t	2026-02-03 09:15:00+00	2026-04-29 16:03:57+00
\.

COPY public.usage_daily (id, user_id, provider_id, date, request_count, error_count, tokens_input, tokens_output, total_cost_usd, avg_latency_ms) FROM stdin;
ud001	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	2026-05-22	1847	12	2847291	4109023	37.9100	287.4
ud002	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	2026-05-22	3012	41	7291048	9847234	412.1000	521.8
ud003	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	2026-05-22	2104	28	4829103	7201847	291.4400	891.2
ud004	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	c3d7e2f5-9a8b-4c03-d5f4-7e0a1b2c3d4f	2026-05-22	841	9	3847291	2904817	89.3200	431.1
ud005	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	d4c6f3a6-1b9c-4d14-e6a5-6f1b2c3d4e5a	2026-05-22	487	7	912847	1204917	16.4600	891.7
ud006	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	2026-05-21	1902	8	2941847	4201938	39.1200	291.2
ud007	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	2026-05-21	2847	19	6847291	9201847	389.4400	498.3
ud008	u1a2b3c4-d5e6-4f70-a1b2-c3d4e5f6a7b8	b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	2026-05-21	1984	14	4201847	6847291	274.1100	847.4
\.

-- 284,923 rows omitted

COPY public.provider_health (id, provider_id, status, latency_p50_ms, latency_p99_ms, error_rate, circuit_breaker, sampled_at) FROM stdin;
ph001	a1f3d9c2-7b4e-4f81-b3d2-9c8e7f6a5b4d	healthy	489	2103	0.0041	CLOSED	2026-05-22 03:00:00+00
ph002	b2e8f1d4-3c7a-4b92-c4e3-8d9f0a1b2c3e	degraded	912	8471	0.0312	OPEN	2026-05-22 03:00:00+00
ph003	c3d7e2f5-9a8b-4c03-d5f4-7e0a1b2c3d4f	healthy	421	1847	0.0021	CLOSED	2026-05-22 03:00:00+00
ph004	d4c6f3a6-1b9c-4d14-e6a5-6f1b2c3d4e5a	healthy	312	30001	0.0087	CLOSED	2026-05-22 03:00:00+00
ph005	e5b5a4b7-2c0d-4e25-f7b6-5a2c3d4e5f6b	healthy	201	847	0.0009	CLOSED	2026-05-22 03:00:00+00
\.

-- ============================================================
-- SEQUENCES & RESTORE
-- ============================================================

SELECT pg_catalog.setval('public.schema_migrations_id_seq', 2, true);

ALTER TABLE ONLY public.providers     ADD CONSTRAINT providers_pkey     PRIMARY KEY (id);
ALTER TABLE ONLY public.models        ADD CONSTRAINT models_pkey        PRIMARY KEY (id);
ALTER TABLE ONLY public.users         ADD CONSTRAINT users_pkey         PRIMARY KEY (id);
ALTER TABLE ONLY public.api_keys      ADD CONSTRAINT api_keys_pkey      PRIMARY KEY (id);
ALTER TABLE ONLY public.routing_policies ADD CONSTRAINT routing_policies_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.agent_runs    ADD CONSTRAINT agent_runs_pkey    PRIMARY KEY (id);
ALTER TABLE ONLY public.usage_daily   ADD CONSTRAINT usage_daily_pkey   PRIMARY KEY (id);
ALTER TABLE ONLY public.provider_health ADD CONSTRAINT provider_health_pkey PRIMARY KEY (id);
ALTER TABLE ONLY public.billing_events  ADD CONSTRAINT billing_events_pkey  PRIMARY KEY (id);

-- ============================================================
-- DUMP METADATA
-- ============================================================

-- pg_dump completed
-- duration:       142s
-- total_size:     18.4 GB
-- rows_exported:  312,847
-- checksum:       sha256:7f3n2kx9m4qrl8pt4b7n3kx9m2qrl8p4twye8dhsf3cbuzea0g1nie7lpsbt4f