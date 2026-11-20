"""L0 anti-tamper guard: git -uall -z status verification, normalized SHA-256, and PreToolUse hook."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .config import (
    DEFAULT_PROTECTED_L0_FILES,
    UnrigConfig,
    UnrigConfigError,
    normalize_rel_path,
    parse_simple_yaml,
)


def compute_normalized_sha256(file_path: Path) -> str:
    """Compute SHA-256 after normalizing CRLF -> LF for cross-platform parity."""
    raw = file_path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def parse_git_status_z(stdout_bytes: bytes) -> list[tuple[str, str]]:
    """
    Parse `git -c core.quotepath=false status --porcelain -uall -z` output.

    CRITICAL: Never `.strip()` entries before slicing `[:2]` and `[3:]`, otherwise
    an unstaged worktree modification `' M path'` loses its leading space and truncates
    the first character of `path` (MUT-T01 root cause).
    """
    if not stdout_bytes:
        return []
    entries = stdout_bytes.split(b"\0")
    results: list[tuple[str, str]] = []
    idx = 0
    total = len(entries)
    while idx < total:
        raw_entry = entries[idx]
        idx += 1
        if not raw_entry:
            continue
        text = raw_entry.decode("utf-8", errors="replace")
        if len(text) < 4:
            continue
        status_code = text[:2]
        rel_path = text[3:].replace("\\", "/")
        results.append((status_code, rel_path))
        if ("R" in status_code or "C" in status_code) and idx < total:
            orig_raw = entries[idx]
            idx += 1
            orig_path = orig_raw.decode("utf-8", errors="replace").replace("\\", "/")
            if orig_path:
                results.append((status_code, orig_path))
    return results


def is_git_repo(repo_root: Path) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return res.returncode == 0 and res.stdout.strip().lower() == "true"


def git_has_head(repo_root: Path) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def git_status_entries(repo_root: Path, scope: str | None = None) -> list[tuple[str, str]]:
    cmd = ["git", "-c", "core.quotepath=false", "status", "--porcelain", "-uall", "-z"]
    if scope:
        cmd.extend(["--", scope])
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, check=True)
    return parse_git_status_z(res.stdout)


def _norm_key(path_str: str) -> str:
    cleaned = normalize_rel_path(path_str)
    if os.name == "nt":
        return cleaned.lower()
    return cleaned


def _resolve_against_repo(raw_path: str, repo_root: Path) -> str | None:
    if not raw_path or not raw_path.strip():
        return None
    p = Path(raw_path.strip())
    repo_resolved = repo_root.resolve()
    try:
        full = p.resolve() if p.is_absolute() else (repo_resolved / p).resolve()
        rel = full.relative_to(repo_resolved)
        return rel.as_posix()
    except ValueError:
        return None


def match_protected_path(candidate: str, repo_root: Path, protected_files: tuple[str, ...]) -> str | None:
    """Return matching normalized L0 protected path if `candidate` resolves to one."""
    protected_map = {_norm_key(p): p for p in protected_files}
    direct = _norm_key(candidate)
    if direct in protected_map:
        return protected_map[direct]
    rel = _resolve_against_repo(candidate, repo_root)
    if rel is not None and _norm_key(rel) in protected_map:
        return protected_map[_norm_key(rel)]
    return None


def check_l0_integrity(
    repo_root: Path,
    protected_files: tuple[str, ...],
    strict_committed: bool = False,
) -> tuple[list[dict[str, str]], list[str]]:
    """
    Check git status against `protected_l0_files`.

    Returns `(blocking_findings, warnings)`.
    """
    blocking: list[dict[str, str]] = []
    warnings: list[str] = []

    if not is_git_repo(repo_root):
        return blocking, warnings

    has_head = git_has_head(repo_root)
    merged_protected: list[str] = [*DEFAULT_PROTECTED_L0_FILES, *protected_files]
    if has_head:
        res_head_cfg = subprocess.run(
            ["git", "-c", "core.quotepath=false", "show", "HEAD:.unrig.yaml"],
            cwd=str(repo_root),
            capture_output=True,
        )
        if res_head_cfg.returncode == 0:
            try:
                head_data = parse_simple_yaml(res_head_cfg.stdout.decode("utf-8", errors="replace"))
                head_prot = head_data.get("protected_l0_files") or []
                if isinstance(head_prot, list):
                    merged_protected.extend(str(p) for p in head_prot if p)
            except (UnrigConfigError, ValueError) as exc:
                warnings.append(f"Could not parse HEAD:.unrig.yaml for L0 lock: {exc}")

    protected_map = {_norm_key(p): normalize_rel_path(p) for p in merged_protected if str(p).strip()}

    for status_code, rel_path in git_status_entries(repo_root):
        norm = _norm_key(rel_path)
        matched = protected_map.get(norm)
        if not matched:
            continue
        if status_code == "??":
            if strict_committed:
                blocking.append({
                    "code": "L0_UNTRACKED",
                    "path": matched,
                    "status": status_code,
                    "message": f"Protected L0 file '{matched}' is untracked under --strict-l0 mode.",
                })
            elif has_head:
                warnings.append(
                    f"Protected L0 file '{matched}' is untracked; commit it to lock baseline integrity in git."
                )
            continue

        # Any tracked/staged/unstaged change (e.g. ' M', 'M ', 'MM', ' D', 'R ') after HEAD exists
        if has_head:
            blocking.append({
                "code": "L0_TAMPER",
                "path": matched,
                "status": status_code,
                "message": (
                    f"Protected L0 gate file '{matched}' has uncommitted git status [{status_code}]. "
                    "Agents must not modify L0 gates; revert or get human review to update baseline."
                ),
            })

    return blocking, warnings


_FILE_PATH_KEYS = (
    "file_path",
    "path",
    "target_file",
    "TargetFile",
    "AbsolutePath",
    "filepath",
    "destination",
    "new_path",
    "old_path",
)

_SHELL_CMD_KEYS = ("command", "CommandLine", "cmd", "script")

_NO_VERIFY_RE = re.compile(r"\bgit\s+commit\b[^|;&\n]*\s(?:--no-verify|-n)\b")
_WRITE_OP_RE = re.compile(
    r"(?:>>?|\btee\b|\bmv\b|\bcp\b|\brm\b|\bdel\b|\bsed\s+-i\b|"
    r"\bOut-File\b|\bSet-Content\b|\bAdd-Content\b|\bCopy-Item\b|\bMove-Item\b|\bRemove-Item\b)",
    re.IGNORECASE,
)


def _collect_strings(obj: Any, target_keys: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in target_keys and isinstance(v, str) and v.strip():
                found.append(v.strip())
            elif isinstance(v, (dict, list)):
                found.extend(_collect_strings(v, target_keys))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_collect_strings(item, target_keys))
    return found


def _command_touches_protected(cmd_text: str, protected_files: tuple[str, ...]) -> str | None:
    norm_cmd = cmd_text.replace("\\", "/")
    cmd_cmp = norm_cmd.lower() if os.name == "nt" else norm_cmd
    for p in protected_files:
        needle = p.lower() if os.name == "nt" else p
        if needle in cmd_cmp:
            if _WRITE_OP_RE.search(cmd_text) or "open(" in cmd_text or "write_text" in cmd_text or "write_bytes" in cmd_text:
                return p
    return None


def evaluate_hook_payload(
    payload_text: str,
    repo_root: Path,
    config: UnrigConfig,
) -> tuple[int, str]:
    """
    Evaluate a `PreToolUse` JSON payload on stdin.

    Returns `(exit_code, diagnostic_message)`:
    - `0`: allowed
    - `2`: blocked L0 tampering or `--no-verify` bypass attempt
    """
    stripped = payload_text.strip()
    if not stripped:
        return 0, ""

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return 2, f"[UNRIG L0 HOOK BLOCKED] Invalid hook JSON payload: {exc}"

    for candidate in _collect_strings(data, _FILE_PATH_KEYS):
        matched = match_protected_path(candidate, repo_root, config.protected_l0_files)
        if matched:
            return 2, (
                f"[UNRIG L0 HOOK BLOCKED] Direct modification to protected L0 file '{matched}' is forbidden."
            )

    if config.shell_write_redirect_guards:
        for cmd_text in _collect_strings(data, _SHELL_CMD_KEYS):
            if _NO_VERIFY_RE.search(cmd_text):
                return 2, "[UNRIG L0 HOOK BLOCKED] `git commit --no-verify` / `-n` bypass is forbidden."
            touched = _command_touches_protected(cmd_text, config.protected_l0_files)
            if touched:
                return 2, (
                    f"[UNRIG L0 HOOK BLOCKED] Shell command appears to mutate protected L0 file '{touched}'."
                )

    return 0, ""
