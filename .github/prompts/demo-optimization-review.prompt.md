---
description: "Review demo-account trading performance and propose controlled optimization steps for better accuracy and turnover."
name: "Demo Optimization Review"
argument-hint: "Paste the period, symbols, and any observations to review"
agent: "agent"
---

Review recent demo-account performance and propose the next optimization steps.

Priorities:
- Keep risk controls strict and unchanged unless explicitly approved.
- Optimize in order: reliability -> signal quality -> turnover growth.
- Focus on small-capital growth through consistency and controlled compounding.

Include:
- Performance summary vs SRS v1 demo gate (trades, win rate, average R, drawdown).
- Top rejection reasons and top loss drivers.
- Execution quality checks (spread, slippage, late fills, stale data).
- Three concrete demo-only experiments with pass/fail criteria.

Output format:
1. KPI status
2. Main problems
3. Next 3 experiments
4. Stop conditions and rollback triggers
