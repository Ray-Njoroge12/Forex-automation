# GitHub Copilot Instructions - Forex Automation (SRS v1)

## Project Scope
- Build and maintain a deterministic Forex automation engine for major pairs only.
- Run locally with Python intelligence and MT5 execution over a JSON file bridge.
- Keep development demo-first; no live capital changes without validated demo evidence.

## Source of Truth
- Use [SRS_v1.md](../SRS_v1.md) as the locked specification.
- Use [CLAUDE.md](../CLAUDE.md) for architecture and command reference.
- Use [fx_ai_engine/README.md](../fx_ai_engine/README.md) for quickstart.
- Use [fx_ai_engine/docs/SYSTEM_ANALYSIS.md](../fx_ai_engine/docs/SYSTEM_ANALYSIS.md) for deep system behavior.

## Working Context
- Run commands from `fx_ai_engine/` unless a task says otherwise.
- Default local development to mock mode (`USE_MT5_MOCK=1`).
- Only run MT5 demo-account checks when credentials and terminal connectivity are confirmed.

## Essential Commands
- Install dependencies: `pip install -r requirements.txt`
- Initialize system: `python init_system.py`
- Run smoke iteration: `python main.py --mode smoke`
- Run demo loop: `python main.py --mode demo`
- Run tests: `python -m pytest -q`
- MT5 connection check: `python test_bridge.py`

## Architecture Boundaries
- Decision flow is fixed: Regime -> Technical -> Adversarial -> Portfolio -> Hard Risk Engine -> Signal Router -> MT5 EA -> Feedback -> SQLite.
- Hard Risk Engine is the final authority. It cannot be bypassed by any upstream approval.
- Adversarial rejections are expected safety behavior.
- MT5 EA is the only component that places broker orders.
- Signal files must be routed with `bridge/signal_router.py` atomic write behavior (tmp -> rename).

## Locked Constraints (Do Not Change Without Explicit User Instruction)
- Risk per trade: 3.2%
- Max open trades: 2
- Max combined exposure: 5%
- Daily stop loss: 8%
- Weekly stop loss: 15%
- Drawdown halt: 20%
- Consecutive loss halt: 3 losses
- Minimum R:R: 2.2
- Instruments: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF
- Timeframes: H1 (regime), M15 (execution)

## Implementation Rules
- Preserve deterministic behavior; avoid hidden randomness in decision logic.
- Keep auditability intact (trade decisions, risk events, execution outcomes).
- Avoid direct writes to bridge pending directories; use router/feedback utilities.
- For changes in risk, agent, bridge, or execution paths, include targeted tests and a smoke run.
- If behavior affects demo execution quality, include diagnostics from spread/limits/bridge checks.

## Demo Validation Gate Before Live Capital
- Complete a 30-day demo cycle with no strategy/risk tuning during the run.
- Minimum acceptance targets:
  - trades >= 25
  - win rate >= 45%
  - average R >= 2.0
  - max drawdown <= 15%
- Abort conditions:
  - drawdown > 20%
  - win rate < 40%
  - average R < 1.8

## Optimization Direction
- Phase 1: reliability and risk-control integrity.
- Phase 2: signal quality and execution accuracy.
- Phase 3: controlled turnover growth after stable demo metrics.
- For small starting capital, prefer consistency and compounding over larger per-trade risk.