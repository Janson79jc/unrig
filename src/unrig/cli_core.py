"""Core workspace operations shared by `cli.py` and `replay.py`."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from .config import UnrigConfigError, load_config
from .diff_counter import find_ratchet_slack, verify_incremental_diff_with_stats
from .junit_verifier import verify_reports_and_artifacts
from .l0_guard import (
    check_l0_integrity,
    detect_hook_client,
    evaluate_hook_payload,
    format_hook_response,
    resolve_hook_repo_root,
)


_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _client_hook_files(client: str | None) -> dict[str, str]:
    if not client:
        return {}
    targets = {"claude", "antigravity", "gemini", "codex"} if client == "all" else {client}
    files: dict[str, str] = {}
    if "claude" in targets:
        files[".claude/settings.json"] = json.dumps({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Write|Edit|MultiEdit|Bash",
                        "hooks": [{"type": "command", "command": "unrig hook --client claude"}],
                    }
                ]
            }
        }, indent=2) + "\n"
    if "antigravity" in targets:
        files[".agents/hooks.json"] = json.dumps({
            "unrig-guard": {
                "PreToolUse": [
                    {
                        "matcher": "run_command|write_file|edit_file|write_to_file|replace_file_content|multi_replace_file_content|Write|Edit|MultiEdit|Bash",
                        "hooks": [{"type": "command", "command": "unrig hook --client antigravity"}],
                    }
                ],
                "Stop": [
                    {"type": "command", "command": "unrig hook --client antigravity"}
                ],
            }
        }, indent=2) + "\n"
    if "gemini" in targets:
        files[".gemini/settings.json"] = json.dumps({
            "hooks": {
                "BeforeTool": [
                    {
                        "hooks": [{"type": "command", "name": "unrig-guard", "command": "unrig hook --client gemini"}]
                    }
                ]
            }
        }, indent=2) + "\n"
    if "codex" in targets:
        files[".codex/hooks.json"] = json.dumps({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "unrig hook --client codex"}],
                    }
                ]
            }
        }, indent=2) + "\n"
    return files


def init_workspace(
    repo_root: Path,
    force: bool = False,
    client: str | None = None,
) -> list[tuple[str, str]]:
    """Initialize `.unrig.yaml`, `.unrig/` templates, and optional agent-client hook configs in `repo_root`."""
    results: list[tuple[str, str]] = []
    files_to_create: dict[str, str] = {
        ".unrig.yaml": (_TEMPLATES_DIR / "unrig.yaml").read_text(encoding="utf-8"),
        ".unrig/PROTOCOL.md": (_TEMPLATES_DIR / "PROTOCOL.md").read_text(encoding="utf-8"),
        ".unrig/blind_auditor_prompt.md": (_TEMPLATES_DIR / "blind_auditor_prompt.md").read_text(encoding="utf-8"),
        ".unrig/ratchet.json": json.dumps({"version": 1, "entries": []}, indent=2) + "\n",
    }
    files_to_create.update(_client_hook_files(client))

    for rel, content in files_to_create.items():
        dest = repo_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not force:
            results.append((rel, "skipped (already exists)"))
            continue
        action = "overwritten" if dest.exists() else "created"
        dest.write_text(content, encoding="utf-8")
        results.append((rel, action))

    return results


def verify_workspace(
    repo_root: Path,
    config_path: Path | None = None,
    baseline: str | None = None,
    strict_l0: bool = False,
    *,
    record_log: bool = True,
) -> tuple[bool, list[str]]:
    """Run all Unrig Level 0 verification checks on `repo_root`."""
    lines: list[str] = []
    try:
        config = load_config(repo_root, config_path)
    except UnrigConfigError as exc:
        return False, [f"[FAIL] [CONFIG_ERROR] {exc}"]

    l0_blocking, l0_warnings = check_l0_integrity(
        repo_root, config.protected_l0_files, strict_committed=strict_l0
    )
    diff_findings, ratchet_errors, clone_warnings, scan_stats = verify_incremental_diff_with_stats(
        repo_root, config, baseline_override=baseline
    )
    scanned_count = scan_stats.scanned
    ratchet_slacks, _ = find_ratchet_slack(repo_root, config)
    report_findings, report_count = verify_reports_and_artifacts(repo_root, config)

    for w in l0_warnings:
        lines.append(f"[WARN] {w}")
    if scan_stats.shallow_history:
        lines.append(
            "[WARN] [SHALLOW_HISTORY] Shallow git clone fell back to HEAD; baseline history is truncated."
        )
    for cw in clone_warnings:
        lines.append(f"[WARN] {cw}")
    for slack in ratchet_slacks:
        lines.append(
            f"[WARN] [RATCHET_SLACK] '{config.ratchet_file}' allows count={slack.entry.count} for "
            f"{slack.entry.file} ({slack.entry.rule}), but workspace only has {slack.actual_count}. "
            "Run `unrig ratchet --prune` to tighten."
        )

    all_failures: list[str] = []
    for blk in l0_blocking:
        all_failures.append(f"[FAIL] [{blk['code']}] {blk['path']}: {blk['message']}")
    for rerr in ratchet_errors:
        all_failures.append(f"[FAIL] {rerr}")
    for df in diff_findings:
        all_failures.append(f"[FAIL] [{df.rule_id}] {df.rel_path}:{df.line} -> {df.message}")
    for rf in report_findings:
        all_failures.append(f"[FAIL] {rf}")

    lines.extend(all_failures)
    stats_suffix = f"skipped_ignored={scan_stats.skipped_ignored}, syntax_fallback={scan_stats.syntax_fallback}"
    if not all_failures:
        lines.append(
            f"[PASS] Unrig verify succeeded (diff_scanned={scanned_count}, junit_reports={report_count}, "
            f"violations=0, {stats_suffix})."
        )
        return True, lines

    lines.append(
        f"[BLOCKED] Unrig verify failed ({len(all_failures)} blocking finding(s); "
        f"diff_scanned={scanned_count}, junit_reports={report_count}, {stats_suffix})."
    )
    if record_log:
        codes: list[str] = []
        for blk in l0_blocking:
            codes.append(blk["code"])
        for df in diff_findings:
            codes.append(df.rule_id)
        for rf in report_findings:
            m_code = re.match(r"^\[([A-Z0-9_\-]+)\]", rf)
            codes.append(m_code.group(1) if m_code else "REPORT_BLOCKED")
        if ratchet_errors:
            codes.append("RATCHET_ERROR")
        record_intercept_event(
            repo_root,
            source="verify",
            event="verify",
            client="cli",
            outcome="blocked",
            codes=codes or ["VERIFY_BLOCKED"],
            detail=f"{len(all_failures)} blocking finding(s)",
        )
    return False, lines


_CACHE_DIR_REL = Path(".unrig") / ".cache"
_EVENTS_FILE_NAME = "events.jsonl"
_STOP_STATE_FILE_NAME = "stop_state.json"
_MAX_STOP_CONTINUE_SAME_REASON = 2


def _ensure_cache_dir(repo_root: Path) -> Path:
    cache_dir = repo_root / _CACHE_DIR_REL
    cache_dir.mkdir(parents=True, exist_ok=True)
    gi = cache_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return cache_dir


def _events_file(repo_root: Path) -> Path:
    return repo_root / _CACHE_DIR_REL / _EVENTS_FILE_NAME


def _stop_state_file(repo_root: Path) -> Path:
    return repo_root / _CACHE_DIR_REL / _STOP_STATE_FILE_NAME


def _update_stop_bounce_count(repo_root: Path, conv_id: str, signature: str) -> int:
    """Increment and return consecutive Stop bounce count for identical failure `signature`."""
    try:
        cache_dir = _ensure_cache_dir(repo_root)
        state_path = cache_dir / _STOP_STATE_FILE_NAME
        state: dict[str, dict[str, Any]] = {}
        if state_path.exists():
            raw = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(raw, dict):
                state = raw
        prev = state.get(conv_id)
        if isinstance(prev, dict) and prev.get("signature") == signature:
            count = int(prev.get("count") or 0) + 1
        else:
            count = 1
        state[conv_id] = {"signature": signature, "count": count}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return count
    except (OSError, ValueError, TypeError):
        return 1


def _clear_stop_bounce_count(repo_root: Path, conv_id: str) -> None:
    try:
        state_path = _stop_state_file(repo_root)
        if not state_path.exists():
            return
        raw = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(raw, dict) and conv_id in raw:
            raw.pop(conv_id, None)
            state_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return


def record_intercept_event(
    repo_root: Path,
    *,
    source: str,
    event: str,
    client: str,
    outcome: str,
    codes: list[str],
    detail: str = "",
) -> None:
    """Append a local-only interception record under `.unrig/.cache/events.jsonl`."""
    if os.environ.get("UNRIG_DISABLE_EVENT_LOG") == "1":
        return
    try:
        _ensure_cache_dir(repo_root)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "event": event,
            "client": client,
            "outcome": outcome,
            "codes": sorted(dict.fromkeys(codes)),
            "detail": detail[:200],
        }
        with _events_file(repo_root).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return


def read_intercept_stats(
    repo_root: Path,
    days: int | None = None,
) -> dict[str, Any]:
    """Read local interception statistics from `.unrig/.cache/events.jsonl`."""
    path = _events_file(repo_root)
    if not path.exists():
        return {"total": 0, "by_outcome": {}, "by_code": {}, "by_client": {}, "recent": []}

    cutoff: datetime | None = None
    if days is not None and days >= 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    events: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if cutoff is not None and isinstance(item.get("ts"), str):
            try:
                ts_val = datetime.fromisoformat(item["ts"])
                if ts_val.tzinfo is None:
                    ts_val = ts_val.replace(tzinfo=timezone.utc)
                if ts_val < cutoff:
                    continue
            except ValueError:
                pass
        events.append(item)

    by_outcome: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    by_client: Counter[str] = Counter()
    for ev in events:
        by_outcome[str(ev.get("outcome") or "unknown")] += 1
        by_client[str(ev.get("client") or "unknown")] += 1
        for code in ev.get("codes") or []:
            by_code[str(code)] += 1

    return {
        "total": len(events),
        "by_outcome": dict(by_outcome.most_common()),
        "by_code": dict(by_code.most_common()),
        "by_client": dict(by_client.most_common()),
        "recent": events[-10:],
    }


def clear_intercept_stats(repo_root: Path) -> bool:
    """Remove `.unrig/.cache/events.jsonl` if present. Returns True if removed."""
    path = _events_file(repo_root)
    if path.exists():
        path.unlink()
        return True
    return False


def handle_hook_invocation(
    payload_text: str,
    explicit_root: str | None = None,
    explicit_client: str = "auto",
) -> tuple[int, str, str]:
    """
    Process a coding-agent hook invocation across Claude Code, Google Antigravity, Gemini CLI, and Codex CLI.

    Returns `(exit_code, stdout_text, stderr_text)`.
    """
    stripped = payload_text.strip()
    data: object = None
    if stripped:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            client = explicit_client if explicit_client != "auto" else "claude"
            msg = f"[UNRIG L0 HOOK BLOCKED] Invalid hook JSON payload: {exc}"
            record_intercept_event(
                resolve_hook_repo_root(explicit_root, None),
                source="hook",
                event="PreToolUse",
                client=client,
                outcome="deny",
                codes=["INVALID_HOOK_JSON"],
                detail=msg,
            )
            return format_hook_response(client, "PreToolUse", 2, msg)

    client = detect_hook_client(data, explicit_client)
    event_name = (
        str(data.get("hookEventName") or data.get("hook_event_name") or "PreToolUse")
        if isinstance(data, dict)
        else "PreToolUse"
    )
    repo_root = resolve_hook_repo_root(explicit_root, data)

    if client == "antigravity" and event_name == "Stop" and isinstance(data, dict):
        term_reason = str(data.get("terminationReason") or "")
        continuation_active = bool(data.get("continuationActive", False))
        conv_id = str(data.get("conversationId") or "default")
        if continuation_active or (term_reason and term_reason != "NO_TOOL_CALL"):
            return format_hook_response(client, "Stop", 0, "", stop_continue=False)
        passed, lines = verify_workspace(repo_root, record_log=False)
        if not passed:
            summary = "\n".join(lines[-8:])
            codes = [m.group(1) for ln in lines for m in [re.search(r"\[FAIL\]\s+\[([A-Z0-9_\-]+)\]", ln)] if m]
            failure_codes = codes or ["STOP_GATE_BLOCKED"]
            bounce_count = _update_stop_bounce_count(repo_root, conv_id, summary)
            if bounce_count > _MAX_STOP_CONTINUE_SAME_REASON:
                record_intercept_event(
                    repo_root,
                    source="hook",
                    event="Stop",
                    client=client,
                    outcome="escalated",
                    codes=failure_codes,
                    detail=f"Released Stop after {_MAX_STOP_CONTINUE_SAME_REASON} identical bounces for human escalation",
                )
                return format_hook_response(client, "Stop", 0, "", stop_continue=False)

            reason = (
                "[UNRIG STOP GATE] Unrig verification failed. Fix all blocking findings before finishing "
                "(if blocked by a human-only L0 gate or missing external credential/service, stop and ask the maintainer):\n"
                + summary
            )
            record_intercept_event(
                repo_root,
                source="hook",
                event="Stop",
                client=client,
                outcome="continue",
                codes=failure_codes,
                detail=lines[-1] if lines else "verify failed",
            )
            return format_hook_response(client, "Stop", 1, reason, stop_continue=True)
        _clear_stop_bounce_count(repo_root, conv_id)
        return format_hook_response(client, "Stop", 0, "", stop_continue=False)

    try:
        config = load_config(repo_root)
    except UnrigConfigError as exc:
        msg = f"[UNRIG L0 HOOK BLOCKED] Invalid .unrig.yaml: {exc}"
        record_intercept_event(
            repo_root,
            source="hook",
            event=event_name,
            client=client,
            outcome="deny",
            codes=["CONFIG_ERROR"],
            detail=msg,
        )
        return format_hook_response(client, event_name, 2, msg)

    code, msg = evaluate_hook_payload(payload_text, repo_root, config)
    if code != 0:
        hook_code = "NO_VERIFY_BYPASS" if "--no-verify" in msg else "L0_HOOK_BLOCK"
        record_intercept_event(
            repo_root,
            source="hook",
            event=event_name,
            client=client,
            outcome="deny",
            codes=[hook_code],
            detail=msg,
        )
    return format_hook_response(client, event_name, code, msg)
