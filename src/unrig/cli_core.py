"""Core workspace operations shared by `cli.py` and `replay.py`."""

from __future__ import annotations

import json
from pathlib import Path

from .config import UnrigConfigError, load_config
from .diff_counter import verify_incremental_diff
from .junit_verifier import verify_reports_and_artifacts
from .l0_guard import check_l0_integrity


_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def init_workspace(repo_root: Path, force: bool = False) -> list[tuple[str, str]]:
    """Initialize `.unrig.yaml` and `.unrig/` templates in `repo_root`."""
    results: list[tuple[str, str]] = []
    files_to_create: dict[str, str] = {
        ".unrig.yaml": (_TEMPLATES_DIR / "unrig.yaml").read_text(encoding="utf-8"),
        ".unrig/PROTOCOL.md": (_TEMPLATES_DIR / "PROTOCOL.md").read_text(encoding="utf-8"),
        ".unrig/blind_auditor_prompt.md": (_TEMPLATES_DIR / "blind_auditor_prompt.md").read_text(encoding="utf-8"),
        ".unrig/ratchet.json": json.dumps({"version": 1, "entries": []}, indent=2) + "\n",
    }

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
    diff_findings, ratchet_errors, scanned_count = verify_incremental_diff(
        repo_root, config, baseline_override=baseline
    )
    report_findings, report_count = verify_reports_and_artifacts(repo_root, config)

    for w in l0_warnings:
        lines.append(f"[WARN] {w}")

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
    if not all_failures:
        lines.append(
            f"[PASS] Unrig verify succeeded (diff_scanned={scanned_count}, junit_reports={report_count}, violations=0)."
        )
        return True, lines

    lines.append(
        f"[BLOCKED] Unrig verify failed ({len(all_failures)} blocking finding(s); "
        f"diff_scanned={scanned_count}, junit_reports={report_count})."
    )
    return False, lines
