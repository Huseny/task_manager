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
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
import zipfile
from pathlib import Path
from typing import Any

from export_tasks import (
    CHINESE_CHAR_RE,
    CHINESE_SEGMENT_RE,
    REPO_TEXT_CLEANUP_SKIP_DIR_NAMES,
    SAFE_FULL_TEXT_REPO_EXTENSIONS,
    contains_chinese_text,
    zip_directory_contents,
)

log = logging.getLogger("fix_chinese_task_zips")


def _prompt_for_translation(chinese_text: str) -> str:
    """Prompt the user to provide English replacement for Chinese text."""
    print("\n" + "=" * 60)
    print(f"Found Chinese text: {chinese_text}")
    print("=" * 60)
    while True:
        english_text = input(
            "Enter English replacement (or 'skip' to keep Chinese): "
        ).strip()
        if english_text.lower() == "skip":
            return chinese_text
        if english_text:
            return english_text
        print("Please enter a non-empty translation or 'skip'.")


def _replace_chinese_segments_in_text_interactive(
    text: str, cache: dict[str, str]
) -> tuple[str, int, int]:
    """Replace Chinese segments with user-provided English translations."""
    if not text or not contains_chinese_text(text):
        return text, 0, 0

    replacements = 0
    translated_segments = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements, translated_segments
        segment = match.group(0)
        stripped = segment.strip()
        if not stripped or not contains_chinese_text(stripped):
            return segment

        # Check cache first
        if stripped in cache:
            translated = cache[stripped]
        else:
            # Prompt user
            translated = _prompt_for_translation(stripped)
            cache[stripped] = translated

        if not translated or translated == stripped:
            return segment

        translated_segments += 1
        replacements += 1
        prefix_len = len(segment) - len(segment.lstrip())
        suffix_len = len(segment) - len(segment.rstrip())
        prefix = segment[:prefix_len]
        suffix = segment[len(segment) - suffix_len :] if suffix_len else ""
        return f"{prefix}{translated}{suffix}"

    updated = CHINESE_SEGMENT_RE.sub(_replace, text)
    return updated, replacements, translated_segments


def _translate_chinese_strings_in_payload_interactive(
    value: Any, cache: dict[str, str]
) -> tuple[Any, int, int]:
    """Recursively translate Chinese strings in JSON payloads with user prompts."""
    if isinstance(value, dict):
        changed = 0
        translated_segments = 0
        updated: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, item_changed, item_segments = (
                _translate_chinese_strings_in_payload_interactive(item, cache)
            )
            updated[key] = new_item
            changed += item_changed
            translated_segments += item_segments
        return updated, changed, translated_segments

    if isinstance(value, list):
        changed = 0
        translated_segments = 0
        updated_items: list[Any] = []
        for item in value:
            new_item, item_changed, item_segments = (
                _translate_chinese_strings_in_payload_interactive(item, cache)
            )
            updated_items.append(new_item)
            changed += item_changed
            translated_segments += item_segments
        return updated_items, changed, translated_segments

    if isinstance(value, str):
        updated, replacements, translated_segments = (
            _replace_chinese_segments_in_text_interactive(value, cache)
        )
        return updated, replacements, translated_segments

    return value, 0, 0


def _translate_chinese_text_in_sessions_interactive(
    root: Path, cache: dict[str, str]
) -> dict[str, Any]:
    """Translate Chinese text in session JSON/JSONL files with user prompts."""
    files_changed = 0
    changed_fields = 0
    translated_segments = 0
    parse_errors = 0
    changed_files: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl"}:
            continue

        if path.suffix.lower() == ".jsonl":
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.warning("chinese cleanup: cannot read %s: %s", path, exc)
                continue

            lines = raw_text.splitlines()
            out_lines: list[str] = []
            file_changed = False

            for line in lines:
                if not line.strip():
                    out_lines.append(line)
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    parse_errors += 1
                    out_lines.append(line)
                    continue

                updated_payload, line_changed, line_segments = (
                    _translate_chinese_strings_in_payload_interactive(payload, cache)
                )
                changed_fields += line_changed
                translated_segments += line_segments
                if line_changed > 0:
                    file_changed = True
                    out_lines.append(
                        json.dumps(
                            updated_payload, ensure_ascii=False, separators=(",", ":")
                        )
                    )
                else:
                    out_lines.append(line)

            if file_changed:
                path.write_text(
                    "\n".join(out_lines) + ("\n" if raw_text.endswith("\n") else ""),
                    encoding="utf-8",
                )
                files_changed += 1
                changed_files.append(str(path))
            continue

        try:
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(raw_text)
        except Exception:
            parse_errors += 1
            continue

        updated_payload, file_changed, file_segments = (
            _translate_chinese_strings_in_payload_interactive(payload, cache)
        )
        changed_fields += file_changed
        translated_segments += file_segments
        if file_changed > 0:
            path.write_text(
                json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))
                + ("\n" if raw_text.endswith("\n") else ""),
                encoding="utf-8",
            )
            files_changed += 1
            changed_files.append(str(path))

    return {
        "files_changed": files_changed,
        "changed_fields": changed_fields,
        "translated_segments": translated_segments,
        "parse_errors": parse_errors,
        "cache_entries": len(cache),
        "changed_files": changed_files,
        "error": "",
    }


def _classify_repo_text_cleanup(path: Path) -> str:
    """Classify file type for cleanup."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix in SAFE_FULL_TEXT_REPO_EXTENSIONS:
        return "full"
    return "full"


def _is_probably_binary_file(path: Path) -> bool:
    """Check if file is probably binary."""
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\x00" in sample


def _should_skip_repo_text_cleanup(path: Path, repo_dir: Path) -> bool:
    """Check if file should be skipped for cleanup."""
    try:
        rel_parts = path.relative_to(repo_dir).parts
    except ValueError:
        return True
    if not rel_parts:
        return True
    if "original_sessions" in rel_parts:
        return True
    return any(part in REPO_TEXT_CLEANUP_SKIP_DIR_NAMES for part in rel_parts[:-1])


def _translate_python_comments_and_strings_interactive(
    raw_text: str, cache: dict[str, str]
) -> tuple[str, int, int]:
    """Translate Chinese in Python comments and strings with user prompts."""
    changed_tokens = 0
    translated_segments = 0
    output_tokens: list[tokenize.TokenInfo] = []

    for token_info in tokenize.generate_tokens(io.StringIO(raw_text).readline):
        token_string = token_info.string
        if token_info.type == tokenize.COMMENT and contains_chinese_text(token_string):
            updated, replacements, segments = (
                _replace_chinese_segments_in_text_interactive(token_string, cache)
            )
            if replacements > 0:
                token_info = token_info._replace(string=updated)
                changed_tokens += replacements
                translated_segments += segments
        elif token_info.type == tokenize.STRING and contains_chinese_text(token_string):
            lower = token_string.lower()
            if "f" in lower[:2] or "b" in lower[:2]:
                output_tokens.append(token_info)
                continue
            try:
                import ast

                literal_value = ast.literal_eval(token_string)
            except Exception:
                output_tokens.append(token_info)
                continue
            if not isinstance(literal_value, str) or not contains_chinese_text(
                literal_value
            ):
                output_tokens.append(token_info)
                continue
            updated_value, replacements, segments = (
                _replace_chinese_segments_in_text_interactive(literal_value, cache)
            )
            if replacements > 0:
                token_info = token_info._replace(string=repr(updated_value))
                changed_tokens += replacements
                translated_segments += segments
        output_tokens.append(token_info)

    return tokenize.untokenize(output_tokens), changed_tokens, translated_segments


def _translate_chinese_text_in_repo_files_interactive(
    repo_dir: Path, cache: dict[str, str]
) -> dict[str, Any]:
    """Translate Chinese text in repo files with user prompts."""
    files_changed = 0
    translated_segments = 0
    skipped_binary = 0
    read_errors = 0
    changed_files: list[str] = []

    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file():
            continue
        if _should_skip_repo_text_cleanup(path, repo_dir):
            continue
        mode = _classify_repo_text_cleanup(path)
        if _is_probably_binary_file(path):
            skipped_binary += 1
            continue

        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        except OSError as exc:
            read_errors += 1
            log.warning("repo chinese cleanup: cannot read %s: %s", path, exc)
            continue

        if not raw_text or not contains_chinese_text(raw_text):
            continue

        if mode == "python":
            try:
                updated_text, replacements, file_segments = (
                    _translate_python_comments_and_strings_interactive(raw_text, cache)
                )
            except tokenize.TokenError as exc:
                read_errors += 1
                log.warning("repo chinese cleanup: cannot tokenize %s: %s", path, exc)
                continue
        else:
            updated_text, replacements, file_segments = (
                _replace_chinese_segments_in_text_interactive(raw_text, cache)
            )
        if replacements == 0:
            continue

        try:
            path.write_text(updated_text, encoding="utf-8")
        except OSError as exc:
            read_errors += 1
            log.warning("repo chinese cleanup: cannot write %s: %s", path, exc)
            continue

        files_changed += 1
        translated_segments += file_segments
        changed_files.append(str(path))

    return {
        "files_changed": files_changed,
        "translated_segments": translated_segments,
        "skipped_binary": skipped_binary,
        "read_errors": read_errors,
        "cache_entries": len(cache),
        "changed_files": changed_files,
        "error": "",
    }


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


def _generate_fix_report_json(task_zip: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Generate a comprehensive JSON report and save it next to the TASK zip."""
    out_dir = task_zip.parent / f"{task_zip.stem}__chinese_fix_validation"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine if validation failed after translation attempt
    validation_failed_after_translation = result["status"] == "fixed_but_still_invalid"

    report = {
        "task_zip": str(task_zip),
        "task_zip_filename": task_zip.name,
        "status": result["status"],
        "changed": result["changed"],
        "validation_exit_code": result["validation_exit_code"],
        "validation_failed_after_translation": validation_failed_after_translation,
        "package_translations": {
            "files_changed": result["package_files_changed"],
            "translated_segments": result["package_translated_segments"],
            "skipped_binary": result["package_skipped_binary"],
            "read_errors": result["package_read_errors"],
        },
        "sessions_translations": {
            "loose_files_changed": result["sessions_loose_files_changed"],
            "loose_changed_fields": result["sessions_loose_changed_fields"],
            "loose_translated_segments": result["sessions_loose_translated_segments"],
            "loose_parse_errors": result["sessions_loose_parse_errors"],
            "inner_zips_changed": result["sessions_inner_zips_changed"],
            "inner_zip_translated_segments": result[
                "sessions_inner_zip_translated_segments"
            ],
            "inner_zip_parse_errors": result["sessions_inner_zip_parse_errors"],
            "inner_zip_errors": result["sessions_inner_zip_errors"],
        },
        "validation_artifacts": {
            "output_dir": result["validation_output_dir"],
            "report_path": result["validation_report_path"],
            "stdout_path": result["validation_stdout_path"],
            "stderr_path": result["validation_stderr_path"],
        },
        "work_dir": result["work_dir"],
    }

    report_path = out_dir / "fix_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "report_data": report,
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


def _fix_inner_sessions_zip_interactive(
    inner_zip: Path, cache: dict[str, str]
) -> dict[str, Any]:
    """Fix Chinese text in inner sessions zip with user prompts."""
    with tempfile.TemporaryDirectory(prefix=inner_zip.stem + ".") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        extract_dir = tmp_dir / "extract"
        _extract_zip(inner_zip, extract_dir)
        session_stats = _translate_chinese_text_in_sessions_interactive(
            extract_dir, cache
        )
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


def _fix_original_sessions_dir_interactive(
    original_sessions_dir: Path, cache: dict[str, str]
) -> dict[str, Any]:
    """Fix Chinese text in original_sessions directory with user prompts."""
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

    loose_stats = _translate_chinese_text_in_sessions_interactive(
        original_sessions_dir, cache
    )
    inner_zips_changed = 0
    inner_zip_translated_segments = 0
    inner_zip_parse_errors = 0
    inner_zip_errors = 0

    for inner_zip in sorted(original_sessions_dir.rglob("*.zip")):
        try:
            stats = _fix_inner_sessions_zip_interactive(inner_zip, cache)
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
    # Use a single shared cache for all translations in this task zip
    translation_cache: dict[str, str] = {}
    try:
        _extract_zip(task_zip, extract_root)

        package_stats = _translate_chinese_text_in_repo_files_interactive(
            extract_root, translation_cache
        )
        sessions_stats = _fix_original_sessions_dir_interactive(
            extract_root / "original_sessions", translation_cache
        )

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

        result = {
            "status": status,
            "changed": changed,
            "package_files_changed": package_stats["files_changed"],
            "package_translated_segments": package_stats["translated_segments"],
            "package_skipped_binary": package_stats["skipped_binary"],
            "package_read_errors": package_stats["read_errors"],
            "sessions_loose_files_changed": sessions_stats["loose_files_changed"],
            "sessions_loose_changed_fields": sessions_stats["loose_changed_fields"],
            "sessions_loose_translated_segments": sessions_stats[
                "loose_translated_segments"
            ],
            "sessions_loose_parse_errors": sessions_stats["loose_parse_errors"],
            "sessions_inner_zips_changed": sessions_stats["inner_zips_changed"],
            "sessions_inner_zip_translated_segments": sessions_stats[
                "inner_zip_translated_segments"
            ],
            "sessions_inner_zip_parse_errors": sessions_stats["inner_zip_parse_errors"],
            "sessions_inner_zip_errors": sessions_stats["inner_zip_errors"],
            "validation_exit_code": validator["exit_code"],
            "validation_output_dir": validator["output_dir"],
            "validation_report_path": validator["report_path"],
            "validation_stdout_path": validator["stdout_path"],
            "validation_stderr_path": validator["stderr_path"],
            "work_dir": str(work_dir),
        }

        # Generate JSON report
        report_info = _generate_fix_report_json(task_zip, result)
        result["report_json_path"] = report_info["report_path"]

        return result
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

        validation_failed_msg = ""
        if result.get("validation_failed_after_translation"):
            validation_failed_msg = " | VALIDATION FAILED AFTER TRANSLATION"

        log.info(
            "%s: %s | changed=%s | package_files=%d | inner_zips=%d | validator_exit=%d | report=%s%s",
            task_zip,
            status,
            result["changed"],
            result["package_files_changed"],
            result["sessions_inner_zips_changed"],
            result["validation_exit_code"],
            result.get("report_json_path", "N/A"),
            validation_failed_msg,
        )
        if args.keep_work_dir:
            log.info("%s: kept work dir at %s", task_zip, result["work_dir"])
        if result["validation_exit_code"] != 0:
            had_error = True

    log.info("done: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
