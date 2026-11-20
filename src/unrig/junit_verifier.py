"""Universal JUnit XML & runtime proof artifact verifier (Maven, pytest, vitest, Go)."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from .config import RuntimeArtifactRule, UnrigConfig, normalize_rel_path
from .format_registry import FORMAT_REGISTRY


@dataclass(frozen=True)
class JUnitReportSummary:
    path: Path
    tests: int
    failures: int
    errors: int
    skipped: int
    suite_names: tuple[str, ...]
    testcase_names: frozenset[str]


_EXCLUDED_DIR_PARTS = {
    ".git",
    ".unrig",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "build",
    "dist",
    "__pycache__",
    ".pytest_cache",
}


def _iter_junit_xml_paths(repo_root: Path, globs: tuple[str, ...]) -> list[Path]:
    found: dict[Path, None] = {}
    for pattern in globs:
        clean = normalize_rel_path(pattern)
        for candidate in repo_root.glob(clean):
            if candidate.is_file():
                found[candidate.resolve()] = None
    return sorted(found.keys())


def _is_ignored_artifact(rel_path: str, ignored_globs: tuple[str, ...]) -> bool:
    norm = rel_path.replace("\\", "/")
    for pat in ignored_globs:
        clean = normalize_rel_path(pat)
        if fnmatch(norm, clean) or (clean.startswith("**/") and fnmatch(norm, clean[3:])):
            return True
    return False


def compute_max_source_mtime(
    repo_root: Path,
    config: UnrigConfig,
    report_paths: set[Path],
) -> tuple[float, str]:
    """Return `(max_mtime, latest_rel_path)` across tracked/unignored source files."""
    max_ts = 0.0
    latest_rel = ""
    artifact_paths = {(repo_root / a.path).resolve() for a in config.runtime_proof_artifacts}

    if config.reports.require_fresh_against:
        candidates: list[Path] = []
        for pat in config.reports.require_fresh_against:
            candidates.extend(p for p in repo_root.glob(normalize_rel_path(pat)) if p.is_file())
    else:
        candidates = []
        for p in repo_root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(repo_root)
            if any(part in _EXCLUDED_DIR_PARTS for part in rel.parts[:-1]):
                continue
            if p.suffix.lower() not in FORMAT_REGISTRY:
                continue
            candidates.append(p)

    for p in candidates:
        resolved = p.resolve()
        if resolved in report_paths or resolved in artifact_paths:
            continue
        rel_str = p.relative_to(repo_root).as_posix()
        if _is_ignored_artifact(rel_str, config.diff_checks.ignore_globs):
            continue
        if _is_ignored_artifact(rel_str, config.reports.junit_globs):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime > max_ts:
            max_ts = mtime
            latest_rel = rel_str

    return max_ts, latest_rel


def parse_junit_xml(xml_path: Path) -> JUnitReportSummary:
    """Parse `<testsuite>` or `<testsuites>` XML into aggregated totals and executed cases."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    suites: list[ET.Element]
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.findall(".//testsuite"))
    else:
        raise ValueError(f"Unsupported JUnit XML root element <{root.tag}>")

    tests = 0
    failures = 0
    errors = 0
    skipped = 0
    suite_names: list[str] = []
    executed_cases: set[str] = set()

    if root.tag == "testsuites" and not suites:
        tests = int(root.attrib.get("tests", 0))
        failures = int(root.attrib.get("failures", 0))
        errors = int(root.attrib.get("errors", 0))
        skipped = int(root.attrib.get("skipped", 0))

    for suite in suites:
        sname = suite.attrib.get("name", "").strip()
        if sname:
            suite_names.append(sname)
        tests += int(suite.attrib.get("tests", 0))
        failures += int(suite.attrib.get("failures", 0))
        errors += int(suite.attrib.get("errors", 0))
        skipped += int(suite.attrib.get("skipped", 0))

        for tc in suite.findall(".//testcase"):
            name = tc.attrib.get("name", "").strip()
            classname = tc.attrib.get("classname", "").strip()
            base_name = name.split("(")[0].split("[")[0].strip()
            # Only treat as executed if NOT marked skipped
            if tc.find("skipped") is not None:
                continue
            if name:
                executed_cases.add(name)
            if base_name:
                executed_cases.add(base_name)
            if classname and name:
                executed_cases.add(f"{classname}.{name}")
                executed_cases.add(f"{classname}#{name}")
            if classname and base_name:
                executed_cases.add(f"{classname}.{base_name}")
                executed_cases.add(f"{classname}#{base_name}")

    return JUnitReportSummary(
        path=xml_path,
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        suite_names=tuple(dict.fromkeys(suite_names)),
        testcase_names=frozenset(executed_cases),
    )


def _extract_report_logs(xml_path: Path) -> str:
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception:
        return ""
    chunks: list[str] = []
    for tag in ("system-out", "system-err", "failure", "error", "skipped"):
        for node in root.findall(f".//{tag}"):
            if node.text:
                chunks.append(node.text)
            msg = node.attrib.get("message")
            if msg:
                chunks.append(msg)
    # Check Surefire sibling .txt file if name follows TEST-<Class>.xml -> <Class>.txt
    if xml_path.name.startswith("TEST-") and xml_path.suffix.lower() == ".xml":
        sibling_txt = xml_path.with_name(xml_path.name[5:-4] + ".txt")
        if sibling_txt.exists():
            try:
                chunks.append(sibling_txt.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return "\n".join(chunks)


def verify_reports_and_artifacts(repo_root: Path, config: UnrigConfig) -> tuple[list[str], int]:
    """
    Verify JUnit XML reports and runtime proof artifacts.

    Returns `(findings, discovered_report_count)`.
    """
    findings: list[str] = []
    xml_paths = _iter_junit_xml_paths(repo_root, config.reports.junit_globs)
    report_set = {p.resolve() for p in xml_paths}
    max_source_mtime, latest_src_rel = compute_max_source_mtime(repo_root, config, report_set)

    need_reports = config.reports.require_reports or bool(config.reports.required_tests)
    if need_reports and not xml_paths:
        findings.append(
            "[REPORT_MISSING] No JUnit XML reports matched configured globs: "
            + ", ".join(config.reports.junit_globs)
        )
        return findings, 0

    summaries: list[JUnitReportSummary] = []
    all_executed: set[str] = set()
    by_suite: dict[str, list[JUnitReportSummary]] = {}

    for xp in xml_paths:
        rel_xml = xp.relative_to(repo_root.resolve()).as_posix()
        try:
            summary = parse_junit_xml(xp)
        except Exception as exc:
            findings.append(f"[REPORT_CORRUPT] Failed to parse JUnit XML '{rel_xml}': {exc}")
            continue
        summaries.append(summary)
        all_executed.update(summary.testcase_names)
        for sname in summary.suite_names:
            by_suite.setdefault(sname, []).append(summary)
        stem = xp.stem[5:] if xp.stem.startswith("TEST-") else xp.stem
        by_suite.setdefault(stem, []).append(summary)

        xml_mtime = xp.stat().st_mtime
        if max_source_mtime > 0.0 and xml_mtime < (max_source_mtime - 1.0):
            findings.append(
                f"[REPORT_STALE] JUnit report '{rel_xml}' is older than source file '{latest_src_rel}'."
            )

        if summary.failures > 0 or summary.errors > 0:
            findings.append(
                f"[TESTS_FAILED] '{rel_xml}' reported failures={summary.failures}, errors={summary.errors}."
            )
        if config.reports.forbid_skipped and summary.skipped > 0:
            findings.append(
                f"[TESTS_SKIPPED] '{rel_xml}' reported skipped={summary.skipped} (forbid_skipped=true)."
            )

        logs_text = _extract_report_logs(xp)
        for tok in config.reports.privilege_log_tokens:
            if tok and tok in logs_text:
                findings.append(
                    f"[PRIVILEGE_VIOLATION] '{rel_xml}' output contains forbidden privilege token '{tok}'."
                )

    for suite_key, req_methods in config.reports.required_tests.items():
        matched_summaries = by_suite.get(suite_key, [])
        if not matched_summaries:
            # Fallback: allow suite_key matched directly via classname prefix in testcase_names
            suite_cases = {
                c for c in all_executed
                if c.startswith(f"{suite_key}.") or c.startswith(f"{suite_key}#")
            }
            if not suite_cases and summaries:
                findings.append(
                    f"[SUITE_MISSING] Required test suite '{suite_key}' not found in JUnit XML reports."
                )
                continue
            executed_for_suite = suite_cases
        else:
            executed_for_suite = set()
            for s in matched_summaries:
                executed_for_suite.update(s.testcase_names)
            if sum(s.tests for s in matched_summaries) == 0:
                findings.append(f"[SUITE_EMPTY] Suite '{suite_key}' executed 0 tests.")

        for method in req_methods:
            if (
                method not in executed_for_suite
                and f"{suite_key}.{method}" not in executed_for_suite
                and f"{suite_key}#{method}" not in executed_for_suite
            ):
                findings.append(
                    f"[METHOD_MISSING] Required test method '{suite_key}#{method}' did not execute."
                )

    now = time.time()
    for art in config.runtime_proof_artifacts:
        art_path = repo_root / art.path
        if not art_path.exists() or not art_path.is_file():
            findings.append(f"[ARTIFACT_MISSING] Required runtime proof artifact missing: '{art.path}'.")
            continue
        st = art_path.stat()
        if st.st_size == 0:
            findings.append(f"[ARTIFACT_EMPTY] Runtime proof artifact is empty: '{art.path}'.")
        if max_source_mtime > 0.0 and st.st_mtime < (max_source_mtime - 1.0):
            findings.append(
                f"[ARTIFACT_STALE] Runtime proof artifact '{art.path}' is older than source '{latest_src_rel}'."
            )
        if art.max_age_seconds is not None and (now - st.st_mtime) > art.max_age_seconds:
            findings.append(
                f"[ARTIFACT_EXPIRED] Runtime proof artifact '{art.path}' exceeds max_age_seconds={art.max_age_seconds}."
            )

    return findings, len(xml_paths)
