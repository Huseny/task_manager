#!/usr/bin/env python3
"""Static delivery package validator.

Usage:
  python validate_package.py /path/to/TASK-001
  python validate_package.py TASK-001
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

try:
    import yaml
except Exception:
    yaml = None

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


def _normalize_metadata_value_token(value: str) -> str:
    return re.sub(r"[\s\-_./\\|]+", "", value.strip().lower())


def _is_invalid_metadata_placeholder(value: str) -> bool:
    token = _normalize_metadata_value_token(value)
    if not token:
        return False
    return token in METADATA_INVALID_PLACEHOLDER_TOKENS


def _metadata_allow_empty_keys(project_type_value: str) -> set[str]:
    if project_type_value in {"web", "ios"}:
        return {"backend_language", "backend_framework", "database"}
    if project_type_value == "server":
        return {"frontend_language", "frontend_framework"}
    if project_type_value in {"android", "desktop"}:
        return {"database"}
    return set()


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
TRAJECTORY_MULTI_RE = re.compile(r"trajectory[-_]\d+\.json$", re.IGNORECASE)
LEGACY_SESSION_JSON_RE = re.compile(r"(trajectory(?:[-_]\d+)?|develop(?:[-_]\d+)?|bugfix(?:[-_]\d+)?)\.json$", re.IGNORECASE)
PROMPT_ENGLISH_RATIO_THRESHOLD = 0.70
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
TOKEN_COST_MAX_USD = 350.0
PROJECT_TYPES_REQUIRE_DOCKER_AND_TEST = {"web", "server", "fullstack"}
PROJECT_TYPES_REQUIRE_API_SPEC = {"server", "fullstack"}

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
LEADING_SESSION_ZIP_WINDOWS_DRIVE_RE = re.compile(r"^[a-z]--", re.IGNORECASE)
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
ROOT_STANDARD_DIR_NAMES = {"docs", ORIGINAL_SESSIONS_DIR_NAME, REPO_DIR_NAME, "skills", ".tmp", ".backup", ".git"}

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

VENDOR_REFERENCE_CONTEXT_DIR_NAMES = {"static", "public", "assets"}
VENDOR_DEPENDENCY_MARKER_FILENAMES = {
    "composer.json",
    "composer.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "Gemfile",
    "Gemfile.lock",
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
    ".vscode": "为本地 IDE 配置目录",
    ".idea": "为本地 IDE 配置目录",
    ".codex": "为本地工具目录",
    ".opencode": "为本地工具目录",
    "__pycache__": "为 Python 缓存目录",
    "__pychache__": "为 Python 缓存目录（疑似 __pycache__ 拼写错误）",
    ".pytest_cache": "为 Python 缓存目录",
    ".venv": "为 Python 虚拟环境目录",
    "venv": "为 Python 虚拟环境目录",
    ".mypy_cache": "为 Python 缓存目录",
    ".ruff_cache": "为 Python 缓存目录",
    "htmlcov": "为 Python 覆盖率目录",
    "node_modules": "位于 Node 依赖目录",
    ".npm": "为 Node 本地缓存目录",
    ".pnpm-store": "为 Node 本地缓存目录",
    ".yarn": "为 Node 本地缓存目录",
    ".next": "为 Node 构建目录",
    "coverage": "为覆盖率产物目录",
    "dist": "为构建产物目录",
    "build": "为构建产物目录",
    "target": "为构建产物目录",
    ".gradle": "为 Gradle 本地目录",
    ".kotlin": "为 Kotlin 本地目录",
    "out": "为构建产物目录",
    "bin": "为构建产物目录",
    "obj": "为构建产物目录",
    "debug": "为构建产物目录",
    "release": "为构建产物目录",
    ".vs": "为 .NET 本地目录",
    "testresults": "为测试结果目录",
    "cmakefiles": "为 C/C++ 构建目录",
    ".dart_tool": "为 Dart/Flutter 本地缓存目录",
    ".bundle": "为 Ruby 本地依赖目录",
    "vendor": "疑似依赖目录，需结合路径与引用确认",
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

DATABASE_FILE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}

SIZE_WARN_SINGLE_FILE_BYTES = 50 * 1024 * 1024
SIZE_WARN_TOTAL_BYTES = 200 * 1024 * 1024
SIZE_SKIP_DIR_NAMES = {".git", ".backup", ".tmp", ORIGINAL_SESSIONS_DIR_NAME}

FILE_VIOLATION_RULES = [
    (lambda p: p.suffix.lower() == ".pyc", "为 Python 缓存文件"),
    (lambda p: p.name.lower() == ".coverage", "为覆盖率文件"),
    (lambda p: p.name.lower() == "coverage.out", "为覆盖率文件"),
    (lambda p: p.suffix.lower() == ".test", "为测试二进制文件"),
    (lambda p: p.suffix.lower() in {".class", ".jar", ".war"}, "为 Java/Kotlin 构建产物"),
    (
        lambda p: p.name.lower() in {"cmakecache.txt", "cmake_install.cmake", "compile_commands.json"},
        "为 C/C++ 构建文件",
    ),
    (lambda p: p.suffix.lower() in {".o", ".obj", ".exe", ".pdb"}, "为二进制/构建产物文件"),
    (
        lambda p: p.name.lower() in {".flutter-plugins", ".flutter-plugins-dependencies"},
        "为 Flutter 本地工具文件",
    ),
    (
        lambda p: p.as_posix().lower().endswith("android/local.properties"),
        "为 Android 本地配置文件",
    ),
    (lambda p: p.suffix.lower() == ".tsbuildinfo", "为 TypeScript 构建缓存文件"),
    (lambda p: p.name.lower() == "session.json", "为不应交付文件（session.json）"),
    (
        lambda p: re.fullmatch(r"rollout-.*\.jsonl", p.name.lower()) is not None,
        "为不应交付文件（rollout-*.jsonl）",
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
    path: Path
    readable: bool
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_record_count: int = 0
    keyword_line_numbers: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class ComposeDockerfileRef:
    compose_path: Path
    service_name: str
    dockerfile_path: Path


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
        self._check_package_size()

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
                print("POST-REPORT-CLEANUP | 无需删除目录（同名预存目录已跳过解压并跳过删除）")
            return

        done = 0
        skipped = 0
        failed = 0

        print("POST-REPORT-CLEANUP | 开始清理解压目录（仅清理本次 zip 解压新增目录）")
        for path in sorted(self._session_extracted_dirs_for_cleanup.values(), key=lambda p: p.as_posix().lower()):
            key = path.as_posix().lower()
            if key in self._session_skip_cleanup_dirs:
                print(f"[SKIP] CLEANUP_DIR {self._rel(path)} | 标记为同名预存目录，跳过删除")
                skipped += 1
                continue
            if not path.exists():
                print(f"[SKIP] CLEANUP_DIR {self._rel(path)} | 目录已不存在")
                skipped += 1
                continue
            if not path.is_dir():
                print(f"[FAIL] CLEANUP_DIR {self._rel(path)} | 目标不是目录")
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
            print("REPAIR | 输入目录无效，跳过修复")
            return 0, 0, 0, None

        print("REPAIR | 正在生成修复计划（目录较大时可能耗时）...", flush=True)
        actions = self._plan_repair_actions()
        print(f"REPAIR | 修复计划生成完成，共 {len(actions)} 项", flush=True)
        if not actions:
            print("REPAIR | 无可执行修复操作")
            return 0, 0, 0, None

        print("REPAIR | 以下为拟执行操作（报告已先生成）:")
        self._print_repair_plan(actions)

        try:
            confirmation = input("确认执行以上修复操作吗？输入 YES 继续，其它任意输入取消: ").strip()
        except EOFError:
            confirmation = ""

        if confirmation.upper() != "YES":
            print("REPAIR | 已取消，未修改任何文件")
            return 0, len(actions), 0, None

        executed, skipped, failed, backup_dir = self._execute_repair_actions(actions)
        if backup_dir is None:
            print(f"REPAIR | executed={executed} skipped={skipped} failed={failed} | 未生成备份（无删除操作）")
        else:
            print(
                f"REPAIR | executed={executed} skipped={skipped} failed={failed} | backup={self._rel(backup_dir)}"
            )
        return executed, skipped, failed, backup_dir

    def run_convert_legacy(self) -> tuple[int, int, int, Path | None]:
        if self.root is None:
            print("CONVERT | 输入目录无效，跳过转换")
            return 0, 0, 0, None

        print("CONVERT | 正在生成旧结构迁移计划...", flush=True)
        actions = self._plan_convert_legacy_actions()
        print(f"CONVERT | 迁移计划生成完成，共 {len(actions)} 项", flush=True)
        if not actions:
            print("CONVERT | 无需迁移（未检测到可转换的旧结构）")
            return 0, 0, 0, None

        print("CONVERT | 以下为拟执行操作:")
        self._print_repair_plan(actions)

        try:
            confirmation = input("确认执行以上迁移操作吗？输入 YES 继续，其它任意输入取消: ").strip()
        except EOFError:
            confirmation = ""

        if confirmation.upper() != "YES":
            print("CONVERT | 已取消，未修改任何文件")
            return 0, len(actions), 0, None

        executed, skipped, failed, backup_dir = self._execute_repair_actions(actions, backup_on_move=True)
        if backup_dir is None:
            print(f"CONVERT | executed={executed} skipped={skipped} failed={failed} | 未生成备份")
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

        # 0) 代码目录下 .tmp 合并到根目录 .tmp，并删除源目录。
        if self.project_type_dir is not None:
            scoped_tmp = self.project_type_dir / ".tmp"
            if scoped_tmp.is_dir():
                actions.append(
                    RepairAction(
                        kind="merge_tmp_dir",
                        src=_abs(scoped_tmp),
                        dst=_abs(root / ".tmp"),
                        reason="将代码目录下 .tmp 内容迁移到根目录 .tmp，并删除原目录",
                    )
                )
                _add_delete(
                    scoped_tmp,
                    "清理代码目录下 .tmp 残留（合并后；若不存在将自动跳过）",
                )

        # 1) 根目录必要文件：位置/命名修复，重复与错位删除。
        for required in ROOT_REQUIRED_FILES:
            correct, misplaced, typos = self._collect_required_file_candidates(
                required,
                ROOT_REQUIRED_FILE_TYPO_ALIASES.get(required, set()),
            )
            destination = root / required

            if correct:
                for duplicate in correct[1:]:
                    _add_delete(duplicate, f"{required} 根目录重复，保留一份")
                for wrong in misplaced:
                    _add_delete(wrong, f"{required} 位置错误，根目录已有正确文件")
                for typo in typos:
                    _add_delete(typo, f"{typo.name} 命名错误，根目录已有正确 {required}")
            else:
                candidates = misplaced + typos
                if candidates:
                    selected = candidates[0]
                    _add_move(selected, destination, f"修复 {required} 的位置/命名")
                    for redundant in candidates[1:]:
                        _add_delete(redundant, f"{required} 候选重复，保留首个修复来源")

        # 1.1) metadata.json 字段补齐（缺失时创建，已有时补全空字段）。
        actions.append(
            RepairAction(
                kind="upsert_metadata",
                src=_abs(root / "metadata.json"),
                dst=None,
                reason="补齐 metadata.json 必需字段",
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
                _add_delete(wrong, "docs/ 错位重复，根目录已有正确 docs/")
            for typo in typo_docs:
                _add_delete(typo, f"{typo.name}/ 命名错误，应为 docs/")
        else:
            if misplaced_docs:
                selected = misplaced_docs[0]
                _add_move(selected, docs_dir, "修复 docs/ 到根目录")
                for redundant in misplaced_docs[1:]:
                    _add_delete(redundant, "docs/ 候选重复，保留首个修复来源")
                for typo in typo_docs:
                    _add_delete(typo, f"{typo.name}/ 命名错误，已使用 docs/ 候选修复")
            elif typo_docs:
                selected = typo_docs[0]
                _add_move(selected, docs_dir, "修复 docs/ 命名并移动到根目录")
                for redundant in typo_docs[1:]:
                    _add_delete(redundant, "docs/ 命名候选重复，保留首个修复来源")

        # 1.3) docs 必要文档（questions/design/api-spec）归位与命名修复。
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
                    _add_delete(duplicate, f"docs/{required_name} 重复，保留一份")
                for path in candidates:
                    _add_delete(path, f"{required_name} 应位于 docs/，删除重复或错位候选")
                return

            if not candidates:
                return

            candidates.sort(key=lambda p: (0 if p.parent == root else 1, p.as_posix().lower()))
            _add_move(candidates[0], destination, f"修复 {required_name} 到 docs/{required_name}")
            for redundant in candidates[1:]:
                _add_delete(redundant, f"{required_name} 候选重复，保留首个修复来源")

        _align_docs_file("questions.md")
        _align_docs_file("design.md")
        _align_docs_file("api-spec.md")

        for prompt_candidate in self._collect_prompt_file_candidates():
            _add_delete(prompt_candidate, "prompt.md 已废弃，内容应保存在 metadata.prompt")

        # 2) 代码目录 repo 修复（兼容旧结构目录）。
        repo_dir = root / REPO_DIR_NAME
        legacy_dirs = list(self.legacy_project_dirs)
        if not repo_dir.is_dir():
            repo_candidates: list[Path] = [path for _, path in legacy_dirs]
            if self.project_type_dir is not None and self.project_type_dir not in repo_candidates:
                repo_candidates.append(self.project_type_dir)
            repo_candidates = sorted(repo_candidates, key=lambda p: p.name.lower())
            if repo_candidates:
                selected = repo_candidates[0]
                _add_move(selected, repo_dir, "修复代码目录命名/位置为 repo/")
                for redundant in repo_candidates[1:]:
                    _add_delete(redundant, "多余旧结构代码目录，保留一份迁移来源")
        else:
            for _, legacy_dir in legacy_dirs:
                _add_delete(legacy_dir, "旧结构代码目录冗余，已存在 repo/")

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
                _add_delete(duplicate, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 根目录重复，保留一份")
            for wrong in sessions_misplaced:
                _add_delete(wrong, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 位置错误，根目录已有正确目录")
            for typo in sessions_typos:
                _add_delete(typo, f"{typo.name}/ 命名错误，根目录已有正确 {ORIGINAL_SESSIONS_DIR_NAME}/")
        else:
            candidates = sessions_misplaced + sessions_typos
            if candidates:
                selected = candidates[0]
                _add_move(selected, sessions_dir, f"修复 {ORIGINAL_SESSIONS_DIR_NAME}/ 到根目录")
                moved_sessions_src = selected
                for redundant in candidates[1:]:
                    _add_delete(redundant, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 候选重复，保留首个修复来源")

        misplaced_session_files = self._collect_misplaced_legacy_session_json_files(sessions_dir)
        for src in misplaced_session_files:
            if moved_sessions_src is not None:
                try:
                    src.relative_to(moved_sessions_src)
                    continue
                except ValueError:
                    pass
            target = self._next_unique_filename_target(sessions_dir, src.name, move_dests)
            _add_move(src, target, f"旧会话 JSON 文件应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议后续打包为压缩文件）")

        # 4) 根目录额外目录清理（豁免仅影响执行，不影响提醒/计划）。
        allowed_root_dirs = ROOT_STANDARD_DIR_NAMES
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name in allowed_root_dirs:
                continue
            _add_delete(entry, "删除根目录规范外目录")

        # 5) 根目录额外文件清理（豁免仅影响执行，不影响提醒/计划）。
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name == "validation_report.md":
                continue
            if entry.name in ROOT_ALLOWED_FILES:
                continue
            _add_delete(entry, "删除根目录规范外文件")

        # 6) 修复模式下清理本地脏目录/文件（优先复用校验阶段结果，避免重复全盘扫描）。
        if self._dirty_findings_cache is not None:
            for path, reason, status in self._dirty_findings_cache:
                if status != "FAIL":
                    continue
                if path.is_dir():
                    _add_delete(path, f"删除本地脏目录：{reason}")
                else:
                    _add_delete(path, f"删除本地脏文件：{reason}")
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
                            _add_delete(dir_path, f"删除本地脏目录：{reason}")
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
                        _add_delete(file_path, f"删除本地脏文件：{reason}")

        # 删除动作去重：若父目录已删除，则子路径删除动作可省略。
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

        # A) docs 目录与核心文档归位（questions/design/api-spec）。
        docs_dir = root / "docs"
        _, misplaced_docs, typo_docs = self._collect_required_dir_candidates(
            "docs",
            {"doc", "document", "documents"},
        )
        if docs_dir.is_dir():
            for wrong in misplaced_docs:
                _add_delete(wrong, "docs/ 错位重复，根目录已有正确 docs/")
            for typo in typo_docs:
                _add_delete(typo, f"{typo.name}/ 命名错误，应为 docs/")
        else:
            if misplaced_docs:
                selected = misplaced_docs[0]
                _add_move(selected, docs_dir, "修复 docs/ 到根目录")
                for redundant in misplaced_docs[1:]:
                    _add_delete(redundant, "docs/ 候选重复，保留首个修复来源")
                for typo in typo_docs:
                    _add_delete(typo, f"{typo.name}/ 命名错误，已使用 docs/ 候选修复")
            elif typo_docs:
                selected = typo_docs[0]
                _add_move(selected, docs_dir, "修复 docs/ 命名并移动到根目录")
                for redundant in typo_docs[1:]:
                    _add_delete(redundant, "docs/ 命名候选重复，保留首个修复来源")

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
                    _add_delete(duplicate, f"docs/{required_name} 重复，保留一份")
                for path in candidates:
                    _add_delete(path, f"{required_name} 应位于 docs/，删除重复或错位候选")
                return

            if not candidates:
                return

            candidates.sort(key=lambda p: (0 if p.parent == root else 1, p.as_posix().lower()))
            _add_move(candidates[0], destination, f"修复 {required_name} 到 docs/{required_name}")
            for redundant in candidates[1:]:
                _add_delete(redundant, f"{required_name} 候选重复，保留首个修复来源")

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
            _add_move(source_dir, repo_dir, "旧结构代码目录迁移为 repo/")

        for _, legacy_dir in legacy_dirs:
            if source_dir is not None and legacy_dir == source_dir and not repo_dir.is_dir():
                continue
            if repo_dir.is_dir():
                nested_dst = repo_dir / legacy_dir.name
                if not nested_dst.exists():
                    _add_move(legacy_dir, nested_dst, "将残留旧结构目录并入 repo/")
                else:
                    _add_delete(legacy_dir, "残留旧结构目录与 repo 内容冲突，删除冗余目录")
            else:
                _add_delete(legacy_dir, "多余旧结构代码目录，保留单一迁移来源")

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
                _add_delete(duplicate, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 根目录重复，保留一份")
            for wrong in sessions_misplaced:
                _add_delete(wrong, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 位置错误，根目录已有正确目录")
            for typo in sessions_typos:
                _add_delete(typo, f"{typo.name}/ 命名错误，根目录已有正确 {ORIGINAL_SESSIONS_DIR_NAME}/")
        else:
            candidates = sessions_misplaced + sessions_typos
            if candidates:
                selected = candidates[0]
                _add_move(selected, sessions_dir, f"修复 {ORIGINAL_SESSIONS_DIR_NAME}/ 到根目录")
                moved_sessions_src = selected
                for redundant in candidates[1:]:
                    _add_delete(redundant, f"{ORIGINAL_SESSIONS_DIR_NAME}/ 候选重复，保留首个修复来源")

        misplaced_session_files = self._collect_misplaced_legacy_session_json_files(sessions_dir)
        for src in misplaced_session_files:
            if moved_sessions_src is not None:
                try:
                    src.relative_to(moved_sessions_src)
                    continue
                except ValueError:
                    pass
            target = self._next_unique_filename_target(sessions_dir, src.name, move_dests)
            _add_move(src, target, f"旧会话 JSON 文件应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议后续打包为压缩文件）")

        # D) metadata.json 迁移/补齐。
        metadata_correct, metadata_misplaced, metadata_typos = self._collect_required_file_candidates(
            "metadata.json",
            ROOT_REQUIRED_FILE_TYPO_ALIASES.get("metadata.json", set()),
        )
        metadata_path = root / "metadata.json"

        if metadata_correct:
            for duplicate in metadata_correct[1:]:
                _add_delete(duplicate, "metadata.json 根目录重复，保留一份")
            for wrong in metadata_misplaced:
                _add_delete(wrong, "metadata.json 位置错误，根目录已有正确文件")
            for typo in metadata_typos:
                _add_delete(typo, f"{typo.name} 命名错误，根目录已有 metadata.json")
        else:
            candidates = metadata_misplaced + metadata_typos
            if candidates:
                _add_move(candidates[0], metadata_path, "修复 metadata.json 到根目录")
                for redundant in candidates[1:]:
                    _add_delete(redundant, "metadata.json 候选重复，保留首个修复来源")

        _add_upsert_metadata(metadata_path, "补齐 metadata.json 必需字段", mode="legacy_convert")
        for prompt_candidate in self._collect_prompt_file_candidates():
            _add_delete(prompt_candidate, "prompt.md 已废弃，迁移时将写入 metadata.prompt")

        # E) 根目录额外目录清理（豁免仅影响执行，不影响提醒/计划）。
        allowed_root_dirs = ROOT_STANDARD_DIR_NAMES
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if entry.name in allowed_root_dirs:
                continue
            _add_delete(entry, "删除根目录规范外目录")

        # F) 根目录额外文件清理（豁免仅影响执行，不影响提醒/计划）。
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name == "validation_report.md":
                continue
            if entry.name in ROOT_ALLOWED_FILES:
                continue
            _add_delete(entry, "删除根目录规范外文件")

        # 删除动作去重：若父目录已删除，则子路径删除动作可省略。
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
            return True, "位于 .tmp 目录（豁免删除）"
        except ValueError:
            pass

        try:
            path.relative_to(backup_dir)
            return True, "位于 .backup 目录（豁免删除）"
        except ValueError:
            pass

        if path == tmp_dir:
            return True, ".tmp 目录（豁免删除）"
        if path == backup_dir:
            return True, ".backup 目录（豁免删除）"

        path_maybe_dir = path.is_dir() or (not path.exists() and path.suffix == "")
        if path_maybe_dir and self._is_compile_exempt_dir(path.name):
            return True, "编译产物目录（豁免删除，仅提醒）"
        if self._is_compile_exempt_file(path):
            return True, "编译产物文件（豁免删除，仅提醒）"

        if path.parent == root and path.suffix.lower() in ROOT_REPAIR_EXEMPT_ARCHIVE_EXTS:
            return True, "根目录压缩包（豁免删除）"

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
        # Linux 绝对路径（以 / 开头）归一化后通常会以 "-" 开头。
        return stem.startswith(LEADING_SESSION_ZIP_LINUX_PREFIX)

    def _is_windows_style_session_zip_stem(self, stem: str) -> bool:
        # Windows 绝对路径归一化后通常会以 "<盘符>--" 开头（大小写不敏感），
        # 例如 C-- / D-- / E--。
        return LEADING_SESSION_ZIP_WINDOWS_DRIVE_RE.match(stem) is not None

    def _is_dockerfile_candidate_filename(self, filename: str) -> bool:
        lower = filename.lower()
        if lower == "dockerfile":
            return True
        if lower.startswith("dockerfile."):
            return True
        if lower.endswith(".dockerfile"):
            return True
        return False

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
            return False, "系统未安装 sudo，无法执行提权重试"

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
            return False, "未找到 unzip/bsdtar，无法通过 sudo 完成提权解压"

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
                failures.append(f"{label} 超时: {SUDO_RETRY_TIMEOUT_SECONDS}s")
                continue
            except OSError as exc:
                failures.append(f"{label} 启动失败: {exc}")
                continue

            if proc.returncode == 0:
                return True, f"{label} 提权重试成功"

            stderr_text = self._truncate_subprocess_output(proc.stderr or "")
            stdout_text = self._truncate_subprocess_output(proc.stdout or "")
            if stderr_text:
                failures.append(f"{label} 失败: {stderr_text}")
            elif stdout_text:
                failures.append(f"{label} 失败: {stdout_text}")
            else:
                failures.append(f"{label} 失败: exit_code={proc.returncode}")

        return False, "；".join(failures)

    def _validate_and_extract_leading_session_zip(
        self, archive_path: Path, destination_dir: Path
    ) -> tuple[bool, str, list[str], bool]:
        # Return: (ok, detail, top_level_names, fatal_skip_followups)
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                infos = zf.infolist()
                if not infos:
                    return False, "压缩包为空", [], False

                broken_member = zf.testzip()
                if broken_member is not None:
                    return False, f"压缩包校验失败，损坏成员: {broken_member}", [], False

                destination_abs = destination_dir.resolve()
                top_level_names: set[str] = set()
                top_level_file_names: set[str] = set()
                top_level_nested_names: set[str] = set()

                for info in infos:
                    normalized_name = info.filename.replace("\\", "/").lstrip("/")
                    if not normalized_name:
                        continue

                    parts = [part for part in normalized_name.split("/") if part and part != "."]
                    if not parts:
                        continue
                    if any(part == ".." for part in parts):
                        return False, f"压缩包包含非法路径: {info.filename}", [], False
                    top_level_names.add(parts[0])
                    if len(parts) == 1 and not info.is_dir():
                        top_level_file_names.add(parts[0])
                    if len(parts) > 1:
                        top_level_nested_names.add(parts[0])

                    target = (destination_dir / "/".join(parts)).resolve()
                    try:
                        target.relative_to(destination_abs)
                    except ValueError:
                        return False, f"压缩包包含越界路径: {info.filename}", [], False

                if self._is_linux_style_session_zip_stem(archive_path.stem):
                    expected_top = archive_path.stem
                    if top_level_names != {expected_top}:
                        return (
                            False,
                            (
                                f"Linux 会话包解压顶层必须仅包含同名目录 {expected_top}/，"
                                f"实际顶层为: {', '.join(sorted(top_level_names)) or '空'}"
                            ),
                            [],
                            True,
                        )
                    if expected_top in top_level_file_names:
                        return (
                            False,
                            (
                                f"Linux 会话包顶层存在同名文件 {expected_top}，"
                                "必须为同名目录并在其下包含 session 内容"
                            ),
                            [],
                            True,
                        )
                    if expected_top not in top_level_nested_names:
                        return (
                            False,
                            (
                                f"Linux 会话包缺少同名目录 {expected_top}/ 下的有效内容，"
                                "不能直接在压缩包顶层展开 session_id/jsonl"
                            ),
                            [],
                            True,
                        )

                try:
                    zf.extractall(destination_dir)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EPERM}:
                        raise
                    retry_ok, retry_detail = self._retry_extract_with_sudo(archive_path, destination_dir)
                    if retry_ok:
                        return True, f"普通权限解压失败后已重试: {retry_detail}", sorted(top_level_names), False
                    return False, f"普通权限解压失败: {exc}；提权重试失败: {retry_detail}", [], False
            if not top_level_names:
                return False, "压缩包中未检测到有效内容", [], False
            return True, "解压成功", sorted(top_level_names), False
        except (OSError, RuntimeError, zipfile.BadZipFile, ValueError) as exc:
            return False, str(exc), [], False

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
        trailing_punct = string.punctuation + "，。！？；：、“”‘’（）【】《》—…"
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
                "reason": "reference_text 为空或无法提取锚点",
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
                "reason": "未在目标文本中找到由头尾锚点界定的候选文本段，已降级为整段相似度判定",
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
            return None, metadata_path, "metadata.json 不存在或不可读，无法读取 prompt 字段"

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, metadata_path, f"metadata.json 不是合法 JSON: {exc.msg}"

        if not isinstance(parsed, dict):
            return None, metadata_path, "metadata.json 顶层不是对象，无法读取 prompt 字段"

        project_type_raw = parsed.get("project_type")
        project_type_normalized = ""
        if isinstance(project_type_raw, str):
            project_type_normalized = project_type_raw.strip().lower()

        if project_type_normalized not in METADATA_PROJECT_TYPE_SET:
            allowed = ", ".join(METADATA_PROJECT_TYPE_ENUM)
            return (
                None,
                metadata_path,
                f"metadata.project_type 非法或缺失，无法进行锚点比对（仅允许: {allowed}）",
            )

        allow_empty_keys = _metadata_allow_empty_keys(project_type_normalized)

        for key in METADATA_REQUIRED_KEYS:
            if key not in parsed:
                return None, metadata_path, f"metadata.json 缺少必需字段 {key}，无法进行锚点比对"
            value = parsed.get(key)
            if not isinstance(value, str):
                return None, metadata_path, f"metadata.json 字段 {key} 不是字符串，无法进行锚点比对"
            normalized_value = value.strip()
            if not normalized_value and key not in allow_empty_keys:
                return None, metadata_path, f"metadata.json 字段 {key} 为空，无法进行锚点比对"
            if normalized_value and _is_invalid_metadata_placeholder(normalized_value) and key not in allow_empty_keys:
                return (
                    None,
                    metadata_path,
                    f"metadata.json 字段 {key} 为无效占位值（{value}），无法进行锚点比对",
                )

        prompt_text = str(parsed.get("prompt", ""))
        if not prompt_text.strip():
            return None, metadata_path, "metadata.prompt 为空，无法进行锚点比对"
        if _is_invalid_metadata_placeholder(prompt_text):
            return None, metadata_path, f"metadata.prompt 为无效占位值（{prompt_text}），无法进行锚点比对"

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
        analysis.total_lines = len(content.splitlines())
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
        role = str(message.get("role", "")).strip().lower()
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, list):
                non_empty_items = [item for item in content if isinstance(item, dict)]
                if non_empty_items and all(
                    str(item.get("type", "")).strip().lower() == "redacted_thinking" for item in non_empty_items
                ):
                    return False
            elif isinstance(content, dict):
                if str(content.get("type", "")).strip().lower() == "redacted_thinking":
                    return False
        if role == "user":
            content_text = self._extract_candidate_content_from_user_payload(payload, message)
            if self._is_session_prompt_compare_exempt_payload(payload, content_text):
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
        if not any(text_content.startswith(prefix) for prefix in local_command_prefixes):
            return False

        # `/plan` 命令的 <command-args> 可能包含完整任务 prompt，不能当作噪声丢弃。
        if self._extract_prompt_candidate_from_local_command(text_content):
            return False

        return True

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
            return False, "轨迹文件不可读"

        if analysis.last_semantic_line is None:
            return False, "未检测到有效 message 事件"

        last_line_no = analysis.last_semantic_line
        last_role = analysis.last_semantic_role or ""
        if last_role != "assistant":
            return False, f"最后一个 message 事件 role 不是 assistant（行号: {last_line_no}，role={last_role or 'unknown'}）"

        if not analysis.last_semantic_has_assistant_text:
            return False, f"最后一个 message 事件 content 未检测到 type=text（行号: {last_line_no}）"

        return True, f"最后一个 message 事件满足 assistant text 收尾（行号: {last_line_no}）"

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
                "最新轨迹完整性检查跳过：未在解压目录第一层 jsonl 中找到可用 timestamp",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        timestamp_candidates.sort(key=lambda item: (item[0], item[3].as_posix().lower()), reverse=True)
        latest_dt, latest_raw, latest_line_no, latest_jsonl = timestamp_candidates[0]
        complete, detail = self._evaluate_trajectory_tail_completeness(latest_jsonl)

        if complete:
            section.add_pass(
                (
                    "最新轨迹完整性检查通过 "
                    f"（目标文件={latest_jsonl.name}，文件内最新 timestamp={latest_raw}，行号={latest_line_no}，"
                    f"窗口基准时间(UTC)={latest_dt.isoformat()}）: {detail}"
                ),
                self._rel(latest_jsonl),
            )
            return

        section.add_fail(
            (
                "最新轨迹完整性检查失败 "
                f"（目标文件={latest_jsonl.name}，文件内最新 timestamp={latest_raw}，行号={latest_line_no}，"
                f"窗口基准时间(UTC)={latest_dt.isoformat()}）: {detail}"
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
        # Read 工具输出常见 `12→` 行号前缀，移除后更接近原始文件文本。
        return re.sub(r"(?m)^\s*\d+\s*→\s*", "", text)

    def _extract_prompt_candidate_from_local_command(self, content_text: str) -> str:
        text = content_text.strip()
        if not text:
            return ""

        name_match = re.search(
            r"<command-name>\s*(.*?)\s*</command-name>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not name_match:
            return ""
        command_name = name_match.group(1).strip().lower()
        if command_name != "/plan":
            return ""

        args_match = re.search(
            r"<command-args>\s*(.*?)\s*</command-args>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not args_match:
            return ""

        return args_match.group(1).strip()

    def _extract_candidate_content_from_user_payload(self, payload: dict[str, object], message: dict[str, object]) -> str:
        # 默认候选：message.content
        fallback = self._stringify_session_message_content(message.get("content"))
        fallback = self._strip_read_tool_line_number_prefixes(fallback)

        local_command_candidate = self._extract_prompt_candidate_from_local_command(fallback)
        if local_command_candidate:
            return local_command_candidate

        # 工具读取文件场景：优先使用 toolUseResult.file.content（原文，无行号）。
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

    def _evaluate_prompt_compare_for_jsonl(
        self,
        jsonl_path: Path,
        prompt_text: str,
        reference_normalized_len: int,
    ) -> dict[str, object]:
        analysis = self._analyze_jsonl_file(jsonl_path)
        window_candidates, exempt_line_count, user_line_count = self._collect_user_content_candidates_in_window_from_jsonl(
            jsonl_path,
            window_minutes=SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES,
        )

        result: dict[str, object] = {
            "jsonl_path": jsonl_path,
            "first_timestamp": analysis.first_timestamp_raw or "",
            "first_timestamp_line": analysis.first_timestamp_line or 0,
            "latest_timestamp": analysis.latest_timestamp_raw or "",
            "total_lines": analysis.total_lines,
            "window_candidates": len(window_candidates),
            "window_candidate_lines": [line_no for _, line_no, _ in window_candidates],
            "window_start_ts": window_candidates[0][2] if window_candidates else "",
            "window_start_line": window_candidates[0][1] if window_candidates else 0,
            "user_line_count": user_line_count,
            "exempt_line_count": exempt_line_count,
            "effective_candidates": 0,
            "effective_candidate_lines": [],
            "length_filtered_count": 0,
            "length_filtered_lines": [],
            "best_similarity": -1.0,
            "best_line_no": None,
            "best_timestamp": None,
            "best_diff_index": -1,
            "best_match_mode": "",
            "no_match_reason": None,
            "match_items": [],
        }

        if not window_candidates:
            if user_line_count > 0 and user_line_count == exempt_line_count:
                result["no_match_reason"] = "文件仅含豁免 user content"
            else:
                result["no_match_reason"] = "窗口内无可用 user 候选"
            return result

        for idx, (candidate_content, candidate_line_no, candidate_timestamp) in enumerate(window_candidates, start=1):
            candidate_normalized_len = len(self._normalize_for_compare(candidate_content))
            if (
                reference_normalized_len > 0
                and candidate_normalized_len < reference_normalized_len * SESSION_PROMPT_MIN_LENGTH_RATIO
            ):
                result["length_filtered_count"] = int(result["length_filtered_count"]) + 1
                length_filtered_lines = list(result.get("length_filtered_lines", []))
                length_filtered_lines.append(candidate_line_no)
                result["length_filtered_lines"] = length_filtered_lines
                continue

            result["effective_candidates"] = int(result["effective_candidates"]) + 1
            effective_candidate_lines = list(result.get("effective_candidate_lines", []))
            effective_candidate_lines.append(candidate_line_no)
            result["effective_candidate_lines"] = effective_candidate_lines
            compare_result = self._compare_by_anchor(
                prompt_text,
                candidate_content,
                anchor_len=SESSION_PROMPT_ANCHOR_LEN,
                similarity_threshold=SESSION_PROMPT_SIMILARITY_THRESHOLD,
            )
            matched_by_anchor = bool(compare_result.get("matched"))
            similarity = float(compare_result.get("similarity", 0.0))
            mode = "锚点命中" if matched_by_anchor else "锚点未命中（整段相似度）"

            if similarity > float(result["best_similarity"]):
                result["best_similarity"] = similarity
                result["best_line_no"] = candidate_line_no
                result["best_timestamp"] = candidate_timestamp
                result["best_diff_index"] = int(compare_result.get("diff_index", -1))
                result["best_match_mode"] = mode

            if not matched_by_anchor and result["no_match_reason"] is None:
                result["no_match_reason"] = str(compare_result.get("reason", "未命中锚点模式"))

            if bool(compare_result.get("near_duplicate")):
                match_items = list(result["match_items"])
                match_items.append((idx, candidate_line_no, candidate_timestamp, similarity, mode))
                result["match_items"] = match_items

        if int(result["effective_candidates"]) == 0:
            result["no_match_reason"] = (
                f"窗口候选均未达到长度门槛（candidate_norm_len/reference_norm_len < {SESSION_PROMPT_MIN_LENGTH_RATIO:.2f}）"
            )
        elif result["no_match_reason"] is None and float(result["best_similarity"]) < 0:
            result["no_match_reason"] = "窗口候选内容不足以进行有效比对"

        return result

    def _write_prompt_compare_statistics_file(
        self,
        prompt_source_path: Path,
        prompt_error: str | None,
        evaluations: list[dict[str, object]],
    ) -> Path | None:
        if self.root is None:
            return None

        output_path = self.root / ".tmp" / "jsonl_sorted_by_first_timestamp_prompt_match.md"
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        lines: list[str] = []
        lines.append("# JSONL Sorted By Internal First Timestamp")
        lines.append("")
        lines.append(f"- TASK: `{self.root.name}`")
        lines.append("- scope: `metadata.prompt 锚点失败后全量扫描统计`")
        lines.append(f"- prompt_source: `{prompt_source_path}`")
        lines.append(f"- prompt_error: `{prompt_error or 'none'}`")
        lines.append(f"- similarity_threshold: `{SESSION_PROMPT_SIMILARITY_THRESHOLD}`")
        lines.append(f"- anchor_len: `{SESSION_PROMPT_ANCHOR_LEN}`")
        lines.append(f"- window_minutes: `{SESSION_PROMPT_CANDIDATE_WINDOW_MINUTES}`")
        lines.append(f"- min_length_ratio: `{SESSION_PROMPT_MIN_LENGTH_RATIO}`")
        lines.append("")

        def _format_line_no_list(raw_lines: object, limit: int = 20) -> str:
            if not isinstance(raw_lines, list) or not raw_lines:
                return "-"
            line_numbers: list[int] = []
            for item in raw_lines:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value <= 0:
                    continue
                line_numbers.append(value)
            if not line_numbers:
                return "-"
            if len(line_numbers) <= limit:
                return ", ".join(str(value) for value in line_numbers)
            head = ", ".join(str(value) for value in line_numbers[:limit])
            return f"{head} ... 共{len(line_numbers)}行"

        lines.append(
            "| # | file | first_timestamp | latest_timestamp | total_lines | "
            "prompt_match_line(s) | match_timestamp(s) | best_similarity | window_candidates | "
            "window_candidate_lines | effective_candidates | effective_candidate_lines | "
            "length_filtered | length_filtered_lines | user_lines | exempt_user_lines | status | reason |"
        )
        lines.append(
            "|---|---|---|---|---:|---|---|---:|---:|---|---:|---|---:|---|---:|---:|---|---|"
        )

        for idx, item in enumerate(evaluations, start=1):
            match_items = list(item.get("match_items", []))
            match_lines = ", ".join(str(entry[1]) for entry in match_items) if match_items else "-"
            match_timestamps = ", ".join(str(entry[2]) for entry in match_items) if match_items else "-"
            status = "MATCH" if match_items else ("NO_WINDOW" if int(item.get("window_candidates", 0)) == 0 else "NO_MATCH")
            reason = str(item.get("no_match_reason") or "-").replace("|", "\\|")
            window_candidate_lines = _format_line_no_list(item.get("window_candidate_lines"))
            effective_candidate_lines = _format_line_no_list(item.get("effective_candidate_lines"))
            length_filtered_lines = _format_line_no_list(item.get("length_filtered_lines"))
            best_similarity = float(item.get("best_similarity", -1.0))
            if best_similarity < 0:
                best_similarity = 0.0
            lines.append(
                "| {idx} | `{file}` | `{first_ts}` | `{latest_ts}` | {total_lines} | {match_lines} | "
                "`{match_ts}` | {best_similarity:.6f} | {window_candidates} | {window_candidate_lines} | "
                "{effective_candidates} | {effective_candidate_lines} | {length_filtered} | {length_filtered_lines} | "
                "{user_lines} | {exempt_lines} | {status} | {reason} |".format(
                    idx=idx,
                    file=Path(str(item.get("jsonl_path"))).name,
                    first_ts=str(item.get("first_timestamp") or "N/A"),
                    latest_ts=str(item.get("latest_timestamp") or "N/A"),
                    total_lines=int(item.get("total_lines", 0)),
                    match_lines=match_lines,
                    match_ts=match_timestamps,
                    best_similarity=best_similarity,
                    window_candidates=int(item.get("window_candidates", 0)),
                    window_candidate_lines=window_candidate_lines,
                    effective_candidates=int(item.get("effective_candidates", 0)),
                    effective_candidate_lines=effective_candidate_lines,
                    length_filtered=int(item.get("length_filtered_count", 0)),
                    length_filtered_lines=length_filtered_lines,
                    user_lines=int(item.get("user_line_count", 0)),
                    exempt_lines=int(item.get("exempt_line_count", 0)),
                    status=status,
                    reason=reason,
                )
            )

        try:
            output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return None
        return output_path

    def _check_original_sessions_prompt_anchor_consistency(
        self, section: CheckSection, bundle_dirs: Iterable[Path]
    ) -> None:
        prompt_text, prompt_source_path, prompt_error = self._read_metadata_prompt_for_session_compare()
        if prompt_error is not None or prompt_text is None:
            section.add_warn(f"metadata.prompt 锚点比对跳过：{prompt_error}", self._rel(prompt_source_path))
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
                "metadata.prompt 锚点比对跳过：未在解压目录第一层 jsonl 中找到可用 timestamp",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
            return

        timestamp_candidates.sort(key=lambda item: (item[0], item[1].as_posix().lower()))
        reference_normalized_len = len(self._normalize_for_compare(prompt_text))
        _, first_jsonl, first_timestamp, first_timestamp_line = timestamp_candidates[0]
        first_eval = self._evaluate_prompt_compare_for_jsonl(first_jsonl, prompt_text, reference_normalized_len)
        first_match_items = list(first_eval.get("match_items", []))

        if first_match_items:
            first_match = first_match_items[0]
            match_idx = int(first_match[0])
            match_line_no = int(first_match[1])
            match_timestamp = str(first_match[2])
            match_similarity = float(first_match[3])
            pass_mode = str(first_match[4]) if first_match[4] else "未知"
            section.add_pass(
                (
                    "metadata.prompt 锚点比对通过（首文件匹配提前结束） "
                    f"（similarity={match_similarity:.6f} >= threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f}）"
                    f"（判定方式={pass_mode}） "
                    f"（目标文件={first_jsonl.name}，文件首 timestamp={first_timestamp}，文件首 timestamp 行号={first_timestamp_line}，"
                    f"窗口起始 timestamp={first_eval.get('window_start_ts') or 'N/A'}，窗口起始行号={int(first_eval.get('window_start_line', 0))}，"
                    f"命中行号={match_line_no}，命中 timestamp={match_timestamp}，窗口候选数={int(first_eval.get('window_candidates', 0))}，"
                    f"有效候选数={int(first_eval.get('effective_candidates', 0))}，长度过滤数={int(first_eval.get('length_filtered_count', 0))}，命中序号={match_idx}）"
                ),
                self._rel(first_jsonl),
            )
            return

        best_similarity = float(first_eval.get("best_similarity", -1.0))
        best_line_no = first_eval.get("best_line_no")
        best_timestamp = first_eval.get("best_timestamp")
        best_diff_index = int(first_eval.get("best_diff_index", -1))
        best_match_mode = str(first_eval.get("best_match_mode", ""))

        if best_similarity >= 0 and best_line_no is not None and best_timestamp is not None:
            diff_note = ""
            if best_diff_index >= 0:
                diff_note = f"，首个差异位置={best_diff_index}"
            fail_message = (
                "metadata.prompt 锚点比对失败（首文件判定）: 相似度不足 "
                f"（similarity={best_similarity:.6f} < threshold={SESSION_PROMPT_SIMILARITY_THRESHOLD:.6f}{diff_note}，判定方式={best_match_mode or '未知'}） "
                f"（目标文件={first_jsonl.name}，文件首 timestamp={first_timestamp}，文件首 timestamp 行号={first_timestamp_line}，"
                f"窗口起始 timestamp={first_eval.get('window_start_ts') or 'N/A'}，窗口起始行号={int(first_eval.get('window_start_line', 0))}，"
                f"最佳候选 timestamp={best_timestamp}，最佳候选行号={best_line_no}，窗口候选数={int(first_eval.get('window_candidates', 0))}，"
                f"有效候选数={int(first_eval.get('effective_candidates', 0))}，长度过滤数={int(first_eval.get('length_filtered_count', 0))}）"
            )
        else:
            reason = str(first_eval.get("no_match_reason") or "窗口内候选内容均未命中锚点模式")
            fail_message = (
                f"metadata.prompt 锚点比对失败（首文件判定）: {reason} "
                f"（目标文件={first_jsonl.name}，文件首 timestamp={first_timestamp}，文件首 timestamp 行号={first_timestamp_line}，"
                f"窗口起始 timestamp={first_eval.get('window_start_ts') or 'N/A'}，窗口起始行号={int(first_eval.get('window_start_line', 0))}，"
                f"窗口候选数={int(first_eval.get('window_candidates', 0))}，有效候选数={int(first_eval.get('effective_candidates', 0))}，"
                f"长度过滤数={int(first_eval.get('length_filtered_count', 0))}）"
            )
        section.add_fail(fail_message, self._rel(first_jsonl))

        evaluations: list[dict[str, object]] = [first_eval]
        for _, selected_jsonl, _, _ in timestamp_candidates[1:]:
            evaluations.append(self._evaluate_prompt_compare_for_jsonl(selected_jsonl, prompt_text, reference_normalized_len))

        summary_path = self._write_prompt_compare_statistics_file(prompt_source_path, prompt_error, evaluations)
        if summary_path is not None:
            matched_files = sum(1 for item in evaluations if list(item.get("match_items", [])))
            section.add_warn(
                (
                    "首文件失败后已完成全部 jsonl 匹配统计输出："
                    f"命中文件数={matched_files}/{len(evaluations)}"
                ),
                self._rel(summary_path),
            )
        else:
            section.add_warn(
                "首文件失败后尝试输出全部 jsonl 匹配统计文件失败",
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

    def _collect_all_original_sessions_json_and_jsonl_files(
        self, sessions_dir: Path
    ) -> tuple[list[Path], list[Path]]:
        jsonl_files: list[Path] = []
        json_files: list[Path] = []
        if not sessions_dir.is_dir():
            return jsonl_files, json_files

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
                lower_name = filename.lower()
                if lower_name.endswith(".jsonl"):
                    jsonl_files.append(current_path / filename)
                elif lower_name.endswith(".json"):
                    json_files.append(current_path / filename)
        jsonl_files.sort(key=lambda p: p.as_posix().lower())
        json_files.sort(key=lambda p: p.as_posix().lower())
        return jsonl_files, json_files

    def _collect_all_original_sessions_jsonl_files(self, sessions_dir: Path) -> list[Path]:
        jsonl_files, _ = self._collect_all_original_sessions_json_and_jsonl_files(sessions_dir)
        return jsonl_files

    def _check_original_sessions_json_and_jsonl_validity(self, section: CheckSection, sessions_dir: Path) -> None:
        jsonl_files, json_files = self._collect_all_original_sessions_json_and_jsonl_files(sessions_dir)
        total_files = len(jsonl_files) + len(json_files)
        if total_files == 0:
            section.add_warn(
                "original_sessions 下未检测到 json/jsonl 文件，跳过 JSON 合法性检查",
                self._rel(sessions_dir),
            )
            return

        failures = 0

        for jsonl_path in jsonl_files:
            content = self._read_text(jsonl_path)
            if content is None:
                section.add_fail("jsonl 文件不可读，无法进行 JSON 合法性检查", self._rel(jsonl_path))
                failures += 1
                continue

            invalid_line_numbers: list[int] = []
            for line_no, raw_line in enumerate(content.splitlines(), start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    invalid_line_numbers.append(line_no)

            if invalid_line_numbers:
                formatted = self._format_line_numbers(invalid_line_numbers)
                section.add_fail(
                    f"jsonl 存在非法 JSON 行（行号: {formatted}）",
                    self._rel(jsonl_path),
                )
                failures += 1

        for json_path in json_files:
            content = self._read_text(json_path)
            if content is None:
                section.add_fail("json 文件不可读，无法进行 JSON 合法性检查", self._rel(json_path))
                failures += 1
                continue

            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                section.add_fail(
                    f"json 非法: {exc.msg}（line={exc.lineno}, col={exc.colno}）",
                    self._rel(json_path),
                )
                failures += 1

        if failures == 0:
            section.add_pass(
                (
                    "original_sessions 下 JSON/JSONL 合法性检查通过 "
                    f"(jsonl={len(jsonl_files)}, json={len(json_files)})"
                ),
                self._rel(sessions_dir),
            )

    def _check_original_sessions_jsonl_forbidden_keywords(self, section: CheckSection, sessions_dir: Path) -> None:
        jsonl_files = self._collect_all_original_sessions_jsonl_files(sessions_dir)
        if not jsonl_files:
            section.add_warn("original_sessions 下未检测到 jsonl 文件，跳过禁止关键词检查", self._rel(sessions_dir))
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
                section.add_warn("jsonl 文件不可读，已跳过禁止关键词检查", self._rel(jsonl_path))
                continue

            for keyword in ORIGINAL_SESSIONS_JSONL_FORBIDDEN_KEYWORDS:
                line_numbers = keyword_lines_map.get(keyword, [])
                if not line_numbers:
                    continue
                findings += 1
                formatted = self._format_line_numbers(line_numbers)
                section.add_fail(
                    f'检测到禁止关键词 "{keyword}"（行号: {formatted}）',
                    self._rel(jsonl_path),
                )

        if findings == 0:
            section.add_pass("original_sessions/*.jsonl 未检测到禁止关键词", self._rel(sessions_dir))

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
                section.add_warn("token 统计跳过：第一层未检测到 jsonl 文件", self._rel(bundle_dir))
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
                    f"{bundle_dir.name}/ token 统计："
                    f"session_jsonl={session_jsonl_count}，"
                    f"subagent_jsonl={subagent_jsonl_count}，"
                    f"usage_records={usage_record_count:,}，"
                    f"input={bundle_tokens['input_tokens']:,}，"
                    f"output={bundle_tokens['output_tokens']:,}，"
                    f"cache_read={bundle_tokens['cache_read_tokens']:,}，"
                    f"cache_write={bundle_tokens['cache_write_tokens']:,}，"
                    f"total={bundle_total_sum:,}，"
                    f"subagent_total={bundle_sub_sum:,}（{bundle_sub_ratio:.1f}%），"
                    f"cost_usd=${bundle_cost_usd:,.4f}"
                ),
                self._rel(bundle_dir),
            )
            if unreadable_count > 0:
                section.add_warn(
                    f"{bundle_dir.name}/ token 统计中有 {unreadable_count} 个 jsonl 不可读，已跳过",
                    self._rel(bundle_dir),
                )

        if checked_bundles == 0:
            section.add_warn(
                "original_sessions token 统计跳过：未在解压目录中检测到可统计的第一层 jsonl",
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
                    "original_sessions token 汇总："
                    f"bundle={checked_bundles}，"
                    f"session_jsonl={total_session_files}，"
                    f"subagent_jsonl={total_subagent_files}，"
                    f"usage_records={total_usage_records:,}，"
                    f"input={total_tokens['input_tokens']:,}，"
                    f"output={total_tokens['output_tokens']:,}，"
                    f"cache_read={total_tokens['cache_read_tokens']:,}，"
                    f"cache_write={total_tokens['cache_write_tokens']:,}，"
                    f"total={overall_total_sum:,}，"
                    f"subagent_total={overall_sub_sum:,}（{sub_ratio:.1f}%），"
                    f"cost_usd=${overall_cost_usd:,.4f}"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

        if overall_cost_usd > TOKEN_COST_MAX_USD:
            section.add_warn(
                (
                    "original_sessions 题目开发成本提醒："
                    f"cost_usd=${overall_cost_usd:,.4f} > max_threshold=${TOKEN_COST_MAX_USD:,.2f}"
                    f"（project_type={threshold_project_type}），请注意 token 使用量"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
        elif overall_cost_usd < threshold_usd:
            section.add_warn(
                (
                    "original_sessions 题目开发成本提醒："
                    f"cost_usd=${overall_cost_usd:,.4f} < threshold=${threshold_usd:,.2f}"
                    f"（project_type={threshold_project_type}），请审查轨迹文件是否齐全"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )
        else:
            section.add_pass(
                (
                    "original_sessions 题目开发成本校验通过："
                    f"cost_usd=${overall_cost_usd:,.4f} >= threshold=${threshold_usd:,.2f}"
                    f"（project_type={threshold_project_type}）"
                ),
                self._rel(self.root / ORIGINAL_SESSIONS_DIR_NAME),
            )

        if total_unreadable_files > 0:
            section.add_warn(
                f"original_sessions token 汇总中有 {total_unreadable_files} 个 jsonl 不可读，已跳过",
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
                    f"{bundle_dir.name}/ 下 memory/ 已豁免：不做内容检查，也不要求 memory.jsonl 对齐",
                    self._rel(exempt_dir),
                )

        if not session_dirs:
            if exempt_child_dirs:
                section.add_pass(
                    f"{bundle_dir.name}/ 下未检测到需校验的 session_id 子目录（豁免目录已跳过）",
                    self._rel(bundle_dir),
                )
            else:
                section.add_warn(f"{bundle_dir.name}/ 下未检测到 session_id 子目录", self._rel(bundle_dir))
            return

        for session_dir in session_dirs:
            session_id = session_dir.name
            required_jsonl = bundle_dir / f"{session_id}.jsonl"
            if not required_jsonl.is_file():
                section.add_fail(
                    f"{bundle_dir.name}/ 中目录 {session_id}/ 缺少同名会话文件 {session_id}.jsonl",
                    self._rel(session_dir),
                )
                bundle_failures += 1

            subagents_dir = session_dir / "subagents"
            if not subagents_dir.is_dir():
                section.add_pass(
                    f"{session_id}/ 未检测到 subagents/（该目录可选，已跳过该项校验）",
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
                        f"{jsonl_file.name} 缺少对应元数据文件 {expected_meta.name}",
                        self._rel(jsonl_file),
                    )
                    bundle_failures += 1

        if bundle_failures == 0:
            section.add_pass(f"{bundle_dir.name}/ 子目录结构检查通过", self._rel(bundle_dir))

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
                return "FAIL", "metadata.json 路径为目录，无法写入"
            content = self._read_text(path)
            if content is None:
                return "FAIL", "metadata.json 非可读文本，无法自动补齐"
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                return "FAIL", f"metadata.json 非法 JSON: {exc.msg}"
            if not isinstance(parsed, dict):
                return "FAIL", "metadata.json 顶层不是对象，无法自动补齐"
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
            return "SKIP", "metadata.json 必需字段已完整"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            return "FAIL", str(exc)

        if changed_keys:
            prompt_note = ""
            if prompt_source is not None and "prompt" in changed_keys:
                prompt_note = f"；prompt 来源: {self._rel(prompt_source)}"
            return "DONE", "补齐字段: " + ", ".join(changed_keys) + prompt_note
        return "DONE", "创建 metadata.json"

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
                    print(f"[FAIL] UPDATE {self._rel(src)} | 目标是目录，无法写入 .gitignore")
                    failed += 1
                    continue

                if src.exists():
                    content = self._read_text(src)
                    if content is None:
                        print(f"[FAIL] UPDATE {self._rel(src)} | .gitignore 非可读文本，无法自动更新")
                        failed += 1
                        continue
                else:
                    content = ""

                updated_content, additions = self._build_gitignore_content_with_exemptions(content)
                if not additions:
                    print(f"[SKIP] UPDATE {self._rel(src)} | 豁免规则已存在")
                    skipped += 1
                    continue

                try:
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text(updated_content, encoding="utf-8")
                    print(
                        f"[DONE] UPDATE {self._rel(src)} | 新增规则: {', '.join(additions)}"
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
                    print(f"[SKIP] MERGE_TMP {self._rel(merge_src)} -> {self._rel(merge_dst)} | 源目录不存在")
                    skipped += 1
                    continue
                if not merge_src.is_dir():
                    print(f"[FAIL] MERGE_TMP {self._rel(merge_src)} | 源路径不是目录")
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
                    print(f"[SKIP] DELETE {self._rel(src)} | 源路径不存在")
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
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} | 目标路径为空")
                skipped += 1
                continue

            if not src.exists():
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} -> {self._rel(dst)} | 源路径不存在")
                skipped += 1
                continue

            if dst.exists():
                print(f"[SKIP] {action.kind.upper()} {self._rel(src)} -> {self._rel(dst)} | 目标已存在")
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
        section = self._new_section("1. 输入目录检查")
        resolved = self._resolve_input_directory()
        if resolved is None:
            section.add_fail(
                f"输入目录不存在或不可访问: {self.input_identifier}",
                self.input_identifier,
            )
            self._record_failures()
            return

        self.root = resolved
        self._gitignore_scopes_cache = None
        self._candidate_entries_cache = None
        self._dirty_findings_cache = None
        section.add_pass("输入目录合法", self._rel(resolved))
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
            section.add_pass(f"根目录存在 {expected_name}", self._rel(correct[0]))
        elif len(correct) > 1:
            section.add_fail(f"{expected_name} 在根目录出现多份，仅允许 1 份", self._rel(correct[0]))
            for dup in correct[1:max_items]:
                section.add_fail(f"{expected_name} 重复出现（根目录不应有多份）", self._rel(dup))
                explained_paths.add(dup)

        if misplaced:
            for path in misplaced[:max_items]:
                parent = self._rel(path.parent)
                if correct:
                    section.add_fail(
                        f"{expected_name} 位置错误：根目录已存在正确文件，该文件不应再放在 {parent}/",
                        self._rel(path),
                    )
                else:
                    section.add_fail(
                        f"{expected_name} 放置位置错误，应放在 TASK 根目录而不是 {parent}/",
                        self._rel(path),
                    )
                explained_paths.add(path)

        if typo_candidates:
            for path in typo_candidates[:max_items]:
                parent = self._rel(path.parent)
                if path.parent == self.root:
                    section.add_fail(
                        f"{path.name} 命名错误，应命名为 {expected_name}（应位于 TASK 根目录）",
                        self._rel(path),
                    )
                else:
                    section.add_fail(
                        f"{path.name} 命名错误，应命名为 {expected_name}，并应放在 TASK 根目录而不是 {parent}/",
                        self._rel(path),
                    )
                explained_paths.add(path)

        if not correct and not misplaced and not typo_candidates:
            section.add_fail(f"缺少 {expected_name}", expected_name)

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
            section.add_pass(f"根目录存在 {ORIGINAL_SESSIONS_DIR_NAME}/ 目录", self._rel(sessions_correct[0]))
        if len(sessions_correct) > 1:
            section.add_fail(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ 在根目录出现多份，仅允许 1 份",
                self._rel(sessions_correct[0]),
            )
            for path in sessions_correct[1:6]:
                section.add_fail(f"{ORIGINAL_SESSIONS_DIR_NAME}/ 重复出现（根目录不应有多份）", self._rel(path))
                explained_paths.add(path)

        if legacy_traj_root:
            for path in legacy_traj_root[:6]:
                section.add_fail(
                    f"根目录不应存在 trajectory.json，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议打包为压缩文件）",
                    self._rel(path),
                )
                explained_paths.add(path)

        for path in legacy_traj_misplaced[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"trajectory.json 为旧命名文件，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（当前位于 {parent}/）",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in legacy_traj_typos[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{path.name} 属于旧命名风格，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（当前位于 {parent}/）",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in sessions_misplaced[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ 位置错误，应放在 TASK 根目录而不是 {parent}/",
                self._rel(path),
            )
            explained_paths.add(path)

        for path in sessions_typos[:6]:
            parent = self._rel(path.parent)
            section.add_fail(
                f"{path.name}/ 命名错误，应命名为 {ORIGINAL_SESSIONS_DIR_NAME}/，并放在 TASK 根目录（当前位于 {parent}/）",
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
                f"检测到旧会话 JSON 文件，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（当前位于 {parent}/）",
                self._rel(path),
            )
            explained_paths.add(path)

        if not sessions_correct and not sessions_misplaced and not sessions_typos:
            section.add_fail(
                f"缺少 {ORIGINAL_SESSIONS_DIR_NAME}/ 目录（会话归档应统一放在该目录）",
                f"{ORIGINAL_SESSIONS_DIR_NAME}/",
            )

        return explained_paths

    def _root_extra_file_issue_message(self, path: Path) -> str:
        name_lower = path.name.lower()
        deprecated_prompt_names = set(ROOT_REQUIRED_FILE_TYPO_ALIASES.get("prompt.md", set())) | {"prompt.md"}

        if name_lower in deprecated_prompt_names:
            return "prompt.md 已废弃，请将内容放入 metadata.json 的 prompt 字段"

        question_names = set(ROOT_REQUIRED_FILE_TYPO_ALIASES.get("questions.md", set())) | {"questions.md"}
        if name_lower in question_names:
            return "questions.md 位置错误，应放在 docs/questions.md"

        typo_target = ROOT_COMMON_FILE_TYPOS.get(name_lower)
        if typo_target:
            return f"根目录文件名疑似错误，建议重命名为 {typo_target}"

        if name_lower == "trajectory.json":
            return f"根目录不应存在 trajectory.json，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议打包为压缩文件）"

        if LEGACY_SESSION_JSON_RE.fullmatch(name_lower):
            return f"根目录不应存在旧会话 JSON 文件，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议打包为压缩文件）"

        if name_lower == "readme.md":
            return "readme.md 位置错误，应放在 repo 目录下"

        return "根目录存在不允许的额外文件"

    def _check_root_fixed_files(self) -> None:
        section = self._new_section("2. 根目录固定文件检查")
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
            section.add_pass("根目录不存在规范外额外文件", ".")

        self._record_failures()

    def _check_repo_directory(self) -> None:
        section = self._new_section("3. 代码目录检查")
        assert self.root is not None

        repo_dir = self.root / REPO_DIR_NAME
        self.legacy_project_dirs = self._collect_legacy_project_directories(self.root)
        report_hints = True

        if repo_dir.is_dir():
            self.project_type_dir = repo_dir
            section.add_pass("代码目录存在且命名合规: repo/", self._rel(repo_dir))
            if self.legacy_project_dirs:
                legacy_desc = ", ".join(path.name for _, path in self.legacy_project_dirs[:5])
                section.add_warn(
                    "检测到旧版项目类型目录，建议执行 --convert-legacy 迁移为 repo 结构",
                    legacy_desc,
                )
        else:
            if len(self.legacy_project_dirs) == 1:
                legacy_type, legacy_dir = self.legacy_project_dirs[0]
                self.project_type_dir = legacy_dir
                self.project_type_name = legacy_type
                section.add_fail(
                    f"代码目录命名不合规：应使用 repo/，当前为旧结构目录 {legacy_dir.name}（已按该目录继续后续检查）",
                    self._rel(legacy_dir),
                )
                report_hints = False
            elif len(self.legacy_project_dirs) > 1:
                self.project_type_dir = self.legacy_project_dirs[0][1]
                self.project_type_name = self.legacy_project_dirs[0][0]
                found_desc = ", ".join(f"{path.name} -> {canonical}" for canonical, path in self.legacy_project_dirs)
                section.add_fail(
                    f"缺少 repo/，且检测到多个旧结构代码目录，不合规: {found_desc}",
                    ".",
                )
                report_hints = False
            else:
                inferred = self._infer_repo_candidate_from_common_dir()
                if inferred is not None:
                    self.project_type_dir = inferred
                    section.add_fail(
                        f"代码目录命名不合规：应使用 repo/，当前目录为 {inferred.name}（已按该目录继续后续检查）",
                        self._rel(inferred),
                    )
                else:
                    section.add_fail("缺少代码目录 repo/", "repo/")

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
            section.add_pass("代码目录存在 readme.md（建议项）", self._rel(root_readmes[0]))
        elif len(root_readmes) > 1:
            section.add_warn("repo 目录下 readme.md 出现多份，建议仅保留 1 份", self._rel(root_readmes[0]))
            for path in root_readmes[1:6]:
                section.add_warn("readme.md 重复出现（建议清理多余副本）", self._rel(path))

        if misplaced_readmes:
            for path in misplaced_readmes[:6]:
                section.add_warn(
                    "检测到 readme.md 放在 repo 外部（建议迁移到 repo/）",
                    self._rel(path),
                )

        if typo_readmes:
            for path in typo_readmes[:6]:
                parent = self._rel(path.parent)
                section.add_warn(
                    f"{path.name} 命名疑似不规范，建议使用 readme.md 并放在 repo 目录（当前位于 {parent}/）",
                    self._rel(path),
                )

        if not root_readmes and not misplaced_readmes and not typo_readmes:
            section.add_warn("代码目录未检测到 readme.md（建议提供）", self._rel(project_dir / "readme.md"))

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
                    f"检测到旧结构目录 {entry.name}，新规范应统一为 repo/ 代码目录",
                    entry,
                )
                continue
            suggested = PROJECT_TYPE_MISNAME_HINTS.get(normalized)
            if suggested is not None:
                _add_hint(
                    f"检测到目录 {entry.name} 疑似代码目录，建议迁移到 repo/（旧结构建议名: {suggested}）",
                    entry,
                )
                continue

            if normalized in {"backend", "server", "api", "service", "frontend", "web", "client", "ui"}:
                _add_hint(
                    f"检测到 {entry.name} 位于根目录，代码目录应统一为 repo/，该目录应位于 repo/ 内",
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
        section = self._new_section("4. original_sessions 文件组织检查")
        assert self.root is not None

        sessions_dir = self.root / ORIGINAL_SESSIONS_DIR_NAME
        has_sessions = sessions_dir.is_dir()
        if not has_sessions:
            section.add_fail(f"缺少 {ORIGINAL_SESSIONS_DIR_NAME}/ 目录", self._rel(sessions_dir))
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
                f"根目录存在旧会话 JSON 文件，应迁移到 {ORIGINAL_SESSIONS_DIR_NAME}/（建议打包为压缩文件）",
                self._rel(path),
            )

        leading_zip_archives = self._collect_leading_session_zip_archives(sessions_dir)
        extracted_bundle_dirs: dict[str, Path] = {}
        initial_dir_by_name: dict[str, Path] = {
            path.name.lower(): path for path in sessions_dir.iterdir() if path.is_dir()
        }

        if leading_zip_archives:
            section.add_pass(
                f"检测到 {len(leading_zip_archives)} 个会话包（Linux: -**.zip / Windows: C--*.zip），开始校验并解压",
                self._rel(sessions_dir),
            )
            extract_success = 0
            skipped_due_same_name = 0
            fatal_archive_layout_failed = False
            for archive_path in leading_zip_archives:
                same_name_existing_dir: Path | None = None
                direct_same_name_dir = sessions_dir / archive_path.stem
                if direct_same_name_dir.is_dir():
                    same_name_existing_dir = direct_same_name_dir
                else:
                    same_name_existing_dir = initial_dir_by_name.get(archive_path.stem.lower())
                if same_name_existing_dir is not None and same_name_existing_dir.is_dir():
                    section.add_warn(
                        f"{archive_path.name} 存在同名预存目录 {same_name_existing_dir.name}/，跳过解压并跳过删除",
                        self._rel(archive_path),
                    )
                    extracted_bundle_dirs[same_name_existing_dir.as_posix().lower()] = same_name_existing_dir
                    self._mark_session_skip_cleanup_dir(same_name_existing_dir)
                    skipped_due_same_name += 1
                    continue

                ok, detail, top_level_names, fatal_skip_followups = self._validate_and_extract_leading_session_zip(
                    archive_path, sessions_dir
                )
                if ok:
                    success_message = f"已校验并解压: {archive_path.name}"
                    if detail and detail != "解压成功":
                        success_message += f"（{detail}）"
                    section.add_pass(success_message, self._rel(archive_path))
                    extract_success += 1
                    archive_bundle_count = 0
                    for top_name in top_level_names:
                        candidate = sessions_dir / top_name
                        if candidate.is_dir():
                            extracted_bundle_dirs[candidate.as_posix().lower()] = candidate
                            if top_name.lower() not in initial_dir_by_name:
                                self._mark_session_extracted_cleanup_dir(candidate)
                            archive_bundle_count += 1
                    if archive_bundle_count == 0:
                        section.add_fail(
                            f"{archive_path.name} 解压后未检测到可审查目录",
                            self._rel(archive_path),
                        )
                else:
                    section.add_fail(f"{archive_path.name} 校验/解压失败: {detail}", self._rel(archive_path))
                    if fatal_skip_followups:
                        fatal_archive_layout_failed = True

            if extract_success > 0:
                self._candidate_entries_cache = None
                self._dirty_findings_cache = None

            if skipped_due_same_name > 0:
                section.add_warn(
                    f"有 {skipped_due_same_name} 个会话包因同名目录已存在而跳过解压，这些目录不会在报告后删除",
                    self._rel(sessions_dir),
                )
            if fatal_archive_layout_failed:
                section.add_warn(
                    "检测到 Linux 会话包顶层结构不合规（未包裹同名顶层目录），跳过 original_sessions 后续检查",
                    self._rel(sessions_dir),
                )
                self._record_failures()
                return
        else:
            section.add_fail(
                "未检测到会话包（Linux: -**.zip / Windows: C--*.zip）",
                self._rel(sessions_dir),
            )

        if self._session_extracted_dirs_for_cleanup:
            for cleanup_dir in sorted(
                self._session_extracted_dirs_for_cleanup.values(), key=lambda p: p.as_posix().lower()
            ):
                section.add_pass(
                    f"{cleanup_dir.name}/ 为本次临时解压目录，将在报告生成后自动删除",
                    self._rel(cleanup_dir),
                )

        entries = sorted(sessions_dir.iterdir(), key=lambda p: p.name.lower())
        if not entries:
            section.add_fail(f"{ORIGINAL_SESSIONS_DIR_NAME}/ 目录为空", self._rel(sessions_dir))
            self._record_failures()
            return

        archive_count = 0
        checked_bundle_paths = set(extracted_bundle_dirs.values())
        for entry in entries:
            if entry.is_dir():
                if entry in checked_bundle_paths:
                    continue
                section.add_warn(
                    f"{ORIGINAL_SESSIONS_DIR_NAME}/ 下存在子目录（按附加产物处理）",
                    self._rel(entry),
                )
                continue
            if self._is_session_archive_file(entry):
                archive_count += 1
                continue
            section.add_warn(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ 中存在非压缩文件（建议使用压缩包归档会话）",
                self._rel(entry),
            )

        if archive_count > 0:
            section.add_pass(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ 下检测到 {archive_count} 个会话压缩包",
                self._rel(sessions_dir),
            )
        else:
            section.add_fail(
                f"{ORIGINAL_SESSIONS_DIR_NAME}/ 下未检测到压缩包（例如 .zip/.rar/.7z/.tar/.gz）",
                self._rel(sessions_dir),
            )

        if extracted_bundle_dirs:
            for bundle_dir in sorted(extracted_bundle_dirs.values(), key=lambda p: p.name.lower()):
                self._check_leading_bundle_dir_structure(section, bundle_dir)
            self._check_original_sessions_token_usage(section, extracted_bundle_dirs.values())
            self._check_original_sessions_prompt_anchor_consistency(section, extracted_bundle_dirs.values())
            self._check_latest_trajectory_file_completeness(section, extracted_bundle_dirs.values())
        else:
            section.add_warn("未检测到 zip 解压后的可审查目录，跳过子目录结构审查", self._rel(sessions_dir))
            section.add_warn("未检测到 zip 解压后的可审查目录，跳过 token 统计", self._rel(sessions_dir))
            section.add_warn("未检测到 zip 解压后的可审查目录，跳过 metadata.prompt 锚点比对", self._rel(sessions_dir))
            section.add_warn("未检测到 zip 解压后的可审查目录，跳过最新轨迹完整性检查", self._rel(sessions_dir))

        self._check_original_sessions_json_and_jsonl_validity(section, sessions_dir)
        self._check_original_sessions_jsonl_forbidden_keywords(section, sessions_dir)

        self._record_failures()

    def _resolve_effective_project_type(self) -> str:
        project_type_raw = ""
        project_type_obj = self.metadata.get("project_type") if isinstance(self.metadata, dict) else None
        if isinstance(project_type_obj, str):
            project_type_raw = project_type_obj.strip().lower()
        if not project_type_raw and self.project_type_name:
            project_type_raw = self.project_type_name.strip().lower()

        if project_type_raw in {"full_stack", "full-stack"}:
            return "fullstack"
        if project_type_raw in {"backend", "pure_backend"}:
            return "server"
        if project_type_raw in {"frontend", "pure_frontend"}:
            return "web"
        return project_type_raw

    def _detect_backend_content(self) -> None:
        if self.backend_content is not None:
            return

        project_type_value = self._resolve_effective_project_type()
        if project_type_value == "server":
            self.backend_content = True
            self.backend_reason = f"metadata.project_type={project_type_value}"
            return
        if project_type_value == "fullstack":
            self.backend_content = True
            self.backend_reason = f"metadata.project_type={project_type_value}"
            return

        self.backend_content = False
        if project_type_value:
            self.backend_reason = f"metadata.project_type={project_type_value}"
        else:
            self.backend_reason = "metadata.project_type 缺失，按非后端处理"

    def _check_docs_directory(self) -> None:
        section = self._new_section("6. docs 目录及设计文档检查")
        assert self.root is not None

        docs_dir = self.root / "docs"
        if docs_dir.is_dir():
            section.add_pass("docs/ 目录存在", self._rel(docs_dir))
        else:
            _, misplaced_docs, typo_docs = self._collect_required_dir_candidates("docs", {"doc", "document", "documents"})
            if misplaced_docs or typo_docs:
                for path in misplaced_docs[:5]:
                    section.add_fail(
                        f"docs/ 目录放置位置错误，应位于 TASK 根目录而不是 {self._rel(path.parent)}/",
                        self._rel(path),
                    )
                for path in typo_docs[:5]:
                    section.add_fail(
                        f"{path.name}/ 目录命名错误，应命名为 docs/ 并位于 TASK 根目录",
                        self._rel(path),
                    )
            else:
                section.add_fail("缺少 docs/ 目录", "docs/")
            self._record_failures()
            return

        design_doc = docs_dir / "design.md"
        if design_doc.is_file():
            section.add_pass("存在 docs/design.md", self._rel(design_doc))
        else:
            section.add_fail("缺少 docs/design.md", self._rel(design_doc))

        questions_doc = docs_dir / "questions.md"
        if questions_doc.is_file():
            section.add_pass("存在 docs/questions.md", self._rel(questions_doc))
        else:
            section.add_fail("缺少 docs/questions.md", self._rel(questions_doc))

        api_spec = docs_dir / "api-spec.md"
        effective_project_type = self._resolve_effective_project_type()
        if effective_project_type in PROJECT_TYPES_REQUIRE_API_SPEC:
            if api_spec.is_file():
                section.add_pass(
                    f"{effective_project_type} 项目存在 docs/api-spec.md",
                    self._rel(api_spec),
                )
            else:
                section.add_fail(
                    f"{effective_project_type} 项目缺少 docs/api-spec.md",
                    self._rel(api_spec),
                )
        else:
            section.add_pass(
                f"project_type={effective_project_type or 'unknown'}，docs/api-spec.md 非必需",
                self._rel(api_spec),
            )

        self._record_failures()

    def _check_metadata_file(self) -> None:
        section = self._new_section("5. metadata.json 检查")
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
                section.add_fail("缺少 metadata.json，无法执行元数据字段检查", "metadata.json")
                self.metadata = {}
                self._record_failures()
                return

        assert source_path is not None
        content = self._read_text(source_path)
        if content is None:
            section.add_fail("metadata.json 非可读文本", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            section.add_fail(f"metadata.json 不是合法 JSON: {exc.msg}", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        if not isinstance(parsed, dict):
            section.add_fail("metadata.json 顶层必须为 JSON 对象", self._rel(source_path))
            self.metadata = {}
            self._record_failures()
            return

        self.metadata = parsed
        self.metadata_source_path = source_path

        if from_nonstandard:
            section.add_warn(
                "根目录 metadata.json 缺失，已使用错位/命名错误文件继续字段检查（请先修复第2项）",
                self._rel(source_path),
            )
        else:
            section.add_pass("根目录存在 metadata.json", self._rel(source_path))

        expected_cwd = str(self.root.resolve())
        cwd_value = parsed.get("cwd")
        if not isinstance(cwd_value, str) or not cwd_value.strip():
            parsed["cwd"] = expected_cwd
            self.metadata = parsed
            try:
                source_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                section.add_fail(f'metadata.json 缺少 "cwd" 且自动写入失败: {exc}', self._rel(source_path))
            else:
                section.add_warn(
                    f'metadata.json 缺少 "cwd"，已自动补齐为题目绝对路径: {expected_cwd}',
                    self._rel(source_path),
                )
        else:
            section.add_pass('metadata.json 字段 "cwd" 存在', self._rel(source_path))

        project_type_raw = parsed.get("project_type")
        project_type_normalized = ""
        if isinstance(project_type_raw, str):
            project_type_normalized = project_type_raw.strip().lower()

        allow_empty_keys = _metadata_allow_empty_keys(project_type_normalized)

        for key in METADATA_REQUIRED_KEYS:
            if key not in parsed:
                section.add_fail(f"metadata.json 缺少必需字段: {key}", self._rel(source_path))
                continue

            value = parsed.get(key)
            if not isinstance(value, str):
                section.add_fail(f"metadata.json 字段 {key} 必须为非空字符串", self._rel(source_path))
                continue

            normalized = value.strip()
            if not normalized:
                if key in allow_empty_keys:
                    section.add_pass(
                        f"metadata.json 字段 {key} 允许为空（project_type={project_type_normalized}）",
                        self._rel(source_path),
                    )
                    continue
                section.add_fail(f"metadata.json 字段 {key} 不能为空", self._rel(source_path))
                continue

            if key == "project_type":
                project_type_value = normalized.lower()
                if project_type_value not in METADATA_PROJECT_TYPE_SET:
                    allowed = ", ".join(METADATA_PROJECT_TYPE_ENUM)
                    section.add_fail(
                        f"metadata.json 字段 project_type 非法: {value}（仅允许: {allowed}）",
                        self._rel(source_path),
                    )
                    continue
                section.add_pass(
                    f"metadata.json 字段 project_type 合法: {project_type_value}",
                    self._rel(source_path),
                )
                continue

            if _is_invalid_metadata_placeholder(normalized) and key not in allow_empty_keys:
                section.add_fail(
                    f"metadata.json 字段 {key} 为无效占位值: {value}",
                    self._rel(source_path),
                )
                continue

            section.add_pass(f"metadata.json 字段 {key} 为非空字符串", self._rel(source_path))

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
        section = self._new_section("7. metadata.prompt 英文模式判定")
        assert self.root is not None

        source_path = self.metadata_source_path or (self.root / "metadata.json")
        prompt_value = self.metadata.get("prompt")
        prompt_text = ""
        prompt_source = self._rel(source_path)

        if prompt_value is not None:
            if not isinstance(prompt_value, str):
                prompt_text = str(prompt_value)
                section.add_warn("metadata.prompt 非字符串，已按字符串形式参与判定", self._rel(source_path))
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
                    "metadata.prompt 缺失或为空，已回退使用 prompts.md/prompt.md 进行英文模式判定",
                    prompt_source,
                )
            else:
                section.add_fail(
                    "metadata.prompt 缺失且未找到可用 prompts.md（或 prompt.md）进行回退判定",
                    self._rel(source_path),
                )
                self.english_mode = False
                self._record_failures()
                return

        english_ratio = self._calc_prompt_english_ratio(prompt_text)
        self.english_mode = english_ratio > PROMPT_ENGLISH_RATIO_THRESHOLD

        if self.english_mode:
            section.add_pass(
                f"metadata.prompt 英文字符占比 {english_ratio:.2%} > 70%，启用英文一致性模式",
                prompt_source,
            )
        else:
            section.add_pass(
                f"metadata.prompt 英文字符占比 {english_ratio:.2%} <= 70%，不启用英文一致性模式",
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
        return f"{head} ... 共{len(line_numbers)}行"

    def _check_english_consistency(self) -> None:
        section = self._new_section("8. 文本文件中文字符检查")
        assert self.root is not None

        if not self.english_mode:
            section.add_pass("未启用英文一致性模式，跳过中文字符检查", ".")
            self._record_failures()
            return

        failures = 0
        for path, content in self._iter_readable_text_files():
            line_numbers = self._find_chinese_line_numbers(content)
            if line_numbers:
                failures += 1
                formatted = self._format_line_numbers(line_numbers)
                section.add_fail(
                    f"检测到中文字符（英文一致性模式不允许，行号: {formatted}）",
                    self._rel(path),
                )

        if failures == 0:
            section.add_pass("未检测到中文字符", ".")

        self._record_failures()

    def _check_backend_content_recognition(self) -> None:
        section = self._new_section("9. 后端内容识别")
        assert self.root is not None

        self._detect_backend_content()
        if self.project_type_dir is None:
            section.add_fail("无法判定后端内容：代码目录不可用", ".")
            self._record_failures()
            return

        if self.backend_content:
            section.add_pass(f"检测到后端内容: {self.backend_reason}", self._rel(self.project_type_dir))
        else:
            section.add_pass(f"未检测到后端内容: {self.backend_reason}", self._rel(self.project_type_dir))

        self._record_failures()

    def _check_backend_project_requirements(self) -> None:
        section = self._new_section("10. 项目附加检查（web/server/fullstack）")
        assert self.root is not None

        if self.project_type_dir is None:
            section.add_fail("代码目录不可用，无法执行项目附加检查", ".")
            self._record_failures()
            return

        effective_project_type = self._resolve_effective_project_type()
        if effective_project_type not in PROJECT_TYPES_REQUIRE_DOCKER_AND_TEST:
            section.add_pass(
                f"project_type={effective_project_type or 'unknown'}，跳过 Docker/Compose/run_tests.sh 检查",
                self._rel(self.project_type_dir),
            )
            self._record_failures()
            return

        compose_candidates = [
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
        ]
        run_tests_candidates = ["run_tests.sh"]
        compose_paths, test_paths, fallback_dockerfiles = self._scan_backend_requirement_files(
            self.project_type_dir,
            compose_candidates,
            run_tests_candidates,
        )

        compose_found = [p.name for p in compose_paths]
        if compose_found:
            section.add_pass(
                f"{effective_project_type} 项目存在 Compose 文件: {compose_found[0]}",
                self._rel(compose_paths[0]),
            )
        else:
            section.add_fail(
                (
                    f"{effective_project_type} 项目缺少 Compose 文件"
                    "（compose.yaml/compose.yml/docker-compose.yaml/docker-compose.yml）"
                ),
                self._rel(self.project_type_dir),
            )

        dockerfile_checked_by_compose = False
        if compose_paths:
            dockerfile_refs, compose_parse_issues = self._collect_compose_dockerfile_references(compose_paths)
            for issue in compose_parse_issues[:5]:
                section.add_warn(f"Compose 解析提示: {issue}", self._rel(self.project_type_dir))
            if len(compose_parse_issues) > 5:
                section.add_warn(
                    f"Compose 解析提示过多，已省略 {len(compose_parse_issues) - 5} 条",
                    self._rel(self.project_type_dir),
                )

            if dockerfile_refs:
                dockerfile_checked_by_compose = True
                missing_refs = [ref for ref in dockerfile_refs if not ref.dockerfile_path.is_file()]
                if not missing_refs:
                    display_refs = ", ".join(
                        f"{ref.service_name}->{self._rel(ref.dockerfile_path)}"
                        for ref in dockerfile_refs[:3]
                    )
                    if len(dockerfile_refs) > 3:
                        display_refs += f" 等{len(dockerfile_refs)}项"
                    section.add_pass(
                        f"{effective_project_type} 项目 Dockerfile 引用校验通过: {display_refs}",
                        self._rel(dockerfile_refs[0].dockerfile_path),
                    )
                else:
                    missing_desc = ", ".join(
                        f"{ref.service_name}:{self._rel(ref.dockerfile_path)}"
                        for ref in missing_refs[:3]
                    )
                    if len(missing_refs) > 3:
                        missing_desc += f" 等{len(missing_refs)}项"
                    section.add_fail(
                        f"{effective_project_type} 项目存在 Dockerfile 引用缺失: {missing_desc}",
                        self._rel(missing_refs[0].compose_path),
                    )
            else:
                section.add_warn(
                    f"{effective_project_type} 项目 Compose 中未定位到可用 Dockerfile 引用，回退为 repo 目录扫描",
                    self._rel(compose_paths[0]),
                )

        if not dockerfile_checked_by_compose:
            if fallback_dockerfiles:
                display_paths = ", ".join(self._rel(p) for p in fallback_dockerfiles[:3])
                if len(fallback_dockerfiles) > 3:
                    display_paths += f" 等{len(fallback_dockerfiles)}处"
                if compose_paths:
                    message = (
                        f"{effective_project_type} 项目 Dockerfile 回退扫描通过（Compose 未定位到可用引用）: "
                        f"{display_paths}"
                    )
                else:
                    message = f"{effective_project_type} 项目 Dockerfile 回退扫描通过: {display_paths}"
                section.add_pass(message, self._rel(fallback_dockerfiles[0]))
            else:
                if compose_paths:
                    message = (
                        f"{effective_project_type} 项目 Dockerfile 检查失败：Compose 未定位到可用引用，"
                        "且 repo 目录回退扫描未发现 Dockerfile"
                    )
                    rel_path = self._rel(compose_paths[0])
                else:
                    message = f"{effective_project_type} 项目缺少 Dockerfile（Compose 未找到且回退扫描未发现）"
                    rel_path = self._rel(self.project_type_dir)
                section.add_fail(message, rel_path)

        tests_found = [p.name for p in test_paths]
        if tests_found:
            section.add_pass(
                f"{effective_project_type} 项目存在统一测试启动脚本: {tests_found[0]}",
                self._rel(test_paths[0]),
            )
        else:
            section.add_fail(
                f"{effective_project_type} 项目缺少统一测试启动脚本（run_tests.sh）",
                self._rel(self.project_type_dir),
            )

        if effective_project_type in PROJECT_TYPES_REQUIRE_API_SPEC:
            api_spec = self.root / "docs" / "api-spec.md"
            if api_spec.is_file():
                section.add_pass(f"{effective_project_type} 项目存在 docs/api-spec.md", self._rel(api_spec))
            else:
                section.add_fail(f"{effective_project_type} 项目缺少 docs/api-spec.md", self._rel(api_spec))

        self._record_failures()

    def _scan_backend_requirement_files(
        self,
        base_dir: Path,
        compose_candidates: list[str],
        run_tests_candidates: list[str],
    ) -> tuple[list[Path], list[Path], list[Path]]:
        compose_files: list[Path] = []
        test_files: list[Path] = []
        dockerfiles: list[Path] = []

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
                if self._is_dockerfile_candidate_filename(filename):
                    dockerfiles.append(path)
                if lower in compose_set:
                    compose_files.append(path)
                if lower in test_set:
                    test_files.append(path)

        dockerfiles.sort(key=lambda p: p.as_posix().lower())
        compose_files.sort(key=lambda p: p.as_posix().lower())
        test_files.sort(key=lambda p: p.as_posix().lower())
        return compose_files, test_files, dockerfiles

    def _collect_compose_dockerfile_references(
        self,
        compose_paths: list[Path],
    ) -> tuple[list[ComposeDockerfileRef], list[str]]:
        refs: list[ComposeDockerfileRef] = []
        issues: list[str] = []

        for compose_path in compose_paths:
            services, issue = self._read_compose_services(compose_path)
            if issue is not None:
                issues.append(f"{self._rel(compose_path)}: {issue}")
                continue

            for service_name, service_config in services.items():
                if not isinstance(service_name, str):
                    continue
                if not isinstance(service_config, dict):
                    continue

                build_config = service_config.get("build")
                if isinstance(build_config, str):
                    context_value = build_config.strip() or "."
                    dockerfile_value = "Dockerfile"
                elif isinstance(build_config, dict):
                    context_value = self._to_non_empty_text(build_config.get("context")) or "."
                    dockerfile_value = self._to_non_empty_text(build_config.get("dockerfile")) or "Dockerfile"
                else:
                    continue

                context_dir = self._resolve_compose_context_dir(compose_path, context_value)
                dockerfile_path = self._resolve_compose_dockerfile_path(context_dir, dockerfile_value)
                refs.append(
                    ComposeDockerfileRef(
                        compose_path=compose_path,
                        service_name=service_name,
                        dockerfile_path=dockerfile_path,
                    )
                )

        deduped: dict[tuple[str, str, str], ComposeDockerfileRef] = {}
        for ref in refs:
            key = (
                ref.compose_path.as_posix().lower(),
                ref.service_name.lower(),
                ref.dockerfile_path.as_posix().lower(),
            )
            if key not in deduped:
                deduped[key] = ref

        ordered = sorted(
            deduped.values(),
            key=lambda ref: (
                ref.compose_path.as_posix().lower(),
                ref.service_name.lower(),
                ref.dockerfile_path.as_posix().lower(),
            ),
        )
        return ordered, issues

    def _read_compose_services(self, compose_path: Path) -> tuple[dict[str, object], str | None]:
        text = self._read_text(compose_path)
        if text is None:
            return {}, "文件不可读"

        if yaml is None:
            return {}, "当前环境缺少 PyYAML，无法解析 compose"

        try:
            payload = yaml.safe_load(text)
        except Exception as exc:
            return {}, f"YAML 解析失败: {exc}"

        if payload is None:
            return {}, "compose 文件为空"
        if not isinstance(payload, dict):
            return {}, "compose 顶层结构不是对象"

        services = payload.get("services")
        if services is None:
            return {}, "未找到 services 字段"
        if not isinstance(services, dict):
            return {}, "services 字段类型不是对象"
        return services, None

    def _to_non_empty_text(self, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        text = str(value).strip()
        return text or None

    def _is_windows_absolute_path_text(self, value: str) -> bool:
        return re.match(r"^[A-Za-z]:[\\/]", value) is not None

    def _resolve_compose_context_dir(self, compose_path: Path, context_value: str) -> Path:
        context_text = context_value.strip() or "."
        context_path = Path(context_text)
        if context_path.is_absolute() or self._is_windows_absolute_path_text(context_text):
            return context_path
        return (compose_path.parent / context_path).resolve()

    def _resolve_compose_dockerfile_path(self, context_dir: Path, dockerfile_value: str) -> Path:
        dockerfile_text = dockerfile_value.strip() or "Dockerfile"
        dockerfile_path = Path(dockerfile_text)
        if dockerfile_path.is_absolute() or self._is_windows_absolute_path_text(dockerfile_text):
            return dockerfile_path
        return (context_dir / dockerfile_path).resolve()

    def _check_gitignore_exists(self) -> None:
        section = self._new_section("11. .gitignore 存在性检查")
        assert self.root is not None

        root_gitignore = self.root / ".gitignore"
        repo_gitignore = (self.project_type_dir / ".gitignore") if self.project_type_dir is not None else None

        found_count = 0
        if root_gitignore.is_file():
            section.add_pass("根目录存在 .gitignore", self._rel(root_gitignore))
            found_count += 1
        if repo_gitignore is not None and repo_gitignore.is_file():
            section.add_pass("代码目录存在 .gitignore", self._rel(repo_gitignore))
            found_count += 1

        if found_count == 0:
            rel = self._rel(repo_gitignore) if repo_gitignore is not None else ".gitignore"
            effective_pt = self._resolve_effective_project_type()
            if effective_pt in PROJECT_TYPES_REQUIRE_DOCKER_AND_TEST:
                section.add_fail(
                    f"未检测到 .gitignore（{effective_pt} 项目必须提供，可在根目录或 repo 目录中）",
                    rel,
                )
            else:
                section.add_warn("未检测到 .gitignore（可在根目录或 repo 目录中提供）", rel)

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
            entries.append(line)
        return entries

    def _parse_gitignore_entry(self, entry: str) -> tuple[str, bool]:
        text = entry.strip()
        if not text:
            return "", False

        negated = text.startswith("!")
        if negated:
            text = text[1:].strip()

        if not text:
            return "", negated
        if text.startswith("#"):
            return "", negated
        return text, negated

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

        matched_scopes: list[tuple[int, str, list[str]]] = []
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

            depth = len(base_dir.parts)
            matched_scopes.append((depth, rel, entries))

        if not matched_scopes:
            return False

        matched_scopes.sort(key=lambda item: item[0])

        ignored = False
        for _, rel, entries in matched_scopes:
            rel_to_match = rel
            if treat_as_dir:
                rel_to_match = f"{rel}/.codex_ignore_probe"

            for raw_entry in entries:
                pattern, negated = self._parse_gitignore_entry(raw_entry)
                if not pattern:
                    continue
                if self._gitignore_pattern_matches_path(pattern, rel_to_match):
                    ignored = not negated

        return ignored

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
        section = self._new_section("12. .gitignore 覆盖检查")
        scopes = self._get_gitignore_scopes()
        if not scopes:
            section.add_warn("未检测到可读 .gitignore，无法进行覆盖检查（提醒项）", ".")
            self._record_failures()
            return

        all_entries: list[str] = []
        scoped_desc = ", ".join(self._rel(base / ".gitignore") for base, _ in scopes)
        for _, entries in scopes:
            for raw_entry in entries:
                pattern, negated = self._parse_gitignore_entry(raw_entry)
                if not pattern or negated:
                    continue
                all_entries.append(pattern)

        if not all_entries:
            section.add_warn(".gitignore 内容为空或不可读，无法覆盖必要规则（提醒项）", scoped_desc)
            self._record_failures()
            return

        if not self.languages:
            section.add_pass("未识别到语言标志文件，跳过语言规则覆盖检查", scoped_desc)
            self._record_failures()
            return

        section.add_pass(
            "识别到语言类型: " + ", ".join(sorted(self.languages)),
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
            section.add_pass(".gitignore 已覆盖当前语言和通用目录的常见产物规则", scoped_desc)
        else:
            dirty = self._ensure_dirty_findings_cached()
            dirty_dir_names = {p.name.lower() for p, _, _ in dirty if p.is_dir()}
            dirty_file_suffixes = {p.suffix.lower() for p, _, _ in dirty if not p.is_dir()}
            dirty_file_names = {p.name.lower() for p, _, _ in dirty if not p.is_dir()}
            for pattern in missing_patterns:
                has_real_dirty = False
                if pattern.endswith("/"):
                    if pattern.rstrip("/").lower() in dirty_dir_names:
                        has_real_dirty = True
                elif pattern.startswith("*."):
                    if pattern[1:].lower() in dirty_file_suffixes:
                        has_real_dirty = True
                else:
                    if pattern.lower() in dirty_file_names:
                        has_real_dirty = True
                if has_real_dirty:
                    section.add_fail(f".gitignore 未覆盖 {pattern}（且已检测到相关脏文件）", scoped_desc)
                else:
                    section.add_warn(f".gitignore 未覆盖 {pattern}", scoped_desc)

        self._record_failures()

    def _is_path_under_dir(self, path: Path, parent_dir: Path) -> bool:
        try:
            path.relative_to(parent_dir)
            return True
        except ValueError:
            return False

    def _is_vendor_static_asset_dir(self, vendor_dir: Path) -> bool:
        if self.root is None:
            return False
        try:
            parts = [part.lower() for part in vendor_dir.relative_to(self.root).parts]
        except ValueError:
            return False
        if not parts or parts[-1] != "vendor":
            return False
        return any(part in VENDOR_REFERENCE_CONTEXT_DIR_NAMES for part in parts[:-1])

    def _build_vendor_reference_tokens(self, vendor_dir: Path) -> list[str]:
        if self.root is None:
            return []
        try:
            rel_parts = [part.lower() for part in vendor_dir.relative_to(self.root).parts]
        except ValueError:
            rel_parts = [part.lower() for part in vendor_dir.parts]

        if not rel_parts:
            return []

        rel_posix = "/".join(rel_parts)
        tokens: set[str] = {
            f"{rel_posix}/",
            f"/{rel_posix}/",
        }

        context_idx: int | None = None
        for idx, part in enumerate(rel_parts[:-1]):
            if part in VENDOR_REFERENCE_CONTEXT_DIR_NAMES:
                context_idx = idx

        if context_idx is not None:
            suffix = "/".join(rel_parts[context_idx:])
            tokens.add(f"{suffix}/")
            tokens.add(f"/{suffix}/")

        return sorted(tokens, key=len)

    def _find_vendor_reference_evidence(self, vendor_dir: Path, max_hits: int = 5) -> list[str]:
        tokens = self._build_vendor_reference_tokens(vendor_dir)
        if not tokens:
            return []

        hits: list[str] = []
        for path, content in self._iter_readable_text_files():
            if self._is_path_under_dir(path, vendor_dir):
                continue
            lowered = content.lower()
            matched_token = next((token for token in tokens if token in lowered), None)
            if matched_token is None:
                continue

            line_no = 0
            for idx, line in enumerate(content.splitlines(), start=1):
                if matched_token in line.lower():
                    line_no = idx
                    break

            rel = self._rel(path)
            hits.append(f"{rel}:{line_no}" if line_no > 0 else rel)
            if len(hits) >= max_hits:
                break

        return hits

    def _collect_vendor_generated_traits(self, vendor_dir: Path, max_items: int = 6) -> list[str]:
        traits: list[str] = []
        parent = vendor_dir.parent

        for marker in sorted(VENDOR_DEPENDENCY_MARKER_FILENAMES, key=str.lower):
            if (parent / marker).is_file():
                traits.append(f"父目录含 {marker}")
                if len(traits) >= max_items:
                    return traits

        if (vendor_dir / "autoload.php").is_file():
            traits.append("含 autoload.php（Composer 依赖产物特征）")
        if (vendor_dir / "composer").is_dir():
            traits.append("含 composer/ 子目录（Composer 依赖产物特征）")
        if (vendor_dir / "bundle").is_dir():
            traits.append("含 bundle/ 子目录（Ruby 依赖产物特征）")
        if (vendor_dir / "cache").is_dir():
            traits.append("含 cache/ 子目录（依赖缓存特征）")

        if self.root is not None:
            try:
                rel_parts = [part.lower() for part in vendor_dir.relative_to(self.root).parts]
            except ValueError:
                rel_parts = []
            if rel_parts == ["vendor"]:
                traits.append("位于任务根目录 vendor/")
            elif len(rel_parts) >= 2 and rel_parts[0] == REPO_DIR_NAME and rel_parts[1] == "vendor":
                traits.append("位于 repo 根目录 vendor/")

        deduped: list[str] = []
        seen: set[str] = set()
        for item in traits:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
            if len(deduped) >= max_items:
                break
        return deduped

    def _collect_vendor_risky_content_evidence(self, vendor_dir: Path, max_items: int = 6) -> list[str]:
        risky_dir_names = {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".cache",
            ".opencode",
            ".codex",
            ".vscode",
            ".idea",
            "tmp",
            "temp",
            "uploads",
            "upload",
        }
        risky_file_suffixes = {".exe", ".dll", ".so", ".dylib", ".bin", ".msi", ".iso"}
        large_file_bytes = 50 * 1024 * 1024

        findings: list[str] = []
        for current_root, dirs, files in os.walk(vendor_dir, topdown=True):
            dirs.sort(key=str.lower)
            files.sort(key=str.lower)
            current_path = Path(current_root)

            for dirname in dirs:
                if dirname.lower() in risky_dir_names:
                    findings.append(f"包含可疑目录 {self._rel(current_path / dirname)}/")
                    if len(findings) >= max_items:
                        return findings

            for filename in files:
                file_path = current_path / filename
                if file_path.suffix.lower() in risky_file_suffixes:
                    findings.append(f"包含可疑二进制文件 {self._rel(file_path)}")
                    if len(findings) >= max_items:
                        return findings
                    continue
                try:
                    size = file_path.stat().st_size
                except OSError:
                    continue
                if size >= large_file_bytes:
                    findings.append(
                        f"包含大体积文件 {self._rel(file_path)}（{size / (1024 * 1024):.1f}MB）"
                    )
                    if len(findings) >= max_items:
                        return findings

        return findings

    def _summarize_vendor_evidence(self, items: list[str], max_items: int = 3) -> str:
        if not items:
            return ""
        if len(items) <= max_items:
            return "；".join(items)
        head = "；".join(items[:max_items])
        return f"{head}；... 共{len(items)}项"

    def _evaluate_vendor_directory(self, vendor_dir: Path) -> tuple[str, str]:
        static_asset_context = self._is_vendor_static_asset_dir(vendor_dir)
        reference_hits = self._find_vendor_reference_evidence(vendor_dir) if static_asset_context else []
        generated_traits = self._collect_vendor_generated_traits(vendor_dir)
        risky_findings = self._collect_vendor_risky_content_evidence(vendor_dir)

        if risky_findings:
            risky_desc = self._summarize_vendor_evidence(risky_findings)
            return (
                "FAIL",
                f"vendor 目录包含不合理内容（{risky_desc}），需清理后再交付",
            )

        if static_asset_context and reference_hits:
            ref_desc = self._summarize_vendor_evidence(reference_hits)
            return (
                "PASS",
                f"vendor 目录作为前端第三方静态资源被引用（证据: {ref_desc}）",
            )

        if generated_traits:
            traits_desc = self._summarize_vendor_evidence(generated_traits)
            return (
                "WARN",
                f"疑似依赖目录，需结合路径与引用确认（命中生成特征: {traits_desc}）",
            )

        if static_asset_context:
            return (
                "WARN",
                "vendor 位于 static/public/assets 下，但未检出源码引用证据；疑似依赖目录，需结合路径与引用确认",
            )

        return (
            "WARN",
            "检测到 vendor 目录；疑似依赖目录，需结合路径与引用确认",
        )

    def _dir_violation_reason(self, dirname: str) -> str | None:
        lower = dirname.lower()
        if re.fullmatch(r"build-.*", lower):
            return "为构建产物目录"
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

    def _ensure_dirty_findings_cached(self) -> list[tuple[Path, str, str]]:
        if self._dirty_findings_cache is not None:
            return self._dirty_findings_cache
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
                dir_path = current_path / dirname
                if self._is_ignored_by_any_gitignore(dir_path, treat_as_dir=True):
                    continue
                if dirname.lower() == "vendor":
                    status, message = self._evaluate_vendor_directory(dir_path)
                    violations.append((dir_path, message, status))
                    pruned_dirs.append(dirname)
                    continue
                reason = self._dir_violation_reason(dirname)
                if reason:
                    violations.append((dir_path, reason, "FAIL"))
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
                if reason:
                    if self._is_compile_exempt_file(file_path):
                        violations.append((file_path, reason, "WARN"))
                    else:
                        violations.append((file_path, reason, "FAIL"))
                elif file_path.suffix.lower() in DATABASE_FILE_SUFFIXES:
                    violations.append((file_path, "为数据库文件（建议排除）", "WARN"))

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
        return ordered

    def _check_local_dirty_files(self) -> None:
        section = self._new_section("13. 本地脏文件检查")
        ordered = self._ensure_dirty_findings_cached()

        if not ordered:
            section.add_pass("未检测到缓存/依赖/构建产物/数据库等本地脏文件", ".")
        else:
            for path, reason, status in ordered:
                rel_path = self._rel(path)
                if path.is_dir():
                    rel_path = rel_path + "/"
                if status == "PASS":
                    section.add_pass(reason, rel_path)
                elif status == "WARN":
                    section.add_warn(f"{rel_path} {reason}", rel_path)
                else:
                    section.add_fail(f"{rel_path} {reason}", rel_path)

        self._record_failures()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes} B"

    def _check_package_size(self) -> None:
        section = self._new_section("14. 包体积检查")
        assert self.root is not None

        total_size = 0
        large_files: list[tuple[Path, int]] = []

        for current_root, dirs, files in os.walk(self.root, topdown=True):
            dirs[:] = [
                d for d in sorted(dirs, key=str.lower)
                if d.lower() not in SIZE_SKIP_DIR_NAMES
            ]
            for filename in files:
                file_path = Path(current_root) / filename
                try:
                    file_size = file_path.stat().st_size
                except OSError:
                    continue
                total_size += file_size
                if file_size > SIZE_WARN_SINGLE_FILE_BYTES:
                    large_files.append((file_path, file_size))

        large_files.sort(key=lambda x: x[1], reverse=True)

        if large_files:
            for file_path, file_size in large_files:
                section.add_warn(
                    f"大文件: {self._rel(file_path)}（{self._format_size(file_size)}）",
                    self._rel(file_path),
                )

        if total_size > SIZE_WARN_TOTAL_BYTES:
            section.add_warn(
                f"包体积（不含 original_sessions/.git/.tmp/.backup）为 {self._format_size(total_size)}，超过 {self._format_size(SIZE_WARN_TOTAL_BYTES)} 阈值",
                ".",
            )

        if not large_files and total_size <= SIZE_WARN_TOTAL_BYTES:
            section.add_pass(
                f"包体积正常: {self._format_size(total_size)}（不含 original_sessions/.git/.tmp/.backup）",
                ".",
            )

        self._record_failures()

    def _write_report(self) -> None:
        if self.report_path is None:
            self.report_path = Path.cwd() / ".tmp" / "validation_report.md"

        self._record_failures()
        lines = ["# 静态质检报告", ""]

        for section in self.sections:
            lines.append(f"## {section.title}")
            if not section.items:
                lines.append("- [PASS] 无检查项（.）")
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
    parser = argparse.ArgumentParser(description="对项目交付目录进行静态合规检查")
    parser.add_argument("target", help="目录路径或目录名")
    parser.add_argument(
        "--convert-legacy",
        "--convert_legacy",
        action="store_true",
        help="将旧结构迁移到新结构（repo/docs/questions.md/original_sessions/metadata），执行前会进行终端确认并备份",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="输出报告后执行修复（转移/重命名/删除），执行前会进行终端确认，并在根目录 .backup 备份",
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
            print("CONVERT | 当前报告内容如下:")
            try:
                print(report.read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"CONVERT | 读取报告失败: {exc}")
        validator.run_convert_legacy()
        passed, errors, report = validator.run()
        status = "PASS" if passed else "FAIL"
        print(f"POST-CONVERT {status} | errors={errors} | report={report}")

    if args.repair:
        if report.is_file():
            print("REPAIR | 当前报告内容如下:")
            try:
                print(report.read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"REPAIR | 读取报告失败: {exc}")
        validator.run_repair()

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
