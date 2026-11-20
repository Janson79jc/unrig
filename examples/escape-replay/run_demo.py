#!/usr/bin/env python3
"""Standalone Escape-Replay Demonstration for Unrig v0.1.0.

Runs without external dependencies and cleans up all temporary sandboxes.
Usage:
    python examples/escape-replay/run_demo.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

# Allow running directly from a source checkout without installing first
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from unrig.cli_core import verify_workspace  # noqa: E402
from unrig.config import load_config  # noqa: E402
from unrig.l0_guard import evaluate_hook_payload  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _create_repo(root: Path, files: dict[str, str]) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "demo@example.com")
    _git(root, "config", "user.name", "Unrig Demo")
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


def main() -> int:
    demos: list[tuple[str, bool, str]] = []

    with tempfile.TemporaryDirectory(prefix="unrig-demo-") as tmp:
        base = Path(tmp)

        # Demo 1: MUT-T01 — Unstaged tamper + disarm attempt on `.unrig.yaml`
        d1 = base / "demo_mut_t01"
        d1.mkdir()
        _create_repo(d1, {".unrig.yaml": "version: 1\n", "app.py": "def ok():\n    return 1\n"})
        (d1 / ".unrig.yaml").write_text("version: 1\nprotected_l0_files: []\n", encoding="utf-8")
        ok1, lines1 = verify_workspace(d1)
        demos.append(("MUT-T01 (Unstaged L0 tamper + disarm)", not ok1, next((l for l in lines1 if "L0_TAMPER" in l), "")))

        # Demo 2: MUT-W02 — Duplicating pre-existing legacy assertion in dirty brownfield file
        d2 = base / "demo_mut_w02"
        d2.mkdir()
        _create_repo(d2, {".unrig.yaml": "version: 1\n", "tests/test_legacy.py": "def test_old():\n    assert True\n"})
        (d2 / "tests/test_legacy.py").write_text(
            "def test_old():\n    assert True\n\ndef test_added():\n    assert True\n",
            encoding="utf-8",
        )
        ok2, lines2 = verify_workspace(d2)
        demos.append(("MUT-W02 (Duplicate baseline violation)", not ok2, next((l for l in lines2 if "TAUTOLOGICAL-ASSERT" in l), "")))

        # Demo 3: MUT-W03 — Untracked empty stub inside nested untracked folder
        d3 = base / "demo_mut_w03"
        d3.mkdir()
        _create_repo(d3, {".unrig.yaml": "version: 1\n"})
        stub = d3 / "pkg" / "nested" / "test_stub.py"
        stub.parent.mkdir(parents=True)
        stub.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
        ok3, lines3 = verify_workspace(d3)
        demos.append(("MUT-W03 (Nested untracked test stub)", not ok3, next((l for l in lines3 if "EMPTY-TEST-STUB" in l), "")))

        # Demo 4: JUNIT-STALE — Reusing stale XML older than latest source edit
        d4 = base / "demo_junit_stale"
        d4.mkdir()
        _create_repo(d4, {".unrig.yaml": "version: 1\nreports:\n  require_reports: true\n", "app.py": "x = 1\n"})
        xml_file = d4 / "test-results" / "junit.xml"
        xml_file.parent.mkdir()
        xml_file.write_text('<testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0"><testcase classname="t" name="test_x"/></testsuite>', encoding="utf-8")
        src_ts = (d4 / "app.py").stat().st_mtime
        os.utime(xml_file, (src_ts - 120, src_ts - 120))
        ok4, lines4 = verify_workspace(d4)
        demos.append(("JUNIT-STALE (Backdated XML report)", not ok4, next((l for l in lines4 if "REPORT_STALE" in l), "")))

        # Demo 5: HOOK-L0-BLOCK — PreToolUse hook catches traversal + --no-verify
        d5 = base / "demo_hook"
        d5.mkdir()
        (d5 / ".unrig.yaml").write_text("version: 1\n", encoding="utf-8")
        cfg = load_config(d5)
        c_trav, m_trav = evaluate_hook_payload(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "src/../.unrig/ratchet.json"}}), d5, cfg)
        c_nover, m_nover = evaluate_hook_payload(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m test --no-verify"}}), d5, cfg)
        demos.append(("HOOK-L0-BLOCK (PreToolUse traversal & --no-verify)", c_trav == 2 and c_nover == 2, f"{m_trav} | {m_nover}"))

    print("Unrig Escape-Replay Standalone Demo:")
    all_passed = True
    for name, blocked_ok, detail in demos:
        tag = "[BLOCKED CLEANLY]" if blocked_ok else "[ESCAPED!]"
        print(f"  {tag} {name}")
        print(f"      -> {detail}")
        if not blocked_ok:
            all_passed = False

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
