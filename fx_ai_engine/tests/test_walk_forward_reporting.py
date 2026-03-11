from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pandas as pd
except ModuleNotFoundError:  # pragma: no cover - environment-dependent
    pd = None

from backtesting.simulation_profile import build_simulation_profile

if pd is not None:
    from backtesting import bt_runner
    from backtesting import walk_forward
    from backtesting import walk_forward_suite
else:  # pragma: no cover - environment-dependent
    bt_runner = None
    walk_forward = None
    walk_forward_suite = None


class _FakeAnalyzer:
    def __init__(self, payload: dict):
        self.payload = payload

    def get_analysis(self) -> dict:
        return self.payload


class _FakeStrategy:
    def __init__(self, profile, results: list[dict], *, max_dd: float, rejections: int):
        self.results = results
        self.rejected_signals = [{} for _ in range(rejections)]
        self.simulation_profile = profile
        self.max_simulated_drawdown_pct = max_dd
        self.analyzers = SimpleNamespace(
            sharpe=_FakeAnalyzer({"sharperatio": 1.25}),
            drawdown=_FakeAnalyzer({"max": {"drawdown": max_dd}}),
        )


def _fixture_df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", "2025-04-30", freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [1.10] * len(idx),
            "high": [1.11] * len(idx),
            "low": [1.09] * len(idx),
            "close": [1.10] * len(idx),
            "volume": [1000.0] * len(idx),
        },
        index=idx,
    )


@unittest.skipIf(pd is None, "pandas not installed")
class WalkForwardReportingTests(unittest.TestCase):
    def test_walk_forward_labels_preserve_10_realistic_results(self) -> None:
        profile = build_simulation_profile("preserve_10")
        train_strategy = _FakeStrategy(profile, [{"pnl": 0.4, "r_multiple": 1.2}], max_dd=2.5, rejections=1)
        test_strategy = _FakeStrategy(profile, [{"pnl": 0.2, "r_multiple": 0.8}], max_dd=3.1, rejections=2)

        with patch.object(walk_forward, "load_ohlc_csv", return_value=_fixture_df()), patch.object(
            walk_forward,
            "_run_on_df",
            side_effect=[train_strategy, test_strategy, train_strategy, test_strategy],
        ):
            results = walk_forward.run_walk_forward(
                "ignored.csv",
                "EURUSD",
                train_months=1,
                test_months=1,
                mode_id="preserve_10",
            )

        self.assertFalse(results.empty)
        self.assertTrue((results["simulation_mode"] == "preserve_10").all())
        self.assertTrue((results["evidence_stream"] == "preserve_10_realistic").all())
        self.assertTrue((results["starting_cash"] == 10.0).all())
        self.assertTrue((results["test_rejections"] == 2).all())

    def test_walk_forward_keeps_core_srs_smoke_test_labels_isolated(self) -> None:
        profile = build_simulation_profile("core_srs")
        train_strategy = _FakeStrategy(profile, [{"pnl": 1.5, "r_multiple": 1.2}], max_dd=2.5, rejections=0)
        test_strategy = _FakeStrategy(profile, [{"pnl": 1.0, "r_multiple": 0.8}], max_dd=3.1, rejections=0)

        with patch.object(walk_forward, "load_ohlc_csv", return_value=_fixture_df()), patch.object(
            walk_forward,
            "_run_on_df",
            side_effect=[train_strategy, test_strategy, train_strategy, test_strategy],
        ):
            results = walk_forward.run_walk_forward(
                "ignored.csv",
                "EURUSD",
                train_months=1,
                test_months=1,
                mode_id="core_srs",
            )

        self.assertFalse(results.empty)
        self.assertTrue((results["simulation_mode"] == "core_srs").all())
        self.assertTrue((results["evidence_label"] == "Core SRS v1").all())
        self.assertTrue((results["evidence_stream"] == "core_srs_smoke_test").all())
        self.assertTrue((results["criteria_label"] == "Core SRS smoke-test criteria").all())

    def test_print_helpers_surface_evidence_labels(self) -> None:
        profile = build_simulation_profile("preserve_10")
        strategy = _FakeStrategy(
            profile,
            [{"pnl": 0.25, "r_multiple": 0.5}],
            max_dd=4.2,
            rejections=3,
        )
        report_df = pd.DataFrame(
            [
                {
                    "window": "2025-01-01",
                    "evidence_label": profile.evidence_label,
                    "evidence_stream": profile.evidence_stream,
                    "realism_label": profile.realism_label,
                    "criteria_label": "SRS benchmark only",
                    "starting_cash": profile.starting_cash,
                    "train_wr": 0.5,
                    "test_wr": 0.5,
                    "test_avg_r": 0.8,
                    "test_max_dd": 3.1,
                    "test_rejections": 2,
                    "srs_criteria_met": False,
                    "param_stability_score": float("nan"),
                }
            ]
        )

        stats_out = io.StringIO()
        with redirect_stdout(stats_out):
            bt_runner._print_stats(strategy)
        self.assertIn("Preserve-$10 doctrine", stats_out.getvalue())
        self.assertIn("preserve_10_realistic", stats_out.getvalue())
        self.assertIn("Starting cash:  $10.00", stats_out.getvalue())

        wf_out = io.StringIO()
        with redirect_stdout(wf_out):
            walk_forward._print_walk_forward_report(report_df)
        self.assertIn("Preserve-$10 doctrine", wf_out.getvalue())
        self.assertIn("preserve_10_realistic", wf_out.getvalue())
        self.assertIn("rejects=2", wf_out.getvalue())
        self.assertIn("SRS_BENCHMARK=FAIL", wf_out.getvalue())

    def test_walk_forward_report_keeps_core_srs_tokens(self) -> None:
        report_df = pd.DataFrame(
            [
                {
                    "window": "2025-01-01",
                    "simulation_mode": "core_srs",
                    "evidence_label": "Core SRS v1",
                    "evidence_stream": "core_srs_smoke_test",
                    "realism_label": "Smoke-test simulation",
                    "criteria_label": "Core SRS smoke-test criteria",
                    "starting_cash": 10000.0,
                    "train_wr": 0.5,
                    "test_wr": 0.5,
                    "test_avg_r": 2.1,
                    "test_max_dd": 3.1,
                    "test_rejections": 0,
                    "srs_criteria_met": True,
                    "param_stability_score": float("nan"),
                }
            ]
        )

        wf_out = io.StringIO()
        with redirect_stdout(wf_out):
            walk_forward._print_walk_forward_report(report_df)

        self.assertIn("Core SRS v1", wf_out.getvalue())
        self.assertIn("core_srs_smoke_test", wf_out.getvalue())
        self.assertIn("Criteria: Core SRS smoke-test criteria", wf_out.getvalue())
        self.assertIn("SRS=PASS", wf_out.getvalue())
        self.assertNotIn("SRS_BENCHMARK", wf_out.getvalue())

    def test_walk_forward_suite_marks_missing_symbol_data(self) -> None:
        summary = walk_forward_suite.run_walk_forward_suite(
            "/tmp/nonexistent-wf-data",
            symbols=("EURUSD", "GBPUSD"),
            train_months=1,
            test_months=1,
            mode_id="core_srs",
        )

        self.assertEqual(list(summary["symbol"]), ["EURUSD", "GBPUSD"])
        self.assertTrue((summary["status"] == "MISSING_DATA").all())

    def test_walk_forward_fixture_forms_real_windows(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "ohlc_walk_forward_fixture.csv"

        results = walk_forward.run_walk_forward(
            str(fixture),
            "EURUSD",
            train_months=6,
            test_months=1,
            mode_id="core_srs",
        )

        self.assertFalse(results.empty)
        self.assertGreaterEqual(len(results), 1)
        self.assertTrue((results["simulation_mode"] == "core_srs").all())
        self.assertIn("window", results.columns)


if __name__ == "__main__":
    unittest.main()
