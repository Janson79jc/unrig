"""#3600 Format Registry 4-invariant contract tests across all registered extensions."""

from __future__ import annotations

import pytest

from unrig.config import ForbiddenPatternRule
from unrig.diff_counter import _matches_any_glob, scan_source_defects
from unrig.format_registry import FORMAT_REGISTRY, blank_comments_for_path


COMMENT_SNIPPETS_BY_EXT: dict[str, tuple[str, str]] = {
    ".py": ("# assert True\n# except Exception: pass\nx = 1\n", "def test_ok():\n    assert True\n"),
    ".pyi": ("# raise NotImplementedError\ndef f(x: int) -> int: ...\n", "raise NotImplementedError\n"),
    ".js": ("// expect(true).toBe(true)\n/* catch (e) {} */\nconst x = 1;\n", "expect(true).toBe(true);\n"),
    ".jsx": ("// expect(true).toBe(true)\n/* catch (e) {} */\nconst x = 1;\n", "expect(true).toBe(true);\n"),
    ".mjs": ("// catch (e) {}\nexport const x = 1;\n", "try {} catch (e) {}\n"),
    ".cjs": ("// catch (e) {}\nmodule.exports = 1;\n", "try {} catch (e) {}\n"),
    ".ts": ("// expect(true).toBe(true)\n/* catch (e) {} */\nconst x: number = 1;\n", "expect(true).toBeTruthy();\n"),
    ".tsx": ("// expect(true).toBe(true)\nconst x: number = 1;\n", "try {} catch (e) {}\n"),
    ".mts": ("// catch (e) {}\nexport const x = 1;\n", "try {} catch (e) {}\n"),
    ".cts": ("// catch (e) {}\nexport const x = 1;\n", "try {} catch (e) {}\n"),
    ".vue": (
        "<!-- catch (e) {} -->\n<script>\n// expect(true).toBe(true)\nconst a = 1;\n</script>\n",
        "<script>\nexpect(true).toBe(true);\n</script>\n",
    ),
    ".java": (
        "// assertTrue(true);\n/* catch (Exception e) {} */\nclass A {}\n",
        "class A { void f() { try {} catch (Exception e) {} } }\n",
    ),
    ".go": ("// FORBIDDEN_TOKEN\n/* FORBIDDEN_TOKEN */\npackage main\n", "package main\nvar x = \"FORBIDDEN_TOKEN\"\n"),
    ".xml": ("<!-- FORBIDDEN_TOKEN\nmulti line -->\n<root/>\n", "<root attr=\"FORBIDDEN_TOKEN\"/>\n"),
    ".html": ("<!-- FORBIDDEN_TOKEN -->\n<div/>\n", "<div>FORBIDDEN_TOKEN</div>\n"),
    ".sql": ("-- FORBIDDEN_TOKEN\n/* FORBIDDEN_TOKEN */\nSELECT 1;\n", "SELECT 'FORBIDDEN_TOKEN';\n"),
    ".sh": ("# FORBIDDEN_TOKEN\necho ok\n", "echo FORBIDDEN_TOKEN\n"),
    ".bash": ("# FORBIDDEN_TOKEN\necho ok\n", "echo FORBIDDEN_TOKEN\n"),
    ".yaml": ("# FORBIDDEN_TOKEN\nversion: 1\n", "key: FORBIDDEN_TOKEN\n"),
    ".yml": ("# FORBIDDEN_TOKEN\nversion: 1\n", "key: FORBIDDEN_TOKEN\n"),
}


def test_every_registered_extension_has_contract_fixture() -> None:
    missing = set(FORMAT_REGISTRY.keys()) - set(COMMENT_SNIPPETS_BY_EXT.keys())
    assert not missing, f"Unregistered contract fixtures for: {sorted(missing)}"


@pytest.mark.parametrize("ext", sorted(FORMAT_REGISTRY.keys()))
def test_invariant_1_comment_silence_zero_false_positives(ext: str) -> None:
    spec = FORMAT_REGISTRY[ext]
    comment_only_src, _ = COMMENT_SNIPPETS_BY_EXT[ext]
    rule = (ForbiddenPatternRule(rule_id="CUSTOM-TOK", pattern=r"\bFORBIDDEN_TOKEN\b", message="no tok"),)
    hits = scan_source_defects(f"tests/sample{ext}", comment_only_src, spec, rule)
    assert hits == []


@pytest.mark.parametrize("ext", sorted(FORMAT_REGISTRY.keys()))
def test_invariant_2_newline_conservation(ext: str) -> None:
    comment_only_src, positive_src = COMMENT_SNIPPETS_BY_EXT[ext]
    for sample in (comment_only_src, positive_src):
        blanked = blank_comments_for_path(f"src/sample{ext}", sample)
        assert blanked.count("\n") == sample.count("\n")
        assert len(blanked.splitlines()) == len(sample.splitlines())


@pytest.mark.parametrize("ext", sorted(FORMAT_REGISTRY.keys()))
def test_invariant_3_positive_code_detected(ext: str) -> None:
    spec = FORMAT_REGISTRY[ext]
    _, positive_src = COMMENT_SNIPPETS_BY_EXT[ext]
    rule = (ForbiddenPatternRule(rule_id="CUSTOM-TOK", pattern=r"\bFORBIDDEN_TOKEN\b", message="no tok"),)
    hits = scan_source_defects(f"tests/sample{ext}", positive_src, spec, rule)
    assert len(hits) >= 1


def test_invariant_4_default_ignore_globs_filter_vendor_and_build() -> None:
    py_spec = FORMAT_REGISTRY[".py"]
    assert _matches_any_glob(".venv/lib/foo.py", py_spec.default_ignore_globs)
    assert not _matches_any_glob("src/foo.py", py_spec.default_ignore_globs)

    java_spec = FORMAT_REGISTRY[".java"]
    assert _matches_any_glob("module/target/Foo.java", java_spec.default_ignore_globs)
    assert not _matches_any_glob("module/src/main/Foo.java", java_spec.default_ignore_globs)
