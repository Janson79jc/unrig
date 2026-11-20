# AGENTS.md — Coding Agent Instructions for developing `unrig`

> **Note**: This file governs agents editing **this repository (`unrig`)**. Do NOT confuse it with `src/unrig/templates/PROTOCOL.md`, which is the user-facing template installed into downstream projects via `unrig init`.

---

## 1. Repository Architecture

`unrig` is a zero-dependency, 100% air-gapped Python `>=3.10` CLI (`src/` layout):

- `src/unrig/config.py`: Pure-stdlib YAML subset parser & `UnrigConfig`. Always keeps `DEFAULT_PROTECTED_L0_FILES` protected even if user config specifies `protected_l0_files: []`. Always use `normalize_rel_path()` — **NEVER** call `.lstrip("./")` (which strips leading dots from `.unrig.yaml`).
- `src/unrig/format_registry.py`: `#3600` language registry (`FORMAT_REGISTRY`) mapping file extensions to newline-preserving comment blankers (`comment_blanker`) and default ignore globs.
- `src/unrig/l0_guard.py`: Cross-platform L0 anti-tamper guard (`compute_normalized_sha256` with `\r\n -> \n`, `parse_git_status_z` using `git -c core.quotepath=false status --porcelain -uall -z`, `HEAD:.unrig.yaml` union lock, and `PreToolUse` stdin hook returning exit code `2` on block). **NEVER** `.strip()` raw `git status --porcelain` lines before slicing `[:2]` / `[3:]` (causes `MUT-T01` unstaged `' M '` truncation).
- `src/unrig/diff_counter.py`: `CounterMultiset(after - before)` incremental defect scanner (Python AST + JS/TS/Java blanked scanners + user `forbidden_patterns`) and `.unrig/ratchet.json` enforcement requiring non-empty `reason` and zero count inflation.
- `src/unrig/junit_verifier.py`: `<testsuite>` / `<testsuites>` JUnit XML freshness (`xml_mtime >= max_source_mtime - 1.0`), zero skipped/failed/errored tests, required method floors, privilege-token log scan, and `runtime_proof_artifacts`.
- `src/unrig/replay.py`: Built-in referee self-mutation / escape-replay sandbox suite (`unrig replay`).
- `src/unrig/cli_core.py` & `src/unrig/cli.py`: `init | hook | verify | replay`.

---

## 2. Non-Negotiable Engineering Rules

1. **Stdlib Only**: Do NOT add runtime third-party dependencies to `pyproject.toml` (`dependencies = []` must stay empty).
2. **Air-Gapped**: Do NOT add HTTP requests, telemetry, analytics, or external service calls anywhere in `src/` or `tests/`.
3. **No Silent Swallowing or Fake Passes**:
   - Never write `except Exception: pass` (`unrig verify --root .` will block it).
   - Never skip tests, assert `True`, or lower test thresholds to make CI green.
4. **`#3600` Contracts for New Formats**: Any extension added to `FORMAT_REGISTRY` must have fixtures added to `tests/test_format_contracts.py` covering all 4 invariants (comment silence, 1:1 newline preservation, positive detection, default ignore globs).
5. **Reproducible Escape Tests**: Any fix to `l0_guard`, `diff_counter`, or `junit_verifier` must include a real test in `tests/` (and when applicable a sandbox scenario in `replay.py`) proving the bypass fails closed.
6. **Clean-Room Isolation**: Keep all examples, templates, and documentation generic; never reference proprietary or internal organizational codebases.

---

## 3. Mandatory Verification Before Finishing

Run all checks after your final edit and report exact observed outputs:

```bash
python -m pytest -v
python -m unrig.cli replay
python -m unrig.cli verify --root .
python examples/escape-replay/run_demo.py
```

If generating PR/issue comments on a human maintainer's behalf, prefix automated contributions per [CONTRIBUTING.md](CONTRIBUTING.md) (`[autonomous]`, `[directed]`, or `[co-designed]`).
