# Product Overview

FX AI Engine is a deterministic, local-hosted Forex automation system for proprietary trading on major currency pairs. The system implements a multi-agent architecture with hard risk controls, designed for aggressive compounding under strict survival constraints.

## Core Principles

- Forex majors only (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF)
- Proprietary capital only (no investor funds in v1)
- Deterministic Python intelligence with MT5 execution bridge
- Zero recurring infrastructure cost (local hosting only)
- Hard, non-negotiable risk controls override all agent decisions
- Full auditability for every decision and execution event

## Key Features

- Multi-agent decision system (Regime, Technical, Adversarial, Portfolio Manager)
- Independent Hard Risk Engine with daily/weekly loss limits
- JSON-based execution bridge to MetaTrader 5
- SQLite persistence for complete trade lifecycle tracking
- Optional ML signal ranking with probability-based TP scaling
- Economic calendar and session filtering
- Sentiment analysis integration (optional)

## Risk Policy (Hard-Locked)

- Risk per trade: 3.2% (base)
- Max simultaneous trades: 2
- Max combined exposure: 5%
- Daily stop loss: 8%
- Weekly stop loss: 15%
- Hard equity drawdown: 20%
- Consecutive loss halt: 3 losses

## Validation Requirements

30-day forward demo validation required before live deployment with minimum acceptance criteria:
- Trades executed: >= 25
- Win rate: >= 45%
- Average R: >= 2.0
- Max drawdown: <= 15%
