"""APScheduler-based continuous validation scheduler."""

from __future__ import annotations

import logging
import signal
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console

from src.models import RiskTier, utc_now

logger = logging.getLogger(__name__)
console = Console()


class ValidationScheduler:
    """Schedules and runs periodic validation cycles for registered agents."""

    def __init__(
        self,
        evaluation_engine,
        registry,
        high_tier_interval_hours: int = 24,
        medium_tier_interval_hours: int = 168,
        low_tier_interval_hours: int = 720,
    ):
        self.engine = evaluation_engine
        self.registry = registry
        self.high_interval_hours = high_tier_interval_hours
        self.medium_interval_hours = medium_tier_interval_hours
        self.low_interval_hours = low_tier_interval_hours

        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone="UTC",
        )

    def start(self) -> None:
        """Start the scheduler with jobs based on risk tier intervals."""
        # Schedule a single job that checks for due agents at the highest-tier frequency
        # Each tier check runs its own subset of agents
        self._scheduler.add_job(
            func=self._check_high_tier,
            trigger=IntervalTrigger(hours=self.high_interval_hours),
            id="high_tier_check",
            name="High Risk Tier Validation Check",
            replace_existing=True,
        )

        self._scheduler.add_job(
            func=self._check_medium_tier,
            trigger=IntervalTrigger(hours=self.medium_interval_hours),
            id="medium_tier_check",
            name="Medium Risk Tier Validation Check",
            replace_existing=True,
        )

        self._scheduler.add_job(
            func=self._check_low_tier,
            trigger=IntervalTrigger(hours=self.low_interval_hours),
            id="low_tier_check",
            name="Low Risk Tier Validation Check",
            replace_existing=True,
        )

        self._scheduler.start()
        console.print(
            f"[green]Validation scheduler started.[/green]\n"
            f"  High tier check: every {self.high_interval_hours}h\n"
            f"  Medium tier check: every {self.medium_interval_hours}h\n"
            f"  Low tier check: every {self.low_interval_hours}h"
        )

        # Set up graceful shutdown
        self._setup_signal_handlers()

        # Keep alive until interrupted
        try:
            console.print("[dim]Press Ctrl+C to stop the scheduler.[/dim]")
            while True:
                time.sleep(60)
                self._log_next_runs()
        except (KeyboardInterrupt, SystemExit):
            self.stop()

    def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
            console.print("[yellow]Validation scheduler stopped.[/yellow]")

    def _setup_signal_handlers(self) -> None:
        """Set up SIGTERM and SIGINT handlers for graceful shutdown."""
        def handle_exit(signum, frame):
            logger.info(f"Received signal {signum}, shutting down scheduler...")
            self.stop()
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, handle_exit)
        signal.signal(signal.SIGINT, handle_exit)

    def _check_high_tier(self) -> None:
        """Check and run high-tier agents due for validation."""
        self._run_due_agents(tier_filter=RiskTier.HIGH)

    def _check_medium_tier(self) -> None:
        """Check and run medium-tier agents due for validation."""
        self._run_due_agents(tier_filter=RiskTier.MEDIUM)

    def _check_low_tier(self) -> None:
        """Check and run low-tier agents due for validation."""
        self._run_due_agents(tier_filter=RiskTier.LOW)

    def check_and_run_due_agents(self) -> list:
        """Check all tiers and run any agents due for validation."""
        return self._run_due_agents(tier_filter=None)

    def _run_due_agents(self, tier_filter: Optional[RiskTier] = None) -> list:
        """Get due agents (optionally filtered by tier) and run validation cycles."""
        due_agents = self.registry.get_agents_due_for_validation()

        if tier_filter is not None:
            due_agents = [a for a in due_agents if a.risk_tier == tier_filter]

        if not due_agents:
            tier_label = tier_filter.value if tier_filter else "all"
            logger.debug(f"No {tier_label}-tier agents due for validation")
            return []

        results = []
        for agent in due_agents:
            try:
                logger.info(
                    f"[Scheduler] Running validation for {agent.name} "
                    f"(tier={agent.risk_tier.value})"
                )
                cycle = self.engine.run_validation_cycle(agent)
                results.append(cycle)
                logger.info(
                    f"[Scheduler] Completed validation for {agent.name}: "
                    f"disposition={cycle.scorecard.disposition if cycle.scorecard else 'N/A'}"
                )
            except Exception as e:
                logger.error(
                    f"[Scheduler] Validation failed for {agent.name}: {e}", exc_info=True
                )

        return results

    def run_once(self, agent_name: Optional[str] = None) -> list:
        """Run validation immediately for one or all agents (for CLI use)."""
        if agent_name:
            agent = self.registry.get_agent(agent_name)
            if agent is None:
                console.print(f"[red]Agent not found:[/red] {agent_name}")
                return []
            agents = [agent]
        else:
            agents = self.registry.get_all()

        results = []
        for agent in agents:
            try:
                console.print(
                    f"[blue]Starting validation:[/blue] {agent.name} (tier={agent.risk_tier.value})"
                )
                cycle = self.engine.run_validation_cycle(agent)
                results.append(cycle)
                disposition = cycle.scorecard.disposition.value if cycle.scorecard else "N/A"
                console.print(
                    f"[green]Completed:[/green] {agent.name} → "
                    f"[bold]{disposition}[/bold]"
                )
            except Exception as e:
                logger.error(f"Validation failed for {agent.name}: {e}", exc_info=True)
                console.print(f"[red]Validation failed for {agent.name}:[/red] {e}")

        return results

    def _log_next_runs(self) -> None:
        """Log next scheduled run times for each job."""
        for job in self._scheduler.get_jobs():
            if job.next_run_time:
                logger.debug(f"Next run for '{job.name}': {job.next_run_time.isoformat()}")
