# ADR 0001: Development plan defaults

Date: 2026-11-03. Status: accepted by the owner ("use defaults").

Context: `fx_ai_engine/docs/plans/2026-11-03-full-development-and-ai-plan.md`, section 3.

| # | Decision | Adopted |
| --- | --- | --- |
| D1 | Broker / account for validation | No funded account exists. The MetaQuotes-Demo account connected to the local MT5 terminal is the validation environment for Phases 1–6. Reports must state that MetaQuotes-Demo spreads, fills and server time may differ from a real broker. |
| D2 | SRS §12.2 "average R" | To be replaced in SRS v1.1 by: expectancy ≥ +0.25R per trade after costs, profit factor ≥ 1.3, average winner ≥ 1.5R. Until v1.1, report both the old and new metrics. |
| D3 | Risk per trade in the 30-day demo | Keep 3.2% only if Phase 2 replay shows the drawdown gate is reachable; otherwise lower to 1.0–1.5% through SRS v1.1. |
| D4 | Live market-data path | The EA exports closed bars, quotes and calendar to the file bridge; Python reads them through a file-backed connection. The MT5 MCP server is for development, research and verification only. |
| D5 | Host for the demo | This Mac, with system sleep disabled and launchd supervision. A native Windows host is required before any real-money trading. |
| D6 | AI inside the running system | Shadow mode only in v1. Promotion later, and only for components that can veto or reduce risk. Requires SRS v1.x change because SRS v1 disables LLM usage. |
| D7 | LLM provider for in-system experiments | Owner's existing Anthropic/OpenAI key for offline jobs; MT5 built-in assistant with Trading set to "not trade at all". |
| D8 | Uncommitted work | Committed to branch `hardening/2026-11` and pushed (commit d2ab588). |
