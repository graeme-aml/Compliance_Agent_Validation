-- =============================================================================
-- AI Agent Production Monitoring — Snowflake Schema
-- compliance.ai_agent_monitoring
-- =============================================================================
-- All tables are append-only / immutable after write.
-- BSA 5-year retention enforced via Snowflake data retention + failsafe policy.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS compliance;
CREATE SCHEMA IF NOT EXISTS compliance.ai_agent_monitoring;

USE SCHEMA compliance.ai_agent_monitoring;

-- ---------------------------------------------------------------------------
-- 1. agent_versions
-- Immutable version registry. Every prompt/model/config change = new row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_versions (
    version_id         VARCHAR(64)   NOT NULL,   -- sha256(agent_name + prompt_hash + model + config_hash)
    agent_name         VARCHAR(255)  NOT NULL,
    version_tag        VARCHAR(64)   NOT NULL,   -- semver e.g. "1.0.0"
    model_id           VARCHAR(128)  NOT NULL,   -- e.g. "claude-sonnet-4-6"
    prompt_hash        VARCHAR(64)   NOT NULL,   -- sha256 of system prompt
    config_hash        VARCHAR(64)   NOT NULL,   -- sha256 of full config JSON
    system_prompt      TEXT,
    config_json        VARIANT,
    deployed_at        TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    deployed_by        VARCHAR(255),
    change_description TEXT,
    PRIMARY KEY (version_id)
);

-- ---------------------------------------------------------------------------
-- 2. agent_registry
-- Live status per agent. One row per agent, updated on each event.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_name          VARCHAR(255)  NOT NULL,
    domain              VARCHAR(64)   NOT NULL,   -- TRANSACTION_MONITORING | KYC_CDD_EDD | SAR_UAR_FILING | OSINT_INVESTIGATION | SANCTIONS_SCREENING | REGULATORY_RESEARCH
    risk_tier           VARCHAR(16)   NOT NULL,   -- HIGH | MEDIUM | LOW
    current_version_id  VARCHAR(64)   REFERENCES agent_versions(version_id),
    status              VARCHAR(32)   NOT NULL DEFAULT 'PENDING_VALIDATION', -- APPROVED | CONDITIONAL | NEEDS_REMEDIATION | PENDING_VALIDATION | UNDER_REVIEW | SUSPENDED
    owner               VARCHAR(255),
    last_validated_at   TIMESTAMP_TZ,
    next_validation_due DATE,
    updated_at          TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (agent_name)
);

-- Seed pilot agent
INSERT INTO agent_registry (agent_name, domain, risk_tier, status, owner, next_validation_due)
SELECT 'osint-kyc-kyb-search', 'OSINT_INVESTIGATION', 'MEDIUM', 'PENDING_VALIDATION', 'Compliance Testing', CURRENT_DATE()
WHERE NOT EXISTS (SELECT 1 FROM agent_registry WHERE agent_name = 'osint-kyc-kyb-search');

-- ---------------------------------------------------------------------------
-- 3. agent_invocation_log
-- Core execution record. Written at moment of agent call. Immutable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_invocation_log (
    invocation_id      VARCHAR(64)   NOT NULL,   -- uuid
    agent_name         VARCHAR(255)  NOT NULL,
    agent_version_id   VARCHAR(64)   NOT NULL REFERENCES agent_versions(version_id),
    user_id            VARCHAR(255),             -- analyst who triggered the call
    session_id         VARCHAR(64),
    input_hash         VARCHAR(64)   NOT NULL,   -- sha256 of PII-tokenized input
    input_token_count  INTEGER,
    raw_output         TEXT,
    output_token_count INTEGER,
    structured_signals VARIANT,                  -- parsed from raw_output at log time
    tool_calls         VARIANT,                  -- list of tool calls made
    latency_ms         INTEGER,
    model_id           VARCHAR(128),
    stop_reason        VARCHAR(64),
    error_code         VARCHAR(64),
    invoked_at         TIMESTAMP_TZ  NOT NULL,
    ingested_at        TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (invocation_id)
);

-- ---------------------------------------------------------------------------
-- 4. agent_output_signals
-- Structured signals extracted from raw output per domain schema.
-- One row per invocation (upsertable after parse, immutable thereafter).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_output_signals (
    invocation_id          VARCHAR(64)   NOT NULL REFERENCES agent_invocation_log(invocation_id),
    agent_name             VARCHAR(255)  NOT NULL,
    domain                 VARCHAR(64)   NOT NULL,
    escalation_recommended BOOLEAN,              -- agent recommended escalation/UAR
    escalation_signal      VARCHAR(64),          -- ESCALATE | NO_ACTION | NEEDS_REVIEW | REFUSED
    citation_count         INTEGER,
    citations_present      BOOLEAN,
    required_fields_present BOOLEAN,
    out_of_scope_refused   BOOLEAN,
    confidence_level       VARCHAR(16),          -- HIGH | MEDIUM | LOW | UNCERTAIN
    domain_signals         VARIANT,              -- domain-specific structured fields
    parsed_at              TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (invocation_id)
);

-- ---------------------------------------------------------------------------
-- 5. analyst_feedback
-- Override and annotation records from analyst review workflow.
-- Tied to specific invocation_id for ground-truth accuracy measurement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analyst_feedback (
    feedback_id       VARCHAR(64)   NOT NULL,    -- uuid
    invocation_id     VARCHAR(64)   NOT NULL REFERENCES agent_invocation_log(invocation_id),
    agent_name        VARCHAR(255)  NOT NULL,
    analyst_id        VARCHAR(255)  NOT NULL,
    action            VARCHAR(32)   NOT NULL,    -- ACCEPTED | MODIFIED | REJECTED | ESCALATED_OVERRIDE | CLOSED_OVERRIDE
    reason_code       VARCHAR(64),               -- INACCURATE | INCOMPLETE | OVER_ESCALATED | UNDER_ESCALATED | REGULATORY_GAP | OTHER
    modification_summary TEXT,
    original_escalation  BOOLEAN,
    analyst_escalation   BOOLEAN,
    submitted_at      TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (feedback_id)
);

-- ---------------------------------------------------------------------------
-- 6. evaluation_run_results
-- Results of each automated evaluation run per agent per check.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evaluation_run_results (
    run_id              VARCHAR(64)   NOT NULL,   -- uuid per evaluation run
    agent_name          VARCHAR(255)  NOT NULL,
    agent_version_id    VARCHAR(64),
    run_type            VARCHAR(32)   NOT NULL,   -- SCHEDULED | TRIGGERED | POST_INCIDENT
    check_name          VARCHAR(128)  NOT NULL,   -- e.g. "required_fields_present"
    scorecard_dimension VARCHAR(128)  NOT NULL,   -- maps to testing framework dimension
    invocations_checked INTEGER,
    passed_count        INTEGER,
    failed_count        INTEGER,
    pass_rate           FLOAT,
    threshold           FLOAT,
    passed              BOOLEAN,
    failure_details     VARIANT,                  -- list of failing invocation_ids + reasons
    drift_detected      BOOLEAN       DEFAULT FALSE,
    drift_details       VARIANT,
    evaluated_at        TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    window_start        TIMESTAMP_TZ,
    window_end          TIMESTAMP_TZ,
    PRIMARY KEY (run_id, check_name)
);

-- ---------------------------------------------------------------------------
-- 7. monitoring_alerts
-- Alerts generated by the evaluation engine, routed and resolved here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring_alerts (
    alert_id          VARCHAR(64)   NOT NULL,    -- uuid
    agent_name        VARCHAR(255)  NOT NULL,
    severity          VARCHAR(16)   NOT NULL,    -- CRITICAL | HIGH | MEDIUM | LOW
    trigger_type      VARCHAR(64)   NOT NULL,    -- PASS_RATE_DROP | OVERRIDE_SPIKE | DRIFT_DETECTED | LOG_GAP | VERSION_UNREGISTERED
    title             VARCHAR(512)  NOT NULL,
    description       TEXT,
    run_id            VARCHAR(64),               -- evaluation run that triggered this
    jira_issue_key    VARCHAR(64),               -- e.g. "CMT-123"
    status            VARCHAR(32)   NOT NULL DEFAULT 'OPEN',   -- OPEN | ACKNOWLEDGED | RESOLVED
    notified_bsa_officer BOOLEAN    DEFAULT FALSE,
    notified_eng      BOOLEAN       DEFAULT FALSE,
    agent_suspended   BOOLEAN       DEFAULT FALSE,
    created_at        TIMESTAMP_TZ  NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    resolved_at       TIMESTAMP_TZ,
    resolved_by       VARCHAR(255),
    resolution_notes  TEXT,
    PRIMARY KEY (alert_id)
);


-- =============================================================================
-- VIEWS
-- =============================================================================

-- Live agent registry — primary dashboard source
CREATE OR REPLACE VIEW vw_live_agent_registry AS
WITH latest_eval AS (
    SELECT
        agent_name,
        AVG(pass_rate)                                              AS live_pass_rate,
        COUNT_IF(NOT passed)                                        AS failing_checks,
        MAX(evaluated_at)                                           AS last_evaluated_at
    FROM evaluation_run_results
    WHERE evaluated_at >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    GROUP BY agent_name
),
override_rate AS (
    SELECT
        agent_name,
        COUNT_IF(action IN ('MODIFIED','REJECTED','ESCALATED_OVERRIDE','CLOSED_OVERRIDE'))
            / NULLIF(COUNT(*), 0)                                   AS override_rate_30d,
        COUNT(*)                                                    AS feedback_count_30d
    FROM analyst_feedback
    WHERE submitted_at >= DATEADD('day', -30, CURRENT_TIMESTAMP())
    GROUP BY agent_name
),
open_alerts AS (
    SELECT agent_name, COUNT(*) AS open_alert_count
    FROM monitoring_alerts
    WHERE status = 'OPEN'
    GROUP BY agent_name
)
SELECT
    r.agent_name,
    r.domain,
    r.risk_tier,
    r.status,
    r.owner,
    v.version_tag                                                   AS current_version,
    v.model_id,
    COALESCE(e.live_pass_rate, NULL)                                AS live_pass_rate,
    COALESCE(e.failing_checks, 0)                                   AS failing_checks,
    COALESCE(o.override_rate_30d, 0)                                AS override_rate_30d,
    COALESCE(o.feedback_count_30d, 0)                               AS feedback_count_30d,
    COALESCE(a.open_alert_count, 0)                                 AS open_alert_count,
    r.last_validated_at,
    r.next_validation_due,
    e.last_evaluated_at,
    r.updated_at
FROM agent_registry r
LEFT JOIN agent_versions v ON r.current_version_id = v.version_id
LEFT JOIN latest_eval e    ON r.agent_name = e.agent_name
LEFT JOIN override_rate o  ON r.agent_name = o.agent_name
LEFT JOIN open_alerts a    ON r.agent_name = a.agent_name;


-- Monthly AI Agent Testing Dashboard — auto-generates the monthly report
CREATE OR REPLACE VIEW vw_monthly_testing_dashboard AS
WITH monthly_window AS (
    SELECT DATEADD('day', -30, CURRENT_TIMESTAMP()) AS window_start
),
eval_summary AS (
    SELECT
        e.agent_name,
        e.scorecard_dimension,
        AVG(e.pass_rate)          AS avg_pass_rate,
        MIN(e.pass_rate)          AS min_pass_rate,
        COUNT_IF(NOT e.passed)    AS failed_runs,
        COUNT(*)                  AS total_runs
    FROM evaluation_run_results e, monthly_window w
    WHERE e.evaluated_at >= w.window_start
    GROUP BY e.agent_name, e.scorecard_dimension
),
alert_summary AS (
    SELECT
        agent_name,
        COUNT(*)                               AS total_alerts,
        COUNT_IF(severity = 'CRITICAL')        AS critical_alerts,
        COUNT_IF(severity = 'HIGH')            AS high_alerts,
        COUNT_IF(status = 'OPEN')              AS open_alerts
    FROM monitoring_alerts, monthly_window w
    WHERE created_at >= w.window_start
    GROUP BY agent_name
)
SELECT
    r.agent_name,
    r.domain,
    r.risk_tier,
    r.status,
    e.scorecard_dimension,
    e.avg_pass_rate,
    e.min_pass_rate,
    e.failed_runs,
    e.total_runs,
    COALESCE(a.total_alerts, 0)    AS total_alerts,
    COALESCE(a.critical_alerts, 0) AS critical_alerts,
    COALESCE(a.high_alerts, 0)     AS high_alerts,
    COALESCE(a.open_alerts, 0)     AS open_alerts,
    r.last_validated_at,
    r.next_validation_due
FROM agent_registry r
LEFT JOIN eval_summary e    ON r.agent_name = e.agent_name
LEFT JOIN alert_summary a   ON r.agent_name = a.agent_name
ORDER BY r.risk_tier, r.agent_name, e.scorecard_dimension;


-- Examiner-ready package — single view for examination response
CREATE OR REPLACE VIEW vw_examiner_package AS
SELECT
    r.agent_name,
    r.domain,
    r.risk_tier,
    r.status                                                        AS validation_status,
    r.owner,
    v.version_tag,
    v.model_id,
    v.deployed_at                                                   AS current_version_deployed,
    r.last_validated_at,
    r.next_validation_due,
    COALESCE(open_a.open_alert_count, 0)                            AS open_alerts,
    COALESCE(open_a.critical_count, 0)                              AS critical_open_alerts,
    DATEDIFF('day', r.last_validated_at, CURRENT_DATE())            AS days_since_last_validation
FROM agent_registry r
LEFT JOIN agent_versions v ON r.current_version_id = v.version_id
LEFT JOIN (
    SELECT
        agent_name,
        COUNT(*)                          AS open_alert_count,
        COUNT_IF(severity = 'CRITICAL')   AS critical_count
    FROM monitoring_alerts WHERE status = 'OPEN'
    GROUP BY agent_name
) open_a ON r.agent_name = open_a.agent_name
ORDER BY
    CASE r.risk_tier WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
    r.agent_name;
