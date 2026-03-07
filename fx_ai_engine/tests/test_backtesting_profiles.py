from __future__ import annotations

import unittest

from backtesting.simulation_profile import assess_trade_feasibility, build_simulation_profile


class BacktestingProfileTests(unittest.TestCase):
    def test_preserve_10_profile_uses_literal_ten_dollar_constraints(self) -> None:
        profile = build_simulation_profile("preserve_10")

        self.assertEqual(profile.starting_cash, 10.0)
        self.assertEqual(profile.fixed_risk_usd, 0.50)
        self.assertTrue(profile.realistic_constraints)
        self.assertEqual(profile.evidence_stream, "preserve_10_realistic")
        self.assertEqual(profile.min_lot, 0.01)
        self.assertEqual(profile.lot_step, 0.01)
        self.assertGreater(profile.commission_per_lot_usd, 0.0)

    def test_core_srs_profile_keeps_smoke_test_defaults(self) -> None:
        profile = build_simulation_profile("core_srs")

        self.assertEqual(profile.starting_cash, 10_000.0)
        self.assertFalse(profile.realistic_constraints)
        self.assertEqual(profile.evidence_stream, "core_srs_smoke_test")
        self.assertIsNone(profile.fixed_risk_usd)

    def test_preserve_10_rejects_infeasible_trade_for_ten_dollar_balance(self) -> None:
        profile = build_simulation_profile("preserve_10")

        decision = assess_trade_feasibility(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_pips=20.0,
            risk_amount_usd=0.50,
            profile=profile,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "REJECTED_LOT_PREROUTE")
        self.assertIn("min_lot=0.0100", decision.details)

    def test_feasible_trade_quantizes_to_supported_lot_grid(self) -> None:
        profile = build_simulation_profile("preserve_10")

        decision = assess_trade_feasibility(
            symbol="EURUSD",
            entry_price=1.1000,
            stop_pips=10.0,
            risk_amount_usd=2.50,
            profile=profile,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason_code, "TRADE_FEASIBLE")
        self.assertGreaterEqual(decision.estimated_lot, 0.01)
        self.assertAlmostEqual(
            decision.estimated_units,
            decision.estimated_lot * profile.contract_size_units,
        )


if __name__ == "__main__":
    unittest.main()