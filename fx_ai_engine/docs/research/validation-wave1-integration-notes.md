# Validation wave 1: integration notes for run_replay.py

Branch `research/validation-wave1` adds the `research/` package without editing `backtesting/run_replay.py`, `unified_replay.py` or `replay_report.py`; another session owns those files. These are the changes the run_replay owner would make to wire the tools in. Nothing below has been applied.

(a) **Gate every replay result.** In `run_replay.main`, after `result = simulate(...)`:

```python
from research.authenticity_gate import audit_replay, render_gate_markdown

gate = audit_replay(frames=frames, policy=policy, cost=cost, candidates=candidates, result=result,
                    exit_mode=exit_mode, r1_report=r1_cache[(policy_hash, frozenset(dataset_hashes.items()))],
                    context={"variant": label, "dataset_sha256_16": dataset_hashes})
stem.with_name(stem.name + "_gate.json").write_text(json.dumps(gate, indent=2, default=str), encoding="utf-8")
```

R1 (`research.bias_audit.run_bias_audit`) depends only on the policy, the data and the seed, so compute it once per `policy_hash` + dataset hashes and reuse it across exit modes and cost profiles.

(b) **Label ungated reports.** Prefix `render_markdown(report)` with the first line of `render_gate_markdown(gate)`, so a report that failed or skipped the gate starts with `# NOT EVIDENCE: replay failed the authenticity gate`.

(c) **Registry.** Add `"gate_verdict": gate["verdict"]` and `"evidence_status": gate["evidence_status"]` to the registry row in `artifacts/experiments/registry.jsonl`.

(d) **3x cost.** Either add `"stress_3x": CostModel("stress_3x", 3.0)` to `unified_replay.COST_SCENARIOS`, or call `research.cost_stress.recost_trades(section_trades, frames, cost, (1.0, 1.5, 2.0, 3.0))` per dev/holdout section to get the fixed-trade-set expectancy and break-even multiple. Pass the `cost` the section was replayed with: multiples are absolute and the re-cost starts from `cost.multiplier`, so a `stress_1_5x` section gives the same 2× row as a `base` section with the same trades. `run_replay`'s profiles scale commission too; pass `scale_commission=True` to match them.

(e) **Monte Carlo.** Replace or augment `_section`'s `monte_carlo` (i.i.d., `replay_report.monte_carlo_drawdown`) with `research.monte_carlo.risk_sweep([t["r_multiple"] for t in sorted(trades, key=lambda t: t["exit_time"])], exit_times=[...])`, which adds the block bootstrap, percentile bands, SRS halt probabilities and the risk sweep. Trades must be sorted by `exit_time` for the block bootstrap to keep streaks.

(f) **Coupling.** `research.common.policy_for_variant` imports `run_replay.VARIANTS`. Renaming or moving `VARIANTS` breaks the R1/R2/R4 CLIs' `--variant` option. `research.bias_audit` also imports the private helper `unified_replay._bar_spread_price`; keep its signature or update the import.

Fixture results on this branch (`tests/fixtures/live_bars_2026-11-03`, baseline, policy exit mode, base cost) are in `artifacts/research/fixture-demo/` and summarised in `docs/decisions/0002-validation-wave1-research-tooling.md`.
