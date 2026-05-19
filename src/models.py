"""All shared Pydantic models for the Compliance Agent Validation framework."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_id() -> str:
    return str(uuid.uuid4())


class RiskTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentDomain(str, Enum):
    TRANSACTION_MONITORING = "TRANSACTION_MONITORING"
    KYC_CDD_EDD = "KYC_CDD_EDD"
    SAR_UAR_FILING = "SAR_UAR_FILING"
    OSINT_INVESTIGATION = "OSINT_INVESTIGATION"
    SANCTIONS_SCREENING = "SANCTIONS_SCREENING"
    REGULATORY_RESEARCH = "REGULATORY_RESEARCH"


class TestCaseType(str, Enum):
    STANDARD = "STANDARD"
    EDGE_CASE = "EDGE_CASE"
    ADVERSARIAL = "ADVERSARIAL"
    BOUNDARY = "BOUNDARY"


class AgentStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CONDITIONAL = "CONDITIONAL"
    NEEDS_REMEDIATION = "NEEDS_REMEDIATION"
    DECOMMISSIONED = "DECOMMISSIONED"


class Disposition(str, Enum):
    APPROVED = "APPROVED"
    CONDITIONAL_APPROVAL = "CONDITIONAL_APPROVAL"
    NEEDS_REMEDIATION = "NEEDS_REMEDIATION"


class IssueSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IssueStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


# SLA days by severity
SEVERITY_SLA_DAYS: dict[IssueSeverity, int] = {
    IssueSeverity.CRITICAL: 10,
    IssueSeverity.HIGH: 15,
    IssueSeverity.MEDIUM: 30,
    IssueSeverity.LOW: 45,
}

# Validation cadence in days by risk tier
VALIDATION_CADENCE_DAYS: dict[RiskTier, int] = {
    RiskTier.HIGH: 90,    # quarterly
    RiskTier.MEDIUM: 182,  # semi-annually
    RiskTier.LOW: 365,    # annually
}

# Minimum sample sizes by risk tier
MIN_SAMPLE_SIZES: dict[RiskTier, int] = {
    RiskTier.HIGH: 25,
    RiskTier.MEDIUM: 15,
    RiskTier.LOW: 10,
}

# Number of runs per test case for variance testing
RUNS_PER_CASE: dict[RiskTier, int] = {
    RiskTier.HIGH: 3,
    RiskTier.MEDIUM: 3,
    RiskTier.LOW: 2,
}


class AgentConfig(BaseModel):
    name: str
    domain: AgentDomain
    risk_tier: RiskTier
    system_prompt: str
    model: str = "claude-sonnet-4-6"
    version: str = "1.0.0"
    owner: str = "Compliance Team"
    created_at: datetime = Field(default_factory=utc_now)
    last_validated: Optional[datetime] = None
    next_validation_due: Optional[datetime] = None
    status: AgentStatus = AgentStatus.PENDING

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class TestCase(BaseModel):
    id: str = Field(default_factory=generate_id)
    domain: AgentDomain
    case_type: TestCaseType
    input_text: str
    expected_signals: list[str] = Field(default_factory=list)
    should_refuse: bool = False
    description: str = ""

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class TestResult(BaseModel):
    test_case_id: str
    agent_name: str
    agent_version: str
    run_number: int = 1
    input_text: str
    raw_output: str
    passed: bool
    failure_reason: Optional[str] = None
    latency_ms: float = 0.0
    timestamp: datetime = Field(default_factory=utc_now)
    judge_score: float = 0.0
    judge_reasoning: str = ""
    signals_found: list[str] = Field(default_factory=list)
    signals_missing: list[str] = Field(default_factory=list)
    case_type: TestCaseType = TestCaseType.STANDARD

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class ScorecardDimension(BaseModel):
    name: str
    weight: str  # "high" or "medium"
    passed: bool
    score: float  # 0.0 to 1.0
    threshold: float
    notes: str = ""


class ValidationScorecard(BaseModel):
    agent_name: str
    agent_version: str
    cycle_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    dimensions: list[ScorecardDimension] = Field(default_factory=list)
    disposition: Optional[Disposition] = None
    overall_notes: str = ""

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class Issue(BaseModel):
    id: str = Field(default_factory=generate_id)
    agent_name: str
    severity: IssueSeverity
    title: str
    description: str
    dimension: str
    created_at: datetime = Field(default_factory=utc_now)
    due_date: Optional[datetime] = None
    status: IssueStatus = IssueStatus.OPEN
    remediation_owner: str = "Compliance Team"
    cycle_id: str = ""

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class ValidationCycle(BaseModel):
    cycle_id: str = Field(default_factory=generate_id)
    agent_name: str
    agent_version: str
    risk_tier: RiskTier
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    test_results: list[TestResult] = Field(default_factory=list)
    scorecard: Optional[ValidationScorecard] = None
    issues_created: list[Issue] = Field(default_factory=list)

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class JudgeEvaluation(BaseModel):
    passed: bool
    score: float
    reasoning: str
    signals_found: list[str] = Field(default_factory=list)
    signals_missing: list[str] = Field(default_factory=list)
