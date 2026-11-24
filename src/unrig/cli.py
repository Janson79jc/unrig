"""Unrig CLI entrypoint (`init`, `hook`, `verify`, `replay`, `ratchet`, `stats`)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import TextIO

from . import __version__
from .cli_core import (
    clear_intercept_stats,
    handle_hook_invocation,
    init_workspace,
    read_intercept_stats,
    verify_workspace,
)
from .config import UnrigConfigError, load_config
from .diff_counter import find_ratchet_slack, prune_ratchet
from .replay import run_builtin_replay_suite


def _resolve_root(raw_root: str | None) -> Path:
    return Path(raw_root).resolve() if raw_root else Path.cwd().resolve()


def _out_line(text: str, *, err: bool = False) -> None:
    _write_utf8_stream(sys.stderr if err else sys.stdout, text + "\n")


def _cmd_init(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    results = init_workspace(root, force=args.force, client=args.client)
    _out_line(f"Unrig v{__version__} initialized in: {root}")
    for rel, status in results:
        _out_line(f"  - {rel}: {status}")
    _out_line("Next step: commit `.unrig.yaml` and `.unrig/` to lock L0 integrity in git.")
    return 0


def _read_utf8_stdin() -> str:
    """Read hook JSON payload from stdin using UTF-8 regardless of Windows console codepage."""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is not None:
        return buf.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def _write_utf8_stream(stream: TextIO, text: str) -> None:
    """Write text to stdout/stderr as UTF-8 so non-ASCII hook responses never raise cp936 encode errors."""
    if not text:
        return
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8", errors="replace"))
        buf.flush()
    else:
        stream.write(text)


def _cmd_hook(args: argparse.Namespace) -> int:
    payload = _read_utf8_stdin()
    code, stdout_text, stderr_text = handle_hook_invocation(
        payload,
        explicit_root=args.root,
        explicit_client=args.client,
    )
    _write_utf8_stream(sys.stdout, stdout_text)
    _write_utf8_stream(sys.stderr, stderr_text)
    return code


def _cmd_verify(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    cfg_path = Path(args.config).resolve() if args.config else None
    passed, lines = verify_workspace(
        root,
        config_path=cfg_path,
        baseline=args.baseline,
        strict_l0=args.strict_l0,
    )
    for ln in lines:
        _out_line(ln)
    return 0 if passed else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    results = run_builtin_replay_suite()
    failed = 0
    _out_line(f"Unrig v{__version__} Referee Escape-Replay Suite ({len(results)} scenarios):")
    for r in results:
        tag = "[PASS]" if r.passed else "[FAIL]"
        _out_line(f"  {tag} {r.case_id}: {r.description} -> {r.detail}")
        if not r.passed:
            failed += 1
    return 0 if failed == 0 else 1


def _cmd_ratchet(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    try:
        config = load_config(root)
    except UnrigConfigError as exc:
        _out_line(f"[FAIL] [CONFIG_ERROR] {exc}", err=True)
        return 1

    if args.prune:
        slacks, errors = prune_ratchet(root, config, dry_run=False)
        if errors:
            for err in errors:
                _out_line(f"[FAIL] {err}", err=True)
            return 1
        if not slacks:
            _out_line("[PASS] Ratchet ledger already tight (0 entries pruned).")
            return 0
        for s in slacks:
            action = "removed (0 remaining)" if s.actual_count == 0 else f"tightened {s.entry.count} -> {s.actual_count}"
            _out_line(f"  - {s.entry.file} ({s.entry.rule}): {action}")
        _out_line(f"[PASS] Pruned {len(slacks)} stale ratchet entry(ies) in '{config.ratchet_file}'.")
        return 0

    slacks, errors = find_ratchet_slack(root, config)
    if errors:
        for err in errors:
            _out_line(f"[FAIL] {err}", err=True)
        return 1
    if slacks:
        for s in slacks:
            _out_line(
                f"[RATCHET_SLACK] {s.entry.file} ({s.entry.rule}): "
                f"cap={s.entry.count}, actual={s.actual_count}"
            )
        return 1
    _out_line("[PASS] Ratchet ledger is tight (no stale slack).")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if args.clear:
        removed = clear_intercept_stats(root)
        _out_line("[PASS] Cleared local interception log." if removed else "[PASS] No local interception log to clear.")
        return 0
    stats = read_intercept_stats(root, days=args.days)
    scope = f"last {args.days} day(s)" if args.days is not None else "all time"
    _out_line(f"Unrig Local Interception Ledger ({scope}) — {root}")
    _out_line(f"  Total intercepted: {stats['total']}")
    if stats["total"] == 0:
        _out_line("  (No blocked escapes recorded yet in `.unrig/.cache/events.jsonl`.)")
        return 0
    _out_line(f"  By outcome: {stats['by_outcome']}")
    _out_line(f"  By rule/code: {stats['by_code']}")
    _out_line(f"  By client: {stats['by_client']}")
    _out_line("  Recent interceptions:")
    for ev in stats["recent"][-5:]:
        _out_line(f"    - [{ev.get('ts')}] {ev.get('client')}/{ev.get('event')} -> {ev.get('codes')} | {ev.get('detail')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unrig",
        description="Unrig — Anti-tamper gates, JUnit/diff verification, and isolated blind review for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"unrig {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize `.unrig.yaml` and `.unrig/` templates.")
    p_init.add_argument("--root", default=None, help="Target repository root (default: cwd).")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing templates.")
    p_init.add_argument(
        "--client",
        choices=["claude", "antigravity", "gemini", "codex", "all"],
        default=None,
        help="Also generate hook configuration for Claude Code, Google Antigravity, Gemini CLI, Codex CLI, or all.",
    )
    p_init.set_defaults(func=_cmd_init)

    p_hook = sub.add_parser("hook", help="PreToolUse / BeforeTool / Stop stdin hook guard.")
    p_hook.add_argument("--root", default=None, help="Repository root (default: resolved from payload or cwd).")
    p_hook.add_argument(
        "--client",
        choices=["auto", "claude", "antigravity", "gemini", "codex"],
        default="auto",
        help="Hook protocol adapter (default: auto-detect from stdin JSON).",
    )
    p_hook.set_defaults(func=_cmd_hook)

    p_verify = sub.add_parser("verify", help="Verify L0 integrity, ratchet, diff counters, and JUnit freshness.")
    p_verify.add_argument("--root", default=None, help="Repository root (default: cwd).")
    p_verify.add_argument("--config", default=None, help="Optional explicit `.unrig.yaml` path.")
    p_verify.add_argument("--baseline", default=None, help="Git revision baseline for diff comparison.")
    p_verify.add_argument("--strict-l0", action="store_true", help="Fail if protected L0 files are untracked.")
    p_verify.set_defaults(func=_cmd_verify)

    p_replay = sub.add_parser("replay", help="Run built-in referee escape-replay and self-mutation tests.")
    p_replay.set_defaults(func=_cmd_replay)

    p_ratchet = sub.add_parser("ratchet", help="Inspect or prune stale `.unrig/ratchet.json` slack.")
    p_ratchet.add_argument("--root", default=None, help="Repository root (default: cwd).")
    p_ratchet.add_argument("--prune", action="store_true", help="Tighten stale ratchet caps in place (human maintenance).")
    p_ratchet.add_argument("--check", action="store_true", help="Exit 1 if any ratchet entry has stale slack (default).")
    p_ratchet.set_defaults(func=_cmd_ratchet)

    p_stats = sub.add_parser("stats", help="Show local interception ledger summary from `.unrig/.cache/events.jsonl`.")
    p_stats.add_argument("--root", default=None, help="Repository root (default: cwd).")
    p_stats.add_argument("--days", type=int, default=None, help="Only include events from the last N days.")
    p_stats.add_argument("--clear", action="store_true", help="Clear the local interception log.")
    p_stats.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
