---
description: "Use when editing trading logic, risk controls, bridge routing, MT5 execution integration, or account-state handling. Enforces SRS v1 locked constraints and demo-first safety checks."
applyTo:
  - "fx_ai_engine/core/agents/**"
  - "fx_ai_engine/core/risk/**"
  - "fx_ai_engine/bridge/**"
  - "fx_ai_engine/main.py"
  - "fx_ai_engine/mt5_ea/**"
---

# SRS v1 Trading Guardrails

## Non-negotiable constraints
- Never modify locked SRS v1 limits unless the user explicitly asks.
- Keep Hard Risk Engine as final authority over all signal approvals.
- Preserve max open trades, exposure caps, drawdown halts, and loss-streak halts.

## Safety-critical architecture rules
- Do not bypass the agent flow: Regime -> Technical -> Adversarial -> Portfolio -> Hard Risk Engine.
- Do not treat adversarial rejections as defects by default.
- Do not write raw signal files directly to `pending_signals/`; use `bridge/signal_router.py` atomic routing.
- Keep MT5 EA as the only component that places broker-side orders.

## Validation expectations for edits in these paths
- Add or update targeted tests for every behavior change.
- Run smoke mode before considering a change complete.
- If bridge/execution behavior changes, include bridge and path diagnostics.
- Keep auditability intact in SQLite for decisions, rejections, and risk events.

## Optimization direction
- First improve reliability and risk-control integrity.
- Next improve signal quality and execution quality.
- Increase turnover only after stable demo metrics are maintained.
