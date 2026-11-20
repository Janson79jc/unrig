# Contributing to Unrig

Thank you for helping make **Unrig** stronger against silent gate tampering, test dilution, and verification theater.

## Design Principles (Non-Negotiable)

Every contribution must preserve these four invariants:

1. **Zero Third-Party Runtime Dependencies**: `src/unrig/` must run on Python `>=3.10` standard library alone. (`pytest` is only a dev/test dependency.)
2. **100% Air-Gapped / Zero Telemetry**: Never add network calls, telemetry hooks, update checks, or remote endpoints.
3. **Cross-Platform Native**: Code must behave identically on Windows, Linux, and macOS (use `Path.as_posix()`, `CRLF -> LF` normalized hashing, and git `-c core.quotepath=false ... -z`).
4. **Brownfield-Safe & Non-Invasive**: Never require source-code marker annotations inside user repositories, and never break pre-existing legacy files unless a regression delta (`CounterMultiset(after - before)`) is introduced.

---

## AI & Coding Agent Policy

Unrig welcomes AI-assisted contributions. Our policy follows modern open-source standards (similar to `GoogleChrome/modern-web-guidance` and Flutter OSS): **we govern contributor behavior and accountability, not tool choice.**

### 1. Human Accountability First
- **Every line must be understood by a human.** You must read, test, and understand every line of diff you submit, and be able to explain design trade-offs during code review.
- **No autonomous PR spam ("AI slop").** Unsupervised bots or agents mass-opening PRs without human verification will be closed immediately.
- **Human-written communication.** PR descriptions, issue explanations, and responses to reviewer questions should be written in your own words. Never paste raw LLM replies to dismiss reviewer feedback.

### 2. Mandatory Disclosure in PRs (Option A)
- You **must** complete the **AI Assistance Disclosure** section in [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) for every PR:
  - Check whether AI/coding agents were used.
  - Name the tool(s)/model(s) used and briefly note which parts were generated vs reviewed by you.
- Commit trailers such as `Assisted-by: <Tool>` or `Co-authored-by: <Name> <email>` are **encouraged**, especially for verbatim generated blocks, though not strictly hard-blocked by CI.

### 3. Agent Comment & Review Etiquette
If a coding agent drafts a GitHub comment or review note on your behalf, prefix the comment clearly so maintainers know the level of human oversight (aligned with `GoogleChrome/modern-web-guidance`):
- `[autonomous]` — Unsteered agent output.
- `[directed]` — Triggered from a quick human prompt.
- `[co-designed]` — Shaped through iterative human review/debugging.

---

## Development Setup & Local Preflight

```bash
git clone <your-fork-url> unrig
cd unrig
python -m pip install -e ".[dev]"
```

Before opening any PR, run all three local gates and ensure zero failures or skipped tests:

```bash
python -m pytest -v
python -m unrig.cli replay
python -m unrig.cli verify --root .
```

If you also touch `examples/escape-replay/`:

```bash
python examples/escape-replay/run_demo.py
```

---

## How to Submit a Quality PR

1. **Open an issue first for behavioral changes**: New `.unrig.yaml` schema fields, new language analyzers, or semantic policy changes should be discussed briefly before coding.
2. **Keep PRs focused**: One concern per PR.
3. **Follow `#3600` contracts for language support**: Adding or modifying an extension in `src/unrig/format_registry.py` requires full coverage in `tests/test_format_contracts.py`:
   - Invariant 1: Comment silence (0 false positives inside comments)
   - Invariant 2: Exact `1:1` newline preservation
   - Invariant 3: Positive detection outside comments
   - Invariant 4: Default ignore glob filtering
4. **Never weaken gates to pass checks**: Do not delete/skip assertions, widen ignore globs arbitrarily, or silence errors with `except Exception: pass`. Follow [TESTING.md](TESTING.md) and ship every escape fix with a reproducible regression test (and ideally a scenario in `src/unrig/replay.py`).
5. **Licensing**: By submitting a PR, you agree that your contribution is licensed under the repository's Apache-2.0 [LICENSE](LICENSE).
