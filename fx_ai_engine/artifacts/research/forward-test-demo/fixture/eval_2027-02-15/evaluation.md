# Forward-test go/no-go evaluation (R10)

**NOT A DECISION: criteria status DRAFT_NOT_APPROVED.**

Verdict: **NO_GO** (GO_CRITERIA_FAILED G4_WIN_RATE)

- Criteria: `docs/research/forward-test/criteria-srs-v1.1-draft.json` (srs-v1.1-30-day-demo v0.1.0-draft, DRAFT_NOT_APPROVED)
- Seal: `artifacts/research/forward-test-demo/fixture/seal.json` (b6860cebf7d4a91f); pre-registration OK
- Database (read-only): `/tmp/fx-r10-fixture.db`
- warning: CRITERIA_DRAFT_NOT_APPROVED: criteria are a draft; no go/no-go decision can rest on them
- Window: 2027-01-04T00:00:00+00:00 to 2027-02-15T00:00:00+00:00; minimum 25 closed trades and 30 trading days

| metric | value |
| --- | --- |
| closed_trades | 30 |
| trading_days_elapsed | 30 |
| win_rate | 0.4333 |
| avg_r_all | 0.3 |
| avg_r_winners_srs | 2.0 |
| expectancy_r | 0.3 |
| expectancy_r_ci95_low | -0.2 |
| expectancy_r_ci95_high | 0.8 |
| profit_factor | 1.5294 |
| avg_winner_r | 2.0 |
| avg_loser_r | -1.0 |
| max_consecutive_losses | 2 |
| max_drawdown | 0.0602 |
| unresolved_feedback_conflicts | 0 |
| equity_samples | 31 |

| group | id | metric | rule | actual | result | criterion status |
| --- | --- | --- | --- | --- | --- | --- |
| go | G1_EXPECTANCY | expectancy_r | >= 0.25 | 0.3 | PASS | PROPOSED_IN_PLAN_NOT_APPROVED |
| go | G2_PROFIT_FACTOR | profit_factor | >= 1.3 | 1.5294 | PASS | PROPOSED_IN_PLAN_NOT_APPROVED |
| go | G3_AVG_WINNER | avg_winner_r | >= 1.5 | 2.0 | PASS | PROPOSED_IN_PLAN_NOT_APPROVED |
| go | G4_WIN_RATE | win_rate | >= 0.45 | 0.4333 | FAIL | SRS_V1_UNCHANGED |
| go | G5_MAX_DRAWDOWN | max_drawdown | <= 0.15 | 0.0602 | PASS | SRS_V1_UNCHANGED |
| go | G6_RECONCILIATION | unresolved_feedback_conflicts | <= 0 | 0 | PASS | PROPOSED_IN_PLAN_NOT_APPROVED |
| abort | A1_DRAWDOWN | max_drawdown | > 0.2 | 0.0602 | CLEAR | SRS_V1_UNCHANGED |
| abort | A2_WIN_RATE | win_rate | < 0.4 | 0.4333 | CLEAR | SRS_V1_UNCHANGED |
| abort | A3_AVG_R_REPLACEMENT | expectancy_r | open | 0.3 | OPEN | OPEN_OWNER_DECISION |

- data warning: FREEZE_NOT_CHECKED: no freeze manifest given

Manual checks (owner sign-off, not automated):
- M1_ORDERS_ONLY_FROM_EA: No order outside the EA (plan Phase 6 abort; AGENTS.md invariant 1). Check MT5 deal history magic numbers against the EA's MagicNumber.
- M2_NO_HARD_RISK_FAILURE: No hard-risk failure (plan Phase 6 abort; SRS 12.3 item 5).
- M3_SPREAD_SLIPPAGE_CONTROLS: No repeated spread or slippage control failures (SRS 12.3 item 4).
- M4_LIVE_COSTS: Live cost per trade within the replay's 1.5x cost assumption (plan Phase 6 exit).
- M5_RISK_TRIGGERS_VERIFIED: Risk engine triggers verified (SRS 12.2 item 5).
- M6_NO_CRITICAL_ANOMALY: No critical execution anomalies (SRS 12.2 item 6).
- M7_NO_CHANGE_RULE: Freeze check MATCH on every day of the window (plan Phase 6 no-change rule); a behaviour-changing fix restarts the clock.
