"""Tests for `.unrig.yaml` parser, JUnit XML freshness/reports, runtime artifacts, and ratchet."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import pytest

from unrig.config import UnrigConfigError, load_config, parse_simple_yaml
from unrig.diff_counter import verify_incremental_diff
from unrig.junit_verifier import parse_junit_xml, verify_reports_and_artifacts


def test_yaml_subset_parser_handles_quotes_comments_and_nested_maps() -> None:
    raw = """
# Top comment
version: 1
protected_l0_files:
  - .unrig.yaml # trailing comment
  - ".unrig/ratchet.json"
reports:
  require_reports: true
  required_tests:
    com.example.AppTest:
      - testAlpha
      - testBeta
diff_checks:
  enabled: true
  forbidden_patterns:
    - id: "NO-FLOAT"
      pattern: "\\\\bfloat\\\\b"
      message: "Hash # inside quotes works cleanly"
      paths: ["src/*.py", "app/**/*.py"]
"""
    parsed = parse_simple_yaml(raw)
    assert parsed["version"] == 1
    assert parsed["protected_l0_files"] == [".unrig.yaml", ".unrig/ratchet.json"]
    assert parsed["reports"]["require_reports"] is True
    assert parsed["reports"]["required_tests"]["com.example.AppTest"] == ["testAlpha", "testBeta"]
    assert parsed["diff_checks"]["forbidden_patterns"][0]["message"] == "Hash # inside quotes works cleanly"
    assert parsed["diff_checks"]["forbidden_patterns"][0]["paths"] == ["src/*.py", "app/**/*.py"]


def test_yaml_rejects_tab_indentation() -> None:
    with pytest.raises(UnrigConfigError):
        parse_simple_yaml("reports:\n\trequire_reports: true\n")


def test_junit_verifier_checks_freshness_skipped_methods_and_artifacts(tmp_path: Path) -> None:
    (tmp_path / ".unrig.yaml").write_text(
        """
version: 1
reports:
  require_reports: true
  forbid_skipped: true
  required_tests:
    tests.test_math:
      - test_add
      - test_sub
  privilege_log_tokens:
    - "--no-verify"
runtime_proof_artifacts:
  - path: artifacts/smoke.json
    max_age_seconds: 120
""",
        encoding="utf-8",
    )
    src_file = tmp_path / "app.py"
    src_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    rep_dir = tmp_path / "test-results"
    rep_dir.mkdir()
    xml_path = rep_dir / "junit.xml"
    xml_path.write_text(
        """<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="0">
    <testcase classname="tests.test_math" name="test_add" time="0.01"/>
    <testcase classname="tests.test_math" name="test_sub" time="0.01"/>
  </testsuite>
</testsuites>""",
        encoding="utf-8",
    )
    art = tmp_path / "artifacts" / "smoke.json"
    art.parent.mkdir()
    art.write_text('{"ok": true}\n', encoding="utf-8")

    # Set source slightly older than reports/artifacts -> PASS
    now = time.time()
    os.utime(src_file, (now - 10, now - 10))
    os.utime(xml_path, (now, now))
    os.utime(art, (now, now))

    cfg = load_config(tmp_path)
    summary = parse_junit_xml(xml_path)
    assert summary.tests == 2
    assert "tests.test_math.test_add" in summary.testcase_names

    findings, count = verify_reports_and_artifacts(tmp_path, cfg)
    assert count == 1
    assert findings == []

    # Mutate XML to have 1 skipped test -> FAIL
    xml_path.write_text(
        """<testsuites>
  <testsuite name="pytest" tests="2" failures="0" errors="0" skipped="1">
    <testcase classname="tests.test_math" name="test_add"/>
    <testcase classname="tests.test_math" name="test_sub"><skipped message="todo"/></testcase>
  </testsuite>
</testsuites>""",
        encoding="utf-8",
    )
    findings, _ = verify_reports_and_artifacts(tmp_path, cfg)
    assert any("TESTS_SKIPPED" in f for f in findings)
    assert any("METHOD_MISSING" in f for f in findings)

    # Restore valid XML but make source newer -> REPORT_STALE + ARTIFACT_STALE
    xml_path.write_text(
        '<testsuite name="tests.test_math" tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.test_math" name="test_add"/>'
        '<testcase classname="tests.test_math" name="test_sub"/></testsuite>',
        encoding="utf-8",
    )
    os.utime(xml_path, (now - 30, now - 30))
    os.utime(art, (now - 30, now - 30))
    os.utime(src_file, (now, now))
    findings, _ = verify_reports_and_artifacts(tmp_path, cfg)
    assert any("REPORT_STALE" in f for f in findings)
    assert any("ARTIFACT_STALE" in f for f in findings)


def test_ratchet_requires_reason_and_blocks_exceeding_cap(tmp_path: Path) -> None:
    (tmp_path / ".unrig.yaml").write_text("version: 1\n", encoding="utf-8")
    ratchet = tmp_path / ".unrig" / "ratchet.json"
    ratchet.parent.mkdir(parents=True)
    ratchet.write_text(
        json.dumps({
            "version": 1,
            "entries": [{"file": "tests/test_a.py", "rule": "TAUTOLOGICAL-ASSERT", "count": 1, "reason": ""}],
        }),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    _, ratchet_errors, _ = verify_incremental_diff(tmp_path, cfg)
    assert any("RATCHET_EMPTY_REASON" in err for err in ratchet_errors)

    # Give explicit reason, cap=1, and test non-git dir with 1 vs 2 occurrences
    ratchet.write_text(
        json.dumps({
            "version": 1,
            "entries": [{
                "file": "tests/test_a.py",
                "rule": "TAUTOLOGICAL-ASSERT",
                "count": 1,
                "reason": "Legacy placeholder tracked for ticket ENG-101 cleanup.",
            }],
        }),
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_a.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    findings, ratchet_errors, _ = verify_incremental_diff(tmp_path, cfg)
    assert ratchet_errors == []
    assert findings == []

    # Now duplicate to 2 -> exceeds cap=1 -> FAIL
    test_file.write_text("def test_one():\n    assert True\n\ndef test_two():\n    assert True\n", encoding="utf-8")
    findings, _, _ = verify_incremental_diff(tmp_path, cfg)
    assert any(f.rule_id == "TAUTOLOGICAL-ASSERT" for f in findings)


def test_ratchet_slack_detection_and_cli_prune(tmp_path: Path) -> None:
    import subprocess, sys
    from unrig.cli_core import verify_workspace
    from unrig.diff_counter import find_ratchet_slack, prune_ratchet

    (tmp_path / ".unrig.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("// @ts-ignore\nexport const a: any = 1;\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("export const clean = 1;\n", encoding="utf-8")
    ratchet = tmp_path / ".unrig" / "ratchet.json"
    ratchet.parent.mkdir(parents=True)
    ratchet.write_text(
        json.dumps({
            "version": 1,
            "entries": [
                {"file": "src/a.ts", "rule": "SUPPRESSION-DIRECTIVE", "count": 3, "reason": "Legacy TS debt."},
                {"file": "src/b.ts", "rule": "SUPPRESSION-DIRECTIVE", "count": 1, "reason": "Cleaned up file."},
            ],
        }),
        encoding="utf-8",
    )

    cfg = load_config(tmp_path)
    slacks, errs = find_ratchet_slack(tmp_path, cfg)
    assert errs == []
    assert len(slacks) == 2
    assert [(s.entry.file, s.actual_count) for s in slacks] == [("src/a.ts", 1), ("src/b.ts", 0)]

    # verify_workspace surfaces [RATCHET_SLACK] warning without crashing
    passed, lines = verify_workspace(tmp_path)
    assert passed
    assert any("RATCHET_SLACK" in ln and "src/a.ts" in ln for ln in lines)

    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_src

    r_check_fail = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "ratchet", "--root", str(tmp_path), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_check_fail.returncode == 1
    assert "RATCHET_SLACK" in r_check_fail.stdout

    r_prune = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "ratchet", "--root", str(tmp_path), "--prune"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_prune.returncode == 0, r_prune.stdout + r_prune.stderr
    updated = json.loads(ratchet.read_text(encoding="utf-8"))
    assert updated["entries"] == [
        {"file": "src/a.ts", "rule": "SUPPRESSION-DIRECTIVE", "count": 1, "reason": "Legacy TS debt."}
    ]

    r_check_ok = subprocess.run(
        [sys.executable, "-m", "unrig.cli", "ratchet", "--root", str(tmp_path), "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r_check_ok.returncode == 0, r_check_ok.stdout
    # Confirm prune_ratchet is idempotent
    slacks_after, _ = prune_ratchet(tmp_path, cfg)
    assert slacks_after == []


def test_junit_child_elements_and_nested_suites_prevent_tamper_and_double_count(tmp_path: Path) -> None:
    from unrig.junit_verifier import parse_junit_xml, verify_reports_and_artifacts

    # 1. Forged top-level skipped="0" / failures="0" cannot hide <testcase><skipped/></testcase>
    forged = tmp_path / "forged-junit.xml"
    forged.write_text(
        '<testsuite name="s" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="a.B" name="test_hidden_skip"><skipped message="no"/></testcase>'
        '</testsuite>',
        encoding="utf-8",
    )
    summary_skip = parse_junit_xml(forged)
    assert summary_skip.skipped == 1
    assert summary_skip.testcase_names == frozenset()
    cfg = load_config(tmp_path)
    findings, _ = verify_reports_and_artifacts(tmp_path, cfg)
    assert any("[TESTS_SKIPPED]" in f for f in findings), findings

    forged.write_text(
        '<testsuite name="s" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="a.B" name="test_hidden_fail"><failure message="boom"/></testcase>'
        '</testsuite>',
        encoding="utf-8",
    )
    summary_fail = parse_junit_xml(forged)
    assert summary_fail.failures == 1
    findings_fail, _ = verify_reports_and_artifacts(tmp_path, cfg)
    assert any("[TESTS_FAILED]" in f for f in findings_fail), findings_fail

    # 2. Nested <testsuite> inside <testsuites> aggregates leaf suites once (no parent double-counting)
    nested = tmp_path / "forged-junit.xml"
    nested.write_text(
        '<testsuites>'
        '<testsuite name="outer" tests="2" failures="0" errors="0" skipped="0">'
        '<testsuite name="inner_a" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="a.B" name="test_one"/>'
        '</testsuite>'
        '<testsuite name="inner_b" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="a.B" name="test_two"/>'
        '</testsuite>'
        '</testsuite>'
        '</testsuites>',
        encoding="utf-8",
    )
    summary_nested = parse_junit_xml(nested)
    assert summary_nested.tests == 2
    assert "test_one" in summary_nested.testcase_names
    assert "test_two" in summary_nested.testcase_names


def test_config_rejects_bad_int_and_invalid_forbidden_regex(tmp_path: Path) -> None:
    from unrig.config import UnrigConfigError, parse_simple_yaml

    # Double-minus string "--1" parses as plain string without ValueError crash
    parsed = parse_simple_yaml("tag: --1\n")
    assert parsed["tag"] == "--1"

    bad_int = tmp_path / ".unrig.yaml"
    bad_int.write_text("version: true\n", encoding="utf-8")
    with pytest.raises(UnrigConfigError, match="version"):
        load_config(tmp_path)

    bad_clone_thr = tmp_path / ".unrig.yaml"
    bad_clone_thr.write_text("diff_checks:\n  clone_warn_threshold: oops\n", encoding="utf-8")
    with pytest.raises(UnrigConfigError, match="clone_warn_threshold"):
        load_config(tmp_path)

    bad_regex = tmp_path / ".unrig.yaml"
    bad_regex.write_text(
        "diff_checks:\n  forbidden_patterns:\n    - id: BAD\n      pattern: \"[unclosed\"\n",
        encoding="utf-8",
    )
    with pytest.raises(UnrigConfigError, match="Invalid regex"):
        load_config(tmp_path)

