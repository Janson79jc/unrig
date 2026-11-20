"""Pure-stdlib configuration loader and dataclasses for `.unrig.yaml`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
from typing import Any


class UnrigConfigError(ValueError):
    """Raised when `.unrig.yaml` contains invalid syntax or schema values."""


DEFAULT_PROTECTED_L0_FILES = (
    ".unrig.yaml",
    ".unrig/ratchet.json",
    ".unrig/PROTOCOL.md",
    ".unrig/blind_auditor_prompt.md",
)

DEFAULT_JUNIT_GLOBS = (
    "**/target/surefire-reports/TEST-*.xml",
    "**/test-results/*.xml",
    "**/junit*.xml",
    "**/*-junit.xml",
)

DEFAULT_PRIVILEGE_TOKENS = (
    "--no-verify",
    "gate.mode=maintenance",
)

DEFAULT_IGNORE_GLOBS = (
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/target/**",
    "**/.venv/**",
)


@dataclass(frozen=True)
class ReportsConfig:
    require_reports: bool = False
    forbid_skipped: bool = True
    junit_globs: tuple[str, ...] = DEFAULT_JUNIT_GLOBS
    required_tests: dict[str, tuple[str, ...]] = field(default_factory=dict)
    require_fresh_against: tuple[str, ...] = ()
    privilege_log_tokens: tuple[str, ...] = DEFAULT_PRIVILEGE_TOKENS


@dataclass(frozen=True)
class RuntimeArtifactRule:
    path: str
    max_age_seconds: int | None = None


@dataclass(frozen=True)
class ForbiddenPatternRule:
    rule_id: str
    pattern: str
    message: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiffChecksConfig:
    enabled: bool = True
    baseline: str = "HEAD~1"
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_GLOBS
    forbidden_patterns: tuple[ForbiddenPatternRule, ...] = ()


@dataclass(frozen=True)
class UnrigConfig:
    version: int = 1
    protected_l0_files: tuple[str, ...] = DEFAULT_PROTECTED_L0_FILES
    reports: ReportsConfig = field(default_factory=ReportsConfig)
    runtime_proof_artifacts: tuple[RuntimeArtifactRule, ...] = ()
    diff_checks: DiffChecksConfig = field(default_factory=DiffChecksConfig)
    ratchet_file: str = ".unrig/ratchet.json"
    shell_write_redirect_guards: bool = True


def _strip_inline_comment(line: str) -> str:
    """Strip `# comment` outside single/double quotes."""
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (idx == 0 or line[idx - 1].isspace()):
            return line[:idx].rstrip()
    return line.rstrip()


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    for ch in text:
        if escaped:
            cur.append(ch)
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            cur.append(ch)
            escaped = True
            continue
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch in ("[", "{"):
            depth += 1
        elif ch in ("]", "}"):
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scalar(raw: str) -> Any:
    val = raw.strip()
    if val == "" or val in ("null", "Null", "NULL", "~"):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val[1:-1]
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1].replace("''", "'")
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_top_level(inner, ",")]
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for item in _split_top_level(inner, ","):
            if ":" not in item:
                raise UnrigConfigError(f"Invalid inline mapping item: {item!r}")
            k, v = item.split(":", 1)
            out[str(_parse_scalar(k))] = _parse_scalar(v)
        return out
    if val.lstrip("-").isdigit() and not (val.startswith("0") and len(val) > 1):
        return int(val)
    return val


def _find_key_value_sep(content: str) -> int:
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(content):
        if escaped:
            escaped = False
            continue
        if quote == '"' and ch == "\\":
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == ":" and (idx + 1 == len(content) or content[idx + 1].isspace()):
            return idx
    return -1


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the clean YAML subset used by `.unrig.yaml` using pure stdlib."""
    tokens: list[tuple[int, int, str]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]:
            raise UnrigConfigError(f"Line {lineno}: tabs are not allowed for YAML indentation.")
        cleaned = _strip_inline_comment(raw_line)
        if not cleaned.strip():
            continue
        if cleaned.strip() in ("---", "..."):
            continue
        indent = len(cleaned) - len(cleaned.lstrip(" "))
        tokens.append((lineno, indent, cleaned.strip()))

    if not tokens:
        return {}

    def parse_block(index: int, expected_indent: int) -> tuple[Any, int]:
        if index >= len(tokens):
            return {}, index
        _, indent, text_tok = tokens[index]
        if text_tok.startswith("- ") or text_tok == "-":
            return parse_list(index, indent)
        return parse_map(index, indent)

    def parse_list(index: int, list_indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(tokens):
            lineno, indent, tok = tokens[index]
            if indent < list_indent:
                break
            if indent > list_indent:
                raise UnrigConfigError(f"Line {lineno}: unexpected indentation in list.")
            if not (tok.startswith("- ") or tok == "-"):
                break
            after_dash = tok[1:].strip() if tok != "-" else ""
            if not after_dash:
                if index + 1 < len(tokens) and tokens[index + 1][1] > list_indent:
                    nested, index = parse_block(index + 1, tokens[index + 1][1])
                    items.append(nested)
                else:
                    items.append(None)
                    index += 1
                continue
            sep = _find_key_value_sep(after_dash)
            if sep != -1:
                key = str(_parse_scalar(after_dash[:sep]))
                rest = after_dash[sep + 1 :].strip()
                item_dict: dict[str, Any] = {}
                if rest:
                    item_dict[key] = _parse_scalar(rest)
                    index += 1
                elif index + 1 < len(tokens) and tokens[index + 1][1] > list_indent + 1:
                    nested, index = parse_block(index + 1, tokens[index + 1][1])
                    item_dict[key] = nested
                else:
                    item_dict[key] = None
                    index += 1
                if index < len(tokens) and tokens[index][1] > list_indent:
                    extra_map, index = parse_map(index, tokens[index][1])
                    item_dict.update(extra_map)
                items.append(item_dict)
            else:
                items.append(_parse_scalar(after_dash))
                index += 1
        return items, index

    def parse_map(index: int, map_indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(tokens):
            lineno, indent, tok = tokens[index]
            if indent < map_indent:
                break
            if indent > map_indent:
                raise UnrigConfigError(f"Line {lineno}: unexpected indentation in mapping.")
            if tok.startswith("- ") or tok == "-":
                break
            sep = _find_key_value_sep(tok)
            if sep == -1:
                raise UnrigConfigError(f"Line {lineno}: expected 'key: value', got {tok!r}.")
            key = str(_parse_scalar(tok[:sep]))
            rest = tok[sep + 1 :].strip()
            if rest:
                mapping[key] = _parse_scalar(rest)
                index += 1
            else:
                if index + 1 < len(tokens):
                    next_indent = tokens[index + 1][1]
                    next_tok = tokens[index + 1][2]
                    if next_indent > map_indent or (
                        next_indent == map_indent and (next_tok.startswith("- ") or next_tok == "-")
                    ):
                        nested, index = parse_block(index + 1, next_indent)
                        mapping[key] = nested
                        continue
                mapping[key] = {}
                index += 1
        return mapping, index

    result, next_idx = parse_block(0, tokens[0][1])
    if next_idx != len(tokens):
        lineno = tokens[next_idx][0]
        raise UnrigConfigError(f"Line {lineno}: trailing unparsed YAML content.")
    if not isinstance(result, dict):
        raise UnrigConfigError("Root of `.unrig.yaml` must be a mapping.")
    return result


def normalize_rel_path(path_str: str) -> str:
    """Normalize slashes and strip leading `./` without trimming dotfile prefixes like `.unrig`."""
    cleaned = path_str.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _norm_rel(path_str: str) -> str:
    return normalize_rel_path(path_str)


def _as_str_tuple(raw: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return fallback
    if not isinstance(raw, list):
        raise UnrigConfigError(f"Expected list of strings, got {type(raw).__name__}.")
    return tuple(str(x) for x in raw if x is not None and str(x).strip())


def load_config(repo_root: Path, config_path: Path | None = None) -> UnrigConfig:
    """Load `.unrig.yaml` from repo root (falls back to clean defaults if absent)."""
    target = config_path or (repo_root / ".unrig.yaml")
    if not target.exists():
        return UnrigConfig()

    raw_text = target.read_text(encoding="utf-8")
    data = parse_simple_yaml(raw_text)

    version = int(data.get("version", 1))
    user_protected = _as_str_tuple(data.get("protected_l0_files"), ())
    protected = tuple(
        dict.fromkeys(_norm_rel(p) for p in (*DEFAULT_PROTECTED_L0_FILES, *user_protected))
    )

    rep_raw = data.get("reports") or {}
    if not isinstance(rep_raw, dict):
        raise UnrigConfigError("`reports` must be a mapping.")
    req_tests_raw = rep_raw.get("required_tests") or {}
    if not isinstance(req_tests_raw, dict):
        raise UnrigConfigError("`reports.required_tests` must be a mapping.")
    req_tests = {
        str(suite): tuple(str(m) for m in (methods or []))
        for suite, methods in req_tests_raw.items()
    }
    reports_cfg = ReportsConfig(
        require_reports=bool(rep_raw.get("require_reports", False)),
        forbid_skipped=bool(rep_raw.get("forbid_skipped", True)),
        junit_globs=_as_str_tuple(rep_raw.get("junit_globs"), DEFAULT_JUNIT_GLOBS),
        required_tests=req_tests,
        require_fresh_against=_as_str_tuple(rep_raw.get("require_fresh_against"), ()),
        privilege_log_tokens=_as_str_tuple(rep_raw.get("privilege_log_tokens"), DEFAULT_PRIVILEGE_TOKENS),
    )

    artifacts_raw = data.get("runtime_proof_artifacts") or []
    if not isinstance(artifacts_raw, list):
        raise UnrigConfigError("`runtime_proof_artifacts` must be a list.")
    artifacts: list[RuntimeArtifactRule] = []
    for item in artifacts_raw:
        if isinstance(item, str):
            artifacts.append(RuntimeArtifactRule(path=_norm_rel(item)))
        elif isinstance(item, dict) and item.get("path"):
            age = item.get("max_age_seconds")
            artifacts.append(
                RuntimeArtifactRule(
                    path=_norm_rel(str(item["path"])),
                    max_age_seconds=int(age) if age is not None else None,
                )
            )
        else:
            raise UnrigConfigError(f"Invalid `runtime_proof_artifacts` entry: {item!r}")

    diff_raw = data.get("diff_checks") or {}
    if not isinstance(diff_raw, dict):
        raise UnrigConfigError("`diff_checks` must be a mapping.")
    forb_raw = diff_raw.get("forbidden_patterns") or []
    if not isinstance(forb_raw, list):
        raise UnrigConfigError("`diff_checks.forbidden_patterns` must be a list.")
    forb_rules: list[ForbiddenPatternRule] = []
    for item in forb_raw:
        if not isinstance(item, dict) or not item.get("pattern"):
            raise UnrigConfigError(f"Invalid `forbidden_patterns` entry: {item!r}")
        forb_rules.append(
            ForbiddenPatternRule(
                rule_id=str(item.get("id") or "CUSTOM-RULE"),
                pattern=str(item["pattern"]),
                message=str(item.get("message") or f"Matched forbidden pattern {item['pattern']}"),
                paths=_as_str_tuple(item.get("paths"), ()),
            )
        )

    diff_cfg = DiffChecksConfig(
        enabled=bool(diff_raw.get("enabled", True)),
        baseline=str(diff_raw.get("baseline", "HEAD~1")),
        ignore_globs=_as_str_tuple(diff_raw.get("ignore_globs"), DEFAULT_IGNORE_GLOBS),
        forbidden_patterns=tuple(forb_rules),
    )

    return UnrigConfig(
        version=version,
        protected_l0_files=protected,
        reports=reports_cfg,
        runtime_proof_artifacts=tuple(artifacts),
        diff_checks=diff_cfg,
        ratchet_file=_norm_rel(str(data.get("ratchet_file", ".unrig/ratchet.json"))),
        shell_write_redirect_guards=bool(data.get("shell_write_redirect_guards", True)),
    )
