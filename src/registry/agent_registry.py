"""Agent registry with CRUD operations and risk-tier-based validation scheduling."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.models import (
    AgentConfig,
    AgentDomain,
    AgentStatus,
    RiskTier,
    VALIDATION_CADENCE_DAYS,
    ValidationScorecard,
    utc_now,
)

logger = logging.getLogger(__name__)


DEFAULT_OSINT_SYSTEM_PROMPT = """You are an OSINT (Open Source Intelligence) investigation specialist
supporting KYC/KYB (Know Your Customer / Know Your Business) compliance processes.

Your role is to:
1. Analyze open-source information about individuals and entities
2. Identify adverse media, regulatory actions, and reputational risks
3. Disambiguate between entities with similar names
4. Provide structured findings with source citations
5. Flag uncertainty and recommend human review when evidence is ambiguous

You do NOT provide legal conclusions, make final compliance decisions, or clear alerts
without human review. Always present findings as investigative intelligence requiring
human judgment for final disposition.

When you encounter requests that exceed your investigative role (e.g., requests to make
legal determinations, approve transactions, or take autonomous actions), politely decline
and explain that such decisions require human review.

Format your responses with clear sections: Summary, Key Findings, Risk Indicators,
Source Quality Assessment, and Recommended Next Steps."""


class AgentRegistry:
    """Registry for AI agents used in BSA/AML compliance workflows."""

    def __init__(self, registry_path: str = "evidence/agent_registry.json"):
        self.registry_path = Path(registry_path)
        self._agents: dict[str, AgentConfig] = {}
        self._load()
        self._ensure_default_agents()

    def _load(self) -> None:
        """Load agents from JSON file."""
        if self.registry_path.exists():
            try:
                with open(self.registry_path, "r") as f:
                    data = json.load(f)
                for agent_data in data.get("agents", []):
                    config = AgentConfig.model_validate(agent_data)
                    self._agents[config.name] = config
                logger.info(f"Loaded {len(self._agents)} agents from registry")
            except Exception as e:
                logger.error(f"Error loading registry: {e}")
                self._agents = {}
        else:
            logger.info("No registry file found, starting fresh")

    def _save(self) -> None:
        """Save agents to JSON file."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": [
                json.loads(agent.model_dump_json()) for agent in self._agents.values()
            ],
            "last_updated": utc_now().isoformat(),
        }
        with open(self.registry_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        logger.debug(f"Saved {len(self._agents)} agents to registry")

    def _ensure_default_agents(self) -> None:
        """Pre-populate with known agents if not already registered."""
        if "osint-kyc-kyb-search" not in self._agents:
            default_agent = AgentConfig(
                name="osint-kyc-kyb-search",
                domain=AgentDomain.OSINT_INVESTIGATION,
                risk_tier=RiskTier.MEDIUM,
                system_prompt=DEFAULT_OSINT_SYSTEM_PROMPT,
                model="claude-sonnet-4-6",
                version="1.0.0",
                owner="KYC Compliance Team",
                status=AgentStatus.PENDING,
                last_validated=None,
                next_validation_due=utc_now(),  # Due immediately for initial validation
            )
            self._agents[default_agent.name] = default_agent
            self._save()
            logger.info("Pre-populated registry with default osint-kyc-kyb-search agent")

    def register_agent(self, config: AgentConfig) -> None:
        """Register a new agent or update existing one."""
        if config.next_validation_due is None:
            config.next_validation_due = utc_now()  # Due immediately for new agents
        self._agents[config.name] = config
        self._save()
        logger.info(f"Registered agent: {config.name} (tier={config.risk_tier})")

    def get_agent(self, name: str) -> Optional[AgentConfig]:
        """Get a specific agent by name."""
        return self._agents.get(name)

    def get_all(self) -> list[AgentConfig]:
        """Return all registered agents."""
        return list(self._agents.values())

    def get_agents_due_for_validation(self) -> list[AgentConfig]:
        """Return agents that are due for validation based on risk tier cadence."""
        now = utc_now()
        due_agents = []

        for agent in self._agents.values():
            if agent.status == AgentStatus.DECOMMISSIONED:
                continue

            if agent.next_validation_due is None or agent.last_validated is None:
                # Never validated — due immediately
                due_agents.append(agent)
                continue

            # Ensure timezone-aware comparison
            next_due = agent.next_validation_due
            if next_due.tzinfo is None:
                next_due = next_due.replace(tzinfo=timezone.utc)

            if now >= next_due:
                due_agents.append(agent)

        logger.info(f"Found {len(due_agents)} agents due for validation")
        return due_agents

    def update_validation_status(
        self, agent_name: str, scorecard: ValidationScorecard
    ) -> None:
        """Update agent's validation status after a completed cycle."""
        agent = self._agents.get(agent_name)
        if agent is None:
            logger.error(f"Agent not found: {agent_name}")
            return

        from src.models import Disposition

        now = utc_now()
        cadence_days = VALIDATION_CADENCE_DAYS[agent.risk_tier]

        agent.last_validated = now
        agent.next_validation_due = now + timedelta(days=cadence_days)

        # Map disposition to agent status
        if scorecard.disposition == Disposition.APPROVED:
            agent.status = AgentStatus.APPROVED
        elif scorecard.disposition == Disposition.CONDITIONAL_APPROVAL:
            agent.status = AgentStatus.CONDITIONAL
        elif scorecard.disposition == Disposition.NEEDS_REMEDIATION:
            agent.status = AgentStatus.NEEDS_REMEDIATION

        self._agents[agent_name] = agent
        self._save()
        logger.info(
            f"Updated validation status for {agent_name}: "
            f"disposition={scorecard.disposition}, next_due={agent.next_validation_due}"
        )

    def decommission_agent(self, agent_name: str) -> None:
        """Mark an agent as decommissioned."""
        agent = self._agents.get(agent_name)
        if agent:
            agent.status = AgentStatus.DECOMMISSIONED
            self._save()
            logger.info(f"Decommissioned agent: {agent_name}")

    def delete_agent(self, agent_name: str) -> bool:
        """Remove an agent from the registry."""
        if agent_name in self._agents:
            del self._agents[agent_name]
            self._save()
            logger.info(f"Deleted agent: {agent_name}")
            return True
        return False
