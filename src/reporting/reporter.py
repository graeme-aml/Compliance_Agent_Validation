"""Dashboard and examiner-ready package generation using rich."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.text import Text

from src.models import (
    AgentConfig,
    AgentStatus,
    Disposition,
    Issue,
    IssueSeverity,
    IssueStatus,
    ValidationCycle,
    ValidationScorecard,
    utc_now,
)

logger = logging.getLogger(__name__)

console = Console()

# Color mappings
DISPOSITION_COLORS = {
    Disposition.APPROVED: "green",
    Disposition.CONDITIONAL_APPROVAL: "yellow",
    Disposition.NEEDS_REMEDIATION: "red",
}

STATUS_COLORS = {
    AgentStatus.APPROVED: "green",
    AgentStatus.CONDITIONAL: "yellow",
    AgentStatus.NEEDS_REMEDIATION: "red",
    AgentStatus.PENDING: "blue",
    AgentStatus.DECOMMISSIONED: "dim",
}

SEVERITY_COLORS = {
    IssueSeverity.CRITICAL: "bold red",
    IssueSeverity.HIGH: "red",
    IssueSeverity.MEDIUM: "yellow",
    IssueSeverity.LOW: "blue",
}

ISSUE_STATUS_COLORS = {
    IssueStatus.OPEN: "red",
    IssueStatus.IN_PROGRESS: "yellow",
    IssueStatus.RESOLVED: "green",
    IssueStatus.CLOSED: "dim",
}


class Reporter:
    """Generates rich terminal reports and examiner-ready packages."""

    def __init__(self, registry, evidence_store, issue_tracker):
        self.registry = registry
        self.evidence_store = evidence_store
        self.issue_tracker = issue_tracker

    def print_agent_dashboard(self) -> None:
        """Print a rich table showing all agents, status, last validated, open issues."""
        agents = self.registry.get_all()
        open_issues = self.issue_tracker.get_open_issues()
        overdue_issues = self.issue_tracker.get_overdue_issues()

        # Build issue count per agent
        issues_by_agent: dict[str, int] = {}
        overdue_by_agent: dict[str, int] = {}
        for issue in open_issues:
            issues_by_agent[issue.agent_name] = issues_by_agent.get(issue.agent_name, 0) + 1
        for issue in overdue_issues:
            overdue_by_agent[issue.agent_name] = overdue_by_agent.get(issue.agent_name, 0) + 1

        table = Table(
            title="BSA/AML Compliance Agent Dashboard",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            border_style="blue",
        )

        table.add_column("Agent Name", style="bold", min_width=20)
        table.add_column("Domain", min_width=22)
        table.add_column("Risk Tier", min_width=8, justify="center")
        table.add_column("Version", min_width=8, justify="center")
        table.add_column("Status", min_width=18, justify="center")
        table.add_column("Last Validated", min_width=12, justify="center")
        table.add_column("Next Due", min_width=12, justify="center")
        table.add_column("Open Issues", min_width=11, justify="center")
        table.add_column("Overdue", min_width=8, justify="center")

        for agent in agents:
            status_color = STATUS_COLORS.get(agent.status, "white")
            open_count = issues_by_agent.get(agent.name, 0)
            overdue_count = overdue_by_agent.get(agent.name, 0)

            last_val = (
                agent.last_validated.strftime("%Y-%m-%d")
                if agent.last_validated
                else "[dim]Never[/dim]"
            )
            next_due = (
                agent.next_validation_due.strftime("%Y-%m-%d")
                if agent.next_validation_due
                else "[dim]—[/dim]"
            )

            # Color next_due red if past due
            if agent.next_validation_due and agent.next_validation_due < utc_now():
                next_due = f"[red]{next_due}[/red]"

            table.add_row(
                agent.name,
                agent.domain.value.replace("_", " ").title(),
                f"[{'red' if agent.risk_tier.value == 'HIGH' else 'yellow' if agent.risk_tier.value == 'MEDIUM' else 'green'}]{agent.risk_tier.value}[/]",
                agent.version,
                f"[{status_color}]{agent.status.value}[/]",
                last_val,
                next_due,
                f"[{'red' if open_count > 0 else 'green'}]{open_count}[/]",
                f"[{'bold red' if overdue_count > 0 else 'dim'}]{overdue_count}[/]",
            )

        console.print()
        console.print(table)
        console.print(
            f"\n[dim]Generated: {utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}[/dim]\n"
        )

        # Summary stats
        total_agents = len(agents)
        approved = sum(1 for a in agents if a.status == AgentStatus.APPROVED)
        needs_rem = sum(1 for a in agents if a.status == AgentStatus.NEEDS_REMEDIATION)
        total_open = len(open_issues)
        total_overdue = len(overdue_issues)

        console.print(
            Panel(
                f"[bold]Agents:[/bold] {total_agents} total | "
                f"[green]{approved} approved[/green] | "
                f"[red]{needs_rem} needs remediation[/red]\n"
                f"[bold]Issues:[/bold] [red]{total_open} open[/red] | "
                f"[bold red]{total_overdue} overdue[/bold red]",
                title="Summary",
                border_style="cyan",
            )
        )

    def print_scorecard(self, scorecard: ValidationScorecard) -> None:
        """Print a rich table with dimension scores, thresholds, pass/fail."""
        disp_color = DISPOSITION_COLORS.get(scorecard.disposition, "white")

        console.print()
        console.print(
            Panel(
                f"Agent: [bold]{scorecard.agent_name}[/bold] v{scorecard.agent_version}\n"
                f"Cycle ID: [dim]{scorecard.cycle_id}[/dim]\n"
                f"Disposition: [{disp_color}][bold]{scorecard.disposition.value if scorecard.disposition else 'PENDING'}[/bold][/]\n"
                f"Completed: {scorecard.completed_at.strftime('%Y-%m-%d %H:%M UTC') if scorecard.completed_at else '—'}",
                title="Validation Scorecard",
                border_style=disp_color,
            )
        )

        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Dimension", min_width=28)
        table.add_column("Weight", min_width=8, justify="center")
        table.add_column("Score", min_width=8, justify="right")
        table.add_column("Threshold", min_width=10, justify="right")
        table.add_column("Result", min_width=8, justify="center")
        table.add_column("Notes", min_width=40)

        for dim in scorecard.dimensions:
            result_text = "[green]PASS[/green]" if dim.passed else "[red]FAIL[/red]"
            weight_text = f"[bold]{'HIGH' if dim.weight == 'high' else 'MED'}[/bold]"
            score_color = "green" if dim.passed else "red"

            table.add_row(
                dim.name,
                weight_text,
                f"[{score_color}]{dim.score:.1%}[/]",
                f"{dim.threshold:.1%}",
                result_text,
                f"[dim]{dim.notes[:60]}[/dim]" if dim.notes else "",
            )

        console.print(table)

        if scorecard.overall_notes:
            console.print(
                Panel(
                    scorecard.overall_notes,
                    title="Assessment Notes",
                    border_style=disp_color,
                )
            )

    def print_cycle_summary(self, cycle: ValidationCycle) -> None:
        """Print a summary of a completed validation run."""
        console.print()

        total_cases = len(set(r.test_case_id for r in cycle.test_results))
        total_runs = len(cycle.test_results)
        passed_runs = sum(1 for r in cycle.test_results if r.passed)
        overall_pass_rate = passed_runs / total_runs if total_runs > 0 else 0.0

        duration = None
        if cycle.completed_at and cycle.started_at:
            duration = (cycle.completed_at - cycle.started_at).total_seconds()

        console.print(
            Panel(
                f"[bold]Cycle ID:[/bold] {cycle.cycle_id}\n"
                f"[bold]Agent:[/bold] {cycle.agent_name} v{cycle.agent_version}\n"
                f"[bold]Risk Tier:[/bold] {cycle.risk_tier.value}\n"
                f"[bold]Test Cases:[/bold] {total_cases} unique cases, {total_runs} total runs\n"
                f"[bold]Overall Pass Rate:[/bold] {overall_pass_rate:.1%}\n"
                f"[bold]Issues Created:[/bold] {len(cycle.issues_created)}\n"
                f"[bold]Duration:[/bold] {duration:.1f}s" if duration else "[bold]Duration:[/bold] —",
                title="Validation Cycle Summary",
                border_style="blue",
            )
        )

        if cycle.scorecard:
            self.print_scorecard(cycle.scorecard)

        if cycle.issues_created:
            console.print("\n[bold red]Issues Created:[/bold red]")
            self.print_issues(cycle.issues_created)

    def print_issues(
        self,
        issues: Optional[list[Issue]] = None,
        status_filter: str = "open",
    ) -> None:
        """Print a table of issues."""
        if issues is None:
            if status_filter == "open":
                issues = self.issue_tracker.get_open_issues()
            else:
                issues = self.issue_tracker.get_all_issues()

        if not issues:
            console.print("[dim]No issues found.[/dim]")
            return

        now = utc_now()
        overdue_ids = {i.id for i in self.issue_tracker.get_overdue_issues()}

        table = Table(
            title=f"Compliance Issues ({len(issues)} found)",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
            border_style="red",
        )
        table.add_column("ID", min_width=8)
        table.add_column("Severity", min_width=10, justify="center")
        table.add_column("Agent", min_width=20)
        table.add_column("Dimension", min_width=22)
        table.add_column("Status", min_width=12, justify="center")
        table.add_column("Due Date", min_width=12, justify="center")
        table.add_column("Title", min_width=35)

        for issue in sorted(issues, key=lambda i: list(IssueSeverity).index(i.severity)):
            sev_color = SEVERITY_COLORS.get(issue.severity, "white")
            status_color = ISSUE_STATUS_COLORS.get(issue.status, "white")
            is_overdue = issue.id in overdue_ids

            due_str = issue.due_date.strftime("%Y-%m-%d") if issue.due_date else "—"
            if is_overdue:
                due_str = f"[bold red]{due_str} ⚠[/bold red]"

            table.add_row(
                issue.id[:8],
                f"[{sev_color}]{issue.severity.value}[/]",
                issue.agent_name[:18],
                issue.dimension[:20],
                f"[{status_color}]{issue.status.value}[/]",
                due_str,
                issue.title[:50] if len(issue.title) > 50 else issue.title,
            )

        console.print()
        console.print(table)

    def generate_examiner_package(self, output_path: str) -> dict:
        """Produce a structured JSON summary for regulatory examiners."""
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        agents = self.registry.get_all()
        open_issues = self.issue_tracker.get_open_issues()
        overdue_issues = self.issue_tracker.get_overdue_issues()
        evidence_index = self.evidence_store.generate_evidence_index()

        package = {
            "examiner_package": {
                "generated_at": utc_now().isoformat(),
                "framework_version": "1.0.0",
                "purpose": "BSA/AML Compliance AI Agent Validation Evidence Package",
                "retention_years": 5,
            },
            "summary": {
                "total_agents": len(agents),
                "agents_by_status": {
                    status.value: sum(1 for a in agents if a.status == status)
                    for status in AgentStatus
                },
                "open_issues": len(open_issues),
                "overdue_issues": len(overdue_issues),
            },
            "agents": [],
            "open_issues": [json.loads(i.model_dump_json()) for i in open_issues],
            "overdue_issues": [json.loads(i.model_dump_json()) for i in overdue_issues],
            "evidence_index": evidence_index,
        }

        # Per-agent detail
        for agent in agents:
            cycle_ids = self.evidence_store.list_cycles(agent.name)
            latest_scorecard = None

            if cycle_ids:
                try:
                    latest_cycle = self.evidence_store.load_cycle(cycle_ids[-1])
                    if latest_cycle and latest_cycle.scorecard:
                        latest_scorecard = json.loads(
                            latest_cycle.scorecard.model_dump_json()
                        )
                except Exception as e:
                    logger.warning(f"Could not load latest cycle for {agent.name}: {e}")

            agent_issues = self.issue_tracker.get_issues_for_agent(agent.name)

            package["agents"].append(
                {
                    "config": json.loads(agent.model_dump_json()),
                    "validation_cycles_count": len(cycle_ids),
                    "latest_scorecard": latest_scorecard,
                    "open_issues_count": sum(
                        1 for i in agent_issues
                        if i.status in (IssueStatus.OPEN, IssueStatus.IN_PROGRESS)
                    ),
                    "issues": [json.loads(i.model_dump_json()) for i in agent_issues],
                }
            )

        # Write to file
        with open(output_file, "w") as f:
            json.dump(package, f, indent=2, default=str)

        console.print(
            f"\n[green]Examiner package generated:[/green] {output_file}\n"
            f"[dim]Agents: {len(agents)} | Open Issues: {len(open_issues)} | "
            f"Overdue: {len(overdue_issues)}[/dim]"
        )

        return package
