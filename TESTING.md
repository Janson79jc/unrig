# Testing Guide & Quality Charter

Unrig acts as a verification referee for coding agents. Tests in this repository must therefore uphold higher rigor than normal unit tests: **every test must fail when the corresponding guard or detector breaks.**

---

## 1. Test Pyramid (4 Layers)

| Layer | Location | What It Proves |
|---|---|---|
| **Format Contracts (`#3600`) & Rule Budgets** | `tests/test_format_contracts.py`, `tests/test_rule_precision_and_fanout.py` | Every registered extension obeys `#3600` 4 invariants (comment silence, exact `1:1` newline preservation, positive detection, ignore-glob filtering); detector rules meet the `>= 80%` Precision Floor and prefilter AST fan-out budget |
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

---

## 4. Rule Acceptance Gates (For New or Relaxed Rules)

Borrowed directly from real-world static-analyzer regressions (such as `GoogleChrome/modern-web-guidance-src` #3638), any PR that adds a new heuristic/AST rule or loosens an existing rule's matching boundary must pass two lightweight quantitative gates in `tests/test_rule_precision_and_fanout.py` before merge:

1. **Static Rule Precision Floor (`>= 80%`)**
   - Evaluate the rule across both **positive cases** (true agent defects) and **clean negative holdout cases** (idiomatic production code, comments, string literals, `@abstractmethod`, `ignored` catch handlers).
   - Compute `Precision = TP / (TP + FP)`.
   - **Auto-reject (`< 80%`)**: Catching one extra edge case by introducing false positives (for example `1 TP / 11 FP = 8.3%`) is a strict veto — refine the pattern or keep it out of default rules.
2. **Prefilter AST Fan-Out Budget**
   - Unrig guards heavy parsers (like `ast.parse` in `python_ast_prefilter`) behind cheap fast-token substring checks so clean utility modules skip AST parsing altogether.
   - Any PR loosening a fast-token prefilter must measure **before vs. after clean-file AST fan-out** (`clean_files_parsed / total_clean_files`).
   - **Veto heuristic**: Loosening a word boundary or token gate that spikes heavy AST parsing on clean files by `>= 25%` (e.g., the `+40%–52%` AST fan-out surge seen when loosening word boundaries) without strictly proportional `>= 80%`-precision `delta_TP` gains is rejected.
