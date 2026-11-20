# Unrig

**Anti-Tamper Gates, Freshness Verification, and Isolated Blind Review for Coding Agents.**

> Software engineering governance CLI (not affiliated with 3D rigging / `UniRig`).

Coding agents rarely fail cleanly. Left unchecked on a failing check, agents frequently weaken assertions, skip skipped-test counters, edit gate configs, duplicate existing brownfield debt, or reuse stale test XML reports instead of fixing the underlying bug.

**Unrig** locks the verification boundary cleanly — **100% offline / zero telemetry / zero third-party runtime dependencies (Python >= 3.10 stdlib only)** and **non-invasive on brownfield repos** (zero source-code marker annotations required).

---

## Architecture at a Glance

```text
  Agent Tool Call (Write / Edit / Bash)
                 │
                 ▼
  ┌─────────────────────────────┐   exit 2 (block L0 writes, traversal, `> file`, `--no-verify`)
  │   1. `unrig hook` (L0 Guard)│──────────────────────────────────────────────► BLOCKED
  └──────────────┬──────────────┘
                 │ exit 0
                 ▼
  ┌─────────────────────────────┐
  │ 2. `unrig verify` Referee   │
  │   • L0 Integrity            │── git `-uall -z` + `HEAD:.unrig.yaml` union + CRLF-normalized SHA-256
  │   • `#3600` Comment Blanker │── 1:1 newline preservation (Py, JS/TS, Vue, Java, Go, XML, SQL, SH, YAML)
  │   • `CounterMultiset` Diff  │── net-new delta `(after - before)` + `.unrig/ratchet.json` (`reason` required)
  │   • JUnit & Runtime Proof   │── `<testsuite>` / `<testsuites>`, `xml_mtime >= src_mtime`, `skipped == 0`
  └──────────────┬──────────────┘
                 │ [PASS]
                 ▼
  ┌─────────────────────────────┐
  │ 3. Blind Auditor `[🛑]`      │   clean context, read-only diff review (no writer excuses leaked)
  └─────────────────────────────┘
```

---

## Why Unrig vs. Ordinary Linters / `pre-commit`?

| Failure Mode | Standard Linter / `pre-commit` | Unrig (`v0.1.0`) |
|---|---|---|
| Agent edits `.unrig.yaml` or `--no-verify` | Bypassed cleanly | Blocked pre-write (`unrig hook` exit `2`) + detected unstaged via `-uall -z` + `HEAD` union lock |
| Agent duplicates existing dirty debt in brownfield file | Ignored by line diffs or fails whole legacy file | Caught by `CounterMultiset(after - before)` without touching untouched legacy debt |
| Agent weakens/skips tests (`assert True`, `skipped=1`) | Compiles & passes CI | Blocked by AST/token stub rules + `forbid_skipped: true` + required method floors |
| Agent submits old green `junit.xml` after editing code | Passes file existence check | Blocked as `[REPORT_STALE]` (`xml_mtime < max_source_mtime - 1s`) |
| External runtime dependencies / cloud sync | Heavy install / cloud accounts | Zero third-party deps (Python stdlib only), 100% air-gapped, zero telemetry |

---

## Quickstart

```bash
pip install -e .

# 1. Initialize `.unrig.yaml` and `.unrig/` protocol/auditor templates
unrig init
git add .unrig.yaml .unrig/ && git commit -m "chore: initialize unrig Level 0 gates"

# 2. Run verification referee
unrig verify

# 3. Verify Unrig's own referee against built-in escape mutations
unrig replay

# Optional: run the standalone escape-replay showcase
python examples/escape-replay/run_demo.py
```

---

## Wiring `unrig hook` (`PreToolUse`)

Point your coding-agent client's `PreToolUse` hook to read JSON from stdin and run `unrig hook` (for example, in Claude Code `.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "unrig hook"
          }
        ]
      }
    ]
  }
}
```

When a tool attempts to modify any path in `protected_l0_files` (even via `../` traversal, Windows backslashes, shell output redirections like `> .unrig.yaml`, or `git commit --no-verify`), `unrig hook` exits **`2`** and prints an actionable block message to `stderr`.

---

## `.unrig.yaml` Reference

| Key | Default | Purpose |
|---|---|---|
| `version` | `1` | Schema version |
| `protected_l0_files` | `[.unrig.yaml, .unrig/ratchet.json, .unrig/PROTOCOL.md, .unrig/blind_auditor_prompt.md]` | Extra paths always merged with immutable L0 defaults + `HEAD:.unrig.yaml` |
| `reports.require_reports` | `false` | Fail with `[REPORT_MISSING]` if no JUnit XML matches `junit_globs` |
| `reports.forbid_skipped` | `true` | Fail if `skipped > 0` in any JUnit XML |
| `reports.junit_globs` | Surefire / `junit*.xml` / `test-results/*.xml` | Glob patterns for JUnit XML discovery |
| `reports.required_tests` | `{}` | Map of `suite_name: [method_a, ...]` that must execute non-skipped |
| `reports.require_fresh_against` | `[]` | Custom source globs for staleness timestamp check (defaults to all non-ignored source files) |
| `reports.privilege_log_tokens` | `["--no-verify", "gate.mode=maintenance"]` | Tokens forbidden inside test XML logs / `.txt` outputs |
| `runtime_proof_artifacts` | `[]` | List of `{path, max_age_seconds}` runtime artifacts verified fresh & non-empty |
| `diff_checks.enabled` | `true` | Toggle `CounterMultiset(after - before)` incremental scan |
| `diff_checks.baseline` | `"HEAD~1"` | Git revision baseline (`HEAD` fallback on single-commit repos) |
| `diff_checks.forbidden_patterns` | `[]` | Team-specific `{id, pattern, message, paths}` rules matched against comment-blanked code |
| `ratchet_file` | `".unrig/ratchet.json"` | JSON ledger requiring non-empty `reason` (>= 3 chars) per grandfathered cap |
| `shell_write_redirect_guards` | `true` | Inspect shell tool commands in `unrig hook` for L0 redirects / `--no-verify` |

---

## Air-Gapped & Privacy Guarantee

`unrig` makes **zero network calls** and contains **zero telemetry**. All configuration, ratchet ledgers, and verification runs stay strictly local on your machine.

---

## Contributing, Agents & Security

- **Contributors & AI Policy**: Read [CONTRIBUTING.md](CONTRIBUTING.md) (human accountability + required PR AI disclosure aligned with modern open-source standards).
- **Testing Guide & Rigor Standards**: Read [TESTING.md](TESTING.md).
- **Coding Agents Working on This Repo**: Read [AGENTS.md](AGENTS.md).
- **Security & Escape Bypass Reports**: Read [SECURITY.md](SECURITY.md).
- **Community Standards**: Read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **License**: [Apache-2.0](LICENSE) — see [CHANGELOG.md](CHANGELOG.md) for release notes.
