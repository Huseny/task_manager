#!/usr/bin/env python3
"""Fix TASK-*.zip packages produced by an older export_tasks where the
sessions zip was written at the package root instead of inside
original_sessions/.

Old layout (broken):
    TASK-<id>.zip
        metadata.json
        repo/...
        <claude-project-name>.zip   <-- loose at root
        ...

New layout (correct):
    TASK-<id>.zip
        metadata.json
        repo/...
        original_sessions/
            <claude-project-name>.zip
        ...

Usage:
    python fix_sessions_zip_location.py <path>

<path> can be:
    * a single TASK-*.zip file
    * a directory containing TASK-*/TASK-*.zip exports (the standard
      output layout) -- every TASK-*.zip found at any depth is checked.

Already-correct packages are skipped. Originals are overwritten in place
after a successful rewrite.
"""

from __future__ import annotations

import argparse
import io
import logging
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

log = logging.getLogger("fix_sessions_zip")


def _find_task_zips(target: Path, pattern: str) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() == ".zip":
            return [target]
        return []
    if target.is_dir():
        return sorted(p for p in target.rglob(pattern) if p.is_file())
    return []


def _zip_contains_member(zf: zipfile.ZipFile, prefix: str) -> bool:
    return any(name.startswith(prefix) for name in zf.namelist())


def _root_loose_zip_name(zf: zipfile.ZipFile) -> str | None:
    """Return the name of a loose zip at the package root, if any."""
    for name in zf.namelist():
        if "/" in name:
            continue
        if not name.lower().endswith(".zip"):
            continue
        return name
    return None


def fix_task_zip(task_zip: Path, dry_run: bool) -> str:
    """Returns one of: 'fixed', 'already_correct', 'no_sessions_zip',
    'multiple_root_zips', 'has_loose_and_dir'."""
    with zipfile.ZipFile(task_zip, "r") as zf:
        names = zf.namelist()
        has_orig_dir = any(
            n == "original_sessions/" or n.startswith("original_sessions/")
            for n in names
        )
        root_zips = [
            n for n in names if "/" not in n and n.lower().endswith(".zip")
        ]

    if not root_zips and has_orig_dir:
        return "already_correct"
    if not root_zips and not has_orig_dir:
        return "no_sessions_zip"
    if len(root_zips) > 1:
        log.warning(
            "%s: %d loose root zip files found, expected 1: %s",
            task_zip,
            len(root_zips),
            root_zips,
        )
        return "multiple_root_zips"
    if has_orig_dir:
        # Both present -- ambiguous, refuse to rewrite.
        log.warning(
            "%s: has both a root *.zip (%s) and an original_sessions/ entry; "
            "skipping to avoid clobbering",
            task_zip,
            root_zips[0],
        )
        return "has_loose_and_dir"

    sessions_zip_name = root_zips[0]

    if dry_run:
        log.info(
            "[DRY-RUN] %s: would move %s -> original_sessions/%s",
            task_zip,
            sessions_zip_name,
            sessions_zip_name,
        )
        return "fixed"

    # Stream-rewrite into a temp file, then atomically replace.
    fd, tmp_path_str = tempfile.mkstemp(
        suffix=".zip", prefix=task_zip.stem + ".", dir=task_zip.parent
    )
    tmp_path = Path(tmp_path_str)
    try:
        with open(fd, "wb") as out_fh:
            with zipfile.ZipFile(task_zip, "r") as src, zipfile.ZipFile(
                out_fh, "w", compression=zipfile.ZIP_DEFLATED
            ) as dst:
                for info in src.infolist():
                    data = src.read(info.filename)
                    if info.filename == sessions_zip_name:
                        new_info = zipfile.ZipInfo(
                            filename=f"original_sessions/{sessions_zip_name}",
                            date_time=info.date_time,
                        )
                        new_info.compress_type = zipfile.ZIP_DEFLATED
                        new_info.external_attr = info.external_attr
                        dst.writestr(new_info, data)
                    else:
                        # Preserve metadata via writestr with the original
                        # ZipInfo so timestamps/permissions survive.
                        new_info = zipfile.ZipInfo(
                            filename=info.filename,
                            date_time=info.date_time,
                        )
                        new_info.compress_type = (
                            info.compress_type
                            if info.compress_type != zipfile.ZIP_STORED
                            else zipfile.ZIP_DEFLATED
                        )
                        new_info.external_attr = info.external_attr
                        dst.writestr(new_info, data)
        shutil.move(str(tmp_path), str(task_zip))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    log.info(
        "%s: moved %s -> original_sessions/%s",
        task_zip,
        sessions_zip_name,
        sessions_zip_name,
    )
    return "fixed"


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
        help="Report what would change without modifying any zips.",
    )
    parser.add_argument(
        "--pattern",
        default="TASK-*.zip",
        help=(
            "Glob pattern (relative to <path>) used when <path> is a "
            "directory. Default: TASK-*.zip. Use '*.zip' to match every "
            "zip recursively."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args(argv)

    target: Path = args.path
    if not target.exists():
        log.error("path does not exist: %s", target)
        return 2

    zips = _find_task_zips(target, args.pattern)
    if not zips:
        log.error(
            "no zip files matching %r found under %s", args.pattern, target
        )
        return 2

    log.info("found %d zip(s) to inspect under %s", len(zips), target)
    counts: dict[str, int] = {}
    for i, z in enumerate(zips, start=1):
        log.info("[%d/%d] inspecting %s", i, len(zips), z)
        try:
            outcome = fix_task_zip(z, dry_run=args.dry_run)
        except Exception as exc:
            log.error("%s: FAILED to rewrite: %s", z, exc)
            counts["error"] = counts.get("error", 0) + 1
            continue
        counts[outcome] = counts.get(outcome, 0) + 1

    log.info("done: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0 if counts.get("error", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
