"""Referee self-mutation, escape-replay, hook blocking, and end-to-end CLI tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from unrig.cli_core import init_workspace, verify_workspace
from unrig.config import load_config
from unrig.l0_guard import compute_normalized_sha256, evaluate_hook_payload, parse_git_status_z
from unrig.replay import run_builtin_replay_suite


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def test_parse_git_status_z_preserves_leading_space_on_unstaged_mod() -> None:
    raw = b" M .unrig.yaml\x00?? pkg/sub/test_new.py\x00R  new.py\x00old.py\x00"
    parsed = parse_git_status_z(raw)
    assert (" M", ".unrig.yaml") in parsed
    assert ("??", "pkg/sub/test_new.py") in parsed
    assert ("R ", "new.py") in parsed
    assert ("R ", "old.py") in parsed


def test_crlf_and_lf_have_identical_normalized_sha256(tmp_path: Path) -> None:
    f_lf = tmp_path / "lf.txt"
    f_crlf = tmp_path / "crlf.txt"
    f_lf.write_bytes(b"line1\nline2\n")
    f_crlf.write_bytes(b"line1\r\nline2\r\n")
    assert compute_normalized_sha256(f_lf) == compute_normalized_sha256(f_crlf)


def test_builtin_replay_suite_all_cases_pass() -> None:
    results = run_builtin_replay_suite()
    assert len(results) >= 6
    failures = [r for r in results if not r.passed]
    assert failures == [], f"Replay failures: {failures}"


def test_mut_t02_staged_l0_edit_and_custom_forbidden_rule(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    (tmp_path / ".unrig.yaml").write_text(
        """version: 1
protected_l0_files:
  - .unrig.yaml
  - .unrig/ratchet.json
diff_checks:
  enabled: true
  forbidden_patterns:
    - id: "NO-FLOAT-PRICE"
      pattern: "\\bfloat\\("
      message: "Use Decimal instead of float() for pricing."
      paths: ["billing/*.py"]
""",
        encoding="utf-8",
    )
    (tmp_path / ".unrig").mkdir()
    (tmp_path / ".unrig" / "ratchet.json").write_text('{"version": 1, "entries": []}\n', encoding="utf-8")
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "calc.py").write_text("from decimal import Decimal\n\ndef tax(v: Decimal) -> Decimal:\n    return v * Decimal('0.1')\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")

    passed, lines = verify_workspace(tmp_path)
    assert passed, lines

    # Stage an L0 edit -> MUT-T02 MUST fail
    (tmp_path / ".unrig" / "ratchet.json").write_text('{"version": 1, "entries": [{"file": "*", "rule": "*", "count": 99, "reason": "hack"}]}\n', encoding="utf-8")
    _git(tmp_path, "add", ".unrig/ratchet.json")
    passed, lines = verify_workspace(tmp_path)
    assert not passed
    assert any("L0_TAMPER" in ln for ln in lines)

    # Restore L0, trigger custom team forbidden_patterns rule
    _git(tmp_path, "checkout", "HEAD", "--", ".unrig/ratchet.json")
    (tmp_path / "billing" / "calc.py").write_text("def tax(v):\n    return float(v) * 0.1\n", encoding="utf-8")
    passed, lines = verify_workspace(tmp_path)
    assert not passed
    assert any("NO-FLOAT-PRICE" in ln for ln in lines)


def test_hook_blocks_path_traversal_backslashes_and_noverify(tmp_path: Path) -> None:
    (tmp_path / ".unrig.yaml").write_text("version: 1\n", encoding="utf-8")
    cfg = load_config(tmp_path)

    for payload in (
        {"tool_name": "Edit", "tool_input": {"file_path": "src/../.unrig/ratchet.json"}},
        {"tool_name": "Write", "tool_input": {"TargetFile": ".unrig\\PROTOCOL.md"}},
        {"tool_name": "Bash", "tool_input": {"command": "git commit -m 'msg' --no-verify"}},
        {"tool_name": "Bash", "tool_input": {"command": "cat tmp.yaml > .unrig.yaml"}},
    ):
        code, msg = evaluate_hook_payload(json.dumps(payload), tmp_path, cfg)
        assert code == 2, f"Expected exit 2 for {payload}, got {code}"
        assert "BLOCKED" in msg

    code_ok, _ = evaluate_hook_payload(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
        tmp_path,
        cfg,
    )
    assert code_ok == 0


def test_cli_end_to_end_init_verify_hook_and_replay(tmp_path: Path) -> None:
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    r_init = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "init", "--root", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_init.returncode == 0, r_init.stderr
    assert (tmp_path / ".unrig.yaml").exists()
    assert (tmp_path / ".unrig" / "PROTOCOL.md").exists()
    assert (tmp_path / ".unrig" / "ratchet.json").exists()
    assert (tmp_path / ".unrig" / "blind_auditor_prompt.md").exists()

    # Commit initialized L0 files cleanly, then verify strict-l0 succeeds
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init unrig")
    r_verify = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "verify", "--root", str(tmp_path), "--strict-l0"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_verify.returncode == 0, r_verify.stdout + r_verify.stderr
    assert "[PASS]" in r_verify.stdout

    # Hook CLI returns exit code 2 via subprocess stdin
    r_hook = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook", "--root", str(tmp_path)],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": ".unrig.yaml"}}),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_hook.returncode == 2
    assert "BLOCKED" in r_hook.stderr

    # Replay CLI succeeds end-to-end
    r_replay = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "replay"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_replay.returncode == 0, r_replay.stdout + r_replay.stderr
