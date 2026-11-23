# AGENTS.md — Coding Agent Instructions for developing `unrig`

> **Note**: This file governs agents editing **this repository (`unrig`)**. Do NOT confuse it with `src/unrig/templates/PROTOCOL.md`, which is the user-facing template installed into downstream projects via `unrig init`.

---

## 1. Repository Architecture

`unrig` is a zero-dependency, 100% air-gapped Python `>=3.10` CLI (`src/` layout):

- `src/unrig/config.py`: Pure-stdlib YAML subset parser & `UnrigConfig`. Always keeps `DEFAULT_PROTECTED_L0_FILES` protected even if user config specifies `protected_l0_files: []`. Always use `normalize_rel_path()` — **NEVER** call `.lstrip("./")` (which strips leading dots from `.unrig.yaml`).
- `src/unrig/format_registry.py`: 4-invariant language registry (`FORMAT_REGISTRY`) mapping file extensions to newline-preserving comment blankers (including JS/TS `/regex/` + nested `${...}` lexical blanker and Vue `<template>`/`<script>` isolation) and default ignore globs.
- `src/unrig/l0_guard.py`: Cross-platform L0 anti-tamper guard (`compute_normalized_sha256` with `\r\n -> \n`, `parse_git_status_z` using `git -c core.quotepath=false status --porcelain -uall -z`, `HEAD:.unrig.yaml` union lock, and `PreToolUse` stdin hook returning exit code `2` on block). **NEVER** `.strip()` raw `git status --porcelain` lines before slicing `[:2]` / `[3:]` (causes `MUT-T01` unstaged `' M '` truncation).
- `src/unrig/diff_counter.py`: `CounterMultiset(after - before)` incremental defect scanner (Python AST + lexical JS/TS/Vue suppression / `TYPE-ESCAPE` / `.only` / noop-test / `catch {}` + Python/JS-TS/Java `DUMMY-CONSTANT-RETURN` + cross-file `Alpha-Normalized` `CLONE-SPAM` + user `forbidden_patterns`), `.unrig/ratchet.json` enforcement requiring non-empty `reason` and zero count inflation, coverage transparency (`DiffScanStats`, `[WARN] [SHALLOW_HISTORY]`), plus stale slack detection (`find_ratchet_slack` / `prune_ratchet`).
- `src/unrig/junit_verifier.py`: `<testsuite>` / `<testsuites>` JUnit XML freshness (`xml_mtime >= max_source_mtime - 1.0`), zero skipped/failed/errored tests, required method floors, privilege-token log scan, and `runtime_proof_artifacts`.
- `src/unrig/replay.py`: Built-in referee self-mutation / escape-replay sandbox suite (`unrig replay`).
- `src/unrig/cli_core.py` & `src/unrig/cli.py`: `init | hook | verify | replay | ratchet | stats`.

---

## 2. Non-Negotiable Engineering Rules

1. **Stdlib Only**: Do NOT add runtime third-party dependencies to `pyproject.toml` (`dependencies = []` must stay empty).
2. **Air-Gapped**: Do NOT add HTTP requests, telemetry, analytics, or external service calls anywhere in `src/` or `tests/`.
3. **No Silent Swallowing or Fake Passes**:
   - Never write `except Exception: pass` (`unrig verify --root .` will block it).
   - Never skip tests, assert `True`, or lower test thresholds to make CI green.
4. **4-Invariant Format Contracts**: Any extension added to `FORMAT_REGISTRY` must have fixtures added to `tests/test_format_contracts.py` covering all 4 invariants (comment silence, 1:1 newline preservation, positive detection, default ignore globs).
5. **Reproducible Escape Tests & Rule Quality Gates**: Any fix or rule addition in `l0_guard`, `diff_counter`, or `junit_verifier` must follow [TESTING.md](TESTING.md): include a real test in `tests/` (and `replay.py` when applicable), maintain the **`>= 80%` precision floor** on clean holdouts, and keep `python_ast_prefilter` AST fan-out tight on clean files.
6. **Clean-Room Isolation**: Keep all examples, templates, and documentation generic; never reference proprietary or internal organizational codebases.

---

## 3. Mandatory Verification Before Finishing

Run all checks after your final edit and report exact observed outputs:

```bash
python -m pytest -v
python -m unrig.cli replay
python -m unrig.cli verify --root .
python -m unrig.cli ratchet --root . --check
python examples/escape-replay/run_demo.py
```

If generating PR/issue comments on a human maintainer's behalf, prefix automated contributions per [CONTRIBUTING.md](CONTRIBUTING.md) (`[autonomous]`, `[directed]`, or `[co-designed]`).
