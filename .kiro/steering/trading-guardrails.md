---
inclusion: fileMatch
fileMatchPattern: ['fx_ai_engine/core/risk/**', 'fx_ai_engine/core/agents/**', 'fx_ai_engine/bridge/**', 'fx_ai_engine/mt5_ea/**', 'fx_ai_engine/main.py', 'fx_ai_engine/config_microcapital.py']
---

# Trading guardrails for safety-critical paths

You are editing code on the decision, risk, bridge or execution path.

- Do not change locked SRS constants unless the owner explicitly asked in this session.
- Keep the Hard Risk Engine last; no upstream component may bypass it.
- Adversarial rejections are expected behavior, not defects.
- Route signals only through `bridge/signal_router.py` (tmp file then rename). Never write raw files into `pending_signals/`.
- The EA is the only component that places broker orders.
- Add or update targeted tests for every behavior change, run the full suite, and run a mock smoke run.
- These paths need a review by a different model and owner approval before merge.
