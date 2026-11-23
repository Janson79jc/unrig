"""Format Registry 4-invariant contract tests across all registered extensions."""

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

    js_spec = FORMAT_REGISTRY[".js"]
    assert _matches_any_glob("app/public/plugins/vendor/chart.js", js_spec.default_ignore_globs)
    assert _matches_any_glob("dist/bundle.min.js", js_spec.default_ignore_globs)
    assert not _matches_any_glob("src/utils/helper.js", js_spec.default_ignore_globs)

    ts_spec = FORMAT_REGISTRY[".ts"]
    assert _matches_any_glob("src/types/auto-imports.d.ts", ts_spec.default_ignore_globs)
    assert _matches_any_glob("src/types/components.d.ts", ts_spec.default_ignore_globs)
    assert not _matches_any_glob("src/utils/math.ts", ts_spec.default_ignore_globs)


def test_js_ts_regex_literals_templates_and_jsx_preserve_comment_boundaries() -> None:
    ts_spec = FORMAT_REGISTRY[".ts"]
    tsx_spec = FORMAT_REGISTRY[".tsx"]
    vue_spec = FORMAT_REGISTRY[".vue"]
    rule = (ForbiddenPatternRule(rule_id="CUSTOM-TOK", pattern=r"\bFORBIDDEN_TOKEN\b", message="no tok"),)

    # 1. Regex with `//`, `'`, `[/]`, and `/*` must not poison quotes or swallow subsequent code
    ts_regex_src = (
        "const r1 = /'\\/\\//;\n"
        "const r2 = /[/]/;\n"
        "const r3 = /\\/\\*/;\n"
        "// FORBIDDEN_TOKEN after regex\n"
        "const ratio = a / b / c; // FORBIDDEN_TOKEN after division\n"
    )
    assert scan_source_defects("src/re.ts", ts_regex_src, ts_spec, rule) == []
    assert blank_comments_for_path("src/re.ts", ts_regex_src).count("\n") == ts_regex_src.count("\n")

    # Code AFTER `/\/\*/` must NOT be blanked as a block comment
    ts_after_regex = "const r = /\\/\\*/;\nconst bad = FORBIDDEN_TOKEN;\n"
    hits = scan_source_defects("src/after_re.ts", ts_after_regex, ts_spec, rule)
    assert len(hits) == 1 and hits[0][0] == 2

    # 2. Nested template `${ /* comment */ }` expressions must have inner comments blanked
    ts_tpl_src = "const msg = `a ${/* FORBIDDEN_TOKEN */ `b ${// FORBIDDEN_TOKEN\n1}`} c`;\n"
    assert scan_source_defects("src/tpl.ts", ts_tpl_src, ts_spec, rule) == []
    assert blank_comments_for_path("src/tpl.ts", ts_tpl_src).count("\n") == ts_tpl_src.count("\n")

    # 3. JSX closing tags `</tag>` must not open regex mode across tags
    tsx_src = "export const ui = <div>{a}</div><div>{b}</div>; // FORBIDDEN_TOKEN\n"
    assert scan_source_defects("src/ui.tsx", tsx_src, tsx_spec, rule) == []

    # 4. Vue <template> apostrophe must not poison <script setup lang="ts"> comment blanking
    vue_src = (
        "<template>\n"
        "  <p>It's fine <!-- FORBIDDEN_TOKEN --></p>\n"
        "</template>\n"
        "<script setup lang=\"ts\">\n"
        "// FORBIDDEN_TOKEN\n"
        "const ok = 1;\n"
        "</script>\n"
    )
    assert scan_source_defects("src/Comp.vue", vue_src, vue_spec, rule) == []
    assert blank_comments_for_path("src/Comp.vue", vue_src).count("\n") == vue_src.count("\n")
