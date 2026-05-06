#!/usr/bin/env python3
"""Fix Chinese text in already-built TASK zip packages and re-run validation.

This script is for delivery zips that are already structurally correct but
still fail the validator's English-consistency check because Chinese text
remains in package files, repo files, or inner original_sessions zip payloads.

What it does for each TASK zip:
  1. Extract the outer TASK zip to a temp work dir.
  2. Run the same Chinese cleanup logic used by export_tasks on readable text
     files in the extracted package (metadata/docs/repo/etc.; original_sessions
     is handled separately).
  3. For loose original_sessions JSON/JSONL files, translate Chinese segments
     in-place.
  4. For any original_sessions/*.zip archives, extract each one, translate its
     JSON/JSONL contents, then rebuild the inner zip.
  5. Re-run validate_package_direct_original_sessions.py against the repaired
     extracted package and persist validation artifacts next to the TASK zip.
  6. If not --dry-run, rebuild the outer TASK zip in place from the repaired
     extract.

Usage:
    python fix_chinese_in_task_zips.py <path>

<path> can be:
    * a single TASK-*.zip file
    * a directory containing TASK-*.zip exports; matching zips are found
      recursively
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from export_tasks import (
    translate_chinese_text_in_repo_files,
    translate_chinese_text_in_sessions,
    zip_directory_contents,
)

log = logging.getLogger("fix_chinese_task_zips")


def _find_task_zips(target: Path, pattern: str) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() == ".zip":
            return [target]
        return []
    if target.is_dir():
        return sorted(p for p in target.rglob(pattern) if p.is_file())
    return []


def _extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)


def _rewrite_zip_from_dir(source_dir: Path, output_zip: Path) -> None:
    tmp_zip = output_zip.with_name(output_zip.name + ".tmp")
    if tmp_zip.exists():
        tmp_zip.unlink()
    zip_directory_contents(source_dir, tmp_zip)
    shutil.move(str(tmp_zip), str(output_zip))


def _remove_validator_tmp(extract_root: Path) -> None:
    tmp_dir = extract_root / ".tmp"
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _persist_validation_artifacts(
    task_zip: Path,
    extract_root: Path,
    stdout: str,
    stderr: str,
) -> dict[str, str]:
    out_dir = task_zip.parent / f"{task_zip.stem}__chinese_fix_validation"
    out_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = out_dir / "validation_stdout.log"
    stderr_path = out_dir / "validation_stderr.log"
    stdout_path.write_text(stdout or "", encoding="utf-8")
    stderr_path.write_text(stderr or "", encoding="utf-8")

    report_src = extract_root / ".tmp" / "validation_report.md"
    report_path = out_dir / "validation_report.md"
    if report_src.is_file():
        shutil.copy2(report_src, report_path)
    elif report_path.exists():
        report_path.unlink()

    return {
        "output_dir": str(out_dir),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "report_path": str(report_path) if report_src.is_file() else "",
    }


def _run_validator(extract_root: Path, task_zip: Path) -> dict[str, Any]:
    validator_path = Path(__file__).resolve().parent / "validate_package_direct_original_sessions.py"
    result = subprocess.run(
        [sys.executable, str(validator_path), str(extract_root)],
        capture_output=True,
        text=True,
    )
    artifacts = _persist_validation_artifacts(
        task_zip=task_zip,
        extract_root=extract_root,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        **artifacts,
    }


def _fix_inner_sessions_zip(inner_zip: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=inner_zip.stem + ".") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        extract_dir = tmp_dir / "extract"
        _extract_zip(inner_zip, extract_dir)
        session_stats = translate_chinese_text_in_sessions(extract_dir)
        changed = (
            session_stats["files_changed"] > 0
            or session_stats["changed_fields"] > 0
            or session_stats["translated_segments"] > 0
        )
        if changed:
            _rewrite_zip_from_dir(extract_dir, inner_zip)
        return {
            "changed": changed,
            "files_changed": session_stats["files_changed"],
            "changed_fields": session_stats["changed_fields"],
            "translated_segments": session_stats["translated_segments"],
            "parse_errors": session_stats["parse_errors"],
            "error": session_stats.get("error", ""),
        }


def _fix_original_sessions_dir(original_sessions_dir: Path) -> dict[str, Any]:
    if not original_sessions_dir.is_dir():
        return {
            "loose_files_changed": 0,
            "loose_changed_fields": 0,
            "loose_translated_segments": 0,
            "loose_parse_errors": 0,
            "inner_zips_changed": 0,
            "inner_zip_translated_segments": 0,
            "inner_zip_parse_errors": 0,
            "inner_zip_errors": 0,
        }

    loose_stats = translate_chinese_text_in_sessions(original_sessions_dir)
    inner_zips_changed = 0
    inner_zip_translated_segments = 0
    inner_zip_parse_errors = 0
    inner_zip_errors = 0

    for inner_zip in sorted(original_sessions_dir.rglob("*.zip")):
        try:
            stats = _fix_inner_sessions_zip(inner_zip)
        except Exception as exc:
            inner_zip_errors += 1
            log.warning("inner sessions zip cleanup failed for %s: %s", inner_zip, exc)
            continue
        if stats["changed"]:
            inner_zips_changed += 1
        inner_zip_translated_segments += int(stats["translated_segments"])
        inner_zip_parse_errors += int(stats["parse_errors"])

    return {
        "loose_files_changed": loose_stats["files_changed"],
        "loose_changed_fields": loose_stats["changed_fields"],
        "loose_translated_segments": loose_stats["translated_segments"],
        "loose_parse_errors": loose_stats["parse_errors"],
        "inner_zips_changed": inner_zips_changed,
        "inner_zip_translated_segments": inner_zip_translated_segments,
        "inner_zip_parse_errors": inner_zip_parse_errors,
        "inner_zip_errors": inner_zip_errors,
    }


def fix_task_zip(task_zip: Path, dry_run: bool, keep_work_dir: bool) -> dict[str, Any]:
    work_dir = Path(
        tempfile.mkdtemp(prefix=task_zip.stem + ".", dir=str(task_zip.parent))
    )
    extract_root = work_dir / "extract"
    try:
        _extract_zip(task_zip, extract_root)

        package_stats = translate_chinese_text_in_repo_files(extract_root)
        sessions_stats = _fix_original_sessions_dir(extract_root / "original_sessions")

        validator = _run_validator(extract_root, task_zip)

        changed = any(
            [
                package_stats["files_changed"] > 0,
                sessions_stats["loose_files_changed"] > 0,
                sessions_stats["loose_changed_fields"] > 0,
                sessions_stats["loose_translated_segments"] > 0,
                sessions_stats["inner_zips_changed"] > 0,
                sessions_stats["inner_zip_translated_segments"] > 0,
            ]
        )

        if changed and not dry_run:
            _remove_validator_tmp(extract_root)
            _rewrite_zip_from_dir(extract_root, task_zip)

        status = "fixed_and_valid" if changed and validator["exit_code"] == 0 else ""
        if not status and changed:
            status = "fixed_but_still_invalid"
        if not status and validator["exit_code"] == 0:
            status = "no_changes_valid"
        if not status:
            status = "no_changes_invalid"

        return {
            "status": status,
            "changed": changed,
            "package_files_changed": package_stats["files_changed"],
            "package_translated_segments": package_stats["translated_segments"],
            "package_skipped_binary": package_stats["skipped_binary"],
            "package_read_errors": package_stats["read_errors"],
            "sessions_loose_files_changed": sessions_stats["loose_files_changed"],
            "sessions_loose_changed_fields": sessions_stats["loose_changed_fields"],
            "sessions_loose_translated_segments": sessions_stats["loose_translated_segments"],
            "sessions_loose_parse_errors": sessions_stats["loose_parse_errors"],
            "sessions_inner_zips_changed": sessions_stats["inner_zips_changed"],
            "sessions_inner_zip_translated_segments": sessions_stats["inner_zip_translated_segments"],
            "sessions_inner_zip_parse_errors": sessions_stats["inner_zip_parse_errors"],
            "sessions_inner_zip_errors": sessions_stats["inner_zip_errors"],
            "validation_exit_code": validator["exit_code"],
            "validation_output_dir": validator["output_dir"],
            "validation_report_path": validator["report_path"],
            "validation_stdout_path": validator["stdout_path"],
            "validation_stderr_path": validator["stderr_path"],
            "work_dir": str(work_dir),
        }
    finally:
        if not keep_work_dir and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="A TASK-*.zip file, or a directory containing them (recursive).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply the fix in a temp extract and revalidate, but do not overwrite the zip.",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Keep per-zip extracted temp dirs for inspection.",
    )
    parser.add_argument(
        "--pattern",
        default="TASK-*.zip",
        help="Glob pattern used when <path> is a directory. Default: TASK-*.zip.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args(argv)

    target = args.path
    if not target.exists():
        log.error("path does not exist: %s", target)
        return 2

    zips = _find_task_zips(target, args.pattern)
    if not zips:
        log.error("no zip files matching %r found under %s", args.pattern, target)
        return 2

    counts: dict[str, int] = {}
    had_error = False

    log.info("found %d zip(s) to inspect under %s", len(zips), target)
    for idx, task_zip in enumerate(zips, start=1):
        log.info("[%d/%d] repairing %s", idx, len(zips), task_zip)
        try:
            result = fix_task_zip(
                task_zip=task_zip,
                dry_run=args.dry_run,
                keep_work_dir=args.keep_work_dir,
            )
        except Exception as exc:
            had_error = True
            counts["error"] = counts.get("error", 0) + 1
            log.error("%s: FAILED to repair: %s", task_zip, exc)
            continue

        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1
        log.info(
            "%s: %s | changed=%s | package_files=%d | inner_zips=%d | validator_exit=%d | artifacts=%s",
            task_zip,
            status,
            result["changed"],
            result["package_files_changed"],
            result["sessions_inner_zips_changed"],
            result["validation_exit_code"],
            result["validation_output_dir"],
        )
        if args.keep_work_dir:
            log.info("%s: kept work dir at %s", task_zip, result["work_dir"])
        if result["validation_exit_code"] != 0:
            had_error = True

    log.info("done: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
