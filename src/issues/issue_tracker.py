"""Issue creation, SLA tracking, and status management."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from src.models import (
    AgentConfig,
    Disposition,
    Issue,
    IssueSeverity,
    IssueStatus,
    SEVERITY_SLA_DAYS,
    ScorecardDimension,
    ValidationScorecard,
    generate_id,
    utc_now,
)

logger = logging.getLogger(__name__)

# Map dimension names to severity based on weight and regulatory importance
DIMENSION_SEVERITY_MAP = {
    "Functional Accuracy": IssueSeverity.CRITICAL,          # high weight
    "Regulatory Completeness": IssueSeverity.HIGH,           # high weight + regulatory
    "Adversarial Input Handling": IssueSeverity.CRITICAL,    # high weight
    "Variance/Consistency": IssueSeverity.CRITICAL,          # high weight
    "Edge Case Handling": IssueSeverity.MEDIUM,              # medium weight
    "Citation Integrity": IssueSeverity.MEDIUM,              # medium weight
    "Boundary/Refusal": IssueSeverity.MEDIUM,                # medium weight
}


class IssueTracker:
    """Creates, tracks, and manages compliance issues."""

    def __init__(self, issues_path: str = "evidence/issues.json"):
        self.issues_path = Path(issues_path)
        self._issues: dict[str, Issue] = {}
        self._load()

    def _load(self) -> None:
        """Load issues from JSON file."""
        if self.issues_path.exists():
            try:
                with open(self.issues_path, "r") as f:
                    data = json.load(f)
                for issue_data in data.get("issues", []):
                    issue = Issue.model_validate(issue_data)
                    self._issues[issue.id] = issue
                logger.debug(f"Loaded {len(self._issues)} issues from store")
            except Exception as e:
                logger.error(f"Error loading issues: {e}")
                self._issues = {}

    def _save(self) -> None:
        """Append-safe save of all issues."""
        self.issues_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "issues": [json.loads(issue.model_dump_json()) for issue in self._issues.values()],
            "last_updated": utc_now().isoformat(),
        }
        with open(self.issues_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def create_issues_from_scorecard(
        self,
        scorecard: ValidationScorecard,
        agent_config: AgentConfig,
    ) -> list[Issue]:
        """Create issues for all failed scorecard dimensions."""
        created_issues: list[Issue] = []
        now = utc_now()

        for dimension in scorecard.dimensions:
            if dimension.passed:
                continue

            severity = self._determine_severity(dimension)
            sla_days = SEVERITY_SLA_DAYS[severity]
            due_date = now + timedelta(days=sla_days)

            title = f"[{severity.value}] {dimension.name} below threshold for {agent_config.name}"
            description = (
                f"Agent '{agent_config.name}' (version {agent_config.version}) failed the "
                f"'{dimension.name}' dimension in validation cycle {scorecard.cycle_id}.\n\n"
                f"Score: {dimension.score:.1%} (threshold: {dimension.threshold:.1%})\n"
                f"Weight: {dimension.weight}\n"
                f"Notes: {dimension.notes}\n\n"
                f"Disposition: {scorecard.disposition}\n"
                f"Remediation due by: {due_date.date().isoformat()}"
            )

            issue = Issue(
                agent_name=agent_config.name,
                severity=severity,
                title=title,
                description=description,
                dimension=dimension.name,
                due_date=due_date,
                status=IssueStatus.OPEN,
                remediation_owner=agent_config.owner,
                cycle_id=scorecard.cycle_id,
            )

            self._issues[issue.id] = issue
            created_issues.append(issue)
            logger.info(
                f"Created issue {issue.id}: [{severity.value}] {dimension.name} "
                f"for agent {agent_config.name}"
            )

        self._save()
        return created_issues

    def _determine_severity(self, dimension: ScorecardDimension) -> IssueSeverity:
        """Map a failed dimension to an issue severity."""
        # Special case: regulatory completeness below 95% is always High
        if dimension.name == "Regulatory Completeness" and dimension.score < 0.95:
            return IssueSeverity.HIGH

        # Use the dimension severity map
        severity = DIMENSION_SEVERITY_MAP.get(dimension.name)
        if severity:
            return severity

        # Default based on weight
        return IssueSeverity.CRITICAL if dimension.weight == "high" else IssueSeverity.MEDIUM

    def get_all_issues(self) -> list[Issue]:
        """Return all issues."""
        return list(self._issues.values())

    def get_open_issues(self) -> list[Issue]:
        """Return all open or in-progress issues."""
        return [
            i for i in self._issues.values()
            if i.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS)
        ]

    def get_overdue_issues(self) -> list[Issue]:
        """Return issues past their SLA due date."""
        now = utc_now()
        overdue: list[Issue] = []
        for issue in self._issues.values():
            if issue.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED):
                continue
            if issue.due_date and issue.due_date < now:
                overdue.append(issue)
        return overdue

    def get_issues_for_agent(self, agent_name: str) -> list[Issue]:
        """Return all issues for a specific agent."""
        return [i for i in self._issues.values() if i.agent_name == agent_name]

    def close_issue(self, issue_id: str) -> bool:
        """Close an issue by ID."""
        issue = self._issues.get(issue_id)
        if issue is None:
            logger.warning(f"Issue not found: {issue_id}")
            return False
        issue.status = IssueStatus.CLOSED
        self._save()
        logger.info(f"Closed issue {issue_id}")
        return True

    def update_issue_status(self, issue_id: str, status: IssueStatus) -> bool:
        """Update issue status."""
        issue = self._issues.get(issue_id)
        if issue is None:
            return False
        issue.status = status
        self._save()
        return True

    def get_issue(self, issue_id: str) -> Optional[Issue]:
        """Get a specific issue by ID."""
        return self._issues.get(issue_id)
