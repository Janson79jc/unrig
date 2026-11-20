# Security & Gate-Bypass Policy

## Scope

Unrig is a local verification and anti-tamper tool (`0` runtime third-party dependencies, `0` network connections, `0` telemetry). Security reports for Unrig generally fall into two categories:

1. **Gate-Escape / Anti-Tamper Bypasses**: Any trick that allows an agent or command to evade `unrig hook`, `unrig verify`, `CounterMultiset`, `.unrig/ratchet.json` enforcement, or JUnit XML freshness checks.
2. **Standard Code Vulnerabilities**: Path traversal in `l0_guard.py`, shell/command parsing bugs in `evaluate_hook_payload()`, unsafe XML parsing in `junit_verifier.py`, or YAML subset parser crashes/abuse in `config.py`.

---

## Reporting a Vulnerability or Bypass

- **Do NOT open public issues for active L0 hook bypasses or path-traversal exploits** before maintainers have a chance to patch them.
- Please report security flaws privately via **GitHub Private Vulnerability Reporting** (Security tab -> *Report a vulnerability*) or contact the repository maintainers directly with:
  1. Affected version (`unrig --version`) and OS/Python version.
  2. Minimal reproduction steps (ideally a small temp-git reproducer or hook JSON payload).
  3. Expected block vs observed bypass behavior.
- For **non-sensitive false-positive / false-negative heuristic ideas** (e.g., improving a language blanker in `FORMAT_REGISTRY`), normal GitHub Issues with the **Escape / Bypass Report** template are welcome.

Once patched, valid bypasses are converted into permanent regression cases in `src/unrig/replay.py` and `examples/escape-replay/` with credit to the reporter (unless anonymity is requested).

---

## Supported Versions

Only the latest `0.1.x` / main development line receives security fixes.
