#!/usr/bin/env python3
"""Validator for task delivery packages.

Performs read-only checks against a delivery package and writes a markdown
report. The validator does not modify any package files.

In ``--prepare-ci`` mode the run is:
     1. Fetch the Aquila task once.
     2. Clone the GitHub repo.
     3. Pre-rearrangement validation: assert the cloned repo matches the
         ``validator.py``-style structure without expecting ``original_sessions/``.
         If this gate fails, write the report and exit before touching anything.
     4. Rearrange: extract the sessions zip into ``<repo>/original_sessions/``.
     5. Run full validation against the rearranged repo, reusing the already
       fetched Aquila task for cross-checks (no second HTTP call).

Sections (full validation):
    1. Input directory
    2. Structure (mandatory paths, .tmp layout, no temp/scratch leftovers)
    3. metadata.json schema
    4. original_sessions/ (file naming, bundle structure, prompt anchor,
       latest trajectory, forbidden keywords)
    5. Aquila alignment (prompt / project_type / database cross-checks)
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_PROJECT_TYPES = {"web", "server", "fullstack", "android", "ios", "desktop"}
REQUIRED_METADATA_FIELDS = {
    "prompt",
    "project_type",
    "frontend_language",
    "backend_language",
    "frontend_framework",
    "backend_framework",
    "database",
}
NULLABLE_METADATA_FIELDS = {
    "backend_language",
    "backend_framework",
    "frontend_language",
    "frontend_framework",
}

# Literal "none" is valid for fields where the absence of a component is expected.
METADATA_NONE_ALLOWED_FIELDS = set(NULLABLE_METADATA_FIELDS) | {"database"}

METADATA_INVALID_PLACEHOLDER_TOKENS = {
    "none",
    "null",
    "nil",
    "undefined",
    "na",
    "nan",
    "nill",
    "unknown",
    "tbd",
    "todo",
    "notset",
    "unset",
    "placeholder",
    "empty",
    "无",
    "暂无",
    "未填写",
    "待定",
    "空",
}

PROMPT_ENGLISH_RATIO_THRESHOLD = 0.70

CHINESE_RE = re.compile(r"[一-鿿]")


def _normalize_metadata_value_token(value: str) -> str:
    return re.sub(r"[\s\-_./\\|]+", "", value.strip().lower())


def is_invalid_metadata_placeholder(value: str, allow_none: bool = False) -> bool:
    token = _normalize_metadata_value_token(value)
    if not token:
        return False
    if allow_none and token == "none":
        return False
    return token in METADATA_INVALID_PLACEHOLDER_TOKENS


def _looks_like_windows_abs_path(value: str) -> bool:
    """Reject Windows-style absolute paths (e.g. ``C:\\foo``) in compose context."""
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _is_dockerfile_name(name: str) -> bool:
    """Match Dockerfile / Dockerfile.* / *.Dockerfile (case-insensitive)."""
    lower = name.lower()
    if lower == "dockerfile":
        return True
    if lower.startswith("dockerfile."):
        return True
    if lower.endswith(".dockerfile") and lower != ".dockerfile":
        return True
    return False


def _load_yaml_module() -> Any:
    try:
        return importlib.import_module("yaml")
    except ImportError:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "PyYAML"],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ValidatorError("PyYAML is required but pip is not available") from exc
        except subprocess.CalledProcessError as exc:
            raise ValidatorError(
                "PyYAML is required and automatic installation failed"
            ) from exc
        return importlib.import_module("yaml")


def _compose_port_text_mentions_3000(value: Any) -> bool:
    def _range_includes_3000(text: str) -> bool:
        parts = text.split("-", 1)
        if len(parts) == 1:
            try:
                return int(parts[0]) == 3000
            except ValueError:
                return False
        try:
            start = int(parts[0])
            end = int(parts[1])
        except ValueError:
            return False
        return start <= 3000 <= end

    if isinstance(value, int):
        return value == 3000
    if isinstance(value, dict):
        for key in ("published", "target", "host_port", "container_port"):
            nested = value.get(key)
            if nested is not None and _compose_port_text_mentions_3000(nested):
                return True
        return False
    if not isinstance(value, str):
        return False

    text = value.strip()
    if not text:
        return False
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    if not text:
        return False

    if text.startswith("[") and "]:" in text:
        text = text.split("]:", 1)[1]

    parts = [part.strip() for part in text.split(":") if part.strip()]
    if not parts:
        return False
    if len(parts) == 1:
        return _range_includes_3000(parts[0])

    for part in parts[-2:]:
        if _range_includes_3000(part):
            return True
    return False


def find_chinese_line_numbers(content: str) -> list[int]:
    return [
        line_no
        for line_no, raw in enumerate(content.splitlines(), start=1)
        if CHINESE_RE.search(raw)
    ]


# --- Comprehensive English/Chinese scan helpers ---------------------------

SKIP_LANGUAGE_CHECK_EXTS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ico",
    ".bmp",
    ".tiff",
    ".svg",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".doc",
    ".xls",
    ".ppt",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".o",
    ".obj",
    ".a",
    ".pyc",
    ".pyo",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".wav",
    ".ogg",
    ".webm",
    ".mov",
    ".avi",
}

ENGLISH_CHECK_EXCLUDED_DIRS = {
    ".tmp",
    ".backup",
    ".git",
    "original_sessions",  # session transcripts often legitimately contain user-facing prompts
}

ENGLISH_CHECK_EXCLUDED_FILES = {
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    "validation_report.md",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "go.sum",
}

RUNTIME_NOISE_FILE_NAMES = {
    ".coverage",
    "coverage.out",
    "cmakecache.txt",
    "cmake_install.cmake",
    "compile_commands.json",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
}


def read_text_multi_encoding(path: Path) -> str | None:
    """Read a file as text. Skip binary files (NUL bytes) and try utf-8/utf-8-sig/gb18030."""
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _is_runtime_noise_dir_name(dirname: str) -> bool:
    lower = dirname.lower()
    if re.fullmatch(r"build-.*", lower):
        return True
    return lower in RUNTIME_NOISE_DIR_NAMES


def _should_skip_english_dir(dirname: str) -> bool:
    lower = dirname.lower()
    if lower in ENGLISH_CHECK_EXCLUDED_DIRS:
        return True
    if _is_runtime_noise_dir_name(dirname):
        return True
    return False


def _should_skip_english_file(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in ENGLISH_CHECK_EXCLUDED_FILES:
        return True
    if lower_name in RUNTIME_NOISE_FILE_NAMES:
        return True
    if path.suffix.lower() in SKIP_LANGUAGE_CHECK_EXTS:
        return True
    if path.suffix.lower() in RUNTIME_NOISE_FILE_SUFFIXES:
        return True
    # Forbidden delivery files are flagged elsewhere; skip Chinese check on them.
    for _, predicate in FORBIDDEN_DELIVERY_FILE_RULES:
        if predicate(path):
            return True
    return False


def load_gitignore_scopes(root: Path) -> list[tuple[Path, list[str]]]:
    """Return [(scope_dir, patterns), ...] for every .gitignore under root."""
    scopes: list[tuple[Path, list[str]]] = []
    for gitignore_file in root.rglob(".gitignore"):
        if not gitignore_file.is_file():
            continue
        txt = read_text(gitignore_file)
        if txt is None:
            continue
        patterns: list[str] = []
        for raw in txt.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
        if patterns:
            scopes.append((gitignore_file.parent, patterns))
    return scopes


def is_ignored_by_gitignore_scopes(
    path: Path, scopes: list[tuple[Path, list[str]]]
) -> bool:
    if not scopes:
        return False
    import fnmatch

    for scope_dir, patterns in scopes:
        try:
            rel = path.relative_to(scope_dir).as_posix()
        except ValueError:
            continue
        if not rel or rel == ".":
            continue
        parts = rel.split("/")
        for pat in patterns:
            if pat.endswith("/"):
                p = pat.rstrip("/")
                if p in parts:
                    return True
                continue
            if pat.startswith("/"):
                anchored = pat.lstrip("/")
                if rel == anchored or rel.startswith(anchored + "/"):
                    return True
                continue
            if any(ch in pat for ch in "*?[\\]"):
                if fnmatch.fnmatch(rel, pat) or any(
                    fnmatch.fnmatch(p, pat) for p in parts
                ):
                    return True
                continue
            if pat in parts or rel.endswith(pat) or rel == pat:
                return True
    return False


def iter_readable_text_files(
    root: Path,
) -> Iterable[tuple[Path, str]]:
    """Walk `root` yielding (path, text) for every readable text file, skipping
    binary files, runtime noise, gitignored paths, and language-check exemptions.
    """
    gitignore_scopes = load_gitignore_scopes(root)
    for current_root, dirs, files in os.walk(root, topdown=True):
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        current_path = Path(current_root)

        kept_dirs: list[str] = []
        for dirname in dirs:
            if _should_skip_english_dir(dirname):
                continue
            dir_path = current_path / dirname
            if is_ignored_by_gitignore_scopes(dir_path, gitignore_scopes):
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in files:
            path = current_path / filename
            if _should_skip_english_file(path):
                continue
            if is_ignored_by_gitignore_scopes(path, gitignore_scopes):
                continue
            text = read_text_multi_encoding(path)
            if text is None:
                continue
            yield path, text


def format_line_numbers(line_numbers: list[int], limit: int = 20) -> str:
    if not line_numbers:
        return ""
    if len(line_numbers) <= limit:
        return ", ".join(str(n) for n in line_numbers)
    return (
        ", ".join(str(n) for n in line_numbers[:limit])
        + f" … total {len(line_numbers)} lines"
    )

SESSION_PROMPT_ANCHOR_LEN = 50
SESSION_PROMPT_SIMILARITY_THRESHOLD = 0.95
SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES = 40
SESSION_PROMPT_MIN_LENGTH_RATIO = 0.8
SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES = (
    "<local-command-caveat>",
    "unknown skill: effot",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "(bash completed with no output)",
    "updated task #",
    "no matches found",
    "no files found",
    "file does not exist. note:",
    "command running in background",
    "<tool_use_error>",
    "entered plan mode.",
    "user has approved your plan.",
)
LOCAL_COMMAND_NOISE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)
ORIGINAL_SESSIONS_DIR = "original_sessions"
SESSION_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ALLOWED_NON_UUID_FIRST_LAYER_FILES = {"memory.jsonl", "memory.md"}
SESSION_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz"}
LEADING_SESSION_ZIP_LINUX_PREFIX = "-"
LEADING_SESSION_ZIP_WINDOWS_PREFIX_RE = re.compile(r"^[A-Za-z]--")
LEADING_BUNDLE_EXEMPT_DIR_NAMES = {"tool-results", "memory"}
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS = (
    "clean_original_sessions.py",
    "validate_package.py",
    "check_chinese.py",
    "merge_claude_subagents_trajectory.py",
)
RUNTIME_NOISE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".npm",
    ".pnpm-store",
    ".yarn",
    ".next",
    "coverage",
    "dist",
    "build",
    "target",
    "out",
    "bin",
    "obj",
    "debug",
    "release",
    ".gradle",
    ".kotlin",
    ".dart_tool",
    "htmlcov",
    "cmakefiles",
    ".bundle",
    "vendor",
    ".cache",
}
RUNTIME_NOISE_FILE_SUFFIXES = {
    ".pyc",
    ".class",
    ".jar",
    ".war",
    ".o",
    ".obj",
    ".exe",
    ".pdb",
    ".test",
    ".tsbuildinfo",
}
COMPILE_EXEMPT_DIR_NAMES = {
    ".next",
    "dist",
    "build",
    "target",
    "out",
    "bin",
    "obj",
    "debug",
    "release",
    "cmakefiles",
}
COMPILE_EXEMPT_FILE_SUFFIXES = {
    ".class",
    ".jar",
    ".war",
    ".o",
    ".obj",
    ".exe",
    ".pdb",
    ".test",
    ".tsbuildinfo",
}
LANGUAGE_GITIGNORE_PATTERNS = {
    "python": [
        "__pycache__/",
        "*.pyc",
        ".pytest_cache/",
        ".venv/",
        "venv/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".coverage",
        "htmlcov/",
    ],
    "js_ts": [
        "node_modules/",
        "dist/",
        "build/",
        "coverage/",
        "*.tsbuildinfo",
        ".npm/",
        ".pnpm-store/",
        ".yarn/",
        ".next/",
    ],
    "java_kotlin": [
        "target/",
        "build/",
        ".gradle/",
        ".kotlin/",
        "out/",
        "*.class",
        "*.jar",
        "*.war",
    ],
    "go": [
        "bin/",
        "dist/",
        "coverage.out",
        "*.test",
    ],
    "php": ["vendor/"],
    "csharp": [
        "bin/",
        "obj/",
        "Debug/",
        "Release/",
        ".vs/",
        "TestResults/",
    ],
    "c_cpp": [
        "build/",
        "build-*/",
        "CMakeFiles/",
        "CMakeCache.txt",
        "cmake_install.cmake",
        "compile_commands.json",
        "*.o",
        "*.obj",
        "*.exe",
        "*.pdb",
    ],
    "rust": ["target/", "debug/", "*.pdb"],
    "dart_flutter": [
        ".dart_tool/",
        ".flutter-plugins",
        ".flutter-plugins-dependencies",
        "build/",
        ".gradle/",
        "android/local.properties",
    ],
    "ruby": [".bundle/", "vendor/bundle/", "vendor/cache/"],
}

DEFAULT_TASK_URL = "https://api.aquila-core.net/api/v1/mindflow-tasks/{task_id}"
DEFAULT_WORKSPACE_DIR = "project_bundle"
DEFAULT_REPO_DIR_NAME = "repo"
DEFAULT_SESSIONS_DIR_NAME = "original_sessions"
TASK_ID_ENV_VARS = ("TASK_ID", "AQUILA_TASK_ID")
ACCESS_TOKEN_ENV_VARS = ("ACCESS_TOKEN", "TASK_ACCESS_TOKEN")
GITHUB_PAT_ENV_VARS = ("GITHUB_PAT", "GITHUB_TOKEN")

FORBIDDEN_KEYWORDS = (
    "clean_original_sessions.py",
    "validate_package.py",
    "check_chinese.py",
    "merge_claude_subagents_trajectory.py",
)

ROLLOUT_JSONL_RE = re.compile(r"^rollout-.*\.jsonl$", re.IGNORECASE)

FORBIDDEN_DELIVERY_FILE_RULES: tuple[tuple[str, Any], ...] = (
    ("Coverage file (.coverage)", lambda p: p.name.lower() == ".coverage"),
    ("Coverage file (coverage.out)", lambda p: p.name.lower() == "coverage.out"),
    ("Test binary (*.test)", lambda p: p.suffix.lower() == ".test"),
    (
        "C/C++ build file",
        lambda p: p.name.lower()
        in {"cmakecache.txt", "cmake_install.cmake", "compile_commands.json"},
    ),
    (
        "Flutter local tool file",
        lambda p: p.name.lower()
        in {".flutter-plugins", ".flutter-plugins-dependencies"},
    ),
    (
        "Android local config (android/local.properties)",
        lambda p: p.as_posix().lower().endswith("android/local.properties"),
    ),
    (
        "Local database file",
        lambda p: p.suffix.lower() in {".db", ".sqlite", ".sqlite3"},
    ),
    (
        "TypeScript build cache (.tsbuildinfo)",
        lambda p: p.suffix.lower() == ".tsbuildinfo",
    ),
    (
        "Disallowed delivery file (session.json)",
        lambda p: p.name.lower() == "session.json",
    ),
    (
        "Disallowed delivery file (rollout-*.jsonl)",
        lambda p: ROLLOUT_JSONL_RE.fullmatch(p.name) is not None,
    ),
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValidatorError(Exception):
    """User-facing validator error. Message is shown without a traceback."""


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def first_non_empty(values: Iterable[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_env_value(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def is_allowed_non_uuid_first_layer_file(filename: str) -> bool:
    """Check if a filename is allowed as a non-UUID first-layer file in sessions.
    Checks against ALLOWED_NON_UUID_FIRST_LAYER_FILES with case-insensitive matching.
    """
    return filename.lower() in {f.lower() for f in ALLOWED_NON_UUID_FIRST_LAYER_FILES}


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "task"


def normalize_aquila_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, list):
        items = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return ", ".join(items) if items else "none"
    if isinstance(value, str):
        return value.strip() or "none"
    return str(value)


def parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Aquila client
# ---------------------------------------------------------------------------


def build_task_url(template: str, task_id: str) -> str:
    encoded = urllib.parse.quote(task_id, safe="")
    if "{task_id}" in template:
        return template.format(task_id=encoded)
    if "{t_id}" in template:
        return template.format(t_id=encoded)
    return template.rstrip("/") + f"/{encoded}"


def http_get_json(url: str, bearer_token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(request) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def extract_task_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("item", "data", "task", "result"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                return candidate
        return payload
    raise ValueError("Task payload is not a JSON object")


def fetch_aquila_task(
    task_id: str | None,
    access_token: str | None,
    task_url_template: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch and unwrap a task payload. Returns (task, error_message)."""
    if not task_id or not access_token:
        return None, "TASK_ID or AQUILA access token is not available"
    url = build_task_url(task_url_template, task_id)
    try:
        payload = http_get_json(url, access_token)
    except Exception as exc:  # noqa: BLE001 - surface any transport/parse error
        return None, f"Failed to fetch Aquila task: {exc}"
    try:
        return extract_task_payload(payload), None
    except ValueError as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# CI preparation (stages artifacts; does not edit them)
# ---------------------------------------------------------------------------


def detect_repo_url(task: dict[str, Any]) -> str:
    repo_url = first_non_empty([task.get("github_link")])
    if not repo_url:
        raise ValueError("github_link is missing from the task payload")
    return repo_url


def detect_sessions_zip_url(task: dict[str, Any]) -> str:
    session_links = task.get("session_files_links")
    if isinstance(session_links, list) and session_links:
        first_link = first_non_empty([session_links[0]])
        if first_link:
            return first_link
    if isinstance(session_links, str) and session_links.strip():
        return session_links.strip()
    raise ValueError("session_files_links is missing from the task payload")


def add_pat_to_github_url(repo_url: str, github_pat: str) -> str:
    quoted = urllib.parse.quote(github_pat, safe="")
    if repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://x-access-token:{quoted}@", 1)
    if repo_url.startswith("http://"):
        return repo_url.replace("http://", f"http://x-access-token:{quoted}@", 1)
    raise ValueError(f"Unsupported repo URL scheme: {repo_url}")


def clone_repo(repo_url: str, github_pat: str | None, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_url = add_pat_to_github_url(repo_url, github_pat) if github_pat else repo_url
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, str(target_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        # Avoid leaking the PAT-injected URL or stderr that may include it.
        raise ValidatorError(
            f"git clone failed for {repo_url} (exit code {exc.returncode})"
        ) from exc
    except FileNotFoundError as exc:
        raise ValidatorError("git executable not available on PATH") from exc


ZIP_MAGIC_BYTES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _download_file(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request) as response, out_path.open("wb") as handle:
            status = getattr(response, "status", 200)
            if status and int(status) >= 400:
                raise ValidatorError(f"Sessions URL returned HTTP {status}")
            shutil.copyfileobj(response, handle)
    except urllib.error.HTTPError as exc:
        raise ValidatorError(f"Sessions URL returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValidatorError(f"Could not reach sessions URL: {exc.reason}") from exc


def _assert_zip_archive(archive_path: Path) -> None:
    try:
        with archive_path.open("rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        raise ValidatorError(
            f"Sessions archive could not be read: {exc.strerror or 'unknown error'}"
        ) from exc
    if not any(head.startswith(magic) for magic in ZIP_MAGIC_BYTES):
        raise ValidatorError(
            "Sessions archive is not a valid zip file "
            "(the URL likely returned an error page or has expired)"
        )


def _collapse_wrapper_directory(root: Path) -> Path:
    entries = [entry for entry in root.iterdir() if entry.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def _locate_sessions_source(extracted_root: Path) -> Path:
    for name in (DEFAULT_SESSIONS_DIR_NAME, "original-sessions"):
        for candidate in extracted_root.rglob(name):
            if candidate.is_dir():
                return candidate
    return _collapse_wrapper_directory(extracted_root)


def _copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for entry in source_dir.iterdir():
        destination = target_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)


def download_and_extract_sessions(
    sessions_zip_url: str, target_dir: Path, staging_dir: Path
) -> Path:
    # Download the sessions archive and place the archive file directly into
    # the target `original_sessions/` directory without extracting or
    # repackaging it. Preserve the archive filename when possible so leading
    # bundle name heuristics still work (e.g. linux/windows leading stems).
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Try to preserve the filename from the URL path; fall back to a
    # deterministic name if unavailable.
    parsed = urllib.parse.urlparse(sessions_zip_url)
    candidate_name = os.path.basename(parsed.path or "")
    archive_name = urllib.parse.unquote(candidate_name) or "sessions.zip"
    archive_path = staging_dir / archive_name

    _download_file(sessions_zip_url, archive_path)
    _assert_zip_archive(archive_path)

    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / archive_name
    try:
        # Overwrite any existing file with the same name.
        shutil.copy2(archive_path, dest)
    except OSError as exc:
        raise ValidatorError(
            f"Failed to place sessions archive into target dir: {exc}"
        ) from exc

    return target_dir


@dataclass
class CiContext:
    task_id: str
    task: dict[str, Any]
    repo_dir: Path
    task_work_dir: Path


def clone_repo_for_ci(args: argparse.Namespace) -> CiContext:
    """Fetch the Aquila task and clone the repo. Does not extract sessions."""
    task_id = args.task_id or get_env_value(TASK_ID_ENV_VARS)
    access_token = args.access_token or get_env_value(ACCESS_TOKEN_ENV_VARS)
    github_pat = args.github_pat or get_env_value(GITHUB_PAT_ENV_VARS)

    if not task_id:
        raise ValidatorError("TASK_ID is required")
    if not access_token:
        raise ValidatorError("ACCESS_TOKEN is required")

    task, error = fetch_aquila_task(task_id, access_token, args.task_url_template)
    if task is None:
        raise ValidatorError(f"[{task_id}] {error}")

    try:
        repo_url = detect_repo_url(task)
    except ValueError as exc:
        raise ValidatorError(str(exc)) from exc
    extracted_task_id = sanitize_name(
        first_non_empty([task.get("mindflow_id"), task.get("task_id"), task.get("id")])
        or "unknown_task"
    )

    workspace_dir = args.workspace_dir.resolve()
    repo_dir = workspace_dir / args.repo_dir_name
    task_work_dir = workspace_dir / ".task_validation" / extracted_task_id

    if task_work_dir.exists():
        shutil.rmtree(task_work_dir)
    task_work_dir.mkdir(parents=True, exist_ok=True)

    if repo_dir.exists():
        shutil.rmtree(repo_dir)

    print(f"[{task_id}] cloning repository: {repo_url}")
    clone_repo(repo_url, github_pat, repo_dir)

    return CiContext(
        task_id=task_id, task=task, repo_dir=repo_dir, task_work_dir=task_work_dir
    )


def stage_sessions_into_repo(ctx: CiContext) -> Path:
    """Download the sessions zip and place it at <repo>/original_sessions/."""
    try:
        sessions_zip_url = detect_sessions_zip_url(ctx.task)
    except ValueError as exc:
        raise ValidatorError(str(exc)) from exc
    sessions_dir = ctx.repo_dir / DEFAULT_SESSIONS_DIR_NAME
    download_and_extract_sessions(sessions_zip_url, sessions_dir, ctx.task_work_dir)
    print(f"[{ctx.task_id}] sessions downloaded to {sessions_dir}")
    return sessions_dir


def write_build_dir_marker(args: argparse.Namespace, repo_dir: Path) -> None:
    args.build_dir_file.parent.mkdir(parents=True, exist_ok=True)
    args.build_dir_file.write_text(str(repo_dir.resolve()) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass
class CheckItem:
    status: str
    message: str
    rel_path: str


@dataclass
class CheckSection:
    title: str
    items: list[CheckItem] = field(default_factory=list)

    def add_pass(self, message: str, rel_path: str = ".") -> None:
        self.items.append(CheckItem("PASS", message, rel_path))

    def add_fail(self, message: str, rel_path: str = ".") -> None:
        self.items.append(CheckItem("FAIL", message, rel_path))

    def add_warn(self, message: str, rel_path: str = ".") -> None:
        self.items.append(CheckItem("WARN", message, rel_path))


@dataclass
class JsonlAnalysis:
    readable: bool
    total_lines: int = 0
    first_timestamp_raw: str | None = None
    first_timestamp_line: int | None = None
    latest_timestamp_dt: datetime | None = None
    latest_timestamp_raw: str | None = None
    latest_timestamp_line: int | None = None
    last_semantic_line: int | None = None
    last_semantic_role: str | None = None
    last_semantic_has_assistant_text: bool = False
    user_line_count: int = 0
    exempt_user_line_count: int = 0
    user_candidates: list[tuple[str, int, str, datetime]] = field(default_factory=list)
    keyword_line_numbers: dict[str, list[int]] = field(default_factory=dict)
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0


@dataclass
class JsonlUsageAnalysis:
    readable: bool
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0
    keyword_line_numbers: dict[str, list[int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Text comparison helpers (prompt anchor)
# ---------------------------------------------------------------------------


def _normalize_for_compare(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.replace(r"\n", "").replace(r"\r", "").replace(r"\t", "")
    text = text.replace(r"\"", "").replace(r"\'", "")
    return re.sub(r"[^A-Za-z0-9一-鿿]+", "", text)


def _strip_trailing_punct(text: str | None) -> str:
    if text is None:
        return ""
    out = text.rstrip()
    trailing = string.punctuation + "，。！？；：、“”‘’（）【】《》—…"
    while out and out[-1] in trailing:
        out = out[:-1].rstrip()
    return out


def _get_head_anchor(
    reference_text: str, anchor_len: int = SESSION_PROMPT_ANCHOR_LEN
) -> str:
    return reference_text.lstrip()[:anchor_len]


def _get_tail_anchor(
    reference_text: str, anchor_len: int = SESSION_PROMPT_ANCHOR_LEN
) -> str:
    ref = _strip_trailing_punct(reference_text)
    if not ref:
        return ""
    return ref[-anchor_len:]


def _build_loose_pattern(head_anchor: str, tail_anchor: str) -> str:
    head = re.escape(head_anchor)
    tail = re.escape(tail_anchor)
    if not head and not tail:
        return r"(.*)"
    if head and not tail:
        return rf"{head}(.*)"
    if tail and not head:
        return rf"(.*?){tail}"
    return rf"{head}(.*?){tail}"


def _similarity_score(text1: str, text2: str) -> float:
    s1 = _normalize_for_compare(text1)
    s2 = _normalize_for_compare(text2)
    return difflib.SequenceMatcher(None, s1, s2).ratio()


def _get_diff_preview(text1: str, text2: str, context: int = 60) -> dict[str, Any]:
    s1 = _normalize_for_compare(text1)
    s2 = _normalize_for_compare(text2)

    min_len = min(len(s1), len(s2))
    diff_index = -1
    for idx in range(min_len):
        if s1[idx] != s2[idx]:
            diff_index = idx
            break
    if diff_index == -1 and len(s1) != len(s2):
        diff_index = min_len

    if diff_index == -1:
        return {"diff_index": -1, "reference_preview": "", "matched_preview": ""}

    start = max(0, diff_index - context)
    end1 = min(len(s1), diff_index + context)
    end2 = min(len(s2), diff_index + context)
    return {
        "diff_index": diff_index,
        "reference_preview": s1[start:end1],
        "matched_preview": s2[start:end2],
    }


def compare_by_anchor(reference_text: str, target_text: str) -> dict[str, Any]:
    head = _get_head_anchor(reference_text)
    tail = _get_tail_anchor(reference_text)
    if not head and not tail:
        return {
            "matched": False,
            "equal": False,
            "near_duplicate": False,
            "similarity": 0.0,
            "reason": "reference_text empty or unable to extract anchor",
            "fallback_full_text": False,
        }

    pattern = _build_loose_pattern(head, tail)
    match = re.search(pattern, target_text, flags=re.DOTALL)
    if not match:
        similarity = _similarity_score(reference_text, target_text)
        diff_info = _get_diff_preview(reference_text, target_text)
        reference_normalized = _normalize_for_compare(reference_text)
        target_normalized = _normalize_for_compare(target_text)
        return {
            "matched": False,
            "equal": reference_normalized == target_normalized,
            "near_duplicate": similarity >= SESSION_PROMPT_SIMILARITY_THRESHOLD,
            "similarity": similarity,
            "head_anchor": head,
            "tail_anchor": tail,
            "matched_segment": target_text,
            "reference_normalized": reference_normalized,
            "matched_normalized": target_normalized,
            "diff_index": diff_info["diff_index"],
            "reference_diff_preview": diff_info["reference_preview"],
            "matched_diff_preview": diff_info["matched_preview"],
            "fallback_full_text": True,
            "reason": "Candidate text segment bounded by head and tail anchors not found in target text, degraded to full-segment similarity check",
        }

    candidate = match.group(0)
    similarity = _similarity_score(reference_text, candidate)
    diff_info = _get_diff_preview(reference_text, candidate)
    reference_normalized = _normalize_for_compare(reference_text)
    matched_normalized = _normalize_for_compare(candidate)
    return {
        "matched": True,
        "equal": reference_normalized == matched_normalized,
        "near_duplicate": similarity >= SESSION_PROMPT_SIMILARITY_THRESHOLD,
        "similarity": similarity,
        "head_anchor": head,
        "tail_anchor": tail,
        "matched_segment": candidate,
        "reference_normalized": reference_normalized,
        "matched_normalized": matched_normalized,
        "diff_index": diff_info["diff_index"],
        "reference_diff_preview": diff_info["reference_preview"],
        "matched_diff_preview": diff_info["matched_preview"],
        "fallback_full_text": False,
    }


# ---------------------------------------------------------------------------
# JSONL analysis
# ---------------------------------------------------------------------------


def _extract_text_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type", "")).lower()
                if item_type == "text":
                    out.append(str(item.get("text", "")))
                elif item_type == "tool_result":
                    out.append(str(item.get("content", "")))
        return "\n".join(out)
    if isinstance(content, dict):
        item_type = str(content.get("type", "")).lower()
        if item_type == "text":
            return str(content.get("text", ""))
        if item_type == "tool_result":
            return str(content.get("content", ""))
    return ""


def _stringify_session_message_content(content: Any) -> str:
    chunks: list[str] = []

    def _collect(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            chunks.append(node)
            return
        if isinstance(node, list):
            for item in node:
                _collect(item)
            return
        if isinstance(node, dict):
            text_value = node.get("text")
            if isinstance(text_value, str):
                chunks.append(text_value)
            elif text_value is not None:
                chunks.append(str(text_value))
            if "content" in node:
                _collect(node.get("content"))
            return
        chunks.append(str(node))

    _collect(content)
    merged = "\n".join(part for part in chunks if part is not None and str(part).strip())
    if merged.strip():
        return merged
    if isinstance(content, (dict, list)):
        try:
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(content)
    return "" if content is None else str(content)


def _strip_read_tool_line_number_prefixes(text: str) -> str:
    return re.sub(r"(?m)^\s*\d+\s*→\s*", "", text)


def _extract_candidate_content_from_user_payload(
    payload: dict[str, Any], message: dict[str, Any]
) -> str:
    fallback = _stringify_session_message_content(message.get("content"))
    fallback = _strip_read_tool_line_number_prefixes(fallback)

    tool_use_result = payload.get("toolUseResult")
    if isinstance(tool_use_result, dict):
        file_obj = tool_use_result.get("file")
        if isinstance(file_obj, dict):
            file_content = file_obj.get("content")
            if isinstance(file_content, str) and file_content.strip():
                return file_content

        tur_content = tool_use_result.get("content")
        if isinstance(tur_content, str) and tur_content.strip():
            return _strip_read_tool_line_number_prefixes(tur_content)

    return fallback


def _is_local_command_noise(payload: dict[str, Any]) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    if str(message.get("role", "")).lower() != "user":
        return False
    return (
        _extract_text_content(message).strip().startswith(LOCAL_COMMAND_NOISE_PREFIXES)
    )


def _new_usage_totals() -> dict[str, int]:
    return {key: 0 for key in TOKEN_USAGE_KEYS}


def _add_usage_totals(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in TOKEN_USAGE_KEYS:
        dst[key] = dst.get(key, 0) + src.get(key, 0)


def _to_int_token_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return 0
    return 0


def _map_usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": _to_int_token_value(usage.get("input_tokens", 0)),
        "output_tokens": _to_int_token_value(usage.get("output_tokens", 0)),
        "cache_read_tokens": _to_int_token_value(
            usage.get("cache_read_input_tokens", 0)
        ),
        "cache_write_tokens": _to_int_token_value(
            usage.get("cache_creation_input_tokens", 0)
        ),
    }


def _extract_usage_tokens_from_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, int] | None, str | None]:
    usage: Any | None = None
    message_id: str | None = None

    message = payload.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        candidate_id = message.get("id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            message_id = candidate_id.strip()
    elif "usage" in payload:
        usage = payload.get("usage")
        candidate_id = payload.get("id")
        if isinstance(candidate_id, str) and candidate_id.strip():
            message_id = candidate_id.strip()

    if not isinstance(usage, dict) or not usage:
        return None, message_id
    return _map_usage_tokens(usage), message_id


def _analyze_jsonl_usage_only(path: Path) -> JsonlUsageAnalysis:
    analysis = JsonlUsageAnalysis(
        readable=False,
        usage_totals=_new_usage_totals(),
        keyword_line_numbers={keyword: [] for keyword in FORBIDDEN_KEYWORDS},
    )
    content = read_text(path)
    if content is None:
        return analysis

    analysis.readable = True
    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        lower_line = raw_line.lower()
        for keyword in FORBIDDEN_KEYWORDS:
            if keyword in lower_line:
                analysis.keyword_line_numbers[keyword].append(line_no)

        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        usage_tokens, message_id = _extract_usage_tokens_from_payload(payload)
        if usage_tokens is None:
            continue
        if message_id:
            # The validator only needs aggregate counts, so keep the same
            # totals while preserving deterministic single-pass accounting.
            _add_usage_totals(analysis.usage_totals, usage_tokens)
            analysis.usage_record_count += 1
        else:
            _add_usage_totals(analysis.usage_totals, usage_tokens)
            analysis.usage_record_count += 1

    return analysis


def analyze_jsonl(path: Path) -> JsonlAnalysis:
    text = read_text(path)
    analysis = JsonlAnalysis(
        readable=text is not None,
        keyword_line_numbers={
            keyword: [] for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS
        },
    )
    if text is None:
        return analysis

    for line_no, raw in enumerate(text.splitlines(), start=1):
        lower = raw.lower()
        for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS:
            if keyword in lower:
                analysis.keyword_line_numbers[keyword].append(line_no)

        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        timestamp_raw = (
            payload.get("timestamp")
            if isinstance(payload.get("timestamp"), str)
            else None
        )
        parsed_ts = parse_iso_timestamp(timestamp_raw)
        if timestamp_raw and analysis.first_timestamp_raw is None:
            analysis.first_timestamp_raw = timestamp_raw
            analysis.first_timestamp_line = line_no
        if parsed_ts is not None and (
            analysis.latest_timestamp_dt is None
            or parsed_ts > analysis.latest_timestamp_dt
        ):
            analysis.latest_timestamp_dt = parsed_ts
            analysis.latest_timestamp_raw = timestamp_raw
            analysis.latest_timestamp_line = line_no

        message = payload.get("message")
        if not isinstance(message, dict):
            continue

        role = str(message.get("role", "")).strip().lower()
        msg_type = str(message.get("type", "")).strip().lower()
        is_noise = _is_local_command_noise(payload)
        if (not msg_type or msg_type == "message") and not is_noise:
            analysis.last_semantic_line = line_no
            analysis.last_semantic_role = role
            analysis.last_semantic_has_assistant_text = role == "assistant" and bool(
                _extract_text_content(message).strip()
            )

        if role == "user":
            analysis.user_line_count += 1
            content_text = _extract_candidate_content_from_user_payload(payload, message)
            if not content_text.strip():
                continue
            if is_noise or content_text.lstrip().lower().startswith(
                SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES
            ):
                analysis.exempt_user_line_count += 1
                continue
            if timestamp_raw and parsed_ts is not None:
                analysis.user_candidates.append(
                    (content_text, line_no, timestamp_raw, parsed_ts)
                )

    return analysis


def _find_first_cwd_in_jsonl(path: Path) -> str | None:
    """Return the first string value found under a key named 'cwd' (case-insensitive) in the jsonl file, or None."""
    content = read_text(path)
    if content is None:
        return None

    def _search_node(node: Any) -> str | None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() == "cwd" and isinstance(v, str):
                    value = v.strip()
                    if value:
                        return value
                found = _search_node(v)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _search_node(item)
                if found:
                    return found
        return None

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        found = _search_node(payload)
        if found:
            return found
    return None


def _build_claude_session_stem_from_cwd(cwd: str) -> str | None:
    """Build Claude-style session bundle stem from a cwd path.

    Linux absolute path: /a/b -> -a-b
    Windows absolute path: D:\\a\\b -> D--a-b
    """
    raw = cwd.strip()
    if not raw:
        return None

    windows_match = re.match(r"^([A-Za-z]):[\\/]*(.*)$", raw)
    if windows_match:
        drive = windows_match.group(1).upper()
        rest = windows_match.group(2).replace("\\", "/")
        parts = [part for part in rest.split("/") if part and part != "."]
        return f"{drive}--{'-'.join(parts)}" if parts else f"{drive}--"

    if raw.startswith("/"):
        parts = [part for part in raw.split("/") if part and part != "."]
        return f"-{'-'.join(parts)}" if parts else "-"

    return None


def _find_first_cwd_in_zip_archive(archive: zipfile.ZipFile) -> str | None:
    members = sorted(
        [
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".jsonl")
        ],
        key=lambda i: i.filename.lower(),
    )
    for info in members:
        try:
            data = archive.read(info)
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            cwd = _find_first_cwd_in_payload(payload)
            if cwd:
                return cwd
    return None


def _find_first_cwd_in_payload(payload: dict[str, Any]) -> str | None:
    def _search(node: Any) -> str | None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and key.lower() == "cwd"
                    and isinstance(value, str)
                ):
                    candidate = value.strip()
                    if candidate:
                        return candidate
                found = _search(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _search(item)
                if found:
                    return found
        return None

    return _search(payload)


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


class SectionValidator:
    """Base for section validators. Holds the package root and a shared section."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _rel(self, path: Path | str) -> str:
        if isinstance(path, str):
            return path
        try:
            rel = path.relative_to(self.root).as_posix()
            return rel if rel else "."
        except ValueError:
            return path.as_posix()


class StructureValidator(SectionValidator):
    BASE_MANDATORY_DIRS = ("docs", "repo", ".tmp")
    MANDATORY_FILES = (
        "docs/design.md",
        "docs/questions.md",
        "metadata.json",
        "repo/docker-compose.yml",
        "repo/README.md",
    )
    BASE_ALLOWED_ROOT_ENTRIES = {
        "docs",
        "repo",
        ".tmp",
        "metadata.json",
        ".git",
        ".gitignore",
        ".github",
    }
    TEST_SCRIPT_OPTIONS = ("repo/run_tests.sh",)

    def __init__(self, root: Path, expect_original_sessions: bool = True) -> None:
        super().__init__(root)
        self.expect_original_sessions = expect_original_sessions
        if expect_original_sessions:
            self.mandatory_dirs = (*self.BASE_MANDATORY_DIRS, ORIGINAL_SESSIONS_DIR)
            self.allowed_root_entries = self.BASE_ALLOWED_ROOT_ENTRIES | {
                ORIGINAL_SESSIONS_DIR
            }
        else:
            self.mandatory_dirs = self.BASE_MANDATORY_DIRS
            self.allowed_root_entries = self.BASE_ALLOWED_ROOT_ENTRIES

    TMP_AUDIT_RE = re.compile(r"^audit_report-(\d+)\.md$")
    TMP_FIX_RE = re.compile(r"^audit_report-(\d+)-fix_check\.md$")
    TMP_REQUIRED_EXTRA = "test_coverage_and_readme_audit_report.md"
    TMP_OPTIONAL_PROMPT_REQ = "prompt_requirements_verification.md"

    TEMP_EXACT_NAMES = {
        "tmp",
        "temp",
        "temporary",
        ".temp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
    }
    TEMP_SUFFIXES = (".temp", ".swp", ".swo", ".bak", ".old", ".orig", ".rej")

    REPO_FORBIDDEN_SUBDIR_NAMES = {"docs", "doc", "documentation"}

    UNNECESSARY_EXACT_NAMES = {
        # Virtualenvs
        "venv",
        ".venv",
        "env",
        # JS deps
        "node_modules",
        # IDE / editor state
        ".idea",
        ".vscode",
        # AI Agent caches
        ".codex",
        ".claude",
        "agent.md",
        "Agent.md",
        "claude.md",
        # Build / framework caches
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".turbo",
        ".parcel-cache",
        # Coverage / test artifacts
        "coverage",
        ".nyc_output",
        "htmlcov",
        # OS noise
        ".DS_Store",
        "__MACOSX",
        "Thumbs.db",
    }

    # Compile/build artifact heuristics
    COMPILE_ARTIFACT_SUFFIXES = (
        ".class",
        ".pyc",
        ".pyo",
        ".o",
        ".so",
        ".dll",
        ".exe",
        ".obj",
        ".ilk",
    )
    COMPILE_ARTIFACT_DIRS = ("build", "dist", "target", "out", "bin")

    def validate(self, section: CheckSection) -> None:
        for dirname in self.mandatory_dirs:
            path = self.root / dirname
            if path.is_dir():
                section.add_pass(f"Directory exists: {dirname}/", self._rel(path))
            else:
                section.add_fail(f"Missing directory: {dirname}/", self._rel(path))

        for file_name in self.MANDATORY_FILES:
            path = self.root / file_name
            if path.is_file():
                section.add_pass(f"File exists: {file_name}", self._rel(path))
            else:
                section.add_fail(f"Missing file: {file_name}", self._rel(path))

        unexpected_root = 0
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if (
                entry.name not in self.allowed_root_entries
                and entry.name != "validation_report.md"
            ):
                kind = "directory" if entry.is_dir() else "file"
                section.add_fail(
                    f"Unexpected {kind} in root: {entry.name}", self._rel(entry)
                )
                unexpected_root += 1
        if unexpected_root == 0:
            section.add_pass("No unexpected entries at repo root", ".")

        docs_dir = self.root / "docs"
        if docs_dir.is_dir():
            invalid_docs = 0
            for entry in sorted(docs_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() or entry.suffix.lower() != ".md":
                    kind = "directory" if entry.is_dir() else "file"
                    section.add_fail(
                        f"Invalid {kind} in docs/: {entry.name}", self._rel(entry)
                    )
                    invalid_docs += 1
            if invalid_docs == 0:
                section.add_pass("docs/ contains only .md files", self._rel(docs_dir))

        self._check_tmp_layout(section)

        found_test = next(
            (opt for opt in self.TEST_SCRIPT_OPTIONS if (self.root / opt).is_file()),
            None,
        )
        if found_test:
            section.add_pass(f"Test script found: {found_test}", found_test)
        else:
            section.add_fail("Missing test script repo/run_tests.sh", "repo/")

        repo_subdir_for_docker = self.root / "repo"
        if repo_subdir_for_docker.is_dir():
            dockerfiles: list[Path] = []
            for entry in repo_subdir_for_docker.rglob("*"):
                if not entry.is_file():
                    continue
                if _is_dockerfile_name(entry.name):
                    dockerfiles.append(entry)
            if dockerfiles:
                display = ", ".join(self._rel(p) for p in dockerfiles[:3])
                if len(dockerfiles) > 3:
                    display += f" (+{len(dockerfiles) - 3} more)"
                section.add_pass(
                    f"Dockerfile found under repo/: {display}",
                    self._rel(dockerfiles[0]),
                )
            else:
                section.add_fail(
                    "Missing Dockerfile under repo/ (Dockerfile, Dockerfile.dev, *.Dockerfile, etc.)",
                    "repo/",
                )

            self._check_compose_dockerfile_references(
                section, repo_subdir_for_docker
            )

        temp_findings = 0
        unnecessary_findings = 0
        env_findings = 0
        forbidden_delivery_findings = 0
        gitignore_patterns = self._load_gitignore_patterns()
        for entry in self.root.rglob("*"):
            name = entry.name
            # Forbidden delivery files: always reported regardless of gitignore.
            # Skip session-bundle archives (e.g. inside original_sessions/) to
            # avoid noise, since session.json/rollout-*.jsonl typically live
            # outside the delivered tree.
            if entry.is_file():
                violation_reason = next(
                    (
                        reason
                        for reason, predicate in FORBIDDEN_DELIVERY_FILE_RULES
                        if predicate(entry)
                    ),
                    None,
                )
                if violation_reason is not None:
                    section.add_fail(
                        f"{violation_reason}: {entry.relative_to(self.root)}",
                        self._rel(entry),
                    )
                    forbidden_delivery_findings += 1
                    continue
            if self._is_temp_entry(entry):
                kind = "directory" if entry.is_dir() else "file"
                section.add_fail(
                    f"Temporary {kind} found: {entry.relative_to(self.root)}",
                    self._rel(entry),
                )
                temp_findings += 1
                continue
            if name in self.UNNECESSARY_EXACT_NAMES:
                # Respect .gitignore: if path is ignored, treat as expected and skip
                if self._is_ignored_by_gitignore(entry, gitignore_patterns):
                    continue
                kind = "directory" if entry.is_dir() else "file"
                section.add_fail(
                    f"Unnecessary {kind} found: {entry.relative_to(self.root)}",
                    self._rel(entry),
                )
                unnecessary_findings += 1
                continue
            if (
                entry.is_file()
                and (name == ".env" or name.startswith(".env."))
                and name != ".env.example"
            ):
                section.add_fail(
                    f"Dotenv file found: {entry.relative_to(self.root)}",
                    self._rel(entry),
                )
                env_findings += 1
            # Detect compile/build artifacts and flag them unless gitignored
            if entry.is_file():
                lower = name.lower()
                if any(
                    lower.endswith(suf) for suf in self.COMPILE_ARTIFACT_SUFFIXES
                ) or (any(part in self.COMPILE_ARTIFACT_DIRS for part in entry.parts)):
                    if not self._is_ignored_by_gitignore(entry, gitignore_patterns):
                        section.add_fail(
                            f"Compile/build artifact or noisy file found: {entry.relative_to(self.root)}",
                            self._rel(entry),
                        )
                        unnecessary_findings += 1
        if temp_findings == 0:
            section.add_pass("No stray temporary files or caches found", ".")
        if unnecessary_findings == 0:
            section.add_pass(
                "No virtualenv/build/IDE/OS-noise paths found", "."
            )
        if env_findings == 0:
            section.add_pass("No dotenv files found (other than .env.example)", ".")
        if forbidden_delivery_findings == 0:
            section.add_pass(
                "No forbidden delivery files found (session.json, rollout-*.jsonl, *.db, .coverage, etc.)",
                ".",
            )

        repo_subdir = self.root / "repo"
        if repo_subdir.is_dir():
            forbidden_repo_findings = 0
            for entry in repo_subdir.rglob("*"):
                if not entry.is_dir():
                    continue
                if entry.name.lower() in self.REPO_FORBIDDEN_SUBDIR_NAMES:
                    section.add_fail(
                        f"Forbidden directory under repo/: {entry.relative_to(self.root)}",
                        self._rel(entry),
                    )
                    forbidden_repo_findings += 1
            if forbidden_repo_findings == 0:
                section.add_pass(
                    "No docs/doc/documentation directories under repo/", "repo/"
                )

    def _is_temp_entry(self, path: Path) -> bool:
        name = path.name.lower()
        if name in self.TEMP_EXACT_NAMES:
            return True
        if name.endswith("~") or name.startswith("~$"):
            return True
        if any(name.endswith(suffix) for suffix in self.TEMP_SUFFIXES):
            return True
        if name.startswith("tmp-") or name.startswith("temp-"):
            return True
        if name.endswith("-tmp") or name.endswith("-temp"):
            return True
        return False

    def _load_gitignore_patterns(self) -> list[tuple[Path, list[str]]]:
        return load_gitignore_scopes(self.root)

    def _is_ignored_by_gitignore(
        self, path: Path, scopes: list[tuple[Path, list[str]]]
    ) -> bool:
        return is_ignored_by_gitignore_scopes(path, scopes)

    def _check_compose_dockerfile_references(
        self, section: CheckSection, repo_dir: Path
    ) -> None:
        """Parse docker-compose files and verify referenced Dockerfiles exist.

        Looks for compose files at the repo root, parses ``services.*.build``
        (string or mapping form), resolves each ``context``/``dockerfile`` pair
        relative to the compose file, and asserts the resulting path exists and
        looks like a Dockerfile (Dockerfile, Dockerfile.*, *.Dockerfile).

        Also verifies that at least one service publishes or targets port 3000,
        which is the expected app entry point for the delivery bundles.
        """
        compose_names = (
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        )
        compose_paths = [
            repo_dir / name for name in compose_names if (repo_dir / name).is_file()
        ]
        if not compose_paths:
            return

        yaml = _load_yaml_module()

        failures = 0
        checked_refs = 0
        port_3000_found = False
        for compose_path in compose_paths:
            text = read_text(compose_path)
            if text is None:
                section.add_warn(
                    "docker-compose file is unreadable; skipping reference check",
                    self._rel(compose_path),
                )
                continue
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                section.add_fail(
                    f"docker-compose file is invalid YAML: {exc}",
                    self._rel(compose_path),
                )
                failures += 1
                continue
            if not isinstance(doc, dict):
                continue
            services = doc.get("services")
            if not isinstance(services, dict):
                continue

            compose_dir = compose_path.parent
            for svc_name, svc in services.items():
                if not isinstance(svc, dict):
                    continue

                ports = svc.get("ports")
                if not port_3000_found:
                    if isinstance(ports, list):
                        port_3000_found = any(
                            _compose_port_text_mentions_3000(port) for port in ports
                        )
                    else:
                        port_3000_found = _compose_port_text_mentions_3000(ports)

                build = svc.get("build")
                if build is None:
                    continue
                if isinstance(build, str):
                    context_value = build
                    dockerfile_value = "Dockerfile"
                elif isinstance(build, dict):
                    context_value = str(build.get("context", "."))
                    dockerfile_value = str(build.get("dockerfile", "Dockerfile"))
                else:
                    continue
                if not context_value or _looks_like_windows_abs_path(context_value):
                    section.add_fail(
                        f"service '{svc_name}': invalid build.context value: {context_value!r}",
                        self._rel(compose_path),
                    )
                    failures += 1
                    continue
                context_dir = (compose_dir / context_value).resolve()
                dockerfile_path = (context_dir / dockerfile_value).resolve()
                checked_refs += 1
                if not dockerfile_path.is_file():
                    section.add_fail(
                        (
                            f"service '{svc_name}': referenced Dockerfile not found "
                            f"(context={context_value}, dockerfile={dockerfile_value})"
                        ),
                        self._rel(compose_path),
                    )
                    failures += 1
                    continue
                if not _is_dockerfile_name(dockerfile_path.name):
                    section.add_warn(
                        (
                            f"service '{svc_name}': referenced file does not look like "
                            f"a Dockerfile: {dockerfile_path.name}"
                        ),
                        self._rel(compose_path),
                    )

        if checked_refs and failures == 0:
            section.add_pass(
                f"docker-compose Dockerfile references resolve ({checked_refs} reference(s))",
                self._rel(compose_paths[0]),
            )

        if port_3000_found:
            section.add_pass(
                "docker-compose exposes app entry point on port 3000",
                self._rel(compose_paths[0]),
            )
        else:
            section.add_fail(
                "docker-compose must expose app entry point on port 3000",
                self._rel(compose_paths[0]),
            )

    def _check_tmp_layout(self, section: CheckSection) -> None:
        tmp_dir = self.root / ".tmp"
        if not tmp_dir.is_dir():
            return

        entries = list(tmp_dir.iterdir())
        files = [e for e in entries if e.is_file()]
        dirs = [e for e in entries if e.is_dir()]

        layout_failures = 0
        for entry in dirs:
            section.add_fail(
                f"Invalid directory in .tmp/: {entry.name}", self._rel(entry)
            )
            layout_failures += 1

        audit_nums: list[str] = []
        fix_nums: list[str] = []
        report_files: list[str] = []
        required_extra_count = 0
        optional_prompt_req = False

        for file in files:
            name = file.name
            if m := self.TMP_FIX_RE.match(name):
                fix_nums.append(m.group(1))
                report_files.append(name)
            elif m := self.TMP_AUDIT_RE.match(name):
                audit_nums.append(m.group(1))
                report_files.append(name)
            elif name == self.TMP_REQUIRED_EXTRA:
                required_extra_count += 1
            elif name == self.TMP_OPTIONAL_PROMPT_REQ:
                optional_prompt_req = True
            else:
                section.add_fail(f"Invalid file in .tmp/: {name}", self._rel(file))
                layout_failures += 1

        rel_tmp = self._rel(tmp_dir)
        if required_extra_count != 1:
            section.add_fail(
                "Missing or duplicate test_coverage_and_readme_audit_report.md in .tmp/",
                rel_tmp,
            )
            layout_failures += 1
        if len(report_files) != 4:
            section.add_fail(
                f".tmp must contain exactly 4 audit/fix-check files, found {len(report_files)}",
                rel_tmp,
            )
            layout_failures += 1
        # Accept either 5 files (standard) or 6 files when the optional
        # prompt_requirements_verification.md is present. If the optional
        # file is missing, emit a warning rather than failing.
        if len(files) not in {5, 6}:
            section.add_fail(
                f".tmp must contain exactly 5 files (or 6 with prompt_requirements_verification.md), found {len(files)}",
                rel_tmp,
            )
            layout_failures += 1
        # Warn if the optional prompt requirements file is not present.
        if not optional_prompt_req:
            section.add_warn(
                "Optional file missing: prompt_requirements_verification.md in .tmp/",
                rel_tmp,
            )

        if len(fix_nums) not in {1, 2}:
            section.add_fail(
                f"Expected 1 or 2 fix-check reports, found {len(fix_nums)}", rel_tmp
            )
            layout_failures += 1
        elif len(fix_nums) == 2 and (
            len(audit_nums) != 2 or sorted(audit_nums) != sorted(fix_nums)
        ):
            section.add_fail(
                "When there are 2 fix-check reports, audit and fix-check numbers must match",
                rel_tmp,
            )
            layout_failures += 1
        elif len(fix_nums) == 1:
            if len(audit_nums) != 3 or fix_nums[0] != "1" or "3" not in audit_nums:
                section.add_fail(
                    "With one fix-check report, expected audit_report-1-fix_check.md and 3 audits including audit_report-3.md",
                    rel_tmp,
                )
                layout_failures += 1

        if layout_failures == 0:
            section.add_pass(
                ".tmp/ layout is valid (5-6 files, audit/fix-check pairing)", rel_tmp
            )


class MetadataValidator(SectionValidator):
    def validate(self, section: CheckSection) -> dict[str, Any]:
        path = self.root / "metadata.json"
        rel = self._rel(path)
        content = read_text(path)
        if content is None:
            section.add_fail("metadata.json is missing or not readable", rel)
            return {}
        section.add_pass("metadata.json is readable", rel)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            section.add_fail(f"metadata.json is invalid JSON: {exc.msg}", rel)
            return {}
        section.add_pass("metadata.json is valid JSON", rel)

        if not isinstance(parsed, dict):
            section.add_fail("metadata.json root must be a JSON object", rel)
            return {}
        section.add_pass("metadata.json root is a JSON object", rel)

        missing = sorted(REQUIRED_METADATA_FIELDS - set(parsed.keys()))
        extra = sorted(set(parsed.keys()) - REQUIRED_METADATA_FIELDS)
        if missing:
            section.add_fail(
                f"Missing required metadata fields: {', '.join(missing)}", rel
            )
        else:
            section.add_pass(
                f"All required metadata fields present ({len(REQUIRED_METADATA_FIELDS)})",
                rel,
            )
        if extra:
            section.add_fail(f"Unexpected metadata fields: {', '.join(extra)}", rel)
        else:
            section.add_pass("No unexpected metadata fields", rel)

        for key in sorted(REQUIRED_METADATA_FIELDS):
            if key not in parsed:
                continue
            value = parsed.get(key)
            if not isinstance(value, str):
                section.add_fail(f"metadata.{key} must be a string", rel)
                continue
            if key not in NULLABLE_METADATA_FIELDS and not value.strip():
                section.add_fail(f"metadata.{key} cannot be empty", rel)
                continue
            allow_none = key in METADATA_NONE_ALLOWED_FIELDS
            if is_invalid_metadata_placeholder(value, allow_none=allow_none):
                section.add_fail(
                    f"metadata.{key} is a placeholder value ({value!r}); fill in a real value",
                    rel,
                )
                continue
            section.add_pass(f"metadata.{key} is set", rel)

        project_type = str(parsed.get("project_type", "")).strip().lower()
        if project_type:
            if project_type not in ALLOWED_PROJECT_TYPES:
                section.add_fail(
                    f"metadata.project_type must be one of: {', '.join(sorted(ALLOWED_PROJECT_TYPES))}",
                    rel,
                )
            else:
                section.add_pass(
                    f"metadata.project_type is a known type ({project_type})",
                    rel,
                )
        # Additional checks: backend recognition and English consistency
        try:
            self._check_backend_expectations(section, parsed)
        except Exception:
            section.add_warn(
                "Backend expectation checks skipped due to internal error", rel
            )

        try:
            self._check_english_consistency(section, parsed)
        except Exception:
            section.add_warn(
                "English consistency checks skipped due to internal error", rel
            )

        return parsed

    def _check_backend_expectations(
        self, section: CheckSection, parsed: dict[str, Any]
    ) -> None:
        """Detect backend language from repo files and assert metadata lists required files, with language-specific checks."""
        backend_lang = str(parsed.get("backend_language", "")).strip().lower()
        project_type = str(parsed.get("project_type", "")).strip().lower()
        repo_dir = self.root / "repo"

        if not repo_dir.is_dir():
            section.add_warn(
                "repo/ subdirectory missing; backend-specific checks skipped",
                self._rel(repo_dir),
            )
            return

        # Comprehensive language markers (from validate_package.py)
        language_markers = {
            "python": ["pyproject.toml", "requirements.txt", "setup.py"],
            "js_ts": ["package.json"],
            "java_kotlin": ["pom.xml", "build.gradle", "build.gradle.kts"],
            "go": ["go.mod"],
            "php": ["composer.json"],
            "rust": ["Cargo.toml"],
            "dart_flutter": ["pubspec.yaml"],
            "ruby": ["Gemfile"],
            "csharp": [".csproj", ".sln"],
            "c_cpp": ["CMakeLists.txt", "Makefile"],
        }

        # Detect languages in repo
        detected_languages: set[str] = set()
        for lang, markers in language_markers.items():
            for marker in markers:
                if any((repo_dir / m).exists() for m in (marker, marker.lower())):
                    detected_languages.add(lang)
                    break

        if not detected_languages:
            section.add_warn(
                "No language markers detected in repo/ (no package.json, requirements.txt, etc.)",
                self._rel(repo_dir),
            )
        else:
            section.add_pass(
                f"Detected languages in repo/: {', '.join(sorted(detected_languages))}",
                self._rel(repo_dir),
            )

        # Check if backend_language metadata matches detected languages
        if backend_lang:
            mapped_lang = backend_lang
            # Normalize common synonyms
            synonym_map = {
                "nodejs": "js_ts",
                "javascript": "js_ts",
                "ts": "js_ts",
                "typescript": "js_ts",
                "py": "python",
                "python3": "python",
                "golang": "go",
                "kt": "java_kotlin",
                "kotlin": "java_kotlin",
                "cs": "csharp",
                ".net": "csharp",
                "c++": "c_cpp",
                "cpp": "c_cpp",
            }
            mapped_lang = synonym_map.get(mapped_lang, backend_lang)

            if detected_languages:
                if mapped_lang not in detected_languages:
                    section.add_warn(
                        f"metadata.backend_language='{backend_lang}' but detected: {', '.join(sorted(detected_languages))}",
                        "metadata.json",
                    )
                else:
                    section.add_pass(
                        f"backend_language consistent with detected languages",
                        "repo/",
                    )
        elif detected_languages:
            section.add_warn(
                f"metadata.backend_language empty but repo has: {', '.join(sorted(detected_languages))}",
                "metadata.json",
            )

        # Check project-type specific requirements (Docker, API spec, etc.)
        if project_type in {"server", "fullstack"}:
            docker_checks = ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]
            has_docker = any((repo_dir / f).exists() for f in docker_checks)
            if not has_docker:
                section.add_warn(
                    f"Backend project '{project_type}' should include Docker/docker-compose files",
                    self._rel(repo_dir),
                )
            else:
                section.add_pass(
                    "Docker/docker-compose file(s) detected", self._rel(repo_dir)
                )

            api_checks = [
                "docs/api-spec.md",
                "docs/api_spec.md",
                "docs/api.md",
                "docs/API.md",
                "docs/openapi.yml",
                "docs/openapi.yaml",
            ]
            has_api_spec = any((self.root / f).exists() for f in api_checks)
            if not has_api_spec:
                section.add_warn(
                    f"Backend project '{project_type}' should include API specification",
                    "docs/",
                )
            else:
                section.add_pass("API specification file(s) detected", "docs/")

        # Check for language-specific gitignore patterns
        gitignore_paths = [repo_dir / ".gitignore", self.root / ".gitignore"]
        has_gitignore = any(p.is_file() for p in gitignore_paths)
        if not has_gitignore:
            section.add_warn(
                ".gitignore file not found in repo/ or root (recommended for all projects)",
                self._rel(repo_dir),
            )
        else:
            # Check if gitignore has language-specific patterns
            expected_patterns_by_lang = {}
            for lang in detected_languages:
                expected_patterns_by_lang[lang] = LANGUAGE_GITIGNORE_PATTERNS.get(
                    lang, []
                )[:3]

            if expected_patterns_by_lang:
                section.add_pass(
                    f"Language-specific gitignore patterns expected for: {', '.join(sorted(detected_languages))}",
                    self._rel(repo_dir),
                )

    def _check_english_consistency(
        self, section: CheckSection, parsed: dict[str, Any]
    ) -> None:
        """Heuristic checks that key user-facing texts are predominantly English."""
        targets = []
        # metadata.prompt
        prompt = str(parsed.get("prompt", "") or "").strip()
        if prompt:
            targets.append(("metadata.prompt", prompt))
        # docs and READMEs
        for doc in (
            "docs/design.md",
            "docs/questions.md",
            "repo/README.md",
            "README.md",
        ):
            p = self.root / doc
            txt = read_text(p)
            if txt:
                targets.append((doc, txt))

        if not targets:
            section.add_warn(
                "No textual artifacts found for English consistency checks",
                "metadata.json",
            )
            return

        def _english_ratio(text: str) -> float:
            if not text:
                return 0.0
            total = 0
            latin = 0
            for ch in text:
                if ch.isspace():
                    continue
                total += 1
                o = ord(ch)
                if (0x0041 <= o <= 0x007A) or (0x00C0 <= o <= 0x024F):
                    latin += 1
            return (latin / total) if total else 0.0

        findings = 0
        non_english: list[tuple[str, float, list[int]]] = []
        for source, text in targets:
            ratio = _english_ratio(text)
            chinese_lines = find_chinese_line_numbers(text)
            if ratio < PROMPT_ENGLISH_RATIO_THRESHOLD or chinese_lines:
                findings += 1
                non_english.append((source, ratio, chinese_lines))

        if findings == 0:
            section.add_pass(
                f"Key user-facing texts appear predominantly English (threshold={PROMPT_ENGLISH_RATIO_THRESHOLD:.2f})",
                "metadata.json",
            )
        else:
            for source, ratio, chinese_lines in non_english[:4]:
                lines_text = format_line_numbers(chinese_lines)
                msg = f"{source}: english_ratio={ratio:.2f} (threshold={PROMPT_ENGLISH_RATIO_THRESHOLD:.2f})"
                if lines_text:
                    msg += f"; chinese lines: {lines_text}"
                section.add_warn(msg, source)

        # Comprehensive scan: every text file in the package must be Chinese-free.
        # Skips binaries, runtime noise, gitignored paths, original_sessions/,
        # validation_report.md, and forbidden-delivery files.
        scan_failures = 0
        scanned = 0
        for path, content in iter_readable_text_files(self.root):
            scanned += 1
            chinese_lines = find_chinese_line_numbers(content)
            if chinese_lines:
                scan_failures += 1
                section.add_fail(
                    f"Chinese characters detected (lines: {format_line_numbers(chinese_lines)})",
                    self._rel(path),
                )

        if scan_failures == 0:
            section.add_pass(
                f"No Chinese characters found in {scanned} scanned text file(s)",
                ".",
            )


class SessionsValidator(SectionValidator):
    def __init__(self, root: Path, metadata: dict[str, Any]) -> None:
        super().__init__(root)
        self.metadata = metadata
        self._session_extract_dirs: list[tempfile.TemporaryDirectory[str]] = []

    def validate(self, section: CheckSection) -> None:
        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR
        if not sessions_dir.is_dir():
            section.add_fail(
                "Missing original_sessions/ directory", self._rel(sessions_dir)
            )
            return

        self._check_root_legacy_sessions(section)

        if not list(sessions_dir.iterdir()):
            section.add_fail("original_sessions/ is empty", self._rel(sessions_dir))
            return

        # Check for ANY zip file in original_sessions/
        all_zips = sorted(
            [p for p in sessions_dir.rglob("*.zip") if p.is_file()],
            key=lambda p: p.name.lower(),
        )
        if not all_zips:
            section.add_fail(
                "No session zip archives found in original_sessions/",
                self._rel(sessions_dir),
            )
            return

        # Validate and extract all zip files found
        extracted_roots = self._validate_and_extract_all_archives(
            section, sessions_dir, all_zips
        )

        if not extracted_roots:
            section.add_fail(
                "No session zip archives could be successfully extracted",
                self._rel(sessions_dir),
            )
            return

        for extracted_root in extracted_roots:
            self._check_session_naming(section, extracted_root)
            self._check_session_id_matches_filename(section, extracted_root)
            self._check_bundle_structure(section, extracted_root)
            self._check_json_and_jsonl_validity(section, extracted_root)
            self._check_original_sessions_token_usage(section, extracted_root)
            self._check_prompt_anchor(section, extracted_root)
            self._check_latest_trajectory(section, extracted_root)
            self._check_forbidden_keywords(section, extracted_root)

    def _validate_and_extract_all_archives(
        self, section: CheckSection, sessions_dir: Path, all_zips: list[Path]
    ) -> list[Path]:
        """Validate and extract all zip archives (leading or non-leading), handling multiple archives."""
        extracted_roots: list[Path] = []
        processed_top_names: dict[str, int] = {}

        for archive_path in all_zips:
            ok, top_level_name, extract_root = self._validate_extract_single_archive(
                section, archive_path, sessions_dir, processed_top_names
            )
            if ok and extract_root is not None:
                extracted_roots.append(extract_root)

        return extracted_roots

    def _validate_extract_single_archive(
        self,
        section: CheckSection,
        archive_path: Path,
        sessions_dir: Path,
        processed_top_names: dict[str, int],
    ) -> tuple[bool, str | None, Path | None]:
        """Validate and extract a single archive. Returns (success, top_level_name, extract_root)."""
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                infos = archive.infolist()
                if not infos:
                    section.add_fail("Archive is empty", self._rel(archive_path))
                    return False, None, None

                broken_member = archive.testzip()
                if broken_member is not None:
                    section.add_fail(
                        f"Archive validation failed, corrupted member: {broken_member}",
                        self._rel(archive_path),
                    )
                    return False, None, None

                # Extract top-level structure
                top_level_names: set[str] = set()
                top_level_file_names: set[str] = set()
                top_level_nested_names: set[str] = set()

                for info in infos:
                    normalized_name = info.filename.replace("\\", "/").lstrip("/")
                    if not normalized_name:
                        continue
                    parts = [p for p in normalized_name.split("/") if p and p != "."]
                    if not parts:
                        continue
                    if any(p == ".." for p in parts):
                        section.add_fail(
                            f"Archive contains illegal path: {info.filename}",
                            self._rel(archive_path),
                        )
                        return False, None, None

                    top_level_names.add(parts[0])
                    if len(parts) == 1 and not info.is_dir():
                        top_level_file_names.add(parts[0])
                    if len(parts) > 1:
                        top_level_nested_names.add(parts[0])

                # Validate single top-level folder
                if len(top_level_names) != 1:
                    section.add_fail(
                        f"Archive must have exactly one top-level folder; found: {', '.join(sorted(top_level_names)) or 'none'}",
                        self._rel(archive_path),
                    )
                    return False, None, None

                top_level_name = next(iter(top_level_names))

                if top_level_name in top_level_file_names:
                    section.add_fail(
                        f"Top-level entry {top_level_name} must be directory not file",
                        self._rel(archive_path),
                    )
                    return False, None, None

                if top_level_name not in top_level_nested_names:
                    section.add_fail(
                        f"Top-level folder {top_level_name}/ has no nested content",
                        self._rel(archive_path),
                    )
                    return False, None, None

                # Validate both zip filename and folder name
                zip_stem = archive_path.stem
                expected_name = zip_stem
                zip_name_valid = self._is_leading_session_zip_archive(archive_path)

                if not zip_name_valid:
                    section.add_fail(
                        f"Archive filename is not leading-style: {archive_path.name}",
                        self._rel(archive_path),
                    )

                if top_level_name != expected_name:
                    section.add_fail(
                        f"Folder name mismatch: archive={expected_name}/, folder={top_level_name}/",
                        self._rel(archive_path),
                    )
                else:
                    section.add_pass(
                        f"Archive and folder naming valid: {top_level_name}/",
                        self._rel(archive_path),
                    )

                # Handle same-name preexisting extracted dirs
                if top_level_name in processed_top_names:
                    processed_top_names[top_level_name] += 1
                    extract_parent = (
                        sessions_dir
                        / f"{top_level_name}-{processed_top_names[top_level_name]}"
                    )
                else:
                    processed_top_names[top_level_name] = 1
                    extract_parent = sessions_dir

                # Extract to temp dir
                temp_dir = tempfile.TemporaryDirectory(prefix="validator_sessions_")
                self._session_extract_dirs.append(temp_dir)
                extract_root = Path(temp_dir.name)
                archive.extractall(extract_root)

                extracted_top = extract_root / top_level_name
                if not extracted_top.is_dir():
                    section.add_fail(
                        f"Extracted folder missing: {top_level_name}/",
                        self._rel(archive_path),
                    )
                    return False, None, None

                section.add_pass(
                    f"Archive {archive_path.name} extracted successfully",
                    self._rel(archive_path),
                )
                return True, top_level_name, extracted_top

        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            section.add_fail(
                f"Archive validation/extraction failed: {exc}",
                self._rel(archive_path),
            )
            return False, None, None

    def _check_session_naming(self, section: CheckSection, sessions_dir: Path) -> None:
        failures = 0
        for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir():
                if entry.name.lower() in LEADING_BUNDLE_EXEMPT_DIR_NAMES:
                    continue
                if not SESSION_UUID_RE.match(entry.name):
                    failures += 1
                    section.add_fail(
                        f"Session directory name is not a UUID: {entry.name}",
                        self._rel(entry),
                    )
                continue
            if not entry.is_file():
                continue
            if is_allowed_non_uuid_first_layer_file(entry.name):
                continue
            if entry.suffix.lower() != ".jsonl":
                failures += 1
                section.add_fail(
                    f"Session file must be .jsonl: {entry.name}",
                    self._rel(entry),
                )
                continue
            if not SESSION_UUID_RE.match(entry.stem):
                failures += 1
                section.add_fail(
                    f"Session file stem is not a UUID: {entry.name}",
                    self._rel(entry),
                )
        if failures == 0:
            section.add_pass(
                "Session file/directory names are valid UUIDs with .jsonl extension",
                self._rel(sessions_dir),
            )

    def _check_session_id_matches_filename(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        """For each first-layer *.jsonl whose stem is a UUID, ensure the
        `sessionId` recorded inside the file matches the filename stem.
        """
        mismatches = 0
        checked = 0
        skipped_no_id = 0
        for path in self._first_layer_jsonl(sessions_dir):
            if is_allowed_non_uuid_first_layer_file(path.name):
                continue
            if not SESSION_UUID_RE.match(path.stem):
                # Naming check will already have failed this; skip to avoid
                # piling on a duplicate error.
                continue
            checked += 1
            internal_id = self._read_session_id(path)
            if internal_id is None:
                skipped_no_id += 1
                section.add_warn(
                    "Could not find sessionId in file; cannot verify name matches contents",
                    self._rel(path),
                )
                continue
            if internal_id != path.stem:
                mismatches += 1
                section.add_fail(
                    (
                        f"Filename does not match internal sessionId "
                        f"(file stem={path.stem}, sessionId={internal_id})"
                    ),
                    self._rel(path),
                )

        if checked and mismatches == 0 and skipped_no_id == 0:
            section.add_pass(
                f"Session filenames match internal sessionId ({checked} file(s))",
                self._rel(sessions_dir),
            )

    @staticmethod
    def _read_session_id(path: Path) -> str | None:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    sid = payload.get("sessionId") or payload.get("session_id")
                    if isinstance(sid, str) and sid.strip():
                        return sid.strip()
        except OSError:
            return None
        return None

    def _check_root_legacy_sessions(self, section: CheckSection) -> None:
        legacy_pattern = re.compile(
            r"^(trajectory(?:[-_]\d+)?|develop(?:[-_]\d+)?|bugfix(?:[-_]\d+)?)\.json$"
        )
        legacy_findings = 0
        for entry in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and legacy_pattern.match(entry.name.lower()):
                section.add_fail(
                    "Legacy session JSON found in root; move it to original_sessions/",
                    self._rel(entry),
                )
                legacy_findings += 1
        if legacy_findings == 0:
            section.add_pass("No legacy session JSON files at repo root", ".")

    def _check_bundle_structure(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        child_dirs = sorted(
            [p for p in sessions_dir.iterdir() if p.is_dir()],
            key=lambda p: p.name.lower(),
        )
        session_dirs = [
            p
            for p in child_dirs
            if p.name.lower() not in LEADING_BUNDLE_EXEMPT_DIR_NAMES
        ]

        if not session_dirs:
            section.add_warn(
                "No session_id directories were found under Session zip/",
                self._rel(sessions_dir),
            )
            return

        failures = 0
        for session_dir in session_dirs:
            session_id = session_dir.name
            required_jsonl = sessions_dir / f"{session_id}.jsonl"
            if not required_jsonl.is_file():
                failures += 1
                section.add_fail(
                    f"Missing required top-level session file {session_id}.jsonl for directory {session_id}/",
                    self._rel(session_dir),
                )
            subagents_dir = session_dir / "subagents"
            if not subagents_dir.is_dir():
                continue
            for jsonl_file in sorted(
                subagents_dir.glob("*.jsonl"), key=lambda p: p.name.lower()
            ):
                stem = jsonl_file.stem.lower()
                if (
                    not stem.startswith("agent-")
                    or stem.startswith("agent-aside_question")
                    or "acompact" in stem
                ):
                    continue
                meta = subagents_dir / f"{jsonl_file.stem}.meta.json"
                if not meta.is_file():
                    failures += 1
                    section.add_fail(
                        f"Missing meta file {meta.name} for {jsonl_file.name}",
                        self._rel(jsonl_file),
                    )

        if failures == 0:
            section.add_pass(
                "Session directory structure check passed", self._rel(sessions_dir)
            )

    @staticmethod
    def _first_layer_jsonl(sessions_dir: Path) -> list[Path]:
        return sorted(
            [
                p
                for p in sessions_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".jsonl"
            ],
            key=lambda p: p.name.lower(),
        )

    def _check_latest_trajectory(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        candidates: list[tuple[datetime, str, int, Path]] = []
        for jsonl_path in self._first_layer_jsonl(sessions_dir):
            if is_allowed_non_uuid_first_layer_file(jsonl_path.name):
                continue
            analysis = analyze_jsonl(jsonl_path)
            if (
                analysis.latest_timestamp_dt
                and analysis.latest_timestamp_raw
                and analysis.latest_timestamp_line
            ):
                candidates.append(
                    (
                        analysis.latest_timestamp_dt,
                        analysis.latest_timestamp_raw,
                        analysis.latest_timestamp_line,
                        jsonl_path,
                    )
                )

        if not candidates:
            section.add_warn(
                "Skipped latest trajectory completeness check: no usable timestamp in first-layer jsonl files",
                self._rel(sessions_dir),
            )
            return

        candidates.sort(
            key=lambda item: (item[0], item[3].as_posix().lower()), reverse=True
        )
        _, latest_raw, latest_line_no, latest_jsonl = candidates[0]
        analysis = analyze_jsonl(latest_jsonl)

        rel = self._rel(latest_jsonl)
        if analysis.last_semantic_line is None:
            section.add_fail(
                "No semantic message event found in latest trajectory file", rel
            )
            return
        if (analysis.last_semantic_role or "") != "assistant":
            section.add_fail(
                f"Last semantic message role is not assistant (line {analysis.last_semantic_line}, role={analysis.last_semantic_role or 'unknown'})",
                rel,
            )
            return
        if not analysis.last_semantic_has_assistant_text:
            section.add_fail(
                f"Last semantic assistant message has no text content (line {analysis.last_semantic_line})",
                rel,
            )
            return
        section.add_pass(
            f"Latest trajectory completeness passed (file={latest_jsonl.name}, latest_timestamp={latest_raw}, line={latest_line_no})",
            rel,
        )

    def _check_prompt_anchor(self, section: CheckSection, sessions_dir: Path) -> None:
        prompt_text = str(self.metadata.get("prompt", "")).strip()
        if not prompt_text:
            section.add_warn(
                "Skipped session prompt-anchor check: metadata.prompt is missing or empty",
                self._rel(self.root / "metadata.json"),
            )
            return

        # Collect first-layer jsonl files with their first timestamp for ordering.
        timestamp_candidates: list[tuple[tuple[int, str], Path, str, int]] = []
        for path in self._first_layer_jsonl(sessions_dir):
            analysis = analyze_jsonl(path)
            if (
                analysis.first_timestamp_raw is None
                or analysis.first_timestamp_line is None
            ):
                continue
            ts = parse_iso_timestamp(analysis.first_timestamp_raw)
            order_key = (0, ts.isoformat()) if ts else (1, analysis.first_timestamp_raw)
            timestamp_candidates.append(
                (
                    order_key,
                    path,
                    analysis.first_timestamp_raw,
                    analysis.first_timestamp_line,
                )
            )

        if not timestamp_candidates:
            section.add_warn(
                "Skipped session prompt-anchor check: no usable timestamp in first-layer jsonl files",
                self._rel(sessions_dir),
            )
            return

        # Sort and pick the first file (earliest by timestamp) as primary reference.
        timestamp_candidates.sort(
            key=lambda item: (item[0], item[1].as_posix().lower())
        )
        _, first_jsonl, first_ts, first_line = timestamp_candidates[0]

        # --- New requirement: ensure first cwd path ends with TASK-req_* ---
        cwd_value = _find_first_cwd_in_jsonl(first_jsonl)
        if cwd_value is None:
            section.add_warn(
                "Could not find a 'cwd' field in the first session file; cwd check skipped",
                self._rel(first_jsonl),
            )
        else:
            if not re.search(r"TASK-req_[A-Za-z0-9_-]+$", cwd_value):
                section.add_warn(
                    "Development appears not to be running from project root: first session 'cwd' does not end with TASK-req_*; development should be done from the project root",
                    self._rel(first_jsonl),
                )
                return

        # Evaluate prompt-anchor against the earliest jsonl file first (follow validate_package.py behavior).
        analysis = analyze_jsonl(first_jsonl)
        window_candidates: list[tuple[str, int, str]] = []
        window_start: datetime | None = None
        window_end: datetime | None = None
        for content_text, line_no, ts_raw, parsed_ts in analysis.user_candidates:
            if window_start is None:
                window_start = parsed_ts
                window_end = parsed_ts + timedelta(
                    minutes=SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES
                )
                window_candidates.append((content_text, line_no, ts_raw))
                continue
            if window_end is not None and window_start <= parsed_ts <= window_end:
                window_candidates.append((content_text, line_no, ts_raw))

        if not window_candidates:
            section.add_warn(
                "Skipped session prompt-anchor check: no comparable user content found in the earliest jsonl",
                self._rel(first_jsonl),
            )
            return

        reference_normalized_len = len(_normalize_for_compare(prompt_text))
        best_similarity = -1.0
        best_line = -1
        best_compare: dict[str, Any] | None = None
        for content_text, line_no, _ in window_candidates:
            # length filter similar to validate_package.py
            cand_norm_len = len(_normalize_for_compare(content_text))
            if (
                reference_normalized_len > 0
                and cand_norm_len
                < reference_normalized_len * SESSION_PROMPT_MIN_LENGTH_RATIO
            ):
                continue
            compare = compare_by_anchor(prompt_text, content_text)
            similarity = float(compare.get("similarity", 0.0))
            if similarity > best_similarity:
                best_similarity = similarity
                best_line = line_no
                best_compare = compare
            if bool(compare.get("near_duplicate")):
                pass_mode = (
                    "Anchor matched"
                    if bool(compare.get("matched"))
                    else "Anchor not matched (full-segment similarity fallback)"
                )
                section.add_pass(
                    (
                        "Session prompt-anchor check passed "
                        f"(similarity={similarity:.6f} >= threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f}) "
                        f"(Check method={pass_mode}) (file={first_jsonl.name}, file_first_timestamp={first_ts}, file_first_timestamp_line={first_line}, match_line={line_no}, window_candidate_count={len(window_candidates)})"
                    ),
                    self._rel(first_jsonl),
                )
                return

        diff_note = ""
        if best_compare is not None and int(best_compare.get("diff_index", -1)) >= 0:
            diff_note = (
                f", first difference position={int(best_compare.get('diff_index', -1))}"
            )

        section.add_fail(
            (
                "Session prompt-anchor check failed due to low similarity "
                f"(similarity={best_similarity:.6f} < threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f}{diff_note}) "
                f"(file={first_jsonl.name}, file_first_timestamp={first_ts}, best_line={best_line}, window_candidate_count={len(window_candidates)})"
            ),
            self._rel(first_jsonl),
        )

    def _check_forbidden_keywords(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        """Check that session JSONL files don't contain forbidden development scripts/tools."""
        jsonl_files = sorted(
            sessions_dir.rglob("*.jsonl"), key=lambda p: p.as_posix().lower()
        )
        if not jsonl_files:
            section.add_warn(
                "No JSONL files found; forbidden keyword check skipped",
                self._rel(sessions_dir),
            )
            return

        findings = 0
        unreadable_count = 0
        for path in jsonl_files:
            analysis = analyze_jsonl(path)
            if not analysis.readable:
                section.add_fail(
                    "JSONL file is unreadable; forbidden keyword check not performed",
                    self._rel(path),
                )
                unreadable_count += 1
                findings += 1
                continue

            for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS:
                lines = analysis.keyword_line_numbers.get(keyword, [])
                if not lines:
                    continue
                findings += 1
                section.add_fail(
                    f'Forbidden keyword "{keyword}" detected (lines: {self._format_lines(lines)})',
                    self._rel(path),
                )

        if findings == 0:
            section.add_pass(
                "No forbidden keywords detected in Session JSONL files",
                self._rel(sessions_dir),
            )

    def _collect_all_original_sessions_json_and_jsonl_files(
        self, sessions_dir: Path
    ) -> tuple[list[Path], list[Path]]:
        json_files: list[Path] = []
        jsonl_files: list[Path] = []
        for path in sorted(sessions_dir.rglob("*"), key=lambda p: p.as_posix().lower()):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix == ".json":
                json_files.append(path)
            elif suffix == ".jsonl":
                jsonl_files.append(path)
        return json_files, jsonl_files

    def _check_json_and_jsonl_validity(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        json_files, jsonl_files = (
            self._collect_all_original_sessions_json_and_jsonl_files(sessions_dir)
        )

        if not json_files and not jsonl_files:
            section.add_warn(
                "No JSON/JSONL files found under Session zip; validity checks skipped",
                self._rel(sessions_dir),
            )
            return

        json_failures = 0
        for path in json_files:
            content = read_text(path)
            if content is None:
                section.add_fail("JSON file is missing or unreadable", self._rel(path))
                json_failures += 1
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                section.add_fail(f"JSON file is invalid: {exc.msg}", self._rel(path))
                json_failures += 1
                continue
            if not isinstance(parsed, dict):
                section.add_fail("JSON file root must be an object", self._rel(path))
                json_failures += 1

        jsonl_failures = 0
        for path in jsonl_files:
            content = read_text(path)
            if content is None:
                section.add_fail("JSONL file is missing or unreadable", self._rel(path))
                jsonl_failures += 1
                continue
            valid = True
            for line_no, raw_line in enumerate(content.splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    section.add_fail(
                        f"JSONL file contains invalid JSON at line {line_no}: {exc.msg}",
                        self._rel(path),
                    )
                    jsonl_failures += 1
                    valid = False
                    break
                if not isinstance(parsed, dict):
                    section.add_fail(
                        f"JSONL file line {line_no} must be a JSON object",
                        self._rel(path),
                    )
                    jsonl_failures += 1
                    valid = False
                    break
            if valid:
                section.add_pass(
                    "JSONL file is readable and line-delimited JSON is valid",
                    self._rel(path),
                )

        if json_failures == 0:
            section.add_pass(
                "All JSON files under Session zip are valid", self._rel(sessions_dir)
            )
        if jsonl_failures == 0:
            section.add_pass(
                "All JSONL files under Session zip are valid", self._rel(sessions_dir)
            )

    def _check_original_sessions_token_usage(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        jsonl_files = sorted(
            sessions_dir.rglob("*.jsonl"), key=lambda p: p.as_posix().lower()
        )
        if not jsonl_files:
            section.add_warn(
                "No jsonl files found under Session zip; token usage check skipped",
                self._rel(sessions_dir),
            )
            return

        total_tokens = _new_usage_totals()
        total_usage_records = 0
        checked_files = 0
        unreadable_files = 0

        for path in jsonl_files:
            analysis = _analyze_jsonl_usage_only(path)
            if not analysis.readable:
                unreadable_files += 1
                section.add_fail(
                    "jsonl file is unreadable; token usage accounting failed",
                    self._rel(path),
                )
                continue
            checked_files += 1
            _add_usage_totals(total_tokens, analysis.usage_totals)
            total_usage_records += analysis.usage_record_count

        if checked_files == 0:
            section.add_fail(
                "No readable jsonl files found under Session zip; token usage accounting failed",
                self._rel(sessions_dir),
            )
            return

        if unreadable_files > 0:
            section.add_fail(
                f"Token usage accounting encountered {unreadable_files} unreadable jsonl file(s)",
                self._rel(sessions_dir),
            )

        section.add_pass(
            (
                "Session token usage accounted for "
                f"{checked_files} file(s), usage_records={total_usage_records:,}, "
                f"input={total_tokens['input_tokens']:,}, "
                f"output={total_tokens['output_tokens']:,}, "
                f"cache_read={total_tokens['cache_read_tokens']:,}, "
                f"cache_write={total_tokens['cache_write_tokens']:,}"
            ),
            self._rel(sessions_dir),
        )

    def _is_session_archive_file(self, path: Path) -> bool:
        suffixes = [suffix.lower() for suffix in path.suffixes]
        if not suffixes:
            return False
        if len(suffixes) >= 2 and suffixes[-2:] == [".tar", ".gz"]:
            return True
        return suffixes[-1] in SESSION_ARCHIVE_SUFFIXES

    def _is_linux_style_session_zip_stem(self, stem: str) -> bool:
        return stem.startswith(LEADING_SESSION_ZIP_LINUX_PREFIX)

    def _is_windows_style_session_zip_stem(self, stem: str) -> bool:
        return LEADING_SESSION_ZIP_WINDOWS_PREFIX_RE.match(stem) is not None

    def _is_leading_session_zip_archive(self, path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() != ".zip":
            return False
        stem = path.stem
        return self._is_linux_style_session_zip_stem(
            stem
        ) or self._is_windows_style_session_zip_stem(stem)

    def _collect_leading_session_zip_archives(self, sessions_dir: Path) -> list[Path]:
        archives = [
            path
            for path in sessions_dir.iterdir()
            if self._is_leading_session_zip_archive(path)
        ]
        archives.sort(key=lambda p: p.name.lower())
        return archives

    def _check_leading_session_zip_archives(
        self, section: CheckSection, sessions_dir: Path
    ) -> None:
        archives = self._collect_leading_session_zip_archives(sessions_dir)
        if not archives:
            return

        failures = 0
        for archive_path in archives:
            try:
                with zipfile.ZipFile(archive_path, "r") as archive:
                    infos = archive.infolist()
                    if not infos:
                        section.add_fail(
                            "Zip archive is empty", self._rel(archive_path)
                        )
                        failures += 1
                        continue

                    broken_member = archive.testzip()
                    if broken_member is not None:
                        section.add_fail(
                            f"Zip archive validation failed, corrupted member: {broken_member}",
                            self._rel(archive_path),
                        )
                        failures += 1
                        continue

                    top_level_names: set[str] = set()
                    top_level_file_names: set[str] = set()
                    top_level_nested_names: set[str] = set()
                    for info in infos:
                        normalized_name = info.filename.replace("\\", "/").lstrip("/")
                        if not normalized_name:
                            continue
                        parts = [
                            part
                            for part in normalized_name.split("/")
                            if part and part != "."
                        ]
                        if not parts:
                            continue
                        if any(part == ".." for part in parts):
                            section.add_fail(
                                f"Zip archive contains illegal path: {info.filename}",
                                self._rel(archive_path),
                            )
                            failures += 1
                            break
                        top_level_names.add(parts[0])
                        if len(parts) == 1 and not info.is_dir():
                            top_level_file_names.add(parts[0])
                        if len(parts) > 1:
                            top_level_nested_names.add(parts[0])

                    if failures and top_level_names:
                        continue

                    if self._is_linux_style_session_zip_stem(archive_path.stem):
                        expected_top = archive_path.stem
                        if top_level_names != {expected_top}:
                            section.add_fail(
                                (
                                    f"Linux session zip must contain only same-name directory {expected_top}/, "
                                    f"found: {', '.join(sorted(top_level_names)) or 'empty'}"
                                ),
                                self._rel(archive_path),
                            )
                            failures += 1
                            continue
                        if expected_top in top_level_file_names:
                            section.add_fail(
                                f"Linux session zip contains same-name file {expected_top}; it must be a directory",
                                self._rel(archive_path),
                            )
                            failures += 1
                            continue
                        if expected_top not in top_level_nested_names:
                            section.add_fail(
                                f"Linux session zip missing nested content under same-name directory {expected_top}/",
                                self._rel(archive_path),
                            )
                            failures += 1
                            continue

                    section.add_pass(
                        "Leading session zip archive layout is valid",
                        self._rel(archive_path),
                    )
            except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
                section.add_fail(
                    f"Zip archive could not be validated: {exc}",
                    self._rel(archive_path),
                )
                failures += 1

        if failures == 0:
            section.add_pass(
                "All leading session zip archives passed layout checks",
                self._rel(sessions_dir),
            )

    @staticmethod
    def _format_lines(line_numbers: list[int]) -> str:
        if not line_numbers:
            return ""
        if len(line_numbers) <= 20:
            return ", ".join(str(n) for n in line_numbers)
        return (
            ", ".join(str(n) for n in line_numbers[:20])
            + f" ... total {len(line_numbers)} lines"
        )


class AquilaAlignmentValidator(SectionValidator):
    def __init__(
        self,
        root: Path,
        metadata: dict[str, Any],
        task_id: str | None,
        access_token: str | None,
        task_url_template: str,
        prefetched_task: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(root)
        self.metadata = metadata
        self.task_id = task_id
        self.access_token = access_token
        self.task_url_template = task_url_template
        self.prefetched_task = prefetched_task

    def validate(self, section: CheckSection) -> None:
        if self.prefetched_task is not None:
            task: dict[str, Any] | None = self.prefetched_task
            error: str | None = None
        else:
            task, error = fetch_aquila_task(
                self.task_id or get_env_value(TASK_ID_ENV_VARS),
                self.access_token or get_env_value(ACCESS_TOKEN_ENV_VARS),
                self.task_url_template,
            )
        if task is None:
            section.add_warn(f"Aquila cross-check skipped: {error}", ".")
            return

        self._check_prompt(section, task)
        self._check_scalar(section, task, "project_type")
        self._check_scalar(section, task, "database")

    def _check_prompt(self, section: CheckSection, task: dict[str, Any]) -> None:
        metadata_prompt = str(self.metadata.get("prompt", "")).strip()
        aquila_prompt = str(task.get("prompt_text", "")).strip()
        if not metadata_prompt or not aquila_prompt:
            section.add_warn(
                "Prompt cross-check skipped: metadata.prompt or aquila.prompt_text is empty",
                "metadata.json",
            )
            return
        compare = compare_by_anchor(aquila_prompt, metadata_prompt)
        similarity = float(compare["similarity"])
        if bool(compare["near_duplicate"]):
            section.add_pass(
                f"metadata.prompt matches Aquila prompt_text (similarity={similarity:.6f})",
                "metadata.json",
            )
        else:
            section.add_fail(
                (
                    "metadata.prompt does not match Aquila prompt_text "
                    f"(similarity={similarity:.6f}, threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f})"
                ),
                "metadata.json",
            )

    def _check_scalar(
        self, section: CheckSection, task: dict[str, Any], field_name: str
    ) -> None:
        metadata_value = normalize_aquila_value(self.metadata.get(field_name)).lower()
        aquila_value = normalize_aquila_value(task.get(field_name)).lower()
        if metadata_value == aquila_value:
            section.add_pass(
                f"metadata.{field_name} matches Aquila {field_name} ({metadata_value})",
                "metadata.json",
            )
        else:
            section.add_fail(
                f"metadata.{field_name} mismatch: metadata={metadata_value or 'empty'}, aquila={aquila_value}",
                "metadata.json",
            )


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

STATUS_ICON = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}
STATUS_ORDER = {"FAIL": 0, "WARN": 1, "PASS": 2}
STATUS_HEADING = {
    "FAIL": "❌ Failures",
    "WARN": "⚠️  Warnings",
    "PASS": "✅ Passed",
}

_GROUP_KEY_SPLIT_RE = re.compile(r"[:\(\-—]")


def _item_group_key(item: CheckItem) -> tuple[str, str]:
    """Cluster similar messages together.

    Take the message up to the first ``:`` / ``(`` / ``-`` / ``—`` separator and
    lowercase it; tie-break by the full message so order is deterministic.
    """
    head = _GROUP_KEY_SPLIT_RE.split(item.message, 1)[0].strip().lower()
    return (head, item.message.lower())


def _render_item_line(item: CheckItem) -> str:
    icon = STATUS_ICON.get(item.status, "•")
    if item.status == "FAIL":
        return f"  - **[{icon} {item.status}] {item.message}** ({item.rel_path})"
    return f"  - [{icon} {item.status}] {item.message} ({item.rel_path})"


def render_report(sections: list[CheckSection]) -> str:
    lines = ["# Validation Report", ""]

    # Top-of-report callout listing every section that has at least one
    # failure, so failures are visible at a glance without scrolling.
    failing_sections = [
        s for s in sections if any(it.status == "FAIL" for it in s.items)
    ]
    total_fails = sum(1 for s in sections for it in s.items if it.status == "FAIL")
    total_warns = sum(1 for s in sections for it in s.items if it.status == "WARN")
    if total_fails:
        section_word = "section" if len(failing_sections) == 1 else "sections"
        fail_word = "failure" if total_fails == 1 else "failures"
        lines.append(
            f"> ❌ **{total_fails} {fail_word}** across "
            f"{len(failing_sections)} {section_word}"
            + (f" (+{total_warns} warnings)" if total_warns else "")
            + ":"
        )
        for sec in failing_sections:
            n = sum(1 for it in sec.items if it.status == "FAIL")
            lines.append(f"> - {sec.title} ({n})")
        lines.append("")
    elif total_warns:
        warn_word = "warning" if total_warns == 1 else "warnings"
        lines.append(f"> ⚠️  No failures, but {total_warns} {warn_word}.")
        lines.append("")

    for section in sections:
        lines.append(f"## {section.title}")
        if not section.items:
            lines.append(f"- [{STATUS_ICON['PASS']} PASS] No checks")
            lines.append("")
            continue

        # Group by status (FAIL → WARN → PASS); within each status, cluster
        # similar messages by their leading phrase so e.g. all "Missing
        # directory: …" entries land next to each other.
        by_status: dict[str, list[CheckItem]] = {"FAIL": [], "WARN": [], "PASS": []}
        for item in section.items:
            by_status.setdefault(item.status, []).append(item)

        for status in ("FAIL", "WARN", "PASS"):
            bucket = by_status.get(status) or []
            if not bucket:
                continue
            bucket.sort(key=_item_group_key)
            heading = STATUS_HEADING.get(status, status)
            lines.append(f"### {heading} ({len(bucket)})")
            for item in bucket:
                lines.append(_render_item_line(item))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def count_status(sections: list[CheckSection]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for section in sections:
        for item in section.items:
            counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def print_report(sections: list[CheckSection]) -> None:
    """Stream the report to stdout so CI logs (uploaded to S3) capture it."""
    sys.stdout.write("\n========== VALIDATION REPORT ==========\n")
    sys.stdout.write(render_report(sections))
    counts = count_status(sections)
    sys.stdout.write(
        f"========== SUMMARY: PASS={counts['PASS']} FAIL={counts['FAIL']} WARN={counts['WARN']} ==========\n"
    )
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Validator:
    def __init__(
        self,
        target: str,
        task_id: str | None,
        access_token: str | None,
        task_url_template: str,
        prefetched_task: dict[str, Any] | None = None,
        preface_sections: list[CheckSection] | None = None,
    ) -> None:
        self.target = target
        self.task_id = task_id
        self.access_token = access_token
        self.task_url_template = task_url_template
        self.prefetched_task = prefetched_task
        # Sections produced before Validator.run() (e.g. CI preflight) are kept
        # in the final report so the structure check is always visible in logs.
        self.sections: list[CheckSection] = list(preface_sections or [])

    def run(self, validate_structure: bool = True) -> int:
        next_no = len(self.sections) + 1
        input_section = self._new_section(f"{next_no}. Input Directory")
        root = self._resolve_root(input_section)
        if root is None:
            print_report(self.sections)
            return 1

        if validate_structure:
            next_no += 1
            structure_section = self._new_section(f"{next_no}. Structure")
            StructureValidator(root, expect_original_sessions=True).validate(
                structure_section
            )

        next_no += 1
        metadata_section = self._new_section(f"{next_no}. metadata.json")
        metadata = MetadataValidator(root).validate(metadata_section)

        next_no += 1
        sessions_section = self._new_section(f"{next_no}. Session zip")
        SessionsValidator(root, metadata).validate(sessions_section)

        next_no += 1
        aquila_section = self._new_section(f"{next_no}. Aquila Alignment")
        AquilaAlignmentValidator(
            root,
            metadata,
            self.task_id,
            self.access_token,
            self.task_url_template,
            prefetched_task=self.prefetched_task,
        ).validate(aquila_section)

        print_report(self.sections)
        fails = count_status(self.sections)["FAIL"]
        print(f"{'PASS' if fails == 0 else 'FAIL'} | errors={fails}")
        return 0 if fails == 0 else 1

    def _new_section(self, title: str) -> CheckSection:
        section = CheckSection(title=title)
        self.sections.append(section)
        return section

    def _resolve_root(self, section: CheckSection) -> Path | None:
        candidate = Path(self.target).expanduser()
        if not candidate.is_dir():
            if not candidate.is_absolute() and (Path.cwd() / candidate).is_dir():
                candidate = Path.cwd() / candidate
            else:
                section.add_fail(
                    f"Input directory does not exist: {self.target}", self.target
                )
                return None
        root = candidate.resolve()
        section.add_pass("Input directory is valid", ".")
        return root


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validator for task delivery packages")
    parser.add_argument("target", nargs="?", default=".", help="Target directory path")
    parser.add_argument(
        "--task-id", help="Aquila task id (optional, can come from env)"
    )
    parser.add_argument(
        "--access-token", help="Aquila bearer token (optional, can come from env)"
    )
    parser.add_argument(
        "--task-url-template",
        default=os.getenv("AQUILA_TASK_API_URL", DEFAULT_TASK_URL),
        help="Aquila task URL template",
    )
    parser.add_argument(
        "--prepare-ci",
        action="store_true",
        help="Fetch task from Aquila, clone repo, validate structure, and stage original_sessions before validation",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=Path(os.getenv("CLONE_DIR", DEFAULT_WORKSPACE_DIR)),
        help="Workspace directory for prepared task repo",
    )
    parser.add_argument(
        "--repo-dir-name",
        default=DEFAULT_REPO_DIR_NAME,
        help="Repository directory name under workspace dir",
    )
    parser.add_argument(
        "--build-dir-file",
        type=Path,
        default=Path("/tmp/build_dir"),
        help="Path to write prepared repo directory for build steps",
    )
    parser.add_argument(
        "--github-pat", help="Optional GitHub PAT for private repositories"
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    target = args.target
    prefetched_task: dict[str, Any] | None = None

    preface_sections: list[CheckSection] = []
    if args.prepare_ci:
        ctx = clone_repo_for_ci(args)
        prefetched_task = ctx.task

        preflight_section = CheckSection(
            title="1. Structure (preflight, pre-stage)"
        )
        StructureValidator(ctx.repo_dir, expect_original_sessions=False).validate(
            preflight_section
        )
        preface_sections.append(preflight_section)
        if any(item.status == "FAIL" for item in preflight_section.items):
            # Abort before staging, but still emit the preflight section so the
            # log clearly shows the structure check ran and what failed.
            print_report(preface_sections)
            return 1

        stage_sessions_into_repo(ctx)
        write_build_dir_marker(args, ctx.repo_dir)
        target = str(ctx.repo_dir)

    validator = Validator(
        target=target,
        task_id=args.task_id,
        access_token=args.access_token,
        task_url_template=args.task_url_template,
        prefetched_task=prefetched_task,
        preface_sections=preface_sections,
    )
    return validator.run(validate_structure=not args.prepare_ci)


def _safe_main(argv: list[str]) -> int:
    """Run main(); convert any unexpected error into a clean message.

    File-system paths and traceback frames are suppressed so that build logs
    do not leak repository internals or third-party library structure.
    """
    try:
        return main(argv)
    except ValidatorError as exc:
        print(f"FAIL | {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("FAIL | interrupted", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Do not print the traceback: it can contain absolute paths,
        # tokens embedded in URLs, or other sensitive context.
        print(f"FAIL | unexpected error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_safe_main(sys.argv[1:]))
