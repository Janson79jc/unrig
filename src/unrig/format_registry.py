"""Format Registry: 4-invariant language-aware comment blankers preserving exact line numbers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


_REGEX_PREFIX_KEYWORDS = frozenset({
    "return",
    "throw",
    "case",
    "default",
    "delete",
    "void",
    "typeof",
    "yield",
    "await",
    "in",
    "instanceof",
    "new",
    "else",
    "do",
    "of",
    "export",
})

_REGEX_PREFIX_CHARS = frozenset("=([{,;?:!&|^~+-*%<>")


def _scan_js_regex_end(source: str, slash_idx: int) -> int | None:
    """Return end index after `/pattern/flags` if valid single-line JS/TS regex, else None."""
    n = len(source)
    j = slash_idx + 1
    in_char_class = False
    while j < n:
        c = source[j]
        if c in ("\n", "\r"):
            return None
        if c == "\\":
            if j + 1 < n and source[j + 1] not in ("\n", "\r"):
                j += 2
                continue
            return None
        if c == "[":
            in_char_class = True
            j += 1
            continue
        if c == "]":
            in_char_class = False
            j += 1
            continue
        if c == "/" and not in_char_class:
            j += 1
            while j < n and source[j].isalpha():
                j += 1
            return j
        j += 1
    return None


def extract_js_ts_comments_and_blank(
    source: str,
    start_line: int = 1,
    *,
    mask_strings: bool = False,
) -> tuple[str, list[tuple[int, str, str]]]:
    """
    Blank JS/TS `//` and `/* ... */` comments with 1:1 newline preservation while
    respecting quotes, nested template `${...}` expressions, and `/regex/` literals.
    When `mask_strings=True`, string/template literal text interiors (keeping delimiters
    and newlines) are also blanked so code pattern scanners avoid string false positives.

    Returns `(blanked_source, comments)` where each comment is `(line_no, kind, raw_body)`
    with `kind` in `{"line", "block"}`.
    """
    out: list[str] = []
    comments: list[tuple[int, str, str]] = []
    n = len(source)
    i = 0
    line_no = start_line
    quote: str | None = None
    mode_stack: list[str | int] = []
    can_start_regex = True

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        # 1. Inside `"..."` or `'...'` string literal
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                esc = source[i + 1]
                if esc == "\n":
                    out.append(" \n" if mask_strings else "\\\n")
                    line_no += 1
                    i += 2
                    continue
                if esc == "\r" and i + 2 < n and source[i + 2] == "\n":
                    out.append(" \r\n" if mask_strings else "\\\r\n")
                    line_no += 1
                    i += 3
                    continue
                out.append("  " if mask_strings else ch + esc)
                i += 2
                continue
            if ch == "\n":
                out.append(ch)
                line_no += 1
                quote = None
                can_start_regex = True
            elif ch == quote:
                out.append(ch)
                quote = None
                can_start_regex = False
            else:
                out.append(" " if (mask_strings and ch != "\r") else ch)
            i += 1
            continue

        # 2. Inside raw text portion of a template literal `` `...` ``
        if mode_stack and mode_stack[-1] == "TEMPLATE":
            if ch == "\\" and i + 1 < n:
                esc = source[i + 1]
                if esc == "\n":
                    out.append(" \n" if mask_strings else "\\\n")
                    line_no += 1
                else:
                    out.append("  " if mask_strings else ch + esc)
                i += 2
                continue
            if ch == "$" and nxt == "{":
                out.append("${")
                mode_stack.append(0)
                can_start_regex = True
                i += 2
                continue
            if ch == "`":
                out.append("`")
                mode_stack.pop()
                can_start_regex = False
                i += 1
                continue
            if ch == "\n":
                line_no += 1
                out.append(ch)
            else:
                out.append(" " if (mask_strings and ch != "\r") else ch)
            i += 1
            continue

        # 3. Line comment `// ...`
        if ch == "/" and nxt == "/":
            c_start_line = line_no
            out.append("  ")
            i += 2
            body_chars: list[str] = []
            while i < n and source[i] not in ("\n", "\r"):
                body_chars.append(source[i])
                out.append(" ")
                i += 1
            comments.append((c_start_line, "line", "".join(body_chars)))
            continue

        # 4. Block comment `/* ... */`
        if ch == "/" and nxt == "*":
            c_start_line = line_no
            out.append("  ")
            i += 2
            body_chars = []
            while i < n:
                c = source[i]
                c_nxt = source[i + 1] if i + 1 < n else ""
                if c == "*" and c_nxt == "/":
                    out.append("  ")
                    i += 2
                    break
                if c in ("\n", "\r"):
                    out.append(c)
                    body_chars.append(c)
                    if c == "\n":
                        line_no += 1
                else:
                    out.append(" ")
                    body_chars.append(c)
                i += 1
            comments.append((c_start_line, "block", "".join(body_chars)))
            continue

        # 5. Template literal start `` ` ``
        if ch == "`":
            out.append("`")
            mode_stack.append("TEMPLATE")
            i += 1
            continue

        # 6. Single / double quote string start
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue

        # 7. Whitespace (preserves `can_start_regex`)
        if ch in (" ", "\t", "\r", "\n"):
            out.append(ch)
            if ch == "\n":
                line_no += 1
            i += 1
            continue

        # 8. Regex literal vs division `/`
        if ch == "/":
            prev_ch = source[i - 1] if i > 0 else ""
            if can_start_regex and prev_ch != "<":
                regex_end = _scan_js_regex_end(source, i)
                if regex_end is not None:
                    out.append(source[i:regex_end])
                    can_start_regex = False
                    i = regex_end
                    continue
            out.append("/")
            can_start_regex = True
            i += 1
            continue

        # 9. Braces (manage template `${...}` nesting + `can_start_regex`)
        if ch == "{":
            out.append(ch)
            if mode_stack and isinstance(mode_stack[-1], int):
                mode_stack[-1] += 1
            can_start_regex = True
            i += 1
            continue

        if ch == "}":
            out.append(ch)
            if mode_stack and isinstance(mode_stack[-1], int):
                if mode_stack[-1] == 0:
                    mode_stack.pop()
                    i += 1
                    continue
                mode_stack[-1] -= 1
            can_start_regex = True
            i += 1
            continue

        # 10. Identifiers / keywords
        if ch.isalpha() or ch in ("_", "$"):
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in ("_", "$")):
                i += 1
            ident = source[start:i]
            out.append(ident)
            can_start_regex = ident in _REGEX_PREFIX_KEYWORDS
            continue

        # 11. Numbers
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in (".", "_")):
                i += 1
            out.append(source[start:i])
            can_start_regex = False
            continue

        # 12. Multi-char operators affecting regex position (`++`, `--`, `=>`)
        if (ch == "+" and nxt == "+") or (ch == "-" and nxt == "-"):
            out.append(source[i : i + 2])
            can_start_regex = False
            i += 2
            continue
        if ch == "=" and nxt == ">":
            out.append("=>")
            can_start_regex = True
            i += 2
            continue

        # 13. Other punctuation / operators
        out.append(ch)
        if ch in _REGEX_PREFIX_CHARS:
            can_start_regex = True
        else:
            can_start_regex = False
        i += 1

    return "".join(out), comments


def blank_js_ts_comments(source: str) -> str:
    """Blank JS/TS comments while preserving newlines, template `${...}`, and `/regex/` literals."""
    blanked, _ = extract_js_ts_comments_and_blank(source)
    return blanked


def _replace_non_newline_with_space(text: str) -> str:
    return "".join(ch if ch in ("\n", "\r") else " " for ch in text)


def blank_c_like_comments(source: str) -> str:
    """Blank // and /* ... */ comments outside quotes while preserving exact newlines."""
    out: list[str] = []
    n = len(source)
    i = 0
    quote: str | None = None
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch in ("\n", "\r"):
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("  ")
                in_block_comment = False
                i += 2
                continue
            out.append(ch if ch in ("\n", "\r") else " ")
            i += 1
            continue

        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n and quote in ('"', "'", "`"):
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            out.append("  ")
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            out.append("  ")
            in_block_comment = True
            i += 2
            continue
        if ch in ('"', "'", "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def blank_python_comments(source: str) -> str:
    """Blank Python `# ...` comments outside strings/triple-strings while preserving newlines."""
    out: list[str] = []
    n = len(source)
    i = 0
    quote: str | None = None
    in_comment = False

    while i < n:
        ch = source[i]
        if in_comment:
            if ch in ("\n", "\r"):
                in_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue

        if quote is not None:
            if quote in ('"""', "'''"):
                if source[i : i + 3] == quote:
                    out.append(quote)
                    quote = None
                    i += 3
                    continue
                if ch == "\\" and i + 1 < n:
                    out.append(ch)
                    out.append(source[i + 1])
                    i += 2
                    continue
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == quote or ch == "\n":
                quote = None
            i += 1
            continue

        if ch == "#":
            out.append(" ")
            in_comment = True
            i += 1
            continue

        if source[i : i + 3] in ('"""', "'''"):
            quote = source[i : i + 3]
            out.append(quote)
            i += 3
            continue

        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def extract_xml_comments_and_blank(
    source: str, start_line: int = 1
) -> tuple[str, list[tuple[int, str, str]]]:
    """Blank XML/HTML `<!-- ... -->` comments outside tag attributes and return `(blanked, comments)`."""
    out: list[str] = []
    comments: list[tuple[int, str, str]] = []
    n = len(source)
    i = 0
    line_no = start_line
    in_comment = False
    in_tag = False
    quote: str | None = None
    c_start_line = start_line
    body_chars: list[str] = []

    while i < n:
        if in_comment:
            if source[i : i + 3] == "-->":
                out.append("   ")
                comments.append((c_start_line, "xml", "".join(body_chars)))
                body_chars = []
                in_comment = False
                i += 3
                continue
            ch = source[i]
            out.append(ch if ch in ("\n", "\r") else " ")
            body_chars.append(ch)
            if ch == "\n":
                line_no += 1
            i += 1
            continue

        ch = source[i]
        if quote is not None:
            out.append(ch)
            if ch == "\n":
                line_no += 1
            if ch == quote:
                quote = None
            i += 1
            continue

        if source[i : i + 4] == "<!--":
            out.append("    ")
            in_comment = True
            c_start_line = line_no
            body_chars = []
            i += 4
            continue

        if ch == "<":
            in_tag = True
        elif ch == ">" and in_tag:
            in_tag = False
        elif in_tag and ch in ('"', "'"):
            quote = ch

        if ch == "\n":
            line_no += 1
        out.append(ch)
        i += 1

    if in_comment:
        comments.append((c_start_line, "xml", "".join(body_chars)))

    return "".join(out), comments


def blank_xml_comments(source: str) -> str:
    """Blank XML/HTML `<!-- ... -->` comments outside attribute strings, preserving newlines."""
    blanked, _ = extract_xml_comments_and_blank(source)
    return blanked


_VUE_SCRIPT_STYLE_RE = re.compile(
    r"(<(script|style)\b[^>]*>)([\s\S]*?)(</\2\s*>)",
    re.IGNORECASE,
)


def extract_vue_comments_and_blank(
    source: str,
    *,
    mask_strings: bool = False,
) -> tuple[str, list[tuple[int, str, str]]]:
    """
    Blank Vue SFC `<!-- ... -->` template comments and `<script>`/`<style>` JS/TS/CSS
    comments with 1:1 newline preservation, isolating `<template>` text from `<script>` lexing.
    """
    xml_blanked, xml_comments = extract_xml_comments_and_blank(source)
    all_comments: list[tuple[int, str, str]] = list(xml_comments)
    out: list[str] = []
    last_idx = 0

    for m in _VUE_SCRIPT_STYLE_RE.finditer(xml_blanked):
        out.append(xml_blanked[last_idx : m.start()])
        open_tag, tag_name, body, close_tag = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        out.append(open_tag)
        body_start_line = xml_blanked.count("\n", 0, m.start(3)) + 1
        if tag_name == "script":
            body_blanked, body_comments = extract_js_ts_comments_and_blank(
                body, start_line=body_start_line, mask_strings=mask_strings
            )
            out.append(body_blanked)
            all_comments.extend(body_comments)
        else:
            out.append(blank_c_like_comments(body))
        out.append(close_tag)
        last_idx = m.end()

    out.append(xml_blanked[last_idx:])
    all_comments.sort(key=lambda c: c[0])
    return "".join(out), all_comments


def blank_vue_comments(source: str) -> str:
    """Blank both `<!-- ... -->` template comments and `<script>`/`<style>` `//` + `/* */` comments."""
    blanked, _ = extract_vue_comments_and_blank(source)
    return blanked


def blank_sql_comments(source: str) -> str:
    """Blank SQL `-- ...` and `/* ... */` comments outside `'...'` / `"..."` literals."""
    out: list[str] = []
    n = len(source)
    i = 0
    quote: str | None = None
    in_line = False
    in_block = False

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line:
            if ch in ("\n", "\r"):
                in_line = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue

        if in_block:
            if ch == "*" and nxt == "/":
                out.append("  ")
                in_block = False
                i += 2
                continue
            out.append(ch if ch in ("\n", "\r") else " ")
            i += 1
            continue

        if quote is not None:
            out.append(ch)
            if ch == quote:
                if nxt == quote:
                    out.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            out.append("  ")
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            out.append("  ")
            in_block = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def blank_hash_comments(source: str) -> str:
    """Blank `# ...` comments outside quotes (YAML / Shell / TOML)."""
    return blank_python_comments(source)


@dataclass(frozen=True)
class FormatSpec:
    name: str
    extensions: tuple[str, ...]
    detector_profile: str
    comment_blanker: Callable[[str], str]
    default_ignore_globs: tuple[str, ...] = ()


FORMAT_REGISTRY: dict[str, FormatSpec] = {}


def _register(spec: FormatSpec) -> None:
    for ext in spec.extensions:
        FORMAT_REGISTRY[ext.lower()] = spec


_register(FormatSpec(
    name="python",
    extensions=(".py", ".pyi"),
    detector_profile="python",
    comment_blanker=blank_python_comments,
    default_ignore_globs=("**/__pycache__/**", "**/.venv/**"),
))
_register(FormatSpec(
    name="javascript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    detector_profile="js_ts",
    comment_blanker=blank_js_ts_comments,
    default_ignore_globs=(
        "**/node_modules/**",
        "**/dist/**",
        "**/public/plugins/**",
        "**/vendor/**",
        "**/*.min.js",
        "**/*.min.mjs",
        "**/*.min.cjs",
    ),
))
_register(FormatSpec(
    name="typescript",
    extensions=(".ts", ".tsx", ".mts", ".cts"),
    detector_profile="js_ts",
    comment_blanker=blank_js_ts_comments,
    default_ignore_globs=(
        "**/node_modules/**",
        "**/dist/**",
        "**/public/plugins/**",
        "**/vendor/**",
        "**/auto-imports*.d.ts",
        "**/components*.d.ts",
    ),
))
_register(FormatSpec(
    name="vue",
    extensions=(".vue",),
    detector_profile="js_ts",
    comment_blanker=blank_vue_comments,
    default_ignore_globs=("**/node_modules/**", "**/dist/**", "**/public/plugins/**"),
))
_register(FormatSpec(
    name="java",
    extensions=(".java",),
    detector_profile="java",
    comment_blanker=blank_c_like_comments,
    default_ignore_globs=("**/target/**", "**/build/**"),
))
_register(FormatSpec(
    name="go",
    extensions=(".go",),
    detector_profile="go",
    comment_blanker=blank_c_like_comments,
    default_ignore_globs=("**/vendor/**",),
))
_register(FormatSpec(
    name="xml",
    extensions=(".xml", ".html"),
    detector_profile="generic",
    comment_blanker=blank_xml_comments,
    default_ignore_globs=("**/target/**", "**/build/**"),
))
_register(FormatSpec(
    name="sql",
    extensions=(".sql",),
    detector_profile="generic",
    comment_blanker=blank_sql_comments,
))
_register(FormatSpec(
    name="shell_yaml",
    extensions=(".sh", ".bash", ".yaml", ".yml"),
    detector_profile="generic",
    comment_blanker=blank_hash_comments,
))


def get_format_for_path(path: str | Path) -> FormatSpec | None:
    suffix = Path(str(path)).suffix.lower()
    return FORMAT_REGISTRY.get(suffix)


def blank_comments_for_path(path: str | Path, source: str) -> str:
    spec = get_format_for_path(path)
    if spec is None:
        return source
    return spec.comment_blanker(source)
