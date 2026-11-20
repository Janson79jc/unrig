# Unrig Agent Working Protocol (Level 0)

## Three Universal STOP Rules

1. **STOP — Never Touch L0 Gates**:
   Never edit, delete, rename, skip, or append exemptions to `.unrig.yaml`, `.unrig/ratchet.json`,
   `.unrig/PROTOCOL.md`, or any path listed under `protected_l0_files`. If a gate blocks your
   change and you believe the gate itself is wrong, **STOP** and ask the human maintainer.

2. **STOP — Never Fabricate Passing Signals**:
   Never replace a failing test with an empty body (`pass`, `{}`), tautological assertion
   (`assert True`, `assertTrue(true)`, `expect(true).toBe(true)`), `@Disabled` / `@pytest.mark.skip`,
   broadened `except Exception: pass` / `catch (e) {}`, or a reused stale XML report.

3. **STOP — Never Bypass or Suppress Fail-Closed Checks**:
   Never pass `--no-verify`, maintenance bypass flags, or duplicate existing baseline debt to hide
   a regression. Every grandfathered entry in `.unrig/ratchet.json` requires human approval and an
   explicit, non-empty `reason`.

## Four Escalation Patterns

- **Ambiguous Requirement**: Stop coding and ask clarifying questions before inventing fallback behavior.
- **Unreachable External Dependency**: Report the missing service/credential rather than stubbing fake success data.
- **Conflicting Existing Debt**: Surface the pre-existing violation and request a human-approved ratchet entry rather than masking it.
- **Repeated Gate Failure (2+ retries on same failure)**: Stop patching symptoms, summarize root cause, and escalate for review.
