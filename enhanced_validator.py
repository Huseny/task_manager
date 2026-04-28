#!/usr/bin/env python3
"""Validator for task delivery packages.

Performs read-only checks against a delivery package and writes a markdown
report. The validator does not modify any package files.

In ``--prepare-ci`` mode the run is:
    1. Fetch the Aquila task once.
    2. Clone the GitHub repo.
    3. Pre-rearrangement validation: assert the cloned repo matches the
       ``validator.py``-style structure (no ``original_sessions/`` yet).
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
import json
import os
import re
import shutil
import string
import subprocess
import sys
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

SESSION_PROMPT_ANCHOR_LEN = 50
SESSION_PROMPT_SIMILARITY_THRESHOLD = 0.95
SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES = 20
SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES = (
    "<local-command-caveat>",
    "unknown skill: effot",
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
ALLOWED_NON_UUID_FIRST_LAYER_FILES = {"memory.jsonl"}
FORBIDDEN_KEYWORDS = (
    "clean_original_sessions.py",
    "validate_package.py",
    "check_chinese.py",
    "merge_claude_subagents_trajectory.py",
)

DEFAULT_TASK_URL = "https://api.aquila-core.net/api/v1/mindflow-tasks/{task_id}"
DEFAULT_WORKSPACE_DIR = "project_bundle"
DEFAULT_REPO_DIR_NAME = "repo"
DEFAULT_SESSIONS_DIR_NAME = "original_sessions"
TASK_ID_ENV_VARS = ("TASK_ID", "AQUILA_TASK_ID")
ACCESS_TOKEN_ENV_VARS = ("AQUILA_ACCESS_TOKEN", "ACCESS_TOKEN", "TASK_ACCESS_TOKEN")
GITHUB_PAT_ENV_VARS = ("GITHUB_PAT", "GITHUB_TOKEN")


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
        headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"},
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
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        # Avoid leaking the PAT-injected URL or stderr that may include it.
        raise ValidatorError(f"git clone failed for {repo_url} (exit code {exc.returncode})") from exc
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
        raise ValidatorError(f"Sessions archive could not be read: {exc.strerror or 'unknown error'}") from exc
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


def download_and_extract_sessions(sessions_zip_url: str, target_dir: Path, staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    archive_path = staging_dir / "sessions.zip"
    extract_root = staging_dir / "sessions_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    _download_file(sessions_zip_url, archive_path)
    _assert_zip_archive(archive_path)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            archive.extractall(extract_root)
    except zipfile.BadZipFile as exc:
        raise ValidatorError(f"Sessions archive is corrupt: {exc}") from exc
    source_root = _locate_sessions_source(extract_root)
    _copy_directory_contents(source_root, target_dir)
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
        raise ValidatorError("AQUILA_ACCESS_TOKEN is required")

    task, error = fetch_aquila_task(task_id, access_token, args.task_url_template)
    if task is None:
        raise ValidatorError(f"[{task_id}] {error}")

    try:
        repo_url = detect_repo_url(task)
    except ValueError as exc:
        raise ValidatorError(str(exc)) from exc
    extracted_task_id = sanitize_name(
        first_non_empty([task.get("mindflow_id"), task.get("task_id"), task.get("id")]) or "unknown_task"
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

    return CiContext(task_id=task_id, task=task, repo_dir=repo_dir, task_work_dir=task_work_dir)


def stage_sessions_into_repo(ctx: CiContext) -> Path:
    """Extract the sessions zip and place it at <repo>/original_sessions/."""
    try:
        sessions_zip_url = detect_sessions_zip_url(ctx.task)
    except ValueError as exc:
        raise ValidatorError(str(exc)) from exc
    sessions_dir = ctx.repo_dir / DEFAULT_SESSIONS_DIR_NAME
    download_and_extract_sessions(sessions_zip_url, sessions_dir, ctx.task_work_dir)
    print(f"[{ctx.task_id}] sessions extracted to {sessions_dir}")
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


# ---------------------------------------------------------------------------
# Text comparison helpers (prompt anchor)
# ---------------------------------------------------------------------------

def _normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace(r"\n", "").replace(r"\r", "").replace(r"\t", "")
    text = text.replace(r'\"', "").replace(r"\'", "")
    return re.sub(r"[^A-Za-z0-9一-鿿]+", "", text)


def _strip_trailing_punct(text: str | None) -> str:
    if text is None:
        return ""
    out = text.rstrip()
    trailing = string.punctuation + "，。！？；：、“”‘’（）【】《》—…"
    while out and out[-1] in trailing:
        out = out[:-1].rstrip()
    return out


def compare_by_anchor(reference_text: str, target_text: str) -> dict[str, Any]:
    head = reference_text.lstrip()[:SESSION_PROMPT_ANCHOR_LEN]
    tail = _strip_trailing_punct(reference_text)[-SESSION_PROMPT_ANCHOR_LEN:]
    if not head and not tail:
        return {"near_duplicate": False, "similarity": 0.0, "matched": False}
    if head and tail:
        pattern = rf"{re.escape(head)}(.*?){re.escape(tail)}"
    elif head:
        pattern = rf"{re.escape(head)}(.*)"
    else:
        pattern = rf"(.*?){re.escape(tail)}"
    match = re.search(pattern, target_text, flags=re.DOTALL)
    candidate = match.group(0) if match else target_text
    s1 = _normalize_for_compare(reference_text)
    s2 = _normalize_for_compare(candidate)
    similarity = 1.0 if s1 == s2 and s1 else difflib.SequenceMatcher(None, s1, s2).ratio()
    return {
        "near_duplicate": similarity >= SESSION_PROMPT_SIMILARITY_THRESHOLD,
        "similarity": similarity,
        "matched": bool(match),
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
            elif isinstance(item, dict) and str(item.get("type", "")).lower() == "text":
                out.append(str(item.get("text", "")))
        return "\n".join(out)
    if isinstance(content, dict) and str(content.get("type", "")).lower() == "text":
        return str(content.get("text", ""))
    return ""


def _is_local_command_noise(payload: dict[str, Any]) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    if str(message.get("role", "")).lower() != "user":
        return False
    return _extract_text_content(message).strip().startswith(LOCAL_COMMAND_NOISE_PREFIXES)


def analyze_jsonl(path: Path) -> JsonlAnalysis:
    text = read_text(path)
    analysis = JsonlAnalysis(
        readable=text is not None,
        keyword_line_numbers={keyword: [] for keyword in FORBIDDEN_KEYWORDS},
    )
    if text is None:
        return analysis

    for line_no, raw in enumerate(text.splitlines(), start=1):
        lower = raw.lower()
        for keyword in FORBIDDEN_KEYWORDS:
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

        timestamp_raw = payload.get("timestamp") if isinstance(payload.get("timestamp"), str) else None
        parsed_ts = parse_iso_timestamp(timestamp_raw)
        if timestamp_raw and analysis.first_timestamp_raw is None:
            analysis.first_timestamp_raw = timestamp_raw
            analysis.first_timestamp_line = line_no
        if parsed_ts is not None and (analysis.latest_timestamp_dt is None or parsed_ts > analysis.latest_timestamp_dt):
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
            analysis.last_semantic_has_assistant_text = (
                role == "assistant" and bool(_extract_text_content(message).strip())
            )

        if role == "user":
            analysis.user_line_count += 1
            content_text = _extract_text_content(message)
            if not content_text.strip():
                continue
            if is_noise or content_text.lstrip().lower().startswith(SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES):
                analysis.exempt_user_line_count += 1
                continue
            if timestamp_raw and parsed_ts is not None:
                analysis.user_candidates.append((content_text, line_no, timestamp_raw, parsed_ts))

    return analysis


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
        "docs", "repo", ".tmp", "metadata.json",
        ".git", ".gitignore", ".github",
    }
    TEST_SCRIPT_OPTIONS = ("repo/run_test.sh", "repo/run_tests.sh")

    def __init__(self, root: Path, expect_original_sessions: bool = True) -> None:
        super().__init__(root)
        self.expect_original_sessions = expect_original_sessions
        if expect_original_sessions:
            self.mandatory_dirs = (*self.BASE_MANDATORY_DIRS, ORIGINAL_SESSIONS_DIR)
            self.allowed_root_entries = self.BASE_ALLOWED_ROOT_ENTRIES | {ORIGINAL_SESSIONS_DIR}
        else:
            self.mandatory_dirs = self.BASE_MANDATORY_DIRS
            self.allowed_root_entries = self.BASE_ALLOWED_ROOT_ENTRIES

    TMP_AUDIT_RE = re.compile(r"^audit_report-(\d+)\.md$")
    TMP_FIX_RE = re.compile(r"^audit_report-(\d+)-fix_check\.md$")
    TMP_REQUIRED_EXTRA = "test_coverage_and_readme_audit_report.md"

    TEMP_EXACT_NAMES = {
        "tmp", "temp", "temporary", ".temp",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    }
    TEMP_SUFFIXES = (".temp", ".swp", ".swo", ".bak", ".old", ".orig", ".rej")

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
            if entry.name not in self.allowed_root_entries and entry.name != "validation_report.md":
                kind = "directory" if entry.is_dir() else "file"
                section.add_fail(f"Unexpected {kind} in root: {entry.name}", self._rel(entry))
                unexpected_root += 1
        if unexpected_root == 0:
            section.add_pass("No unexpected entries at repo root", ".")

        docs_dir = self.root / "docs"
        if docs_dir.is_dir():
            invalid_docs = 0
            for entry in sorted(docs_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() or entry.suffix.lower() != ".md":
                    kind = "directory" if entry.is_dir() else "file"
                    section.add_fail(f"Invalid {kind} in docs/: {entry.name}", self._rel(entry))
                    invalid_docs += 1
            if invalid_docs == 0:
                section.add_pass("docs/ contains only .md files", self._rel(docs_dir))

        self._check_tmp_layout(section)

        found_test = next((opt for opt in self.TEST_SCRIPT_OPTIONS if (self.root / opt).is_file()), None)
        if found_test:
            section.add_pass(f"Test script found: {found_test}", found_test)
        else:
            section.add_fail("Missing test script (run_test.sh or run_tests.sh)", "repo/")

        temp_findings = 0
        for entry in self.root.rglob("*"):
            if self._is_temp_entry(entry):
                kind = "directory" if entry.is_dir() else "file"
                section.add_fail(
                    f"Temporary {kind} found: {entry.relative_to(self.root)}",
                    self._rel(entry),
                )
                temp_findings += 1
        if temp_findings == 0:
            section.add_pass("No stray temporary files or caches found", ".")

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

    def _check_tmp_layout(self, section: CheckSection) -> None:
        tmp_dir = self.root / ".tmp"
        if not tmp_dir.is_dir():
            return

        entries = list(tmp_dir.iterdir())
        files = [e for e in entries if e.is_file()]
        dirs = [e for e in entries if e.is_dir()]

        layout_failures = 0
        for entry in dirs:
            section.add_fail(f"Invalid directory in .tmp/: {entry.name}", self._rel(entry))
            layout_failures += 1

        audit_nums: list[str] = []
        fix_nums: list[str] = []
        report_files: list[str] = []
        required_extra_count = 0

        for file in files:
            name = file.name
            if (m := self.TMP_FIX_RE.match(name)):
                fix_nums.append(m.group(1))
                report_files.append(name)
            elif (m := self.TMP_AUDIT_RE.match(name)):
                audit_nums.append(m.group(1))
                report_files.append(name)
            elif name == self.TMP_REQUIRED_EXTRA:
                required_extra_count += 1
            else:
                section.add_fail(f"Invalid file in .tmp/: {name}", self._rel(file))
                layout_failures += 1

        rel_tmp = self._rel(tmp_dir)
        if required_extra_count != 1:
            section.add_fail(
                "Missing or duplicate test_coverage_and_readme_audit_report.md in .tmp/", rel_tmp,
            )
            layout_failures += 1
        if len(report_files) != 4:
            section.add_fail(
                f".tmp must contain exactly 4 audit/fix-check files, found {len(report_files)}", rel_tmp,
            )
            layout_failures += 1
        if len(files) != 5:
            section.add_fail(f".tmp must contain exactly 5 files, found {len(files)}", rel_tmp)
            layout_failures += 1

        if len(fix_nums) not in {1, 2}:
            section.add_fail(f"Expected 1 or 2 fix-check reports, found {len(fix_nums)}", rel_tmp)
            layout_failures += 1
        elif len(fix_nums) == 2 and (len(audit_nums) != 2 or sorted(audit_nums) != sorted(fix_nums)):
            section.add_fail(
                "When there are 2 fix-check reports, audit and fix-check numbers must match", rel_tmp,
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
            section.add_pass(".tmp/ layout is valid (5 files, audit/fix-check pairing)", rel_tmp)


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
            section.add_fail(f"Missing required metadata fields: {', '.join(missing)}", rel)
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

        return parsed


class SessionsValidator(SectionValidator):
    EXEMPT_BUNDLE_DIRS = {"tool-results", "memory"}

    def __init__(self, root: Path, metadata: dict[str, Any]) -> None:
        super().__init__(root)
        self.metadata = metadata

    def validate(self, section: CheckSection) -> None:
        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR
        if not sessions_dir.is_dir():
            section.add_fail("Missing original_sessions/ directory", self._rel(sessions_dir))
            return

        self._check_root_legacy_sessions(section)

        if not list(sessions_dir.iterdir()):
            section.add_fail("original_sessions/ is empty", self._rel(sessions_dir))
            return

        self._check_session_naming(section, sessions_dir)
        self._check_bundle_structure(section, sessions_dir)
        self._check_prompt_anchor(section, sessions_dir)
        self._check_latest_trajectory(section, sessions_dir)
        self._check_forbidden_keywords(section, sessions_dir)

    def _check_session_naming(self, section: CheckSection, sessions_dir: Path) -> None:
        failures = 0
        for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir():
                if entry.name.lower() in self.EXEMPT_BUNDLE_DIRS:
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
            if entry.name in ALLOWED_NON_UUID_FIRST_LAYER_FILES:
                continue
            if entry.suffix.lower() != ".jsonl":
                failures += 1
                section.add_fail(
                    f"Session file must be .jsonl: {entry.name}", self._rel(entry),
                )
                continue
            if not SESSION_UUID_RE.match(entry.stem):
                failures += 1
                section.add_fail(
                    f"Session file stem is not a UUID: {entry.name}", self._rel(entry),
                )
        if failures == 0:
            section.add_pass(
                "Session file/directory names are valid UUIDs with .jsonl extension",
                self._rel(sessions_dir),
            )

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
            section.add_pass(
                "No legacy session JSON files at repo root", "."
            )

    def _check_bundle_structure(self, section: CheckSection, sessions_dir: Path) -> None:
        child_dirs = sorted([p for p in sessions_dir.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
        session_dirs = [p for p in child_dirs if p.name.lower() not in self.EXEMPT_BUNDLE_DIRS]

        if not session_dirs:
            section.add_warn("No session_id directories were found under original_sessions/", self._rel(sessions_dir))
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
            for jsonl_file in sorted(subagents_dir.glob("*.jsonl"), key=lambda p: p.name.lower()):
                stem = jsonl_file.stem.lower()
                if not stem.startswith("agent-") or stem.startswith("agent-aside_question") or "acompact" in stem:
                    continue
                meta = subagents_dir / f"{jsonl_file.stem}.meta.json"
                if not meta.is_file():
                    failures += 1
                    section.add_fail(f"Missing meta file {meta.name} for {jsonl_file.name}", self._rel(jsonl_file))

        if failures == 0:
            section.add_pass("Session directory structure check passed", self._rel(sessions_dir))

    @staticmethod
    def _first_layer_jsonl(sessions_dir: Path) -> list[Path]:
        return sorted(
            [p for p in sessions_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jsonl"],
            key=lambda p: p.name.lower(),
        )

    def _check_latest_trajectory(self, section: CheckSection, sessions_dir: Path) -> None:
        candidates: list[tuple[datetime, str, int, Path]] = []
        for jsonl_path in self._first_layer_jsonl(sessions_dir):
            if jsonl_path.name.lower() == "memory.jsonl":
                continue
            analysis = analyze_jsonl(jsonl_path)
            if analysis.latest_timestamp_dt and analysis.latest_timestamp_raw and analysis.latest_timestamp_line:
                candidates.append(
                    (analysis.latest_timestamp_dt, analysis.latest_timestamp_raw, analysis.latest_timestamp_line, jsonl_path)
                )

        if not candidates:
            section.add_warn(
                "Skipped latest trajectory completeness check: no usable timestamp in first-layer jsonl files",
                self._rel(sessions_dir),
            )
            return

        candidates.sort(key=lambda item: (item[0], item[3].as_posix().lower()), reverse=True)
        _, latest_raw, latest_line_no, latest_jsonl = candidates[0]
        analysis = analyze_jsonl(latest_jsonl)

        rel = self._rel(latest_jsonl)
        if analysis.last_semantic_line is None:
            section.add_fail("No semantic message event found in latest trajectory file", rel)
            return
        if (analysis.last_semantic_role or "") != "assistant":
            section.add_fail(
                f"Last semantic message role is not assistant (line {analysis.last_semantic_line}, role={analysis.last_semantic_role or 'unknown'})",
                rel,
            )
            return
        if not analysis.last_semantic_has_assistant_text:
            section.add_fail(
                f"Last semantic assistant message has no text content (line {analysis.last_semantic_line})", rel,
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

        timestamp_candidates: list[tuple[tuple[int, str], Path, str, int]] = []
        for path in self._first_layer_jsonl(sessions_dir):
            analysis = analyze_jsonl(path)
            if analysis.first_timestamp_raw is None or analysis.first_timestamp_line is None:
                continue
            ts = parse_iso_timestamp(analysis.first_timestamp_raw)
            order_key = (0, ts.isoformat()) if ts else (1, analysis.first_timestamp_raw)
            timestamp_candidates.append((order_key, path, analysis.first_timestamp_raw, analysis.first_timestamp_line))

        if not timestamp_candidates:
            section.add_warn(
                "Skipped session prompt-anchor check: no usable timestamp in first-layer jsonl files",
                self._rel(sessions_dir),
            )
            return

        timestamp_candidates.sort(key=lambda item: (item[0], item[1].as_posix().lower()))
        for _, selected_jsonl, first_ts, first_line in timestamp_candidates:
            analysis = analyze_jsonl(selected_jsonl)
            window_candidates: list[tuple[str, int, str]] = []
            window_start: datetime | None = None
            window_end: datetime | None = None
            for content_text, line_no, ts_raw, parsed_ts in analysis.user_candidates:
                if window_start is None:
                    window_start = parsed_ts
                    window_end = parsed_ts + timedelta(minutes=SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES)
                    window_candidates.append((content_text, line_no, ts_raw))
                    continue
                if window_end is not None and window_start <= parsed_ts <= window_end:
                    window_candidates.append((content_text, line_no, ts_raw))

            if not window_candidates:
                continue

            best_similarity = -1.0
            best_line = -1
            for content_text, line_no, _ in window_candidates:
                compare = compare_by_anchor(prompt_text, content_text)
                similarity = float(compare["similarity"])
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_line = line_no
                if bool(compare["near_duplicate"]):
                    section.add_pass(
                        (
                            "Session prompt-anchor check passed "
                            f"(file={selected_jsonl.name}, file_first_timestamp={first_ts}, file_first_timestamp_line={first_line}, "
                            f"match_line={line_no}, similarity={similarity:.6f})"
                        ),
                        self._rel(selected_jsonl),
                    )
                    return

            section.add_fail(
                (
                    "Session prompt-anchor check failed due to low similarity "
                    f"(file={selected_jsonl.name}, file_first_timestamp={first_ts}, best_line={best_line}, "
                    f"best_similarity={best_similarity:.6f}, threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f})"
                ),
                self._rel(selected_jsonl),
            )
            return

        section.add_warn(
            "Skipped session prompt-anchor check: no comparable user content found",
            self._rel(sessions_dir),
        )

    def _check_forbidden_keywords(self, section: CheckSection, sessions_dir: Path) -> None:
        jsonl_files = sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: p.as_posix().lower())
        if not jsonl_files:
            section.add_warn(
                "No jsonl files found under original_sessions; forbidden keyword check skipped",
                self._rel(sessions_dir),
            )
            return

        findings = 0
        for path in jsonl_files:
            analysis = analyze_jsonl(path)
            if not analysis.readable:
                section.add_warn("jsonl file is unreadable; skipped forbidden keyword check", self._rel(path))
                continue
            for keyword in FORBIDDEN_KEYWORDS:
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
                "No forbidden keywords detected in original_sessions/*.jsonl", self._rel(sessions_dir),
            )

    @staticmethod
    def _format_lines(line_numbers: list[int]) -> str:
        if not line_numbers:
            return ""
        if len(line_numbers) <= 20:
            return ", ".join(str(n) for n in line_numbers)
        return ", ".join(str(n) for n in line_numbers[:20]) + f" ... total {len(line_numbers)} lines"


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

    def _check_scalar(self, section: CheckSection, task: dict[str, Any], field_name: str) -> None:
        metadata_value = normalize_aquila_value(self.metadata.get(field_name)).lower()
        aquila_value = normalize_aquila_value(task.get(field_name)).lower()
        if metadata_value == aquila_value:
            section.add_pass(
                f"metadata.{field_name} matches Aquila {field_name} ({metadata_value})", "metadata.json",
            )
        else:
            section.add_fail(
                f"metadata.{field_name} mismatch: metadata={metadata_value or 'empty'}, aquila={aquila_value}",
                "metadata.json",
            )


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def render_report(sections: list[CheckSection]) -> str:
    lines = ["# Validation Report", ""]
    for section in sections:
        lines.append(f"## {section.title}")
        if not section.items:
            lines.append("- [PASS] No checks")
        else:
            for item in section.items:
                lines.append(f"- [{item.status}] {item.message} ({item.rel_path})")
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
        initial_sections: list[CheckSection] | None = None,
    ) -> None:
        self.target = target
        self.task_id = task_id
        self.access_token = access_token
        self.task_url_template = task_url_template
        self.prefetched_task = prefetched_task
        self.sections: list[CheckSection] = list(initial_sections or [])

    def run(self) -> int:
        input_section = self._new_section("1. Input Directory")
        root = self._resolve_root(input_section)
        if root is None:
            print_report(self.sections)
            return 1

        structure_section = self._new_section("2. Structure")
        StructureValidator(root, expect_original_sessions=True).validate(structure_section)

        metadata_section = self._new_section("3. metadata.json")
        metadata = MetadataValidator(root).validate(metadata_section)

        sessions_section = self._new_section("4. original_sessions")
        SessionsValidator(root, metadata).validate(sessions_section)

        aquila_section = self._new_section("5. Aquila Alignment")
        AquilaAlignmentValidator(
            root, metadata, self.task_id, self.access_token, self.task_url_template,
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
                section.add_fail(f"Input directory does not exist: {self.target}", self.target)
                return None
        root = candidate.resolve()
        try:
            rel = root.relative_to(root).as_posix() or "."
        except ValueError:
            rel = root.as_posix()
        section.add_pass("Input directory is valid", rel)
        return root


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validator for task delivery packages")
    parser.add_argument("target", nargs="?", default=".", help="Target directory path")
    parser.add_argument("--task-id", help="Aquila task id (optional, can come from env)")
    parser.add_argument("--access-token", help="Aquila bearer token (optional, can come from env)")
    parser.add_argument(
        "--task-url-template",
        default=os.getenv("AQUILA_TASK_API_URL", DEFAULT_TASK_URL),
        help="Aquila task URL template",
    )
    parser.add_argument(
        "--prepare-ci",
        action="store_true",
        help="Fetch task from Aquila, clone repo, and stage original_sessions before validation",
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
    parser.add_argument("--github-pat", help="Optional GitHub PAT for private repositories")
    return parser.parse_args(argv)


def run_pre_rearrangement_checks(repo_dir: Path) -> tuple[list[CheckSection], int]:
    """Validate the freshly cloned repo before original_sessions/ is placed.

    Mirrors the structural and metadata checks from validator.py: there is
    no original_sessions/ at this point and any presence would be unexpected.
    """
    structure = CheckSection(title="0a. Cloned Repo Structure (pre-rearrangement)")
    StructureValidator(repo_dir, expect_original_sessions=False).validate(structure)
    metadata = CheckSection(title="0b. Cloned Repo metadata.json (pre-rearrangement)")
    MetadataValidator(repo_dir).validate(metadata)
    sections = [structure, metadata]
    fails = sum(1 for s in sections for it in s.items if it.status == "FAIL")
    return sections, fails


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    target = args.target
    prefetched_task: dict[str, Any] | None = None
    initial_sections: list[CheckSection] = []

    if args.prepare_ci:
        ctx = clone_repo_for_ci(args)
        prefetched_task = ctx.task

        initial_sections, pre_fails = run_pre_rearrangement_checks(ctx.repo_dir)
        if pre_fails:
            print_report(initial_sections)
            write_build_dir_marker(args, ctx.repo_dir)
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
        initial_sections=initial_sections,
    )
    return validator.run()


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
