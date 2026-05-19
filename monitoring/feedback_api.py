"""
Analyst Feedback Capture API — Layer 3 of the monitoring architecture.

A FastAPI service that receives override/annotation records from the analyst
workflow tool and writes them to compliance.ai_agent_monitoring.analyst_feedback.

Deploy alongside the agent workflow or as a sidecar. All records are tied to
a specific invocation_id for ground-truth accuracy measurement.

Run:  uvicorn feedback_api:app --host 0.0.0.0 --port 8080
"""

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

import snowflake.connector
from fastapi import FastAPI, HTTPException, Header, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="Agent Feedback API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Snowflake connection (one connection pool per process)
# ---------------------------------------------------------------------------

def _get_conn() -> snowflake.connector.SnowflakeConnection:
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        private_key=os.environ.get("SNOWFLAKE_PRIVATE_KEY"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPLIANCE_WH"),
        database="compliance",
        schema="ai_agent_monitoring",
        role=os.environ.get("SNOWFLAKE_ROLE", "COMPLIANCE_ANALYST"),
    )


# ---------------------------------------------------------------------------
# Auth — simple shared secret header; swap for OAuth in production
# ---------------------------------------------------------------------------
API_SECRET = os.getenv("FEEDBACK_API_SECRET", "")

def verify_token(x_api_key: str = Header(...)):
    if API_SECRET and not hmac_compare(x_api_key, API_SECRET):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def hmac_compare(a: str, b: str) -> bool:
    return hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

ActionType = Literal["ACCEPTED", "MODIFIED", "REJECTED", "ESCALATED_OVERRIDE", "CLOSED_OVERRIDE"]
ReasonCode = Literal["INACCURATE", "INCOMPLETE", "OVER_ESCALATED", "UNDER_ESCALATED", "REGULATORY_GAP", "OTHER"]


class FeedbackRequest(BaseModel):
    invocation_id: str = Field(..., description="UUID of the agent invocation being reviewed")
    agent_name: str = Field(..., min_length=1)
    analyst_id: str = Field(..., description="Analyst user ID from the workflow tool")
    action: ActionType
    reason_code: ReasonCode | None = None
    modification_summary: str | None = Field(None, max_length=2000)
    original_escalation: bool | None = None
    analyst_escalation: bool | None = None

    @field_validator("invocation_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("invocation_id must be a valid UUID")
        return v


class FeedbackResponse(BaseModel):
    feedback_id: str
    invocation_id: str
    agent_name: str
    action: str
    submitted_at: str


class HealthResponse(BaseModel):
    status: str
    timestamp: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/feedback", response_model=FeedbackResponse, status_code=201,
          dependencies=[Depends(verify_token)])
def submit_feedback(body: FeedbackRequest):
    feedback_id = str(uuid.uuid4())
    submitted_at = datetime.now(timezone.utc)

    sql = """
        INSERT INTO analyst_feedback
        (feedback_id, invocation_id, agent_name, analyst_id, action,
         reason_code, modification_summary, original_escalation,
         analyst_escalation, submitted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::TIMESTAMP_TZ)
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            # Verify invocation_id exists
            cur.execute(
                "SELECT invocation_id FROM agent_invocation_log WHERE invocation_id = %s",
                (body.invocation_id,)
            )
            if cur.fetchone() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"invocation_id {body.invocation_id} not found",
                )
            cur.execute(sql, (
                feedback_id,
                body.invocation_id,
                body.agent_name,
                body.analyst_id,
                body.action,
                body.reason_code,
                body.modification_summary,
                body.original_escalation,
                body.analyst_escalation,
                submitted_at.isoformat(),
            ))
        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    return FeedbackResponse(
        feedback_id=feedback_id,
        invocation_id=body.invocation_id,
        agent_name=body.agent_name,
        action=body.action,
        submitted_at=submitted_at.isoformat(),
    )


@app.get("/feedback/{invocation_id}", dependencies=[Depends(verify_token)])
def get_feedback_for_invocation(invocation_id: str):
    """Retrieve all feedback records for a given invocation."""
    try:
        uuid.UUID(invocation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid invocation_id format")

    sql = """
        SELECT feedback_id, analyst_id, action, reason_code,
               modification_summary, submitted_at
        FROM analyst_feedback
        WHERE invocation_id = %s
        ORDER BY submitted_at DESC
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, (invocation_id,))
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return [
        {
            "feedback_id": r[0],
            "analyst_id": r[1],
            "action": r[2],
            "reason_code": r[3],
            "modification_summary": r[4],
            "submitted_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


@app.get("/agents/{agent_name}/override-rate", dependencies=[Depends(verify_token)])
def get_override_rate(agent_name: str, days: int = 30):
    """Current override rate for an agent over the last N days."""
    sql = """
        SELECT
            COUNT(*) AS total,
            COUNT_IF(action IN ('MODIFIED','REJECTED','ESCALATED_OVERRIDE','CLOSED_OVERRIDE')) AS overrides
        FROM analyst_feedback
        WHERE agent_name = %s
          AND submitted_at >= DATEADD('day', %s, CURRENT_TIMESTAMP())
    """
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, (agent_name, -days))
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    total = row[0] or 0
    overrides = row[1] or 0
    return {
        "agent_name": agent_name,
        "window_days": days,
        "total_reviewed": total,
        "override_count": overrides,
        "override_rate": round(overrides / total, 4) if total > 0 else None,
    }
