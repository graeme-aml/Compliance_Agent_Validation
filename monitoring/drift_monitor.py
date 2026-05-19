"""
Statistical drift monitors — detect distributional shifts between the current
window and the validated baseline. Triggers before any individual output has
been manually reviewed.

Monitors:
  1. Escalation rate drift       (vs. validated baseline)
  2. Output length anomaly       (mean/stddev shift)
  3. Classification distribution (escalation_signal label proportions)
  4. Override rate spike         (analyst feedback override rate 2x baseline)
  5. Latency spike               (p95 latency vs. baseline)
"""

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DriftResult:
    agent_name: str
    monitor_name: str
    drift_detected: bool
    current_value: float | None
    baseline_value: float | None
    deviation_pct: float | None
    details: dict = field(default_factory=dict)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "monitor_name": self.monitor_name,
            "drift_detected": self.drift_detected,
            "current_value": self.current_value,
            "baseline_value": self.baseline_value,
            "deviation_pct": self.deviation_pct,
            "details": self.details,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


def _pct_deviation(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return abs(current - baseline) / baseline * 100


def _fetch_scalar(conn, sql: str, params: tuple) -> float | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# 1. Escalation rate drift
# ---------------------------------------------------------------------------
ESCALATION_DRIFT_THRESHOLD_PCT = 25.0  # flag if escalation rate shifts >25%

def monitor_escalation_rate(conn, agent_name: str,
                             current_start: datetime, current_end: datetime,
                             baseline_start: datetime, baseline_end: datetime) -> DriftResult:
    rate_sql = """
        SELECT COUNT_IF(escalation_recommended = TRUE) / NULLIF(COUNT(*), 0)
        FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s AND parsed_at BETWEEN %s AND %s
    """
    current_rate = _fetch_scalar(conn, rate_sql,
                                  (agent_name, current_start.isoformat(), current_end.isoformat()))
    baseline_rate = _fetch_scalar(conn, rate_sql,
                                   (agent_name, baseline_start.isoformat(), baseline_end.isoformat()))

    if current_rate is None or baseline_rate is None:
        return DriftResult(agent_name, "escalation_rate", False, current_rate, baseline_rate, None,
                           {"note": "insufficient_data"})

    dev = _pct_deviation(current_rate, baseline_rate)
    drift = dev is not None and dev > ESCALATION_DRIFT_THRESHOLD_PCT

    return DriftResult(
        agent_name=agent_name,
        monitor_name="escalation_rate",
        drift_detected=drift,
        current_value=current_rate,
        baseline_value=baseline_rate,
        deviation_pct=dev,
        details={
            "threshold_pct": ESCALATION_DRIFT_THRESHOLD_PCT,
            "current_window": f"{current_start.date()} – {current_end.date()}",
            "baseline_window": f"{baseline_start.date()} – {baseline_end.date()}",
        },
    )


# ---------------------------------------------------------------------------
# 2. Output length anomaly
# ---------------------------------------------------------------------------
OUTPUT_LENGTH_SIGMA_THRESHOLD = 2.0  # flag if current mean is >2σ from baseline mean

def monitor_output_length(conn, agent_name: str,
                           current_start: datetime, current_end: datetime,
                           baseline_start: datetime, baseline_end: datetime) -> DriftResult:
    stats_sql = """
        SELECT AVG(LEN(raw_output)), STDDEV(LEN(raw_output))
        FROM compliance.ai_agent_monitoring.agent_invocation_log
        WHERE agent_name = %s AND invoked_at BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(stats_sql, (agent_name, baseline_start.isoformat(), baseline_end.isoformat()))
        row = cur.fetchone()
        baseline_mean = float(row[0]) if row and row[0] is not None else None
        baseline_std = float(row[1]) if row and row[1] is not None else None

        cur.execute(stats_sql, (agent_name, current_start.isoformat(), current_end.isoformat()))
        row = cur.fetchone()
        current_mean = float(row[0]) if row and row[0] is not None else None

    if current_mean is None or baseline_mean is None or baseline_std is None or baseline_std == 0:
        return DriftResult(agent_name, "output_length", False, current_mean, baseline_mean, None,
                           {"note": "insufficient_data"})

    sigma_distance = abs(current_mean - baseline_mean) / baseline_std
    drift = sigma_distance > OUTPUT_LENGTH_SIGMA_THRESHOLD
    dev = _pct_deviation(current_mean, baseline_mean)

    return DriftResult(
        agent_name=agent_name,
        monitor_name="output_length",
        drift_detected=drift,
        current_value=current_mean,
        baseline_value=baseline_mean,
        deviation_pct=dev,
        details={
            "sigma_distance": round(sigma_distance, 2),
            "baseline_std": round(baseline_std, 2),
            "threshold_sigma": OUTPUT_LENGTH_SIGMA_THRESHOLD,
        },
    )


# ---------------------------------------------------------------------------
# 3. Classification distribution drift (escalation_signal label proportions)
# ---------------------------------------------------------------------------
DISTRIBUTION_DRIFT_THRESHOLD_PCT = 30.0

def monitor_classification_distribution(conn, agent_name: str,
                                         current_start: datetime, current_end: datetime,
                                         baseline_start: datetime, baseline_end: datetime) -> DriftResult:
    dist_sql = """
        SELECT escalation_signal, COUNT(*) / SUM(COUNT(*)) OVER () AS proportion
        FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s AND parsed_at BETWEEN %s AND %s
        GROUP BY escalation_signal
    """

    def fetch_dist(start, end):
        with conn.cursor() as cur:
            cur.execute(dist_sql, (agent_name, start.isoformat(), end.isoformat()))
            return {r[0]: float(r[1]) for r in cur.fetchall()}

    current_dist = fetch_dist(current_start, current_end)
    baseline_dist = fetch_dist(baseline_start, baseline_end)

    if not current_dist or not baseline_dist:
        return DriftResult(agent_name, "classification_distribution", False, None, None, None,
                           {"note": "insufficient_data"})

    max_dev = 0.0
    label_deviations = {}
    for label in set(list(current_dist.keys()) + list(baseline_dist.keys())):
        c = current_dist.get(label, 0.0)
        b = baseline_dist.get(label, 0.0)
        dev = _pct_deviation(c, b) if b > 0 else (100.0 if c > 0 else 0.0)
        label_deviations[label] = {"current": round(c, 3), "baseline": round(b, 3), "deviation_pct": dev}
        if dev is not None and dev > max_dev:
            max_dev = dev

    drift = max_dev > DISTRIBUTION_DRIFT_THRESHOLD_PCT

    return DriftResult(
        agent_name=agent_name,
        monitor_name="classification_distribution",
        drift_detected=drift,
        current_value=max_dev,
        baseline_value=None,
        deviation_pct=max_dev,
        details={"label_deviations": label_deviations, "threshold_pct": DISTRIBUTION_DRIFT_THRESHOLD_PCT},
    )


# ---------------------------------------------------------------------------
# 4. Override rate spike (2x baseline in 7-day window)
# ---------------------------------------------------------------------------
OVERRIDE_SPIKE_MULTIPLIER = 2.0

def monitor_override_rate(conn, agent_name: str,
                           current_start: datetime, current_end: datetime,
                           baseline_start: datetime, baseline_end: datetime) -> DriftResult:
    rate_sql = """
        SELECT
            COUNT_IF(action IN ('MODIFIED','REJECTED','ESCALATED_OVERRIDE','CLOSED_OVERRIDE'))
            / NULLIF(COUNT(*), 0)
        FROM compliance.ai_agent_monitoring.analyst_feedback
        WHERE agent_name = %s AND submitted_at BETWEEN %s AND %s
    """
    current_rate = _fetch_scalar(conn, rate_sql,
                                  (agent_name, current_start.isoformat(), current_end.isoformat()))
    baseline_rate = _fetch_scalar(conn, rate_sql,
                                   (agent_name, baseline_start.isoformat(), baseline_end.isoformat()))

    if current_rate is None or baseline_rate is None:
        return DriftResult(agent_name, "override_rate", False, current_rate, baseline_rate, None,
                           {"note": "insufficient_data"})

    dev = _pct_deviation(current_rate, baseline_rate)
    drift = (baseline_rate > 0 and current_rate >= baseline_rate * OVERRIDE_SPIKE_MULTIPLIER) or \
            (baseline_rate == 0 and current_rate > 0.2)

    return DriftResult(
        agent_name=agent_name,
        monitor_name="override_rate",
        drift_detected=drift,
        current_value=current_rate,
        baseline_value=baseline_rate,
        deviation_pct=dev,
        details={
            "spike_multiplier_threshold": OVERRIDE_SPIKE_MULTIPLIER,
            "current_window": f"{current_start.date()} – {current_end.date()}",
        },
    )


# ---------------------------------------------------------------------------
# 5. Latency spike (p95 vs baseline)
# ---------------------------------------------------------------------------
LATENCY_SPIKE_THRESHOLD_PCT = 50.0

def monitor_latency(conn, agent_name: str,
                    current_start: datetime, current_end: datetime,
                    baseline_start: datetime, baseline_end: datetime) -> DriftResult:
    p95_sql = """
        SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
        FROM compliance.ai_agent_monitoring.agent_invocation_log
        WHERE agent_name = %s AND invoked_at BETWEEN %s AND %s AND latency_ms IS NOT NULL
    """
    current_p95 = _fetch_scalar(conn, p95_sql,
                                 (agent_name, current_start.isoformat(), current_end.isoformat()))
    baseline_p95 = _fetch_scalar(conn, p95_sql,
                                  (agent_name, baseline_start.isoformat(), baseline_end.isoformat()))

    if current_p95 is None or baseline_p95 is None:
        return DriftResult(agent_name, "latency_p95", False, current_p95, baseline_p95, None,
                           {"note": "insufficient_data"})

    dev = _pct_deviation(current_p95, baseline_p95)
    drift = dev is not None and dev > LATENCY_SPIKE_THRESHOLD_PCT

    return DriftResult(
        agent_name=agent_name,
        monitor_name="latency_p95",
        drift_detected=drift,
        current_value=current_p95,
        baseline_value=baseline_p95,
        deviation_pct=dev,
        details={"threshold_pct": LATENCY_SPIKE_THRESHOLD_PCT, "unit": "ms"},
    )


ALL_DRIFT_MONITORS = [
    monitor_escalation_rate,
    monitor_output_length,
    monitor_classification_distribution,
    monitor_override_rate,
    monitor_latency,
]
