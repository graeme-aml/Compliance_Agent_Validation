"""
Alerting — maps evaluation results and drift signals to monitoring_alerts rows
and dispatches to Jira CMT and (optionally) Slack.

Alert conditions from the monitoring architecture:
  CRITICAL — pass rate drops below threshold on any High-weight dimension
           — new agent version deployed without version record
  HIGH     — override rate spikes >2x baseline in 7-day window
           — statistical drift on escalation rate or classification distribution
  MEDIUM   — invocation log gap >1 hour for High-tier agent during business hours
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from rule_checks import CheckResult, DIMENSION_WEIGHT
from drift_monitor import DriftResult


# ---------------------------------------------------------------------------
# Configuration (override via env vars)
# ---------------------------------------------------------------------------
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://lithichq.atlassian.net")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "CMT")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL", "")

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

BSA_OFFICER_EMAIL = os.getenv("BSA_OFFICER_EMAIL", "")
ENG_SLACK_CHANNEL = os.getenv("ENG_SLACK_CHANNEL", "#compliance-eng-alerts")


@dataclass
class MonitoringAlert:
    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str = ""
    severity: str = "HIGH"          # CRITICAL | HIGH | MEDIUM | LOW
    trigger_type: str = ""          # PASS_RATE_DROP | OVERRIDE_SPIKE | DRIFT_DETECTED | LOG_GAP | VERSION_UNREGISTERED
    title: str = ""
    description: str = ""
    run_id: str | None = None
    jira_issue_key: str | None = None
    status: str = "OPEN"
    notified_bsa_officer: bool = False
    notified_eng: bool = False
    agent_suspended: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_db_row(self) -> dict:
        return {
            "ALERT_ID": self.alert_id,
            "AGENT_NAME": self.agent_name,
            "SEVERITY": self.severity,
            "TRIGGER_TYPE": self.trigger_type,
            "TITLE": self.title,
            "DESCRIPTION": self.description,
            "RUN_ID": self.run_id,
            "JIRA_ISSUE_KEY": self.jira_issue_key,
            "STATUS": self.status,
            "NOTIFIED_BSA_OFFICER": self.notified_bsa_officer,
            "NOTIFIED_ENG": self.notified_eng,
            "AGENT_SUSPENDED": self.agent_suspended,
            "CREATED_AT": self.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Alert factory — classify check/drift results into alerts
# ---------------------------------------------------------------------------

def alerts_from_check_results(results: list[CheckResult], run_id: str) -> list[MonitoringAlert]:
    alerts = []
    for r in results:
        if r.passed:
            continue
        weight = DIMENSION_WEIGHT.get(r.scorecard_dimension, "medium")
        if weight == "high":
            severity = "CRITICAL"
        else:
            severity = "HIGH"

        alert = MonitoringAlert(
            agent_name=r.agent_name,
            severity=severity,
            trigger_type="PASS_RATE_DROP",
            title=f"{r.agent_name}: {r.check_name} below threshold ({r.pass_rate:.1%} < {r.threshold:.1%})",
            description=(
                f"Check '{r.check_name}' (dimension: {r.scorecard_dimension}) failed.\n"
                f"Pass rate: {r.pass_rate:.1%} | Threshold: {r.threshold:.1%} | "
                f"Checked: {r.invocations_checked} invocations | Failed: {r.failed_count}\n"
                f"Top failures: {json.dumps(r.failure_details[:5])}"
            ),
            run_id=run_id,
        )
        alerts.append(alert)
    return alerts


def alerts_from_drift_results(drift_results: list[DriftResult], run_id: str) -> list[MonitoringAlert]:
    alerts = []
    for d in drift_results:
        if not d.drift_detected:
            continue
        severity = "CRITICAL" if d.monitor_name == "override_rate" and (d.current_value or 0) > 0.5 else "HIGH"
        alert = MonitoringAlert(
            agent_name=d.agent_name,
            severity=severity,
            trigger_type="DRIFT_DETECTED",
            title=f"{d.agent_name}: drift detected on {d.monitor_name} ({d.deviation_pct:.1f}% deviation)",
            description=(
                f"Statistical drift on '{d.monitor_name}'.\n"
                f"Current: {d.current_value} | Baseline: {d.baseline_value} | "
                f"Deviation: {d.deviation_pct:.1f}%\nDetails: {json.dumps(d.details)}"
            ),
            run_id=run_id,
        )
        alerts.append(alert)
    return alerts


def alert_log_gap(agent_name: str, gap_hours: float) -> MonitoringAlert:
    return MonitoringAlert(
        agent_name=agent_name,
        severity="MEDIUM",
        trigger_type="LOG_GAP",
        title=f"{agent_name}: invocation log gap detected ({gap_hours:.1f}h)",
        description=(
            f"No invocation records received for {gap_hours:.1f} hours during business hours. "
            "Possible instrumentation failure or agent downtime."
        ),
    )


def alert_unregistered_version(agent_name: str, detected_version_hint: str) -> MonitoringAlert:
    return MonitoringAlert(
        agent_name=agent_name,
        severity="CRITICAL",
        trigger_type="VERSION_UNREGISTERED",
        title=f"{agent_name}: invocation logged with unregistered version",
        description=(
            f"Invocation recorded referencing a version not in agent_versions. "
            f"Hint: {detected_version_hint}. Invocations flagged pending investigation."
        ),
    )


# ---------------------------------------------------------------------------
# Jira CMT dispatcher
# ---------------------------------------------------------------------------

def _jira_priority(severity: str) -> str:
    return {"CRITICAL": "Highest", "HIGH": "High", "MEDIUM": "Medium", "LOW": "Low"}.get(severity, "Medium")


def _jira_issue_type(severity: str) -> str:
    return "Bug" if severity in ("CRITICAL", "HIGH") else "Task"


def create_jira_issue(alert: MonitoringAlert) -> str | None:
    """Creates a Jira CMT issue. Returns the issue key (e.g. 'CMT-123') or None on failure."""
    if not JIRA_API_TOKEN or not JIRA_USER_EMAIL:
        print(f"[JIRA] Skipped (no credentials) — would create: {alert.title}")
        return None

    url = f"{JIRA_BASE_URL}/rest/api/3/issue"
    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": f"[AI Agent Monitor] {alert.title}",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": alert.description}]}],
            },
            "issuetype": {"name": _jira_issue_type(alert.severity)},
            "priority": {"name": _jira_priority(alert.severity)},
            "labels": ["ai-agent-monitoring", alert.agent_name, alert.trigger_type.lower()],
        }
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            auth=(JIRA_USER_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        key = resp.json().get("key")
        print(f"[JIRA] Created {key}: {alert.title}")
        return key
    except Exception as e:
        print(f"[JIRA] Failed to create issue: {e}")
        return None


# ---------------------------------------------------------------------------
# Slack dispatcher
# ---------------------------------------------------------------------------

def send_slack_alert(alert: MonitoringAlert, channel: str | None = None):
    if not SLACK_WEBHOOK_URL:
        print(f"[SLACK] Skipped (no webhook) — {alert.severity}: {alert.title}")
        return

    color = {"CRITICAL": "#FF0000", "HIGH": "#FF6600", "MEDIUM": "#FFCC00", "LOW": "#36A64F"}.get(alert.severity, "#808080")
    payload = {
        "channel": channel or ENG_SLACK_CHANNEL,
        "attachments": [{
            "color": color,
            "title": f"[{alert.severity}] {alert.title}",
            "text": alert.description[:500],
            "fields": [
                {"title": "Agent", "value": alert.agent_name, "short": True},
                {"title": "Trigger", "value": alert.trigger_type, "short": True},
                {"title": "Jira", "value": alert.jira_issue_key or "pending", "short": True},
            ],
            "footer": "AI Agent Production Monitoring",
            "ts": int(alert.created_at.timestamp()),
        }],
    }
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"[SLACK] Failed to send alert: {e}")


# ---------------------------------------------------------------------------
# Persist alert to Snowflake
# ---------------------------------------------------------------------------

def write_alert(conn, alert: MonitoringAlert):
    sql = """
        INSERT INTO compliance.ai_agent_monitoring.monitoring_alerts
        (alert_id, agent_name, severity, trigger_type, title, description,
         run_id, jira_issue_key, status, notified_bsa_officer, notified_eng,
         agent_suspended, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::TIMESTAMP_TZ)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            alert.alert_id, alert.agent_name, alert.severity, alert.trigger_type,
            alert.title, alert.description, alert.run_id, alert.jira_issue_key,
            alert.status, alert.notified_bsa_officer, alert.notified_eng,
            alert.agent_suspended, alert.created_at.isoformat(),
        ))


def suspend_agent(conn, agent_name: str):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE compliance.ai_agent_monitoring.agent_registry
            SET status = 'SUSPENDED', updated_at = CURRENT_TIMESTAMP()
            WHERE agent_name = %s
        """, (agent_name,))


# ---------------------------------------------------------------------------
# Dispatch — full alert lifecycle for a batch of results
# ---------------------------------------------------------------------------

def dispatch_alerts(
    conn,
    check_results: list[CheckResult],
    drift_results: list[DriftResult],
    run_id: str,
):
    """
    Convert check and drift results to alerts, create Jira issues,
    notify channels, persist to Snowflake, and suspend CRITICAL agents.
    """
    alerts = (
        alerts_from_check_results(check_results, run_id) +
        alerts_from_drift_results(drift_results, run_id)
    )

    for alert in alerts:
        # Create Jira issue
        key = create_jira_issue(alert)
        if key:
            alert.jira_issue_key = key

        # Slack notification
        if alert.severity == "CRITICAL":
            send_slack_alert(alert, channel="#compliance-critical")
            suspend_agent(conn, alert.agent_name)
            alert.agent_suspended = True
            alert.notified_bsa_officer = True
        elif alert.severity == "HIGH":
            send_slack_alert(alert, channel=ENG_SLACK_CHANNEL)
            alert.notified_eng = True

        # Persist
        write_alert(conn, alert)

    return alerts
