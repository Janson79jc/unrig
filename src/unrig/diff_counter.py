"""Incremental CounterMultiset(after - before) defect detector & ratchet verifier."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable

from .config import ForbiddenPatternRule, UnrigConfig, normalize_rel_path
from .format_registry import (
    FormatSpec,
    extract_js_ts_comments_and_blank,
    extract_vue_comments_and_blank,
    get_format_for_path,
)
from .l0_guard import git_has_head, git_status_entries, is_git_repo


@dataclass(frozen=True)
class DiffFinding:
    rule_id: str
    rel_path: str
    line: int
    signature: str
    message: str


@dataclass(frozen=True)
class RatchetEntry:
    rule: str
    file: str
    count: int
    reason: str
    signature: str = ""


def _matches_any_glob(rel_path: str, globs: Iterable[str]) -> bool:
    norm = rel_path.replace("\\", "/")
    for pat in globs:
        clean_pat = normalize_rel_path(pat)
        if fnmatch(norm, clean_pat):
            return True
        if clean_pat.startswith("**/") and fnmatch(norm, clean_pat[3:]):
            return True
    return False


def _is_test_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lower()
    name = Path(norm).name
    return (
        "/tests/" in f"/{norm}"
        or "/test/" in f"/{norm}"
        or "/__tests__/" in f"/{norm}"
        or "/src/test/" in f"/{norm}"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or name.endswith("test.java")
        or name.endswith("tests.java")
    )


def load_and_validate_ratchet(
    repo_root: Path, ratchet_rel_path: str
) -> tuple[list[RatchetEntry], list[str]]:
    """Load `.unrig/ratchet.json` and enforce non-empty `reason` on every entry."""
    ratchet_path = repo_root / ratchet_rel_path
    if not ratchet_path.exists():
        return [], []

    errors: list[str] = []
    try:
        data = json.loads(ratchet_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], [f"[RATCHET_INVALID] Failed to parse '{ratchet_rel_path}': {exc}"]

    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        return [], [f"[RATCHET_INVALID] '{ratchet_rel_path}' must be a JSON object with an 'entries' array."]

    entries: list[RatchetEntry] = []
    for idx, item in enumerate(data.get("entries", [])):
        if not isinstance(item, dict):
            errors.append(f"[RATCHET_INVALID] Entry #{idx} in '{ratchet_rel_path}' must be an object.")
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 3 or reason.strip().lower() in {"todo", "tbd", "n/a", "none"}:
            errors.append(
                f"[RATCHET_EMPTY_REASON] Entry #{idx} in '{ratchet_rel_path}' has missing or empty 'reason'. "
                "Every grandfathered ratchet exception requires an explicit justification."
            )
            continue
        raw_count = item.get("count", item.get("max_count"))
        if not isinstance(raw_count, int) or raw_count < 0:
            errors.append(
                f"[RATCHET_INVALID] Entry #{idx} in '{ratchet_rel_path}' requires non-negative integer 'count'."
            )
            continue
        entries.append(
            RatchetEntry(
                rule=str(item.get("rule") or "*").strip(),
                file=normalize_rel_path(str(item.get("file") or "*")),
                count=raw_count,
                reason=reason.strip(),
                signature=str(item.get("signature") or "").strip(),
            )
        )
    return entries, errors


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_abstractmethod(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        name = _call_name(dec)
        if name == "abstractmethod":
            return True
    return False


def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_noop_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Return) and stmt.value is None:
        return True
    return False


_PY_DUMMY_CONST_RET_RE = re.compile(
    r"\breturn\s+(?:True|False|0|1|\"\"|''|\[\s*\]|\{\s*\})\s*(?:#[^\n]*)?$",
    re.MULTILINE,
)
_PY_LOCAL_CONST_ASSIGN_RE = re.compile(
    r"=\s*(?:True|False|0|1|\"\"|''|\[\s*\]|\{\s*\})\s*(?:#[^\n]*)?$",
    re.MULTILINE,
)
_PY_IDENT_RET_RE = re.compile(
    r"\breturn\s+[A-Za-z_]\w*\s*(?:#[^\n]*)?$",
    re.MULTILINE,
)


def _is_skipped_stub_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        if _call_name(dec) in ("abstractmethod", "property", "overload", "setter", "getter", "deleter"):
            return True
    return False


def _is_dummy_constant_expr(expr: ast.expr, local_consts: set[str]) -> bool:
    if isinstance(expr, ast.Constant):
        return expr.value is not None
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.USub, ast.UAdd, ast.Not)):
        return isinstance(expr.operand, ast.Constant) and expr.operand.value is not None
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return len(expr.elts) == 0
    if isinstance(expr, ast.Dict):
        return len(expr.keys) == 0
    if isinstance(expr, ast.Name) and expr.id in local_consts:
        return True
    return False


def _is_dummy_stmt_block(stmts: list[ast.stmt]) -> bool:
    return all(_is_noop_stmt(s) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in stmts)


def _check_python_dummy_constant_return(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Detect non-abstract functions taking named business parameters yet returning a hardcoded constant."""
    if _is_skipped_stub_decorator(fn):
        return False

    all_args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    named_params = [
        a.arg for a in all_args
        if a.arg not in ("self", "cls") and not a.arg.startswith("_")
    ]
    if not named_params:
        return False

    stmts = _body_without_docstring(fn.body)
    if not stmts or not isinstance(stmts[-1], ast.Return) or stmts[-1].value is None:
        return False

    local_consts: set[str] = set()
    for stmt in stmts[:-1]:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Constant, ast.Name)):
            continue
        if isinstance(stmt, ast.If) and _is_dummy_stmt_block(stmt.body) and _is_dummy_stmt_block(stmt.orelse):
            continue
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            tname = stmt.targets[0].id
            if tname == "_":
                continue
            if _is_dummy_constant_expr(stmt.value, local_consts):
                local_consts.add(tname)
                continue
            return False
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            tname = stmt.target.id
            if tname == "_":
                continue
            if _is_dummy_constant_expr(stmt.value, local_consts):
                local_consts.add(tname)
                continue
            return False
        return False

    return _is_dummy_constant_expr(stmts[-1].value, local_consts)


def python_ast_prefilter(rel_path: str, raw_source: str) -> bool:
    """Fast-token gate: skip `ast.parse` on clean Python modules lacking target tokens."""
    if _is_test_path(rel_path) and "def test_" in raw_source:
        return True
    if "assert" in raw_source or "except" in raw_source or "NotImplementedError" in raw_source:
        return True
    if not _is_test_path(rel_path) and not rel_path.endswith(".pyi") and "def " in raw_source:
        if _PY_DUMMY_CONST_RET_RE.search(raw_source):
            return True
        if _PY_LOCAL_CONST_ASSIGN_RE.search(raw_source) and _PY_IDENT_RET_RE.search(raw_source):
            return True
    return False


def _scan_python_ast(
    rel_path: str, raw_source: str
) -> list[tuple[int, str, str, str]] | None:
    """Return list of `(line_no, rule_id, signature, message)` or `None` on SyntaxError."""
    if not python_ast_prefilter(rel_path, raw_source):
        return []
    try:
        tree = ast.parse(raw_source)
    except SyntaxError:
        return None

    occurrences: list[tuple[int, str, str, str]] = []
    abstract_raise_lines: set[int] = set()
    is_test = _is_test_path(rel_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            test_expr = node.test
            if isinstance(test_expr, ast.Constant) and bool(test_expr.value) is True:
                occurrences.append((
                    node.lineno,
                    "TAUTOLOGICAL-ASSERT",
                    "assert True",
                    "Tautological `assert True` proves nothing.",
                ))
            elif (
                isinstance(test_expr, ast.UnaryOp)
                and isinstance(test_expr.op, ast.Not)
                and isinstance(test_expr.operand, ast.Constant)
                and bool(test_expr.operand.value) is False
            ):
                occurrences.append((
                    node.lineno,
                    "TAUTOLOGICAL-ASSERT",
                    "assert not False",
                    "Tautological `assert not False` proves nothing.",
                ))
            elif (
                isinstance(test_expr, ast.Compare)
                and len(test_expr.ops) == 1
                and isinstance(test_expr.ops[0], (ast.Eq, ast.Is))
                and len(test_expr.comparators) == 1
                and ast.dump(test_expr.left) == ast.dump(test_expr.comparators[0])
            ):
                sig = f"assert {ast.dump(test_expr.left)} == self"
                occurrences.append((
                    node.lineno,
                    "TAUTOLOGICAL-ASSERT",
                    sig,
                    "Tautological self-comparison assertion.",
                ))
            elif isinstance(test_expr, ast.BoolOp) and isinstance(test_expr.op, ast.Or):
                if any(isinstance(v, ast.Constant) and bool(v.value) is True for v in test_expr.values):
                    occurrences.append((
                        node.lineno,
                        "TAUTOLOGICAL-ASSERT",
                        "assert ... or True",
                        "Tautological `... or True` short-circuits assertion to always pass.",
                    ))

        elif isinstance(node, ast.Call):
            cname = _call_name(node.func)
            if cname == "assertTrue" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value is True:
                occurrences.append((
                    node.lineno,
                    "TAUTOLOGICAL-ASSERT",
                    "assertTrue(True)",
                    "Tautological `assertTrue(True)` proves nothing.",
                ))

        elif isinstance(node, ast.ExceptHandler):
            is_broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")
            )
            handler_name = (node.name or "").lower()
            if is_broad and handler_name not in ("ignored", "ignore", "_"):
                stmts = _body_without_docstring(node.body)
                if stmts and all(_is_noop_stmt(s) for s in stmts):
                    exc_label = "bare except" if node.type is None else f"except {node.type.id}"
                    occurrences.append((
                        node.lineno,
                        "SILENT-EXCEPT",
                        f"{exc_label}: pass",
                        f"Silent exception swallow (`{exc_label}: pass`).",
                    ))

        elif isinstance(node, ast.Raise) and isinstance(node.exc, (ast.Name, ast.Call)):
            exc_name = _call_name(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
            if exc_name == "NotImplementedError":
                occurrences.append((
                    node.lineno,
                    "PLACEHOLDER-STUB",
                    "raise NotImplementedError",
                    "Placeholder `raise NotImplementedError` left in code.",
                ))

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_abstractmethod(node):
                for s in node.body:
                    if isinstance(s, ast.Raise):
                        abstract_raise_lines.add(s.lineno)
            if is_test and node.name.startswith("test_"):
                stmts = _body_without_docstring(node.body)
                if not stmts or all(_is_noop_stmt(s) for s in stmts):
                    occurrences.append((
                        node.lineno,
                        "EMPTY-TEST-STUB",
                        f"empty test {node.name}",
                        f"Empty test stub `{node.name}()`.",
                    ))
                else:
                    has_operation = False
                    for sub in ast.walk(node):
                        if isinstance(sub, (ast.Assert, ast.Raise)):
                            has_operation = True
                            break
                        if isinstance(sub, ast.Call):
                            cn = _call_name(sub.func).lower()
                            if cn in ("print", "pprint", "sleep", "debug", "info", "warning", "warn", "error", "critical"):
                                continue
                            if cn:
                                has_operation = True
                                break
                    if not has_operation:
                        occurrences.append((
                            node.lineno,
                            "EMPTY-TEST-STUB",
                            f"unverified test {node.name}",
                            f"Test `{node.name}()` contains no assertions or operational calls.",
                        ))
            elif not is_test and not rel_path.endswith(".pyi"):
                if _check_python_dummy_constant_return(node):
                    occurrences.append((
                        node.lineno,
                        "DUMMY-CONSTANT-RETURN",
                        f"dummy-return {node.name}",
                        f"Function `{node.name}()` ignores its parameter(s) and returns a hardcoded constant.",
                    ))

    if abstract_raise_lines:
        occurrences = [
            occ for occ in occurrences
            if not (occ[1] == "PLACEHOLDER-STUB" and occ[0] in abstract_raise_lines)
        ]

    return occurrences


_PY_FALLBACK_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("TAUTOLOGICAL-ASSERT", re.compile(r"\bassert\s+True\b"), "Tautological `assert True`."),
    ("PLACEHOLDER-STUB", re.compile(r"\braise\s+NotImplementedError\b"), "Placeholder `NotImplementedError`."),
)

_JS_TEST_NAME_PART = r"(?:'[^'\n]*'|\"[^\"\n]*\"|`[^`\n]*`|[^,\n;()]+)"

_JS_TS_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(
            r"\bexpect\s*\(\s*true\s*\)\s*\.\s*(?:toBe\s*\(\s*true\s*\)|toBeTruthy\s*\(\s*\)|toEqual\s*\(\s*true\s*\)|toStrictEqual\s*\(\s*true\s*\))"
        ),
        "Tautological `expect(true)` assertion.",
    ),
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(
            r"\bexpect\s*\(\s*false\s*\)\s*\.\s*(?:toBe\s*\(\s*false\s*\)|toBeFalsy\s*\(\s*\)|toEqual\s*\(\s*false\s*\)|toStrictEqual\s*\(\s*false\s*\))"
        ),
        "Tautological `expect(false)` assertion.",
    ),
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(
            r"\bassert\s*\.\s*(?:ok\s*\(\s*true\s*\)|strictEqual\s*\(\s*true\s*,\s*true\s*\)|strictEqual\s*\(\s*false\s*,\s*false\s*\)|isTrue\s*\(\s*true\s*\)|isFalse\s*\(\s*false\s*\))"
        ),
        "Tautological `assert` boolean assertion.",
    ),
    (
        "EMPTY-TEST-STUB",
        re.compile(
            rf"\b(?:it|test)\s*\(\s*{_JS_TEST_NAME_PART}\s*,\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$]\w*)\s*(?::\s*[^=;{{}}()]+)?\s*=>\s*\{{\s*\}}\s*\)"
        ),
        "Empty JS/TS test callback `{}`.",
    ),
    (
        "EMPTY-TEST-STUB",
        re.compile(
            rf"\b(?:it|test)\s*\(\s*{_JS_TEST_NAME_PART}\s*,\s*(?:async\s+)?function(?:\s+[A-Za-z_$]\w*)?\s*\([^)]*\)\s*(?::\s*[^;{{}}()]+)?\s*\{{\s*\}}\s*\)"
        ),
        "Empty JS/TS function test callback `{}`.",
    ),
    (
        "SILENT-CATCH",
        re.compile(r"\bcatch\s*(?:\(\s*([A-Za-z_$]\w*)(?:\s*:\s*[^)]+)?\s*\))?\s*\{\s*\}"),
        "Empty catch block silently swallows exceptions.",
    ),
    (
        "TYPE-ESCAPE",
        re.compile(r"\bas\s+unknown\s+as\b"),
        "Double type cast `as unknown as` bypasses TypeScript contract checking.",
    ),
    (
        "TYPE-ESCAPE",
        re.compile(r"\bas\s+any\b"),
        "Type escape `as any` bypasses TypeScript contract checking.",
    ),
)

_JS_TS_SKIPPED_TEST_RE = re.compile(
    r"\b(?:(?:it|test|describe)\s*\.\s*(?:skip|todo)|x(?:it|test|describe))\s*\("
)
_JS_TS_FOCUSED_TEST_RE = re.compile(
    r"\b(?:(?:it|test|describe|context|suite)\s*\.\s*only(?:\s*\.\s*each)?|f(?:it|describe))\s*\("
)

_JS_TS_TEST_CALL_RE = re.compile(r"(?<!\.)\b(?:it|test)\s*\(")
_JS_TS_CALL_EXPR_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*(?:<[^;=()\n{}]+>\s*)?\("
)
_JS_TS_NONOP_KW_RE = re.compile(
    r"\b(?:throw|should|fail|rejects|resolves|(?:expect|assert|verify|check)[A-Za-z0-9_$]*)\b"
)

_TS_NOCHECK_RE = re.compile(r"^@ts-nocheck(?=$|\s|[:\-])")
_TS_IGNORE_RE = re.compile(r"^@ts-ignore(?=$|\s|[:\-])")
_TS_EXPECT_ERROR_RE = re.compile(r"^@ts-expect-error(?=$|\s|[:\-])(.*)$")
_ESLINT_DISABLE_RE = re.compile(r"^(eslint-disable(?:-next-line|-line)?)(?=$|\s|[,;])")
_ESLINT_INLINE_OFF_RE = re.compile(r"^eslint\s+(?=.*:\s*(?:['\"]off['\"]|0\b))")
_PLACEHOLDER_JUSTIFICATIONS = frozenset({"todo", "tbd", "n/a", "none", "fix", "hack"})


def _skip_js_string(src: str, idx: int) -> int:
    """Advance past a `'...'`, `\"...\"`, or `` `...` `` literal starting at `idx`."""
    quote = src[idx]
    n = len(src)
    i = idx + 1
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _scan_js_ts_noop_tests(blanked: str) -> list[tuple[int, str, str, str]]:
    """Detect non-empty JS/TS `it`/`test` bodies containing only trivial/console/return statements."""
    hits: list[tuple[int, str, str, str]] = []
    n = len(blanked)
    for m in _JS_TS_TEST_CALL_RE.finditer(blanked):
        open_paren = m.end() - 1
        i = open_paren + 1
        paren_depth = 1
        brace_depth = 0
        bracket_depth = 0
        comma_idx: int | None = None

        while i < n and paren_depth > 0:
            ch = blanked[i]
            if ch in ('"', "'", "`"):
                i = _skip_js_string(blanked, i)
                continue
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    break
            elif ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == "," and paren_depth == 1 and brace_depth == 0 and bracket_depth == 0:
                comma_idx = i
                break
            i += 1

        if comma_idx is None:
            continue

        # Scan callback header in arg1 until `{` at call paren_depth==1 or `)`
        j = comma_idx + 1
        saw_fn_header = False
        body_open: int | None = None
        cb_paren = 0
        while j < n:
            ch = blanked[j]
            if ch in ('"', "'", "`"):
                j = _skip_js_string(blanked, j)
                continue
            if ch == "(":
                cb_paren += 1
                j += 1
                continue
            if ch == ")":
                if cb_paren == 0:
                    break
                cb_paren -= 1
                j += 1
                continue
            if ch == "=" and j + 1 < n and blanked[j + 1] == ">" and cb_paren == 0:
                saw_fn_header = True
                j += 2
                continue
            if blanked[j : j + 8] == "function" and cb_paren == 0:
                prev_ok = j == 0 or not (blanked[j - 1].isalnum() or blanked[j - 1] in ("_", "$"))
                nxt_ok = j + 8 >= n or not (blanked[j + 8].isalnum() or blanked[j + 8] in ("_", "$"))
                if prev_ok and nxt_ok:
                    saw_fn_header = True
            if ch == "{" and cb_paren == 0:
                if saw_fn_header:
                    body_open = j
                break
            j += 1

        if body_open is None:
            continue

        # Walk balanced `{ ... }` to find the callback body
        k = body_open + 1
        depth = 1
        while k < n and depth > 0:
            ch = blanked[k]
            if ch in ('"', "'", "`"):
                k = _skip_js_string(blanked, k)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1

        if depth != 0:
            continue

        body = blanked[body_open + 1 : k]
        # Skip truly empty bodies (`{}`) — already handled by `_JS_TS_PATTERNS`
        if not body.strip():
            continue

        if _JS_TS_NONOP_KW_RE.search(body):
            continue

        has_real_call = False
        for cm in _JS_TS_CALL_EXPR_RE.finditer(body):
            callee = "".join(cm.group(1).split())
            if not callee.startswith("console."):
                has_real_call = True
                break
        if has_real_call:
            continue

        line_no = blanked.count("\n", 0, m.start()) + 1
        hits.append((
            line_no,
            "EMPTY-TEST-STUB",
            "noop-body test",
            "JS/TS test callback contains no assertions or operational calls.",
        ))

    return hits


_JS_TS_DUMMY_FUNC_HEAD_RE = re.compile(
    r"(?:\bfunction\s+([A-Za-z_$][\w$]*)|"
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?=\()|"
    r"(?:^|[;\n{}]\s*)(?:public\s+|private\s+|protected\s+|async\s+|static\s+)*([A-Za-z_$][\w$]*)\s*)"
    r"\(\s*([^)]*)\)\s*(?::\s*[^=>{]+?)?\s*(?:=>\s*)?\{"
)
_JS_TS_CTRL_KEYWORDS = frozenset({"if", "else", "for", "while", "switch", "catch", "return", "new", "typeof", "function"})
_JS_TS_DUMMY_BODY_RE = re.compile(
    r"^(?:\s*(?:_\s*=\s*[A-Za-z_$][\w$]*|void\s+[A-Za-z_$][\w$]*)\s*;?)*\s*"
    r"(?:(?:const|let)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=;]+)?=\s*"
    r"(?:true|false|0|1|\"\"|''|\[\s*\]|\{\s*\})\s*;\s*)?"
    r"return\s+(?:true|false|0|1|\"\"|''|\[\s*\]|\{\s*\}|([A-Za-z_$][\w$]*))\s*;?\s*$"
)


def _split_top_level_params(raw_params: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    angle = 0
    cur: list[str] = []
    for ch in raw_params:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "," and depth == 0 and angle == 0:
            part = "".join(cur).strip()
            cur = []
            if part:
                parts.append(part)
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_js_params(raw_params: str) -> list[str]:
    cleaned: list[str] = []
    for p in _split_top_level_params(raw_params):
        head = p.split(":", 1)[0].split("=", 1)[0].strip().lstrip(".")
        if head and not head.startswith("_"):
            cleaned.append(head)
    return cleaned


def _extract_balanced_brace_body(
    src: str,
    open_brace_idx: int,
    quotes: tuple[str, ...] = ('"', "'", "`"),
) -> tuple[str, int] | None:
    n = len(src)
    k = open_brace_idx + 1
    depth = 1
    while k < n and depth > 0:
        ch = src[k]
        if ch in quotes:
            k = _skip_js_string(src, k)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_brace_idx + 1 : k], k + 1
        k += 1
    return None


def _scan_js_ts_dummy_returns(blanked: str) -> list[tuple[int, str, str, str]]:
    """Detect JS/TS non-test functions that accept named params yet return a hardcoded constant."""
    hits: list[tuple[int, str, str, str]] = []
    for m in _JS_TS_DUMMY_FUNC_HEAD_RE.finditer(blanked):
        fn_name = m.group(1) or m.group(2) or m.group(3) or ""
        if not fn_name or fn_name in _JS_TS_CTRL_KEYWORDS:
            continue
        params = _split_js_params(m.group(4))
        if not params:
            continue
        open_idx = m.end() - 1
        extracted = _extract_balanced_brace_body(blanked, open_idx)
        if extracted is None:
            continue
        body, _ = extracted
        bm = _JS_TS_DUMMY_BODY_RE.match(body)
        if not bm:
            continue
        const_var = bm.group(1)
        ret_var = bm.group(2)
        if ret_var is not None and ret_var != const_var:
            continue
        line_no = blanked.count("\n", 0, m.start()) + 1
        hits.append((
            line_no,
            "DUMMY-CONSTANT-RETURN",
            f"dummy-return {fn_name}",
            f"Function `{fn_name}()` ignores its parameter(s) and returns a hardcoded constant.",
        ))
    return hits


_PY_CLONE_KEEP_KEYWORDS = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally", "for",
    "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
    "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "len", "isinstance", "pytest", "range", "enumerate", "zip", "dict", "list", "set",
})
_JS_CLONE_KEEP_KEYWORDS = frozenset({
    "async", "await", "break", "case", "catch", "class", "const", "continue",
    "default", "else", "export", "extends", "false", "finally", "for", "function",
    "if", "import", "in", "instanceof", "let", "new", "null", "of", "return",
    "switch", "throw", "true", "try", "typeof", "undefined", "var", "void", "while",
    "expect", "assert", "it", "test", "describe", "toBe", "toEqual", "toStrictEqual",
})
_CLONE_TOKEN_RE = re.compile(r"<STR>|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|===|!==|=>|:=|==|!=|<=|>=|&&|\|\||[^\s]")
_MASKED_QUOTES_RE = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|`[^`]*`")


def _alpha_hash_tokens(raw_tokens: list[str], keep_words: frozenset[str], min_tokens: int) -> str | None:
    if len(raw_tokens) < min_tokens:
        return None
    ident_map: dict[str, str] = {}
    norm: list[str] = []
    for tok in raw_tokens:
        if tok[0].isdigit():
            norm.append("<NUM>")
        elif tok in ("<STR>", "<NUM>") or tok in keep_words or not (tok[0].isalpha() or tok[0] in ("_", "$")):
            norm.append(tok)
        else:
            mapped = ident_map.get(tok)
            if mapped is None:
                mapped = f"V{len(ident_map)}"
                ident_map[tok] = mapped
            norm.append(mapped)
    return hashlib.sha256(" ".join(norm).encode("utf-8")).hexdigest()[:12]


def _extract_python_clone_blocks(raw_source: str, min_tokens: int) -> list[tuple[int, str, str]]:
    if "def " not in raw_source:
        return []
    try:
        tree = ast.parse(raw_source)
    except SyntaxError:
        return []

    blocks: list[tuple[int, str, str]] = []

    def _walk_stmt_tokens(node: ast.AST, out: list[str]) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                out.append("<STR>")
            elif isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                out.append("<NUM>")
            else:
                out.append(repr(node.value))
            return
        if isinstance(node, ast.Name):
            out.append(node.id)
            return
        if isinstance(node, ast.arg):
            out.append(node.arg)
            return
        out.append(type(node).__name__)
        for field_name, val in ast.iter_fields(node):
            if field_name in ("ctx", "type_comment"):
                continue
            if isinstance(val, ast.AST):
                _walk_stmt_tokens(val, out)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, ast.AST):
                        _walk_stmt_tokens(item, out)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stmts = _body_without_docstring(node.body)
            if not stmts:
                continue
            tokens: list[str] = []
            for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if a.arg not in ("self", "cls"):
                    tokens.append(a.arg)
            for s in stmts:
                _walk_stmt_tokens(s, tokens)
            h = _alpha_hash_tokens(tokens, _PY_CLONE_KEEP_KEYWORDS, min_tokens)
            if h is not None:
                blocks.append((node.lineno, h, node.name))
    return blocks


def _extract_js_ts_clone_blocks(blanked: str, min_tokens: int) -> list[tuple[int, str, str]]:
    blocks: list[tuple[int, str, str]] = []
    seen_spans: set[tuple[int, int]] = set()

    def _add_body(start_pos: int, open_brace_idx: int, label: str) -> None:
        extracted = _extract_balanced_brace_body(blanked, open_brace_idx)
        if extracted is None:
            return
        body, end_pos = extracted
        span = (open_brace_idx, end_pos)
        if span in seen_spans:
            return
        seen_spans.add(span)
        norm_body = _MASKED_QUOTES_RE.sub("<STR>", body)
        tokens = _CLONE_TOKEN_RE.findall(norm_body)
        h = _alpha_hash_tokens(tokens, _JS_CLONE_KEEP_KEYWORDS, min_tokens)
        if h is not None:
            line_no = blanked.count("\n", 0, start_pos) + 1
            blocks.append((line_no, h, label))

    for m in _JS_TS_DUMMY_FUNC_HEAD_RE.finditer(blanked):
        fn_name = m.group(1) or m.group(2) or m.group(3) or ""
        if not fn_name or fn_name in _JS_TS_CTRL_KEYWORDS:
            continue
        _add_body(m.start(), m.end() - 1, fn_name)

    for m in _JS_TS_TEST_CALL_RE.finditer(blanked):
        open_paren = m.end() - 1
        i = open_paren + 1
        n = len(blanked)
        paren_depth = 1
        brace_depth = 0
        bracket_depth = 0
        comma_idx: int | None = None
        while i < n and paren_depth > 0:
            ch = blanked[i]
            if ch in ('"', "'", "`"):
                i = _skip_js_string(blanked, i)
                continue
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    break
            elif ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif ch == "," and paren_depth == 1 and brace_depth == 0 and bracket_depth == 0:
                comma_idx = i
                break
            i += 1
        if comma_idx is None:
            continue
        j = comma_idx + 1
        cb_paren = 0
        while j < n:
            ch = blanked[j]
            if ch in ('"', "'", "`"):
                j = _skip_js_string(blanked, j)
                continue
            if ch == "(":
                cb_paren += 1
            elif ch == ")":
                if cb_paren == 0:
                    break
                cb_paren -= 1
            elif ch == "{" and cb_paren == 0:
                _add_body(m.start(), j, "test-case")
                break
            j += 1

    return blocks


_JAVA_CLONE_KEEP_KEYWORDS = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "false", "final", "finally", "float", "for", "if", "implements",
    "import", "instanceof", "int", "interface", "long", "native", "new", "null",
    "package", "private", "protected", "public", "return", "short", "static",
    "strictfp", "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "true", "try", "var", "void", "volatile", "while",
    "String", "Object", "List", "Map", "Set", "Optional", "Assertions",
    "assertEquals", "assertTrue", "assertFalse", "assertNotNull", "assertNull",
})
_JAVA_CTRL_KEYWORDS = frozenset({
    "if", "else", "for", "while", "switch", "catch", "synchronized", "try", "assert", "new", "return", "throw",
})
_JAVA_NON_RET_PREFIXES = frozenset({
    "return", "new", "throw", "else", "case", "package", "import", "class", "interface", "enum", "record",
})
_JAVA_METHOD_HEAD_RE = re.compile(
    r"(?:^|[;\n{}])[ \t]*(?P<mods>(?:(?:public|private|protected|static|final|synchronized|native|strictfp|default)[ \t]+)*)"
    r"(?:<[^>{};\n]+>[ \t]+)?"
    r"(?P<ret>[A-Za-z_][\w$.]*(?:\s*<[^>{};\n]*>)?(?:\s*\[\s*\])*)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*\(\s*(?P<params>[^()]*)\)\s*(?:throws\s+[A-Za-z_][\w$.]*(?:\s*,\s*[A-Za-z_][\w$.]*)*)?\s*\{"
)
_JAVA_DUMMY_BODY_RE = re.compile(
    r"^\s*(?:(?:final\s+)?[A-Za-z_][\w$.<>?\[\]\s]*?\s+([A-Za-z_]\w*)\s*=\s*"
    r"(?:true|false|0|1|\"\"|Collections\s*\.\s*empty(?:List|Set|Map)\s*\(\s*\)|(?:List|Set|Map)\s*\.\s*of\s*\(\s*\))\s*;\s*)?"
    r"return\s+(?:true|false|0|1|\"\"|Collections\s*\.\s*empty(?:List|Set|Map)\s*\(\s*\)|(?:List|Set|Map)\s*\.\s*of\s*\(\s*\)|([A-Za-z_]\w*))\s*;\s*$"
)
_JAVA_STR_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def _split_java_params(raw_params: str) -> list[str]:
    names: list[str] = []
    for seg in _split_top_level_params(raw_params):
        toks = re.findall(r"[A-Za-z_]\w*", seg)
        if toks and not toks[-1].startswith("_"):
            names.append(toks[-1])
    return names


def _scan_java_dummy_returns(blanked: str) -> list[tuple[int, str, str, str]]:
    """Detect Java concrete non-test methods accepting named params yet returning a hardcoded constant."""
    hits: list[tuple[int, str, str, str]] = []
    for m in _JAVA_METHOD_HEAD_RE.finditer(blanked):
        mods = m.group("mods") or ""
        if "default" in mods.split():
            continue
        ret = (m.group("ret") or "").strip()
        ret_head = ret.split("<", 1)[0].split("[", 1)[0].strip()
        if not ret_head or ret_head in _JAVA_NON_RET_PREFIXES:
            continue
        fn_name = m.group("name") or ""
        if not fn_name or fn_name in _JAVA_CTRL_KEYWORDS:
            continue
        if fn_name in ("equals", "toString", "hashCode"):
            continue
        if not _split_java_params(m.group("params") or ""):
            continue
        extracted = _extract_balanced_brace_body(blanked, m.end() - 1, quotes=('"', "'"))
        if extracted is None:
            continue
        body, _ = extracted
        bm = _JAVA_DUMMY_BODY_RE.match(body)
        if not bm:
            continue
        const_var = bm.group(1)
        ret_var = bm.group(2)
        if ret_var is not None and ret_var != const_var:
            continue
        line_no = blanked.count("\n", 0, m.start()) + 1
        hits.append((
            line_no,
            "DUMMY-CONSTANT-RETURN",
            f"dummy-return {fn_name}",
            f"Method `{fn_name}()` ignores its parameter(s) and returns a hardcoded constant.",
        ))
    return hits


def _extract_java_clone_blocks(blanked: str, min_tokens: int) -> list[tuple[int, str, str]]:
    blocks: list[tuple[int, str, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for m in _JAVA_METHOD_HEAD_RE.finditer(blanked):
        ret_head = (m.group("ret") or "").split("<", 1)[0].split("[", 1)[0].strip()
        if not ret_head or ret_head in _JAVA_NON_RET_PREFIXES:
            continue
        fn_name = m.group("name") or ""
        if not fn_name or fn_name in _JAVA_CTRL_KEYWORDS:
            continue
        open_idx = m.end() - 1
        extracted = _extract_balanced_brace_body(blanked, open_idx, quotes=('"', "'"))
        if extracted is None:
            continue
        body, end_pos = extracted
        span = (open_idx, end_pos)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        norm_body = _JAVA_STR_RE.sub("<STR>", body)
        tokens = _CLONE_TOKEN_RE.findall(norm_body)
        h = _alpha_hash_tokens(tokens, _JAVA_CLONE_KEEP_KEYWORDS, min_tokens)
        if h is not None:
            line_no = blanked.count("\n", 0, m.start()) + 1
            blocks.append((line_no, h, fn_name))
    return blocks


def _extract_clone_blocks_for_spec(
    raw_source: str, spec: FormatSpec, min_tokens: int
) -> list[tuple[int, str, str]]:
    if spec.detector_profile == "python":
        return _extract_python_clone_blocks(raw_source, min_tokens)
    if spec.detector_profile == "js_ts":
        if spec.name == "vue":
            blanked, _ = extract_vue_comments_and_blank(raw_source, mask_strings=True)
        else:
            blanked, _ = extract_js_ts_comments_and_blank(raw_source, mask_strings=True)
        return _extract_js_ts_clone_blocks(blanked, min_tokens)
    if spec.detector_profile == "java":
        blanked = spec.comment_blanker(raw_source)
        return _extract_java_clone_blocks(blanked, min_tokens)
    return []


def _is_type_test_path(rel_path: str) -> bool:
    name = Path(rel_path.replace("\\", "/").lower()).name
    return ".test-d." in name or ".spec-d." in name


def _scan_js_ts_suppressions(
    comments: list[tuple[int, str, str]],
    *,
    allow_bare_ts_expect_error: bool = False,
) -> list[tuple[int, str, str, str]]:
    """Detect lint/type-checker suppression escape directives inside real JS/TS/Vue comments."""
    hits: list[tuple[int, str, str, str]] = []
    for start_line, kind, body in comments:
        for offset, raw_line in enumerate(body.splitlines() or [""]):
            line_no = start_line + offset
            cleaned = raw_line.strip()
            if kind == "line":
                cleaned = cleaned.lstrip("/").strip()
            elif kind == "block":
                cleaned = cleaned.lstrip("*").strip()
            if not cleaned:
                continue

            if _TS_NOCHECK_RE.match(cleaned):
                hits.append((
                    line_no,
                    "SUPPRESSION-DIRECTIVE",
                    "@ts-nocheck",
                    "TypeScript `@ts-nocheck` suppresses type checking across the file.",
                ))
            elif _TS_IGNORE_RE.match(cleaned):
                hits.append((
                    line_no,
                    "SUPPRESSION-DIRECTIVE",
                    "@ts-ignore",
                    "TypeScript `@ts-ignore` silently hides errors without verification; fix the type or use a justified `@ts-expect-error`.",
                ))
            else:
                m_exp = _TS_EXPECT_ERROR_RE.match(cleaned)
                if m_exp:
                    if allow_bare_ts_expect_error:
                        continue
                    desc = m_exp.group(1).lstrip(" \t:-\u2013\u2014").strip()
                    if len(desc) < 3 or desc.lower() in _PLACEHOLDER_JUSTIFICATIONS:
                        hits.append((
                            line_no,
                            "SUPPRESSION-DIRECTIVE",
                            "@ts-expect-error (unjustified)",
                            "Unjustified `@ts-expect-error` without an explicit explanation (>= 3 chars).",
                        ))
                elif _ESLINT_DISABLE_RE.match(cleaned) or _ESLINT_INLINE_OFF_RE.match(cleaned):
                    sig = " ".join(cleaned.split())
                    hits.append((
                        line_no,
                        "SUPPRESSION-DIRECTIVE",
                        sig,
                        f"ESLint suppression directive `{sig}` weakens static enforcement.",
                    ))
    return hits

_JAVA_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(r"\b(?:Assertions\s*\.\s*)?assertTrue\s*\(\s*true\s*\)"),
        "Tautological `assertTrue(true)` proves nothing.",
    ),
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(r"\b(?:Assertions\s*\.\s*)?assertFalse\s*\(\s*false\s*\)"),
        "Tautological `assertFalse(false)` proves nothing.",
    ),
    (
        "SILENT-CATCH",
        re.compile(r"\bcatch\s*\([^)]*\b([A-Za-z_]\w*)\s*\)\s*\{\s*\}"),
        "Empty Java catch block silently swallows exceptions.",
    ),
    (
        "PLACEHOLDER-STUB",
        re.compile(r"\bthrow\s+new\s+UnsupportedOperationException\s*\("),
        "Placeholder `UnsupportedOperationException` stub.",
    ),
    (
        "EMPTY-TEST-STUB",
        re.compile(r"@Test\b[\s\w@()\".,=]*\bvoid\s+[A-Za-z_]\w*\s*\([^)]*\)\s*\{\s*\}"),
        "Empty `@Test` method body `{}`.",
    ),
)


def scan_source_defects(
    rel_path: str,
    raw_source: str,
    spec: FormatSpec,
    custom_rules: tuple[ForbiddenPatternRule, ...] = (),
    *,
    syntax_fallback_flag: list[bool] | None = None,
) -> list[tuple[int, str, str, str]]:
    """
    Scan source after stripping/blanking comments (4-invariant format contract).

    Returns list of `(line_no, rule_id, normalized_signature, message)`.
    """
    if spec.detector_profile == "js_ts" and spec.name == "vue":
        blanked, js_ts_comments = extract_vue_comments_and_blank(raw_source, mask_strings=True)
    elif spec.detector_profile == "js_ts" and spec.name in ("javascript", "typescript"):
        blanked, js_ts_comments = extract_js_ts_comments_and_blank(raw_source, mask_strings=True)
    else:
        blanked = spec.comment_blanker(raw_source)
        js_ts_comments = []

    occurrences: list[tuple[int, str, str, str]] = []

    if spec.detector_profile == "python":
        ast_hits = _scan_python_ast(rel_path, raw_source)
        if ast_hits is not None:
            occurrences.extend(ast_hits)
        else:
            if syntax_fallback_flag is not None:
                syntax_fallback_flag.append(True)
            for rule_id, pat, msg in _PY_FALLBACK_PATTERNS:
                for m in pat.finditer(blanked):
                    line_no = blanked.count("\n", 0, m.start()) + 1
                    sig = " ".join(m.group(0).split())
                    occurrences.append((line_no, rule_id, sig, msg))

    elif spec.detector_profile == "js_ts":
        if js_ts_comments:
            occurrences.extend(
                _scan_js_ts_suppressions(
                    js_ts_comments,
                    allow_bare_ts_expect_error=_is_type_test_path(rel_path),
                )
            )
        for rule_id, pat, msg in _JS_TS_PATTERNS:
            for m in pat.finditer(blanked):
                if rule_id == "SILENT-CATCH" and m.lastindex and m.group(1):
                    pname = m.group(1).lower()
                    if pname in ("ignored", "ignore", "_"):
                        continue
                line_no = blanked.count("\n", 0, m.start()) + 1
                sig = " ".join(m.group(0).split())
                occurrences.append((line_no, rule_id, sig, msg))
        if not _is_test_path(rel_path) and not rel_path.endswith(".d.ts"):
            occurrences.extend(_scan_js_ts_dummy_returns(blanked))
        if _is_test_path(rel_path):
            for m in _JS_TS_SKIPPED_TEST_RE.finditer(blanked):
                line_no = blanked.count("\n", 0, m.start()) + 1
                sig = " ".join(m.group(0).split())
                occurrences.append((
                    line_no,
                    "EMPTY-TEST-STUB",
                    sig,
                    "Skipped or todo JS/TS test placeholder.",
                ))
            for m in _JS_TS_FOCUSED_TEST_RE.finditer(blanked):
                line_no = blanked.count("\n", 0, m.start()) + 1
                sig = " ".join(m.group(0).split())
                occurrences.append((
                    line_no,
                    "FOCUSED-TEST",
                    sig,
                    "Focused test (`.only`/`fit`/`fdescribe`) silently skips the rest of the suite.",
                ))
            occurrences.extend(_scan_js_ts_noop_tests(blanked))

    elif spec.detector_profile == "java":
        for rule_id, pat, msg in _JAVA_PATTERNS:
            for m in pat.finditer(blanked):
                if rule_id == "SILENT-CATCH" and m.lastindex:
                    pname = m.group(1).lower()
                    if pname in ("ignored", "ignore"):
                        continue
                line_no = blanked.count("\n", 0, m.start()) + 1
                sig = " ".join(m.group(0).split())
                occurrences.append((line_no, rule_id, sig, msg))
        if not _is_test_path(rel_path):
            occurrences.extend(_scan_java_dummy_returns(blanked))

    for rule in custom_rules:
        if rule.paths and not _matches_any_glob(rel_path, rule.paths):
            continue
        compiled = re.compile(rule.pattern)
        for m in compiled.finditer(blanked):
            line_no = blanked.count("\n", 0, m.start()) + 1
            sig = " ".join(m.group(0).split())
            occurrences.append((line_no, rule.rule_id, sig, rule.message))

    return occurrences


def _git_show_text(repo_root: Path, rev: str, rel_path: str) -> str:
    res = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", f"{rev}:{rel_path}"],
        cwd=str(repo_root),
        capture_output=True,
    )
    if res.returncode != 0:
        return ""
    return res.stdout.decode("utf-8", errors="replace")


def _resolve_compare_ref(repo_root: Path, baseline: str | None) -> str | None:
    if not is_git_repo(repo_root) or not git_has_head(repo_root):
        return None
    if baseline:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", baseline],
            cwd=str(repo_root),
            capture_output=True,
        )
        if check.returncode == 0:
            return baseline
    return "HEAD"


def _collect_candidate_files(repo_root: Path, baseline_ref: str | None) -> list[str]:
    files: set[str] = set()
    if is_git_repo(repo_root):
        for status_code, rel_path in git_status_entries(repo_root):
            if "D" in status_code:
                continue
            files.add(rel_path)
        if baseline_ref and baseline_ref != "HEAD":
            res = subprocess.run(
                ["git", "-c", "core.quotepath=false", "diff", "--name-only", f"{baseline_ref}..HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if line.strip():
                        files.add(line.strip().replace("\\", "/"))
    return sorted(files)


def _matching_ratchet_limit(
    entries: list[RatchetEntry], rel_path: str, rule_id: str, sig: str
) -> int | None:
    best: int | None = None
    for entry in entries:
        file_ok = entry.file in ("*", "") or fnmatch(rel_path, entry.file)
        rule_ok = entry.rule in ("*", "") or entry.rule == rule_id
        sig_ok = (not entry.signature) or entry.signature == sig
        if file_ok and rule_ok and sig_ok:
            best = entry.count if best is None else max(best, entry.count)
    return best


@dataclass(frozen=True)
class DiffScanStats:
    scanned: int = 0
    skipped_ignored: int = 0
    skipped_unsupported: int = 0
    syntax_fallback: int = 0
    shallow_history: bool = False


def verify_incremental_diff_with_stats(
    repo_root: Path,
    config: UnrigConfig,
    baseline_override: str | None = None,
) -> tuple[list[DiffFinding], list[str], list[str], DiffScanStats]:
    """
    Verify incremental workspace changes using CounterMultiset(after - before),
    cross-file isomorphic clone detection, and `.unrig/ratchet.json` enforcement.

    Returns `(findings, ratchet_errors, clone_warnings, stats)`.
    """
    ratchet_entries, ratchet_errors = load_and_validate_ratchet(repo_root, config.ratchet_file)
    findings: list[DiffFinding] = []
    clone_warnings: list[str] = []

    if not config.diff_checks.enabled:
        return findings, ratchet_errors, clone_warnings, DiffScanStats()

    requested_baseline = baseline_override or config.diff_checks.baseline
    compare_ref = _resolve_compare_ref(repo_root, requested_baseline)
    shallow_history = bool(
        requested_baseline
        and compare_ref != requested_baseline
        and (repo_root / ".git" / "shallow").exists()
    )
    candidates = _collect_candidate_files(repo_root, compare_ref)
    if not candidates and not is_git_repo(repo_root):
        for p in repo_root.rglob("*"):
            if p.is_file():
                candidates.append(p.relative_to(repo_root).as_posix())

    scanned = 0
    skipped_ignored = 0
    skipped_unsupported = 0
    syntax_fallback = 0

    clone_min_tokens = config.diff_checks.clone_min_tokens
    clone_warn_thr = config.diff_checks.clone_warn_threshold
    clone_block_thr = config.diff_checks.clone_block_threshold
    net_clone_delta: Counter[str] = Counter()
    after_clone_total: Counter[str] = Counter()
    clone_locations: dict[str, list[tuple[str, int, str]]] = {}

    for rel_path in candidates:
        if rel_path.startswith((".git/", ".unrig/")):
            continue
        spec = get_format_for_path(rel_path)
        if spec is None:
            skipped_unsupported += 1
            continue
        all_ignores = config.diff_checks.ignore_globs + spec.default_ignore_globs
        if _matches_any_glob(rel_path, all_ignores):
            skipped_ignored += 1
            continue

        full_path = repo_root / rel_path
        if not full_path.exists() or not full_path.is_file():
            continue

        scanned += 1
        after_src = full_path.read_text(encoding="utf-8", errors="replace")
        before_src = _git_show_text(repo_root, compare_ref, rel_path) if compare_ref else ""

        fallback_flag: list[bool] = []
        after_occs = scan_source_defects(
            rel_path,
            after_src,
            spec,
            config.diff_checks.forbidden_patterns,
            syntax_fallback_flag=fallback_flag,
        )
        if fallback_flag:
            syntax_fallback += 1
        before_occs = (
            scan_source_defects(rel_path, before_src, spec, config.diff_checks.forbidden_patterns)
            if before_src
            else []
        )

        # Incremental Alpha-Normalized clone tracking
        if clone_min_tokens > 0 and (clone_warn_thr > 0 or clone_block_thr > 0):
            after_blocks = _extract_clone_blocks_for_spec(after_src, spec, clone_min_tokens)
            before_blocks = (
                _extract_clone_blocks_for_spec(before_src, spec, clone_min_tokens)
                if before_src
                else []
            )
            file_after_hashes = Counter(h for _, h, _ in after_blocks)
            file_before_hashes = Counter(h for _, h, _ in before_blocks)
            file_delta = file_after_hashes - file_before_hashes
            after_clone_total += file_after_hashes
            if file_delta:
                net_clone_delta += file_delta
                for ln, h, lbl in after_blocks:
                    if h in file_delta:
                        clone_locations.setdefault(h, []).append((rel_path, ln, lbl))

        after_counter: Counter[tuple[str, str]] = Counter((r, s) for _, r, s, _ in after_occs)
        before_counter: Counter[tuple[str, str]] = Counter((r, s) for _, r, s, _ in before_occs)

        # CounterMultiset(after - before): strictly blocks any duplicated/new occurrence (MUT-W02)
        delta_counter = after_counter - before_counter
        if not delta_counter:
            continue

        # Locate latest line numbers in `after_occs` for each violating `(rule_id, sig)`
        by_key: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for line_no, rule_id, sig, msg in after_occs:
            by_key.setdefault((rule_id, sig), []).append((line_no, msg))

        for (rule_id, sig), delta_count in sorted(delta_counter.items()):
            after_total = after_counter[(rule_id, sig)]
            ratchet_cap = _matching_ratchet_limit(ratchet_entries, rel_path, rule_id, sig)
            if ratchet_cap is not None and after_total <= ratchet_cap:
                continue
            locs = by_key.get((rule_id, sig), [(1, rule_id)])
            # Prefer reporting the newly added tail occurrence(s)
            for line_no, msg in locs[-delta_count:]:
                findings.append(
                    DiffFinding(
                        rule_id=rule_id,
                        rel_path=rel_path,
                        line=line_no,
                        signature=sig,
                        message=f"{msg} (delta=+{delta_count}, after={after_total}, before={before_counter[(rule_id, sig)]})",
                    )
                )

    # Evaluate aggregated net-new isomorphic clone clusters
    for alpha_hash, delta_count in sorted(net_clone_delta.items()):
        locs = clone_locations.get(alpha_hash, [])
        sample_file, sample_line, sample_lbl = locs[0] if locs else ("*", 1, "block")
        sig = f"clone:{alpha_hash}"
        if clone_block_thr > 0 and delta_count >= clone_block_thr:
            ratchet_cap = _matching_ratchet_limit(ratchet_entries, sample_file, "CLONE-SPAM", sig)
            if ratchet_cap is not None and after_clone_total[alpha_hash] <= ratchet_cap:
                continue
            findings.append(
                DiffFinding(
                    rule_id="CLONE-SPAM",
                    rel_path=sample_file,
                    line=sample_line,
                    signature=sig,
                    message=(
                        f"{delta_count} net-new isomorphic blocks share normalized structure `{sig}` "
                        f"(e.g. `{sample_lbl}`, >= {clone_block_thr}); consolidate via `@pytest.mark.parametrize` / "
                        "`it.each` or a shared helper (or justify in `.unrig/ratchet.json`)."
                    ),
                )
            )
        elif clone_warn_thr > 0 and delta_count >= clone_warn_thr:
            clone_warnings.append(
                f"[CLONE-SPAM] {sample_file}:{sample_line} -> {delta_count} net-new isomorphic blocks "
                f"share normalized structure `{sig}` (`{sample_lbl}`); consider parameterizing or extracting a shared helper."
            )

    stats = DiffScanStats(
        scanned=scanned,
        skipped_ignored=skipped_ignored,
        skipped_unsupported=skipped_unsupported,
        syntax_fallback=syntax_fallback,
        shallow_history=shallow_history,
    )
    return findings, ratchet_errors, clone_warnings, stats


def verify_incremental_diff(
    repo_root: Path,
    config: UnrigConfig,
    baseline_override: str | None = None,
) -> tuple[list[DiffFinding], list[str], int]:
    """
    Verify incremental workspace changes using CounterMultiset(after - before)
    and `.unrig/ratchet.json` enforcement.

    Returns `(findings, ratchet_errors, scanned_file_count)`.
    """
    findings, ratchet_errors, _, stats = verify_incremental_diff_with_stats(
        repo_root, config, baseline_override=baseline_override
    )
    return findings, ratchet_errors, stats.scanned


@dataclass(frozen=True)
class RatchetSlack:
    index: int
    entry: RatchetEntry
    actual_count: int


def _count_clone_blocks_for_ratchet(
    repo_root: Path,
    config: UnrigConfig,
    entry: RatchetEntry,
) -> int:
    """Count matching Alpha-Normalized clone blocks in `entry.file` and across same-profile files when needed."""
    target = repo_root / entry.file
    if not target.exists() or not target.is_file():
        return 0
    spec = get_format_for_path(entry.file)
    if spec is None:
        return 0

    sig_prefix = "clone:"
    target_hash = entry.signature[len(sig_prefix):] if entry.signature.startswith(sig_prefix) else ""
    min_tokens = config.diff_checks.clone_min_tokens
    if min_tokens <= 0:
        return 0

    def _count_in_file(file_path: Path, fspec: FormatSpec) -> int:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        blocks = _extract_clone_blocks_for_spec(text, fspec, min_tokens)
        if target_hash:
            return sum(1 for _, h, _ in blocks if h == target_hash)
        return len(blocks)

    in_target = _count_in_file(target, spec)
    if in_target == 0 or in_target >= entry.count:
        return in_target

    # Cross-file CLONE-SPAM cluster anchored at `entry.file`: count across non-ignored workspace files
    # until `entry.count` is reached so valid cross-file ratchets are not falsely flagged as slack.
    total = in_target
    all_ignores = config.diff_checks.ignore_globs + spec.default_ignore_globs
    for candidate in repo_root.rglob("*"):
        if not candidate.is_file() or candidate.resolve() == target.resolve():
            continue
        rel = candidate.relative_to(repo_root).as_posix()
        if rel.startswith((".git/", ".unrig/")):
            continue
        cspec = get_format_for_path(rel)
        if cspec is None or cspec.detector_profile != spec.detector_profile:
            continue
        if _matches_any_glob(rel, all_ignores):
            continue
        total += _count_in_file(candidate, cspec)
        if total >= entry.count:
            return total
    return total


def find_ratchet_slack(
    repo_root: Path,
    config: UnrigConfig,
) -> tuple[list[RatchetSlack], list[str]]:
    """Detect concrete-file `.unrig/ratchet.json` entries whose cap exceeds current workspace count."""
    entries, errors = load_and_validate_ratchet(repo_root, config.ratchet_file)
    if errors or not entries:
        return [], errors

    slacks: list[RatchetSlack] = []
    scan_cache: dict[str, list[tuple[int, str, str, str]]] = {}

    for idx, entry in enumerate(entries):
        if not entry.file or any(ch in entry.file for ch in "*?["):
            continue
        if entry.count <= 0:
            continue

        if entry.rule == "CLONE-SPAM":
            actual_clone_count = _count_clone_blocks_for_ratchet(repo_root, config, entry)
            if actual_clone_count < entry.count:
                slacks.append(RatchetSlack(index=idx, entry=entry, actual_count=actual_clone_count))
            continue

        target = repo_root / entry.file
        if not target.exists():
            slacks.append(RatchetSlack(index=idx, entry=entry, actual_count=0))
            continue
        if not target.is_file():
            continue

        spec = get_format_for_path(entry.file)
        if spec is None:
            continue

        if entry.file not in scan_cache:
            src = target.read_text(encoding="utf-8", errors="replace")
            scan_cache[entry.file] = scan_source_defects(
                entry.file, src, spec, config.diff_checks.forbidden_patterns
            )

        occs = scan_cache[entry.file]
        actual_count = sum(
            1
            for _, rule_id, sig, _ in occs
            if (entry.rule in ("*", "") or rule_id == entry.rule)
            and (not entry.signature or sig == entry.signature)
        )
        if actual_count < entry.count:
            slacks.append(RatchetSlack(index=idx, entry=entry, actual_count=actual_count))

    return slacks, []


def prune_ratchet(
    repo_root: Path,
    config: UnrigConfig,
    dry_run: bool = False,
) -> tuple[list[RatchetSlack], list[str]]:
    """Tighten concrete `.unrig/ratchet.json` caps to match current workspace counts (and drop 0s)."""
    slacks, errors = find_ratchet_slack(repo_root, config)
    if errors or not slacks or dry_run:
        return slacks, errors

    ratchet_path = repo_root / config.ratchet_file
    data = json.loads(ratchet_path.read_text(encoding="utf-8"))
    by_idx = {s.index: s for s in slacks}
    new_entries: list[dict[str, object]] = []

    for idx, item in enumerate(data.get("entries", [])):
        if idx not in by_idx:
            new_entries.append(item)
            continue
        actual = by_idx[idx].actual_count
        if actual <= 0:
            continue
        updated = dict(item)
        if "max_count" in updated and "count" not in updated:
            updated["max_count"] = actual
        else:
            updated["count"] = actual
        new_entries.append(updated)

    data["entries"] = new_entries
    ratchet_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return slacks, []
