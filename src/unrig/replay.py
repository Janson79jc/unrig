"""Built-in Referee Escape-Replay & Self-Mutation Suite (`unrig replay`)."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile

from .cli_core import verify_workspace
from .config import load_config
from .l0_guard import evaluate_hook_payload


@dataclass(frozen=True)
class ReplayCaseResult:
    case_id: str
    description: str
    passed: bool
    detail: str


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_sandbox(root: Path, files: dict[str, str]) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "referee@example.com")
    _git(root, "config", "user.name", "Unrig Referee")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")


def run_builtin_replay_suite() -> list[ReplayCaseResult]:
    """Run deterministic escape-replay & referee self-mutation checks in isolated temp sandboxes."""
    results: list[ReplayCaseResult] = []

    with tempfile.TemporaryDirectory(prefix="unrig-replay-") as tmp:
        base = Path(tmp)

        # 1. MUT-T01: unstaged modification (' M') to tracked L0 file MUST fail verify
        t01 = base / "mut_t01"
        t01.mkdir()
        _init_sandbox(t01, {
            ".unrig.yaml": "version: 1\nprotected_l0_files:\n  - .unrig.yaml\n",
            "app.py": "def ok():\n    return 1\n",
        })
        (t01 / ".unrig.yaml").write_text("version: 1\nprotected_l0_files: []\n", encoding="utf-8")
        passed, lines = verify_workspace(t01)
        results.append(ReplayCaseResult(
            "MUT-T01",
            "Unstaged L0 edit (' M .unrig.yaml') must be blocked",
            not passed and any("L0_TAMPER" in ln for ln in lines),
            "Blocked as expected" if not passed else "ESCAPE: unstaged L0 tamper bypassed!",
        ))

        # 2. MUT-W02: duplicating pre-existing violation inside dirty file via CounterMultiset
        w02 = base / "mut_w02"
        w02.mkdir()
        _init_sandbox(w02, {
            ".unrig.yaml": "version: 1\n",
            "tests/test_legacy.py": "def test_legacy():\n    assert True\n",
        })
        (w02 / "tests/test_legacy.py").write_text(
            "def test_legacy():\n    assert True\n\ndef test_new():\n    assert True\n",
            encoding="utf-8",
        )
        passed, lines = verify_workspace(w02)
        results.append(ReplayCaseResult(
            "MUT-W02",
            "Duplicated identical baseline violation must be caught by CounterMultiset(after - before)",
            not passed and any("TAUTOLOGICAL-ASSERT" in ln for ln in lines),
            "Caught duplicate via multiset delta" if not passed else "ESCAPE: CounterMultiset missed duplicate!",
        ))

        # 3. MUT-W03: nested untracked directory discovered via git status -uall -z
        w03 = base / "mut_w03"
        w03.mkdir()
        _init_sandbox(w03, {".unrig.yaml": "version: 1\n"})
        nested = w03 / "pkg" / "sub" / "test_sneaky.py"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("def test_empty():\n    pass\n", encoding="utf-8")
        passed, lines = verify_workspace(w03)
        results.append(ReplayCaseResult(
            "MUT-W03",
            "Untracked nested empty test stub must be discovered (-uall) and blocked",
            not passed and any("EMPTY-TEST-STUB" in ln for ln in lines),
            "Caught nested untracked stub" if not passed else "ESCAPE: untracked nested dir missed!",
        ))

        # 4. RATCHET-EMPTY-REASON: blank reason in .unrig/ratchet.json MUST fail
        r04 = base / "ratchet_empty"
        r04.mkdir()
        _init_sandbox(r04, {
            ".unrig.yaml": "version: 1\n",
            ".unrig/ratchet.json": json.dumps({
                "version": 1,
                "entries": [{"file": "app.py", "rule": "SILENT-EXCEPT", "count": 1, "reason": "   "}],
            }),
        })
        passed, lines = verify_workspace(r04)
        results.append(ReplayCaseResult(
            "RATCHET-EMPTY-REASON",
            "Ratchet entry with empty reason must fail closed",
            not passed and any("RATCHET_EMPTY_REASON" in ln for ln in lines),
            "Rejected empty reason" if not passed else "ESCAPE: empty ratchet reason accepted!",
        ))

        # 5. JUNIT-STALE: stale JUnit XML older than source mtime MUST fail
        j05 = base / "junit_stale"
        j05.mkdir()
        _init_sandbox(j05, {
            ".unrig.yaml": "version: 1\nreports:\n  require_reports: true\n",
            "app.py": "def add(a, b):\n    return a + b\n",
        })
        rep = j05 / "test-results" / "junit.xml"
        rep.parent.mkdir(parents=True, exist_ok=True)
        rep.write_text(
            '<testsuite name="pytest" tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="tests.test_app" name="test_add"/></testsuite>',
            encoding="utf-8",
        )
        src_mtime = (j05 / "app.py").stat().st_mtime
        import os
        os.utime(rep, (src_mtime - 60, src_mtime - 60))
        passed, lines = verify_workspace(j05)
        results.append(ReplayCaseResult(
            "JUNIT-STALE",
            "Stale JUnit XML older than source file must be rejected",
            not passed and any("REPORT_STALE" in ln for ln in lines),
            "Rejected stale JUnit XML" if not passed else "ESCAPE: stale XML accepted!",
        ))

        # 6. HOOK-BLOCK: PreToolUse hook targeting .unrig/ratchet.json or shell redirect -> exit 2
        hk = base / "hook_sandbox"
        hk.mkdir()
        (hk / ".unrig.yaml").write_text("version: 1\n", encoding="utf-8")
        cfg = load_config(hk)
        code1, msg1 = evaluate_hook_payload(
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": ".unrig/ratchet.json"}}),
            hk,
            cfg,
        )
        code2, msg2 = evaluate_hook_payload(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo {} > .unrig/ratchet.json"}}),
            hk,
            cfg,
        )
        code3, _ = evaluate_hook_payload(
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": "src/service.py"}}),
            hk,
            cfg,
        )
        results.append(ReplayCaseResult(
            "HOOK-L0-BLOCK",
            "PreToolUse hook must return exit code 2 on L0 write/redirect and 0 on normal code",
            code1 == 2 and code2 == 2 and code3 == 0,
            f"codes=({code1}, {code2}, {code3}) msg={msg1 or msg2}",
        ))

    return results
