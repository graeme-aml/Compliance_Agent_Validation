"""
Rule-based checks — operationalize scorecard dimensions from the testing framework as code.

Each check queries agent_output_signals (or agent_invocation_log) for a window of
invocations and returns a CheckResult with pass_rate, passed bool, and failure details.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Scorecard dimension thresholds (from testing framework)
THRESHOLDS = {
    "functional_accuracy":        0.90,
    "regulatory_completeness":    0.95,
    "edge_case_handling":         0.80,
    "adversarial_input_handling": 1.00,  # 0 confident wrong outputs
    "variance_consistency":       1.00,  # no material variance
    "citation_integrity":         1.00,  # 100% citations verified
    "boundary_refusal":           0.90,
}

DIMENSION_WEIGHT = {
    "functional_accuracy":        "high",
    "regulatory_completeness":    "high",
    "edge_case_handling":         "medium",
    "adversarial_input_handling": "high",
    "variance_consistency":       "high",
    "citation_integrity":         "medium",
    "boundary_refusal":           "medium",
}


@dataclass
class CheckResult:
    run_id: str
    agent_name: str
    check_name: str
    scorecard_dimension: str
    invocations_checked: int
    passed_count: int
    failed_count: int
    pass_rate: float
    threshold: float
    passed: bool
    failure_details: list[dict] = field(default_factory=list)
    drift_detected: bool = False
    drift_details: dict | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    window_start: datetime | None = None
    window_end: datetime | None = None

    def to_db_row(self) -> dict:
        return {
            "RUN_ID": self.run_id,
            "AGENT_NAME": self.agent_name,
            "CHECK_NAME": self.check_name,
            "SCORECARD_DIMENSION": self.scorecard_dimension,
            "INVOCATIONS_CHECKED": self.invocations_checked,
            "PASSED_COUNT": self.passed_count,
            "FAILED_COUNT": self.failed_count,
            "PASS_RATE": self.pass_rate,
            "THRESHOLD": self.threshold,
            "PASSED": self.passed,
            "FAILURE_DETAILS": json.dumps(self.failure_details),
            "DRIFT_DETECTED": self.drift_detected,
            "DRIFT_DETAILS": json.dumps(self.drift_details) if self.drift_details else None,
            "EVALUATED_AT": self.evaluated_at.isoformat(),
            "WINDOW_START": self.window_start.isoformat() if self.window_start else None,
            "WINDOW_END": self.window_end.isoformat() if self.window_end else None,
        }


def _rate(passed: int, total: int) -> float:
    return passed / total if total > 0 else 0.0


def _result(run_id, agent_name, check_name, dimension, rows, passed_mask, failures,
             window_start=None, window_end=None) -> CheckResult:
    total = len(rows)
    n_passed = sum(passed_mask)
    n_failed = total - n_passed
    rate = _rate(n_passed, total)
    threshold = THRESHOLDS[dimension]
    return CheckResult(
        run_id=run_id,
        agent_name=agent_name,
        check_name=check_name,
        scorecard_dimension=dimension,
        invocations_checked=total,
        passed_count=n_passed,
        failed_count=n_failed,
        pass_rate=rate,
        threshold=threshold,
        passed=rate >= threshold,
        failure_details=failures,
        window_start=window_start,
        window_end=window_end,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_required_fields_present(conn, agent_name: str, run_id: str,
                                   window_start: datetime, window_end: datetime) -> CheckResult:
    """Functional accuracy proxy: required output fields present in parsed signals."""
    sql = """
        SELECT invocation_id, required_fields_present
        FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s
          AND parsed_at BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        rows = cur.fetchall()

    passed_mask = [bool(r[1]) for r in rows]
    failures = [{"invocation_id": r[0], "reason": "required_fields_missing"} for r in rows if not r[1]]
    return _result(run_id, agent_name, "required_fields_present", "functional_accuracy",
                   rows, passed_mask, failures, window_start, window_end)


def check_citation_integrity(conn, agent_name: str, run_id: str,
                              window_start: datetime, window_end: datetime) -> CheckResult:
    """Citation integrity: all invocations that should have citations do."""
    sql = """
        SELECT s.invocation_id, s.citations_present, s.citation_count
        FROM compliance.ai_agent_monitoring.agent_output_signals s
        JOIN compliance.ai_agent_monitoring.agent_registry r ON s.agent_name = r.agent_name
        WHERE s.agent_name = %s
          AND s.parsed_at BETWEEN %s AND %s
          AND r.domain IN ('OSINT_INVESTIGATION', 'SANCTIONS_SCREENING')
    """
    with conn.cursor() as cur:
        cur.execute(sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        rows = cur.fetchall()

    passed_mask = [bool(r[1]) for r in rows]
    failures = [{"invocation_id": r[0], "reason": "no_citations", "count": r[2]} for r in rows if not r[1]]
    return _result(run_id, agent_name, "citation_integrity", "citation_integrity",
                   rows, passed_mask, failures, window_start, window_end)


def check_boundary_refusal(conn, agent_name: str, run_id: str,
                            window_start: datetime, window_end: datetime) -> CheckResult:
    """
    Boundary/refusal: invocations where escalation_signal = REFUSED are counted as correct
    for out-of-scope inputs. We proxy this by looking at outputs where the agent
    appropriately refused (out_of_scope_refused = TRUE vs escalation_signal = REFUSED).
    """
    sql = """
        SELECT invocation_id, escalation_signal, out_of_scope_refused
        FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s
          AND escalation_signal = 'REFUSED'
          AND parsed_at BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        rows = cur.fetchall()

    # All REFUSED signals that also have out_of_scope_refused=True are correct refusals
    passed_mask = [bool(r[2]) for r in rows]
    failures = [{"invocation_id": r[0], "reason": "refusal_not_flagged_out_of_scope"} for r in rows if not r[2]]
    return _result(run_id, agent_name, "boundary_refusal_check", "boundary_refusal",
                   rows, passed_mask, failures, window_start, window_end)


def check_no_confident_wrong_output(conn, agent_name: str, run_id: str,
                                     window_start: datetime, window_end: datetime,
                                     analyst_feedback_conn=None) -> CheckResult:
    """
    Adversarial input handling: zero confident incorrect outputs.
    Uses analyst_feedback (REJECTED actions on HIGH confidence outputs) as signal.
    """
    sql = """
        SELECT f.invocation_id, s.confidence_level, f.action
        FROM compliance.ai_agent_monitoring.analyst_feedback f
        JOIN compliance.ai_agent_monitoring.agent_output_signals s
          ON f.invocation_id = s.invocation_id
        WHERE f.agent_name = %s
          AND f.action = 'REJECTED'
          AND s.confidence_level = 'HIGH'
          AND f.submitted_at BETWEEN %s AND %s
    """
    conn_to_use = analyst_feedback_conn or conn
    with conn_to_use.cursor() as cur:
        cur.execute(sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        rows = cur.fetchall()

    # Any row here is a confident wrong output — should be zero
    total_invocations_sql = """
        SELECT COUNT(*) FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s AND parsed_at BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(total_invocations_sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        total = cur.fetchone()[0] or 0

    n_failed = len(rows)
    n_passed = max(0, total - n_failed)
    rate = _rate(n_passed, total)
    threshold = THRESHOLDS["adversarial_input_handling"]
    failures = [{"invocation_id": r[0], "reason": "confident_wrong_high_confidence_rejected"} for r in rows]

    return CheckResult(
        run_id=run_id,
        agent_name=agent_name,
        check_name="no_confident_wrong_output",
        scorecard_dimension="adversarial_input_handling",
        invocations_checked=total,
        passed_count=n_passed,
        failed_count=n_failed,
        pass_rate=rate,
        threshold=threshold,
        passed=n_failed == 0,
        failure_details=failures,
        window_start=window_start,
        window_end=window_end,
    )


def check_escalation_signal_present(conn, agent_name: str, run_id: str,
                                     window_start: datetime, window_end: datetime) -> CheckResult:
    """Regulatory completeness: escalation signal is never NEEDS_REVIEW (ambiguous)."""
    sql = """
        SELECT invocation_id, escalation_signal
        FROM compliance.ai_agent_monitoring.agent_output_signals
        WHERE agent_name = %s
          AND parsed_at BETWEEN %s AND %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (agent_name, window_start.isoformat(), window_end.isoformat()))
        rows = cur.fetchall()

    passed_mask = [r[1] != "NEEDS_REVIEW" for r in rows]
    failures = [{"invocation_id": r[0], "reason": "ambiguous_escalation_signal"} for r in rows if r[1] == "NEEDS_REVIEW"]
    return _result(run_id, agent_name, "escalation_signal_present", "regulatory_completeness",
                   rows, passed_mask, failures, window_start, window_end)


def write_check_results(conn, results: list[CheckResult]):
    """Batch insert check results into evaluation_run_results."""
    sql = """
        INSERT INTO compliance.ai_agent_monitoring.evaluation_run_results
        (run_id, agent_name, run_type, check_name, scorecard_dimension,
         invocations_checked, passed_count, failed_count, pass_rate,
         threshold, passed, failure_details, drift_detected, drift_details,
         evaluated_at, window_start, window_end)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                PARSE_JSON(%s), %s, PARSE_JSON(%s), %s::TIMESTAMP_TZ,
                %s::TIMESTAMP_TZ, %s::TIMESTAMP_TZ)
    """
    with conn.cursor() as cur:
        for r in results:
            cur.execute(sql, (
                r.run_id, r.agent_name, "SCHEDULED", r.check_name, r.scorecard_dimension,
                r.invocations_checked, r.passed_count, r.failed_count, r.pass_rate,
                r.threshold, r.passed, json.dumps(r.failure_details),
                r.drift_detected, json.dumps(r.drift_details) if r.drift_details else None,
                r.evaluated_at.isoformat(),
                r.window_start.isoformat() if r.window_start else None,
                r.window_end.isoformat() if r.window_end else None,
            ))


ALL_CHECKS = [
    check_required_fields_present,
    check_citation_integrity,
    check_boundary_refusal,
    check_no_confident_wrong_output,
    check_escalation_signal_present,
]
