"""Regression validator — baseline comparison against prior cycles."""

from __future__ import annotations

import logging
from typing import Optional

from src.models import TestResult, ValidationCycle

logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD = 0.05  # 5% drop in pass rate is flagged


class RegressionValidator:
    """Compares current validation results against a prior baseline cycle."""

    def __init__(self, regression_threshold: float = REGRESSION_THRESHOLD):
        self.regression_threshold = regression_threshold

    def compare_to_baseline(
        self,
        current_results: list[TestResult],
        baseline_cycle: Optional[ValidationCycle],
    ) -> dict:
        """Compare current results to baseline.

        Returns:
          - passed: bool
          - regression_detected: bool
          - current_pass_rate: float
          - baseline_pass_rate: float or None
          - delta: float (current - baseline; negative means regression)
          - regressed_test_cases: list[str] of case IDs that regressed
          - improved_test_cases: list[str] of case IDs that improved
          - details: dict with per-case comparison
        """
        current_pass_rate = self._calculate_pass_rate(current_results)
        current_by_case = self._group_by_case(current_results)

        if baseline_cycle is None:
            logger.info("No baseline cycle available — regression check skipped")
            return {
                "passed": True,
                "regression_detected": False,
                "current_pass_rate": current_pass_rate,
                "baseline_pass_rate": None,
                "delta": None,
                "regressed_test_cases": [],
                "improved_test_cases": [],
                "details": {},
                "note": "No baseline available — first validation cycle",
            }

        baseline_results = baseline_cycle.test_results
        baseline_pass_rate = self._calculate_pass_rate(baseline_results)
        baseline_by_case = self._group_by_case(baseline_results)

        delta = current_pass_rate - baseline_pass_rate
        regression_detected = delta < -self.regression_threshold

        # Per-case comparison
        regressed_cases: list[str] = []
        improved_cases: list[str] = []
        per_case_details: dict[str, dict] = {}

        all_case_ids = set(current_by_case.keys()) | set(baseline_by_case.keys())
        for case_id in all_case_ids:
            current_case_results = current_by_case.get(case_id, [])
            baseline_case_results = baseline_by_case.get(case_id, [])

            current_case_pass_rate = self._calculate_pass_rate(current_case_results)
            baseline_case_pass_rate = self._calculate_pass_rate(baseline_case_results)

            case_delta = current_case_pass_rate - baseline_case_pass_rate

            if case_delta < -self.regression_threshold:
                regressed_cases.append(case_id)
            elif case_delta > self.regression_threshold:
                improved_cases.append(case_id)

            per_case_details[case_id] = {
                "current_pass_rate": current_case_pass_rate,
                "baseline_pass_rate": baseline_case_pass_rate,
                "delta": case_delta,
                "regressed": case_delta < -self.regression_threshold,
            }

        passed = not regression_detected

        logger.info(
            f"Regression analysis: current={current_pass_rate:.2%}, "
            f"baseline={baseline_pass_rate:.2%}, delta={delta:+.2%}, "
            f"regressed_cases={len(regressed_cases)}"
        )

        return {
            "passed": passed,
            "regression_detected": regression_detected,
            "current_pass_rate": current_pass_rate,
            "baseline_pass_rate": baseline_pass_rate,
            "delta": delta,
            "regressed_test_cases": regressed_cases,
            "improved_test_cases": improved_cases,
            "details": per_case_details,
        }

    def _calculate_pass_rate(self, results: list[TestResult]) -> float:
        """Calculate the pass rate for a list of results."""
        if not results:
            return 0.0
        return sum(1 for r in results if r.passed) / len(results)

    def _group_by_case(
        self, results: list[TestResult]
    ) -> dict[str, list[TestResult]]:
        """Group results by test case ID (first run only for baseline comparison)."""
        by_case: dict[str, list[TestResult]] = {}
        for result in results:
            if result.test_case_id not in by_case:
                by_case[result.test_case_id] = []
            by_case[result.test_case_id].append(result)
        return by_case
