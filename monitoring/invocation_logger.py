"""
Invocation logger — instruments agent calls to write append-only records
to compliance.ai_agent_monitoring.agent_invocation_log in Snowflake.

Usage (wrap any agent call):
    with InvocationLogger(conn, agent_name="osint-kyc-kyb-search") as log:
        response = client.messages.create(...)
        log.record(response, input_text=input_text)
"""

import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import snowflake.connector


@dataclass
class InvocationRecord:
    invocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_name: str = ""
    agent_version_id: str = ""
    user_id: str | None = None
    session_id: str | None = None
    input_hash: str = ""
    input_token_count: int | None = None
    raw_output: str = ""
    output_token_count: int | None = None
    structured_signals: dict | None = None
    tool_calls: list | None = None
    latency_ms: int | None = None
    model_id: str | None = None
    stop_reason: str | None = None
    error_code: str | None = None
    invoked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_snowflake_row(self) -> dict:
        return {
            "INVOCATION_ID": self.invocation_id,
            "AGENT_NAME": self.agent_name,
            "AGENT_VERSION_ID": self.agent_version_id,
            "USER_ID": self.user_id,
            "SESSION_ID": self.session_id,
            "INPUT_HASH": self.input_hash,
            "INPUT_TOKEN_COUNT": self.input_token_count,
            "RAW_OUTPUT": self.raw_output,
            "OUTPUT_TOKEN_COUNT": self.output_token_count,
            "STRUCTURED_SIGNALS": json.dumps(self.structured_signals) if self.structured_signals else None,
            "TOOL_CALLS": json.dumps(self.tool_calls) if self.tool_calls else None,
            "LATENCY_MS": self.latency_ms,
            "MODEL_ID": self.model_id,
            "STOP_REASON": self.stop_reason,
            "ERROR_CODE": self.error_code,
            "INVOKED_AT": self.invoked_at.isoformat(),
        }


def _hash_input(text: str) -> str:
    """SHA-256 of PII-tokenized input. In production, tokenize PII before hashing."""
    return hashlib.sha256(text.encode()).hexdigest()


def _extract_tool_calls(response: Any) -> list | None:
    """Extract tool use blocks from an Anthropic message response."""
    if not hasattr(response, "content"):
        return None
    calls = [
        {"name": block.name, "input": block.input}
        for block in response.content
        if hasattr(block, "type") and block.type == "tool_use"
    ]
    return calls or None


def _extract_text(response: Any) -> str:
    """Extract concatenated text from an Anthropic message response."""
    if not hasattr(response, "content"):
        return str(response)
    parts = [
        block.text
        for block in response.content
        if hasattr(block, "type") and block.type == "text"
    ]
    return "\n".join(parts)


class InvocationLogger:
    """
    Context manager that wraps an agent call and logs the invocation to Snowflake.

    conn: snowflake.connector.SnowflakeConnection (or None for dry-run/local mode)
    """

    _INSERT_SQL = """
        INSERT INTO compliance.ai_agent_monitoring.agent_invocation_log
        (invocation_id, agent_name, agent_version_id, user_id, session_id,
         input_hash, input_token_count, raw_output, output_token_count,
         structured_signals, tool_calls, latency_ms, model_id, stop_reason,
         error_code, invoked_at)
        VALUES
        (%(INVOCATION_ID)s, %(AGENT_NAME)s, %(AGENT_VERSION_ID)s, %(USER_ID)s,
         %(SESSION_ID)s, %(INPUT_HASH)s, %(INPUT_TOKEN_COUNT)s, %(RAW_OUTPUT)s,
         %(OUTPUT_TOKEN_COUNT)s, PARSE_JSON(%(STRUCTURED_SIGNALS)s),
         PARSE_JSON(%(TOOL_CALLS)s), %(LATENCY_MS)s, %(MODEL_ID)s,
         %(STOP_REASON)s, %(ERROR_CODE)s, %(INVOKED_AT)s::TIMESTAMP_TZ)
    """

    def __init__(
        self,
        conn: "snowflake.connector.SnowflakeConnection | None",
        agent_name: str,
        agent_version_id: str,
        user_id: str | None = None,
        session_id: str | None = None,
        dry_run: bool = False,
    ):
        self._conn = conn
        self._agent_name = agent_name
        self._agent_version_id = agent_version_id
        self._user_id = user_id
        self._session_id = session_id
        self._dry_run = dry_run
        self._record: InvocationRecord | None = None
        self._start_ns: int = 0

    def __enter__(self) -> "InvocationLogger":
        self._start_ns = time.monotonic_ns()
        return self

    def record(self, response: Any, input_text: str, parser=None) -> str:
        """
        Call after receiving the agent response. Returns the invocation_id.

        parser: optional callable(raw_output, agent_name) -> dict of structured signals
        """
        elapsed_ms = (time.monotonic_ns() - self._start_ns) // 1_000_000
        raw_output = _extract_text(response)

        signals = None
        if parser:
            try:
                signals = parser(raw_output, self._agent_name)
            except Exception:
                pass

        self._record = InvocationRecord(
            agent_name=self._agent_name,
            agent_version_id=self._agent_version_id,
            user_id=self._user_id,
            session_id=self._session_id,
            input_hash=_hash_input(input_text),
            input_token_count=getattr(getattr(response, "usage", None), "input_tokens", None),
            raw_output=raw_output,
            output_token_count=getattr(getattr(response, "usage", None), "output_tokens", None),
            structured_signals=signals,
            tool_calls=_extract_tool_calls(response),
            latency_ms=elapsed_ms,
            model_id=getattr(response, "model", None),
            stop_reason=getattr(response, "stop_reason", None),
        )
        return self._record.invocation_id

    def record_error(self, error: Exception, input_text: str) -> str:
        elapsed_ms = (time.monotonic_ns() - self._start_ns) // 1_000_000
        self._record = InvocationRecord(
            agent_name=self._agent_name,
            agent_version_id=self._agent_version_id,
            user_id=self._user_id,
            session_id=self._session_id,
            input_hash=_hash_input(input_text),
            latency_ms=elapsed_ms,
            error_code=type(error).__name__,
        )
        return self._record.invocation_id

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._record is None:
            return False
        if self._dry_run:
            print(f"[DRY RUN] Would log invocation {self._record.invocation_id} for {self._agent_name}")
            return False
        if self._conn is None:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute(self._INSERT_SQL, self._record.to_snowflake_row())
        except Exception as e:
            # Log but never raise — invocation logging must not break the agent call
            print(f"[WARNING] Failed to write invocation log: {e}")
        return False


def get_or_register_version(
    conn: "snowflake.connector.SnowflakeConnection",
    agent_name: str,
    version_tag: str,
    model_id: str,
    system_prompt: str,
    config: dict,
    deployed_by: str | None = None,
    change_description: str | None = None,
) -> str:
    """
    Idempotently registers an agent version. Returns version_id.
    Raises if a version_id exists with conflicting prompt/model — forces explicit new version.
    """
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
    config_str = json.dumps(config, sort_keys=True)
    config_hash = hashlib.sha256(config_str.encode()).hexdigest()
    version_id = hashlib.sha256(
        f"{agent_name}:{prompt_hash}:{model_id}:{config_hash}".encode()
    ).hexdigest()

    check_sql = """
        SELECT version_id FROM compliance.ai_agent_monitoring.agent_versions
        WHERE version_id = %s
    """
    insert_sql = """
        INSERT INTO compliance.ai_agent_monitoring.agent_versions
        (version_id, agent_name, version_tag, model_id, prompt_hash, config_hash,
         system_prompt, config_json, deployed_by, change_description)
        VALUES (%s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s)
    """
    with conn.cursor() as cur:
        cur.execute(check_sql, (version_id,))
        if cur.fetchone() is None:
            cur.execute(insert_sql, (
                version_id, agent_name, version_tag, model_id,
                prompt_hash, config_hash, system_prompt, config_str,
                deployed_by, change_description,
            ))
            # Update live registry
            cur.execute("""
                UPDATE compliance.ai_agent_monitoring.agent_registry
                SET current_version_id = %s, updated_at = CURRENT_TIMESTAMP()
                WHERE agent_name = %s
            """, (version_id, agent_name))

    return version_id
