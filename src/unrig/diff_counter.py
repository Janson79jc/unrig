"""Incremental CounterMultiset(after - before) defect detector & ratchet verifier."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable

from .config import ForbiddenPatternRule, UnrigConfig, normalize_rel_path
from .format_registry import FormatSpec, get_format_for_path
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


def _scan_python_ast(
    rel_path: str, raw_source: str
) -> list[tuple[int, str, str, str]] | None:
    """Return list of `(line_no, rule_id, signature, message)` or `None` on SyntaxError."""
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
                    has_verification = False
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Assert):
                            has_verification = True
                            break
                        if isinstance(sub, ast.Call):
                            cn = _call_name(sub.func).lower()
                            if (
                                cn.startswith(("assert", "verify", "check", "expect"))
                                or cn in ("raises", "fail", "warns", "deprecated_call")
                            ):
                                has_verification = True
                                break
                    if not has_verification:
                        occurrences.append((
                            node.lineno,
                            "EMPTY-TEST-STUB",
                            f"unverified test {node.name}",
                            f"Test `{node.name}()` contains no assertions or verification calls.",
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

_JS_TS_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(r"\bexpect\s*\(\s*true\s*\)\s*\.\s*(?:toBe\s*\(\s*true\s*\)|toBeTruthy\s*\(\s*\))"),
        "Tautological `expect(true)` assertion.",
    ),
    (
        "TAUTOLOGICAL-ASSERT",
        re.compile(r"\bassert\s*\.\s*(?:ok|strictEqual)\s*\(\s*true(?:\s*,\s*true)?\s*\)"),
        "Tautological `assert.ok(true)` assertion.",
    ),
    (
        "EMPTY-TEST-STUB",
        re.compile(r"\b(?:it|test)\s*\([^,]+,\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>\s*\{\s*\}\s*\)"),
        "Empty JS/TS test callback `{}`.",
    ),
    (
        "SILENT-CATCH",
        re.compile(r"\bcatch\s*\(\s*([A-Za-z_$]\w*)\s*\)\s*\{\s*\}"),
        "Empty catch block silently swallows exceptions.",
    ),
)

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
) -> list[tuple[int, str, str, str]]:
    """
    Scan source after stripping/blanking comments (invariant #3600).

    Returns list of `(line_no, rule_id, normalized_signature, message)`.
    """
    blanked = spec.comment_blanker(raw_source)
    occurrences: list[tuple[int, str, str, str]] = []

    if spec.detector_profile == "python":
        ast_hits = _scan_python_ast(rel_path, raw_source)
        if ast_hits is not None:
            occurrences.extend(ast_hits)
        else:
            for rule_id, pat, msg in _PY_FALLBACK_PATTERNS:
                for m in pat.finditer(blanked):
                    line_no = blanked.count("\n", 0, m.start()) + 1
                    sig = " ".join(m.group(0).split())
                    occurrences.append((line_no, rule_id, sig, msg))

    elif spec.detector_profile == "js_ts":
        for rule_id, pat, msg in _JS_TS_PATTERNS:
            for m in pat.finditer(blanked):
                if rule_id == "SILENT-CATCH" and m.lastindex:
                    pname = m.group(1).lower()
                    if pname in ("ignored", "ignore", "_"):
                        continue
                line_no = blanked.count("\n", 0, m.start()) + 1
                sig = " ".join(m.group(0).split())
                occurrences.append((line_no, rule_id, sig, msg))

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
    ratchet_entries, ratchet_errors = load_and_validate_ratchet(repo_root, config.ratchet_file)
    findings: list[DiffFinding] = []

    if not config.diff_checks.enabled:
        return findings, ratchet_errors, 0

    compare_ref = _resolve_compare_ref(repo_root, baseline_override or config.diff_checks.baseline)
    candidates = _collect_candidate_files(repo_root, compare_ref)
    if not candidates and not is_git_repo(repo_root):
        for p in repo_root.rglob("*"):
            if p.is_file():
                candidates.append(p.relative_to(repo_root).as_posix())

    scanned = 0
    for rel_path in candidates:
        if rel_path.startswith((".git/", ".unrig/")):
            continue
        spec = get_format_for_path(rel_path)
        if spec is None:
            continue
        all_ignores = config.diff_checks.ignore_globs + spec.default_ignore_globs
        if _matches_any_glob(rel_path, all_ignores):
            continue

        full_path = repo_root / rel_path
        if not full_path.exists() or not full_path.is_file():
            continue

        scanned += 1
        after_src = full_path.read_text(encoding="utf-8", errors="replace")
        before_src = _git_show_text(repo_root, compare_ref, rel_path) if compare_ref else ""

        after_occs = scan_source_defects(rel_path, after_src, spec, config.diff_checks.forbidden_patterns)
        before_occs = (
            scan_source_defects(rel_path, before_src, spec, config.diff_checks.forbidden_patterns)
            if before_src
            else []
        )

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

    return findings, ratchet_errors, scanned
