"""JSON-based evidence storage with BSA retention compliance."""

from __future__ import annotations

import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.models import ValidationCycle, utc_now

logger = logging.getLogger(__name__)

BSA_RETENTION_YEARS = 5


class EvidenceStore:
    """Stores and retrieves validation cycle evidence with BSA-compliant retention."""

    def __init__(self, base_path: str = "evidence", retention_years: int = BSA_RETENTION_YEARS):
        self.base_path = Path(base_path)
        self.retention_years = retention_years
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._check_retention()

    def _cycle_path(self, cycle: ValidationCycle) -> Path:
        """Compute storage path for a cycle: evidence/{year}/Q{quarter}/{agent}/{cycle_id}/"""
        dt = cycle.started_at
        year = dt.year
        quarter = (dt.month - 1) // 3 + 1
        return (
            self.base_path
            / str(year)
            / f"Q{quarter}"
            / cycle.agent_name
            / cycle.cycle_id
        )

    def save_cycle(self, cycle: ValidationCycle) -> Path:
        """Save a validation cycle to the evidence store (append-only JSON)."""
        cycle_dir = self._cycle_path(cycle)
        cycle_dir.mkdir(parents=True, exist_ok=True)

        # Save each component separately for auditability
        self._write_json(cycle_dir / "cycle_metadata.json", {
            "cycle_id": cycle.cycle_id,
            "agent_name": cycle.agent_name,
            "agent_version": cycle.agent_version,
            "risk_tier": cycle.risk_tier.value,
            "started_at": cycle.started_at.isoformat(),
            "completed_at": cycle.completed_at.isoformat() if cycle.completed_at else None,
            "total_test_cases": len(set(r.test_case_id for r in cycle.test_results)),
            "total_test_runs": len(cycle.test_results),
            "issues_created": len(cycle.issues_created),
            "saved_at": utc_now().isoformat(),
        })

        self._write_json(
            cycle_dir / "test_results.json",
            {"test_results": [json.loads(r.model_dump_json()) for r in cycle.test_results]},
        )

        if cycle.scorecard:
            self._write_json(
                cycle_dir / "scorecard.json",
                json.loads(cycle.scorecard.model_dump_json()),
            )

        if cycle.issues_created:
            self._write_json(
                cycle_dir / "issues_created.json",
                {"issues": [json.loads(i.model_dump_json()) for i in cycle.issues_created]},
            )

        # Write full cycle as single document
        self._write_json(
            cycle_dir / "full_cycle.json",
            json.loads(cycle.model_dump_json()),
        )

        # Update the cycle index for this agent
        self._update_cycle_index(cycle)

        logger.info(f"Saved evidence cycle {cycle.cycle_id} to {cycle_dir}")
        return cycle_dir

    def _write_json(self, path: Path, data: dict) -> None:
        """Write JSON data to a file. Append-only: never overwrites existing files."""
        if path.exists():
            logger.warning(
                f"Evidence file already exists (not overwriting): {path}. "
                "Writing versioned copy."
            )
            # Create a versioned copy instead of overwriting
            versioned_path = path.with_suffix(f".{utc_now().strftime('%H%M%S')}.json")
            path = versioned_path

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _update_cycle_index(self, cycle: ValidationCycle) -> None:
        """Update the cycle index file for an agent."""
        index_path = self.base_path / "cycle_index.json"
        index: dict = {}

        if index_path.exists():
            try:
                with open(index_path, "r") as f:
                    index = json.load(f)
            except Exception:
                index = {}

        agent_cycles = index.get(cycle.agent_name, [])

        # Add new cycle entry
        cycle_entry = {
            "cycle_id": cycle.cycle_id,
            "started_at": cycle.started_at.isoformat(),
            "completed_at": cycle.completed_at.isoformat() if cycle.completed_at else None,
            "disposition": cycle.scorecard.disposition.value if cycle.scorecard else None,
            "path": str(self._cycle_path(cycle)),
        }

        # Avoid duplicates
        existing_ids = {e["cycle_id"] for e in agent_cycles}
        if cycle.cycle_id not in existing_ids:
            agent_cycles.append(cycle_entry)

        index[cycle.agent_name] = agent_cycles
        index["_last_updated"] = utc_now().isoformat()

        with open(index_path, "w") as f:
            json.dump(index, f, indent=2, default=str)

    def load_cycle(self, cycle_id: str) -> Optional[ValidationCycle]:
        """Load a validation cycle by ID from the evidence store."""
        # Search for the cycle in the index
        index_path = self.base_path / "cycle_index.json"
        if not index_path.exists():
            return None

        try:
            with open(index_path, "r") as f:
                index = json.load(f)
        except Exception as e:
            logger.error(f"Error reading cycle index: {e}")
            return None

        # Find the cycle path
        for agent_name, cycles in index.items():
            if agent_name.startswith("_"):
                continue
            for entry in cycles:
                if entry["cycle_id"] == cycle_id:
                    cycle_path = Path(entry["path"]) / "full_cycle.json"
                    if cycle_path.exists():
                        with open(cycle_path, "r") as f:
                            data = json.load(f)
                        return ValidationCycle.model_validate(data)

        logger.warning(f"Cycle {cycle_id} not found in evidence store")
        return None

    def list_cycles(self, agent_name: str) -> list[str]:
        """Return list of cycle IDs for an agent, sorted by start time."""
        index_path = self.base_path / "cycle_index.json"
        if not index_path.exists():
            return []

        try:
            with open(index_path, "r") as f:
                index = json.load(f)
        except Exception:
            return []

        cycles = index.get(agent_name, [])
        # Sort by started_at
        cycles.sort(key=lambda c: c.get("started_at", ""))
        return [c["cycle_id"] for c in cycles]

    def generate_evidence_index(self) -> dict:
        """Generate a summary dict for examiner packages."""
        index_path = self.base_path / "cycle_index.json"
        summary: dict = {
            "generated_at": utc_now().isoformat(),
            "retention_years": self.retention_years,
            "agents": {},
        }

        if not index_path.exists():
            return summary

        try:
            with open(index_path, "r") as f:
                index = json.load(f)
        except Exception as e:
            logger.error(f"Error reading index: {e}")
            return summary

        for agent_name, cycles in index.items():
            if agent_name.startswith("_"):
                continue

            summary["agents"][agent_name] = {
                "total_cycles": len(cycles),
                "latest_cycle": cycles[-1] if cycles else None,
                "cycles": cycles,
            }

        return summary

    def _check_retention(self) -> None:
        """Log a warning if any evidence directories exceed the retention period."""
        cutoff_year = utc_now().year - self.retention_years
        for year_dir in self.base_path.iterdir():
            if not year_dir.is_dir():
                continue
            try:
                year = int(year_dir.name)
                if year < cutoff_year:
                    logger.warning(
                        f"Evidence directory {year_dir} exceeds {self.retention_years}-year "
                        f"BSA retention period. Consider archiving or secure deletion."
                    )
            except ValueError:
                pass  # Not a year directory
