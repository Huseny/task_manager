#!/usr/bin/env python3
"""
Standalone runner for original-sessions normalization inside ZIP archives.

Usage:
    python3 normalize_sessions_zip.py /path/to/input_dir
    python3 normalize_sessions_zip.py --input-dir /path/to/input_dir --dry-run

Behavior:
- Iterates each top-level *.zip directly under input_dir.
- For each ZIP:
    - extracts to a temp directory
    - finds nested directories named "original-sessions"
    - processes top-level *.json and *.jsonl files under each original-sessions (non-recursive)
            - rewrites model/provider fields:
                * provider names -> normalized canonical names (openai/anthropic)
                * openai/anthropic provider (or inferred provider) -> model becomes claude-opus-4-6
                * model-switch text markers (e.g., local-command-stdout "Set model to ...") -> Opus 4.6
    - if not dry-run, writes transformed ZIP to: <input_dir>/transformed/<same_filename.zip>
        (original ZIP is never modified)
- Writes a CSV report in transformed dir (non-dry-run), and prints aggregate summary.
- Dry-run prints compact change markers (file:line-or-file, fields_changed) and writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

MODEL_KEYS = {
    "model",
    "modelid",
    "model_id",
    "assistant_model",
    "default_model",
    "requested_model",
}

PROVIDER_KEYS = {
    "provider",
    "providerid",
    "provider_id",
    "model_provider",
}

OPENAI_MODEL = "gpt-5.4"
ANTHROPIC_MODEL = "claude-opus-4-6"
ANTHROPIC_MODEL_NEW = "claude-opus-4-7"
DISPLAY_OPUS_MODEL = "Opus 4.6"
DISPLAY_OPUS_MODEL_NEW = "Opus 4.7"
OPUS_MODEL_CUTOFF = datetime(2026, 4, 16, tzinfo=timezone.utc)


def parse_iso_timestamp(value: Any) -> "datetime | None":
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def select_anthropic_model(timestamp: "datetime | None") -> tuple[str, str]:
    if timestamp is not None and timestamp >= OPUS_MODEL_CUTOFF:
        return ANTHROPIC_MODEL_NEW, DISPLAY_OPUS_MODEL_NEW
    return ANTHROPIC_MODEL, DISPLAY_OPUS_MODEL


def find_earliest_timestamp_in_payload(data: Any) -> "datetime | None":
    earliest: "datetime | None" = None
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "timestamp":
                ts = parse_iso_timestamp(value)
                if ts is not None and (earliest is None or ts < earliest):
                    earliest = ts
            nested = find_earliest_timestamp_in_payload(value)
            if nested is not None and (earliest is None or nested < earliest):
                earliest = nested
    elif isinstance(data, list):
        for item in data:
            nested = find_earliest_timestamp_in_payload(item)
            if nested is not None and (earliest is None or nested < earliest):
                earliest = nested
    return earliest


class ProcessFileResult(TypedDict):
    changed_lines: int
    changed_fields: int
    parse_errors: int
    details: list[str]


def normalize_provider(value: str | None) -> str | None:
    if not value:
        return None
    provider = str(value).strip().lower()
    if "openai" in provider:
        return "openai"
    if "anthropic" in provider:
        return "anthropic"
    return None


def infer_provider_from_model(value: str | None) -> str | None:
    if not value:
        return None
    model = str(value).strip().lower()
    if "claude" in model or "anthropic" in model:
        return "anthropic"
    if "gpt" in model or "openai" in model:
        return "openai"
    return None


def normalize_model_change_text(
    value: str, session_timestamp: "datetime | None" = None
) -> tuple[str, int]:
    updated = value
    _, display_model = select_anthropic_model(session_timestamp)

    # Normalize CLI stdout model-switch marker payloads.
    updated = re.sub(
        r"(?is)(<local-command-stdout>\s*set\s+model\s+to\s*)(.*?)(</local-command-stdout>)",
        lambda m: f"{m.group(1)}\u001b[1m{display_model}\u001b[22m{m.group(3)}",
        updated,
    )

    changed = 1 if updated != value else 0
    return updated, changed


def rewrite_models_by_provider(
    data: Any,
    inherited_provider: str | None = None,
    session_timestamp: "datetime | None" = None,
) -> tuple[Any, int]:
    changed = 0

    if isinstance(data, dict):
        provider_fields = [
            k
            for k in data.keys()
            if k.lower() in PROVIDER_KEYS and isinstance(data[k], str)
        ]
        model_fields = [
            k
            for k in data.keys()
            if k.lower() in MODEL_KEYS and isinstance(data[k], str)
        ]

        local_provider = None
        for key in provider_fields:
            normalized = normalize_provider(str(data[key]))
            if normalized:
                local_provider = normalized
                break

        inferred_provider = None
        for key in model_fields:
            inferred = infer_provider_from_model(str(data[key]))
            if inferred:
                inferred_provider = inferred
                break

        effective_provider = local_provider or inferred_provider or inherited_provider

        if effective_provider in {"openai", "anthropic"}:
            for key in provider_fields:
                if data[key] != effective_provider:
                    data[key] = effective_provider
                    changed += 1

        if effective_provider == "openai":
            for key in model_fields:
                if data[key] != OPENAI_MODEL:
                    changed += 1
                    data[key] = OPENAI_MODEL
        elif effective_provider == "anthropic":
            anthropic_model, _ = select_anthropic_model(session_timestamp)
            for key in model_fields:
                if data[key] != anthropic_model:
                    data[key] = anthropic_model
                    changed += 1

        for key, value in data.items():
            new_value, nested_changed = rewrite_models_by_provider(
                value, effective_provider, session_timestamp
            )
            data[key] = new_value
            changed += nested_changed
        return data, changed

    if isinstance(data, list):
        for idx, item in enumerate(data):
            new_item, nested_changed = rewrite_models_by_provider(
                item, inherited_provider, session_timestamp
            )
            data[idx] = new_item
            changed += nested_changed
        return data, changed

    if isinstance(data, str):
        return normalize_model_change_text(data, session_timestamp)

    return data, 0


def process_jsonl_file(path: Path, dry_run: bool) -> ProcessFileResult:
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()

    session_timestamp: "datetime | None" = None
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            ts = parse_iso_timestamp(payload.get("timestamp"))
            if ts is not None and (session_timestamp is None or ts < session_timestamp):
                session_timestamp = ts

    out_lines: list[str] = []
    changed_lines = 0
    changed_fields = 0
    parse_errors = 0
    details: list[str] = []

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            out_lines.append(line)
            continue

        try:
            payload = json.loads(line)
        except Exception:
            parse_errors += 1
            out_lines.append(line)
            continue

        line_timestamp = (
            parse_iso_timestamp(payload.get("timestamp"))
            if isinstance(payload, dict)
            else None
        ) or session_timestamp
        updated_payload, changed = rewrite_models_by_provider(
            payload, session_timestamp=line_timestamp
        )
        changed_fields += changed
        if changed > 0:
            changed_lines += 1
            if dry_run:
                details.append(f"{path}:{idx}: fields_changed={changed}")
            out_lines.append(
                json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))
            )
        else:
            out_lines.append(line)

    if not dry_run and changed_lines > 0:
        path.write_text(
            "\n".join(out_lines) + ("\n" if raw_text.endswith("\n") else ""),
            encoding="utf-8",
        )

    return {
        "changed_lines": changed_lines,
        "changed_fields": changed_fields,
        "parse_errors": parse_errors,
        "details": details,
    }


def process_json_file(path: Path, dry_run: bool) -> ProcessFileResult:
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    details: list[str] = []

    try:
        payload = json.loads(raw_text)
    except Exception:
        return {
            "changed_lines": 0,
            "changed_fields": 0,
            "parse_errors": 1,
            "details": details,
        }

    session_timestamp = find_earliest_timestamp_in_payload(payload)
    updated_payload, changed = rewrite_models_by_provider(
        payload, session_timestamp=session_timestamp
    )
    if changed > 0:
        if dry_run:
            details.append(f"{path}: fields_changed={changed}")
        else:
            path.write_text(
                json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))
                + ("\n" if raw_text.endswith("\n") else ""),
                encoding="utf-8",
            )

    return {
        "changed_lines": 1 if changed > 0 else 0,
        "changed_fields": changed,
        "parse_errors": 0,
        "details": details,
    }


def run_original_sessions_dir(directory: Path, dry_run: bool) -> dict[str, int]:
    files = sorted(
        [
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in {".json", ".jsonl"}
        ]
    )
    jsonl_files = len([p for p in files if p.suffix.lower() == ".jsonl"])
    json_files = len([p for p in files if p.suffix.lower() == ".json"])
    print(f"Directory: {directory}")
    print(
        f"Top-level session files found: {len(files)} "
        f"(jsonl={jsonl_files}, json={json_files})"
    )

    changed_files = 0
    changed_lines = 0
    changed_fields = 0
    parse_errors = 0

    for file_path in files:
        if file_path.suffix.lower() == ".jsonl":
            result = process_jsonl_file(file_path, dry_run=dry_run)
        else:
            result = process_json_file(file_path, dry_run=dry_run)
        changed_lines += result["changed_lines"]
        changed_fields += result["changed_fields"]
        parse_errors += result["parse_errors"]
        if result["changed_lines"] > 0:
            changed_files += 1
        print(
            f"- {file_path.name}: changed_lines={result['changed_lines']}, "
            f"changed_fields={result['changed_fields']}, parse_errors={result['parse_errors']}"
        )
        if dry_run:
            for detail in result["details"]:
                print(detail)

    return {
        "session_files": len(files),
        "jsonl_files": jsonl_files,
        "json_files": json_files,
        "changed_files": changed_files,
        "changed_lines": changed_lines,
        "changed_fields": changed_fields,
        "parse_errors": parse_errors,
    }


def iter_top_level_zip_files(root: Path) -> list[Path]:
    return sorted(
        [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
    )


def find_original_sessions_in_extracted_tree(extracted_root: Path) -> list[Path]:
    return sorted(
        set([p for p in extracted_root.rglob("original-sessions") if p.is_dir()])
    )


def rebuild_zip_from_directory(source_root: Path, zip_output_path: Path) -> None:
    with zipfile.ZipFile(zip_output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(source_root).as_posix()
            zf.write(path, arcname=arcname)


def is_under_original_sessions(path: Path) -> bool:
    return "original-sessions" in path.parts


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_non_original_manifest(root: Path) -> dict[str, str]:
    """
    Build file manifest for everything EXCEPT any path under original-sessions.
    Key: relative POSIX path, Value: sha256 hash
    """
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if is_under_original_sessions(rel):
            continue
        manifest[rel.as_posix()] = file_sha256(path)
    return manifest


def diff_manifest(before: dict[str, str], after: dict[str, str]) -> dict[str, int]:
    before_keys = set(before.keys())
    after_keys = set(after.keys())
    added = after_keys - before_keys
    removed = before_keys - after_keys
    common = before_keys & after_keys
    changed = {k for k in common if before[k] != after[k]}
    return {
        "before_files": len(before_keys),
        "after_files": len(after_keys),
        "added_files": len(added),
        "removed_files": len(removed),
        "changed_files": len(changed),
    }


def process_zip_archive(
    zip_path: Path, output_dir: Path, dry_run: bool
) -> dict[str, int | str]:
    totals = {
        "archive_name": zip_path.name,
        "directories": 0,
        "session_files": 0,
        "jsonl_files": 0,
        "json_files": 0,
        "changed_files": 0,
        "changed_lines": 0,
        "changed_fields": 0,
        "parse_errors": 0,
        "archives_written": 0,
        "output_zip": "",
        "non_original_before_files": 0,
        "non_original_after_files": 0,
        "non_original_added_files": 0,
        "non_original_removed_files": 0,
        "non_original_changed_files": 0,
    }

    with tempfile.TemporaryDirectory(prefix="normalize_sessions_") as tmp:
        tmp_root = Path(tmp)
        extracted_root = tmp_root / "extracted"
        extracted_root.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extracted_root)
        except zipfile.BadZipFile:
            print(f"WARNING: Skipping invalid ZIP archive: {zip_path}")
            return totals

        non_original_before = build_non_original_manifest(extracted_root)
        targets = find_original_sessions_in_extracted_tree(extracted_root)
        print(f"Archive: {zip_path}")
        print(f"Found original-sessions directories: {len(targets)}")
        if not targets:
            return totals

        for target in targets:
            print("")
            stats = run_original_sessions_dir(target, dry_run=dry_run)
            totals["directories"] += 1
            totals["session_files"] += stats["session_files"]
            totals["jsonl_files"] += stats["jsonl_files"]
            totals["json_files"] += stats["json_files"]
            totals["changed_files"] += stats["changed_files"]
            totals["changed_lines"] += stats["changed_lines"]
            totals["changed_fields"] += stats["changed_fields"]
            totals["parse_errors"] += stats["parse_errors"]

        non_original_after = build_non_original_manifest(extracted_root)
        non_original_diff = diff_manifest(non_original_before, non_original_after)
        totals["non_original_before_files"] = non_original_diff["before_files"]
        totals["non_original_after_files"] = non_original_diff["after_files"]
        totals["non_original_added_files"] = non_original_diff["added_files"]
        totals["non_original_removed_files"] = non_original_diff["removed_files"]
        totals["non_original_changed_files"] = non_original_diff["changed_files"]

        if not dry_run:
            rebuilt_zip = tmp_root / "rebuilt.zip"
            rebuild_zip_from_directory(extracted_root, rebuilt_zip)
            output_zip = output_dir / zip_path.name
            output_zip.parent.mkdir(parents=True, exist_ok=True)
            output_zip.write_bytes(rebuilt_zip.read_bytes())
            totals["archives_written"] = 1
            totals["output_zip"] = str(output_zip)

    return totals


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Iterate top-level ZIP files under an input directory, extract and find inner "
            "original-sessions directories, normalize top-level JSON/JSONL session files, "
            "and re-pack ZIPs."
        )
    )
    parser.add_argument(
        "root_dir",
        nargs="?",
        help="Input directory containing top-level ZIP archives (positional alias).",
    )
    parser.add_argument(
        "--input-dir",
        dest="input_dir",
        help="Input directory containing top-level ZIP archives.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change (file + line only), without writing files.",
    )
    parser.add_argument(
        "--output-dir",
        default="transformed",
        help='Output directory name/path for transformed ZIPs (default: "transformed" under input-dir).',
    )
    args = parser.parse_args()

    input_dir_arg = args.input_dir or args.root_dir
    if not input_dir_arg:
        parser.error("Provide input directory via positional root_dir or --input-dir")

    root = Path(input_dir_arg)
    if not root.exists() or not root.is_dir():
        print(f"ERROR: input directory not found: {root}")
        return 1

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    archives = iter_top_level_zip_files(root)
    print(f"Root: {root}")
    print(f"Found top-level ZIP archives: {len(archives)}")
    if not args.dry_run:
        print(f"Transformed output dir: {output_dir}")
    if not archives:
        return 0

    totals = {
        "archives": 0,
        "archives_written": 0,
        "directories": 0,
        "session_files": 0,
        "jsonl_files": 0,
        "json_files": 0,
        "changed_files": 0,
        "changed_lines": 0,
        "changed_fields": 0,
        "parse_errors": 0,
    }
    per_archive_rows: list[dict[str, int | str]] = []

    for archive in archives:
        print("")
        stats = process_zip_archive(
            archive, output_dir=output_dir, dry_run=args.dry_run
        )
        per_archive_rows.append(stats)
        totals["archives"] += 1
        totals["archives_written"] += int(stats["archives_written"])
        totals["directories"] += int(stats["directories"])
        totals["session_files"] += int(stats["session_files"])
        totals["jsonl_files"] += int(stats["jsonl_files"])
        totals["json_files"] += int(stats["json_files"])
        totals["changed_files"] += int(stats["changed_files"])
        totals["changed_lines"] += int(stats["changed_lines"])
        totals["changed_fields"] += int(stats["changed_fields"])
        totals["parse_errors"] += int(stats["parse_errors"])

    if not args.dry_run:
        report_path = output_dir / "normalization_report.csv"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "archive_name",
                    "output_zip",
                    "directories",
                    "session_files",
                    "jsonl_files",
                    "json_files",
                    "changed_files",
                    "changed_lines",
                    "changed_fields",
                    "parse_errors",
                    "non_original_before_files",
                    "non_original_after_files",
                    "non_original_added_files",
                    "non_original_removed_files",
                    "non_original_changed_files",
                ]
            )
            for row in per_archive_rows:
                writer.writerow(
                    [
                        row.get("archive_name", ""),
                        row.get("output_zip", ""),
                        row.get("directories", 0),
                        row.get("session_files", 0),
                        row.get("jsonl_files", 0),
                        row.get("json_files", 0),
                        row.get("changed_files", 0),
                        row.get("changed_lines", 0),
                        row.get("changed_fields", 0),
                        row.get("parse_errors", 0),
                        row.get("non_original_before_files", 0),
                        row.get("non_original_after_files", 0),
                        row.get("non_original_added_files", 0),
                        row.get("non_original_removed_files", 0),
                        row.get("non_original_changed_files", 0),
                    ]
                )
        print(f"Report: {report_path}")

    mode = "DRY RUN" if args.dry_run else "APPLY"
    print("")
    print(f"{mode} aggregate summary:")
    print(f"  archives: {totals['archives']}")
    print(f"  archives_written: {totals['archives_written']}")
    print(f"  directories: {totals['directories']}")
    print(f"  session_files: {totals['session_files']}")
    print(f"  jsonl_files: {totals['jsonl_files']}")
    print(f"  json_files: {totals['json_files']}")
    print(f"  changed_files: {totals['changed_files']}")
    print(f"  changed_lines: {totals['changed_lines']}")
    print(f"  changed_fields: {totals['changed_fields']}")
    print(f"  parse_errors: {totals['parse_errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
