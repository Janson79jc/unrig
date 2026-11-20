# Escape-Replay Demo (`examples/escape-replay`)

This standalone example demonstrates **five real coding-agent gate-escape patterns** inside isolated temporary Git repositories and shows how **Unrig** catches each one deterministically without touching your main repo:

1. **`MUT-T01` (Unstaged L0 Tamper + Disarm Attempt)**: An agent makes an unstaged edit to `.unrig.yaml` (`protected_l0_files: []`) to try disabling L0 locks.
2. **`MUT-W02` (Baseline Debt Duplication)**: A brownfield file already has one legacy `assert True`. An agent duplicates `assert True` into a second test; set-based diffs miss it, whereas Unrig's `CounterMultiset(after - before)` catches the `+1` delta.
3. **`MUT-W03` (Nested Untracked Stub Discovery)**: An agent creates an empty stub test inside a nested untracked directory (`pkg/sub/test_sneaky.py`), which `git status --porcelain -uall -z` discovers and blocks.
4. **`JUNIT-STALE` (Backdated / Reused Test Report)**: An agent modifies production code without re-running tests, leaving an older `junit.xml` report behind.
5. **`HOOK-L0-BLOCK` (`PreToolUse` Guard)**: Direct file writes, path traversals (`src/../.unrig/ratchet.json`), shell redirects (`>`), and `git commit --no-verify` are blocked immediately with exit code **`2`**.

## Run Locally

```bash
python examples/escape-replay/run_demo.py
```

Or run Unrig's built-in CLI self-test:

```bash
unrig replay
```
