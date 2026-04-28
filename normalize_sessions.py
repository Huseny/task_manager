#!/usr/bin/env python3
"""
Extract user/assistant conversation from a session JSONL.

Usage:
    python3 normalize_sessions.py --extract-conversation /path/to/session.jsonl
    python3 normalize_sessions.py --extract-conversation /path/to/session.jsonl --minutes 45

Behavior:
- Reads JSONL session entries and keeps only the user/assistant conversation.
- Strips tool calls/results, meta/sidechain entries, and local-command noise.
- Limits extraction to the first N minutes from the first timestamp (default: 45).
- Always writes JSON output to a file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
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


def select_anthropic_model(timestamp: datetime | None) -> tuple[str, str]:
    if timestamp is not None and timestamp >= OPUS_MODEL_CUTOFF:
        return ANTHROPIC_MODEL_NEW, DISPLAY_OPUS_MODEL_NEW
    return ANTHROPIC_MODEL, DISPLAY_OPUS_MODEL


class ProcessFileResult(TypedDict):
    changed_lines: int
    changed_fields: int
    parse_errors: int
    details: list[str]


class ConversationTurn(TypedDict):
    timestamp: str
    role: str
    text: str


def parse_iso_timestamp(value: Any) -> datetime | None:
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


def is_non_conversation_user_text(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "<local-command-caveat>",
        "<local-command-stdout>",
        "<command-name>",
        "<command-message>",
    )
    return any(marker in lowered for marker in markers)


def extract_message_text(raw_message: Any, role: str) -> str | None:
    if isinstance(raw_message, str):
        text = raw_message.strip()
        if not text:
            return None
        if role == "user" and is_non_conversation_user_text(text):
            return None
        return text

    if not isinstance(raw_message, list):
        return None

    parts: list[str] = []
    for item in raw_message:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        if role == "user" and is_non_conversation_user_text(text):
            continue
        parts.append(text)

    if not parts:
        return None
    return "\n\n".join(parts)


def extract_conversation_turns(
    session_jsonl: Path, minutes: int
) -> tuple[list[ConversationTurn], int]:
    raw_text = session_jsonl.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()

    turns: list[ConversationTurn] = []
    parse_errors = 0
    start_timestamp: datetime | None = None
    cutoff_timestamp: datetime | None = None

    for line in lines:
        if not line.strip():
            continue

        try:
            payload = json.loads(line)
        except Exception:
            parse_errors += 1
            continue

        if not isinstance(payload, dict):
            continue

        timestamp = parse_iso_timestamp(payload.get("timestamp"))
        if timestamp is None:
            continue

        if start_timestamp is None:
            start_timestamp = timestamp
            cutoff_timestamp = start_timestamp + timedelta(minutes=minutes)

        if cutoff_timestamp is not None and timestamp > cutoff_timestamp:
            continue

        if payload.get("isMeta") is True:
            continue
        if payload.get("isSidechain") is True:
            continue

        if payload.get("type") not in {"user", "assistant"}:
            continue

        message = payload.get("message")
        if not isinstance(message, dict):
            continue

        role = str(message.get("role", "")).strip().lower()
        if role not in {"user", "assistant"}:
            continue

        text = extract_message_text(message.get("content"), role)
        if not text:
            continue

        turns.append(
            {
                "timestamp": timestamp.isoformat(),
                "role": role,
                "text": text,
            }
        )

    return turns, parse_errors


def find_earliest_timestamp_in_payload(data: Any) -> datetime | None:
    earliest: datetime | None = None
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


def get_earliest_timestamp_in_session_file(path: Path) -> datetime | None:
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    earliest: datetime | None = None
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        timestamp = parse_iso_timestamp(payload.get("timestamp"))
        if timestamp is None:
            continue
        if earliest is None or timestamp < earliest:
            earliest = timestamp
    return earliest


def find_earliest_session_file_in_directory(
    root: Path,
) -> tuple[Path | None, datetime | None]:
    candidates = sorted([p for p in root.rglob("*.jsonl") if p.is_file()])
    best_path: Path | None = None
    best_timestamp: datetime | None = None

    for candidate in candidates:
        earliest = get_earliest_timestamp_in_session_file(candidate)
        if earliest is None:
            continue
        if (
            best_timestamp is None
            or earliest < best_timestamp
            or (
                earliest == best_timestamp
                and best_path is not None
                and candidate.as_posix() < best_path.as_posix()
            )
        ):
            best_timestamp = earliest
            best_path = candidate

    return best_path, best_timestamp


def run_conversation_extract_mode(
    session_jsonl_path: Path,
    output_path: Path | None,
    minutes: int,
) -> int:
    if not session_jsonl_path.exists() or not session_jsonl_path.is_file():
        print(f"ERROR: session file not found: {session_jsonl_path}")
        return 1

    turns, parse_errors = extract_conversation_turns(session_jsonl_path, minutes=minutes)
    if output_path is None:
        output_path = session_jsonl_path.with_name(
            f"{session_jsonl_path.stem}_conversation_{minutes}m.json"
        )

    if output_path.suffix.lower() != ".json":
        output_path = output_path.with_suffix(".json")

    payload = {
        "source": str(session_jsonl_path),
        "minutes": minutes,
        "turns_extracted": len(turns),
        "parse_errors_skipped": parse_errors,
        "conversation": [
            {
                "timestamp": turn["timestamp"],
                "role": turn["role"],
                "content": turn["text"],
            }
            for turn in turns
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Conversation JSON written: {output_path}")

    print(f"Turns extracted: {len(turns)}")
    print(f"Parse errors skipped: {parse_errors}")
    return 0


def default_output_path_for_input(
    requested_output: Path | None,
    input_path: Path,
    selected_session_file: Path,
    minutes: int,
) -> Path:
    if requested_output is not None:
        return requested_output

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        return input_path.with_name(f"{input_path.stem}_conversation_{minutes}m.json")

    return selected_session_file.with_name(
        f"{selected_session_file.stem}_conversation_{minutes}m.json"
    )


def select_session_from_input(input_path: Path) -> tuple[Path, datetime | None]:
    if input_path.is_file() and input_path.suffix.lower() == ".jsonl":
        return input_path, get_earliest_timestamp_in_session_file(input_path)

    if input_path.is_dir():
        selected, first_ts = find_earliest_session_file_in_directory(input_path)
        if selected is None:
            raise ValueError(
                f"No parseable session JSONL files found in directory: {input_path}"
            )
        return selected, first_ts

    raise ValueError(
        "--extract-conversation must point to a .jsonl file, a directory, or a .zip file"
    )


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
    session_timestamp = get_earliest_timestamp_in_session_file(path)

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
            "Extract clean user/assistant conversation from a session JSONL for the first "
            "N minutes and write JSON output."
        )
    )
    parser.add_argument(
        "--extract-conversation",
        dest="extract_conversation",
        required=True,
        help=(
            "Path to a session JSONL file, directory, or ZIP archive. For directory/ZIP, "
            "the earliest session by timestamp is selected automatically."
        ),
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=45,
        help="Conversation extraction window in minutes (default: 45).",
    )
    parser.add_argument(
        "--conversation-output",
        dest="conversation_output",
        help=(
            "Output JSON file path (default: <input_stem>_conversation_<minutes>m.json "
            "next to input)."
        ),
    )
    args = parser.parse_args()

    if args.minutes <= 0:
        print("ERROR: --minutes must be > 0")
        return 1

    input_path = Path(args.extract_conversation)
    requested_output = Path(args.conversation_output) if args.conversation_output else None

    if not input_path.exists():
        print(f"ERROR: input path not found: {input_path}")
        return 1

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="session_extract_") as tmp:
            extracted_root = Path(tmp) / "unzipped"
            extracted_root.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(input_path, "r") as zf:
                    zf.extractall(extracted_root)
            except zipfile.BadZipFile:
                print(f"ERROR: invalid ZIP archive: {input_path}")
                return 1

            selected_file, first_ts = find_earliest_session_file_in_directory(
                extracted_root
            )
            if selected_file is None:
                print(
                    f"ERROR: no parseable session JSONL files found in ZIP archive: {input_path}"
                )
                return 1

            print(f"Selected session file (from ZIP): {selected_file}")
            if first_ts is not None:
                print(f"Selected session first timestamp: {first_ts.isoformat()}")

            output_path = default_output_path_for_input(
                requested_output=requested_output,
                input_path=input_path,
                selected_session_file=selected_file,
                minutes=args.minutes,
            )
            return run_conversation_extract_mode(
                session_jsonl_path=selected_file,
                output_path=output_path,
                minutes=args.minutes,
            )

    try:
        selected_file, first_ts = select_session_from_input(input_path)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    if input_path.is_dir():
        print(f"Selected session file (from folder): {selected_file}")
        if first_ts is not None:
            print(f"Selected session first timestamp: {first_ts.isoformat()}")

    output_path = default_output_path_for_input(
        requested_output=requested_output,
        input_path=input_path,
        selected_session_file=selected_file,
        minutes=args.minutes,
    )

    return run_conversation_extract_mode(
        session_jsonl_path=selected_file,
        output_path=output_path,
        minutes=args.minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
