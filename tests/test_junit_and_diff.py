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
