---
description: "Implement a requested change while preserving SRS v1 safety limits, bridge reliability, and auditability."
name: "Implement Safely"
argument-hint: "Describe the change you want implemented"
agent: "agent"
---

Implement the requested change with strict safety controls.

Rules:
- Do not change locked SRS v1 constraints unless explicitly instructed.
- Keep Hard Risk Engine as final authority.
- Keep atomic signal routing semantics intact (tmp -> rename).
- Preserve logging and database audit trails for decisions and risk events.

Validation checklist:
- Run targeted tests for changed files.
- Run full test suite when risk/agent/bridge behavior changes.
- Run smoke mode and report the result.
- If bridge/execution changed, run bridge and diagnostics checks.

Output format:
1. What changed
2. Why it is safe
3. Test and run results
4. Remaining risks and next actions
