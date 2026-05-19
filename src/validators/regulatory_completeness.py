"""Regulatory completeness validator — SME-style regulatory review via Claude."""

from __future__ import annotations

import json
import logging

import anthropic

from src.models import AgentConfig, TestCase, TestResult, utc_now

logger = logging.getLogger(__name__)

REGULATORY_REVIEW_SYSTEM_PROMPT = """You are a subject matter expert (SME) in BSA/AML regulatory
compliance with 15+ years of experience. You have deep expertise in:
- Bank Secrecy Act (BSA) and implementing regulations (31 CFR Part 1000-1099)
- FinCEN guidance and administrative rulings
- FFIEC BSA/AML Examination Manual
- OFAC regulations and compliance programs
- SAR/CTR filing requirements
- KYC/CDD/EDD regulatory standards
- USA PATRIOT Act requirements

Evaluate AI agent responses for regulatory accuracy and completeness.
Respond ONLY with valid JSON."""

REGULATORY_COMPLETENESS_PROMPT = """Evaluate the following AI compliance agent response for
regulatory accuracy and completeness from a BSA/AML SME perspective.

DOMAIN: {domain}
INPUT TO AGENT: {input_text}
AGENT RESPONSE: {agent_output}

Assess the response on these regulatory criteria:
1. Cites correct regulations/statutes (BSA, 31 CFR, FinCEN guidance, OFAC regs)
2. Uses correct regulatory thresholds ($10,000 CTR, $5,000 SAR mandatory threshold, 25% BO)
3. Correctly identifies regulatory obligations
4. Provides actionable compliance guidance
5. Does not give incorrect legal conclusions or regulatory misinformation
6. Appropriately defers to human judgment for final decisions

Respond with ONLY this JSON:
{{
  "regulatory_score": <float 0.0 to 1.0>,
  "citations_correct": <true or false>,
  "thresholds_correct": <true or false>,
  "obligations_identified": <true or false>,
  "actionable_guidance": <true or false>,
  "no_incorrect_conclusions": <true or false>,
  "passes_regulatory_review": <true or false — true if score >= 0.95>,
  "regulatory_issues": ["<list any specific regulatory inaccuracies or gaps>"],
  "regulatory_strengths": ["<list regulatory elements correctly addressed>"],
  "reasoning": "<brief SME explanation>"
}}"""


class RegulatoryCompletenessValidator:
    """Performs SME-style regulatory review of agent outputs via Claude."""

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        judge_model: str = "claude-sonnet-4-6",
    ):
        self.client = anthropic_client
        self.judge_model = judge_model

    def evaluate_regulatory_completeness(
        self,
        test_case: TestCase,
        agent_output: str,
        agent_config: AgentConfig,
    ) -> dict:
        """Evaluate a single agent response for regulatory completeness."""
        prompt = REGULATORY_COMPLETENESS_PROMPT.format(
            domain=test_case.domain.value,
            input_text=test_case.input_text,
            agent_output=agent_output,
        )

        try:
            response = self.client.messages.create(
                model=self.judge_model,
                max_tokens=1024,
                system=REGULATORY_REVIEW_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.content[0].text.strip() if response.content else "{}"

            if response_text.startswith("```"):
                lines = response_text.split("\n")
                response_text = "\n".join(l for l in lines if not l.startswith("```"))

            return json.loads(response_text)

        except Exception as e:
            logger.error(f"Regulatory completeness evaluation failed: {e}")
            return {
                "regulatory_score": 0.0,
                "citations_correct": False,
                "thresholds_correct": False,
                "obligations_identified": False,
                "actionable_guidance": False,
                "no_incorrect_conclusions": False,
                "passes_regulatory_review": False,
                "regulatory_issues": [f"Evaluation error: {str(e)}"],
                "regulatory_strengths": [],
                "reasoning": f"Evaluation failed: {str(e)}",
            }

    def evaluate_batch(
        self,
        test_results: list[TestResult],
        test_cases: list[TestCase],
        agent_config: AgentConfig,
    ) -> tuple[float, list[dict]]:
        """Evaluate regulatory completeness across a batch of test results.

        Returns (aggregate_score, list_of_individual_evaluations).
        Only evaluates standard and edge case results (not boundary/adversarial).
        """
        from src.models import TestCaseType

        # Build a lookup from test_case_id -> TestCase
        case_lookup = {tc.id: tc for tc in test_cases}

        # Evaluate each unique test result (first run only to avoid redundancy)
        seen_cases: set[str] = set()
        evaluations: list[dict] = []

        for result in test_results:
            if result.test_case_id in seen_cases:
                continue
            if result.case_type in (TestCaseType.BOUNDARY,):
                continue  # Skip boundary tests for regulatory completeness

            seen_cases.add(result.test_case_id)
            tc = case_lookup.get(result.test_case_id)
            if tc is None:
                continue

            evaluation = self.evaluate_regulatory_completeness(tc, result.raw_output, agent_config)
            evaluation["test_case_id"] = result.test_case_id
            evaluation["agent_output_snippet"] = result.raw_output[:200]
            evaluations.append(evaluation)

        if not evaluations:
            return 0.0, []

        scores = [e.get("regulatory_score", 0.0) for e in evaluations]
        aggregate_score = sum(scores) / len(scores)

        logger.info(
            f"Regulatory completeness: {aggregate_score:.2%} across {len(evaluations)} cases"
        )
        return aggregate_score, evaluations

    def calculate_citation_integrity(self, evaluations: list[dict]) -> float:
        """Calculate the citation integrity score (proportion with correct citations)."""
        if not evaluations:
            return 0.0
        correct = sum(1 for e in evaluations if e.get("citations_correct", False))
        return correct / len(evaluations)
