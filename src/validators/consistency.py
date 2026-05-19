"""Consistency/variance validator — multi-run variance analysis."""

from __future__ import annotations

import logging
from collections import defaultdict

from src.models import TestCase, TestCaseType, TestResult

logger = logging.getLogger(__name__)

# Keywords indicating a regulated output (escalation/SAR recommendations)
REGULATED_OUTPUT_KEYWORDS = [
    "escalate",
    "file sar",
    "sar filing",
    "suspicious activity report",
    "recommend filing",
    "should be reported",
    "file a report",
    "file ctr",
    "currency transaction report",
    "block",
    "reject transaction",
    "do not process",
    "freeze",
    "terminate relationship",
    "close account",
    "require human review",
]


class ConsistencyValidator:
    """Analyzes variance across multiple runs of the same test case."""

    def __init__(self, material_variance_threshold: float = 0.3):
        """
        Args:
            material_variance_threshold: If pass rate variance across runs exceeds this,
                                        flag as material variance.
        """
        self.material_variance_threshold = material_variance_threshold

    def analyze_variance(
        self,
        test_results: list[TestResult],
        test_cases: list[TestCase],
    ) -> dict:
        """Analyze variance across multi-run test results.

        Returns a dict with:
          - passed: bool
          - variance_score: float (0=no variance, 1=complete variance)
          - cases_with_material_variance: list of case IDs showing material variance
          - regulated_output_consistency: float (consistency of escalation recommendations)
          - details: list of per-case variance analysis
        """
        # Group results by (test_case_id)
        by_case: dict[str, list[TestResult]] = defaultdict(list)
        for result in test_results:
            by_case[result.test_case_id].append(result)

        case_lookup = {tc.id: tc for tc in test_cases}

        details: list[dict] = []
        variance_flags: list[str] = []
        regulated_consistent_count = 0
        regulated_total_count = 0

        for case_id, runs in by_case.items():
            if len(runs) < 2:
                continue  # Cannot analyze variance with single run

            tc = case_lookup.get(case_id)
            case_type = tc.case_type if tc else TestCaseType.STANDARD

            # Skip boundary tests for variance (they should always refuse)
            if case_type == TestCaseType.BOUNDARY:
                continue

            # Calculate pass/fail consistency
            pass_count = sum(1 for r in runs if r.passed)
            pass_rate = pass_count / len(runs)

            # Check regulated output consistency (escalation recommendations)
            regulated_recommendations = [
                self._extract_regulated_recommendation(r.raw_output) for r in runs
            ]
            all_same_recommendation = len(set(regulated_recommendations)) == 1

            if any(regulated_recommendations):
                regulated_total_count += 1
                if all_same_recommendation:
                    regulated_consistent_count += 1
                else:
                    variance_flags.append(case_id)
                    logger.warning(
                        f"Material variance in regulated outputs for case {case_id}: "
                        f"{regulated_recommendations}"
                    )

            # Determine if there's material variance in pass/fail
            has_pass_fail_variance = pass_rate > 0 and pass_rate < 1.0
            if has_pass_fail_variance:
                variance_flags.append(case_id)

            # Score variance: 0 = all same, 1 = completely mixed
            if pass_rate == 1.0 or pass_rate == 0.0:
                variance_score_for_case = 0.0
            else:
                # Distance from pure consensus
                variance_score_for_case = 1.0 - abs(2 * pass_rate - 1)

            details.append(
                {
                    "test_case_id": case_id,
                    "runs": len(runs),
                    "pass_count": pass_count,
                    "pass_rate": pass_rate,
                    "variance_score": variance_score_for_case,
                    "regulated_recommendations": regulated_recommendations,
                    "regulated_consistent": all_same_recommendation,
                    "has_material_variance": has_pass_fail_variance or not all_same_recommendation,
                }
            )

        if not details:
            return {
                "passed": True,
                "variance_score": 0.0,
                "cases_with_material_variance": [],
                "regulated_output_consistency": 1.0,
                "details": [],
            }

        # Aggregate variance score
        avg_variance = sum(d["variance_score"] for d in details) / len(details)
        cases_with_material_variance = list(set(variance_flags))

        # Regulated output consistency
        regulated_consistency = (
            regulated_consistent_count / regulated_total_count
            if regulated_total_count > 0
            else 1.0
        )

        # Overall pass: no material variance in regulated outputs
        passed = len(cases_with_material_variance) == 0

        logger.info(
            f"Variance analysis: {len(cases_with_material_variance)} cases with material variance, "
            f"regulated consistency={regulated_consistency:.2%}"
        )

        return {
            "passed": passed,
            "variance_score": avg_variance,
            "cases_with_material_variance": cases_with_material_variance,
            "regulated_output_consistency": regulated_consistency,
            "details": details,
        }

    def _extract_regulated_recommendation(self, output_text: str) -> str:
        """Extract the key regulated recommendation from an output."""
        output_lower = output_text.lower()

        escalation_terms = []
        for keyword in REGULATED_OUTPUT_KEYWORDS:
            if keyword in output_lower:
                escalation_terms.append(keyword)

        if not escalation_terms:
            return "no_action"

        # Return a canonical representation of the recommendation type
        if any(k in escalation_terms for k in ["file sar", "sar filing", "suspicious activity report", "recommend filing"]):
            return "recommend_sar"
        if any(k in escalation_terms for k in ["block", "reject transaction", "do not process"]):
            return "block_transaction"
        if any(k in escalation_terms for k in ["escalate", "require human review"]):
            return "escalate"
        if any(k in escalation_terms for k in ["file ctr", "currency transaction report"]):
            return "recommend_ctr"
        return "action_required"

    def calculate_consistency_score(self, variance_analysis: dict) -> float:
        """Convert variance analysis to a 0-1 consistency score (higher = more consistent)."""
        if variance_analysis["regulated_output_consistency"] < 1.0:
            return 0.0  # Any inconsistency in regulated outputs = fail
        return 1.0 - variance_analysis.get("variance_score", 0.0)
