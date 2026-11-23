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
    assert len(results) >= 9
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


def test_multi_client_init_and_hook_adapters(tmp_path: Path) -> None:
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    r_init = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "init", "--root", str(tmp_path), "--client", "all"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_init.returncode == 0, r_init.stderr
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".agents" / "hooks.json").exists()
    assert (tmp_path / ".gemini" / "settings.json").exists()

    agy_cfg = json.loads((tmp_path / ".agents" / "hooks.json").read_text(encoding="utf-8"))
    assert "PreToolUse" in agy_cfg["unrig-guard"]
    assert "Stop" in agy_cfg["unrig-guard"]

    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init with all client hooks")

    # Simulate Antigravity launching hook from `.agents/` subdirectory WITHOUT `--root`
    agents_dir = tmp_path / ".agents"
    r_agy_deny = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(agents_dir),
        input=json.dumps({
            "hookEventName": "PreToolUse",
            "cwd": str(tmp_path),
            "toolCall": {"id": "c1", "name": "edit_file", "args": {"Path": ".unrig.yaml"}},
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_agy_deny.returncode == 0
    assert json.loads(r_agy_deny.stdout)["decision"] == "deny"

    # Read-only tool (`view_file`) on `.unrig.yaml` must be allowed
    r_agy_read = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(agents_dir),
        input=json.dumps({
            "hookEventName": "PreToolUse",
            "workspacePaths": [str(tmp_path)],
            "toolCall": {"id": "c2", "name": "view_file", "args": {"AbsolutePath": str(tmp_path / ".unrig.yaml")}},
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_agy_read.returncode == 0
    assert json.loads(r_agy_read.stdout)["decision"] == "allow"

    # Gemini CLI BeforeTool JSON adapter
    r_gem_deny = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(tmp_path),
        input=json.dumps({
            "hook_event_name": "BeforeTool",
            "cwd": str(tmp_path),
            "tool_name": "write_file",
            "tool_input": {"file_path": ".unrig/PROTOCOL.md"},
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_gem_deny.returncode == 0
    assert json.loads(r_gem_deny.stdout)["decision"] == "deny"

    # Antigravity Stop hook: clean -> {}, dirty -> continue once, continuationActive -> {}
    r_stop_clean = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(agents_dir),
        input=json.dumps({
            "hookEventName": "Stop",
            "workspacePaths": [str(tmp_path)],
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_stop_clean.returncode == 0
    assert json.loads(r_stop_clean.stdout) == {}

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    r_stop_dirty = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(agents_dir),
        input=json.dumps({
            "hookEventName": "Stop",
            "workspacePaths": [str(tmp_path)],
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": False,
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_stop_dirty.returncode == 0
    stop_res = json.loads(r_stop_dirty.stdout)
    assert stop_res["decision"] == "continue"
    assert "TAUTOLOGICAL-ASSERT" in stop_res["reason"]

    # Second bounce in same turn (continuationActive=True) or USER_CANCELED must allow stop
    r_stop_loop_guard = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        cwd=str(agents_dir),
        input=json.dumps({
            "hookEventName": "Stop",
            "workspacePaths": [str(tmp_path)],
            "terminationReason": "NO_TOOL_CALL",
            "continuationActive": True,
        }),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_stop_loop_guard.returncode == 0
    assert json.loads(r_stop_loop_guard.stdout) == {}


def test_local_interception_log_stats_and_git_cleanliness(tmp_path: Path) -> None:
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    subprocess.run([sys.executable, "-m", "unrig.cli", "init", "--root", str(tmp_path)], env=env, check=True, capture_output=True)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")

    # Passing verify writes zero events
    r_pass = subprocess.run([sys.executable, "-m", "unrig.cli", "verify", "--root", str(tmp_path)], env=env, capture_output=True, text=True)
    assert r_pass.returncode == 0
    assert not (tmp_path / ".unrig" / ".cache" / "events.jsonl").exists()

    # 1. Claude hook deny
    subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook", "--root", str(tmp_path)],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": ".unrig.yaml"}}),
        env=env,
        capture_output=True,
        text=True,
    )
    # 2. Verify block on dirty test stub
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_stub():\n    pass\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "unrig.cli", "verify", "--root", str(tmp_path)], env=env, capture_output=True, text=True)

    # `.unrig/.cache/` must self-ignore in git even when repo has no root `.gitignore`
    gs = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=str(tmp_path), capture_output=True, text=True, check=True)
    assert ".unrig/.cache" not in gs.stdout

    r_stats = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "stats", "--root", str(tmp_path), "--days", "7"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_stats.returncode == 0
    assert "Total intercepted: 2" in r_stats.stdout
    assert "L0_HOOK_BLOCK" in r_stats.stdout
    assert "EMPTY-TEST-STUB" in r_stats.stdout

    r_clear = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "stats", "--root", str(tmp_path), "--clear"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_clear.returncode == 0
    assert not (tmp_path / ".unrig" / ".cache" / "events.jsonl").exists()


def test_antigravity_stop_hook_releases_after_identical_bounce_limit(tmp_path: Path) -> None:
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    subprocess.run(
        [sys.executable, "-m", "unrig.cli", "init", "--root", str(tmp_path), "--client", "antigravity"],
        env=env,
        check=True,
        capture_output=True,
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")

    # Introduce a blocking defect
    (tmp_path / "tests" / "test_bad.py").write_text("def test_bad():\n    assert True\n", encoding="utf-8")

    stop_payload = json.dumps({
        "hookEventName": "Stop",
        "conversationId": "conv-stop-loop-1",
        "workspacePaths": [str(tmp_path)],
        "terminationReason": "NO_TOOL_CALL",
        "continuationActive": False,
    })

    # First 2 identical Stop triggers bounce with continue
    for _ in range(2):
        r = subprocess.run(
            [sys.executable, "-m", "unrig.cli", "hook"],
            input=stop_payload,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0
        assert json.loads(r.stdout)["decision"] == "continue"

    # 3rd identical Stop trigger releases for human escalation and logs escalated
    r_release = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        input=stop_payload,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_release.returncode == 0
    assert json.loads(r_release.stdout) == {}

    r_stats = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "stats", "--root", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert "escalated" in r_stats.stdout

    # Once clean, Stop resets bounce counter
    (tmp_path / "tests" / "test_bad.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    r_clean = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook"],
        input=stop_payload,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_clean.returncode == 0
    assert json.loads(r_clean.stdout) == {}


def test_hook_blocks_noverify_evasion_variants_and_protects_client_hook_files(tmp_path: Path) -> None:
    from unrig.config import load_config
    from unrig.l0_guard import evaluate_hook_payload

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")

    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src
    subprocess.run(
        [sys.executable, "-m", "unrig.cli", "init", "--root", str(tmp_path), "--client", "all"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    cfg = load_config(tmp_path)

    # Blocked bypass variants
    for bad_cmd in (
        "git -C . commit --no-verify -m x",
        "git -c user.name=bot commit -n -m x",
        'git commit -nm "bypass"',
        "git push --no-verify origin main",
    ):
        code, msg = evaluate_hook_payload(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": bad_cmd}}),
            tmp_path,
            cfg,
        )
        assert code == 2, f"Expected block for {bad_cmd!r}, got {code}"
        assert "--no-verify" in msg

    # Benign commands must NOT false-positive (quoted -n text & git push -n dry-run)
    for ok_cmd in (
        'git commit -m "fix -n edge case in cli"',
        "git push -n origin main",
    ):
        code, msg = evaluate_hook_payload(
            json.dumps({"tool_name": "Bash", "tool_input": {"command": ok_cmd}}),
            tmp_path,
            cfg,
        )
        assert code == 0, f"Unexpected block for {ok_cmd!r}: {msg}"

    # All 3 client hook config files are protected L0 targets even without explicit .unrig.yaml listing
    for hook_path in (".agents/hooks.json", ".claude/settings.json", ".gemini/settings.json"):
        code, msg = evaluate_hook_payload(
            json.dumps({"tool_name": "Write", "tool_input": {"file_path": hook_path}}),
            tmp_path,
            cfg,
        )
        assert code == 2, f"Expected hook protection for {hook_path}"
        assert hook_path in msg


