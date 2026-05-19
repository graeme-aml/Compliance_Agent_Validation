"""Base test case loader and domain routing."""

from __future__ import annotations

import logging
from typing import Optional

from src.models import AgentDomain, TestCase

logger = logging.getLogger(__name__)


def get_all_test_cases_for_domain(domain: AgentDomain) -> list[TestCase]:
    """Return all test cases for a given agent domain."""
    from src.test_cases.transaction_monitoring import get_test_cases as get_tm_cases
    from src.test_cases.kyc_cdd_edd import get_test_cases as get_kyc_cases
    from src.test_cases.sar_uar_filing import get_test_cases as get_sar_cases
    from src.test_cases.osint_investigation import get_test_cases as get_osint_cases
    from src.test_cases.sanctions_screening import get_test_cases as get_sanctions_cases

    domain_map = {
        AgentDomain.TRANSACTION_MONITORING: get_tm_cases,
        AgentDomain.KYC_CDD_EDD: get_kyc_cases,
        AgentDomain.SAR_UAR_FILING: get_sar_cases,
        AgentDomain.OSINT_INVESTIGATION: get_osint_cases,
        AgentDomain.SANCTIONS_SCREENING: get_sanctions_cases,
        AgentDomain.REGULATORY_RESEARCH: get_osint_cases,  # Fallback for research domain
    }

    getter = domain_map.get(domain)
    if getter is None:
        logger.warning(f"No test cases found for domain: {domain}")
        return []

    cases = getter()
    logger.info(f"Loaded {len(cases)} test cases for domain: {domain}")
    return cases


class TestCaseLoader:
    """Utility class for loading and managing test cases."""

    def __init__(self, domain: AgentDomain):
        self.domain = domain
        self._cases: list[TestCase] = []

    def load(self) -> list[TestCase]:
        """Load test cases for the configured domain."""
        self._cases = get_all_test_cases_for_domain(self.domain)
        return self._cases

    def get_by_type(self, case_type) -> list[TestCase]:
        """Filter test cases by type."""
        return [c for c in self._cases if c.case_type == case_type]

    def get_standard_cases(self) -> list[TestCase]:
        from src.models import TestCaseType
        return self.get_by_type(TestCaseType.STANDARD)

    def get_edge_cases(self) -> list[TestCase]:
        from src.models import TestCaseType
        return self.get_by_type(TestCaseType.EDGE_CASE)

    def get_adversarial_cases(self) -> list[TestCase]:
        from src.models import TestCaseType
        return self.get_by_type(TestCaseType.ADVERSARIAL)

    def get_boundary_cases(self) -> list[TestCase]:
        from src.models import TestCaseType
        return self.get_by_type(TestCaseType.BOUNDARY)
