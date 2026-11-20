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
