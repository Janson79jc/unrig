"""#3600 Format Registry: language-aware comment blankers preserving exact line numbers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


def blank_xml_comments(source: str) -> str:
    """Blank XML/HTML `<!-- ... -->` comments outside attribute strings, preserving newlines."""
    out: list[str] = []
    n = len(source)
    i = 0
    in_comment = False
    quote: str | None = None

    while i < n:
        if in_comment:
            if source[i : i + 3] == "-->":
                out.append("   ")
                in_comment = False
                i += 3
                continue
            ch = source[i]
            out.append(ch if ch in ("\n", "\r") else " ")
            i += 1
            continue

        ch = source[i]
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue

        if source[i : i + 4] == "<!--":
            out.append("    ")
            in_comment = True
            i += 4
            continue

        if ch in ('"', "'"):
            quote = ch
        out.append(ch)
        i += 1

    return "".join(out)


def blank_vue_comments(source: str) -> str:
    """Blank both `<!-- ... -->` template comments and JS/TS/CSS `//` + `/* */` comments."""
    return blank_c_like_comments(blank_xml_comments(source))


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
    comment_blanker=blank_c_like_comments,
    default_ignore_globs=("**/node_modules/**", "**/dist/**"),
))
_register(FormatSpec(
    name="typescript",
    extensions=(".ts", ".tsx", ".mts", ".cts"),
    detector_profile="js_ts",
    comment_blanker=blank_c_like_comments,
    default_ignore_globs=("**/node_modules/**", "**/dist/**"),
))
_register(FormatSpec(
    name="vue",
    extensions=(".vue",),
    detector_profile="js_ts",
    comment_blanker=blank_vue_comments,
    default_ignore_globs=("**/node_modules/**", "**/dist/**"),
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
