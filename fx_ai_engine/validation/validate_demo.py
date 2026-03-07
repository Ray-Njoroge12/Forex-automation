"""Pre-live 30-day demo validation harness.

Reads the SQLite trading database and evaluates whether the 30-day demo
period meets an explicit validation profile.

Usage:
    python -m validation.validate_demo                               # Core SRS profile (default)
    python -m validation.validate_demo --days 7                      # Core SRS spot check
    python -m validation.validate_demo --profile preserve_10_wave_a  # Preserve-$10 Wave A safety gate
    python -m validation.validate_demo --profile preserve_10_operational  # Preserve-$10 operational proof

Core SRS Acceptance Criteria (§12.2):
    >=25 trades | >=45% win rate | >=2.0 avg R | <=15% max drawdown

Core SRS Abort Criteria (§12.3):
    drawdown >20% | win rate <40% | avg R <1.8

Preserve-$10 Wave A Safety Gate:
    drawdown <=15% | late MT5 lot rejects = 0 | stale/reconciliation failures = 0
    This gate is Preserve-$10 doctrine evidence only. It is not Core SRS live-readiness proof.

Verdicts:
    PASS    -- selected validation profile satisfied
    ABORT   -- selected validation profile hit a hard-stop threshold
    WARN    -- selected validation profile not satisfied, but no hard-stop threshold hit
    PENDING -- insufficient evidence for the selected validation profile
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "database" / "trading_state.db"


@dataclass(frozen=True)
class ValidationProfile:
    profile_id: str
    label: str
    evidence_label: str
    purpose: str

# SRS v1 §12.2 -- Acceptance
MIN_TRADES = 25
MIN_WIN_RATE = 0.45
MIN_AVG_R = 2.0
MAX_DRAWDOWN = 0.15

# SRS v1 §12.3 -- Abort
ABORT_WIN_RATE = 0.40
ABORT_AVG_R = 1.8
ABORT_DRAWDOWN = 0.20

PRESERVE_10_CRITICAL_TRADE_REASONS = ("REJECTED_LOT",)
PRESERVE_10_CRITICAL_RISK_RULES = ("STATE_STALE", "STATE_RECONCILIATION_FAILED")

CORE_SRS_PROFILE = ValidationProfile(
    profile_id="core_srs",
    label="Core SRS 30-day demo validation",
    evidence_label="Core SRS v1",
    purpose="Locked SRS §12 live-readiness evidence.",
)

PRESERVE_10_WAVE_A_PROFILE = ValidationProfile(
    profile_id="preserve_10_wave_a",
    label="Preserve-$10 Wave A safety gate",
    evidence_label="Preserve-$10 doctrine",
    purpose="Wave A survival proof only; does not replace Core SRS live-readiness validation.",
)

PRESERVE_10_OPERATIONAL_PROFILE = ValidationProfile(
    profile_id="preserve_10_operational",
    label="Preserve-$10 operational proof",
    evidence_label="Preserve-$10 operational proof",
    purpose="Preserve-first operational evidence only; does not replace Core SRS live-readiness validation.",
)

VALIDATION_PROFILES = {
    CORE_SRS_PROFILE.profile_id: CORE_SRS_PROFILE,
    PRESERVE_10_WAVE_A_PROFILE.profile_id: PRESERVE_10_WAVE_A_PROFILE,
    PRESERVE_10_OPERATIONAL_PROFILE.profile_id: PRESERVE_10_OPERATIONAL_PROFILE,
}


def _load_trades(days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT trade_id, symbol, direction, r_multiple, profit_loss,
               status, close_time, reason_code
          FROM trades
         WHERE status LIKE 'CLOSED%'
           AND close_time >= ?
         ORDER BY close_time ASC
        """,
        (since,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _load_equity_curve(days: int) -> list[float]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT equity FROM account_metrics WHERE timestamp >= ? ORDER BY timestamp ASC",
        (since,),
    ).fetchall()
    conn.close()
    return [float(row["equity"]) for row in rows if row["equity"] is not None]


def _count_trade_reasons(days: int, reason_codes: tuple[str, ...]) -> dict[str, int]:
    counts = {code: 0 for code in reason_codes}
    if not DB_PATH.exists() or not reason_codes:
        return counts
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    placeholders = ", ".join("?" for _ in reason_codes)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT reason_code, COUNT(*) AS total
          FROM trades
         WHERE reason_code IN ({placeholders})
           AND COALESCE(close_time, open_time, execution_time) >= ?
         GROUP BY reason_code
        """,
        (*reason_codes, since),
    ).fetchall()
    conn.close()
    for row in rows:
        counts[str(row["reason_code"])] = int(row["total"])
    return counts


def _count_risk_rules(days: int, rule_names: tuple[str, ...]) -> dict[str, int]:
    counts = {rule: 0 for rule in rule_names}
    if not DB_PATH.exists() or not rule_names:
        return counts
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    placeholders = ", ".join("?" for _ in rule_names)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT rule_name, COUNT(*) AS total
          FROM risk_events
         WHERE rule_name IN ({placeholders})
           AND severity = 'BLOCK'
           AND timestamp >= ?
         GROUP BY rule_name
        """,
        (*rule_names, since),
    ).fetchall()
    conn.close()
    for row in rows:
        counts[str(row["rule_name"])] = int(row["total"])
    return counts


def _count_risk_events(days: int, *, rule_name: str, severity: str | None = None) -> int:
    if not DB_PATH.exists():
        return 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if severity is None:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
              FROM risk_events
             WHERE rule_name = ?
               AND timestamp >= ?
            """,
            (rule_name, since),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
              FROM risk_events
             WHERE rule_name = ?
               AND severity = ?
               AND timestamp >= ?
            """,
            (rule_name, severity, since),
        ).fetchone()
    conn.close()
    return int((row or {"total": 0})["total"] or 0)


def _load_account_metric_summary(days: int) -> dict[str, int]:
    if not DB_PATH.exists():
        return {"account_metric_samples": 0, "halted_account_samples": 0}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN is_trading_halted = 1 THEN 1 ELSE 0 END), 0) AS halted
          FROM account_metrics
         WHERE timestamp >= ?
        """,
        (since,),
    ).fetchone()
    conn.close()
    return {
        "account_metric_samples": int((row or {"total": 0})["total"] or 0),
        "halted_account_samples": int((row or {"halted": 0})["halted"] or 0),
    }


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Return maximum peak-to-trough drawdown as a fraction (0.0-1.0)."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def _compute_metrics(trades: list[dict], equity_curve: list[float]) -> dict:
    total = len(trades)
    if total == 0:
        return {"total_trades": 0, "win_rate": 0.0, "avg_r": 0.0, "avg_r_winners": 0.0,
                "max_drawdown": _compute_max_drawdown(equity_curve),
                "wins": 0, "losses": 0, "r_multiples": []}
    r_multiples = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    wins = sum(1 for r in r_multiples if r > 0)
    losses = total - wins
    win_rate = wins / total if total > 0 else 0.0
    winning_r = [r for r in r_multiples if r > 0]
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0
    avg_r_winners = sum(winning_r) / len(winning_r) if winning_r else 0.0
    warnings: list[str] = []
    zero_r = sum(1 for r in r_multiples if abs(r) < 1e-9)
    if r_multiples and zero_r == len(r_multiples):
        warnings.append("All closed trades have zero r_multiple; execution/exit analytics may be incomplete.")
    zero_pnl = sum(1 for t in trades if abs(float(t.get("profit_loss", 0.0))) < 1e-9)
    if trades and zero_pnl == len(trades):
        warnings.append("All closed trades have zero PnL; verify exit feedback ingestion.")
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "avg_r": round(avg_r, 4),
        "avg_r_winners": round(avg_r_winners, 4),
        "max_drawdown": _compute_max_drawdown(equity_curve),
        "wins": wins,
        "losses": losses,
        "r_multiples": r_multiples,
        "warnings": warnings,
    }


def _per_symbol_breakdown(trades: list[dict]) -> dict[str, dict]:
    from collections import defaultdict
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        groups[t["symbol"]].append(t)
    result = {}
    for sym, sym_trades in sorted(groups.items()):
        r_vals = [float(t["r_multiple"]) for t in sym_trades if t["r_multiple"] is not None]
        wins = sum(1 for r in r_vals if r > 0)
        result[sym] = {
            "trades": len(sym_trades),
            "wins": wins,
            "win_rate": round(wins / len(sym_trades), 3) if sym_trades else 0.0,
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else 0.0,
        }
    return result


def _augment_preserve_10_metrics(days: int, metrics: dict) -> dict:
    trade_counts = _count_trade_reasons(days, PRESERVE_10_CRITICAL_TRADE_REASONS)
    risk_counts = _count_risk_rules(days, PRESERVE_10_CRITICAL_RISK_RULES)
    late_lot_rejections = trade_counts.get("REJECTED_LOT", 0)
    stale_state_failures = risk_counts.get("STATE_STALE", 0)
    reconciliation_failures = risk_counts.get("STATE_RECONCILIATION_FAILED", 0)
    metrics.update(
        {
            "late_lot_rejections": late_lot_rejections,
            "stale_state_failures": stale_state_failures,
            "reconciliation_failures": reconciliation_failures,
            "critical_gate_failures": (
                late_lot_rejections + stale_state_failures + reconciliation_failures
            ),
            "observed_gate_events": (
                metrics["total_trades"]
                + late_lot_rejections
                + stale_state_failures
                + reconciliation_failures
            ),
        }
    )
    return metrics


def _augment_preserve_10_operational_metrics(days: int, metrics: dict) -> dict:
    startup_approval_passes = _count_risk_events(
        days,
        rule_name="PRESERVE_10_STARTUP_APPROVAL",
        severity="INFO",
    )
    startup_approval_failures = _count_risk_events(
        days,
        rule_name="PRESERVE_10_STARTUP_APPROVAL",
        severity="BLOCK",
    )
    preroute_rejections = _count_risk_events(
        days,
        rule_name="PRE_ROUTE_FEASIBILITY",
        severity="WARN",
    )
    halt_summary = _load_account_metric_summary(days)
    startup_approval_observations = startup_approval_passes + startup_approval_failures
    feasibility_observations = (
        metrics["total_trades"] + preroute_rejections + metrics["late_lot_rejections"]
    )
    restart_risk_anomalies = (
        metrics["stale_state_failures"] + metrics["reconciliation_failures"]
    )
    halted_account_samples = halt_summary["halted_account_samples"]
    account_metric_samples = halt_summary["account_metric_samples"]
    metrics.update(
        {
            "startup_approval_passes": startup_approval_passes,
            "startup_approval_failures": startup_approval_failures,
            "startup_approval_observations": startup_approval_observations,
            "preroute_rejections": preroute_rejections,
            "preroute_rejection_rate": round(
                (preroute_rejections / feasibility_observations), 4
            ) if feasibility_observations else 0.0,
            "feasibility_observations": feasibility_observations,
            "restart_risk_anomalies": restart_risk_anomalies,
            "account_metric_samples": account_metric_samples,
            "halted_account_samples": halted_account_samples,
            "halted_sample_rate": round(
                (halted_account_samples / account_metric_samples), 4
            ) if account_metric_samples else 0.0,
        }
    )
    return metrics


def _resolve_profile(profile_id: str) -> ValidationProfile:
    try:
        return VALIDATION_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(
            f"Unknown validation profile {profile_id!r}. "
            f"Expected one of: {', '.join(sorted(VALIDATION_PROFILES))}"
        ) from exc


def _determine_core_srs_verdict(metrics: dict) -> tuple[str, list[str]]:
    """Return (verdict, list_of_reason_strings)."""
    reasons: list[str] = []
    scope_note = (
        "Evidence scope: Core SRS v1 live-readiness only. "
        "Preserve-$10 doctrine and operational diagnostics are reported separately and do not change this verdict."
    )

    # Abort checks first (highest priority -- ordered by severity)
    if metrics["max_drawdown"] > ABORT_DRAWDOWN:
        reasons.append(
            f"ABORT: Core SRS drawdown {metrics['max_drawdown']:.1%} exceeded the locked {ABORT_DRAWDOWN:.0%} abort threshold."
        )
        reasons.append(scope_note)
        return "ABORT", reasons
    if metrics["total_trades"] >= MIN_TRADES and metrics["win_rate"] < ABORT_WIN_RATE:
        reasons.append(
            f"ABORT: Core SRS win rate {metrics['win_rate']:.1%} fell below the {ABORT_WIN_RATE:.0%} abort threshold."
        )
        reasons.append(scope_note)
        return "ABORT", reasons
    if metrics["total_trades"] >= MIN_TRADES and metrics["avg_r"] < ABORT_AVG_R:
        reasons.append(
            f"ABORT: Core SRS average R {metrics['avg_r']:.2f} fell below the {ABORT_AVG_R} abort threshold."
        )
        reasons.append(scope_note)
        return "ABORT", reasons

    # Pending -- not enough trades
    if metrics["total_trades"] < MIN_TRADES:
        reasons.append(
            f"PENDING: Core SRS validation has only {metrics['total_trades']}/{MIN_TRADES} closed trades; more demo evidence is required before a live-readiness verdict is possible."
        )
        reasons.append(scope_note)
        return "PENDING", reasons

    # Acceptance criteria
    fails: list[str] = []
    if metrics["win_rate"] < MIN_WIN_RATE:
        fails.append(
            f"Core SRS win rate {metrics['win_rate']:.1%} is below the {MIN_WIN_RATE:.0%} pass requirement."
        )
    if metrics["avg_r"] < MIN_AVG_R:
        fails.append(
            f"Core SRS average R {metrics['avg_r']:.2f} is below the {MIN_AVG_R} pass requirement."
        )
    if metrics["max_drawdown"] > MAX_DRAWDOWN:
        fails.append(
            f"Core SRS drawdown {metrics['max_drawdown']:.1%} is above the {MAX_DRAWDOWN:.0%} pass ceiling."
        )

    if fails:
        reasons.extend(fails)
        reasons.append(scope_note)
        return "WARN", reasons

    reasons.append("PASS: Core SRS §12.2 acceptance criteria satisfied.")
    reasons.append(scope_note)
    return "PASS", reasons


def _determine_preserve_10_wave_a_verdict(metrics: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    scope_note = (
        "Evidence scope: Preserve-$10 doctrine only. "
        "This does not satisfy Core SRS §12 live-readiness validation."
    )

    if metrics["max_drawdown"] > ABORT_DRAWDOWN:
        reasons.append(
            f"ABORT: Preserve-$10 Wave A drawdown {metrics['max_drawdown']:.1%} breached the {ABORT_DRAWDOWN:.0%} preserve safety floor."
        )
        reasons.append("Preserve-$10 doctrine evidence is invalidated until the drawdown breach is reviewed.")
        reasons.append(scope_note)
        return "ABORT", reasons

    if metrics["observed_gate_events"] == 0:
        reasons.append(
            "PENDING: Preserve-$10 Wave A still has no gate evidence in the selected window (no closed trades or tracked safety failures)."
        )
        reasons.append(scope_note)
        return "PENDING", reasons

    fails: list[str] = []
    if metrics["late_lot_rejections"] > 0:
        fails.append(
            f"Late MT5 lot rejections={metrics['late_lot_rejections']} (>0); Preserve-$10 Wave A expects infeasible trades to be blocked before MT5 routing."
        )
    if metrics["stale_state_failures"] > 0:
        fails.append(
            f"State stale blocks={metrics['stale_state_failures']} (>0); restart-safe risk authority is not yet stable enough for Preserve-$10 doctrine evidence."
        )
    if metrics["reconciliation_failures"] > 0:
        fails.append(
            f"State reconciliation failures={metrics['reconciliation_failures']} (>0); Preserve-$10 requires fail-closed trust in broker/local risk state."
        )
    if metrics["max_drawdown"] > MAX_DRAWDOWN:
        fails.append(
            f"Drawdown {metrics['max_drawdown']:.1%} is above the {MAX_DRAWDOWN:.0%} Wave A safety ceiling."
        )

    if fails:
        reasons.extend(fails)
        reasons.append("Preserve-$10 Wave A gate is not satisfied.")
        reasons.append(scope_note)
        return "WARN", reasons

    reasons.append(
        "PASS: Preserve-$10 Wave A safety gate stayed clean: no late lot rejects, no stale-state/reconciliation failures, and drawdown remained within the 15% safety ceiling."
    )
    reasons.append(scope_note)
    return "PASS", reasons


def _determine_preserve_10_operational_verdict(metrics: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    scope_note = (
        "Evidence scope: Preserve-$10 operational proof only. "
        "This does not satisfy Core SRS §12 live-readiness validation."
    )

    if metrics["max_drawdown"] > ABORT_DRAWDOWN:
        reasons.append(
            f"ABORT: Preserve-$10 operational drawdown {metrics['max_drawdown']:.1%} breached the {ABORT_DRAWDOWN:.0%} preserve safety floor."
        )
        reasons.append("Preserve-$10 operational proof is invalidated until the drawdown breach is reviewed.")
        reasons.append(scope_note)
        return "ABORT", reasons

    fails: list[str] = []
    if metrics["startup_approval_failures"] > 0:
        fails.append(
            f"Startup approval refusals={metrics['startup_approval_failures']} (>0); operators saw Preserve-$10 startup approval fail closed before trading could begin cleanly."
        )
    if metrics["preroute_rejections"] > 0:
        fails.append(
            f"Pre-route refusals={metrics['preroute_rejections']}/{metrics['feasibility_observations']} ({metrics['preroute_rejection_rate']:.1%}); operators saw trades blocked before MT5 routing, so broker/account viability is not yet operationally clean."
        )
    if metrics["late_lot_rejections"] > 0:
        fails.append(
            f"Late MT5 lot rejections={metrics['late_lot_rejections']} (>0); Preserve-$10 expects infeasible trades to fail before routing."
        )
    if metrics["restart_risk_anomalies"] > 0:
        fails.append(
            f"Restart/state-authority anomalies={metrics['restart_risk_anomalies']} (>0); stale-state blocks or reconciliation failures were observed during the proof window."
        )
    if metrics["halted_account_samples"] > 0:
        fails.append(
            f"Halt outcomes observed in {metrics['halted_account_samples']}/{metrics['account_metric_samples']} runtime samples ({metrics['halted_sample_rate']:.1%}); Preserve-$10 operational proof expects a clean window with no account-level halts."
        )
    if metrics["max_drawdown"] > MAX_DRAWDOWN:
        fails.append(
            f"Drawdown {metrics['max_drawdown']:.1%} is above the {MAX_DRAWDOWN:.0%} Preserve-$10 operational safety ceiling."
        )

    if fails:
        reasons.extend(fails)
        reasons.append("Preserve-$10 operational proof is not satisfied.")
        reasons.append(scope_note)
        return "WARN", reasons

    missing_evidence: list[str] = []
    if metrics["startup_approval_observations"] == 0:
        missing_evidence.append("startup approval evidence")
    if metrics["feasibility_observations"] == 0:
        missing_evidence.append("pre-route refusal evidence")
    if metrics["account_metric_samples"] == 0:
        missing_evidence.append("halt outcome evidence")

    if missing_evidence:
        reasons.append(
            "PENDING: Preserve-$10 operational proof still needs "
            + ", ".join(missing_evidence)
            + " before the window is operator-complete."
        )
        reasons.append(scope_note)
        return "PENDING", reasons

    reasons.append(
        "PASS: Preserve-$10 operational proof stayed clean: startup approval passed, no pre-route or late MT5 lot rejects were observed, no stale-state/reconciliation blocks occurred, no halt outcomes were observed, and drawdown stayed within the 15% safety ceiling."
    )
    reasons.append(scope_note)
    return "PASS", reasons


def determine_verdict(metrics: dict, profile_id: str = "core_srs") -> tuple[str, list[str]]:
    """Return (verdict, list_of_reason_strings) for the selected validation profile."""
    if profile_id == PRESERVE_10_WAVE_A_PROFILE.profile_id:
        return _determine_preserve_10_wave_a_verdict(metrics)
    if profile_id == PRESERVE_10_OPERATIONAL_PROFILE.profile_id:
        return _determine_preserve_10_operational_verdict(metrics)
    return _determine_core_srs_verdict(metrics)


def check_abort_criteria(
    drawdown_pct: float,
    win_rate: float,
    avg_r: float,
    total_trades: int,
) -> dict:
    """Programmatic abort check per SRS v1 §12.3.

    Returns dict with keys:
      abort (bool) — True if any abort threshold is breached
      reason (str) — human-readable reason, empty string if no abort

    Win rate and avg R are only evaluated once >= 25 trades have closed.
    Drawdown is always evaluated regardless of trade count.
    """
    if drawdown_pct > ABORT_DRAWDOWN:
        return {
            "abort": True,
            "reason": f"drawdown {drawdown_pct:.1%} exceeds {ABORT_DRAWDOWN:.0%} abort threshold",
        }
    if total_trades >= MIN_TRADES:
        if win_rate < ABORT_WIN_RATE:
            return {
                "abort": True,
                "reason": f"win rate {win_rate:.1%} below {ABORT_WIN_RATE:.0%} abort threshold",
            }
        if avg_r < ABORT_AVG_R:
            return {
                "abort": True,
                "reason": f"avg R {avg_r:.2f} below {ABORT_AVG_R} abort threshold",
            }
    return {"abort": False, "reason": ""}


def _print_report(
    profile: ValidationProfile,
    metrics: dict,
    breakdown: dict,
    verdict: str,
    reasons: list[str],
    days: int,
) -> None:
    bar = "=" * 62
    print(f"\n{bar}")
    print(f"  FX AI Engine -- Pre-Live Validation Report")
    print(f"  Period: last {days} days  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Profile: {profile.label}")
    print(f"  Evidence Label: {profile.evidence_label}")
    print(f"  Purpose: {profile.purpose}")
    print(f"{bar}")
    if profile.profile_id == CORE_SRS_PROFILE.profile_id:
        print(f"\n  Core Metrics                   Value       Requirement")
        print(f"  {'-' * 52}")
        print(f"  Total Trades               {metrics['total_trades']:>9}       >=25")
        print(f"  Win Rate                   {metrics['win_rate']:>8.1%}       >=45%")
        print(f"  Average R-Multiple (All)   {metrics['avg_r']:>9.2f}       >=2.0")
        print(f"  Average R-Multiple (Wins)  {metrics['avg_r_winners']:>9.2f}       diagnostic")
        print(f"  Max Drawdown               {metrics['max_drawdown']:>8.1%}       <=15%")
    elif profile.profile_id == PRESERVE_10_OPERATIONAL_PROFILE.profile_id:
        print(f"\n  Preserve-$10 Operator Checks  Value       Requirement")
        print(f"  {'-' * 52}")
        print(f"  Max Drawdown               {metrics['max_drawdown']:>8.1%}       <=15%")
        print(f"  Startup Approval Refusals  {metrics['startup_approval_failures']:>9}       =0")
        print(f"  Startup Approval Events    {metrics['startup_approval_observations']:>9}       >=1 evidence")
        print(f"  Pre-route Refusals         {metrics['preroute_rejections']:>9}       =0")
        print(f"  Pre-route Refusal Rate     {metrics['preroute_rejection_rate']:>8.1%}       =0% clean proof")
        print(f"  Late MT5 Lot Rejects       {metrics['late_lot_rejections']:>9}       =0")
        print(f"  State Authority Anomalies  {metrics['restart_risk_anomalies']:>9}       =0")
        print(f"  Halt Outcomes Observed     {metrics['halted_account_samples']:>9}       =0")
        print(f"  Halt Outcome Rate          {metrics['halted_sample_rate']:>8.1%}       =0% clean proof")
        print(f"  Runtime Samples            {metrics['account_metric_samples']:>9}       >=1 evidence")
        print(f"  Closed Trades             {metrics['total_trades']:>10}       diagnostic")
        print(f"  Win Rate                   {metrics['win_rate']:>8.1%}       diagnostic")
        print(f"  Average R-Multiple (All)   {metrics['avg_r']:>9.2f}       diagnostic")
    else:
        print(f"\n  Preserve-$10 Gate Checks      Value       Requirement")
        print(f"  {'-' * 52}")
        print(f"  Max Drawdown               {metrics['max_drawdown']:>8.1%}       <=15%")
        print(f"  Late MT5 Lot Rejects       {metrics['late_lot_rejections']:>9}       =0")
        print(f"  State Stale Failures       {metrics['stale_state_failures']:>9}       =0")
        print(f"  Reconciliation Failures    {metrics['reconciliation_failures']:>9}       =0")
        print(f"  Closed Trades             {metrics['total_trades']:>10}       diagnostic")
        print(f"  Win Rate                   {metrics['win_rate']:>8.1%}       diagnostic")
        print(f"  Average R-Multiple (All)   {metrics['avg_r']:>9.2f}       diagnostic")
    if breakdown:
        print(f"\n  Per-Symbol    Trades   Wins      WR   Avg R")
        print(f"  {'-' * 46}")
        for sym, d in breakdown.items():
            print(f"  {sym:<12} {d['trades']:>6} {d['wins']:>6} {d['win_rate']:>7.1%} {d['avg_r']:>7.2f}")
    print(f"\n{bar}")
    print(f"  VERDICT: {verdict}")
    for r in reasons:
        print(f"    -> {r}")
    for warning in metrics.get("warnings", []):
        print(f"    -> DATA WARNING: {warning}")
    print(f"{bar}\n")


def run_validation(days: int = 30, profile: str = "core_srs") -> tuple[str, dict]:
    """Run full validation. Returns (verdict, metrics) for programmatic use."""
    selected_profile = _resolve_profile(profile)
    trades = _load_trades(days)
    equity_curve = _load_equity_curve(days)
    metrics = _compute_metrics(trades, equity_curve)
    if selected_profile.profile_id in {
        PRESERVE_10_WAVE_A_PROFILE.profile_id,
        PRESERVE_10_OPERATIONAL_PROFILE.profile_id,
    }:
        metrics = _augment_preserve_10_metrics(days, metrics)
    if selected_profile.profile_id == PRESERVE_10_OPERATIONAL_PROFILE.profile_id:
        metrics = _augment_preserve_10_operational_metrics(days, metrics)
    breakdown = _per_symbol_breakdown(trades)
    verdict, reasons = determine_verdict(metrics, profile_id=selected_profile.profile_id)
    _print_report(selected_profile, metrics, breakdown, verdict, reasons, days)
    return verdict, metrics


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FX AI Engine pre-live validation")
    p.add_argument("--days", type=int, default=30, help="Days to analyse (default: 30)")
    p.add_argument(
        "--profile",
        choices=sorted(VALIDATION_PROFILES),
        default=CORE_SRS_PROFILE.profile_id,
        help="Validation profile to apply (default: core_srs)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    verdict, _ = run_validation(args.days, profile=args.profile)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
