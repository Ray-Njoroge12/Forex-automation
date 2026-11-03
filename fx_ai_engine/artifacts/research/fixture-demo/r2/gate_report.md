# NOT EVIDENCE: replay failed the authenticity gate

Bound to: variant `baseline`, exit_mode `policy`, cost `base`, policy_hash `f00f1f9c3e9120971378f3c1eb95d28f09d1d5e2ee97035a7f4c00a998535dc8`.
Datasets: bars_AUDUSD_D1.json=7e0084f84d5a3123, bars_AUDUSD_H1.json=e1b2176447e2ff30, bars_AUDUSD_H4.json=b52a352c6c5dea3d, bars_AUDUSD_M15.json=527c2e059cb25164, bars_GBPUSD_D1.json=eebb56ba6462a62f, bars_GBPUSD_H1.json=fb1b31649a9a4de6, bars_GBPUSD_H4.json=5b4f1add42007c8c, bars_GBPUSD_M15.json=a1b992bdc5184ded, status.json=bd5f28d6d94a42f0.

Evidence status: **NOT_EVIDENCE**.
A replay report counts as evidence only with a PASS verdict whose policy_hash and dataset hashes match it.

| # | check | status | blocking |
| --- | --- | --- | --- |
| 1 | R1A_TRUNCATION | PASS | yes |
| 2 | R1B_FUTURE_CORRUPTION | PASS | yes |
| 3 | R1C_WARMUP_PARITY | PASS | yes |
| 4 | R1C_WARMUP_DEVIATION | INCONCLUSIVE | no |
| 5 | R2_NO_SAME_BAR_FILL | PASS | yes |
| 6 | R2_FILLS_WITHIN_BID_ASK | PASS | yes |
| 7 | R2_NO_FILL_IN_GAP_OR_CLOSED_MARKET | PASS | yes |
| 8 | R4_COST_STRESS | FAIL | yes |

Blocking failures:

- R4_COST_STRESS: FAIL

Non-blocking:

- R1C_WARMUP_DEVIATION: INCONCLUSIVE
