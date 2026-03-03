"""FX AI Engine — Monitoring Dashboard.

Run:
    streamlit run dashboard/app.py --server.port 8501

Reads from database/trading_state.db in read-only mode.
Refreshes on page reload or via the Refresh button in the sidebar.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Path resolution — support running from either project root or fx_ai_engine/
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ENGINE_ROOT = _HERE.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

DB_PATH = _ENGINE_ROOT / "database" / "trading_state.db"

# ---------------------------------------------------------------------------
# DB helpers (read-only; never commit)
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=30)
def load_account_metrics() -> pd.DataFrame:
    try:
        with _conn() as c:
            return pd.read_sql_query(
                "SELECT timestamp, balance, equity, drawdown_percent, "
                "daily_loss_percent, weekly_loss_percent, consecutive_losses, "
                "open_risk_percent, is_trading_halted "
                "FROM account_metrics ORDER BY timestamp",
                c,
            )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_trades(limit: int = 200) -> pd.DataFrame:
    try:
        with _conn() as c:
            return pd.read_sql_query(
                f"""
                SELECT trade_id, symbol, direction, status, reason_code,
                       risk_percent, stop_loss, take_profit, market_regime,
                       profit_loss, r_multiple, open_time, close_time
                  FROM trades
                 ORDER BY open_time DESC
                 LIMIT {limit}
                """,
                c,
            )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_risk_events(limit: int = 100) -> pd.DataFrame:
    try:
        with _conn() as c:
            return pd.read_sql_query(
                f"""
                SELECT timestamp, rule_name, severity, reason, trade_id
                  FROM risk_events
                 ORDER BY timestamp DESC
                 LIMIT {limit}
                """,
                c,
            )
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="FX AI Engine — Monitor",
    page_icon="📈",
    layout="wide",
)

st.title("FX AI Engine — Live Monitor")

# Sidebar
with st.sidebar:
    st.header("Controls")
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"DB: {DB_PATH}")

metrics_df = load_account_metrics()
trades_df = load_trades()
events_df = load_risk_events()

# ---------------------------------------------------------------------------
# Section 1 — Live Risk Gauges
# ---------------------------------------------------------------------------
st.subheader("Live Risk Gauges")

if metrics_df.empty:
    st.info("No account data yet. Start the engine to populate metrics.")
else:
    latest = metrics_df.iloc[-1]
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Equity", f"${float(latest.get('equity', 0)):,.2f}")
    col2.metric("Drawdown", f"{float(latest.get('drawdown_percent', 0)):.2%}")
    col3.metric("Daily loss", f"{float(latest.get('daily_loss_percent', 0)):.2%}")
    col4.metric("Open risk", f"{float(latest.get('open_risk_percent', 0)):.2%}")
    halted = bool(int(latest.get("is_trading_halted", 0)))
    col5.metric("Status", "🛑 HALTED" if halted else "✅ ACTIVE")

# ---------------------------------------------------------------------------
# Section 2 — Equity Curve
# ---------------------------------------------------------------------------
st.subheader("Equity Curve")

if not metrics_df.empty and "equity" in metrics_df.columns:
    plot_df = metrics_df.copy()
    plot_df["timestamp"] = pd.to_datetime(plot_df["timestamp"], errors="coerce")
    plot_df = plot_df.dropna(subset=["timestamp"]).set_index("timestamp")
    st.line_chart(plot_df["equity"])
else:
    st.info("Insufficient equity data.")

# ---------------------------------------------------------------------------
# Section 3 — Recent Trades P&L Waterfall
# ---------------------------------------------------------------------------
st.subheader("Recent Trades — P&L")

if trades_df.empty:
    st.info("No trades recorded yet.")
else:
    closed = trades_df[trades_df["status"].isin(["EXECUTED", "CLOSED"])].copy()
    if closed.empty:
        st.info("No closed trades yet.")
    else:
        closed["profit_loss"] = pd.to_numeric(closed["profit_loss"], errors="coerce").fillna(0)
        closed["colour"] = closed["profit_loss"].apply(lambda x: "green" if x >= 0 else "red")

        col_a, col_b, col_c = st.columns(3)
        wins = (closed["profit_loss"] > 0).sum()
        total_c = len(closed)
        col_a.metric("Closed trades", total_c)
        col_a.metric("Win rate", f"{wins / total_c:.1%}" if total_c else "N/A")
        avg_r = closed["r_multiple"].mean() if "r_multiple" in closed.columns else 0.0
        col_b.metric("Avg R", f"{avg_r:.2f}" if total_c else "N/A")
        col_b.metric("Total P&L", f"${closed['profit_loss'].sum():,.2f}")
        col_c.metric("Max DD (trades)", "see gauges above")

        # Mini bar chart of P&L per trade (last 50).
        pnl_series = closed.head(50)["profit_loss"].iloc[::-1]
        st.bar_chart(pnl_series)

    # Full trade table (filterable by Streamlit native).
    with st.expander("All trades (last 200)"):
        st.dataframe(trades_df, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 4 — Agent Rejection Breakdown
# ---------------------------------------------------------------------------
st.subheader("Agent Rejection Breakdown")

if trades_df.empty:
    st.info("No trade data.")
else:
    rejection_counts = (
        trades_df[trades_df["status"] == "REJECTED"]
        .groupby("reason_code")
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    if rejection_counts.empty:
        st.success("No rejections recorded.")
    else:
        st.bar_chart(rejection_counts.set_index("reason_code")["count"])

# ---------------------------------------------------------------------------
# Section 5 — Recent Risk Events
# ---------------------------------------------------------------------------
st.subheader("Recent Risk Events")

if events_df.empty:
    st.info("No risk events yet.")
else:
    severity_colours = {"BLOCK": "🔴", "WARN": "🟡", "INFO": "🔵"}
    for _, row in events_df.head(20).iterrows():
        icon = severity_colours.get(str(row.get("severity", "")), "⚪")
        st.markdown(
            f"{icon} `{row.get('timestamp', '')}` **{row.get('rule_name', '')}** — "
            f"{row.get('reason', '')}"
        )
