---
description: "Use before coding in this repo. Creates a clear system analysis, risk check, and safe implementation plan."
name: "Analyze System First"
argument-hint: "What feature, bug, or subsystem should be analyzed?"
agent: "agent"
---

Analyze the requested area before making any edits.

Requirements:
- Explain current behavior in plain language first, then technical detail.
- Identify constraints from SRS v1 that could be affected.
- List findings by severity (critical, high, medium, low).
- Call out risks to demo-account stability and trade-control integrity.
- Produce a minimal, testable implementation plan.

Output format:
1. Current behavior summary
2. Findings by severity
3. SRS v1 impact check
4. Safe implementation plan
5. Validation commands to run
