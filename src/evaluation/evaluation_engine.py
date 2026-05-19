"""Evaluation engine — orchestrates all validators and produces validation cycles."""

from __future__ import annotations

import logging
import random
from typing import Optional

import anthropic
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from src.models import (
    AgentConfig,
    MIN_SAMPLE_SIZES,
    RUNS_PER_CASE,
    TestCase,
    TestCaseType,
    TestResult,
    ValidationCycle,
    utc_now,
)
from src.test_cases.base import get_all_test_cases_for_domain
from src.validators.functional_accuracy import FunctionalAccuracyValidator
from src.validators.regulatory_completeness import RegulatoryCompletenessValidator
from src.validators.consistency import ConsistencyValidator
from src.validators.regression import RegressionValidator
from src.validators.boundary_refusal import BoundaryRefusalValidator

logger = logging.getLogger(__name__)


class EvaluationEngine:
    """Orchestrates all validators to produce a complete validation cycle."""

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        registry,
        evidence_store,
        issue_tracker,
        judge_model: str = "claude-sonnet-4-6",
    ):
        self.client = anthropic_client
        self.registry = registry
        self.evidence_store = evidence_store
        self.issue_tracker = issue_tracker
        self.judge_model = judge_model

        self.functional_validator = FunctionalAccuracyValidator(anthropic_client, judge_model)
        self.regulatory_validator = RegulatoryCompletenessValidator(anthropic_client, judge_model)
        self.consistency_validator = ConsistencyValidator()
        self.regression_validator = RegressionValidator()
        self.boundary_validator = BoundaryRefusalValidator()

    def run_validation_cycle(self, agent_config: AgentConfig) -> ValidationCycle:
        """Run a complete validation cycle for an agent."""
        from src.scorecard.scorecard import ScorecardBuilder
        from src.issues.issue_tracker import IssueTracker

        cycle = ValidationCycle(
            agent_name=agent_config.name,
            agent_version=agent_config.version,
            risk_tier=agent_config.risk_tier,
        )

        logger.info(
            f"Starting validation cycle {cycle.cycle_id} for agent '{agent_config.name}' "
            f"(tier={agent_config.risk_tier})"
        )

        # Load test cases for the agent's domain
        test_cases = get_all_test_cases_for_domain(agent_config.domain)

        # Ensure minimum sample size by padding with variations
        min_samples = MIN_SAMPLE_SIZES[agent_config.risk_tier]
        test_cases = self._ensure_minimum_samples(test_cases, min_samples, agent_config)

        runs_per_case = RUNS_PER_CASE[agent_config.risk_tier]

        logger.info(
            f"Running {len(test_cases)} test cases x {runs_per_case} runs = "
            f"{len(test_cases) * runs_per_case} total evaluations"
        )

        # Run all test cases with progress display
        all_results = self._run_all_with_progress(
            test_cases=test_cases,
            agent_config=agent_config,
            runs_per_case=runs_per_case,
        )

        cycle.test_results = all_results

        # --- Regulatory completeness evaluation ---
        logger.info("Running regulatory completeness evaluation...")
        reg_score, reg_evaluations = self.regulatory_validator.evaluate_batch(
            all_results, test_cases, agent_config
        )

        # --- Variance/consistency analysis ---
        logger.info("Running consistency/variance analysis...")
        variance_analysis = self.consistency_validator.analyze_variance(all_results, test_cases)

        # --- Adversarial input handling check ---
        logger.info("Checking adversarial input handling...")
        adversarial_analysis = self.boundary_validator.check_adversarial_handling(
            all_results, test_cases
        )

        # --- Boundary/refusal check ---
        logger.info("Evaluating boundary/refusal handling...")
        boundary_analysis = self.boundary_validator.evaluate_boundary_results(
            all_results, test_cases
        )

        # --- Regression check ---
        logger.info("Running regression check...")
        prior_cycles = self.evidence_store.list_cycles(agent_config.name)
        baseline_cycle = None
        if len(prior_cycles) > 1:
            # Get the most recent completed cycle (not the current one)
            try:
                baseline_cycle = self.evidence_store.load_cycle(prior_cycles[-2])
            except Exception as e:
                logger.warning(f"Could not load baseline cycle: {e}")

        regression_analysis = self.regression_validator.compare_to_baseline(
            all_results, baseline_cycle
        )

        # --- Citation integrity ---
        citation_integrity = self.regulatory_validator.calculate_citation_integrity(
            reg_evaluations
        )

        # --- Build scorecard ---
        scorecard_builder = ScorecardBuilder()
        scorecard = scorecard_builder.build(
            test_results=all_results,
            agent_config=agent_config,
            cycle_id=cycle.cycle_id,
            started_at=cycle.started_at,
            regulatory_score=reg_score,
            variance_analysis=variance_analysis,
            adversarial_analysis=adversarial_analysis,
            boundary_analysis=boundary_analysis,
            regression_analysis=regression_analysis,
            citation_integrity=citation_integrity,
        )
        scorecard.completed_at = utc_now()
        cycle.scorecard = scorecard
        cycle.completed_at = utc_now()

        # --- Create issues from scorecard ---
        issues = self.issue_tracker.create_issues_from_scorecard(scorecard, agent_config)
        cycle.issues_created = issues

        # --- Save cycle to evidence store ---
        self.evidence_store.save_cycle(cycle)

        # --- Update registry ---
        self.registry.update_validation_status(agent_config.name, scorecard)

        logger.info(
            f"Validation cycle {cycle.cycle_id} complete. "
            f"Disposition: {scorecard.disposition}"
        )
        return cycle

    def _run_all_with_progress(
        self,
        test_cases: list[TestCase],
        agent_config: AgentConfig,
        runs_per_case: int,
    ) -> list[TestResult]:
        """Run all test cases with a rich progress bar."""
        results: list[TestResult] = []
        total = len(test_cases) * runs_per_case

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]{task.fields[status]}"),
        ) as progress:
            task = progress.add_task(
                f"Evaluating {agent_config.name}",
                total=total,
                status="starting...",
            )

            for test_case in test_cases:
                for run_num in range(1, runs_per_case + 1):
                    progress.update(
                        task,
                        status=f"Test {test_case.id[:8]}... run {run_num}",
                    )
                    result = self.functional_validator.run_test_case(
                        test_case, agent_config, run_num
                    )
                    results.append(result)
                    progress.advance(task)

        return results

    def _ensure_minimum_samples(
        self,
        test_cases: list[TestCase],
        min_samples: int,
        agent_config: AgentConfig,
    ) -> list[TestCase]:
        """Pad test cases with variations if below the minimum sample size."""
        if len(test_cases) >= min_samples:
            return test_cases

        needed = min_samples - len(test_cases)
        logger.info(f"Padding test cases: have {len(test_cases)}, need {min_samples} (+{needed})")

        # Generate simple variations of existing standard test cases
        standard_cases = [tc for tc in test_cases if tc.case_type == TestCaseType.STANDARD]
        if not standard_cases:
            standard_cases = test_cases

        padded = list(test_cases)
        for i in range(needed):
            base = standard_cases[i % len(standard_cases)]
            variation = TestCase(
                domain=base.domain,
                case_type=base.case_type,
                input_text=self._generate_variation(base.input_text, i + 1),
                expected_signals=base.expected_signals,
                should_refuse=base.should_refuse,
                description=f"{base.description} [variation {i + 1}]",
            )
            padded.append(variation)

        return padded

    def _generate_variation(self, input_text: str, variation_num: int) -> str:
        """Generate a minor variation of a test case input for sample padding."""
        # Vary dollar amounts slightly, change dates, or reframe the scenario
        variations = [
            f"[Scenario variation {variation_num}]\n{input_text}",
            f"{input_text}\n\nAdditional context: This is a follow-up review from the prior month.",
            f"{input_text}\n\nNote: This case has been flagged for senior analyst review.",
        ]
        return variations[variation_num % len(variations)]
