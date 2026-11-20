"""Unrig CLI entrypoint (`init`, `hook`, `verify`, `replay`)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__
from .cli_core import init_workspace, verify_workspace
from .config import UnrigConfigError, load_config
from .l0_guard import evaluate_hook_payload
from .replay import run_builtin_replay_suite


def _resolve_root(raw_root: str | None) -> Path:
    return Path(raw_root).resolve() if raw_root else Path.cwd().resolve()


def _cmd_init(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    results = init_workspace(root, force=args.force)
    print(f"Unrig v{__version__} initialized in: {root}")
    for rel, status in results:
        print(f"  - {rel}: {status}")
    print("Next step: commit `.unrig.yaml` and `.unrig/` to lock L0 integrity in git.")
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    try:
        config = load_config(root)
    except UnrigConfigError as exc:
        print(f"[UNRIG L0 HOOK BLOCKED] Invalid .unrig.yaml: {exc}", file=sys.stderr)
        return 2
    payload = sys.stdin.read()
    code, msg = evaluate_hook_payload(payload, root, config)
    if code != 0 and msg:
        print(msg, file=sys.stderr)
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
        print(ln)
    return 0 if passed else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    results = run_builtin_replay_suite()
    failed = 0
    print(f"Unrig v{__version__} Referee Escape-Replay Suite ({len(results)} scenarios):")
    for r in results:
        tag = "[PASS]" if r.passed else "[FAIL]"
        print(f"  {tag} {r.case_id}: {r.description} -> {r.detail}")
        if not r.passed:
            failed += 1
    return 0 if failed == 0 else 1


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
    p_init.set_defaults(func=_cmd_init)

    p_hook = sub.add_parser("hook", help="PreToolUse stdin hook guard (exits 2 on L0 tamper).")
    p_hook.add_argument("--root", default=None, help="Repository root (default: cwd).")
    p_hook.set_defaults(func=_cmd_hook)

    p_verify = sub.add_parser("verify", help="Verify L0 integrity, ratchet, diff counters, and JUnit freshness.")
    p_verify.add_argument("--root", default=None, help="Repository root (default: cwd).")
    p_verify.add_argument("--config", default=None, help="Optional explicit `.unrig.yaml` path.")
    p_verify.add_argument("--baseline", default=None, help="Git revision baseline for diff comparison.")
    p_verify.add_argument("--strict-l0", action="store_true", help="Fail if protected L0 files are untracked.")
    p_verify.set_defaults(func=_cmd_verify)

    p_replay = sub.add_parser("replay", help="Run built-in referee escape-replay and self-mutation tests.")
    p_replay.set_defaults(func=_cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
