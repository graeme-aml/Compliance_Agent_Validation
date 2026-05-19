"""
Automated Evaluation Engine — Layer 2 of the monitoring architecture.

Runs on a scheduled cadence (daily for High-tier, weekly for Medium-tier)
against the Snowflake invocation log. Executes all rule-based checks and
statistical drift monitors, writes results, and dispatches alerts.

Usage:
    python evaluation_engine.py --agent osint-kyc-kyb-search
    python evaluation_engine.py --run-all
    python evaluation_engine.py --run-all --dry-run
"""

import argparse
import os
import uuid
from datetime import datetime, timedelta, timezone

import snowflake.connector
from rich.console import Console
from rich.table import Table

from rule_checks import ALL_CHECKS, write_check_results, CheckResult
from drift_monitor import ALL_DRIFT_MONITORS, DriftResult
from alerting import dispatch_alerts

console = Console()


# ---------------------------------------------------------------------------
# Snowflake connection
# ---------------------------------------------------------------------------

def get_connection() -> snowflake.connector.SnowflakeConnection:
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
# Agent lookup
# ---------------------------------------------------------------------------

def get_agents_due(conn, risk_tier: str | None = None) -> list[dict]:
    """Return agents that are APPROVED or CONDITIONAL (active in production)."""
    sql = """
        SELECT agent_name, domain, risk_tier, status
        FROM compliance.ai_agent_monitoring.agent_registry
        WHERE status IN ('APPROVED', 'CONDITIONAL')
    """
    params = []
    if risk_tier:
        sql += " AND risk_tier = %s"
        params.append(risk_tier)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_agent(conn, agent_name: str) -> dict | None:
    sql = """
        SELECT agent_name, domain, risk_tier, status
        FROM compliance.ai_agent_monitoring.agent_registry
        WHERE agent_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (agent_name,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0].lower() for d in cur.description]
        return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Evaluation windows
# ---------------------------------------------------------------------------

def _windows(risk_tier: str) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """Return (current_window, baseline_window) based on risk tier."""
    now = datetime.now(timezone.utc)
    if risk_tier == "HIGH":
        current_days = 1
        baseline_days = 7
    elif risk_tier == "MEDIUM":
        current_days = 7
        baseline_days = 30
    else:
        current_days = 30
        baseline_days = 90

    current_start = now - timedelta(days=current_days)
    baseline_start = now - timedelta(days=current_days + baseline_days)
    baseline_end = current_start

    return (current_start, now), (baseline_start, baseline_end)


# ---------------------------------------------------------------------------
# Core evaluation run
# ---------------------------------------------------------------------------

def run_evaluation(conn, agent: dict, dry_run: bool = False) -> tuple[list[CheckResult], list[DriftResult]]:
    agent_name = agent["agent_name"]
    risk_tier = agent["risk_tier"]
    run_id = str(uuid.uuid4())

    (current_start, current_end), (baseline_start, baseline_end) = _windows(risk_tier)

    console.print(f"\n[bold cyan]Evaluating:[/] {agent_name} ({risk_tier} tier) — run {run_id}")
    console.print(f"  Window: {current_start.strftime('%Y-%m-%d %H:%M')} → {current_end.strftime('%Y-%m-%d %H:%M')} UTC")

    # Rule-based checks
    check_results = []
    for check_fn in ALL_CHECKS:
        try:
            result = check_fn(conn, agent_name, run_id, current_start, current_end)
            check_results.append(result)
            status_icon = "✅" if result.passed else "❌"
            console.print(
                f"  {status_icon} {result.check_name}: {result.pass_rate:.1%} "
                f"(threshold {result.threshold:.1%}, {result.invocations_checked} invocations)"
            )
        except Exception as e:
            console.print(f"  [yellow]⚠ {check_fn.__name__} error: {e}[/]")

    # Drift monitors
    drift_results = []
    for monitor_fn in ALL_DRIFT_MONITORS:
        try:
            result = monitor_fn(conn, agent_name, current_start, current_end, baseline_start, baseline_end)
            drift_results.append(result)
            drift_icon = "🔴" if result.drift_detected else "🟢"
            console.print(
                f"  {drift_icon} drift/{result.monitor_name}: "
                f"current={result.current_value}, baseline={result.baseline_value}, "
                f"dev={f'{result.deviation_pct:.1f}%' if result.deviation_pct is not None else 'n/a'}"
            )
        except Exception as e:
            console.print(f"  [yellow]⚠ {monitor_fn.__name__} error: {e}[/]")

    if dry_run:
        console.print("  [dim][DRY RUN] Results not written to Snowflake.[/]")
        return check_results, drift_results

    # Persist results
    write_check_results(conn, check_results)

    # Dispatch alerts for any failures
    alerts = dispatch_alerts(conn, check_results, drift_results, run_id)
    if alerts:
        console.print(f"  [bold red]{len(alerts)} alert(s) dispatched[/]")
        for a in alerts:
            console.print(f"    [{a.severity}] {a.title}")

    return check_results, drift_results


def _print_summary(all_check_results: list[CheckResult]):
    table = Table(title="Evaluation Summary", show_lines=True)
    table.add_column("Agent", style="cyan")
    table.add_column("Check")
    table.add_column("Dimension")
    table.add_column("Pass Rate", justify="right")
    table.add_column("Threshold", justify="right")
    table.add_column("Result", justify="center")

    for r in all_check_results:
        table.add_row(
            r.agent_name,
            r.check_name,
            r.scorecard_dimension,
            f"{r.pass_rate:.1%}",
            f"{r.threshold:.1%}",
            "✅ PASS" if r.passed else "❌ FAIL",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Log gap check — separate from scheduled runs (called on-demand or via cron)
# ---------------------------------------------------------------------------

def check_log_gap(conn, high_tier_agents: list[str], gap_threshold_hours: float = 1.0):
    """Alert if any High-tier agent has no invocations in the last N hours during business hours."""
    from alerting import alert_log_gap, write_alert

    now = datetime.now(timezone.utc)
    is_business_hours = 13 <= now.hour <= 22  # 9am–6pm ET in UTC
    if not is_business_hours:
        return

    window_start = now - timedelta(hours=gap_threshold_hours)
    sql = """
        SELECT agent_name, MAX(invoked_at) AS last_seen
        FROM compliance.ai_agent_monitoring.agent_invocation_log
        WHERE agent_name = %s AND invoked_at >= %s
        GROUP BY agent_name
    """
    for agent_name in high_tier_agents:
        with conn.cursor() as cur:
            cur.execute(sql, (agent_name, window_start.isoformat()))
            row = cur.fetchone()
        if row is None:
            gap_hours = gap_threshold_hours
            alert = alert_log_gap(agent_name, gap_hours)
            write_alert(conn, alert)
            console.print(f"[yellow]⚠ Log gap alert: {agent_name} — {gap_hours:.1f}h without invocations[/]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AI Agent Evaluation Engine")
    parser.add_argument("--agent", help="Evaluate a specific agent by name")
    parser.add_argument("--run-all", action="store_true", help="Evaluate all active agents")
    parser.add_argument("--dry-run", action="store_true", help="Run checks but do not write results or dispatch alerts")
    parser.add_argument("--check-log-gaps", action="store_true", help="Check for invocation log gaps on High-tier agents")
    args = parser.parse_args()

    if not args.agent and not args.run_all and not args.check_log_gaps:
        parser.error("Specify --agent <name>, --run-all, or --check-log-gaps")

    conn = get_connection()
    all_check_results = []

    try:
        if args.check_log_gaps:
            high_agents = [a["agent_name"] for a in get_agents_due(conn, risk_tier="HIGH")]
            check_log_gap(conn, high_agents)

        if args.agent:
            agent = get_agent(conn, args.agent)
            if not agent:
                console.print(f"[red]Agent '{args.agent}' not found in registry.[/]")
                return
            checks, _ = run_evaluation(conn, agent, dry_run=args.dry_run)
            all_check_results.extend(checks)

        elif args.run_all:
            agents = get_agents_due(conn)
            if not agents:
                console.print("[yellow]No active agents found in registry.[/]")
                return
            for agent in agents:
                checks, _ = run_evaluation(conn, agent, dry_run=args.dry_run)
                all_check_results.extend(checks)

        if all_check_results:
            _print_summary(all_check_results)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
