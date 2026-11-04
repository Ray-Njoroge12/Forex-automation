# Multiple-testing check (R3): PASS

Verdict **PASS**, evidence status **EVIDENCE_ELIGIBLE**. Registry `/private/tmp/r3-synthetic/artifacts/experiments/registry.jsonl` (sha256_16 `7b586c1dfb91207f`): 6 rows read, 0 unparseable, 6 evaluated, 0 superseded.

Trials N = **6**: distinct (policy_hash, exit_mode) pairs over all parseable rows, whatever the cost, halts mode or superseded flag. Rows without a usable series still count.

## Thresholds

- Deflated Sharpe ratio >= 0.95 (and PSR(0) >= 0.95).
- PBO (CSCV, 16 blocks of FX weeks, needs >= 64 weeks) <= 0.2.
- Dev trades >= 307: target mean 0.25R, reward 2.2R vs -1R, q = 0.390625, sigma = 1.5612, n = ((1.9600 + 0.8416) x sigma / 0.25)^2 = 306.1, rounded up to 307 (two-sided 95% CI excludes 0 with 80% power; CI alone would need 150).

## Groups (halts mode, cost)

| halts | cost | series | missing | weeks | V(SR) | SR0 | PBO | status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| edge | base | 6 | 0 | 157 | 0.01879 | 0.1782 | 0.0000 | OK |

## Rows

| label | cost | run_id | dev trades | E[R] | SR/trade | skew | kurt | PSR(0) | DSR | verdict | reason | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline__policy__edge | base | 20260701T000000Z | 600 | 0.0560 | 0.0372 | 0.723 | 1.523 | 0.8219 | 0.0002 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| baseline__fixed_tp__edge | base | 20260701T000000Z | 600 | 0.0187 | 0.0125 | 0.780 | 1.608 | 0.6209 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__policy__edge | base | 20260701T000000Z | 600 | -0.0880 | -0.0609 | 0.953 | 1.907 | 0.0738 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| coded_defaults__fixed_tp__edge | base | 20260701T000000Z | 600 | -0.0347 | -0.0236 | 0.864 | 1.747 | 0.2839 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__policy__edge | base | 20260701T000000Z | 600 | -0.0613 | -0.0421 | 0.908 | 1.824 | 0.1562 | 0.0000 | FAIL | PSR_BELOW_MIN | NOT EVIDENCE |
| pullback_2__fixed_tp__edge | base | 20260701T000000Z | 600 | 0.4933 | 0.3088 | 0.134 | 1.018 | 1.0000 | 0.9994 | PASS | - | EVIDENCE_ELIGIBLE |

## What is missing

- Every evaluated row has a matching trades series.

## Notes

- The registry rows carry no per-trade series, standard deviation, skew or kurtosis. Sharpe, skew and kurtosis come from the `*_trades.csv` next to each row's report, and only when that file belongs to the same run (JSON `generated_at` = `run_id`, same policy_hash and cost) and its dev/holdout counts equal the registry's.
- A trial is a distinct (policy_hash, exit_mode) pair over all parseable registry rows, whatever the cost, halts mode or superseded flag. Rows without a usable series still count toward N.
- Cross-trial statistics (variance of Sharpe, CSCV) are computed per group (halts mode, cost profile): SRS-halt runs, edge runs and different cost profiles are not comparable return streams.
- Development trades only (entry_time < holdout_start); the holdout is not used. Unit: r_multiple per trade.
- A PASS here is necessary, not sufficient: the replay still needs an R2 authenticity-gate PASS with matching hashes.
