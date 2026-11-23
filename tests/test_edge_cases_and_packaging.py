"""Edge-case coverage: multi-language rules, multi-commit git, renames, JUnit integration, and packaging."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from unrig.cli_core import verify_workspace
from unrig.config import load_config
from unrig.diff_counter import scan_source_defects
from unrig.format_registry import FORMAT_REGISTRY


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(root: Path, files: dict[str, str]) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Unrig Tester")
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def test_python_abstractmethod_exempt_while_concrete_not_implemented_blocked() -> None:
    py_spec = FORMAT_REGISTRY[".py"]
    src = """
from abc import ABC, abstractmethod

class Base(ABC):
    @abstractmethod
    def ok_contract(self):
        raise NotImplementedError

class Concrete:
    def unfinished(self):
        raise NotImplementedError("todo")
"""
    hits = scan_source_defects("src/service.py", src, py_spec)
    lines = [h[0] for h in hits if h[1] == "PLACEHOLDER-STUB"]
    assert len(lines) == 1
    assert lines[0] > 8


def test_python_tautologies_and_bare_except_vs_ignored() -> None:
    py_spec = FORMAT_REGISTRY[".py"]
    src = """
def test_bad():
    assert 1 == 1
    assert not False
    try:
        risky()
    except:
        pass
    try:
        risky2()
    except Exception as ignored:
        pass
"""
    hits = scan_source_defects("tests/test_bad.py", src, py_spec)
    rules = [h[1] for h in hits]
    assert rules.count("TAUTOLOGICAL-ASSERT") == 2
    # Only bare `except: pass` triggers; `except Exception as ignored: pass` is exempt
    assert rules.count("SILENT-EXCEPT") == 1


def test_java_and_js_ts_defect_detection_and_ignored_exemption() -> None:
    java_spec = FORMAT_REGISTRY[".java"]
    java_src = """
class SampleTest {
    @Test
    void emptyCase() {}

    void work() {
        try { run(); } catch (Exception e) {}
        try { run(); } catch (Exception ignored) {}
        assertTrue(true);
        throw new UnsupportedOperationException("stub");
    }
}
"""
    j_hits = [h[1] for h in scan_source_defects("src/test/java/SampleTest.java", java_src, java_spec)]
    assert "EMPTY-TEST-STUB" in j_hits
    assert j_hits.count("SILENT-CATCH") == 1
    assert "TAUTOLOGICAL-ASSERT" in j_hits
    assert "PLACEHOLDER-STUB" in j_hits

    ts_spec = FORMAT_REGISTRY[".ts"]
    ts_src = """
it("empty test", () => {});
expect(true).toBe(true);
try { doWork(); } catch (err) {}
try { doWork(); } catch (ignored) {}
"""
    t_hits = [h[1] for h in scan_source_defects("tests/app.test.ts", ts_src, ts_spec)]
    assert "EMPTY-TEST-STUB" in t_hits
    assert "TAUTOLOGICAL-ASSERT" in t_hits
    assert t_hits.count("SILENT-CATCH") == 1


def test_multi_commit_regression_caught_while_older_debt_stays_isolated(tmp_path: Path) -> None:
    # Commit 0: brownfield repo with legacy untouched file containing existing debt
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\ndiff_checks:\n  baseline: HEAD~1\n",
        "legacy/old.py": "def old():\n    try:\n        x()\n    except Exception:\n        pass\n",
        "app/core.py": "def run():\n    return 42\n",
    })
    # Commit 1 (`HEAD~1`): clean feature commit
    (tmp_path / "app" / "feature.py").write_text("def feat():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "clean baseline")

    # Commit 2 (`HEAD`): agent committed a regression with clean working tree!
    (tmp_path / "app" / "core.py").write_text(
        "def run():\n    try:\n        return 42\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "sloppy committed change")

    passed, lines = verify_workspace(tmp_path)
    assert not passed
    assert any("SILENT-EXCEPT" in ln and "app/core.py" in ln for ln in lines)
    assert not any("legacy/old.py" in ln for ln in lines)


def test_rename_l0_file_and_head_union_lock_prevent_custom_gate_bypass(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\nprotected_l0_files:\n  - .unrig.yaml\n  - scripts/custom_gate.py\n",
        ".unrig/PROTOCOL.md": "# stop\n",
        "scripts/custom_gate.py": "print('gate')\n",
    })

    # Agent edits `.unrig.yaml` to remove `scripts/custom_gate.py` AND tampers `scripts/custom_gate.py`
    (tmp_path / ".unrig.yaml").write_text("version: 1\nprotected_l0_files: []\n", encoding="utf-8")
    (tmp_path / "scripts" / "custom_gate.py").write_text("print('bypassed')\n", encoding="utf-8")

    passed, lines = verify_workspace(tmp_path)
    assert not passed
    assert any("L0_TAMPER" in ln and ".unrig.yaml" in ln for ln in lines)
    assert any("L0_TAMPER" in ln and "scripts/custom_gate.py" in ln for ln in lines)

    # Restore those two, then test `git mv` rename on a protected L0 file
    _git(tmp_path, "checkout", "HEAD", "--", ".unrig.yaml", "scripts/custom_gate.py")
    _git(tmp_path, "mv", ".unrig/PROTOCOL.md", ".unrig/MOVED_PROTOCOL.md")
    passed_mv, lines_mv = verify_workspace(tmp_path)
    assert not passed_mv
    assert any("L0_TAMPER" in ln and ".unrig/PROTOCOL.md" in ln for ln in lines_mv)


def test_real_pytest_junitxml_integration_pass_and_skip_block(tmp_path: Path) -> None:
    (tmp_path / ".unrig.yaml").write_text(
        """version: 1
reports:
  require_reports: true
  forbid_skipped: true
  required_tests:
    tests.test_calc:
      - test_add
      - test_mul
""",
        encoding="utf-8",
    )
    (tmp_path / "calc.py").write_text("def add(a, b): return a + b\ndef mul(a, b): return a * b\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calc.py").write_text(
        "from calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )

    xml_out = tmp_path / "test-results" / "junit.xml"
    r1 = subprocess.run(
        [sys.executable, "-m", "pytest", f"--junitxml={xml_out}"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert r1.returncode == 0, r1.stdout + r1.stderr

    passed, lines = verify_workspace(tmp_path)
    assert passed, lines

    # Skip one required test cleanly via pytest -> real XML now has skipped=1 -> MUST BLOCK
    (tests_dir / "test_calc.py").write_text(
        "import pytest\nfrom calc import add, mul\n\ndef test_add():\n    assert add(1, 2) == 3\n\n@pytest.mark.skip(reason='lazy')\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    r2 = subprocess.run(
        [sys.executable, "-m", "pytest", f"--junitxml={xml_out}"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert r2.returncode == 0
    passed_skip, lines_skip = verify_workspace(tmp_path)
    assert not passed_skip
    assert any("TESTS_SKIPPED" in ln for ln in lines_skip)
    assert any("METHOD_MISSING" in ln for ln in lines_skip)


def test_escape_replay_demo_script_succeeds() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    demo = repo_root / "examples" / "escape-replay" / "run_demo.py"
    res = subprocess.run([sys.executable, str(demo)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[BLOCKED CLEANLY]" in res.stdout


def test_installed_package_ships_templates_and_runs_without_src_pythonpath(tmp_path: Path) -> None:
    assert importlib.util.find_spec("setuptools") is not None
    assert importlib.util.find_spec("pip") is not None

    repo_root = Path(__file__).resolve().parents[1]
    install_dir = tmp_path / "pkg_target"
    sandbox_repo = tmp_path / "sandbox_app"
    sandbox_repo.mkdir()
    _init_repo(sandbox_repo, {"README.md": "# toy\n"})

    r_install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-build-isolation",
            "--no-deps",
            "--target",
            str(install_dir),
            str(repo_root),
        ],
        capture_output=True,
        text=True,
    )
    assert r_install.returncode == 0, r_install.stdout + r_install.stderr

    # Run from sandbox directory using ONLY installed package target (strip PYTHONPATH pointing to source)
    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    clean_env["PYTHONPATH"] = str(install_dir)
    unrig_bin = install_dir / "bin" / ("unrig.exe" if os.name == "nt" else "unrig")

    cmd_prefix = [str(unrig_bin)] if unrig_bin.exists() else [sys.executable, "-m", "unrig.cli"]

    r_init = subprocess.run(
        [*cmd_prefix, "init", "--root", str(sandbox_repo)],
        cwd=str(sandbox_repo),
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert r_init.returncode == 0, r_init.stdout + r_init.stderr
    assert (sandbox_repo / ".unrig.yaml").exists()
    assert (sandbox_repo / ".unrig" / "PROTOCOL.md").exists()
    assert (sandbox_repo / ".unrig" / "blind_auditor_prompt.md").exists()
    assert (sandbox_repo / ".unrig" / "ratchet.json").exists()

    _git(sandbox_repo, "add", ".")
    _git(sandbox_repo, "commit", "-m", "init unrig")
    r_verify = subprocess.run(
        [*cmd_prefix, "verify", "--root", str(sandbox_repo), "--strict-l0"],
        cwd=str(sandbox_repo),
        env=clean_env,
        capture_output=True,
        text=True,
    )
    assert r_verify.returncode == 0, r_verify.stdout + r_verify.stderr
    assert "[PASS]" in r_verify.stdout


def test_js_ts_suppression_directives_brownfield_isolation_and_ratchet(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        ".unrig/ratchet.json": json.dumps({
            "version": 1,
            "entries": [{
                "file": "src/ratcheted.ts",
                "rule": "SUPPRESSION-DIRECTIVE",
                "count": 1,
                "reason": "Temporary interop cast tracked in TICKET-42.",
            }],
        }),
        "src/legacy.ts": "// eslint-disable-next-line no-console\nconsole.log('existing');\n",
        "src/ratcheted.ts": "// @ts-ignore\nexport const legacyAny: any = 1;\n",
        "src/clean.ts": "export const ok = 1;\n",
        "tests/app.test.ts": "it('adds', () => { expect(1 + 1).toBe(2); });\n",
    })

    # Clean commit with justified @ts-expect-error and ignored typed catch must pass
    (tmp_path / "src/clean.ts").write_text(
        "// @ts-expect-error: verifying invalid runtime shape rejection\n"
        "export const badArg = 1;\n"
        "try { badArg; } catch (ignored: unknown) {}\n",
        encoding="utf-8",
    )
    passed_clean, lines_clean = verify_workspace(tmp_path)
    assert passed_clean, lines_clean

    # Introducing net-new suppressions, paramless catch, and skipped test must fail cleanly
    (tmp_path / "src/clean.ts").write_text(
        "/* eslint @typescript-eslint/no-explicit-any: \"off\" */\n"
        "// @ts-ignore\n"
        "export const re = /\\/\\*/;\n"
        "try { re; } catch {}\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/app.test.ts").write_text(
        "it.skip('broken', () => { expect(1).toBe(1); });\n",
        encoding="utf-8",
    )
    # Also duplicate the grandfathered suppression in ratcheted.ts beyond cap=1 -> must fail
    (tmp_path / "src/ratcheted.ts").write_text(
        "// @ts-ignore\nexport const a: any = 1;\n// @ts-ignore\nexport const b: any = 2;\n",
        encoding="utf-8",
    )

    passed_bad, lines_bad = verify_workspace(tmp_path)
    assert not passed_bad
    assert any("SUPPRESSION-DIRECTIVE" in ln and "src/clean.ts:1" in ln for ln in lines_bad)
    assert any("SUPPRESSION-DIRECTIVE" in ln and "src/clean.ts:2" in ln for ln in lines_bad)
    assert any("SILENT-CATCH" in ln and "src/clean.ts:4" in ln for ln in lines_bad)
    assert any("EMPTY-TEST-STUB" in ln and "tests/app.test.ts:1" in ln for ln in lines_bad)
    assert any("SUPPRESSION-DIRECTIVE" in ln and "src/ratcheted.ts" in ln for ln in lines_bad)
    assert not any("src/legacy.ts" in ln for ln in lines_bad)


def test_hook_cli_handles_utf8_non_ascii_payload_on_stdin(tmp_path: Path) -> None:
    _init_repo(tmp_path, {".unrig.yaml": "version: 1\n", "app.vue": "<template/>\n"})
    payload_bytes = json.dumps(
        {
            "agent_action_name": "pre_run_command",
            "tool_info": {
                "command_line": "rg -n \"工作台|错题\" apps/web-app/src/App.vue",
                "cwd": str(tmp_path),
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    res = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "hook", "--client", "antigravity", "--root", str(tmp_path)],
        input=payload_bytes,
        env=env,
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", errors="replace")


def test_ts_type_escape_as_any_and_double_cast_blocked(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        "src/legacy.ts": "export const legacyCast = ({} as unknown as { ok: boolean });\n",
        "src/app.ts": "export const label = 'treat as any inside string';\n",
    })

    # Legitimate single-step concrete narrowing must pass
    (tmp_path / "src/app.ts").write_text(
        "export const label = 'treat as any and as unknown as inside string';\n"
        "export function asEl(node: EventTarget | null): HTMLInputElement | null {\n"
        "  return node as HTMLInputElement | null;\n"
        "}\n",
        encoding="utf-8",
    )
    ok_clean, lines_clean = verify_workspace(tmp_path)
    assert ok_clean, lines_clean

    # Net-new `as any` and `as unknown as` must be blocked
    (tmp_path / "src/app.ts").write_text(
        "export const a = ({} as any).x;\n"
        "export const b = ({} as unknown as { y: number }).y;\n",
        encoding="utf-8",
    )
    ok_bad, lines_bad = verify_workspace(tmp_path)
    assert not ok_bad
    assert any("TYPE-ESCAPE" in ln and "src/app.ts:1" in ln for ln in lines_bad)
    assert any("TYPE-ESCAPE" in ln and "src/app.ts:2" in ln for ln in lines_bad)
    assert not any("src/legacy.ts" in ln for ln in lines_bad)


def test_dummy_constant_return_python_and_ts_evasion_vs_benign(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        "src/stubs.py": (
            "from abc import ABC, abstractmethod\n\n"
            "class BaseHandler(ABC):\n"
            "    @abstractmethod\n"
            "    def check(self, item: str) -> bool:\n"
            "        return False\n\n"
            "def default_opt(_ctx: object) -> bool:\n"
            "    return True\n\n"
            "def noop_none(msg: str) -> None:\n"
            "    return None\n"
        ),
        "src/stubs.ts": (
            "export function onDispose(resource: unknown): null {\n"
            "  return null;\n"
            "}\n"
        ),
    })

    # Benign updates only -> PASS
    (tmp_path / "src/real.py").write_text(
        "def verify_sig(sig: str) -> bool:\n    return len(sig) > 0\n",
        encoding="utf-8",
    )
    ok_clean, lines_clean = verify_workspace(tmp_path)
    assert ok_clean, lines_clean

    # Parameter-ignoring constant stubs (direct + local-var alias evasion) in Python & TS -> FAIL
    (tmp_path / "src/fake.py").write_text(
        "def verify_auth(token: str) -> bool:\n"
        "    _ = token\n"
        "    flag = True\n"
        "    return flag\n",
        encoding="utf-8",
    )
    (tmp_path / "src/fake.ts").write_text(
        "export function validatePayload(input: Record<string, unknown>): boolean {\n"
        "  const ok = false;\n"
        "  return ok;\n"
        "}\n",
        encoding="utf-8",
    )
    ok_bad, lines_bad = verify_workspace(tmp_path)
    assert not ok_bad
    assert any("DUMMY-CONSTANT-RETURN" in ln and "src/fake.py" in ln for ln in lines_bad)
    assert any("DUMMY-CONSTANT-RETURN" in ln and "src/fake.ts" in ln for ln in lines_bad)
    assert not any("src/stubs.py" in ln for ln in lines_bad)


def test_clone_spam_warns_at_two_blocks_at_three_and_allows_refactor_or_ratchet(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        "tests/legacy_clones.py": (
            "def test_legacy_alpha():\n"
            "    payload = {'kind': 'legacy_a', 'score': 10}\n"
            "    assert payload['score'] >= 10\n"
            "    assert payload['kind'].startswith('legacy_')\n\n"
            "def test_legacy_beta():\n"
            "    payload = {'kind': 'legacy_b', 'score': 20}\n"
            "    assert payload['score'] >= 10\n"
            "    assert payload['kind'].startswith('legacy_')\n\n"
            "def test_legacy_gamma():\n"
            "    payload = {'kind': 'legacy_c', 'score': 30}\n"
            "    assert payload['score'] >= 10\n"
            "    assert payload['kind'].startswith('legacy_')\n"
        ),
    })

    # 2 net-new isomorphic blocks -> PASS with [WARN] [CLONE-SPAM]
    (tmp_path / "tests/test_clones.py").write_text(
        "def test_case_one():\n"
        "    record = {'name': 'alpha', 'limit': 100}\n"
        "    assert record['limit'] == 100\n"
        "    assert record['name'].isalpha()\n\n"
        "def test_case_two():\n"
        "    record = {'name': 'beta', 'limit': 200}\n"
        "    assert record['limit'] == 100\n"
        "    assert record['name'].isalpha()\n",
        encoding="utf-8",
    )
    ok_warn, lines_warn = verify_workspace(tmp_path)
    assert ok_warn, lines_warn
    assert any("[WARN] [CLONE-SPAM]" in ln for ln in lines_warn), lines_warn

    # 3 net-new isomorphic blocks across Python -> FAIL with [CLONE-SPAM]
    (tmp_path / "tests/test_clones.py").write_text(
        "def test_case_one():\n"
        "    record = {'name': 'alpha', 'limit': 100}\n"
        "    assert record['limit'] == 100\n"
        "    assert record['name'].isalpha()\n\n"
        "def test_case_two():\n"
        "    record = {'name': 'beta', 'limit': 200}\n"
        "    assert record['limit'] == 100\n"
        "    assert record['name'].isalpha()\n\n"
        "def test_case_three():\n"
        "    record = {'name': 'gamma', 'limit': 300}\n"
        "    assert record['limit'] == 100\n"
        "    assert record['name'].isalpha()\n",
        encoding="utf-8",
    )
    ok_fail, lines_fail = verify_workspace(tmp_path)
    assert not ok_fail
    assert any("CLONE-SPAM" in ln for ln in lines_fail), lines_fail

    # Approved ratchet entry for CLONE-SPAM with count 3 unblocks cleanly
    (tmp_path / ".unrig").mkdir(exist_ok=True)
    sig_line = next(ln for ln in lines_fail if "CLONE-SPAM" in ln and "clone:" in ln)
    clone_sig = "clone:" + sig_line.split("clone:")[1].split("`")[0].split()[0]
    (tmp_path / ".unrig/ratchet.json").write_text(
        json.dumps({
            "version": 1,
            "entries": [
                {
                    "file": "tests/test_clones.py",
                    "rule": "CLONE-SPAM",
                    "signature": clone_sig,
                    "count": 3,
                    "reason": "Legacy boundary fixture parity pending shared table helper",
                }
            ],
        }),
        encoding="utf-8",
    )
    ok_ratchet, lines_ratchet = verify_workspace(tmp_path)
    assert ok_ratchet, lines_ratchet
    assert not any("RATCHET_SLACK" in ln for ln in lines_ratchet), lines_ratchet

    from unrig.diff_counter import find_ratchet_slack, prune_ratchet

    slacks_active, errs_active = find_ratchet_slack(tmp_path, load_config(tmp_path))
    assert errs_active == []
    assert slacks_active == []

    # Refactor down to @pytest.mark.parametrize while keeping ratchet -> slack detected & pruned cleanly
    (tmp_path / "tests/test_clones.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.parametrize('name,limit', [('alpha', 100), ('beta', 200), ('gamma', 300)])\n"
        "def test_cases(name: str, limit: int) -> None:\n"
        "    record = {'name': name, 'limit': limit}\n"
        "    assert record['limit'] >= 100\n"
        "    assert record['name'].isalpha()\n",
        encoding="utf-8",
    )
    pruned, prune_errs = prune_ratchet(tmp_path, load_config(tmp_path))
    assert prune_errs == []
    assert len(pruned) == 1
    assert json.loads((tmp_path / ".unrig/ratchet.json").read_text(encoding="utf-8"))["entries"] == []

    ok_refactored, lines_refactored = verify_workspace(tmp_path)
    assert ok_refactored, lines_refactored


def test_clone_spam_typescript_it_blocks_and_coverage_transparency(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        "src/base.py": "def ok(x: int) -> int:\n    return x + 1\n",
    })
    # Add an ignored directory file (skipped_ignored) + syntax fallback file (broken Python)
    (tmp_path / "node_modules/pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_modules/pkg/index.js").write_text("export const ignored = 1;\n", encoding="utf-8")
    (tmp_path / "src/syntax_broken.py").write_text("def broken(:\n    assert 1 == 1\n", encoding="utf-8")
    # Simulate shallow clone metadata with unavailable HEAD~1
    (tmp_path / ".git/shallow").write_text("0000000000000000000000000000000000000000\n", encoding="utf-8")

    ok_meta, lines_meta = verify_workspace(tmp_path)
    assert ok_meta, lines_meta
    joined = "\n".join(lines_meta)
    assert "[WARN] [SHALLOW_HISTORY]" in joined
    assert "skipped_ignored=1" in joined
    assert "syntax_fallback=1" in joined

    # Now add 3 isomorphic TS `it(...)` blocks -> FAIL with CLONE-SPAM
    (tmp_path / "src/syntax_broken.py").unlink()
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests/spec.test.ts").write_text(
        "it('handles first case', () => {\n"
        "  const cfg = { role: 'reader', level: 1 };\n"
        "  expect(cfg.level).toBe(1);\n"
        "  expect(cfg.role.length).toBeGreaterThan(2);\n"
        "});\n"
        "it('handles second case', () => {\n"
        "  const cfg = { role: 'writer', level: 2 };\n"
        "  expect(cfg.level).toBe(1);\n"
        "  expect(cfg.role.length).toBeGreaterThan(2);\n"
        "});\n"
        "it('handles third case', () => {\n"
        "  const cfg = { role: 'admin', level: 3 };\n"
        "  expect(cfg.level).toBe(1);\n"
        "  expect(cfg.role.length).toBeGreaterThan(2);\n"
        "});\n",
        encoding="utf-8",
    )
    ok_ts_fail, lines_ts_fail = verify_workspace(tmp_path)
    assert not ok_ts_fail
    assert any("CLONE-SPAM" in ln for ln in lines_ts_fail), lines_ts_fail

    # Refactoring to `it.each` passes cleanly
    (tmp_path / "tests/spec.test.ts").write_text(
        "it.each([\n"
        "  ['reader', 1],\n"
        "  ['writer', 2],\n"
        "  ['admin', 3],\n"
        "])('handles %s', (role, level) => {\n"
        "  const cfg = { role, level };\n"
        "  expect(cfg.level).toBeGreaterThan(0);\n"
        "  expect(cfg.role.length).toBeGreaterThan(2);\n"
        "});\n",
        encoding="utf-8",
    )
    ok_ts_pass, lines_ts_pass = verify_workspace(tmp_path)
    assert ok_ts_pass, lines_ts_pass


def test_java_dummy_constant_return_and_clone_spam(tmp_path: Path) -> None:
    _init_repo(tmp_path, {
        ".unrig.yaml": "version: 1\n",
        "src/main/java/demo/Spi.java": (
            "package demo;\n"
            "public interface Spi {\n"
            "    default boolean supports(String kind) {\n"
            "        return false;\n"
            "    }\n"
            "}\n"
        ),
    })

    # Clean concrete Java service passes
    (tmp_path / "src/main/java/demo/RealService.java").write_text(
        "package demo;\n"
        "public class RealService {\n"
        "    private int count;\n"
        "    public int getCount() { return this.count; }\n"
        "    public boolean isAllowed(String role) {\n"
        "        return \"admin\".equals(role);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    ok_clean, lines_clean = verify_workspace(tmp_path)
    assert ok_clean, lines_clean

    # Parameter-ignoring constant-return stubs (direct + local-var alias) fail with DUMMY-CONSTANT-RETURN
    (tmp_path / "src/main/java/demo/FakeService.java").write_text(
        "package demo;\n"
        "import java.util.*;\n"
        "public class FakeService {\n"
        "    public List<String> getAnalystsByDept(String dept) {\n"
        "        return Collections.emptyList();\n"
        "    }\n"
        "    public boolean verifyPermit(String token) {\n"
        "        boolean ok = true;\n"
        "        return ok;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    ok_dummy, lines_dummy = verify_workspace(tmp_path)
    assert not ok_dummy
    assert any("DUMMY-CONSTANT-RETURN" in ln and "getAnalystsByDept" in ln for ln in lines_dummy), lines_dummy
    assert any("DUMMY-CONSTANT-RETURN" in ln and "verifyPermit" in ln for ln in lines_dummy), lines_dummy

    # Fix dummy stubs, add 3 isomorphic Java service methods across files -> blocked by CLONE-SPAM
    (tmp_path / "src/main/java/demo/FakeService.java").unlink()
    for idx, mod in enumerate(("Alpha", "Beta", "Gamma"), start=1):
        (tmp_path / f"src/main/java/demo/{mod}Controller.java").write_text(
            f"package demo;\npublic class {mod}Controller {{\n"
            f"    public String export{mod}(String filter, int pageSize) {{\n"
            f"        if (filter == null || pageSize <= 0) {{\n"
            f"            throw new IllegalArgumentException(\"bad-{idx}\");\n"
            f"        }}\n"
            f"        String key = filter.trim().toLowerCase();\n"
            f"        return key + \":{idx}\";\n"
            f"    }}\n"
            f"}}\n",
            encoding="utf-8",
        )
    ok_clone_fail, lines_clone_fail = verify_workspace(tmp_path)
    assert not ok_clone_fail
    assert any("CLONE-SPAM" in ln for ln in lines_clone_fail), lines_clone_fail

    # Consolidate into 1 shared helper -> passes cleanly
    for mod in ("Alpha", "Beta", "Gamma"):
        (tmp_path / f"src/main/java/demo/{mod}Controller.java").unlink()
    (tmp_path / "src/main/java/demo/SharedExportHelper.java").write_text(
        "package demo;\npublic class SharedExportHelper {\n"
        "    public static String exportEntity(String filter, int pageSize, int tag) {\n"
        "        if (filter == null || pageSize <= 0) {\n"
        "            throw new IllegalArgumentException(\"bad-\" + tag);\n"
        "        }\n"
        "        String key = filter.trim().toLowerCase();\n"
        "        return key + \":\" + tag;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    ok_clone_pass, lines_clone_pass = verify_workspace(tmp_path)
    assert ok_clone_pass, lines_clone_pass




