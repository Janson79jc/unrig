# Unrig

**Anti-Tamper Gates, Freshness Verification, and Isolated Blind Review for Coding Agents.**

> Software engineering governance CLI (not affiliated with 3D rigging / `UniRig`).

Coding agents rarely fail cleanly. Left unchecked on a failing check, agents frequently weaken tests, skip skipped-test counters, edit gate configs, or reuse stale test XML reports instead of fixing the underlying bug.

**Unrig** locks the verification boundary cleanly — **100% offline / zero telemetry / zero third-party runtime dependencies (Python >= 3.10 stdlib only)** and **non-invasive on brownfield repos** (zero mandatory code annotations).

## What `v0.1.0` Does

1. **L0 Anti-Tamper Guard (`unrig hook` + `unrig verify`)**:
   - Deterministic `PreToolUse` hook returns exit code **`2`** whenever a tool call attempts to write/edit protected gate files (`.unrig.yaml`, `.unrig/ratchet.json`, `.unrig/PROTOCOL.md`, custom paths) or redirect shell output into them.
   - Verifies tracked L0 integrity using `git -c core.quotepath=false status --porcelain -uall -z` without line `.strip()` corruption (catching unstaged ` M` edits, nested untracked files, and LF/CRLF spoofing).
2. **JUnit XML Freshness & Runtime Proof (`unrig verify`)**:
   - Works out of the box with Maven Surefire (`TEST-*.xml`), `pytest --junitxml`, `vitest`, or Go `gotestsum`.
   - Rejects missing reports, stale reports (`xml_mtime < max_source_mtime`), `failures > 0`, `errors > 0`, `skipped > 0`, missing required test methods, maintenance-mode privilege tokens in logs, and stale `runtime_proof_artifacts`.
3. **Brownfield-Safe Incremental `CounterMultiset(after - before)` (`unrig verify`)**:
   - Blanks comments per language via `FORMAT_REGISTRY` (strict 1:1 newline preservation).
   - Counts semantic defects (empty test stubs, tautological assertions like `assert True` / `assertTrue(true)` / `expect(true).toBe(true)`, silent `catch {}`, bare placeholder stubs) as multiset deltas against `HEAD` — so duplicating an existing dirty violation is immediately caught, while legacy debt stays isolated.
   - Optional `.unrig/ratchet.json` exceptions require an explicit non-empty `reason` and forbid count inflation.
4. **Built-In Referee Escape Replay (`unrig replay`)**:
   - Spins up isolated temporary Git sandboxes and runs self-mutation / escape-replay checks (`MUT-T01` unstaged L0 tamper, `MUT-W02` duplicated baseline violation, `MUT-W03` nested untracked dir, empty ratchet reason, stale JUnit XML, PreToolUse hook block).

## Quickstart

```bash
pip install -e .

# 1. Initialize Level 0 templates in your repo (commit .unrig.yaml & .unrig/ to lock L0)
unrig init

# 2. Verify workspace & test reports
unrig verify

# 3. Verify Unrig's own referee against built-in escape mutations
unrig replay
```

To wire the `PreToolUse` hook into your coding agent client, point its hook command to:

```bash
unrig hook
```

When invoked with Claude/OpenCode-style `PreToolUse` JSON on stdin, `unrig hook` exits `2` to block edits targeting `protected_l0_files` or shell redirections into protected paths.

## Air-Gapped Guarantee

`unrig` makes **zero network calls** and contains **zero telemetry**. All configuration, ratchet ledgers, and verification runs stay strictly local on your machine.
