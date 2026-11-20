---
name: Escape / Gate-Bypass Report
about: Report a coding-agent evasion pattern that slips past `unrig verify` or `unrig hook`
title: "[Escape]: "
labels: escape-bypass
---

> **Security Note**: If you found a critical L0 hook bypass or path-traversal exploit, please report it privately via **GitHub Security Advisories** (see `SECURITY.md`) before opening a public issue. For heuristic/AST evasion ideas (e.g. a new tautology or stub trick), this public template is ideal.

## Target Gate Layer

- [ ] `l0_guard` / `PreToolUse` hook (`unrig hook`)
- [ ] `diff_counter` / `CounterMultiset`
- [ ] `junit_verifier` / freshness / `runtime_proof_artifacts`
- [ ] `ratchet.json` enforcement

## Minimal Evasion Snippet

```text
# Show the exact snippet or edit pattern that should have been blocked
```

## Observed vs Expected Outcome

- **Observed**: `[PASS]` / exit `0`
- **Expected**: `[BLOCKED]` / exit `1` or `2`
