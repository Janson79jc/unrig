"""Rule acceptance gates: >= 80% precision floor & prefilter AST fan-out budget."""

from __future__ import annotations

import ast
import pytest

from unrig.diff_counter import python_ast_prefilter, scan_source_defects
from unrig.format_registry import FORMAT_REGISTRY


PRECISION_FLOOR = 0.80
MAX_CLEAN_AST_FANOUT_RATIO = 0.15


def evaluate_rule_budget(
    *, tp: int, fp: int, clean_ast_parsed: int, total_clean_files: int
) -> tuple[bool, float, float]:
    """Compute precision and clean-file AST fan-out ratio against acceptance thresholds."""
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
    fanout = (clean_ast_parsed / total_clean_files) if total_clean_files > 0 else 0.0
    passed = precision >= PRECISION_FLOOR and fanout <= MAX_CLEAN_AST_FANOUT_RATIO
    return passed, precision, fanout


def test_builtin_rules_meet_precision_floor_on_labeled_corpus() -> None:
    """Every built-in language detector must hold >= 80% precision across clean & buggy samples."""
    labeled_samples: list[tuple[str, str, bool]] = [
        # --- Positive (expect >= 1 defect) ---
        ("tests/test_a.py", "def test_stub():\n    pass\n", True),
        ("tests/test_b.py", "def test_taut():\n    assert True\n", True),
        ("src/svc.py", "def load():\n    try:\n        work()\n    except Exception:\n        pass\n", True),
        ("src/svc2.py", "def todo():\n    raise NotImplementedError('later')\n", True),
        ("tests/AppTest.java", "class AppTest { @Test void blank() {} }\n", True),
        ("src/Main.java", "class Main { void f() { try { a(); } catch (Exception e) {} } }\n", True),
        ("tests/ui.test.ts", "it('blank', () => {});\n", True),
        ("tests/ok.test.ts", "expect(true).toBe(true);\n", True),
        # --- Negative clean holdouts (expect 0 defects) ---
        ("src/math_utils.py", "def add(x: int, y: int) -> int:\n    return x + y\n", False),
        ("src/models.py", "from dataclasses import dataclass\n\n@dataclass\nclass Item:\n    name: str\n    price: int\n", False),
        ("src/abc_contract.py", "from abc import ABC, abstractmethod\n\nclass Base(ABC):\n    @abstractmethod\n    def run(self):\n        raise NotImplementedError\n", False),
        ("src/io_safe.py", "def close_quietly(h):\n    try:\n        h.close()\n    except Exception as ignored:\n        pass\n", False),
        ("src/commented.py", "# assert True\n# except Exception: pass\nVALUE = 10\n", False),
        ("tests/test_real.py", "def test_sum():\n    res = sum([1, 2, 3])\n    assert res == 6\n", False),
        ("src/Clean.java", "class Clean { void f() { try { a(); } catch (Exception ignored) {} } }\n", False),
        ("tests/clean.test.ts", "// expect(true).toBe(true)\nit('sums', () => { expect(1 + 2).toBe(3); });\n", False),
    ]

    tp = 0
    fp = 0
    fn = 0
    for rel_path, src, expected_defect in labeled_samples:
        ext = "." + rel_path.rsplit(".", 1)[-1]
        spec = FORMAT_REGISTRY[ext]
        hits = scan_source_defects(rel_path, src, spec)
        if expected_defect:
            if hits:
                tp += 1
            else:
                fn += 1
        else:
            if hits:
                fp += 1

    assert fn == 0, f"Built-in corpus missed {fn} positive defect(s)"
    ok, precision, _ = evaluate_rule_budget(tp=tp, fp=fp, clean_ast_parsed=0, total_clean_files=8)
    assert ok and precision == 1.0, f"Expected precision >= {PRECISION_FLOOR:.0%}, got {precision:.1%} (TP={tp}, FP={fp})"


def test_python_prefilter_bounds_clean_ast_fanout_with_full_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean utility modules must not fan out into `ast.parse`, while defects retain 100% recall."""
    clean_modules = [
        (f"src/mod_{i}.py", f"def compute_{i}(x: int, y: int) -> int:\n    return (x * {i + 1}) + y\n")
        for i in range(20)
    ]
    defective_modules = [
        ("src/bad_stub.py", "def f():\n    raise NotImplementedError\n"),
        ("src/bad_except.py", "def g():\n    try:\n        work()\n    except Exception:\n        pass\n"),
        ("tests/test_bad.py", "def test_empty():\n    pass\n"),
        ("tests/test_taut.py", "def test_taut():\n    assert True\n"),
    ]

    real_ast_parse = ast.parse
    parse_calls = {"count": 0}

    def counted_parse(*args, **kwargs):
        parse_calls["count"] += 1
        return real_ast_parse(*args, **kwargs)

    monkeypatch.setattr(ast, "parse", counted_parse)
    py_spec = FORMAT_REGISTRY[".py"]

    clean_fp = 0
    for rel, src in clean_modules:
        if scan_source_defects(rel, src, py_spec):
            clean_fp += 1
    clean_parses = parse_calls["count"]

    defect_tp = 0
    for rel, src in defective_modules:
        assert python_ast_prefilter(rel, src) is True
        if scan_source_defects(rel, src, py_spec):
            defect_tp += 1

    ok, precision, fanout = evaluate_rule_budget(
        tp=defect_tp,
        fp=clean_fp,
        clean_ast_parsed=clean_parses,
        total_clean_files=len(clean_modules),
    )
    assert defect_tp == len(defective_modules)
    assert clean_parses == 0
    assert ok and fanout <= MAX_CLEAN_AST_FANOUT_RATIO and precision >= PRECISION_FLOOR


def test_acceptance_gate_vetoes_noisy_relaxed_rule_regression() -> None:
    """Verify the acceptance gate rejects #3638-style noise (+45% fanout, 1 TP / 11 FP = 8.3%)."""
    passed, precision, fanout = evaluate_rule_budget(
        tp=1,
        fp=11,
        clean_ast_parsed=9,
        total_clean_files=20,  # 45% clean AST fan-out spike
    )
    assert not passed
    assert precision < PRECISION_FLOOR
    assert fanout > MAX_CLEAN_AST_FANOUT_RATIO
