"""research.swap_allowance: rollover counting and the negative-swap charge on a 5-row trades CSV."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from research import swap_allowance as sa

SNAPSHOT = {"schema_version": 1, "generated_at": "2026-11-04T12:11:18Z", "symbols": {
    "EURUSD": {"point": 1e-05, "swap_long": -0.7, "swap_short": -1.0},
    "AUDUSD": {"point": 1e-05, "swap_long": 6.3, "swap_short": -14.8},
    "USDJPY": {"point": 0.001, "swap_long": -0.1, "swap_short": -0.6},
}}
ROWS = [  # entry, exit (UTC, January: 17:00 New York = 22:00 UTC)
    ("T1", "EURUSD", "BUY", "2026-01-12T10:00:00Z", "2026-01-12T15:00:00Z", 20.0, 2.2),    # same day: 0
    ("T2", "EURUSD", "BUY", "2026-01-12T10:00:00Z", "2026-01-13T23:00:00Z", 20.0, -1.0),   # Mon, Tue: 2
    ("T3", "EURUSD", "SELL", "2026-01-13T10:00:00Z", "2026-01-15T10:00:00Z", 20.0, 2.2),   # Tue 1 + Wed 3
    ("T4", "AUDUSD", "BUY", "2026-01-12T10:00:00Z", "2026-01-16T10:00:00Z", 25.0, -1.0),   # positive swap
    ("T5", "USDJPY", "SELL", "2026-01-16T20:00:00Z", "2026-01-19T10:00:00Z", 30.0, 2.2),   # Fri only: 1
]
HOLDOUT = "2026-01-16T00:00:00Z"


@pytest.fixture()
def trades_csv(tmp_path):
    path = tmp_path / "arm__stress_1_5x_trades.csv"
    pd.DataFrame(ROWS, columns=["trade_id", "symbol", "direction", "entry_time", "exit_time", "stop_pips",
                                "r_multiple"]).to_csv(path, index=False)
    return path


@pytest.mark.parametrize("entry, exit, expected", [
    ("2026-01-12T10:00:00Z", "2026-01-12T15:00:00Z", 0),
    ("2026-01-12T10:00:00Z", "2026-01-12T22:00:00Z", 1),   # exit exactly at the rollover counts
    ("2026-01-12T22:00:00Z", "2026-01-13T10:00:00Z", 0),   # entry exactly at it does not
    ("2026-01-14T21:00:00Z", "2026-01-14T23:00:00Z", 3),   # Wednesday x3
    ("2026-01-16T20:00:00Z", "2026-01-19T10:00:00Z", 1),   # Friday counts, the weekend does not
    ("2026-01-12T10:00:00Z", "2026-01-19T10:00:00Z", 7),   # one full week = 7 nights
    ("2026-07-06T20:30:00Z", "2026-07-06T21:30:00Z", 1),   # summer: 17:00 New York = 21:00 UTC
    ("2026-01-13T10:00:00Z", "2026-01-12T10:00:00Z", 0),
])
def test_count_rollovers(entry, exit, expected):
    assert sa.count_rollovers(pd.Timestamp(entry), pd.Timestamp(exit)) == expected


def test_snapshot_charges_negative_swap_only(tmp_path, trades_csv):
    snap = tmp_path / "symbols.json"
    snap.write_text(json.dumps(SNAPSHOT))
    swaps, sha = sa.load_swap_snapshot(snap)
    assert len(sha) == 16
    report, adjusted = sa.swap_report(pd.read_csv(trades_csv).to_dict("records"), pd.Timestamp(HOLDOUT),
                                      swaps=swaps, snapshot_sha256_16=sha)
    by_id = {t["trade_id"]: t for t in adjusted}
    assert [by_id[i]["rollovers"] for i in ("T1", "T2", "T3", "T4", "T5")] == [0, 2, 4, 6, 1]
    assert by_id["T1"]["swap_r"] == 0.0
    assert by_id["T2"]["swap_r"] == pytest.approx(2 * 0.07 / 20)   # 0.7 points = 0.07 pips per night
    assert by_id["T3"]["swap_r"] == pytest.approx(4 * 0.10 / 20)
    assert by_id["T4"]["swap_r"] == 0.0                              # +6.3 points is never credited
    assert by_id["T5"]["swap_r"] == pytest.approx(1 * 0.06 / 30)    # JPY: 0.6 points of 0.001 = 0.06 pips
    assert by_id["T3"]["r_multiple_swap_adjusted"] == pytest.approx(2.2 - 0.02)
    dev, hold = report["development"], report["holdout"]
    assert (dev["trades"], hold["trades"]) == (4, 1)
    assert dev["expectancy_r"] == pytest.approx((2.2 - 1.0 + 2.2 - 1.0) / 4)
    assert dev["total_swap_r"] == pytest.approx(0.007 + 0.02, abs=1e-4)
    assert report["method"] == "snapshot_points" and report["symbols_json_sha256_16"] == sha


def test_flat_pips_fallback_and_cli(tmp_path, trades_csv, capsys):
    out = tmp_path / "swap.json"
    assert sa.main(["--trades", str(trades_csv), "--flat-pips-per-rollover", "1.0", "--holdout-start", HOLDOUT,
                    "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["method"] == "flat_pips_per_rollover" and report["symbols_json_sha256_16"] is None
    adjusted = pd.read_csv(out.with_suffix(".csv")).set_index("trade_id")
    assert adjusted.loc["T4", "swap_r"] == pytest.approx(6 * 1.0 / 25)  # flat charge ignores the sign
    assert "development: n=4" in capsys.readouterr().out


def test_cli_and_snapshot_errors(tmp_path, trades_csv):
    with pytest.raises(SystemExit) as exc:
        sa.main(["--trades", str(trades_csv), "--holdout-start", HOLDOUT])  # neither source
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        sa.main(["--trades", str(trades_csv), "--flat-pips-per-rollover", "-1", "--holdout-start", HOLDOUT])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"symbols": {"EURUSD": {"swap_long": "x", "swap_short": 1}}}))
    with pytest.raises(ValueError, match="EURUSD"):
        sa.load_swap_snapshot(bad)
    missing = {"EURUSD": {"swap_long": -1.0, "swap_short": -1.0, "point": 1e-05}}
    with pytest.raises(ValueError, match="AUDUSD"):
        sa.apply_swap([dict(zip(["trade_id", "symbol", "direction", "entry_time", "exit_time", "stop_pips",
                                 "r_multiple"], ROWS[3]))], swaps=missing)
