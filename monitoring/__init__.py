"""
Production monitoring layer — Layer 1–4 of the AI Agent Production Monitoring Architecture.

Modules:
  snowflake_schema.sql    — DDL for compliance.ai_agent_monitoring (7 tables + 3 views)
  invocation_logger.py   — Instrument agent calls to write append-only invocation records
  output_parser.py       — Extract structured signals from raw output per domain schema
  rule_checks.py         — Rule-based scorecard dimension checks against invocation log
  drift_monitor.py       — Statistical drift monitors (escalation rate, output length, etc.)
  evaluation_engine.py   — Orchestrates checks + drift, writes results, dispatches alerts
  alerting.py            — Jira CMT dispatcher + Slack notifications + agent suspension
  feedback_api.py        — FastAPI endpoint for analyst override/annotation capture
  dashboard/index.html   — Live agent registry dashboard (swap mock data for Snowflake view)
"""
