# Changelog

All notable changes to **Unrig** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased (Level 0 Core)

### Added
- Pure-stdlib Python `>=3.10` CLI (`unrig init`, `unrig hook`, `unrig verify`, `unrig replay`) with zero third-party runtime dependencies and zero telemetry.
- **L0 Anti-Tamper Guard** (`l0_guard.py`):
  - `PreToolUse` stdin JSON hook returning exit code `2` on protected L0 path mutations, path traversal, shell redirects, and `git commit --no-verify`.
  - Cross-platform `git -c core.quotepath=false status --porcelain -uall -z` parsing (`MUT-T01` unstaged ` M` and `MUT-W03` nested untracked directory protection).
  - Union locking against `HEAD:.unrig.yaml` + immutable `DEFAULT_PROTECTED_L0_FILES` (blocks attempts to disarm gates via `protected_l0_files: []`).
- **Brownfield Incremental CounterMultiset** (`diff_counter.py`) + `#3600` **`FORMAT_REGISTRY`** (`format_registry.py`):
  - Language-aware newline-preserving comment blanking across Python, JS/TS, Vue, Java, Go, XML/HTML, SQL, Shell, and YAML.
  - Detects net-new empty test stubs, tautological assertions, silent exception swallowing, placeholder stubs, and custom `.unrig.yaml` `forbidden_patterns`.
  - Enforces non-empty `reason` and zero count inflation in `.unrig/ratchet.json`.
- **JUnit XML Freshness & Runtime Artifacts** (`junit_verifier.py`):
  - Supports `<testsuite>` and `<testsuites>` formats with timestamp freshness checks, zero-skip/failure/error policy, required method floors, privilege-token log scanning, and runtime artifact checks.
- **Built-in Referee Escape-Replay** (`replay.py`) & standalone runnable demo (`examples/escape-replay/`).
- Community & agent governance suite (`CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `.github/` templates, CI workflow).
