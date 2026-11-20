# Testing Guide & Quality Charter

Unrig acts as a verification referee for coding agents. Tests in this repository must therefore uphold higher rigor than normal unit tests: **every test must fail when the corresponding guard or detector breaks.**

---

## 1. Test Pyramid (4 Layers)

| Layer | Location | What It Proves |
|---|---|---|
| **Format Contracts (`#3600`)** | `tests/test_format_contracts.py` | Every registered extension obeys 4 invariants: comment silence (0 FP), exact `1:1` newline preservation, positive code detection, and ignore-glob filtering |
| **Parsers & Verifiers** | `tests/test_junit_and_diff.py` | YAML subset parser, JUnit XML `<testsuite>`/`<testsuites>` freshness, `skipped == 0`, required method floors, runtime proof artifacts, and `.unrig/ratchet.json` `reason`/cap enforcement |
| **Referee Self-Mutation & Edge Cases** | `tests/test_referee_mutations.py`, `tests/test_edge_cases_and_packaging.py` | Real temporary Git repositories verifying `MUT-T01` unstaged ` M`, `MUT-T02` staged, `MUT-W02` multiset duplicate delta, `MUT-W03` `-uall` nested untracked dir, `R ` renames, `HEAD:.unrig.yaml` union locks, multi-commit `HEAD~1..HEAD` regressions, cross-language JS/TS/Java/Python rules, real `pytest --junitxml`, and clean packaged wheel/target installs |
| **Escape-Replay Showcase** | `src/unrig/replay.py`, `examples/escape-replay/run_demo.py` | Deterministic self-contained sandboxes executed via `unrig replay` and CI |

---

## 2. Running the Suite Locally

```bash
# Full test suite (recommended before every commit/PR)
python -m pytest -v

# Generate a real JUnit XML report locally (optional verification)
python -m pytest --junitxml=test-results/junit.xml

# Built-in referee escape-replay suite
python -m unrig.cli replay

# Standalone escape demonstration
python examples/escape-replay/run_demo.py

# Self-verification on the Unrig repository itself
python -m unrig.cli verify --root .
```

---

## 3. Rules for Writing Tests (Anti-Theater Standard)

Matches the `[🛑]` P0-2 checklist in `src/unrig/templates/blind_auditor_prompt.md`:

1. **No empty stubs or tautologies**: Never submit `pass`-only tests, `assert True`, or mock-testing-a-mock assertions.
2. **Real Git / Real Subprocess for Guard Tests**: Changes to `l0_guard.py`, `diff_counter.py`, or CLI exit codes must be tested against real temporary `git init` sandboxes (not monkeypatched `subprocess.run` stubs alone).
3. **Positive + Negative Pairing**:
   - Every suppression/exemption (e.g., `@abstractmethod`, `catch (Exception ignored) {}`, comment blanking) must prove **both** that legitimate code passes (`0` findings) **and** that non-exempt siblings still fail cleanly.
4. **New Language Support (`#3600`)**: Adding an extension to `FORMAT_REGISTRY` must add a matching fixture entry to `COMMENT_SNIPPETS_BY_EXT` in `tests/test_format_contracts.py`.
5. **Packaging Integrity**: Keep `tests/test_edge_cases_and_packaging.py` green so `src/unrig/templates/*` always ships cleanly inside installed packages without depending on `PYTHONPATH=src`.
