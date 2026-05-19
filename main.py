"""CLI entry point for the BSA/AML Compliance Agent Validation framework."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console

console = Console()


def _setup_logging(level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Suppress noisy library loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def _load_config() -> dict:
    """Load configuration from config.yaml."""
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


def _build_components(config: dict):
    """Build and return all framework components."""
    import anthropic

    cfg_anthropic = config.get("anthropic", {})
    cfg_evidence = config.get("evidence", {})
    cfg_scheduler = config.get("scheduler", {})

    judge_model = cfg_anthropic.get("model", "claude-sonnet-4-6")
    evidence_base = cfg_evidence.get("base_path", "evidence")
    retention_years = cfg_evidence.get("retention_years", 5)

    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[bold red]Error:[/bold red] ANTHROPIC_API_KEY environment variable not set.\n"
            "Set it with: export ANTHROPIC_API_KEY=your-api-key"
        )
        sys.exit(1)

    anthropic_client = anthropic.Anthropic(api_key=api_key)

    from src.registry.agent_registry import AgentRegistry
    from src.evidence.evidence_store import EvidenceStore
    from src.issues.issue_tracker import IssueTracker
    from src.evaluation.evaluation_engine import EvaluationEngine
    from src.reporting.reporter import Reporter
    from src.scheduler.scheduler import ValidationScheduler

    registry = AgentRegistry(registry_path=f"{evidence_base}/agent_registry.json")
    evidence_store = EvidenceStore(
        base_path=evidence_base, retention_years=retention_years
    )
    issue_tracker = IssueTracker(issues_path=f"{evidence_base}/issues.json")

    engine = EvaluationEngine(
        anthropic_client=anthropic_client,
        registry=registry,
        evidence_store=evidence_store,
        issue_tracker=issue_tracker,
        judge_model=judge_model,
    )

    reporter = Reporter(
        registry=registry,
        evidence_store=evidence_store,
        issue_tracker=issue_tracker,
    )

    scheduler = ValidationScheduler(
        evaluation_engine=engine,
        registry=registry,
        high_tier_interval_hours=cfg_scheduler.get("high_tier_check_interval_hours", 24),
        medium_tier_interval_hours=cfg_scheduler.get("medium_tier_check_interval_hours", 168),
        low_tier_interval_hours=cfg_scheduler.get("low_tier_check_interval_hours", 720),
    )

    return {
        "anthropic_client": anthropic_client,
        "registry": registry,
        "evidence_store": evidence_store,
        "issue_tracker": issue_tracker,
        "engine": engine,
        "reporter": reporter,
        "scheduler": scheduler,
    }


@click.group()
@click.version_option(version="1.0.0", prog_name="Compliance Agent Validation")
def cli():
    """BSA/AML Compliance Agent Validation Framework.

    Continuously validates AI agents used in compliance workflows against
    regulatory standards, accuracy thresholds, and behavioral boundaries.
    """
    config = _load_config()
    log_level = config.get("logging", {}).get("level", "INFO")
    _setup_logging(log_level)


@cli.command("validate")
@click.option(
    "--agent",
    "agent_name",
    default=None,
    help="Name of specific agent to validate. If omitted, validates all registered agents.",
)
def validate_command(agent_name: Optional[str]):
    """Run validation now for one or all registered agents."""
    config = _load_config()
    components = _build_components(config)
    scheduler = components["scheduler"]
    reporter = components["reporter"]

    console.print("\n[bold blue]BSA/AML Compliance Agent Validation[/bold blue]")
    console.print("=" * 50)

    cycles = scheduler.run_once(agent_name=agent_name)

    if not cycles:
        console.print("[yellow]No agents were validated.[/yellow]")
        return

    for cycle in cycles:
        reporter.print_cycle_summary(cycle)

    console.print(f"\n[green]Completed {len(cycles)} validation cycle(s).[/green]")


@cli.command("dashboard")
def dashboard_command():
    """Show agent status dashboard."""
    config = _load_config()
    components = _build_components(config)
    components["reporter"].print_agent_dashboard()


@cli.command("schedule")
def schedule_command():
    """Start the continuous validation scheduler."""
    config = _load_config()
    components = _build_components(config)

    console.print("\n[bold blue]Starting Continuous Validation Scheduler[/bold blue]")
    components["scheduler"].start()


@cli.command("issues")
@click.option(
    "--status",
    "status_filter",
    default="open",
    type=click.Choice(["open", "all"]),
    help="Filter issues by status (default: open)",
)
def issues_command(status_filter: str):
    """List compliance issues."""
    config = _load_config()
    components = _build_components(config)
    reporter = components["reporter"]
    issue_tracker = components["issue_tracker"]

    if status_filter == "open":
        issues = issue_tracker.get_open_issues()
        console.print(f"\n[bold]Open Issues ({len(issues)} found)[/bold]")
    else:
        issues = issue_tracker.get_all_issues()
        console.print(f"\n[bold]All Issues ({len(issues)} found)[/bold]")

    reporter.print_issues(issues=issues, status_filter=status_filter)

    # Show overdue issues separately
    overdue = issue_tracker.get_overdue_issues()
    if overdue:
        console.print(f"\n[bold red]Overdue Issues ({len(overdue)}):[/bold red]")
        for issue in overdue:
            due_str = issue.due_date.strftime("%Y-%m-%d") if issue.due_date else "N/A"
            console.print(
                f"  [red]•[/red] [{issue.severity.value}] {issue.title[:60]} "
                f"(due: {due_str})"
            )


@cli.command("examiner-package")
@click.option(
    "--output",
    "output_path",
    default="evidence/examiner_package.json",
    help="Output path for examiner package JSON",
    show_default=True,
)
def examiner_package_command(output_path: str):
    """Generate examiner-ready evidence package."""
    config = _load_config()
    components = _build_components(config)

    console.print("\n[bold blue]Generating Examiner Evidence Package[/bold blue]")
    package = components["reporter"].generate_examiner_package(output_path)

    agents_count = len(package.get("agents", []))
    open_issues = package.get("summary", {}).get("open_issues", 0)
    console.print(
        f"\n[dim]Package summary: {agents_count} agent(s), {open_issues} open issue(s)[/dim]"
    )


@cli.command("register")
@click.option("--name", required=True, help="Agent name (unique identifier)")
@click.option(
    "--domain",
    required=True,
    type=click.Choice(
        [
            "TRANSACTION_MONITORING",
            "KYC_CDD_EDD",
            "SAR_UAR_FILING",
            "OSINT_INVESTIGATION",
            "SANCTIONS_SCREENING",
            "REGULATORY_RESEARCH",
        ]
    ),
    help="Agent domain",
)
@click.option(
    "--risk-tier",
    required=True,
    type=click.Choice(["HIGH", "MEDIUM", "LOW"]),
    help="Risk tier (determines validation frequency)",
)
@click.option(
    "--system-prompt-file",
    required=True,
    type=click.Path(exists=True, readable=True),
    help="Path to file containing the agent's system prompt",
)
@click.option("--version", default="1.0.0", show_default=True, help="Agent version")
@click.option("--owner", default="Compliance Team", show_default=True, help="Responsible team/owner")
@click.option("--model", default="claude-sonnet-4-6", show_default=True, help="Model name for the agent")
def register_command(
    name: str,
    domain: str,
    risk_tier: str,
    system_prompt_file: str,
    version: str,
    owner: str,
    model: str,
):
    """Register a new compliance agent."""
    from src.models import AgentConfig, AgentDomain, RiskTier, AgentStatus

    config = _load_config()
    components = _build_components(config)
    registry = components["registry"]

    # Load system prompt from file
    with open(system_prompt_file, "r") as f:
        system_prompt = f.read().strip()

    if not system_prompt:
        console.print("[red]Error:[/red] System prompt file is empty.")
        sys.exit(1)

    agent_config = AgentConfig(
        name=name,
        domain=AgentDomain(domain),
        risk_tier=RiskTier(risk_tier),
        system_prompt=system_prompt,
        model=model,
        version=version,
        owner=owner,
        status=AgentStatus.PENDING,
    )

    registry.register_agent(agent_config)
    console.print(
        f"\n[green]Agent registered successfully:[/green]\n"
        f"  Name: [bold]{name}[/bold]\n"
        f"  Domain: {domain}\n"
        f"  Risk Tier: {risk_tier}\n"
        f"  Version: {version}\n"
        f"  Owner: {owner}\n"
        f"\n[dim]Run 'python main.py validate --agent {name}' to validate now.[/dim]"
    )


if __name__ == "__main__":
    cli()
