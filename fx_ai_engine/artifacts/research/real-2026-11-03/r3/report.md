# NOT EVIDENCE: no variant passed the multiple-testing check (R3)

Verdict **INCONCLUSIVE**, evidence status **NOT_EVIDENCE**. Registry `/Users/raynj/Desktop/Forex-automation/fx_ai_engine/artifacts/experiments/registry.jsonl` (sha256_16 `95a510f00d5db123`): 57 rows read, 0 unparseable, 39 evaluated, 18 superseded.

Trials N = **18**: distinct (policy_hash, exit_mode) pairs over all parseable rows, whatever the cost, halts mode or superseded flag. Rows without a usable series still count.

## Thresholds

- Deflated Sharpe ratio >= 0.95 (and PSR(0) >= 0.95).
- PBO (CSCV, 16 blocks of FX weeks, needs >= 64 weeks) <= 0.2.
- Dev trades >= 307: target mean 0.25R, reward 2.2R vs -1R, q = 0.390625, sigma = 1.5612, n = ((1.9600 + 0.8416) x sigma / 0.25)^2 = 306.1, rounded up to 307 (two-sided 95% CI excludes 0 with 80% power; CI alone would need 150).

## Groups (halts mode, cost)

| halts | cost | series | missing | weeks | V(SR) | SR0 | PBO | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| edge | base | 9 | 9 | 176 | 0.00155 | 0.0729 | 0.0000 | OK |
| edge | stress_1_5x | 9 | 9 | 176 | 0.00385 | 0.1151 | 0.0076 | OK |
| srs | base | 1 | 0 | - | - | - | - | GROUP_TOO_FEW_SERIES |
| srs | stress_1_5x | 1 | 0 | - | - | - | - | GROUP_TOO_FEW_SERIES |
| srs | stress_2x | 1 | 0 | - | - | - | - | GROUP_TOO_FEW_SERIES |

## Rows

| label | cost | run_id | dev trades | E[R] | SR/trade | skew | kurt | PSR(0) | DSR | verdict | reason | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline__policy__srs | base | 20261103T174558Z | 16 | -0.5420 | -0.7938 | 0.591 | 1.663 | 0.0071 | - | INCONCLUSIVE | BELOW_MIN_SAMPLE | NOT EVIDENCE |
| baseline__policy__srs | stress_1_5x | 20261103T174558Z | 17 | -0.5386 | -0.7037 | 0.684 | 1.696 | 0.0123 | - | INCONCLUSIVE | BELOW_MIN_SAMPLE | NOT EVIDENCE |
| baseline__policy__srs | stress_2x | 20261103T174558Z | 16 | -0.5249 | -0.6233 | 0.694 | 1.796 | 0.0247 | - | INCONCLUSIVE | BELOW_MIN_SAMPLE | NOT EVIDENCE |
| rsi_bounded__policy__edge | base | 20261103T175026Z | 1281 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| rsi_bounded__policy__edge | stress_1_5x | 20261103T175026Z | 868 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| rsi_bounded__fixed_tp__edge | base | 20261103T175026Z | 1178 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| rsi_bounded__fixed_tp__edge | stress_1_5x | 20261103T175026Z | 919 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| rsi_bounded__hybrid__edge | base | 20261103T175026Z | 1351 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| rsi_bounded__hybrid__edge | stress_1_5x | 20261103T175026Z | 958 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__policy__edge | base | 20261103T175026Z | 1182 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__policy__edge | stress_1_5x | 20261103T175026Z | 801 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__fixed_tp__edge | base | 20261103T175026Z | 1136 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__fixed_tp__edge | stress_1_5x | 20261103T175026Z | 840 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__hybrid__edge | base | 20261103T175026Z | 1163 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| adx_20_25__hybrid__edge | stress_1_5x | 20261103T175026Z | 833 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__policy__edge | base | 20261103T175026Z | 1291 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__policy__edge | stress_1_5x | 20261103T175026Z | 840 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__fixed_tp__edge | base | 20261103T175026Z | 1187 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__fixed_tp__edge | stress_1_5x | 20261103T175026Z | 964 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__hybrid__edge | base | 20261103T175026Z | 1341 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| trend_distance_5pip__hybrid__edge | stress_1_5x | 20261103T175026Z | 930 | - | - | - | - | - | - | INCONCLUSIVE | SERIES_FILES_MISSING | NOT EVIDENCE |
| baseline__policy__edge | base | 20261103T184434Z | 5622 | -0.3466 | -0.3191 | 1.090 | 6.172 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__policy__edge | stress_1_5x | 20261103T184434Z | 5556 | -0.5112 | -0.4783 | 1.189 | 6.674 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__fixed_tp__edge | base | 20261103T184434Z | 4039 | -0.3612 | -0.2461 | 1.256 | 2.809 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__fixed_tp__edge | stress_1_5x | 20261103T184434Z | 4040 | -0.5073 | -0.3565 | 1.378 | 3.207 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__hybrid__edge | base | 20261103T184434Z | 6016 | -0.3447 | -0.3221 | 0.756 | 3.690 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__hybrid__edge | stress_1_5x | 20261103T184434Z | 5947 | -0.5009 | -0.4711 | 0.882 | 4.152 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__policy__edge | base | 20261103T184434Z | 2195 | -0.3220 | -0.2939 | 1.335 | 6.956 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__policy__edge | stress_1_5x | 20261103T184434Z | 2168 | -0.5088 | -0.4737 | 1.526 | 7.784 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__fixed_tp__edge | base | 20261103T184434Z | 1777 | -0.3417 | -0.2308 | 1.221 | 2.677 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__fixed_tp__edge | stress_1_5x | 20261103T184434Z | 1777 | -0.5224 | -0.3685 | 1.404 | 3.239 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__hybrid__edge | base | 20261103T184434Z | 2250 | -0.3193 | -0.2971 | 0.830 | 2.571 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__hybrid__edge | stress_1_5x | 20261103T184434Z | 2218 | -0.5035 | -0.4764 | 1.021 | 3.015 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__policy__edge | base | 20261103T184434Z | 3218 | -0.3426 | -0.3152 | 1.415 | 7.159 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__policy__edge | stress_1_5x | 20261103T184434Z | 3133 | -0.5132 | -0.4781 | 1.548 | 7.772 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__fixed_tp__edge | base | 20261103T184434Z | 2483 | -0.3407 | -0.2294 | 1.204 | 2.687 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__fixed_tp__edge | stress_1_5x | 20261103T184434Z | 2409 | -0.4838 | -0.3342 | 1.310 | 3.027 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__hybrid__edge | base | 20261103T184434Z | 3307 | -0.3381 | -0.3188 | 0.873 | 2.605 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__hybrid__edge | stress_1_5x | 20261103T184434Z | 3206 | -0.5042 | -0.4776 | 1.013 | 2.982 | 0.0000 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |

## What is missing

- rsi_bounded__policy__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__policy__edge__base.json, rsi_bounded__policy__edge__base_trades.csv
- rsi_bounded__policy__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__policy__edge__stress_1_5x.json, rsi_bounded__policy__edge__stress_1_5x_trades.csv
- rsi_bounded__fixed_tp__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__fixed_tp__edge__base.json, rsi_bounded__fixed_tp__edge__base_trades.csv
- rsi_bounded__fixed_tp__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__fixed_tp__edge__stress_1_5x.json, rsi_bounded__fixed_tp__edge__stress_1_5x_trades.csv
- rsi_bounded__hybrid__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__hybrid__edge__base.json, rsi_bounded__hybrid__edge__base_trades.csv
- rsi_bounded__hybrid__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing rsi_bounded__hybrid__edge__stress_1_5x.json, rsi_bounded__hybrid__edge__stress_1_5x_trades.csv
- adx_20_25__policy__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__policy__edge__base.json, adx_20_25__policy__edge__base_trades.csv
- adx_20_25__policy__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__policy__edge__stress_1_5x.json, adx_20_25__policy__edge__stress_1_5x_trades.csv
- adx_20_25__fixed_tp__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__fixed_tp__edge__base.json, adx_20_25__fixed_tp__edge__base_trades.csv
- adx_20_25__fixed_tp__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__fixed_tp__edge__stress_1_5x.json, adx_20_25__fixed_tp__edge__stress_1_5x_trades.csv
- adx_20_25__hybrid__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__hybrid__edge__base.json, adx_20_25__hybrid__edge__base_trades.csv
- adx_20_25__hybrid__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing adx_20_25__hybrid__edge__stress_1_5x.json, adx_20_25__hybrid__edge__stress_1_5x_trades.csv
- trend_distance_5pip__policy__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__policy__edge__base.json, trend_distance_5pip__policy__edge__base_trades.csv
- trend_distance_5pip__policy__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__policy__edge__stress_1_5x.json, trend_distance_5pip__policy__edge__stress_1_5x_trades.csv
- trend_distance_5pip__fixed_tp__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__fixed_tp__edge__base.json, trend_distance_5pip__fixed_tp__edge__base_trades.csv
- trend_distance_5pip__fixed_tp__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__fixed_tp__edge__stress_1_5x.json, trend_distance_5pip__fixed_tp__edge__stress_1_5x_trades.csv
- trend_distance_5pip__hybrid__edge / base (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__hybrid__edge__base.json, trend_distance_5pip__hybrid__edge__base_trades.csv
- trend_distance_5pip__hybrid__edge / stress_1_5x (run 20261103T175026Z): SERIES_FILES_MISSING: missing trend_distance_5pip__hybrid__edge__stress_1_5x.json, trend_distance_5pip__hybrid__edge__stress_1_5x_trades.csv
- Group srs / base: GROUP_TOO_FEW_SERIES (1 valid series); V and PBO cannot be computed.
- Group srs / stress_1_5x: GROUP_TOO_FEW_SERIES (1 valid series); V and PBO cannot be computed.
- Group srs / stress_2x: GROUP_TOO_FEW_SERIES (1 valid series); V and PBO cannot be computed.

Superseded rows not evaluated: 18 (they still count toward N).

## Notes

- The registry rows carry no per-trade series, standard deviation, skew or kurtosis. Sharpe, skew and kurtosis come from the `*_trades.csv` next to each row's report, and only when that file belongs to the same run (JSON `generated_at` = `run_id`, same policy_hash and cost) and its dev/holdout counts equal the registry's.
- A trial is a distinct (policy_hash, exit_mode) pair over all parseable registry rows, whatever the cost, halts mode or superseded flag. Rows without a usable series still count toward N.
- Cross-trial statistics (variance of Sharpe, CSCV) are computed per group (halts mode, cost profile): SRS-halt runs, edge runs and different cost profiles are not comparable return streams.
- Development trades only (entry_time < holdout_start); the holdout is not used. Unit: r_multiple per trade.
- A PASS here is necessary, not sufficient: the replay still needs an R2 authenticity-gate PASS with matching hashes.
