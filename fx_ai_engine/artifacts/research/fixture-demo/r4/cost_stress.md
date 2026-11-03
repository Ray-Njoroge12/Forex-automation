# Cost stress (R4): baseline / policy

Policy hash `f00f1f9c3e91`. Commission scaled: False. Re-cost input replayed at 1x (multiples are absolute).

Break-even cost multiple (re-cost, fixed trade set): **<=1.0** (expectancy is already <= 0 at 1x)

| multiple | re-cost trades | re-cost expectancy R | re-run trades | re-run expectancy R | re-run CI95 | re-run PF | re-run net USD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1x | 19 | -0.4937 | 19 | -0.4937 | [-0.847, -0.1133] | 0.1654 | -21652.27 |
| 1.5x | 19 | -0.5843 | 16 | -0.5293 | [-0.9188, -0.0775] | 0.1625 | -20110.67 |
| 2x | 19 | -0.6748 | 3 | -1.0924 | [-1.126, -1.0743] | 0.0 | -6593.11 |
| 3x | 19 | -0.8559 | 0 | None | None | None | None |

## Notes

- run_replay already provides cost profiles base (1.0x), stress_1_5x (1.5x) and stress_2x (2.0x) through unified_replay.COST_SCENARIOS; each scales spread, slippage and commission together and re-runs stage 2 with candidates generated once at base cost, one report per profile.
- Built here because it was missing: a 3x multiple; a stress of spread plus slippage with commission held at 1x; a fixed-trade-set re-cost that separates the cost effect from the selection effect (the adversarial spread filter drops entries as spreads widen); the cost multiple at which expectancy reaches zero; and one table across multiples.
- Re-cost model: R(m) = r - (m - m0) * (spread_1x + 2 * slippage_1x) / stop_pips, where m0 is the cost multiple the trades were replayed at (1.0 for base, 1.5 for stress_1_5x) and multiples are absolute. Spread is taken from the entry bar for BUY (ask paid at entry) and from the exit bar for SELL (ask paid at exit). Exact under this linear model; approximate for trades with a partial close, where part of the position exits at a different bar. Rows below m0 (below_replay_cost) add cost back out of the realised trades.
- Commission: with commission_1x = commission_usd / k, where k is the commission multiple the replay applied (1.5 for stress_1_5x), the re-cost holds commission at 1x by default (subtracting (1 - k) * commission_1x / risk_usd) and with --scale-commission sets it to m (subtracting (m - k) * commission_1x / risk_usd), matching run_replay's convention.
