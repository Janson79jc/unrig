## Summary

<!-- Describe what this PR changes and why. Keep PR descriptions human-written in your own words. -->

Related issue: #

## Type of Change

- [ ] Bug fix (non-breaking fix for an existing issue)
- [ ] Gate / escape-bypass hardening (includes reproducible failure -> block regression test)
- [ ] New language or `.unrig.yaml` capability (includes 4-invariant format contract tests)
- [ ] Documentation / example update

---

## AI Assistance Disclosure (Required)

> Following `CONTRIBUTING.md`, AI assistance is welcome, provided a named human contributor reads, verifies, and takes full responsibility for every line submitted.

- [ ] **No AI assistance used** — all changes were authored manually by me.
- [ ] **AI-assisted** — I used AI / coding agents (e.g., Claude, Copilot, Cursor, Codex, Antigravity) to generate or refine parts of this PR, AND I have personally reviewed and verified every changed line.
  - **Tool(s) / Model(s) used**: <!-- e.g., Claude Sonnet / Cursor / none -->
  - **Human review notes**: <!-- Briefly state what you verified or rewrote by hand -->

---

## Local Preflight & Anti-Tamper Checklist (Required)

- [ ] Ran `python -m pytest -v` locally after the final edit (`0` failures, `0` skipped).
- [ ] Ran `python -m unrig.cli replay` locally (all built-in scenarios passed).
- [ ] Ran `python -m unrig.cli verify --root .` locally (`[PASS]`, `0` violations).
- [ ] Ran `python -m unrig.cli ratchet --root . --check` locally (`[PASS]`, no stale slack).
- [ ] Ran `python examples/escape-replay/run_demo.py` locally (`[PASS]`).
- [ ] Confirmed zero runtime third-party dependencies added to `pyproject.toml`.
- [ ] Confirmed zero outbound network calls or telemetry introduced.
- [ ] Confirmed no existing tests were skipped, deleted, or weakened to make CI pass.
