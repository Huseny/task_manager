#!/usr/bin/env python3
"""Static delivery package validator.

Usage:
  python validate_package_direct_original_sessions.py /path/to/TASK-001
  python validate_package_direct_original_sessions.py TASK-001
"""

from __future__ import annotations

import argparse
import difflib
import errno
import fnmatch
import json
import os
import re
import shutil
import string
import subprocess
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

PROJECT_TYPE_ALIASES = {
    "fullstack": {
        "fullstack",
        "full_stack",
        "full-stack",
        "full stack",
        "fullstack_app",
        "full-stack-app",
        "full_stack_app",
    },
    "pure_backend": {
        "pure_backend",
        "pure-backend",
        "pure backend",
        "purebackend",
        "backend_only",
        "backend-only",
        "backend only",
    },
    "pure_frontend": {
        "pure_frontend",
        "pure-frontend",
        "pure frontend",
        "purefrontend",
        "frontend_only",
        "frontend-only",
        "frontend only",
    },
    "cross_platform_app": {
        "cross_platform_app",
        "cross-platform-app",
        "cross platform app",
        "crossplatformapp",
        "cross_platform",
        "cross-platform",
        "cross platform",
        "crossplatform",
        "multi_platform_app",
        "multiplatform_app",
        "multi-platform-app",
        "multiplatform",
    },
    "mobile_app": {
        "mobile_app",
        "mobile-app",
        "mobile app",
        "mobileapp",
        "mobile",
        "app_mobile",
        "app-mobile",
        "app mobile",
    },
}


def _normalize_project_type_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _build_project_type_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in PROJECT_TYPE_ALIASES.items():
        all_tokens = set(aliases)
        all_tokens.add(canonical)
        for token in all_tokens:
            normalized = _normalize_project_type_token(token)
            existing = lookup.get(normalized)
            if existing is not None and existing != canonical:
                raise ValueError(
                    f"project type alias conflict: '{token}' -> {canonical}, already mapped to {existing}"
                )
            lookup[normalized] = canonical
    return lookup


PROJECT_TYPE_LOOKUP = _build_project_type_lookup()
BACKEND_KEYWORDS = ("backend", "server", "api", "service")
BACKEND_MARKER_FILES = {
    "requirements.txt",
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
    "go.mod",
    "composer.json",
    "cargo.toml",
}

CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
TRAJECTORY_MULTI_RE = re.compile(r"trajectory[-_]\d+\.json$", re.IGNORECASE)
LEGACY_SESSION_JSON_RE = re.compile(r"(trajectory(?:[-_]\d+)?|develop(?:[-_]\d+)?|bugfix(?:[-_]\d+)?)\.json$", re.IGNORECASE)
PROMPT_ENGLISH_RATIO_THRESHOLD = 0.70
SESSION_PROMPT_ANCHOR_LEN = 50
SESSION_PROMPT_SIMILARITY_THRESHOLD = 0.95
SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES = 20
SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES = (
    "<local-command-caveat>",
    "unknown skill: effot",
)
SUDO_RETRY_TIMEOUT_SECONDS = 300
ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS = (
    "clean_original_sessions.py",
    "validate_package.py",
    "check_chinese.py",
    "merge_claude_subagents_trajectory.py",
)
TOKEN_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
TOKEN_PRICE_INPUT_PER_M_USD = 5.0
TOKEN_PRICE_OUTPUT_PER_M_USD = 25.0
TOKEN_PRICE_CACHE_READ_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 0.1
TOKEN_PRICE_CACHE_WRITE_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 1.25
TOKEN_COST_THRESHOLD_SERVER_WEB_USD = 15.0
TOKEN_COST_THRESHOLD_FULLSTACK_USD = 30.0
TOKEN_COST_THRESHOLD_DEFAULT_USD = 20.0

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
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".o",
    ".obj",
    ".a",
}

ROOT_ALLOWED_FILES = {
    "metadata.json",
    ".gitignore",
}

ROOT_REQUIRED_FILES = (
    "metadata.json",
)

ROOT_REQUIRED_FILE_TYPO_ALIASES = {
    "prompt.md": {
        "prompts.md",
        "prompt.mdown",
        "prompts.mdown",
        "prompt.markdown",
        "promts.md",
        "promot.md",
    },
    "questions.md": {
        "question.md",
        "questions.mdown",
        "question.markdown",
        "quesitons.md",
        "questionn.md",
        "questionss.md",
    },
    "metadata.json": {
        "metadatas.json",
        "meta-data.json",
        "meta.json",
        "metainfo.json",
    },
    "trajectory.json": {
        "session.json",
        "sessions.json",
        "trajectorys.json",
        "trajectories.json",
        "trajactory.json",
        "trajectroy.json",
    },
}

ROOT_REQUIRED_DIR_TYPO_ALIASES = {
    "original_sessions": {
        "origin_sessions",
        "sessions",
        "session",
        "sesions",
        "sessionss",
        "originsessions",
        "origin-session",
        "origin_session",
        "origin-sessions",
        "origin session",
        "originalsessions",
        "original-session",
        "original_session",
        "original-sessions",
        "original session",
    }
}

ROOT_COMMON_FILE_TYPOS = {
    "prompts.md": "prompt.md",
    "prompts.mdown": "prompt.md",
    "prompt.markdown": "prompt.md",
    "question.md": "questions.md",
    "questions.mdown": "questions.md",
    "question.markdown": "questions.md",
    "metadatas.json": "metadata.json",
    "meta-data.json": "metadata.json",
    "meta.json": "metadata.json",
}

DOC_REQUIRED_FILE_TYPO_ALIASES = {
    "questions.md": {
        "question.md",
        "questions.mdown",
        "question.markdown",
        "quesitons.md",
        "questionn.md",
        "questionss.md",
    },
    "design.md": {
        "desgin.md",
        "design.mdown",
        "design.markdown",
        "design-doc.md",
        "design_doc.md",
    },
    "api-spec.md": {
        "api_spec.md",
        "apispec.md",
        "api-specification.md",
        "api-specs.md",
        "api.md",
    },
}

REPO_DIR_NAME = "repo"
ORIGINAL_SESSIONS_DIR_NAME = "original_sessions"
SESSION_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz"}
LEADING_BUNDLE_EXEMPT_DIR_NAMES = {"tool-results", "memory"}
LEADING_SESSION_ZIP_LINUX_PREFIX = "-"
LEADING_SESSION_ZIP_WINDOWS_PREFIX = "c--"
METADATA_REQUIRED_KEYS = (
    "prompt",
    "project_type",
    "frontend_language",
    "backend_language",
    "frontend_framework",
    "backend_framework",
    "database",
)
METADATA_PROJECT_TYPE_ENUM = ("web", "server", "fullstack", "android", "ios", "desktop")
METADATA_PROJECT_TYPE_SET = set(METADATA_PROJECT_TYPE_ENUM)
ROOT_STANDARD_DIR_NAMES = {"docs", ORIGINAL_SESSIONS_DIR_NAME, REPO_DIR_NAME, ".tmp", ".backup", ".git"}

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

RUNTIME_NOISE_FILE_NAMES = {
    ".coverage",
    "coverage.out",
    "cmakecache.txt",
    "cmake_install.cmake",
    "compile_commands.json",
    ".flutter-plugins",
    ".flutter-plugins-dependencies",
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

PROJECT_TYPE_MISNAME_HINTS = {
    "backend": "pure_backend",
    "server": "pure_backend",
    "api": "pure_backend",
    "service": "pure_backend",
    "frontend": "pure_frontend",
    "web": "pure_frontend",
    "client": "pure_frontend",
    "crossplatform": "cross_platform_app",
    "crossplatformapp": "cross_platform_app",
    "mobile": "mobile_app",
    "appmobile": "mobile_app",
}

ENGLISH_CHECK_EXCLUDED_DIRS = {
    ".tmp",
    ".backup",
    ".git",
}

ENGLISH_CHECK_EXCLUDED_FILES = {
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
}

ROOT_REPAIR_EXEMPT_ARCHIVE_EXTS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
}

REPAIR_GITIGNORE_HEADER = "# Added by validate_package --repair (exemptions)"
REPAIR_GITIGNORE_EXEMPT_PATTERNS = [
    ".tmp/",
    ".backup/",
    "*.rar",
    "*.7z",
    "*.tar",
    "*.gz",
]

UNIVERSAL_GITIGNORE_PATTERNS = [
    ".vscode/",
    ".idea/",
    ".codex/",
    ".opencode/",
]

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

DIR_VIOLATION_REASONS = {
    ".vscode": "is local IDE config directory",
    ".idea": "is local IDE config directory",
    ".codex": "is local tool directory",
    ".opencode": "is local tool directory",
    "__pycache__": "is Python cache directory",
    "__pychache__": "is Python cache directory (suspected __pycache__ typo)",
    ".pytest_cache": "is Python cache directory",
    ".venv": "is Python virtual environment directory",
    "venv": "is Python virtual environment directory",
    ".mypy_cache": "is Python cache directory",
    ".ruff_cache": "is Python cache directory",
    "htmlcov": "is Python coverage directory",
    "node_modules": "located in Node dependencies directory",
    ".npm": "is Node local cache directory",
    ".pnpm-store": "is Node local cache directory",
    ".yarn": "is Node local cache directory",
    ".next": "is Node build directory",
    "coverage": "is coverage artifact directory",
    "dist": "is build artifact directory",
    "build": "is build artifact directory",
    "target": "is build artifact directory",
    ".gradle": "is Gradle local directory",
    ".kotlin": "is Kotlin local directory",
    "out": "is build artifact directory",
    "bin": "is build artifact directory",
    "obj": "is build artifact directory",
    "debug": "is build artifact directory",
    "release": "is build artifact directory",
    ".vs": "is .NET local directory",
    "testresults": "is test results directory",
    "cmakefiles": "is C/C++ build directory",
    ".dart_tool": "is Dart/Flutter local cache directory",
    ".bundle": "is Ruby local dependency directory",
    "vendor": "is dependency directory",
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

COMPILE_EXEMPT_FILE_NAMES = {
    "cmakecache.txt",
    "cmake_install.cmake",
    "compile_commands.json",
}

FILE_VIOLATION_RULES = [
    (lambda p: p.suffix.lower() == ".pyc", "is Python cache file"),
    (lambda p: p.name.lower() == ".coverage", "is coverage file"),
    (lambda p: p.name.lower() == "coverage.out", "is coverage file"),
    (lambda p: p.suffix.lower() == ".test", "is test binary file"),
    (lambda p: p.suffix.lower() in {".class", ".jar", ".war"}, "is Java/Kotlin build artifact"),
    (
        lambda p: p.name.lower() in {"cmakecache.txt", "cmake_install.cmake", "compile_commands.json"},
        "is C/C++ build file",
    ),
    (lambda p: p.suffix.lower() in {".o", ".obj", ".exe", ".pdb"}, "is binary/build artifact file"),
    (
        lambda p: p.name.lower() in {".flutter-plugins", ".flutter-plugins-dependencies"},
        "is Flutter local tool file",
    ),
    (
        lambda p: p.as_posix().lower().endswith("android/local.properties"),
        "is Android local config file",
    ),
    (lambda p: p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}, "is local database file"),
    (lambda p: p.suffix.lower() == ".tsbuildinfo", "is TypeScript build cache file"),
    (lambda p: p.name.lower() == "session.json", "is non-deliverable file (session.json)"),
    (
        lambda p: re.fullmatch(r"rollout-.*\.jsonl", p.name.lower()) is not None,
        "is non-deliverable file (rollout-*.jsonl)",
    ),
]


@dataclass
class CheckItem:
    status: str
    message: str
    rel_path: str


@dataclass
class CheckSection:
    title: str
    items: list[CheckItem] = field(default_factory=list)

    def add_pass(self, message: str, rel_path: str) -> None:
        self.items.append(CheckItem("PASS", message, rel_path))

    def add_fail(self, message: str, rel_path: str) -> None:
        self.items.append(CheckItem("FAIL", message, rel_path))

    def add_warn(self, message: str, rel_path: str) -> None:
        self.items.append(CheckItem("WARN", message, rel_path))


@dataclass
class RepairAction:
    kind: str
    src: Path
    dst: Path | None
    reason: str
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class JsonlAnalysis:
    path: Path
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
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0


@dataclass
class JsonlUsageAnalysis:
    path: Path
    readable: bool
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0
    keyword_line_numbers: dict[str, list[int]] = field(default_factory=dict)


class PackageValidator:
    def __init__(self, input_identifier: str) -> None:
        self.input_identifier = input_identifier
        self.root: Path | None = None
        self.report_path: Path | None = None

        self.sections: list[CheckSection] = []
        self.error_count = 0

        self.project_type_name: str | None = None
        self.project_type_dir: Path | None = None
        self.metadata: dict[str, object] = {}
        self.metadata_source_path: Path | None = None
        self.legacy_project_dirs: list[tuple[str, Path]] = []

        self.backend_content: bool | None = None
        self.backend_reason: str = ""

        self.english_mode: bool = False
        self.languages: set[str] = set()
        self._gitignore_scopes_cache: list[tuple[Path, list[str]]] | None = None
        self._candidate_entries_cache: list[tuple[Path, bool, bool, str]] | None = None
        self._dirty_findings_cache: list[tuple[Path, str, str]] | None = None
        self._jsonl_analysis_cache: dict[str, JsonlAnalysis] = {}
        self._jsonl_usage_analysis_cache: dict[str, JsonlUsageAnalysis] = {}
        self._session_extracted_dirs_for_cleanup: dict[str, Path] = {}
        self._session_skip_cleanup_dirs: dict[str, Path] = {}

    def _reset_run_state(self) -> None:
        self.sections = []
        self.error_count = 0
        self.project_type_name = None
        self.project_type_dir = None
        self.metadata = {}
        self.metadata_source_path = None
        self.legacy_project_dirs = []
        self.backend_content = None
        self.backend_reason = ""
        self.english_mode = False
        self.languages = set()
        self._gitignore_scopes_cache = None
        self._candidate_entries_cache = None
        self._dirty_findings_cache = None
        self._jsonl_analysis_cache = {}
        self._jsonl_usage_analysis_cache = {}
        self._session_extracted_dirs_for_cleanup = {}
        self._session_skip_cleanup_dirs = {}

    def run(self) -> tuple[bool, int, Path]:
        self._reset_run_state()
        self._check_input_directory()
        if self.root is None:
            fallback_report = Path.cwd() / ".tmp" / "validation_report.md"
            self.report_path = fallback_report
            self._write_report()
            return False, self.error_count, fallback_report

        self.report_path = self.root / ".tmp" / "validation_report.md"

        self._check_root_fixed_files()
        self._check_repo_directory()
        self._check_trajectory_organization()
        self._check_metadata_file()
        self._check_docs_directory()
        self._check_metadata_prompt_english_mode()
        self._check_english_consistency()
        self._check_backend_content_recognition()
        self._check_backend_project_requirements()

        self._check_gitignore_exists()
        self._detect_languages()
        self._check_gitignore_coverage()
        self._check_local_dirty_files()

        self._write_report()
        self._cleanup_extracted_session_dirs_after_report()
        return self.error_count == 0, self.error_count, self.report_path

    def _mark_session_skip_cleanup_dir(self, path: Path) -> None:
        key = path.as_posix().lower()
        self._session_skip_cleanup_dirs[key] = path
        self._session_extracted_dirs_for_cleanup.pop(key, None)

    def _mark_session_extracted_cleanup_dir(self, path: Path) -> None:
        key = path.as_posix().lower()
        if key in self._session_skip_cleanup_dirs:
            return
        self._session_extracted_dirs_for_cleanup[key] = path

    def _cleanup_extracted_session_dirs_after_report(self) -> None:
        if not self._session_extracted_dirs_for_cleanup:
            if self._session_skip_cleanup_dirs:
                print("POST-REPORT-CLEANUP | No directory deletion needed (existing directories with same name skipped decompression and deletion)")
            return

        done = 0
        skipped = 0
        failed = 0

        print("POST-REPORT-CLEANUP | Starting cleanup of decompressed directories (only newly decompressed dirs)")
        for path in sorted(self._session_extracted_dirs_for_cleanup.values(), key=lambda p: p.as_posix().lower()):
            key = path.as_posix().lower()
            if key in self._session_skip_cleanup_dirs:
                print(f"[SKIP] CLEANUP_DIR {self._rel(path)} | Marked as existing directory with same name, skipping deletion")
                skipped += 1
                continue
            if not path.exists():
                print(f"[SKIP] CLEANUP_DIR {self._rel(path)} | Directory no longer exists")
                skipped += 1
                continue
            if not path.is_dir():
                print(f"[FAIL] CLEANUP_DIR {self._rel(path)} | Target is not a directory")
                failed += 1
                continue
            try:
                shutil.rmtree(path)
                print(f"[DONE] CLEANUP_DIR {self._rel(path)}")
                done += 1
            except OSError as exc:
                print(f"[FAIL] CLEANUP_DIR {self._rel(path)} | {exc}")
                failed += 1

        print(f"POST-REPORT-CLEANUP | done={done} skipped={skipped} failed={failed}")

    def run_repair(self) -> tuple[int, int, int, Path | None]:
        if self.root is None:
            print("REPAIR | Invalid input directory, skipping repair")
            return 0, 0, 0, None

        print("REPAIR | Generating repair plan (might take time for large directories)...", flush=True)
        actions = self._plan_repair_actions()
        print(f"REPAIR | Repair plan generated, total {len(actions)} items", flush=True)
        if not actions:
            print("REPAIR | No executable repair operations")
            return 0, 0, 0, None

        print("REPAIR | Proposed operations (report already generated):")
        self._print_repair_plan(actions)

        try:
            confirmation = input("Confirm to execute above repair operations? Enter YES to continue, any other input to cancel: ").strip()
        except EOFError:
            confirmation = ""

        if confirmation.upper() != "YES":
            print("REPAIR | Cancelled, no files modified")
            return 0, len(actions), 0, None

        executed, skipped, failed, backup_dir = self._execute_repair_actions(actions)
        if backup_dir is None:
            print(f"REPAIR | executed={executed} skipped={skipped} failed={failed} | No backup generated (no deletion operations)")
        else:
            print(
                f"REPAIR | executed={executed} skipped={skipped} failed={failed} | backup={self._rel(backup_dir)}"
            )
        return executed, skipped, failed, backup_dir

    def run_convert_legacy(self) -> tuple[int, int, int, Path | None]:
        if self.root is None:
            print("CONVERT | Invalid input directory, skipping conversion")
            return 0, 0, 0, None

        print("CONVERT | Generating old structure migration plan...", flush=True)
        actions = self._plan_convert_legacy_actions()
        print(f"CONVERT | Migration plan generated, total {len(actions)} items", flush=True)
        if not actions:
            print("CONVERT | No migration needed (no convertible old structure detected)")
            return 0, 0, 0, None

        print("CONVERT | Proposed operations:")
        self._print_repair_plan(actions)

        try:
            confirmation = input("Confirm to execute above migration operations? Enter YES to continue, any other input to cancel: ").strip()
        except EOFError:
            confirmation = ""

        if confirmation.upper() != "YES":
            print("CONVERT | Cancelled, no files modified")
            return 0, len(actions), 0, None

        executed, skipped, failed, backup_dir = self._execute_repair_actions(actions, backup_on_move=True)
        if backup_dir is None:
            print(f"CONVERT | executed={executed} skipped={skipped} failed={failed} | No backup generated")
        else:
            print(
                f"CONVERT | executed={executed} skipped={skipped} failed={failed} | backup={self._rel(backup_dir)}"
            )
        return executed, skipped, failed, backup_dir

    def _plan_repair_actions(self) -> list[RepairAction]:
        assert self.root is not None
        root = self.root
        actions: list[RepairAction] = []
        move_sources: set[Path] = set()
        delete_paths: set[Path] = set()
        move_dests: set[Path] = set()

        def _abs(path: Path) -> Path:
            try:
                return path.resolve()
            except OSError:
                return path.absolute()

        def _add_move(src: Path, dst: Path, reason: str) -> None:
            src_abs = _abs(src)
            dst_abs = _abs(dst)
            if src_abs == dst_abs:
                return
            if src_abs in move_sources or src_abs in delete_paths:
                return
            if dst_abs in move_dests:
                return

            kind = "rename" if src_abs.parent == dst_abs.parent else "move"
            action = RepairAction(kind=kind, src=src_abs, dst=dst_abs, reason=reason)
            actions.append(action)
            move_sources.add(src_abs)
            move_dests.add(dst_abs)

        def _add_delete(path: Path, reason: str) -> None:
            path_abs = _abs(path)
            if path_abs == root:
                return
            if path_abs in move_sources or path_abs in delete_paths:
                return
            actions.append(RepairAction(kind="delete", src=path_abs, dst=None, reason=reason))
            delete_paths.add(path_abs)

        # 0) 根目录 .gitignore：写入豁免规则（.tmp/.backup/压缩包类型)。
        actions.append(
            RepairAction(
                kind="update_gitignore",
                src=_abs(root / ".gitignore"),
                dst=None,
                reason="Write exemption rules in root .gitignore (.tmp/.backup/archives)",
            )
        )

        # 0.1) 代码目录下 .tmp 合并到根目录 .tmp, 并删除源目录。
        if self.project_type_dir is not None:
            scoped_tmp = self.project_type_dir / ".tmp"
            if scoped_tmp.is_dir():
                actions.append(
                    RepairAction(
                        kind="merge_tmp_dir",
                        src=_abs(scoped_tmp),
                        dst=_abs(root / ".tmp"),
                        reason="Migrate .tmp content in code directory to root .tmp, and delete original directory",
                    )
                )
                _add_delete(
                    scoped_tmp,
                    "Clean up .tmp residue in code directory (after merge; if none exists, will automatically skip)",
                )

        # 1) 根目录必要文件：位置/命名修复, 重复与错位删除。
        for required in ROOT_REQUIRED_FILES:
            correct, misplaced, typos = self._collect_required_file_candidates(
                required,
                ROOT_REQUIRED_FILE_TYPO_ALIASES.get(required, set()),
            )
            destination = root / required

            if correct:
                for duplicate in correct[1:]:
                    _add_delete(duplicate, f"{required} duplicate in root directory, keeping one")
                for wrong in misplaced:
                    _add_delete(wrong, f"{required} location incorrect, correct file already exists in root directory")
                for typo in typos:
                    _add_delete(typo, f"{typo.name} naming incorrect, correct one already exists in root directory {required}")
            else:
                candidates = misplaced + typos
                if candidates:
                    selected = candidates[0]
                    _add_move(selected, destination, f"Repair {required} location/naming")
                    for redundant in candidates[1:]:
                        _add_delete(redundant, f"{required} candidates duplicate, keeping first repair source")

        # 1.1) metadata.json 字段补齐（缺失时创建, 已有时补全空字段)。
        actions.append(
            RepairAction(
                kind="upsert_metadata",
                src=_abs(root / "metadata.json"),
                dst=None,
                reason="Fill in metadata.json required fields",
                options={"mode": "repair"},
            )
        )

        # 1.2) docs 目录位置修复。
        docs_dir = root / "docs"
        _, misplaced_docs, typo_docs = self._collect_required_dir_candidates(
            "docs",
            {"doc", "document", "documents"},
        )
        if docs_dir.is_dir():
            for wrong in misplaced_docs:
                _add_delete(wrong, "docs/ misplaced duplicate, correct docs/ already exists in root directory")
            for typo in typo_docs:
                _add_delete(typo, f"{typo.name}/ naming incorrect, should be docs/")
        else:
            if misplaced_docs:
                selected = misplaced_docs[0]
                _add_move(selected, docs_dir, "Repair docs/ to root directory")
                for redundant in misplaced_docs[1:]:
                    _add_delete(redundant, "docs/ candidates duplicate, keeping first repair source")
                for typo in typo_docs:
                    _add_delete(typo, f"{typo.name}/ naming incorrect, docs/ candidate used for repair")
            elif typo_docs:
                selected = typo_docs[0]
                _add_move(selected, docs_dir, "Repair docs/ naming and move to root directory")
                for redundant in typo_docs[1:]:
                    _add_delete(redundant, "docs/ naming candidates duplicate, keeping first repair source")

        # 1.3) docs 必要文档（questions/design/api-spec)归位与命名修复。
        def _align_docs_file(required_name: str) -> None:
            destination = docs_dir / required_name
            in_docs, outside_docs, typo_candidates = self._collect_file_candidates_for_target_dir(
                required_name,
                docs_dir,
                DOC_REQUIRED_FILE_TYPO_ALIASES.get(required_name, set()),
            )
            candidates = [p for p in (outside_docs + typo_candidates) if _abs(p) != _abs(destination)]

            if in_docs:
                for duplicate in in_docs[1:]:
                    _add_delete(duplicate, f"docs/{required_name} duplicate, keeping one")
                for path in candidates:
                    _add_delete(path, f"{required_name} should be in docs/, deleting duplicates or misplaced candidates")
                return

            if not candidates:
                return

            candidates.sort(key=lambda p: (0 if p.parent == root else 1, p.as_posix().lower()))
            _add_move(candidates[0], destination, f"Repair {required_name} to docs/{required_name}")
            for redundant in candidates[1:]:
                _add_delete(redundant, f"{required_name} candidates duplicate, keeping first repair source")

        _align_docs_file("questions.md")
        _align_docs_file("design.md")
        _align_docs_file("api-spec.md")

        for prompt_candidate in self._collect_prompt_file_candidates():
            _add_delete(prompt_candidate, "prompt.md is deprecated, content should be saved in metadata.prompt")

        # 2) 代码目录 repo 修复（兼容旧结构目录)。
        repo_dir = root / REPO_DIR_NAME
        legacy_dirs = list(self.legacy_project_dirs)
        if not repo_dir.is_dir():
            repo_candidates: list[Path] = [path for _, path in legacy_dirs]
            if self.project_type_dir is not None and self.project_type_dir not in repo_candidates:
                repo_candidates.append(self.project_type_dir)
            repo_candidates = sorted(repo_candidates, key=lambda p: p.name.lower())
            if repo_candidates:
                selected = repo_candidates[0]
                _add_move(selected, repo_dir, "Repair code directory naming/location to repo/")
                for redundant in repo_candidates[1:]:
                    _add_delete(redundant, "Extra old structure code directories, keeping one migration source")
        else:
            for _, legacy_dir in legacy_dirs:
                _add_delete(legacy_dir, "Old structure code directory redundant, repo/ already exists")

        # 3) original_sessions 与旧会话 JSON 文件归位。
        sessions_correct, sessions_misplaced, sessions_typos = self._collect_required_dir_candidates(
            ORIGINAL_SESSIONS_DIR_NAME,
            ROOT_REQUIRED_DIR_TYPO_ALIASES.get(ORIGINAL_SESSIONS_DIR_NAME, set()),
        )
        if self._should_filter_code_scope_session_aliases(sessions_correct, sessions_typos):
            sessions_misplaced = self._filter_original_sessions_candidates(sessions_misplaced)
            sessions_typos = self._filter_original_sessions_candidates(sessions_typos)
        sessions_dir = root / ORIGINAL_SESSIONS_DIR_NAME
        moved_sessions_src: Path | None = None

        if sessions_correct:
            for duplicate in sessions_correct[1:]:
                _add_delete(duplicate, f"{ORIGINAL_SESSIONS_DIR_NAME}/ duplicate in root directory, keeping one")
            for wrong in sessions_misplaced:
                _add_delete(wrong, f"{ORIGINAL_SESSIONS_DIR_NAME}/ location incorrect, correct directory already exists in root directory")
            for typo in sessions_typos:
                _add_delete(typo, f"{typo.name}/ naming incorrect, correct one already exists in root directory {ORIGINAL_SESSIONS_DIR_NAME}/")
        else:
            candidates = sessions_misplaced + sessions_typos
            if candidates:
                selected = candidates[0]
                _add_move(selected, sessions_dir, f"Repair {ORIGINAL_SESSIONS_DIR_NAME}/ to root directory")
                moved_sessions_src = selected
                for redundant in candidates[1:]:
                    _add_delete(redundant, f"{ORIGINAL_SESSIONS_DIR_NAME}/ candidates duplicate, keeping first repair source")

        misplaced_session_files = self._collect_misplaced_legacy_session_json_files(sessions_dir)
        for src in misplaced_session_files:
            if moved_sessions_src is not None:
                try:
                    src.relative_to(moved_sessions_src)
                    continue
                except ValueError:
                    pass
            target = self._next_unique_filename_target(sessions_dir, src.name, move_dests)
            _add_move(src, target, f"Old session JSON files should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (suggest packing as archive later)")

        # 4) 根目录额外目录清理（豁免仅影响执 rows, 不影响提醒/计划)。
        allowed_root_dirs = ROOT_STANDARD_DIR_NAMES
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name in allowed_root_dirs:
                continue
            _add_delete(entry, "Delete non-standard directory in root directory")

        # 5) 根目录额外文件清理（豁免仅影响执 rows, 不影响提醒/计划)。
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name == "validation_report.md":
                continue
            if entry.name in ROOT_ALLOWED_FILES:
                continue
            _add_delete(entry, "Delete non-standard file in root directory")

        # 6) 修复模式下清理本地脏目录/文件（优先复用校验阶段结果, 避免重复全盘扫描)。
        if self._dirty_findings_cache is not None:
            for path, reason, status in self._dirty_findings_cache:
                if status != "FAIL":
                    continue
                if path.is_dir():
                    _add_delete(path, f"Delete local dirty directory: {reason}")
                else:
                    _add_delete(path, f"Delete local dirty file: {reason}")
        else:
            for current_root, dirs, files in os.walk(root, topdown=True):
                dirs.sort(key=str.lower)
                files.sort(key=str.lower)
                current_path = Path(current_root)
                pruned_dirs: list[str] = []

                for dirname in dirs:
                    lower_dir = dirname.lower()
                    if lower_dir in {".git", ".backup", ".tmp"}:
                        continue
                    dir_path = current_path / dirname
                    if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                        continue
                    reason = self._dir_violation_reason(dirname)
                    if reason:
                        if not self._is_compile_exempt_dir(dirname):
                            _add_delete(dir_path, f"Delete local dirty directory: {reason}")
                    else:
                        pruned_dirs.append(dirname)
                dirs[:] = pruned_dirs

                for filename in files:
                    file_path = current_path / filename
                    if file_path.name == "validation_report.md":
                        continue
                    if self._is_ignored_by_any_gitignore(file_path):
                        continue
                    reason = self._file_violation_reason(file_path)
                    if reason and not self._is_compile_exempt_file(file_path):
                        _add_delete(file_path, f"Delete local dirty file: {reason}")

        # 删除动作去重：若父目录已删除, 则子路径删除动作可省略。
        delete_roots: list[Path] = []
        normalized_actions: list[RepairAction] = []
        for action in actions:
            if action.kind != "delete":
                normalized_actions.append(action)
                continue

            skip = False
            for parent in delete_roots:
                try:
                    action.src.relative_to(parent)
                    skip = True
                    break
                except ValueError:
                    continue
            if skip:
                continue

            delete_roots.append(action.src)
            normalized_actions.append(action)

        return normalized_actions

    def _plan_convert_legacy_actions(self) -> list[RepairAction]:
        assert self.root is not None
        root = self.root
        actions: list[RepairAction] = []
        move_sources: set[Path] = set()
        move_dests: set[Path] = set()
        delete_paths: set[Path] = set()

        def _abs(path: Path) -> Path:
            try:
                return path.resolve()
            except OSError:
                return path.absolute()

        def _add_move(src: Path, dst: Path, reason: str) -> None:
            src_abs = _abs(src)
            dst_abs = _abs(dst)
            if src_abs == dst_abs:
                return
            if src_abs in move_sources or src_abs in delete_paths:
                return
            if dst_abs in move_dests:
                return
            kind = "rename" if src_abs.parent == dst_abs.parent else "move"
            actions.append(RepairAction(kind=kind, src=src_abs, dst=dst_abs, reason=reason))
            move_sources.add(src_abs)
            move_dests.add(dst_abs)

        def _add_delete(path: Path, reason: str) -> None:
            path_abs = _abs(path)
            if path_abs == root:
                return
            if path_abs in move_sources or path_abs in delete_paths:
                return
            actions.append(RepairAction(kind="delete", src=path_abs, dst=None, reason=reason))
            delete_paths.add(path_abs)

        def _add_upsert_metadata(path: Path, reason: str, mode: str = "repair") -> None:
            actions.append(
                RepairAction(
                    kind="upsert_metadata",
                    src=_abs(path),
                    dst=None,
                    reason=reason,
                    options={"mode": mode},
                )
            )

        # A) docs 目录与核心文档归位（questions/design/api-spec)。
        docs_dir = root / "docs"
        _, misplaced_docs, typo_docs = self._collect_required_dir_candidates(
            "docs",
            {"doc", "document", "documents"},
        )
        if docs_dir.is_dir():
            for wrong in misplaced_docs:
                _add_delete(wrong, "docs/ misplaced duplicate, correct docs/ already exists in root directory")
            for typo in typo_docs:
                _add_delete(typo, f"{typo.name}/ naming incorrect, should be docs/")
        else:
            if misplaced_docs:
                selected = misplaced_docs[0]
                _add_move(selected, docs_dir, "Repair docs/ to root directory")
                for redundant in misplaced_docs[1:]:
                    _add_delete(redundant, "docs/ candidates duplicate, keeping first repair source")
                for typo in typo_docs:
                    _add_delete(typo, f"{typo.name}/ naming incorrect, docs/ candidate used for repair")
            elif typo_docs:
                selected = typo_docs[0]
                _add_move(selected, docs_dir, "Repair docs/ naming and move to root directory")
                for redundant in typo_docs[1:]:
                    _add_delete(redundant, "docs/ naming candidates duplicate, keeping first repair source")

        def _align_docs_file(required_name: str) -> None:
            destination = docs_dir / required_name
            in_docs, outside_docs, typo_candidates = self._collect_file_candidates_for_target_dir(
                required_name,
                docs_dir,
                DOC_REQUIRED_FILE_TYPO_ALIASES.get(required_name, set()),
            )
            candidates = [p for p in (outside_docs + typo_candidates) if _abs(p) != _abs(destination)]

            if in_docs:
                for duplicate in in_docs[1:]:
                    _add_delete(duplicate, f"docs/{required_name} duplicate, keeping one")
                for path in candidates:
                    _add_delete(path, f"{required_name} should be in docs/, deleting duplicates or misplaced candidates")
                return

            if not candidates:
                return

            candidates.sort(key=lambda p: (0 if p.parent == root else 1, p.as_posix().lower()))
            _add_move(candidates[0], destination, f"Repair {required_name} to docs/{required_name}")
            for redundant in candidates[1:]:
                _add_delete(redundant, f"{required_name} candidates duplicate, keeping first repair source")

        _align_docs_file("questions.md")
        _align_docs_file("design.md")
        _align_docs_file("api-spec.md")

        # B) 旧项目类型目录迁移到 repo。
        repo_dir = root / REPO_DIR_NAME
        legacy_dirs = self._collect_legacy_project_directories(root)
        source_dir: Path | None = None
        if repo_dir.is_dir():
            source_dir = repo_dir
        elif legacy_dirs:
            source_dir = legacy_dirs[0][1]
        else:
            inferred = self._infer_repo_candidate_from_common_dir()
            if inferred is not None:
                source_dir = inferred

        if not repo_dir.is_dir() and source_dir is not None and source_dir != repo_dir:
            _add_move(source_dir, repo_dir, "Old structure code directory migrated to repo/")

        for _, legacy_dir in legacy_dirs:
            if source_dir is not None and legacy_dir == source_dir and not repo_dir.is_dir():
                continue
            if repo_dir.is_dir():
                nested_dst = repo_dir / legacy_dir.name
                if not nested_dst.exists():
                    _add_move(legacy_dir, nested_dst, "Merge residual old structure directory into repo/")
                else:
                    _add_delete(legacy_dir, "Residual old structure directory conflicts with repo content, deleting redundant directory")
            else:
                _add_delete(legacy_dir, "Extra old structure code directories, keeping single migration source")

        # C) original_sessions/ 与旧会话 JSON 文件迁移。
        sessions_dir = root / ORIGINAL_SESSIONS_DIR_NAME
        sessions_correct, sessions_misplaced, sessions_typos = self._collect_required_dir_candidates(
            ORIGINAL_SESSIONS_DIR_NAME,
            ROOT_REQUIRED_DIR_TYPO_ALIASES.get(ORIGINAL_SESSIONS_DIR_NAME, set()),
        )
        if self._should_filter_code_scope_session_aliases(sessions_correct, sessions_typos):
            sessions_misplaced = self._filter_original_sessions_candidates(sessions_misplaced)
            sessions_typos = self._filter_original_sessions_candidates(sessions_typos)
        moved_sessions_src: Path | None = None
        if sessions_correct:
            for duplicate in sessions_correct[1:]:
                _add_delete(duplicate, f"{ORIGINAL_SESSIONS_DIR_NAME}/ duplicate in root directory, keeping one")
            for wrong in sessions_misplaced:
                _add_delete(wrong, f"{ORIGINAL_SESSIONS_DIR_NAME}/ location incorrect, correct directory already exists in root directory")
            for typo in sessions_typos:
                _add_delete(typo, f"{typo.name}/ naming incorrect, correct one already exists in root directory {ORIGINAL_SESSIONS_DIR_NAME}/")
        else:
            candidates = sessions_misplaced + sessions_typos
            if candidates:
                selected = candidates[0]
                _add_move(selected, sessions_dir, f"Repair {ORIGINAL_SESSIONS_DIR_NAME}/ to root directory")
                moved_sessions_src = selected
                for redundant in candidates[1:]:
                    _add_delete(redundant, f"{ORIGINAL_SESSIONS_DIR_NAME}/ candidates duplicate, keeping first repair source")

        misplaced_session_files = self._collect_misplaced_legacy_session_json_files(sessions_dir)
        for src in misplaced_session_files:
            if moved_sessions_src is not None:
                try:
                    src.relative_to(moved_sessions_src)
                    continue
                except ValueError:
                    pass
            target = self._next_unique_filename_target(sessions_dir, src.name, move_dests)
            _add_move(src, target, f"Old session JSON files should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (suggest packing as archive later)")

        # D) metadata.json 迁移/补齐。
        metadata_correct, metadata_misplaced, metadata_typos = self._collect_required_file_candidates(
            "metadata.json",
            ROOT_REQUIRED_FILE_TYPO_ALIASES.get("metadata.json", set()),
        )
        metadata_path = root / "metadata.json"

        if metadata_correct:
            for duplicate in metadata_correct[1:]:
                _add_delete(duplicate, "metadata.json duplicate in root directory, keeping one")
            for wrong in metadata_misplaced:
                _add_delete(wrong, "metadata.json location incorrect, correct file already exists in root directory")
            for typo in metadata_typos:
                _add_delete(typo, f"{typo.name} naming incorrect, metadata.json already exists in root directory")
        else:
            candidates = metadata_misplaced + metadata_typos
            if candidates:
                _add_move(candidates[0], metadata_path, "Repair metadata.json to root directory")
                for redundant in candidates[1:]:
                    _add_delete(redundant, "metadata.json candidates duplicate, keeping first repair source")

        _add_upsert_metadata(metadata_path, "Fill in metadata.json required fields", mode="legacy_convert")
        for prompt_candidate in self._collect_prompt_file_candidates():
            _add_delete(prompt_candidate, "prompt.md is deprecated, will be written to metadata.prompt during migration")

        # E) 根目录额外目录清理（豁免仅影响执 rows, 不影响提醒/计划)。
        allowed_root_dirs = ROOT_STANDARD_DIR_NAMES
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name in allowed_root_dirs:
                continue
            _add_delete(entry, "Delete non-standard directory in root directory")

        # F) 根目录额外文件清理（豁免仅影响执 rows, 不影响提醒/计划)。
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name == "validation_report.md":
                continue
            if entry.name in ROOT_ALLOWED_FILES:
                continue
            _add_delete(entry, "Delete non-standard file in root directory")

        # 删除动作去重：若父目录已删除, 则子路径删除动作可省略。
        delete_roots: list[Path] = []
        normalized_actions: list[RepairAction] = []
        for action in actions:
            if action.kind != "delete":
                normalized_actions.append(action)
                continue

            skip = False
            for parent in delete_roots:
                try:
                    action.src.relative_to(parent)
                    skip = True
                    break
                except ValueError:
                    continue
            if skip:
                continue

            delete_roots.append(action.src)
            normalized_actions.append(action)

        return normalized_actions

    def _is_repair_delete_exempt(self, path: Path) -> tuple[bool, str]:
        assert self.root is not None
        root = self.root
        backup_dir = root / ".backup"
        tmp_dir = root / ".tmp"

        try:
            path.relative_to(tmp_dir)
            return True, "located in .tmp directory (exempt from deletion)"
        except ValueError:
            pass

        try:
            path.relative_to(backup_dir)
            return True, "located in .backup directory (exempt from deletion)"
        except ValueError:
            pass

        if path == tmp_dir:
            return True, ".tmp directory (exempt from deletion)"
        if path == backup_dir:
            return True, ".backup directory (exempt from deletion)"

        path_maybe_dir = path.is_dir() or (not path.exists() and path.suffix == "")
        if path_maybe_dir and self._is_compile_exempt_dir(path.name):
            return True, "Build artifact directory (exempt from deletion, reminder only)"
        if self._is_compile_exempt_file(path):
            return True, "Build artifact file (exempt from deletion, reminder only)"

        if path.parent == root and path.suffix.lower() in ROOT_REPAIR_EXEMPT_ARCHIVE_EXTS:
            return True, "Root directory archive (exempt from deletion)"

        return False, ""

    def _print_repair_plan(self, actions: list[RepairAction]) -> None:
        for idx, action in enumerate(actions, start=1):
            src = self._rel(action.src)
            if action.kind == "update_gitignore":
                print(f"{idx:03d}. [UPDATE] {src} | {action.reason}")
                continue
            if action.kind == "merge_tmp_dir":
                dst = self._rel(action.dst) if action.dst is not None else ".tmp"
                print(f"{idx:03d}. [MERGE_TMP] {src} -> {dst} | {action.reason}")
                continue
            if action.kind == "upsert_metadata":
                print(f"{idx:03d}. [UPSERT_METADATA] {src} | {action.reason}")
                continue
            if action.kind in {"move", "rename"} and action.dst is not None:
                dst = self._rel(action.dst)
                verb = "RENAME" if action.kind == "rename" else "MOVE"
                print(f"{idx:03d}. [{verb}] {src} -> {dst} | {action.reason}")
                continue

            exempt, exempt_reason = self._is_repair_delete_exempt(action.src)
            if exempt:
                print(
                    f"{idx:03d}. [DELETE-SKIP] {src} | {action.reason} | {exempt_reason}"
                )
            else:
                print(f"{idx:03d}. [DELETE] {src} | {action.reason}")

    def _backup_original_path(self, path: Path, backup_root_dir: Path) -> None:
        assert self.root is not None
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            rel = Path(path.name)
        dest = backup_root_dir / rel

        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            idx = 1
            while True:
                candidate = dest.with_name(f"{dest.name}.bak-{stamp}-{idx}")
                if not candidate.exists():
                    dest = candidate
                    break
                idx += 1

        if path.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, dest)
            return

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    def _build_gitignore_content_with_exemptions(self, existing_content: str) -> tuple[str, list[str]]:
        lines = existing_content.splitlines()
        stripped_set = {line.strip() for line in lines if line.strip()}

        additions: list[str] = []
        if REPAIR_GITIGNORE_HEADER not in stripped_set:
            additions.append(REPAIR_GITIGNORE_HEADER)

        for pattern in REPAIR_GITIGNORE_EXEMPT_PATTERNS:
            if pattern not in stripped_set:
                additions.append(pattern)

        if not additions:
            normalized = "\n".join(lines)
            if normalized and not normalized.endswith("\n"):
                normalized += "\n"
            return normalized, []

        out_lines = list(lines)
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.extend(additions)
        return "\n".join(out_lines).rstrip("\n") + "\n", additions

    def _build_metadata_defaults(self, current: dict[str, object]) -> dict[str, str]:
        project_type_value = self._infer_metadata_project_type(current)
        return {
            "prompt": "",
            "project_type": project_type_value,
            "frontend_language": "",
            "backend_language": "",
            "frontend_framework": "",
            "backend_framework": "",
            "database": "",
        }

    def _infer_metadata_project_type(self, metadata_like: dict[str, object] | None = None) -> str:
        legacy_map = {
            "pure_backend": "server",
            "pure_frontend": "web",
            "fullstack": "fullstack",
            "cross_platform_app": "desktop",
            "mobile_app": "android",
        }
        if self.project_type_name in legacy_map:
            return legacy_map[self.project_type_name]

        source = metadata_like or {}
        frontend = str(source.get("frontend_language", "")).strip().lower()
        backend = str(source.get("backend_language", "")).strip().lower()
        none_tokens = {"", "none", "null", "n/a", "na", "-", "no"}

        has_frontend = frontend not in none_tokens and frontend not in {"server", "backend"}
        has_backend = backend not in none_tokens
        if frontend in {"server", "backend"} and not has_backend:
            return "server"
        if has_frontend and has_backend:
            return "fullstack"
        if has_backend:
            return "server"
        if has_frontend:
            return "web"
        return "web"

    def _collect_misplaced_legacy_session_json_files(self, target_dir: Path) -> list[Path]:
        matches: list[Path] = []
        legacy_aliases = {name.lower() for name in ROOT_REQUIRED_FILE_TYPO_ALIASES.get("trajectory.json", set())}
        for path, is_file, _, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_file:
                continue
            try:
                path.relative_to(target_dir)
                continue
            except ValueError:
                pass

            if (
                name_lower == "trajectory.json"
                or name_lower in legacy_aliases
                or TRAJECTORY_MULTI_RE.fullmatch(name_lower) is not None
                or LEGACY_SESSION_JSON_RE.fullmatch(name_lower) is not None
            ):
                matches.append(path)
        matches.sort(key=lambda p: p.as_posix().lower())
        return matches

    def _next_unique_filename_target(self, target_dir: Path, desired_name: str, move_dests: set[Path]) -> Path:
        candidate = target_dir / desired_name
        if not candidate.exists() and candidate.resolve() not in move_dests:
            return candidate

        stem = Path(desired_name).stem
        suffix = Path(desired_name).suffix
        idx = 1
        while True:
            alt = target_dir / f"{stem}-{idx}{suffix}"
            if not alt.exists() and alt.resolve() not in move_dests:
                return alt
            idx += 1

    def _is_session_archive_file(self, path: Path) -> bool:
        suffixes = [s.lower() for s in path.suffixes]
        if not suffixes:
            return False
        if len(suffixes) >= 2 and suffixes[-2:] == [".tar", ".gz"]:
            return True
        return suffixes[-1] in SESSION_ARCHIVE_SUFFIXES

    def _is_linux_style_session_zip_stem(self, stem: str) -> bool:
        # Linux 绝对路径（以 / 开头)归一化后通常会以 "-" 开头。
        return stem.startswith(LEADING_SESSION_ZIP_LINUX_PREFIX)

    def _is_windows_style_session_zip_stem(self, stem: str) -> bool:
        # Windows C 盘路径归一化后通常会以 "C--" 开头（大小写不敏感)。
        return stem.lower().startswith(LEADING_SESSION_ZIP_WINDOWS_PREFIX)

    def _is_leading_session_zip_archive(self, path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() != ".zip":
            return False
        stem = path.stem
        return self._is_linux_style_session_zip_stem(stem) or self._is_windows_style_session_zip_stem(stem)

    def _collect_leading_session_zip_archives(self, sessions_dir: Path) -> list[Path]:
        archives = [
            path
            for path in sessions_dir.iterdir()
            if self._is_leading_session_zip_archive(path)
        ]
        archives.sort(key=lambda p: p.name.lower())
        return archives

    def _is_acompact_agent_stem(self, stem: str) -> bool:
        lower = stem.lower()
        return lower.startswith("agent-acompact-") or lower.startswith("agent-compact-")

    def _is_subagent_meta_exempt_stem(self, stem: str) -> bool:
        lower = stem.lower()
        if lower.startswith("agent-aside_question"):
            return True
        return self._is_acompact_agent_stem(stem)

    def _truncate_subprocess_output(self, text: str, limit: int = 240) -> str:
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return ""
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3] + "..."

    def _retry_extract_with_sudo(self, archive_path: Path, destination_dir: Path) -> tuple[bool, str]:
        sudo_bin = shutil.which("sudo")
        if sudo_bin is None:
            return False, "System does not have sudo installed, cannot perform privilege escalation retry"

        attempts: list[tuple[str, list[str]]] = []
        unzip_bin = shutil.which("unzip")
        if unzip_bin is not None:
            attempts.append(
                (
                    "sudo unzip",
                    [sudo_bin, "-n", unzip_bin, "-o", str(archive_path), "-d", str(destination_dir)],
                )
            )
        bsdtar_bin = shutil.which("bsdtar")
        if bsdtar_bin is not None:
            attempts.append(
                (
                    "sudo bsdtar",
                    [sudo_bin, "-n", bsdtar_bin, "-xf", str(archive_path), "-C", str(destination_dir)],
                )
            )

        if not attempts:
            return False, "unzip/bsdtar not found, cannot complete privilege escalation decompression via sudo"

        failures: list[str] = []
        for label, command in attempts:
            try:
                proc = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=SUDO_RETRY_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                failures.append(f"{label} timeout: {SUDO_RETRY_TIMEOUT_SECONDS}s")
                continue
            except OSError as exc:
                failures.append(f"{label} startup failed: {exc}")
                continue

            if proc.returncode == 0:
                return True, f"{label} privilege escalation retry succeeded"

            stderr_text = self._truncate_subprocess_output(proc.stderr or "")
            stdout_text = self._truncate_subprocess_output(proc.stdout or "")
            if stderr_text:
                failures.append(f"{label} failed: {stderr_text}")
            elif stdout_text:
                failures.append(f"{label} failed: {stdout_text}")
            else:
                failures.append(f"{label} failed: exit_code={proc.returncode}")

        return False, "；".join(failures)

    def _validate_and_extract_leading_session_zip(self, archive_path: Path, destination_dir: Path) -> tuple[bool, str, list[str]]:
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                infos = zf.infolist()
                if not infos:
                    return False, "Archive is empty", []

                broken_member = zf.testzip()
                if broken_member is not None:
                    return False, f"Archive validation failed, corrupted member: {broken_member}", []

                destination_abs = destination_dir.resolve()
                top_level_names: set[str] = set()

                for info in infos:
                    normalized_name = info.filename.replace("\\", "/").lstrip("/")
                    if not normalized_name:
                        continue

                    parts = [part for part in normalized_name.split("/") if part and part != "."]
                    if not parts:
                        continue
                    if any(part == ".." for part in parts):
                        return False, f"Archive contains illegal path: {info.filename}", []
                    top_level_names.add(parts[0])

                    target = (destination_dir / "/".join(parts)).resolve()
                    try:
                        target.relative_to(destination_abs)
                    except ValueError:
                        return False, f"Archive contains out-of-bounds path: {info.filename}", []

                try:
                    zf.extractall(destination_dir)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EPERM}:
                        raise
                    retry_ok, retry_detail = self._retry_extract_with_sudo(archive_path, destination_dir)
                    if retry_ok:
                        return True, f"Normal privilege decompression failed, retried: {retry_detail}", sorted(top_level_names)
                    return False, f"Normal privilege decompression failed: {exc}; privilege escalation retry failed: {retry_detail}", []
            if not top_level_names:
                return False, "No valid content detected in archive", []
            return True, "Decompression successful", sorted(top_level_names)
        except (OSError, RuntimeError, zipfile.BadZipFile, ValueError) as exc:
            return False, str(exc), []

    def _normalize_for_compare(self, text: str) -> str:
        if not text:
            return ""

        text = unicodedata.normalize("NFKC", text)
        text = text.replace(r"\n", "")
        text = text.replace(r"\r", "")
        text = text.replace(r"\t", "")
        text = text.replace(r"\"", "")
        text = text.replace(r"\'", "")
        text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "", text)
        return text

    def _strip_trailing_punct_and_space(self, text: str | None) -> str:
        if text is None:
            return ""

        normalized = text.rstrip()
        trailing_punct = string.punctuation + ", 。！？；：、“”‘’（)【】《》—…"
        while normalized and normalized[-1] in trailing_punct:
            normalized = normalized[:-1].rstrip()
        return normalized

    def _get_head_anchor(self, reference_text: str, anchor_len: int = 40) -> str:
        ref = reference_text.lstrip()
        return ref[:anchor_len]

    def _get_tail_anchor(self, reference_text: str, anchor_len: int = 40) -> str:
        ref = self._strip_trailing_punct_and_space(reference_text)
        if not ref:
            return ""
        return ref[-anchor_len:]

    def _build_loose_pattern(self, head_anchor: str, tail_anchor: str) -> str:
        head = re.escape(head_anchor)
        tail = re.escape(tail_anchor)
        if not head and not tail:
            return r"(.*)"
        if head and not tail:
            return rf"{head}(.*)"
        if tail and not head:
            return rf"(.*?){tail}"
        return rf"{head}(.*?){tail}"

    def _similarity_score(self, text1: str, text2: str) -> float:
        s1 = self._normalize_for_compare(text1)
        s2 = self._normalize_for_compare(text2)
        return difflib.SequenceMatcher(None, s1, s2).ratio()

    def _get_diff_preview(self, text1: str, text2: str, context: int = 60) -> dict[str, object]:
        s1 = self._normalize_for_compare(text1)
        s2 = self._normalize_for_compare(text2)

        min_len = min(len(s1), len(s2))
        diff_index = -1
        for idx in range(min_len):
            if s1[idx] != s2[idx]:
                diff_index = idx
                break
        if diff_index == -1 and len(s1) != len(s2):
            diff_index = min_len

        if diff_index == -1:
            return {
                "diff_index": -1,
                "reference_preview": "",
                "matched_preview": "",
            }

        start = max(0, diff_index - context)
        end1 = min(len(s1), diff_index + context)
        end2 = min(len(s2), diff_index + context)
        return {
            "diff_index": diff_index,
            "reference_preview": s1[start:end1],
            "matched_preview": s2[start:end2],
        }

    def _compare_by_anchor(
        self,
        reference_text: str,
        target_text: str,
        anchor_len: int = 40,
        similarity_threshold: float = SESSION_PROMPT_SIMILARITY_THRESHOLD,
    ) -> dict[str, object]:
        head_anchor = self._get_head_anchor(reference_text, anchor_len=anchor_len)
        tail_anchor = self._get_tail_anchor(reference_text, anchor_len=anchor_len)

        if not head_anchor and not tail_anchor:
            return {
                "matched": False,
                "equal": False,
                "near_duplicate": False,
                "similarity": 0.0,
                "reason": "reference_text empty or unable to extract anchor",
            }

        pattern = self._build_loose_pattern(head_anchor, tail_anchor)
        matched = re.search(pattern, target_text, flags=re.DOTALL)
        if not matched:
            reference_normalized = self._normalize_for_compare(reference_text)
            target_normalized = self._normalize_for_compare(target_text)
            similarity = self._similarity_score(reference_text, target_text)
            near_duplicate = similarity >= similarity_threshold
            diff_info = self._get_diff_preview(reference_text, target_text)
            return {
                "matched": False,
                "equal": reference_normalized == target_normalized,
                "near_duplicate": near_duplicate,
                "similarity": similarity,
                "head_anchor": head_anchor,
                "tail_anchor": tail_anchor,
                "matched_segment": target_text,
                "reference_normalized": reference_normalized,
                "matched_normalized": target_normalized,
                "diff_index": diff_info["diff_index"],
                "reference_diff_preview": diff_info["reference_preview"],
                "matched_diff_preview": diff_info["matched_preview"],
                "fallback_full_text": True,
                "reason": "Candidate text segment bounded by head and tail anchors not found in target text, degraded to full-segment similarity check",
            }

        matched_segment = matched.group(0)
        reference_normalized = self._normalize_for_compare(reference_text)
        matched_normalized = self._normalize_for_compare(matched_segment)
        similarity = self._similarity_score(reference_text, matched_segment)
        near_duplicate = similarity >= similarity_threshold
        diff_info = self._get_diff_preview(reference_text, matched_segment)
        return {
            "matched": True,
            "equal": reference_normalized == matched_normalized,
            "near_duplicate": near_duplicate,
            "similarity": similarity,
            "head_anchor": head_anchor,
            "tail_anchor": tail_anchor,
            "matched_segment": matched_segment,
            "reference_normalized": reference_normalized,
            "matched_normalized": matched_normalized,
            "diff_index": diff_info["diff_index"],
            "reference_diff_preview": diff_info["reference_preview"],
            "matched_diff_preview": diff_info["matched_preview"],
            "fallback_full_text": False,
        }

    def _read_metadata_prompt_for_session_compare(self) -> tuple[str | None, Path, str | None]:
        assert self.root is not None
        metadata_path = self.root / "metadata.json"
        content = self._read_text(metadata_path)
        if content is None:
            return None, metadata_path, "metadata.json missing or unreadable, cannot read prompt field"

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, metadata_path, f"metadata.json is not valid JSON: {exc.msg}"

        if not isinstance(parsed, dict):
            return None, metadata_path, "metadata.json top-level is not object, cannot read prompt field"

        prompt_value = parsed.get("prompt")
        if prompt_value is None:
            return None, metadata_path, "metadata.json missing prompt field, cannot compare anchors"

        prompt_text = prompt_value if isinstance(prompt_value, str) else str(prompt_value)
        if not prompt_text.strip():
            return None, metadata_path, "metadata.prompt is empty, cannot compare anchors"

        return prompt_text, metadata_path, None

    def _collect_first_layer_jsonl_files(self, bundle_dir: Path) -> list[Path]:
        files = [path for path in bundle_dir.iterdir() if path.is_file() and path.suffix.lower() == ".jsonl"]
        files.sort(key=lambda p: p.name.lower())
        return files

    def _new_usage_totals(self) -> dict[str, int]:
        return {key: 0 for key in TOKEN_USAGE_KEYS}

    def _add_usage_totals(self, dst: dict[str, int], src: dict[str, int]) -> None:
        for key in TOKEN_USAGE_KEYS:
            dst[key] = dst.get(key, 0) + src.get(key, 0)

    def _to_int_token_value(self, value: object) -> int:
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

    def _map_usage_tokens(self, usage: dict[str, object]) -> dict[str, int]:
        return {
            "input_tokens": self._to_int_token_value(usage.get("input_tokens", 0)),
            "output_tokens": self._to_int_token_value(usage.get("output_tokens", 0)),
            "cache_read_tokens": self._to_int_token_value(usage.get("cache_read_input_tokens", 0)),
            "cache_write_tokens": self._to_int_token_value(usage.get("cache_creation_input_tokens", 0)),
        }

    def _calc_token_cost_usd(self, tokens: dict[str, int]) -> float:
        return (
            tokens.get("input_tokens", 0) / 1_000_000 * TOKEN_PRICE_INPUT_PER_M_USD
            + tokens.get("output_tokens", 0) / 1_000_000 * TOKEN_PRICE_OUTPUT_PER_M_USD
            + tokens.get("cache_read_tokens", 0) / 1_000_000 * TOKEN_PRICE_CACHE_READ_PER_M_USD
            + tokens.get("cache_write_tokens", 0) / 1_000_000 * TOKEN_PRICE_CACHE_WRITE_PER_M_USD
        )

    def _resolve_token_cost_threshold(self) -> tuple[float, str]:
        project_type = ""
        project_type_obj = self.metadata.get("project_type") if isinstance(self.metadata, dict) else None
        if isinstance(project_type_obj, str):
            project_type = project_type_obj.strip().lower()

        if not project_type and self.root is not None:
            metadata_path = self.root / "metadata.json"
            content = self._read_text(metadata_path)
            if content is not None:
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    from_file = parsed.get("project_type")
                    if isinstance(from_file, str):
                        project_type = from_file.strip().lower()

        if project_type in {"server", "web"}:
            return TOKEN_COST_THRESHOLD_SERVER_WEB_USD, project_type
        if project_type in {"fullstack", "full_stack", "full-stack"}:
            return TOKEN_COST_THRESHOLD_FULLSTACK_USD, project_type
        return TOKEN_COST_THRESHOLD_DEFAULT_USD, project_type or "unknown"

    def _extract_usage_tokens_from_payload(self, payload: dict[str, object]) -> tuple[dict[str, int] | None, str | None]:
        usage: object | None = None
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
        return self._map_usage_tokens(usage), message_id

    def _scan_keyword_line_numbers_from_text(self, content: str) -> dict[str, list[int]]:
        findings = {keyword: [] for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS}
        if not content:
            return findings

        for line_no, raw_line in enumerate(content.splitlines(), start=1):
            lower_line = raw_line.lower()
            for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS:
                if keyword in lower_line:
                    findings[keyword].append(line_no)
        return findings

    def _jsonl_analysis_cache_key(self, jsonl_path: Path) -> str:
        try:
            return jsonl_path.resolve().as_posix().lower()
        except OSError:
            return jsonl_path.absolute().as_posix().lower()

    def _analyze_jsonl_usage_only(self, jsonl_path: Path) -> JsonlUsageAnalysis:
        key = self._jsonl_analysis_cache_key(jsonl_path)
        cached = self._jsonl_usage_analysis_cache.get(key)
        if cached is not None:
            return cached

        analysis = JsonlUsageAnalysis(
            path=jsonl_path,
            readable=False,
            usage_totals=self._new_usage_totals(),
            keyword_line_numbers={keyword: [] for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS},
        )
        content = self._read_text(jsonl_path)
        if content is None:
            self._jsonl_usage_analysis_cache[key] = analysis
            return analysis

        analysis.readable = True
        analysis.keyword_line_numbers = self._scan_keyword_line_numbers_from_text(content)
        usage_by_message_id: dict[str, dict[str, int]] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            usage_tokens, message_id = self._extract_usage_tokens_from_payload(payload)
            if usage_tokens is None:
                continue
            if message_id:
                usage_by_message_id[message_id] = usage_tokens
            else:
                self._add_usage_totals(analysis.usage_totals, usage_tokens)
                analysis.usage_record_count += 1

        for usage_tokens in usage_by_message_id.values():
            self._add_usage_totals(analysis.usage_totals, usage_tokens)
            analysis.usage_record_count += 1

        self._jsonl_usage_analysis_cache[key] = analysis
        return analysis

    def _analyze_jsonl_file(self, jsonl_path: Path) -> JsonlAnalysis:
        key = self._jsonl_analysis_cache_key(jsonl_path)
        cached = self._jsonl_analysis_cache.get(key)
        if cached is not None:
            return cached

        analysis = JsonlAnalysis(
            path=jsonl_path,
            readable=False,
            keyword_line_numbers={keyword: [] for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS},
            usage_totals=self._new_usage_totals(),
        )

        content = self._read_text(jsonl_path)
        if content is None:
            self._jsonl_analysis_cache[key] = analysis
            return analysis

        analysis.readable = True
        analysis.keyword_line_numbers = self._scan_keyword_line_numbers_from_text(content)
        usage_by_message_id: dict[str, dict[str, int]] = {}
        for line_no, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue

            usage_tokens, message_id = self._extract_usage_tokens_from_payload(payload)
            if usage_tokens is not None:
                if message_id:
                    usage_by_message_id[message_id] = usage_tokens
                else:
                    self._add_usage_totals(analysis.usage_totals, usage_tokens)
                    analysis.usage_record_count += 1

            timestamp_raw: str | None = None
            parsed_ts: datetime | None = None
            timestamp_value = payload.get("timestamp")
            if isinstance(timestamp_value, str) and timestamp_value.strip():
                timestamp_raw = timestamp_value.strip()
                if analysis.first_timestamp_raw is None:
                    analysis.first_timestamp_raw = timestamp_raw
                    analysis.first_timestamp_line = line_no

                parsed_ts = self._parse_timestamp_to_utc(timestamp_raw)
                if parsed_ts is not None:
                    if analysis.latest_timestamp_dt is None or parsed_ts > analysis.latest_timestamp_dt:
                        analysis.latest_timestamp_dt = parsed_ts
                        analysis.latest_timestamp_raw = timestamp_raw
                        analysis.latest_timestamp_line = line_no

            if self._is_semantic_message_event(payload):
                message = self._event_message_dict(payload)
                role = ""
                if message is not None:
                    role = str(message.get("role", "")).strip().lower()
                analysis.last_semantic_line = line_no
                analysis.last_semantic_role = role or None
                analysis.last_semantic_has_assistant_text = self._event_has_assistant_text(payload)

            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            role_value = message.get("role")
            if not isinstance(role_value, str) or role_value.lower() != "user":
                continue

            analysis.user_line_count += 1
            content_text = self._extract_candidate_content_from_user_payload(payload, message)
            if not content_text.strip():
                continue
            if self._is_session_prompt_compare_exempt_payload(payload, content_text):
                analysis.exempt_user_line_count += 1
                continue

            if timestamp_raw is None:
                continue
            if parsed_ts is None:
                parsed_ts = self._parse_timestamp_to_utc(timestamp_raw)
            if parsed_ts is None:
                continue

            analysis.user_candidates.append((content_text, line_no, timestamp_raw, parsed_ts))

        for usage_tokens in usage_by_message_id.values():
            self._add_usage_totals(analysis.usage_totals, usage_tokens)
            analysis.usage_record_count += 1

        self._jsonl_analysis_cache[key] = analysis
        return analysis

    def _extract_latest_timestamp_from_jsonl(self, jsonl_path: Path) -> tuple[datetime | None, str | None, int | None]:
        analysis = self._analyze_jsonl_file(jsonl_path)
        if not analysis.readable:
            return None, None, None
        return analysis.latest_timestamp_dt, analysis.latest_timestamp_raw, analysis.latest_timestamp_line

    def _event_message_dict(self, payload: dict[str, object]) -> dict[str, object] | None:
        message = payload.get("message")
        if isinstance(message, dict):
            return message
        return None

    def _is_semantic_message_event(self, payload: dict[str, object]) -> bool:
        message = self._event_message_dict(payload)
        if message is None:
            return False
        message_type = str(message.get("type", "")).strip().lower()
        if message_type and message_type != "message":
            return False
        if self._is_local_command_noise_user_message(payload):
            return False
        return True

    def _extract_message_text_content(self, message: dict[str, object]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    text_parts.append(item)
                    continue
                if isinstance(item, dict):
                    item_type = str(item.get("type", "")).strip().lower()
                    if item_type == "text":
                        text_parts.append(str(item.get("text", "")))
                    elif item_type == "tool_result":
                        text_parts.append(str(item.get("content", "")))
            return "\n".join(text_parts)
        if isinstance(content, dict):
            item_type = str(content.get("type", "")).strip().lower()
            if item_type == "text":
                return str(content.get("text", ""))
            if item_type == "tool_result":
                return str(content.get("content", ""))
        return ""

    def _is_local_command_noise_user_message(self, payload: dict[str, object]) -> bool:
        message = self._event_message_dict(payload)
        if message is None:
            return False
        role = str(message.get("role", "")).strip().lower()
        if role != "user":
            return False

        text_content = self._extract_message_text_content(message).strip()
        if not text_content:
            return False

        local_command_prefixes = (
            "<local-command-caveat>",
            "<command-name>",
            "<command-message>",
            "<command-args>",
            "<local-command-stdout>",
            "<local-command-stderr>",
        )
        return any(text_content.startswith(prefix) for prefix in local_command_prefixes)

    def _event_has_assistant_text(self, payload: dict[str, object]) -> bool:
        message = self._event_message_dict(payload)
        if message is None:
            return False
        content = message.get("content")
        if isinstance(content, dict):
            item_type = str(content.get("type", "")).strip().lower()
            if item_type == "text" and bool(str(content.get("text", "")).strip()):
                return True
            return False
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    item_type = str(item.get("type", "")).strip().lower()
                    if item_type == "text" and bool(str(item.get("text", "")).strip()):
                        return True
            return False
        return False

    def _evaluate_trajectory_tail_completeness(self, jsonl_path: Path) -> tuple[bool, str]:
        analysis = self._analyze_jsonl_file(jsonl_path)
        if not analysis.readable:
            return False, "Trajectory file unreadable"

        if analysis.last_semantic_line is None:
            return False, "No valid message event detected"

        last_line_no = analysis.last_semantic_line
        last_role = analysis.last_semantic_role or ""
        if last_role != "assistant":
            return False, f"Last message event role is not assistant (Line number: {last_line_no}, role={last_role or 'unknown'})"

        if not analysis.last_semantic_has_assistant_text:
            return False, f"Last message event content did not detect type=text (Line number: {last_line_no})"

        return True, f"Last message event ends with assistant text (Line number: {last_line_no})"

    def _check_latest_trajectory_file_completeness(self, section: CheckSection, bundle_dirs: Iterable[Path]) -> None:
        timestamp_candidates: list[tuple[datetime, str, int, Path]] = []
        for bundle_dir in sorted(bundle_dirs, key=lambda p: p.as_posix().lower()):
            if not bundle_dir.is_dir():
                continue
            for jsonl_path in self._collect_first_layer_jsonl_files(bundle_dir):
                if jsonl_path.name.lower() == "memory.jsonl":
                    continue
                latest_dt, latest_raw, latest_line_no = self._extract_latest_timestamp_from_jsonl(jsonl_path)
                if latest_dt is None or latest_raw is None or latest_line_no is None:
                    continue
                timestamp_candidates.append((latest_dt, latest_raw, latest_line_no, jsonl_path))

        if not timestamp_candidates:
            section.add_warn(
                "Latest trajectory integrity check skipped: no usable timestamp found in original_sessions first layer jsonl",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        timestamp_candidates.sort(key=lambda item: (item[0], item[3].as_posix().lower()), reverse=True)
        latest_dt, latest_raw, latest_line_no, latest_jsonl = timestamp_candidates[0]
        complete, detail = self._evaluate_trajectory_tail_completeness(latest_jsonl)

        if complete:
            section.add_pass(
                (
                    "Latest trajectory integrity check passed "
                    f"(Target file={latest_jsonl.name}, latest timestamp in file={latest_raw}, Line number={latest_line_no}, "
                    f"window base time (UTC)={latest_dt.isoformat()}): {detail}"
                ),
                self._rel(latest_jsonl),
            )
            return

        section.add_fail(
            (
                "Latest trajectory integrity check failed "
                f"(Target file={latest_jsonl.name}, latest timestamp in file={latest_raw}, Line number={latest_line_no}, "
                f"window base time (UTC)={latest_dt.isoformat()}): {detail}"
            ),
            self._rel(latest_jsonl),
        )

    def _extract_first_timestamp_from_jsonl(self, jsonl_path: Path) -> tuple[str | None, int | None]:
        analysis = self._analyze_jsonl_file(jsonl_path)
        if not analysis.readable:
            return None, None
        return analysis.first_timestamp_raw, analysis.first_timestamp_line

    def _timestamp_order_key(self, timestamp_text: str) -> tuple[int, str]:
        raw = timestamp_text.strip()
        if not raw:
            return (2, "")

        candidate = raw
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return (0, parsed.isoformat())
        except ValueError:
            return (1, raw)

    def _parse_timestamp_to_utc(self, timestamp_text: str) -> datetime | None:
        raw = timestamp_text.strip()
        if not raw:
            return None

        candidate = raw
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"

        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _stringify_session_message_content(self, content: object) -> str:
        chunks: list[str] = []

        def _collect(node: object) -> None:
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

    def _strip_read_tool_line_number_prefixes(self, text: str) -> str:
        # Read 工具输出常见 `12→`  rows号前缀, 移除后更接近原始文件文本。
        return re.sub(r"(?m)^\s*\d+\s*→\s*", "", text)

    def _extract_candidate_content_from_user_payload(self, payload: dict[str, object], message: dict[str, object]) -> str:
        # 默认候选：message.content
        fallback = self._stringify_session_message_content(message.get("content"))
        fallback = self._strip_read_tool_line_number_prefixes(fallback)

        # 工具读取文件场景：优先使用 toolUseResult.file.content（原文, 无 rows号)。
        tool_use_result = payload.get("toolUseResult")
        if isinstance(tool_use_result, dict):
            file_obj = tool_use_result.get("file")
            if isinstance(file_obj, dict):
                file_content = file_obj.get("content")
                if isinstance(file_content, str) and file_content.strip():
                    return file_content

            tur_content = tool_use_result.get("content")
            if isinstance(tur_content, str) and tur_content.strip():
                return self._strip_read_tool_line_number_prefixes(tur_content)

        return fallback

    def _is_session_prompt_compare_exempt_content(self, content_text: str) -> bool:
        stripped = content_text.lstrip().lower()
        if not stripped:
            return False
        return any(stripped.startswith(prefix) for prefix in SESSION_PROMPT_EXEMPT_CONTENT_PREFIXES)

    def _is_session_prompt_compare_exempt_payload(
        self, payload: dict[str, object], content_text: str
    ) -> bool:
        if self._is_local_command_noise_user_message(payload):
            return True
        return self._is_session_prompt_compare_exempt_content(content_text)

    def _collect_user_content_candidates_in_window_from_jsonl(
        self,
        jsonl_path: Path,
        window_minutes: int = SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES,
    ) -> tuple[list[tuple[str, int, str]], int, int]:
        analysis = self._analyze_jsonl_file(jsonl_path)
        if not analysis.readable:
            return [], 0, 0

        candidates: list[tuple[str, int, str]] = []
        window_start: datetime | None = None
        window_end: datetime | None = None

        for content_text, line_no, timestamp_raw, parsed_ts in analysis.user_candidates:
            if window_start is None:
                window_start = parsed_ts
                window_end = parsed_ts + timedelta(minutes=window_minutes)
                candidates.append((content_text, line_no, timestamp_raw))
                continue

            if window_end is None:
                continue
            if parsed_ts < window_start:
                continue
            if parsed_ts <= window_end:
                candidates.append((content_text, line_no, timestamp_raw))

        return candidates, analysis.exempt_user_line_count, analysis.user_line_count

    def _check_original_sessions_prompt_anchor_consistency(
        self, section: CheckSection, bundle_dirs: Iterable[Path]
    ) -> None:
        prompt_text, prompt_source_path, prompt_error = self._read_metadata_prompt_for_session_compare()
        if prompt_error is not None or prompt_text is None:
            section.add_warn(f"metadata.prompt anchor comparison skipped: {prompt_error}", self._rel(prompt_source_path))
            return

        timestamp_candidates: list[tuple[tuple[int, str], Path, str, int]] = []
        for bundle_dir in sorted(bundle_dirs, key=lambda p: p.as_posix().lower()):
            if not bundle_dir.is_dir():
                continue
            for jsonl_path in self._collect_first_layer_jsonl_files(bundle_dir):
                first_timestamp, line_no = self._extract_first_timestamp_from_jsonl(jsonl_path)
                if first_timestamp is None or line_no is None:
                    continue
                timestamp_candidates.append(
                    (self._timestamp_order_key(first_timestamp), jsonl_path, first_timestamp, line_no)
                )

        if not timestamp_candidates:
            section.add_warn(
                "metadata.prompt anchor comparison skipped: no usable timestamp found in original_sessions first layer jsonl",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        timestamp_candidates.sort(key=lambda item: (item[0], item[1].as_posix().lower()))
        skipped_exempt_files = 0
        skipped_unusable_files = 0

        for _, selected_jsonl, first_timestamp, first_timestamp_line in timestamp_candidates:
            window_candidates, exempt_line_count, user_line_count = (
                self._collect_user_content_candidates_in_window_from_jsonl(
                    selected_jsonl,
                    window_minutes=SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES,
                )
            )
            if not window_candidates:
                if user_line_count > 0 and user_line_count == exempt_line_count:
                    skipped_exempt_files += 1
                else:
                    skipped_unusable_files += 1
                continue

            skipped_note = ""
            if skipped_exempt_files > 0:
                skipped_note = f", skipped {skipped_exempt_files} earlier files containing only exempt user content"

            window_start_ts = window_candidates[0][2]
            window_start_line = window_candidates[0][1]

            best_similarity = -1.0
            best_line_no: int | None = None
            best_timestamp: str | None = None
            best_diff_index = -1
            best_match_mode = ""
            no_match_reason: str | None = None

            for idx, (candidate_content, candidate_line_no, candidate_timestamp) in enumerate(window_candidates, start=1):
                compare_result = self._compare_by_anchor(
                    prompt_text,
                    candidate_content,
                    anchor_len=SESSION_PROMPT_ANCHOR_LEN,
                    similarity_threshold=SESSION_PROMPT_SIMILARITY_THRESHOLD,
                )
                matched_by_anchor = bool(compare_result.get("matched"))
                similarity = float(compare_result.get("similarity", 0.0))
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_line_no = candidate_line_no
                    best_timestamp = candidate_timestamp
                    best_diff_index = int(compare_result.get("diff_index", -1))
                    best_match_mode = "Anchor matched" if matched_by_anchor else "Anchor not matched (full-segment similarity)"

                if not matched_by_anchor and no_match_reason is None:
                    no_match_reason = str(compare_result.get("reason", "Anchor mode not matched"))

                if bool(compare_result.get("near_duplicate")):
                    pass_mode = "Anchor matched" if matched_by_anchor else "Anchor not matched (full-segment similarity fallback)"
                    section.add_pass(
                        (
                            "metadata.prompt anchor comparison passed "
                            f"（similarity={similarity:.6f} >= threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f})"
                            f"(Check method={pass_mode})"
                            " "
                            f"(Target file={selected_jsonl.name}, first timestamp in file={first_timestamp}, first timestamp line number in file={first_timestamp_line}, "
                            f"window start timestamp={window_start_ts}, window start line number={window_start_line}, "
                            f"match line number={candidate_line_no}, window candidate count={len(window_candidates)}, match index={idx}{skipped_note})"
                        ),
                        self._rel(selected_jsonl),
                    )
                    return

            if best_similarity >= 0 and best_line_no is not None and best_timestamp is not None:
                diff_note = ""
                if best_diff_index >= 0:
                    diff_note = f", first difference position={best_diff_index}"
                section.add_fail(
                    (
                        "metadata.prompt anchor comparison failed: insufficient similarity "
                        f"（similarity={best_similarity:.6f} < threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f}{diff_note}, check method={best_match_mode or 'Unknown'})"
                        " "
                        f"(Target file={selected_jsonl.name}, first timestamp in file={first_timestamp}, first timestamp line number in file={first_timestamp_line}, "
                        f"window start timestamp={window_start_ts}, window start line number={window_start_line}, "
                        f"best candidate timestamp={best_timestamp}, best candidate line number={best_line_no}, window candidate count={len(window_candidates)}{skipped_note})"
                    ),
                    self._rel(selected_jsonl),
                )
                return

            reason = no_match_reason or "No candidate content within window matched anchor mode"
            section.add_fail(
                (
                    f"metadata.prompt anchor comparison failed: {reason} "
                    f"(Target file={selected_jsonl.name}, first timestamp in file={first_timestamp}, first timestamp line number in file={first_timestamp_line}, "
                    f"window start timestamp={window_start_ts}, window start line number={window_start_line}, window candidate count={len(window_candidates)}{skipped_note})"
                ),
                self._rel(selected_jsonl),
            )
            return

        if skipped_exempt_files > 0 or skipped_unusable_files > 0:
            section.add_warn(
                (
                    "metadata.prompt anchor comparison skipped: no usable user content found; "
                    f"contains only exempt content files={skipped_exempt_files}, unusable file={skipped_unusable_files}"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        section.add_warn(
            "metadata.prompt anchor comparison skipped: candidate jsonl has no usable user content for comparison",
            self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
        )

    def _collect_all_original_sessions_jsonl_files(self, sessions_dir: Path) -> list[Path]:
        jsonl_files: list[Path] = []
        if not sessions_dir.is_dir():
            return jsonl_files

        for current_root, dirs, files in os.walk(sessions_dir, topdown=True):
            dirs.sort(key=str.lower)
            current_path = Path(current_root)
            pruned_dirs: list[str] = []
            for dirname in dirs:
                if self._is_runtime_noise_dir_name(dirname):
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for filename in files:
                if filename.lower().endswith(".jsonl"):
                    jsonl_files.append(current_path / filename)
        jsonl_files.sort(key=lambda p: p.as_posix().lower())
        return jsonl_files

    def _check_original_sessions_jsonl_forbidden_keywords(self, section: CheckSection, sessions_dir: Path) -> None:
        jsonl_files = self._collect_all_original_sessions_jsonl_files(sessions_dir)
        if not jsonl_files:
            section.add_warn("no jsonl files detected in original_sessions, skipping forbidden keyword check", self._rel(sessions_dir))
            return

        findings = 0
        for jsonl_path in jsonl_files:
            cache_key = self._jsonl_analysis_cache_key(jsonl_path)
            full_analysis = self._jsonl_analysis_cache.get(cache_key)
            usage_analysis = self._jsonl_usage_analysis_cache.get(cache_key)

            keyword_lines_map: dict[str, list[int]] | None = None
            readable = False
            if full_analysis is not None:
                readable = full_analysis.readable
                keyword_lines_map = full_analysis.keyword_line_numbers
            elif usage_analysis is not None:
                readable = usage_analysis.readable
                keyword_lines_map = usage_analysis.keyword_line_numbers
            else:
                content = self._read_text(jsonl_path)
                if content is not None:
                    readable = True
                    keyword_lines_map = self._scan_keyword_line_numbers_from_text(content)

            if not readable or keyword_lines_map is None:
                section.add_warn("jsonl file unreadable, skipped forbidden keyword check", self._rel(jsonl_path))
                continue

            for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS:
                line_numbers = keyword_lines_map.get(keyword, [])
                if not line_numbers:
                    continue
                findings += 1
                formatted = self._format_line_numbers(line_numbers)
                section.add_fail(
                    f'Detected forbidden keyword "{keyword}" (Line number: {formatted})',
                    self._rel(jsonl_path),
                )

        if findings == 0:
            section.add_pass("no forbidden keywords detected in original_sessions/*.jsonl", self._rel(sessions_dir))

    def _analyze_bundle_token_usage(
        self, bundle_dir: Path
    ) -> tuple[dict[str, int], dict[str, int], int, int, int, int]:
        bundle_totals = self._new_usage_totals()
        subagent_totals = self._new_usage_totals()
        session_jsonl_count = 0
        subagent_jsonl_count = 0
        usage_record_count = 0
        unreadable_count = 0

        for main_jsonl in self._collect_first_layer_jsonl_files(bundle_dir):
            session_jsonl_count += 1
            main_analysis = self._analyze_jsonl_file(main_jsonl)
            if not main_analysis.readable:
                unreadable_count += 1
            else:
                self._add_usage_totals(bundle_totals, main_analysis.usage_totals)
                usage_record_count += main_analysis.usage_record_count

            subagents_dir = bundle_dir / main_jsonl.stem / "subagents"
            if not subagents_dir.is_dir():
                continue

            for sub_jsonl in sorted(subagents_dir.glob("*.jsonl"), key=lambda p: p.name.lower()):
                subagent_jsonl_count += 1
                sub_analysis = self._analyze_jsonl_usage_only(sub_jsonl)
                if not sub_analysis.readable:
                    unreadable_count += 1
                    continue
                self._add_usage_totals(bundle_totals, sub_analysis.usage_totals)
                self._add_usage_totals(subagent_totals, sub_analysis.usage_totals)
                usage_record_count += sub_analysis.usage_record_count

        return (
            bundle_totals,
            subagent_totals,
            session_jsonl_count,
            subagent_jsonl_count,
            usage_record_count,
            unreadable_count,
        )

    def _check_original_sessions_token_usage(self, section: CheckSection, bundle_dirs: Iterable[Path]) -> None:
        assert self.root is not None
        total_tokens = self._new_usage_totals()
        total_sub_tokens = self._new_usage_totals()
        total_session_files = 0
        total_subagent_files = 0
        total_usage_records = 0
        total_unreadable_files = 0
        checked_bundles = 0

        for bundle_dir in sorted(bundle_dirs, key=lambda p: p.as_posix().lower()):
            if not bundle_dir.is_dir():
                continue
            (
                bundle_tokens,
                bundle_sub_tokens,
                session_jsonl_count,
                subagent_jsonl_count,
                usage_record_count,
                unreadable_count,
            ) = self._analyze_bundle_token_usage(bundle_dir)
            if session_jsonl_count == 0:
                section.add_warn("token statistics skipped: no jsonl files detected in first layer", self._rel(bundle_dir))
                continue

            checked_bundles += 1
            total_session_files += session_jsonl_count
            total_subagent_files += subagent_jsonl_count
            total_usage_records += usage_record_count
            total_unreadable_files += unreadable_count
            self._add_usage_totals(total_tokens, bundle_tokens)
            self._add_usage_totals(total_sub_tokens, bundle_sub_tokens)

            bundle_total_sum = sum(bundle_tokens.values())
            bundle_sub_sum = sum(bundle_sub_tokens.values())
            bundle_sub_ratio = (bundle_sub_sum / bundle_total_sum * 100) if bundle_total_sum > 0 else 0.0
            bundle_cost_usd = self._calc_token_cost_usd(bundle_tokens)
            section.add_pass(
                (
                    f"{bundle_dir.name}/ token statistics: "
                    f"session_jsonl={session_jsonl_count}, "
                    f"subagent_jsonl={subagent_jsonl_count}, "
                    f"usage_records={usage_record_count:,}, "
                    f"input={bundle_tokens['input_tokens']:,}, "
                    f"output={bundle_tokens['output_tokens']:,}, "
                    f"cache_read={bundle_tokens['cache_read_tokens']:,}, "
                    f"cache_write={bundle_tokens['cache_write_tokens']:,}, "
                    f"total={bundle_total_sum:,}, "
                    f"subagent_total={bundle_sub_sum:,}（{bundle_sub_ratio:.1f}%), "
                    f"cost_usd=${bundle_cost_usd:,.4f}"
                ),
                self._rel(bundle_dir),
            )
            if unreadable_count > 0:
                section.add_warn(
                    f"{bundle_dir.name}/ token statistics has {unreadable_count} jsonl unreadable, skipped",
                    self._rel(bundle_dir),
                )

        if checked_bundles == 0:
            section.add_warn(
                "original_sessions token statistics skipped: no countable first layer jsonl detected in original_sessions",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        overall_total_sum = sum(total_tokens.values())
        overall_sub_sum = sum(total_sub_tokens.values())
        sub_ratio = (overall_sub_sum / overall_total_sum * 100) if overall_total_sum > 0 else 0.0
        overall_cost_usd = self._calc_token_cost_usd(total_tokens)
        threshold_usd, threshold_project_type = self._resolve_token_cost_threshold()

        if checked_bundles > 1:
            section.add_pass(
                (
                    "original_sessions token summary:"
                    f"bundle={checked_bundles}, "
                    f"session_jsonl={total_session_files}, "
                    f"subagent_jsonl={total_subagent_files}, "
                    f"usage_records={total_usage_records:,}, "
                    f"input={total_tokens['input_tokens']:,}, "
                    f"output={total_tokens['output_tokens']:,}, "
                    f"cache_read={total_tokens['cache_read_tokens']:,}, "
                    f"cache_write={total_tokens['cache_write_tokens']:,}, "
                    f"total={overall_total_sum:,}, "
                    f"subagent_total={overall_sub_sum:,}（{sub_ratio:.1f}%), "
                    f"cost_usd=${overall_cost_usd:,.4f}"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

        if overall_cost_usd < threshold_usd:
            section.add_fail(
                (
                    "original_sessions task development cost check failed: "
                    f"cost_usd=${overall_cost_usd:,.4f} < threshold=${threshold_usd:,.2f}"
                    f"（project_type={threshold_project_type})"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
        else:
            section.add_pass(
                (
                    "original_sessions task development cost check passed: "
                    f"cost_usd=${overall_cost_usd:,.4f} >= threshold=${threshold_usd:,.2f}"
                    f"（project_type={threshold_project_type})"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

        if total_unreadable_files > 0:
            section.add_warn(
                f"original_sessions token summary contains {total_unreadable_files} jsonl unreadable, skipped",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

    def _check_leading_bundle_dir_structure(self, section: CheckSection, bundle_dir: Path) -> None:
        bundle_failures = 0
        child_dirs = sorted([path for path in bundle_dir.iterdir() if path.is_dir()], key=lambda p: p.name.lower())
        exempt_child_dirs = [path for path in child_dirs if path.name.lower() in LEADING_BUNDLE_EXEMPT_DIR_NAMES]
        session_dirs = [path for path in child_dirs if path.name.lower() not in LEADING_BUNDLE_EXEMPT_DIR_NAMES]

        for exempt_dir in exempt_child_dirs:
            if exempt_dir.name.lower() == "memory":
                section.add_pass(
                    f"{bundle_dir.name}/ memory/ is exempt: no content check, no memory.jsonl alignment required",
                    self._rel(exempt_dir),
                )

        if not session_dirs:
            if exempt_child_dirs:
                section.add_pass(
                    f"{bundle_dir.name}/ no session_id subdirectories to check detected (exempt dirs skipped)",
                    self._rel(bundle_dir),
                )
            else:
                section.add_warn(f"{bundle_dir.name}/ no session_id subdirectories detected", self._rel(bundle_dir))
            return

        for session_dir in session_dirs:
            session_id = session_dir.name
            required_jsonl = bundle_dir / f"{session_id}.jsonl"
            if not required_jsonl.is_file():
                section.add_fail(
                    f"{bundle_dir.name}/ directory {session_id}/ missing session file with same name {session_id}.jsonl",
                    self._rel(session_dir),
                )
                bundle_failures += 1

            subagents_dir = session_dir / "subagents"
            if not subagents_dir.is_dir():
                section.add_pass(
                    f"{session_id}/ no subagents/ detected (directory is optional, skipped)",
                    self._rel(session_dir),
                )
                continue

            subagent_jsonl_files = sorted(
                [path for path in subagents_dir.iterdir() if path.is_file() and path.suffix.lower() == ".jsonl"],
                key=lambda p: p.name.lower(),
            )
            for jsonl_file in subagent_jsonl_files:
                stem = jsonl_file.stem
                lower_stem = stem.lower()
                if not lower_stem.startswith("agent-"):
                    continue
                if self._is_subagent_meta_exempt_stem(stem):
                    continue

                expected_meta = subagents_dir / f"{stem}.meta.json"
                if not expected_meta.is_file():
                    section.add_fail(
                        f"{jsonl_file.name} missing corresponding metadata file {expected_meta.name}",
                        self._rel(jsonl_file),
                    )
                    bundle_failures += 1

        if bundle_failures == 0:
            section.add_pass(f"{bundle_dir.name}/ subdirectory structure check passed", self._rel(bundle_dir))

    def _is_original_sessions_memory_exempt_path(self, path: Path) -> bool:
        if self.root is None:
            return False
        try:
            rel_parts = [part.lower() for part in path.resolve().relative_to(self.root).parts]
        except ValueError:
            return False

        # 匹配 original_sessions/<bundle>/memory[/...]
        if len(rel_parts) >= 3 and rel_parts[0] == ORIGINAL_SESSIONS_DIR_NAME and rel_parts[2] == "memory":
            return True
        # 匹配 original_sessions/<bundle>/memory.jsonl
        if len(rel_parts) == 3 and rel_parts[0] == ORIGINAL_SESSIONS_DIR_NAME and rel_parts[2] == "memory.jsonl":
            return True
        return False

    def _collect_prompt_file_candidates(self) -> list[Path]:
        assert self.root is not None
        correct, misplaced, typos = self._collect_required_file_candidates(
            "prompt.md",
            ROOT_REQUIRED_FILE_TYPO_ALIASES.get("prompt.md", set()),
        )
        candidates: list[Path] = []
        seen: set[Path] = set()
        for path in correct + misplaced + typos:
            if path in seen:
                continue
            seen.add(path)
            candidates.append(path)
        candidates.sort(key=lambda p: p.as_posix().lower())
        return candidates

    def _resolve_prompt_content_from_files(self) -> tuple[str, Path | None]:
        candidates = self._collect_prompt_file_candidates()
        prioritized: list[Path] = []
        tail: list[Path] = []
        for path in candidates:
            lower = path.name.lower()
            if lower == "prompts.md":
                prioritized.insert(0, path)
                continue
            if lower == "prompt.md":
                if not prioritized or prioritized[0].name.lower() != "prompts.md":
                    prioritized.insert(0, path)
                else:
                    prioritized.append(path)
                continue
            tail.append(path)

        ordered_candidates = prioritized + tail
        for path in ordered_candidates:
            content = self._read_text(path)
            if content is None:
                continue
            return content, path
        return "", None

    def _upsert_metadata_file(
        self,
        path: Path,
        mode: str = "repair",
    ) -> tuple[str, str]:
        current: dict[str, object] = {}
        if path.exists():
            if path.is_dir():
                return "FAIL", "metadata.json path is a directory, cannot write"
            content = self._read_text(path)
            if content is None:
                return "FAIL", "metadata.json is unreadable text, cannot auto-complete"
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                return "FAIL", f"metadata.json invalid JSON: {exc.msg}"
            if not isinstance(parsed, dict):
                return "FAIL", "metadata.json top-level is not object, cannot auto-complete"
            current = dict(parsed)

        defaults = self._build_metadata_defaults(current)
        prompt_content, prompt_source = self._resolve_prompt_content_from_files()
        changed_keys: list[str] = []
        if mode == "legacy_convert":
            for key in METADATA_REQUIRED_KEYS:
                if key == "prompt":
                    value = current.get(key)
                    desired_prompt = prompt_content if prompt_content else defaults["prompt"]
                    if value is None or (isinstance(value, str) and not value.strip()):
                        current[key] = desired_prompt
                        changed_keys.append(key)
                    continue

                if key == "project_type":
                    value = current.get(key)
                    if value is None or (isinstance(value, str) and not value.strip()):
                        current[key] = self._infer_metadata_project_type(current)
                        changed_keys.append(key)
                    continue

                value = current.get(key)
                if value is None:
                    current[key] = defaults[key]
                    changed_keys.append(key)
                    continue
                if isinstance(value, str) and not value.strip():
                    current[key] = defaults[key]
                    changed_keys.append(key)
        else:
            for key in METADATA_REQUIRED_KEYS:
                value = current.get(key)
                if key == "prompt":
                    desired_prompt = prompt_content if prompt_content else defaults["prompt"]
                    if value is None or (isinstance(value, str) and not value.strip()):
                        current[key] = desired_prompt
                        changed_keys.append(key)
                    continue

                if value is None:
                    current[key] = defaults[key]
                    changed_keys.append(key)
                    continue
                if isinstance(value, str) and not value.strip():
                    current[key] = defaults[key]
                    changed_keys.append(key)

        if not changed_keys and path.exists():
            return "SKIP", "metadata.json required fields complete"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return "FAIL", str(exc)

        if changed_keys:
            prompt_note = ""
            if prompt_source is not None and "prompt" in changed_keys:
                prompt_note = f"; prompt source: {self._rel(prompt_source)}"
            return "DONE", "Fill in field: " + ", ".join(changed_keys) + prompt_note
        return "DONE", "Create metadata.json"

    def _execute_repair_actions(
        self,
        actions: list[RepairAction],
        backup_on_move: bool = False,
    ) -> tuple[int, int, int, Path | None]:
        assert self.root is not None
        root = self.root

        executed = 0
        skipped = 0
        failed = 0
        backup_run_dir: Path | None = None

        def _ensure_backup_dir() -> Path:
            nonlocal backup_run_dir
            if backup_run_dir is None:
                backup_run_dir = root / ".backup"
                backup_run_dir.mkdir(parents=True, exist_ok=True)
            return backup_run_dir

        def _unique_merge_target(base_dir: Path, name: str, scope_label: str) -> Path:
            candidate = base_dir / name
            if not candidate.exists():
                return candidate

            stem = Path(name).stem
            suffix = Path(name).suffix
            prefixed = f"{scope_label}-{stem}" if stem else f"{scope_label}-{name}"
            idx = 1
            while True:
                alt_name = f"{prefixed}{suffix}" if idx == 1 else f"{prefixed}-{idx}{suffix}"
                alt = base_dir / alt_name
                if not alt.exists():
                    return alt
                idx += 1

        for action in actions:
            src = action.src
            dst = action.dst

            if action.kind == "upsert_metadata":
                mode = str(action.options.get("mode", "repair"))
                status, detail = self._upsert_metadata_file(src, mode=mode)
                if status == "DONE":
                    print(f"[DONE] UPSERT_METADATA {self._rel(src)} | {detail}")
                    executed += 1
                elif status == "SKIP":
                    print(f"[SKIP] UPSERT_METADATA {self._rel(src)} | {detail}")
                    skipped += 1
                else:
                    print(f"[FAIL] UPSERT_METADATA {self._rel(src)} | {detail}")
                    failed += 1
                continue

            if action.kind == "update_gitignore":
                if src.exists() and src.is_dir():
                    print(f"[FAIL] UPDATE {self._rel(src)} | Target is a directory, cannot write to .gitignore")
                    failed += 1
                    continue

                if src.exists():
                    content = self._read_text(src)
                    if content is None:
                        print(f"[FAIL] UPDATE {self._rel(src)} | .gitignore is unreadable, cannot update automatically")
                        failed += 1
                        continue
                else:
                    content = ""

                updated_content, additions = self._build_gitignore_content_with_exemptions(content)
                if not additions:
                    print(f"[SKIP] UPDATE {self._rel(src)} | Exemption rule already exists")
                    skipped += 1
                    continue

                try:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text(updated_content, encoding="utf-8")
                    print(
                        f"[DONE] UPDATE {self._rel(src)} | Added rule: {', '.join(additions)}"
                    )
                    executed += 1
                except OSError as exc:
                    print(f"[FAIL] UPDATE {self._rel(src)} | {exc}")
                    failed += 1
                continue

            if action.kind == "merge_tmp_dir":
                merge_src = src
                merge_dst = dst if dst is not None else (root / ".tmp")
                if not merge_src.exists():
                    print(f"[SKIP] MERGE_TMP {self._rel(merge_src)} -> {self._rel(merge_dst)} | Source directory does not exist")
                    skipped += 1
                    continue
                if not merge_src.is_dir():
                    print(f"[FAIL] MERGE_TMP {self._rel(merge_src)} | Source path is not a directory")
                    failed += 1
                    continue

                try:
                    merge_dst.mkdir(parents=True, exist_ok=True)

                    moved_count = 0
                    renamed_count = 0
                    scope_label = merge_src.parent.name or "scope"
                    children = sorted(merge_src.iterdir(), key=lambda p: p.name.lower())
                    for child in children:
                        target = merge_dst / child.name
                        if target.exists():
                            target = _unique_merge_target(merge_dst, child.name, scope_label)
                            renamed_count += 1
                        shutil.move(str(child), str(target))
                        moved_count += 1

                    residual_kept = False
                    if merge_src.exists():
                        try:
                            merge_src.rmdir()
                        except OSError:
                            # Avoid deleting residual content in merge flow.
                            # Content deletion must go through delete action (with backup).
                            residual_kept = True

                    if residual_kept:
                        print(
                            f"[DONE] MERGE_TMP {self._rel(merge_src)} -> {self._rel(merge_dst)} | moved={moved_count} renamed={renamed_count} residual_kept=true"
                        )
                    else:
                        print(
                            f"[DONE] MERGE_TMP {self._rel(merge_src)} -> {self._rel(merge_dst)} | moved={moved_count} renamed={renamed_count}"
                        )
                    executed += 1
                except OSError as exc:
                    print(f"[FAIL] MERGE_TMP {self._rel(merge_src)} -> {self._rel(merge_dst)} | {exc}")
                    failed += 1
                continue

            if action.kind == "delete":
                exempt, exempt_reason = self._is_repair_delete_exempt(src)
                if exempt:
                    print(f"[SKIP][EXEMPT] DELETE {self._rel(src)} | {exempt_reason}")
                    skipped += 1
                    continue

                if not src.exists():
                    print(f"[SKIP] DELETE {self._rel(src)} | Source path does not exist")
                    skipped += 1
                    continue

                try:
                    self._backup_original_path(src, _ensure_backup_dir())
                    if src.is_dir():
                        shutil.rmtree(src)
                    else:
                        src.unlink()
                    print(f"[DONE] DELETE {self._rel(src)}")
                    executed += 1
                except OSError as exc:
                    print(f"[FAIL] DELETE {self._rel(src)} | {exc}")
                    failed += 1
                continue

            if dst is None:
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} | Target path is empty")
                skipped += 1
                continue

            if not src.exists():
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} -> {self._rel(dst)} | Source path does not exist")
                skipped += 1
                continue

            if dst.exists():
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} -> {self._rel(dst)} | Target already exists")
                skipped += 1
                continue

            try:
                if backup_on_move:
                    self._backup_original_path(src, _ensure_backup_dir())
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                verb = "RENAME" if action.kind == "rename" else "MOVE"
                print(f"[DONE] {verb} {self._rel(src)} -> {self._rel(dst)}")
                executed += 1
            except OSError as exc:
                verb = "RENAME" if action.kind == "rename" else "MOVE"
                print(f"[FAIL] {verb} {self._rel(src)} -> {self._rel(dst)} | {exc}")
                failed += 1

        return executed, skipped, failed, backup_run_dir

    def _new_section(self, title: str) -> CheckSection:
        section = CheckSection(title=title)
        self.sections.append(section)
        return section

    def _rel(self, path: Path | str) -> str:
        if isinstance(path, str):
            return path.replace("\\", "/")
        if self.root is None:
            return path.as_posix()
        try:
            rel = path.relative_to(self.root)
            text = rel.as_posix()
            return text if text else "."
        except ValueError:
            return path.as_posix()

    def _record_failures(self) -> None:
        self.error_count = sum(1 for section in self.sections for item in section.items if item.status == "FAIL")

    def _resolve_input_directory(self) -> Path | None:
        candidate = Path(self.input_identifier).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        if not candidate.is_absolute():
            local_candidate = (Path.cwd() / self.input_identifier).resolve()
            if local_candidate.is_dir():
                return local_candidate
        return None

    def _check_input_directory(self) -> None:
        section = self._new_section("1. Input Directory Check")
        resolved = self._resolve_input_directory()
        if resolved is None:
            section.add_fail(
                f"Input directory does not exist or is inaccessible: {self.input_identifier}",
                self.input_identifier,
            )
            self._record_failures()
            return

        self.root = resolved
        self._gitignore_scopes_cache = None
        self._candidate_entries_cache = None
        self._dirty_findings_cache = None
        section.add_pass("Input directory is valid", self._rel(resolved))
        self._record_failures()

    def _is_similar_filename(self, candidate_name: str, expected_name: str) -> bool:
        c = candidate_name.lower()
        e = expected_name.lower()
        if Path(c).suffix.lower() != Path(e).suffix.lower():
            return False
        score_name = difflib.SequenceMatcher(None, c, e).ratio()
        score_stem = difflib.SequenceMatcher(None, Path(c).stem, Path(e).stem).ratio()
        return max(score_name, score_stem) >= 0.84

    def _iter_candidate_entries_for_root_requirements(self) -> Iterable[tuple[Path, bool, bool, str]]:
        assert self.root is not None
        if self._candidate_entries_cache is None:
            entries: list[tuple[Path, bool, bool, str]] = []
            for current_root, dirs, files in os.walk(self.root, topdown=True):
                current_path = Path(current_root)
                pruned_dirs: list[str] = []
                for dirname in dirs:
                    if self._should_skip_heavy_walk_dir(dirname):
                        continue
                    dir_path = current_path / dirname
                    if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                        continue
                    pruned_dirs.append(dirname)
                dirs[:] = pruned_dirs
                for dirname in dirs:
                    path = current_path / dirname
                    entries.append((path, False, True, dirname.lower()))
                for filename in files:
                    path = current_path / filename
                    entries.append((path, True, False, filename.lower()))
            entries.sort(key=lambda item: item[0].as_posix().lower())
            self._candidate_entries_cache = entries

        for entry in self._candidate_entries_cache:
            yield entry

    def _collect_required_file_candidates(
        self,
        expected_name: str,
        typo_aliases: set[str],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        assert self.root is not None
        expected_lower = expected_name.lower()

        correct_at_root: list[Path] = []
        exact_wrong_location: list[Path] = []
        typo_candidates: list[Path] = []

        normalized_aliases = {alias.lower() for alias in typo_aliases}
        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR_NAME

        for path, is_file, _, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_file:
                continue

            if expected_lower == "trajectory.json":
                try:
                    path.relative_to(sessions_dir)
                    # original_sessions/ 下的文件由会话归档规则单独检查；
                    # 这里仅扫描根目录及其它错位路径中的旧 trajectory 命名。
                    continue
                except ValueError:
                    pass

            if name_lower == expected_lower:
                if path.parent == self.root:
                    correct_at_root.append(path)
                else:
                    exact_wrong_location.append(path)
                continue

            # trajectory-N.json is a valid multi-trajectory filename and should
            # not be treated as a typo of trajectory.json.
            if expected_lower == "trajectory.json" and TRAJECTORY_MULTI_RE.fullmatch(name_lower):
                continue

            if name_lower in normalized_aliases or self._is_similar_filename(name_lower, expected_lower):
                typo_candidates.append(path)

        correct_at_root.sort(key=lambda p: p.as_posix().lower())
        exact_wrong_location.sort(key=lambda p: p.as_posix().lower())
        typo_candidates.sort(key=lambda p: p.as_posix().lower())
        return correct_at_root, exact_wrong_location, typo_candidates

    def _collect_required_dir_candidates(
        self,
        expected_dir_name: str,
        typo_aliases: set[str],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        assert self.root is not None
        expected_lower = expected_dir_name.lower()
        normalized_aliases = {alias.lower() for alias in typo_aliases}

        correct_at_root: list[Path] = []
        exact_wrong_location: list[Path] = []
        typo_candidates: list[Path] = []

        for path, _, is_dir, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_dir:
                continue

            if name_lower == expected_lower:
                if path.parent == self.root:
                    correct_at_root.append(path)
                else:
                    exact_wrong_location.append(path)
                continue

            score = difflib.SequenceMatcher(None, name_lower, expected_lower).ratio()
            if name_lower in normalized_aliases or score >= 0.78:
                typo_candidates.append(path)

        correct_at_root.sort(key=lambda p: p.as_posix().lower())
        exact_wrong_location.sort(key=lambda p: p.as_posix().lower())
        typo_candidates.sort(key=lambda p: p.as_posix().lower())
        return correct_at_root, exact_wrong_location, typo_candidates

    def _code_scope_roots(self) -> list[Path]:
        assert self.root is not None
        roots: list[Path] = [self.root / REPO_DIR_NAME]

        if self.project_type_dir is not None:
            roots.append(self.project_type_dir)

        legacy_dirs = self.legacy_project_dirs
        if not legacy_dirs:
            legacy_dirs = self._collect_legacy_project_directories(self.root)
        for _, path in legacy_dirs:
            roots.append(path)

        deduped: list[Path] = []
        seen: set[Path] = set()
        for root_path in roots:
            resolved = root_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            deduped.append(resolved)
        return deduped

    def _is_within_code_scope(self, path: Path) -> bool:
        target = path.resolve()
        for root_path in self._code_scope_roots():
            try:
                target.relative_to(root_path)
                return True
            except ValueError:
                continue
        return False

    def _filter_original_sessions_candidates(self, paths: list[Path]) -> list[Path]:
        filtered: list[Path] = []
        for path in paths:
            if self._is_within_code_scope(path):
                continue
            filtered.append(path)
        return filtered

    def _should_filter_code_scope_session_aliases(
        self,
        sessions_correct: list[Path],
        sessions_typos: list[Path],
    ) -> bool:
        assert self.root is not None
        for path in sessions_correct:
            if path.parent == self.root:
                return True
        for path in sessions_typos:
            if path.parent == self.root:
                return True
        return False

    def _collect_file_candidates_for_target_dir(
        self,
        expected_name: str,
        target_dir: Path,
        typo_aliases: set[str] | None = None,
    ) -> tuple[list[Path], list[Path], list[Path]]:
        expected_lower = expected_name.lower()
        normalized_aliases = {alias.lower() for alias in (typo_aliases or set())}

        correct_in_target: list[Path] = []
        exact_outside_target: list[Path] = []
        typo_candidates: list[Path] = []

        for path, is_file, _, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_file:
                continue
            if name_lower == expected_lower:
                if path.parent == target_dir:
                    correct_in_target.append(path)
                else:
                    exact_outside_target.append(path)
                continue

            if name_lower in normalized_aliases or self._is_similar_filename(name_lower, expected_lower):
                typo_candidates.append(path)

        correct_in_target.sort(key=lambda p: p.as_posix().lower())
        exact_outside_target.sort(key=lambda p: p.as_posix().lower())
        typo_candidates.sort(key=lambda p: p.as_posix().lower())
        return correct_in_target, exact_outside_target, typo_candidates

    def _report_root_required_file(
        self,
        section: CheckSection,
        expected_name: str,
        typo_aliases: set[str],
        max_items: int = 6,
    ) -> set[Path]:
        assert self.root is not None
        explained_paths: set[Path] = set()

        correct, misplaced, typo_candidates = self._collect_required_file_candidates(expected_name, typo_aliases)

        if len(correct) == 1:
            section.add_pass(f"Root directory has {expected_name}", self._rel(correct[0]))
        elif len(correct) > 1:
            section.add_fail(f"{expected_name} appears multiple times in root directory, only 1 allowed", self._rel(correct[0]))
            for dup in correct[1:max_items]:
                section.add_fail(f"{expected_name} appears repeatedly (root directory should not have multiples)", self._rel(dup))
                explained_paths.add(dup)

        if misplaced:
            for path in misplaced[:max_items]:
                parent = self._rel(path.parent)
                if correct:
                    section.add_fail(
                        f"{expected_name} location incorrect: correct file already exists in root directory, this file should not be placed in {parent}/",
                        self._rel(path),
                    )
                else:
                    section.add_fail(
                        f"{expected_name} placed incorrectly, should be in TASK root directory instead of {parent}/",
                        self._rel(path),
                    )
                explained_paths.add(path)

        if typo_candidates:
            for path in typo_candidates[:max_items]:
                parent = self._rel(path.parent)
                if path.parent == self.root:
                    section.add_fail(
                        f"{path.name} naming incorrect, should be named {expected_name} (Should be in TASK root directory)",
                        self._rel(path),
                    )
                else:
                    section.add_fail(
                        f"{path.name} naming incorrect, should be named {expected_name}, and should be in TASK root directory instead of {parent}/",
                        self._rel(path),
                    )
                explained_paths.add(path)

        if not correct and not misplaced and not typo_candidates:
            section.add_fail(f"Missing {expected_name}", expected_name)

        return explained_paths

    def _report_root_trajectory_presence(self, section: CheckSection) -> set[Path]:
        explained_paths: set[Path] = set()
        assert self.root is not None
        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR_NAME

        legacy_traj_root, legacy_traj_misplaced, legacy_traj_typos = self._collect_required_file_candidates(
            "trajectory.json",
            ROOT_REQUIRED_FILE_TYPO_ALIASES.get("trajectory.json", set()),
        )
        sessions_correct, sessions_misplaced, sessions_typos = self._collect_required_dir_candidates(
            ORIGINAL_SESSIONS_DIR_NAME,
            ROOT_REQUIRED_DIR_TYPO_ALIASES.get(ORIGINAL_SESSIONS_DIR_NAME, set()),
        )
        if self._should_filter_code_scope_session_aliases(sessions_correct, sessions_typos):
            sessions_misplaced = self._filter_original_sessions_candidates(sessions_misplaced)
            sessions_typos = self._filter_original_sessions_candidates(sessions_typos)

        if sessions_correct:
            section.add_pass(f"Root directory has {ORIGINAL_SESSIONS_DIR_NAME}/ directory", self._rel(sessions_correct[0]))
        if len(sessions_correct) > 1:
            section.add_fail(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ appears multiple times in root directory, only 1 allowed",
                self._rel(sessions_correct[0]),
            )
            for path in sessions_correct[1:6]:
                section.add_fail(f"{ORIGINAL_SESSIONS_DIR_NAME}/ appears repeatedly (root directory should not have multiples)", self._rel(path))
                explained_paths.add(path)

        if legacy_traj_root:
            for path in legacy_traj_root[:6]:
                section.add_fail(
                    f"Root directory should not have trajectory.json, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (suggest packing as archive)",
                    self._rel(path),
                )
                explained_paths.add(path)

        for path in legacy_traj_misplaced[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"trajectory.json is old naming file, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (currently located at {parent}/)",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in legacy_traj_typos[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{path.name} belongs to old naming style, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (currently located at {parent}/)",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in sessions_misplaced[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ location incorrect, should be in TASK root directory instead of {parent}/",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in sessions_typos[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{path.name}/ naming incorrect, should be named {ORIGINAL_SESSIONS_DIR_NAME}/, and place in TASK root directory (currently at {parent}/)",
                self._rel(path),
            )
            explained_paths.add(path)

        legacy_session_json_in_root: list[Path] = sorted(
            [
                p
                for p in self.root.iterdir()
                if p.is_file() and LEGACY_SESSION_JSON_RE.fullmatch(p.name.lower()) is not None
            ],
            key=lambda p: p.name.lower(),
        )

        for path in legacy_session_json_in_root[:6]:
            if path in explained_paths:
                continue
            parent = self._rel(path.parent)
            section.add_fail(
                f"Detected old session JSON file, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (currently located at {parent}/)",
                self._rel(path),
            )
            explained_paths.add(path)

        if not sessions_correct and not sessions_misplaced and not sessions_typos:
            section.add_fail(
                f"Missing {ORIGINAL_SESSIONS_DIR_NAME}/ directory (sessions should be unified here)",
                f"{ORIGINAL_SESSIONS_DIR_NAME}/",
            )

        return explained_paths

    def _root_extra_file_issue_message(self, path: Path) -> str:
        name_lower = path.name.lower()
        deprecated_prompt_names = set(ROOT_REQUIRED_FILE_TYPO_ALIASES.get("prompt.md", set())) | {"prompt.md"}

        if name_lower in deprecated_prompt_names:
            return "prompt.md is deprecated, please put content into metadata.json prompt field"

        question_names = set(ROOT_REQUIRED_FILE_TYPO_ALIASES.get("questions.md", set())) | {"questions.md"}
        if name_lower in question_names:
            return "questions.md location incorrect, should be in docs/questions.md"

        typo_target = ROOT_COMMON_FILE_TYPOS.get(name_lower)
        if typo_target:
            return f"Root directory filename suspected error, suggest renaming to {typo_target}"

        if name_lower == "trajectory.json":
            return f"Root directory should not have trajectory.json, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (suggest packing as archive)"

        if LEGACY_SESSION_JSON_RE.fullmatch(name_lower):
            return f"Root directory should not have old session JSON files, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/ (suggest packing as archive)"

        if name_lower == "readme.md":
            return "readme.md location incorrect, should be in repo directory"

        return "Root directory has disallowed extra files"

    def _check_root_fixed_files(self) -> None:
        section = self._new_section("2. Root Directory Fixed File Check")
        assert self.root is not None

        explained_root_files: set[Path] = set()

        for required in ROOT_REQUIRED_FILES:
            explained_root_files.update(
                self._report_root_required_file(
                    section,
                    required,
                    ROOT_REQUIRED_FILE_TYPO_ALIASES.get(required, set()),
                )
            )

        explained_root_files.update(self._report_root_trajectory_presence(section))

        extra_root_files = []
        for entry in self.root.iterdir():
            if not entry.is_file():
                continue
            if entry.name == "validation_report.md":
                continue
            if entry.name in ROOT_ALLOWED_FILES:
                continue
            if entry in explained_root_files:
                continue
            extra_root_files.append(entry)

        if extra_root_files:
            for path in sorted(extra_root_files, key=lambda p: p.name.lower()):
                section.add_fail(self._root_extra_file_issue_message(path), self._rel(path))
        elif not any(item.status == "FAIL" for item in section.items):
            section.add_pass("Root directory has no non-standard extra files", ".")

        self._record_failures()

    def _check_repo_directory(self) -> None:
        section = self._new_section("3. Code Directory Check")
        assert self.root is not None

        repo_dir = self.root / REPO_DIR_NAME
        self.legacy_project_dirs = self._collect_legacy_project_directories(self.root)
        report_hints = True

        if repo_dir.is_dir():
            self.project_type_dir = repo_dir
            section.add_pass("Code directory exists and naming is compliant: repo/", self._rel(repo_dir))
            if self.legacy_project_dirs:
                legacy_desc = ", ".join(path.name for _, path in self.legacy_project_dirs[:5])
                section.add_warn(
                    "Detected old project type directory, suggest executing --convert-legacy to migrate to repo structure",
                    legacy_desc,
                )
        else:
            if len(self.legacy_project_dirs) == 1:
                legacy_type, legacy_dir = self.legacy_project_dirs[0]
                self.project_type_dir = legacy_dir
                self.project_type_name = legacy_type
                section.add_fail(
                    f"Code directory naming non-compliant: should use repo/, currently old structure directory {legacy_dir.name} (Continuing subsequent checks with this directory)",
                    self._rel(legacy_dir),
                )
                report_hints = False
            elif len(self.legacy_project_dirs) > 1:
                self.project_type_dir = self.legacy_project_dirs[0][1]
                self.project_type_name = self.legacy_project_dirs[0][0]
                found_desc = ", ".join(f"{path.name} -> {canonical}" for canonical, path in self.legacy_project_dirs)
                section.add_fail(
                    f"Missing repo/, and detected multiple old structure code directories, non-compliant: {found_desc}",
                    ".",
                )
                report_hints = False
            else:
                inferred = self._infer_repo_candidate_from_common_dir()
                if inferred is not None:
                    self.project_type_dir = inferred
                    section.add_fail(
                        f"Code directory naming non-compliant: should use repo/, currently directory is {inferred.name} (Continuing subsequent checks with this directory)",
                        self._rel(inferred),
                    )
                else:
                    section.add_fail("Missing code directory repo/", "repo/")

        if report_hints:
            for message, path in self._collect_repo_root_hints():
                section.add_fail(message, self._rel(path))

        if self.project_type_dir is not None:
            self._report_repo_readme(section, self.project_type_dir)

        self._record_failures()

    def _report_repo_readme(self, section: CheckSection, project_dir: Path) -> None:
        root_readmes = self._find_readme_files_in_project_dir(project_dir)
        misplaced_readmes = self._find_readme_files_outside_project_dir(project_dir)
        typo_readmes = self._find_readme_typo_candidates(project_dir)

        if len(root_readmes) == 1:
            section.add_pass("Code directory has readme.md (recommended)", self._rel(root_readmes[0]))
        elif len(root_readmes) > 1:
            section.add_warn("multiple copies of readme.md in repo directory, suggest keeping only 1 copy", self._rel(root_readmes[0]))
            for path in root_readmes[1:6]:
                section.add_warn("readme.md appears repeatedly (suggest cleaning up extra copies)", self._rel(path))

        if misplaced_readmes:
            for path in misplaced_readmes[:6]:
                section.add_warn(
                    "Detected readme.md outside of repo (suggest migrating to repo/)",
                    self._rel(path),
                )

        if typo_readmes:
            for path in typo_readmes[:6]:
                parent = self._rel(path.parent)
                section.add_warn(
                    f"{path.name} naming seems non-standard, suggest using readme.md and placing in repo directory (currently at {parent}/)",
                    self._rel(path),
                )

        if not root_readmes and not misplaced_readmes and not typo_readmes:
            section.add_warn("Code directory missing readme.md (suggest providing)", self._rel(project_dir / "readme.md"))

    def _collect_legacy_project_directories(self, root: Path) -> list[tuple[str, Path]]:
        matches: list[tuple[str, Path]] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.lower() in ROOT_STANDARD_DIR_NAMES:
                continue
            normalized = _normalize_project_type_token(entry.name)
            canonical = PROJECT_TYPE_LOOKUP.get(normalized)
            if canonical is None:
                continue
            matches.append((canonical, entry))
        matches.sort(key=lambda x: x[1].name.lower())
        return matches

    def _collect_repo_root_hints(self) -> list[tuple[str, Path]]:
        assert self.root is not None
        hints: list[tuple[str, Path]] = []
        seen_paths: set[Path] = set()

        def _add_hint(message: str, path: Path) -> None:
            if path in seen_paths:
                return
            seen_paths.add(path)
            hints.append((message, path))

        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.lower() in ROOT_STANDARD_DIR_NAMES:
                continue

            normalized = _normalize_project_type_token(entry.name)
            if normalized in PROJECT_TYPE_LOOKUP:
                _add_hint(
                    f"Detected old structure directory {entry.name}, new specification should be unified as repo/ code directory",
                    entry,
                )
                continue
            suggested = PROJECT_TYPE_MISNAME_HINTS.get(normalized)
            if suggested is not None:
                _add_hint(
                    f"Detected directory {entry.name} suspected code directory, suggest migrating to repo/ (old structure suggested name: {suggested})",
                    entry,
                )
                continue

            if normalized in {"backend", "server", "api", "service", "frontend", "web", "client", "ui"}:
                _add_hint(
                    f"Detected {entry.name} is in root directory, code directories should be under repo/, this directory should be inside repo/",
                    entry,
                )

        return hints

    def _infer_repo_candidate_from_common_dir(self) -> Path | None:
        assert self.root is not None

        candidates: list[Path] = []

        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.lower() in ROOT_STANDARD_DIR_NAMES:
                continue

            if entry.name.startswith("."):
                continue
            candidates.append(entry)

        candidates.sort(key=lambda p: p.name.lower())
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _find_readme_files_outside_project_dir(self, project_dir: Path, max_items: int = 5) -> list[Path]:
        assert self.root is not None
        matches: list[Path] = []
        for path, is_file, _, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_file:
                continue
            if name_lower != "readme.md":
                continue
            try:
                path.relative_to(project_dir)
                continue
            except ValueError:
                pass
            matches.append(path)
        matches.sort(key=lambda p: p.as_posix().lower())
        return matches[:max_items]

    def _find_readme_files_in_project_dir(self, project_dir: Path) -> list[Path]:
        matches: list[Path] = []
        if not project_dir.is_dir():
            return matches
        for entry in project_dir.iterdir():
            if entry.is_file() and entry.name.lower() == "readme.md":
                matches.append(entry)
        matches.sort(key=lambda p: p.as_posix().lower())
        return matches

    def _find_readme_typo_candidates(self, project_dir: Path, max_items: int = 6) -> list[Path]:
        assert self.root is not None
        candidates: list[Path] = []
        typo_names = {
            "readme.mdown",
            "read_me.md",
            "reademe.md",
            "readmee.md",
            "reamde.md",
            "readme.txt",
        }
        for path, is_file, _, name_lower in self._iter_candidate_entries_for_root_requirements():
            if not is_file:
                continue
            if name_lower == "readme.md":
                continue
            if name_lower in typo_names or self._is_similar_filename(name_lower, "readme.md"):
                candidates.append(path)
        candidates.sort(key=lambda p: p.as_posix().lower())
        return candidates[:max_items]

    def _check_trajectory_organization(self) -> None:
        section = self._new_section("4. original_sessions Organization Check")
        assert self.root is not None

        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR_NAME
        has_sessions = sessions_dir.is_dir()
        if not has_sessions:
            section.add_fail(f"Missing {ORIGINAL_SESSIONS_DIR_NAME}/ directory", self._rel(sessions_dir))
            self._record_failures()
            return

        root_legacy_files = sorted(
            [
                p
                for p in self.root.iterdir()
                if p.is_file()
                and (
                    p.name.lower() == "trajectory.json"
                    or TRAJECTORY_MULTI_RE.fullmatch(p.name.lower()) is not None
                    or LEGACY_SESSION_JSON_RE.fullmatch(p.name.lower()) is not None
                )
            ],
            key=lambda p: p.name.lower(),
        )
        for path in root_legacy_files:
            section.add_fail(
                f"Root directory has old session JSON files, should be migrated to {ORIGINAL_SESSIONS_DIR_NAME}/",
                self._rel(path),
            )

        entries = sorted(sessions_dir.iterdir(), key=lambda p: p.name.lower())
        if not entries:
            section.add_fail(f"{ORIGINAL_SESSIONS_DIR_NAME}/ directory is empty", self._rel(sessions_dir))
            self._record_failures()
            return

        section.add_pass(
            f"Direct check mode enabled: on {ORIGINAL_SESSIONS_DIR_NAME}/ executing session directory checks directly (no zip search and extract)",
            self._rel(sessions_dir),
        )

        self._check_leading_bundle_dir_structure(section, sessions_dir)
        self._check_original_sessions_token_usage(section, [sessions_dir])
        self._check_original_sessions_prompt_anchor_consistency(section, [sessions_dir])
        self._check_latest_trajectory_file_completeness(section, [sessions_dir])
        self._check_original_sessions_jsonl_forbidden_keywords(section, sessions_dir)

        self._record_failures()

    def _detect_backend_content(self) -> None:
        if self.backend_content is not None:
            return

        project_type_value = str(self.metadata.get("project_type", "")).strip().lower()
        frontend_language_value = str(self.metadata.get("frontend_language", "")).strip().lower()
        backend_language_value = str(self.metadata.get("backend_language", "")).strip().lower()
        backend_framework_value = str(self.metadata.get("backend_framework", "")).strip().lower()

        if project_type_value in {"server", "backend", "pure_backend"}:
            self.backend_content = True
            self.backend_reason = f"metadata.project_type={project_type_value}"
            return
        if project_type_value in {"fullstack", "full_stack", "full-stack"}:
            self.backend_content = True
            self.backend_reason = f"metadata.project_type={project_type_value}"
            return

        if backend_language_value and backend_language_value not in {"none", "null", "n/a", "na", "-", "no"}:
            self.backend_content = True
            self.backend_reason = f"metadata.backend_language={backend_language_value}"
            return

        if backend_framework_value and backend_framework_value not in {"none", "null", "n/a", "na", "-", "no"}:
            self.backend_content = True
            self.backend_reason = f"metadata.backend_framework={backend_framework_value}"
            return

        if frontend_language_value in {"server", "backend"}:
            self.backend_content = True
            self.backend_reason = f"metadata.frontend_language={frontend_language_value} (Processed as backend)"
            return

        if self.project_type_name in {"pure_backend", "fullstack"}:
            self.backend_content = True
            self.backend_reason = f"Old structure project type is {self.project_type_name}"
            return

        project_dir = self.project_type_dir
        if project_dir is None:
            self.backend_content = False
            self.backend_reason = "Code directory unavailable"
            return

        for current_root, dirs, files in os.walk(project_dir, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)
            pruned_dirs: list[str] = []
            for dirname in dirs:
                if self._should_skip_heavy_walk_dir(dirname):
                    continue
                dir_path = current_path / dirname
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for dirname in dirs:
                name = dirname.lower()
                if any(keyword in name for keyword in BACKEND_KEYWORDS):
                    path = current_path / dirname
                    self.backend_content = True
                    self.backend_reason = f"Detected backend keyword directory: {self._rel(path)}"
                    return
            for filename in files:
                file_path = current_path / filename
                if self._is_ignored_by_any_gitignore(file_path):
                    continue
                lower = filename.lower()
                if lower in BACKEND_MARKER_FILES or lower.endswith(".csproj"):
                    self.backend_content = True
                    self.backend_reason = f"Detected backend marker file: {self._rel(file_path)}"
                    return

        self.backend_content = False
        self.backend_reason = "No backend keyword directory or backend marker file detected"

    def _check_docs_directory(self) -> None:
        section = self._new_section("6. docs Directory and Design Document Check")
        assert self.root is not None

        docs_dir = self.root / "docs"
        if docs_dir.is_dir():
            section.add_pass("docs/ directory exists", self._rel(docs_dir))
        else:
            _, misplaced_docs, typo_docs = self._collect_required_dir_candidates("docs", {"doc", "document", "documents"})
            if misplaced_docs or typo_docs:
                for path in misplaced_docs[:5]:
                    section.add_fail(
                        f"docs/ directory placed incorrectly, should be in TASK root directory instead of {self._rel(path.parent)}/",
                        self._rel(path),
                    )
                for path in typo_docs[:5]:
                    section.add_fail(
                        f"{path.name}/ directory naming incorrect, should be named docs/ and located in TASK root directory",
                        self._rel(path),
                    )
            else:
                section.add_fail("Missing docs/ directory", "docs/")
            self._record_failures()
            return

        design_doc = docs_dir / "design.md"
        if design_doc.is_file():
            section.add_pass("docs/design.md exists", self._rel(design_doc))
        else:
            section.add_fail("Missing docs/design.md", self._rel(design_doc))

        questions_doc = docs_dir / "questions.md"
        if questions_doc.is_file():
            section.add_pass("docs/questions.md exists", self._rel(questions_doc))
        else:
            section.add_fail("Missing docs/questions.md", self._rel(questions_doc))

        self._detect_backend_content()
        api_spec = docs_dir / "api-spec.md"
        if self.backend_content:
            if api_spec.is_file():
                section.add_pass("Backend content project has docs/api-spec.md", self._rel(api_spec))
            else:
                section.add_fail("Detected backend content, missing docs/api-spec.md", self._rel(api_spec))
        else:
            section.add_pass("No backend content detected, docs/api-spec.md is not required", self._rel(api_spec))

        self._record_failures()

    def _check_metadata_file(self) -> None:
        section = self._new_section("5. metadata.json Check")
        assert self.root is not None
        self.metadata_source_path = None

        metadata_path = self.root / "metadata.json"
        source_path: Path | None = None
        from_nonstandard = False

        if metadata_path.is_file():
            source_path = metadata_path
        else:
            _, misplaced, typos = self._collect_required_file_candidates(
                "metadata.json",
                ROOT_REQUIRED_FILE_TYPO_ALIASES.get("metadata.json", set()),
            )
            if misplaced:
                source_path = misplaced[0]
                from_nonstandard = True
            elif typos:
                source_path = typos[0]
                from_nonstandard = True
            else:
                section.add_fail("Missing metadata.json, cannot perform metadata field checks", "metadata.json")
                self.metadata = {}
                self._record_failures()
                return

        assert source_path is not None
        content = self._read_text(source_path)
        if content is None:
            section.add_fail("metadata.json is unreadable text", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            section.add_fail(f"metadata.json is not valid JSON: {exc.msg}", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        if not isinstance(parsed, dict):
            section.add_fail("metadata.json top-level must be JSON object", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        self.metadata = parsed
        self.metadata_source_path = source_path

        if from_nonstandard:
            section.add_warn(
                "Root directory metadata.json missing, using misplaced/incorrectly named file for field checks (please fix item 2 first)",
                self._rel(source_path),
            )
        else:
            section.add_pass("Root directory has metadata.json", self._rel(source_path))

        project_type_raw = parsed.get("project_type")
        project_type_normalized = ""
        if isinstance(project_type_raw, str):
            project_type_normalized = project_type_raw.strip().lower()

        allow_empty_keys: set[str] = set()
        if project_type_normalized == "web":
            allow_empty_keys.update({"backend_language", "backend_framework"})
        elif project_type_normalized == "server":
            allow_empty_keys.update({"frontend_language", "frontend_framework"})

        for key in METADATA_REQUIRED_KEYS:
            if key not in parsed:
                section.add_fail(f"metadata.json missing required fields: {key}", self._rel(source_path))
                continue

            value = parsed.get(key)
            if not isinstance(value, str):
                section.add_fail(f"metadata.json field {key} must be non-empty string", self._rel(source_path))
                continue

            normalized = value.strip()
            if not normalized:
                if key in allow_empty_keys:
                    section.add_pass(
                        f"metadata.json field {key} allowed empty (project_type={project_type_normalized})",
                        self._rel(source_path),
                    )
                    continue
                section.add_fail(f"metadata.json field {key} cannot be empty", self._rel(source_path))
                continue

            if key == "project_type":
                project_type_value = normalized.lower()
                if project_type_value not in METADATA_PROJECT_TYPE_SET:
                    allowed = ", ".join(METADATA_PROJECT_TYPE_ENUM)
                    section.add_fail(
                        f"metadata.json field project_type invalid: {value} (Only allowed: {allowed})",
                        self._rel(source_path),
                    )
                    continue
                section.add_pass(
                    f"metadata.json field project_type valid: {project_type_value}",
                    self._rel(source_path),
                )
                continue

            section.add_pass(f"metadata.json field {key} is non-empty string", self._rel(source_path))

        self._record_failures()

    def _read_text(self, path: Path) -> str | None:
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

    def _calc_prompt_english_ratio(self, content: str) -> float:
        english_letters = 0
        total_letters = 0

        for ch in content:
            if not ch.isalpha():
                continue
            total_letters += 1
            if ch.isascii():
                english_letters += 1

        if total_letters == 0:
            return 0.0
        return english_letters / total_letters

    def _check_metadata_prompt_english_mode(self) -> None:
        section = self._new_section("7. metadata.prompt English Mode Check")
        assert self.root is not None

        source_path = self.metadata_source_path or (self.root / "metadata.json")
        prompt_value = self.metadata.get("prompt")
        prompt_text = ""
        prompt_source = self._rel(source_path)

        if prompt_value is not None:
            if not isinstance(prompt_value, str):
                prompt_text = str(prompt_value)
                section.add_warn("metadata.prompt is not string, checking as string format", self._rel(source_path))
            else:
                prompt_text = prompt_value

        if not prompt_text.strip():
            fallback_candidates = [self.root / "prompts.md", self.root / "prompt.md"]
            fallback_used: Path | None = None
            for candidate in fallback_candidates:
                if not candidate.is_file():
                    continue
                fallback_content = self._read_text(candidate)
                if fallback_content is None:
                    continue
                prompt_text = fallback_content
                fallback_used = candidate
                break

            if fallback_used is not None:
                prompt_source = self._rel(fallback_used)
                section.add_warn(
                    "metadata.prompt missing or empty, falling back to prompts.md/prompt.md for English mode check",
                    prompt_source,
                )
            else:
                section.add_fail(
                    "metadata.prompt missing and no usable prompts.md (or prompt.md) found for fallback check",
                    self._rel(source_path),
                )
                self.english_mode = False
                self._record_failures()
                return

        english_ratio = self._calc_prompt_english_ratio(prompt_text)
        self.english_mode = english_ratio > PROMPT_ENGLISH_RATIO_THRESHOLD

        if self.english_mode:
            section.add_pass(
                f"metadata.prompt English character ratio {english_ratio:.2%} > 70%, English consistency mode enabled",
                prompt_source,
            )
        else:
            section.add_pass(
                f"metadata.prompt English character ratio {english_ratio:.2%} <= 70%, English consistency mode not enabled",
                prompt_source,
            )

        self._record_failures()

    def _iter_readable_text_files(self) -> Iterable[tuple[Path, str]]:
        assert self.root is not None

        for current_root, dirs, files in os.walk(self.root, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)

            pruned_dirs: list[str] = []
            for dirname in dirs:
                if self._should_skip_english_dir(dirname):
                    continue
                dir_path = current_path / dirname
                if self._is_original_sessions_memory_exempt_path(dir_path):
                    continue
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for filename in files:
                path = current_path / filename
                if self._is_original_sessions_memory_exempt_path(path):
                    continue
                if self._should_skip_english_file(path):
                    continue
                if self._is_ignored_by_any_gitignore(path):
                    continue

                text = self._read_text(path)
                if text is None:
                    continue

                yield path, text

    def _should_skip_heavy_walk_dir(self, dirname: str) -> bool:
        lower = dirname.lower()
        if lower in {".git", ".tmp", ".backup"}:
            return True
        if self._is_runtime_noise_dir_name(dirname):
            return True
        if self._dir_violation_reason(dirname) is not None:
            return True
        return False

    def _is_runtime_noise_dir_name(self, dirname: str) -> bool:
        lower = dirname.lower()
        if re.fullmatch(r"build-.*", lower):
            return True
        return lower in RUNTIME_NOISE_DIR_NAMES

    def _is_runtime_noise_file(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name in RUNTIME_NOISE_FILE_NAMES:
            return True
        return path.suffix.lower() in RUNTIME_NOISE_FILE_SUFFIXES

    def _should_skip_english_dir(self, dirname: str) -> bool:
        if self._should_skip_heavy_walk_dir(dirname):
            return True
        lower = dirname.lower()
        return lower in ENGLISH_CHECK_EXCLUDED_DIRS

    def _should_skip_english_file(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name in ENGLISH_CHECK_EXCLUDED_FILES:
            return True
        if lower_name == "validation_report.md":
            return True
        if path.suffix.lower() in SKIP_LANGUAGE_CHECK_EXTS:
            return True
        if self._file_violation_reason(path) is not None:
            return True
        return False

    def _find_chinese_line_numbers(self, content: str) -> list[int]:
        line_numbers: list[int] = []
        for idx, line in enumerate(content.splitlines(), start=1):
            if CHINESE_RE.search(line):
                line_numbers.append(idx)
        return line_numbers

    def _format_line_numbers(self, line_numbers: list[int]) -> str:
        if not line_numbers:
            return ""
        if len(line_numbers) <= 20:
            return ", ".join(str(n) for n in line_numbers)
        head = ", ".join(str(n) for n in line_numbers[:20])
        return f"{head} ... Total{len(line_numbers)} rows"

    def _check_english_consistency(self) -> None:
        section = self._new_section("8. Text File Chinese Character Check")
        assert self.root is not None

        if not self.english_mode:
            section.add_pass("English consistency mode not enabled, skipping Chinese character check", ".")
            self._record_failures()
            return

        failures = 0
        for path, content in self._iter_readable_text_files():
            line_numbers = self._find_chinese_line_numbers(content)
            if line_numbers:
                failures += 1
                formatted = self._format_line_numbers(line_numbers)
                section.add_fail(
                    f"Detected Chinese characters (not allowed in English consistency mode, Line number: {formatted})",
                    self._rel(path),
                )

        if failures == 0:
            section.add_pass("No Chinese characters detected", ".")

        self._record_failures()

    def _check_backend_content_recognition(self) -> None:
        section = self._new_section("9. Backend Content Identification")
        assert self.root is not None

        self._detect_backend_content()
        if self.project_type_dir is None:
            section.add_fail("Cannot determine backend content: code directory unavailable", ".")
            self._record_failures()
            return

        if self.backend_content:
            section.add_pass(f"Detected backend content: {self.backend_reason}", self._rel(self.project_type_dir))
        else:
            section.add_pass(f"No backend content detected: {self.backend_reason}", self._rel(self.project_type_dir))

        self._record_failures()

    def _check_backend_project_requirements(self) -> None:
        section = self._new_section("10. Backend Project Additional Check")
        assert self.root is not None

        self._detect_backend_content()
        if self.project_type_dir is None:
            section.add_fail("Code directory unavailable, cannot perform backend additional checks", ".")
            self._record_failures()
            return

        if not self.backend_content:
            section.add_pass("Not a backend content project, skipping Docker/Compose/test script check", self._rel(self.project_type_dir))
            self._record_failures()
            return

        compose_candidates = [
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        ]
        run_tests_candidates = ["run_tests.sh", "run_tests.bat", "run_tests.ps1"]
        dockerfiles, compose_paths, test_paths = self._scan_backend_requirement_files(
            self.project_type_dir,
            compose_candidates,
            run_tests_candidates,
        )

        if dockerfiles:
            display_paths = ", ".join(self._rel(p) for p in dockerfiles[:3])
            if len(dockerfiles) > 3:
                display_paths += f" etc.{len(dockerfiles)} places"
            section.add_pass(f"Backend project has Dockerfile (can be in subdirectories): {display_paths}", self._rel(dockerfiles[0]))
        else:
            section.add_fail(
                "Backend project missing Dockerfile (allowed in repo directory or its subdirectories)",
                self._rel(self.project_type_dir),
            )

        compose_found = [p.name for p in compose_paths]
        if compose_found:
            section.add_pass(
                f"Backend project has Compose file: {compose_found[0]}",
                self._rel(compose_paths[0]),
            )
        else:
            section.add_fail(
                "Backend project missing Compose file (compose.yaml/compose.yml/docker-compose.yaml/docker-compose.yml)",
                self._rel(self.project_type_dir),
            )

        tests_found = [p.name for p in test_paths]
        if tests_found:
            section.add_pass(
                f"Backend project has unified test startup script: {tests_found[0]}",
                self._rel(test_paths[0]),
            )
        else:
            section.add_fail(
                "Backend project missing unified test startup script (run_tests.sh/run_tests.bat/run_tests.ps1)",
                self._rel(self.project_type_dir),
            )

        api_spec = self.root / "docs" / "api-spec.md"
        if api_spec.is_file():
            section.add_pass("Backend project has docs/api-spec.md", self._rel(api_spec))
        else:
            section.add_fail("Backend project missing docs/api-spec.md", self._rel(api_spec))

        self._record_failures()

    def _scan_backend_requirement_files(
        self,
        base_dir: Path,
        compose_candidates: list[str],
        run_tests_candidates: list[str],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        dockerfiles: list[Path] = []
        compose_files: list[Path] = []
        test_files: list[Path] = []

        compose_set = {name.lower() for name in compose_candidates}
        test_set = {name.lower() for name in run_tests_candidates}

        for current_root, dirs, files in os.walk(base_dir, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)
            pruned_dirs: list[str] = []
            for dirname in dirs:
                if self._should_skip_heavy_walk_dir(dirname):
                    continue
                dir_path = current_path / dirname
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for filename in files:
                lower = filename.lower()
                path = current_path / filename
                if self._is_runtime_noise_file(path):
                    continue
                if lower == "dockerfile":
                    dockerfiles.append(path)
                if lower in compose_set:
                    compose_files.append(path)
                if lower in test_set:
                    test_files.append(path)

        dockerfiles.sort(key=lambda p: p.as_posix().lower())
        compose_files.sort(key=lambda p: p.as_posix().lower())
        test_files.sort(key=lambda p: p.as_posix().lower())
        return dockerfiles, compose_files, test_files

    def _check_gitignore_exists(self) -> None:
        section = self._new_section("11. .gitignore Existence Check")
        assert self.root is not None

        root_gitignore = self.root / ".gitignore"
        repo_gitignore = (self.project_type_dir / ".gitignore") if self.project_type_dir is not None else None

        found_count = 0
        if root_gitignore.is_file():
            section.add_pass("Root directory has .gitignore", self._rel(root_gitignore))
            found_count += 1
        if repo_gitignore is not None and repo_gitignore.is_file():
            section.add_pass("Code directory has .gitignore", self._rel(repo_gitignore))
            found_count += 1

        if found_count == 0:
            rel = self._rel(repo_gitignore) if repo_gitignore is not None else ".gitignore"
            section.add_warn("No .gitignore detected (can be provided in root or repo directory)", rel)

        self._record_failures()

    def _detect_languages(self) -> None:
        self.languages = set()
        if self.project_type_dir is None:
            return

        seen_names: set[str] = set()
        has_csharp = False

        for current_root, dirs, files in os.walk(self.project_type_dir, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)

            pruned_dirs: list[str] = []
            for dirname in dirs:
                if self._should_skip_heavy_walk_dir(dirname):
                    continue
                dir_path = current_path / dirname
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for filename in files:
                path = current_path / filename
                if self._is_ignored_by_any_gitignore(path):
                    continue
                lower = filename.lower()
                seen_names.add(lower)
                if lower.endswith(".csproj") or lower.endswith(".sln"):
                    has_csharp = True

        if "pyproject.toml" in seen_names or "requirements.txt" in seen_names:
            self.languages.add("python")
        if "package.json" in seen_names:
            self.languages.add("js_ts")
        if any(x in seen_names for x in ("pom.xml", "build.gradle", "build.gradle.kts")):
            self.languages.add("java_kotlin")
        if "go.mod" in seen_names:
            self.languages.add("go")
        if "composer.json" in seen_names:
            self.languages.add("php")
        if "cargo.toml" in seen_names:
            self.languages.add("rust")
        if "pubspec.yaml" in seen_names:
            self.languages.add("dart_flutter")
        if "gemfile" in seen_names:
            self.languages.add("ruby")
        if has_csharp:
            self.languages.add("csharp")
        if "cmakelists.txt" in seen_names or "makefile" in seen_names:
            self.languages.add("c_cpp")

    def _read_gitignore_entries(self, gitignore_path: Path) -> list[str]:
        content = self._read_text(gitignore_path)
        if content is None:
            return []

        entries: list[str] = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if line.startswith("!"):
                continue
            entries.append(line)
        return entries

    def _get_gitignore_scopes(self) -> list[tuple[Path, list[str]]]:
        if self._gitignore_scopes_cache is not None:
            return self._gitignore_scopes_cache

        scopes: list[tuple[Path, list[str]]] = []
        seen_gitignores: set[Path] = set()

        candidates: list[Path] = []
        if self.root is not None:
            candidates.append(self.root / ".gitignore")
        if self.project_type_dir is not None:
            candidates.append(self.project_type_dir / ".gitignore")

        for gitignore_path in candidates:
            gitignore_path = gitignore_path.resolve()
            if gitignore_path in seen_gitignores:
                continue
            seen_gitignores.add(gitignore_path)
            if not gitignore_path.is_file():
                continue
            entries = self._read_gitignore_entries(gitignore_path)
            if not entries:
                continue
            scopes.append((gitignore_path.parent, entries))

        self._gitignore_scopes_cache = scopes
        return self._gitignore_scopes_cache

    def _gitignore_pattern_matches_path(self, pattern: str, rel_posix_path: str) -> bool:
        pat = pattern.strip().replace("\\", "/")
        if not pat:
            return False

        anchored = pat.startswith("/")
        if anchored:
            pat = pat.lstrip("/")
        dir_only = pat.endswith("/")
        if dir_only:
            pat = pat.rstrip("/")
        if not pat:
            return False

        path_parts = rel_posix_path.split("/")
        file_name = path_parts[-1]
        dir_prefixes = ["/".join(path_parts[:idx]) for idx in range(1, len(path_parts))]

        if dir_only:
            if anchored:
                return any(fnmatch.fnmatch(prefix, pat) for prefix in dir_prefixes)
            if "/" in pat:
                return any(
                    fnmatch.fnmatch(prefix, pat) or fnmatch.fnmatch(prefix, f"**/{pat}")
                    for prefix in dir_prefixes
                )
            return any(any(fnmatch.fnmatch(seg, pat) for seg in prefix.split("/")) for prefix in dir_prefixes)

        if anchored:
            return fnmatch.fnmatch(rel_posix_path, pat)
        if "/" in pat:
            return fnmatch.fnmatch(rel_posix_path, pat) or fnmatch.fnmatch(rel_posix_path, f"**/{pat}")
        return any(fnmatch.fnmatch(seg, pat) for seg in path_parts) or fnmatch.fnmatch(file_name, pat)

    def _is_ignored_by_any_gitignore(self, path: Path, treat_as_dir: bool = False) -> bool:
        scopes = self._get_gitignore_scopes()
        if not scopes:
            return False

        for base_dir, entries in scopes:
            try:
                rel = path.relative_to(base_dir).as_posix()
            except ValueError:
                continue

            # .gitignore only governs descendants in its own domain.
            # A scoped .gitignore (e.g. pure_backend/.gitignore) should not
            # make the scope root directory itself disappear in outer traversal.
            if rel in {"", "."}:
                continue

            rel_to_match = rel
            if treat_as_dir:
                rel_to_match = f"{rel}/.codex_ignore_probe"

            if any(self._gitignore_pattern_matches_path(pattern, rel_to_match) for pattern in entries):
                return True

        return False

    def _pattern_variants(self, required: str) -> set[str]:
        req = required.strip()
        variants = {req}

        if req.endswith("/"):
            base = req.rstrip("/")
            variants.update(
                {
                    base,
                    f"/{req}",
                    f"/{base}",
                    f"**/{req}",
                    f"**/{base}",
                    f"{base}/**",
                    f"**/{base}/**",
                }
            )
        elif req.startswith("*."):
            variants.update({f"**/{req}"})
        elif "/" in req:
            variants.update({f"/{req}", f"**/{req}"})
        else:
            variants.update({f"/{req}", f"**/{req}"})

        return variants

    def _is_pattern_covered(self, required: str, gitignore_entries: list[str]) -> bool:
        variants = {v.lower() for v in self._pattern_variants(required)}

        normalized_entries: set[str] = set()
        for entry in gitignore_entries:
            e = entry.strip().lower()
            if not e:
                continue
            normalized_entries.add(e)
            normalized_entries.add(e.rstrip("/"))

        for variant in variants:
            if variant in normalized_entries:
                return True
            if variant.rstrip("/") in normalized_entries:
                return True

        return False

    def _check_gitignore_coverage(self) -> None:
        section = self._new_section("12. .gitignore Coverage Check")
        scopes = self._get_gitignore_scopes()
        if not scopes:
            section.add_warn("No readable .gitignore detected, cannot perform coverage check (reminder)", ".")
            self._record_failures()
            return

        all_entries: list[str] = []
        scoped_desc = ", ".join(self._rel(base / ".gitignore") for base, _ in scopes)
        for _, entries in scopes:
            all_entries.extend(entries)

        if not all_entries:
            section.add_warn(".gitignore content is empty or unreadable, cannot override necessary rules (reminder)", scoped_desc)
            self._record_failures()
            return

        if not self.languages:
            section.add_pass("Language marker file not identified, skipping language rule coverage check", scoped_desc)
            self._record_failures()
            return

        section.add_pass(
            "Identified language type: " + ", ".join(sorted(self.languages)),
            scoped_desc,
        )

        required_patterns: list[str] = list(UNIVERSAL_GITIGNORE_PATTERNS)
        for language in sorted(self.languages):
            required_patterns.extend(LANGUAGE_GITIGNORE_PATTERNS.get(language, []))

        missing_patterns = []
        for required in required_patterns:
            if not self._is_pattern_covered(required, all_entries):
                missing_patterns.append(required)

        if not missing_patterns:
            section.add_pass(".gitignore covers common artifacts rules for current language and general directories", scoped_desc)
        else:
            for pattern in missing_patterns:
                section.add_warn(f".gitignore missing coverage for {pattern}", scoped_desc)

        self._record_failures()

    def _dir_violation_reason(self, dirname: str) -> str | None:
        lower = dirname.lower()
        if re.fullmatch(r"build-.*", lower):
            return "is build artifact directory"
        return DIR_VIOLATION_REASONS.get(lower)

    def _is_compile_exempt_dir(self, dirname: str) -> bool:
        lower = dirname.lower()
        if re.fullmatch(r"build-.*", lower):
            return True
        return lower in COMPILE_EXEMPT_DIR_NAMES

    def _is_compile_exempt_file(self, path: Path) -> bool:
        lower_name = path.name.lower()
        if lower_name in COMPILE_EXEMPT_FILE_NAMES:
            return True
        return path.suffix.lower() in COMPILE_EXEMPT_FILE_SUFFIXES

    def _file_violation_reason(self, path: Path) -> str | None:
        for rule, reason in FILE_VIOLATION_RULES:
            if rule(path):
                return reason
        return None

    def _check_local_dirty_files(self) -> None:
        section = self._new_section("13. Local Dirty File Check")
        assert self.root is not None

        violations: list[tuple[Path, str, str]] = []

        for current_root, dirs, files in os.walk(self.root, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)

            pruned_dirs: list[str] = []
            for dirname in dirs:
                if dirname in {".git", ".backup"}:
                    continue
                if self._is_runtime_noise_dir_name(dirname):
                    continue
                dir_path = current_path / dirname
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                reason = self._dir_violation_reason(dirname)
                if reason:
                    if self._is_compile_exempt_dir(dirname):
                        violations.append((dir_path, reason, "WARN"))
                    else:
                        violations.append((dir_path, reason, "FAIL"))
                else:
                    pruned_dirs.append(dirname)
            dirs[:] = pruned_dirs

            for filename in files:
                file_path = current_path / filename
                if file_path.name == "validation_report.md":
                    continue
                if self._is_runtime_noise_file(file_path):
                    continue
                if self._is_ignored_by_any_gitignore(file_path):
                    continue
                reason = self._file_violation_reason(file_path)
                if reason:
                    if self._is_compile_exempt_file(file_path):
                        violations.append((file_path, reason, "WARN"))
                    else:
                        violations.append((file_path, reason, "FAIL"))

        deduped: dict[tuple[str, str, str], tuple[Path, str, str]] = {}
        for path, reason, status in violations:
            rel = self._rel(path)
            if path.is_dir():
                rel = rel + "/"
            key = (rel.lower(), status, reason)
            if key not in deduped:
                deduped[key] = (path, reason, status)

        ordered = sorted(deduped.values(), key=lambda x: (self._rel(x[0]).lower(), x[2], x[1]))
        self._dirty_findings_cache = ordered

        if not ordered:
            section.add_pass("No local dirty files such as cache/dependencies/build artifacts/databases detected", ".")
        else:
            for path, reason, status in ordered:
                rel_path = self._rel(path)
                if path.is_dir():
                    rel_path = rel_path + "/"
                if status == "WARN":
                    reason_msg = f"{reason} (Build artifact, exempt from deletion, reminder only)"
                    section.add_warn(f"{rel_path} {reason_msg}", rel_path)
                else:
                    section.add_fail(f"{rel_path} {reason}", rel_path)

        self._record_failures()

    def _write_report(self) -> None:
        if self.report_path is None:
            self.report_path = Path.cwd() / ".tmp" / "validation_report.md"

        self._record_failures()
        lines = ["# Static QC Report", ""]

        for section in self.sections:
            lines.append(f"## {section.title}")
            if not section.items:
                lines.append("- [PASS] No items to check (.)")
            else:
                for item in section.items:
                    lines.append(f"- [{item.status}] {item.message} ({item.rel_path})")
            lines.append("")

        content = "\n".join(lines).rstrip() + "\n"

        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(content, encoding="utf-8")
        except OSError:
            fallback = Path.cwd() / ".tmp" / "validation_report.md"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            fallback.write_text(content, encoding="utf-8")
            self.report_path = fallback


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Perform static compliance checks on project delivery directory")
    parser.add_argument("target", help="Directory path or directory name")
    parser.add_argument(
        "--convert-legacy",
        "--convert_legacy",
        action="store_true",
        help="Migrate old structure to new structure (repo/docs/questions.md/original_sessions/metadata), will prompt for confirmation and backup before execution",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Execute repair (move/rename/delete) after outputting report, will prompt for confirmation and backup to .backup in root directory before execution",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    validator = PackageValidator(args.target)
    passed, errors, report = validator.run()

    status = "PASS" if passed else "FAIL"
    print(f"{status} | errors={errors} | report={report}")

    if args.convert_legacy:
        if report.is_file():
            print("CONVERT | Current report content:")
            try:
                print(report.read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"CONVERT | Failed to read report: {exc}")
        validator.run_convert_legacy()
        passed, errors, report = validator.run()
        status = "PASS" if passed else "FAIL"
        print(f"POST-CONVERT {status} | errors={errors} | report={report}")

    if args.repair:
        if report.is_file():
            print("REPAIR | Current report content:")
            try:
                print(report.read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"REPAIR | Failed to read report: {exc}")
        validator.run_repair()

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
