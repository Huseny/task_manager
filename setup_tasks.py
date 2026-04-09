#!/usr/bin/env python3
"""
Advanced task setup pipeline.

For each task in response_1775093873090.json:
1) Clone/pull repository (same behavior as before)
2) Download sessions
3) Auto-detect session type and convert:
   - Claude sessions -> merge_claude_subagents_trajectory.py
   - Other sessions  -> convert_ai_session.py
4) Compare generated trajectories against existing trajectories
5) Clean transient files from cloned repo
6) Produce a zip artifact for the cleaned repo
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import gdown
except ImportError:
    print("ERROR: gdown is not installed. Run: pip install gdown")
    sys.exit(1)


BASE_DIR = Path(__file__).parent
JSON_FILE = BASE_DIR / "response_1775093873090.json"
OUTPUT_ROOT = BASE_DIR / "tasks"
CONVERT_SCRIPT = BASE_DIR / "convert_ai_session.py"
MERGE_CLAUDE_SCRIPT = BASE_DIR / "merge_claude_subagents_trajectory.py"

URL_RE = re.compile(r"https?://[^\s,]+", re.IGNORECASE)

IGNORED_META_KEYS = {
    "meta",
    "_metadata",
    "token_counts",
    "session_meta",
    "merge_stats",
    "merge_warnings",
    "subagents",
    "skipped_events",
    "skipped_events_count",
}

TRANSIENT_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".idea",
    ".vscode",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    "tmp",
}

TRANSIENT_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    ".coverage",
    "coverage.xml",
)

ZIP_EXCLUDE_DIR_NAMES = TRANSIENT_DIR_NAMES | {".github"}
ZIP_EXCLUDE_FILE_NAMES = {".gitignore", ".gitattributes", ".gitmodules"}

DEVELOP_RE = re.compile(r"^develop-(\d+)\.json$", re.IGNORECASE)
BUGFIX_RE = re.compile(r"^bugfix-(\d+)\.json$", re.IGNORECASE)

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

ANTHROPIC_MODEL = "claude-opus-4-6"
OPENAI_MODEL = "gpt-5.4"
ANTHROPIC_MODEL_SESSIONS = "claude-opus-4"


def normalize_links(value) -> list[str]:
    """Extract clean URLs from list/string fields that may contain comma-separated URLs."""
    if not value:
        return []

    chunks = value if isinstance(value, list) else [value]
    urls: list[str] = []
    for chunk in chunks:
        if chunk is None:
            continue
        text = str(chunk)
        urls.extend(URL_RE.findall(text))

    # Keep order, remove duplicates
    seen = set()
    out = []
    for url in urls:
        cleaned = url.strip().rstrip(")]}.,;")
        if cleaned and cleaned not in seen:
            out.append(cleaned)
            seen.add(cleaned)
    return out


def extract_gdrive_id(url: str) -> tuple[str, str]:
    """
    Returns (gdrive_id, kind) where kind is 'file' or 'folder'.
    Supports:
      - /file/d/<ID>/view
      - /drive/folders/<ID>
      - open?id=<ID>
    """
    if not url:
        raise ValueError("Empty URL")

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    if "file" in parts and "d" in parts:
        idx = parts.index("d") + 1
        if idx < len(parts):
            return parts[idx], "file"

    if "folders" in parts:
        idx = parts.index("folders") + 1
        if idx < len(parts):
            return parts[idx], "folder"

    query = parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return query["id"][0], "file"

    raise ValueError(f"Unrecognised Google Drive URL: {url}")


def download_gdrive(url: str, dest_dir: Path) -> list[Path]:
    """Download a Google Drive file/folder into dest_dir and return downloaded file paths."""
    if not url:
        return []

    dest_dir.mkdir(parents=True, exist_ok=True)
    before = {p.resolve() for p in dest_dir.rglob("*") if p.is_file()}

    try:
        gid, kind = extract_gdrive_id(url)
    except ValueError as exc:
        print(f"    SKIP (cannot parse URL): {exc}")
        return []

    readable_url = (
        f"https://drive.google.com/file/d/{gid}/view"
        if kind == "file"
        else f"https://drive.google.com/drive/folders/{gid}"
    )
    print(f"    Downloading {kind}: {readable_url}")

    try:
        if kind == "file":
            output = gdown.download(
                id=gid, output=str(dest_dir) + "/", quiet=False, fuzzy=True
            )
            if output:
                return [Path(output)]
        else:
            gdown.download_folder(id=gid, output=str(dest_dir), quiet=False)
    except Exception as exc:
        print(f"    WARNING: download failed - {exc}")
        return []

    after = {p.resolve() for p in dest_dir.rglob("*") if p.is_file()}
    return sorted(after - before)


def clone_repo(github_url: str, dest_dir: Path) -> bool:
    """git clone github_url into dest_dir. Returns True on success."""
    if not github_url:
        return False
    print(f"    Cloning: {github_url}")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", github_url, str(dest_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    WARNING: git clone failed - {result.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: git is not installed or not in PATH")
        return False


def git_pull_repo(repo_dir: Path) -> bool:
    """git pull --ff-only in repo_dir. Returns True on success."""
    print(f"    Repo exists at {repo_dir.name}, pulling latest (git pull --ff-only)...")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            print(f"    WARNING: git pull failed - {stderr}")
            return False
        return True
    except FileNotFoundError:
        print("    ERROR: git is not installed or not in PATH")
        return False


def write_info_txt(task: dict, dest_dir: Path) -> None:
    task_id = task.get("task_id", task.get("id", "unknown"))
    txt_path = dest_dir / f"{task_id}_info.txt"
    tools = task.get("development_tools") or []
    tools_str = ", ".join(tools) if isinstance(tools, list) else str(tools)

    session_ids = task.get("session_ids") or []
    session_ids_str = (
        "\n".join(str(s) for s in session_ids)
        if isinstance(session_ids, list)
        else str(session_ids)
    )

    content = (
        f"=== PROMPT TEXT ===\n{task.get('prompt_text', '')}\n\n"
        f"=== TASK ID ===\n{task_id}\n\n"
        f"=== SESSION IDS ===\n{session_ids_str}\n\n"
        f"=== DEVELOPMENT TOOLS ===\n{tools_str}\n"
    )
    txt_path.write_text(content, encoding="utf-8")
    print(f"    Written: {txt_path.name}")


def detect_session_format(session_file: Path) -> str:
    """Use converter-native detection; fallback to extension-based inference."""
    try:
        from convert_ai_session import detect_format

        return detect_format(session_file)
    except Exception:
        suffix = session_file.suffix.lower()
        return "claude_jsonl" if suffix == ".jsonl" else "unknown"


def run_convert_ai_session(input_file: Path, output_file: Path) -> tuple[bool, str]:
    cmd = [
        sys.executable,
        str(CONVERT_SCRIPT),
        "-i",
        str(input_file),
        "-o",
        str(output_file),
        "--format",
        "auto",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, (
            result.stderr.strip() or result.stdout.strip() or "converter failed"
        )
    return True, ""


def run_merge_claude(session_file: Path, output_file: Path) -> tuple[bool, str]:
    tmp_dir = output_file.parent / f"_merge_tmp_{session_file.stem}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(MERGE_CLAUDE_SCRIPT),
        "-r",
        str(session_file.parent),
        "-s",
        session_file.stem,
        "-o",
        str(tmp_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, (
            result.stderr.strip() or result.stdout.strip() or "claude merge failed"
        )

    merged = tmp_dir / "trajectory.json"
    if not merged.exists():
        return False, "merge output trajectory.json not found"

    shutil.copy2(merged, output_file)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return True, ""


def collect_session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    candidates = []
    for p in sessions_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".json", ".jsonl"}:
            continue
        if p.name.endswith("_converted.json"):
            continue
        candidates.append(p)
    return sorted(candidates)


def convert_sessions(sessions_dir: Path, generated_dir: Path) -> list[dict]:
    generated_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    for session_file in collect_session_files(sessions_dir):
        detected = detect_session_format(session_file)
        is_claude = detected == "claude_jsonl"
        output_name = (
            f"{session_file.stem}_{'trajectory' if is_claude else 'converted'}.json"
        )
        output_file = generated_dir / output_name

        print(f"    Converting {session_file.name} [{detected}] ...")
        if is_claude:
            ok, err = run_merge_claude(session_file, output_file)
            method = "merge_claude_subagents_trajectory.py"
        else:
            ok, err = run_convert_ai_session(session_file, output_file)
            method = "convert_ai_session.py"

        item = {
            "input": str(session_file),
            "detected_format": detected,
            "method": method,
            "output": str(output_file) if ok else None,
            "ok": ok,
            "error": err if not ok else None,
        }
        if ok:
            print(f"      OK -> {output_file.name}")
        else:
            print(f"      FAIL -> {err}")
        results.append(item)

    return results


def load_json_file(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def read_json_with_fallback(path: Path):
    with path.open("rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return json.loads(raw.decode("utf-16"))
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("latin-1"))


def strip_meta(obj):
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            if key in IGNORED_META_KEYS:
                continue
            cleaned[key] = strip_meta(value)
        return cleaned
    if isinstance(obj, list):
        return [strip_meta(item) for item in obj]
    return obj


def canonical_hash(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_anthropic_context(model_value: str | None, provider_value: str | None) -> bool:
    model = (model_value or "").strip().lower()
    provider = (provider_value or "").strip().lower()
    return "anthropic" in provider or "claude" in model or "anthropic" in model


def rewrite_models_in_json(data):
    """Recursively rewrites model/provider references in session-like JSON objects."""
    changed = 0

    if isinstance(data, dict):
        model_fields = [
            k
            for k in data.keys()
            if k.lower() in MODEL_KEYS and isinstance(data[k], str)
        ]
        provider_fields = [
            k
            for k in data.keys()
            if k.lower() in PROVIDER_KEYS and isinstance(data[k], str)
        ]

        current_model = next((str(data[k]) for k in model_fields), None)
        current_provider = next((str(data[k]) for k in provider_fields), None)
        anthropic = is_anthropic_context(current_model, current_provider)

        target_model = ANTHROPIC_MODEL if anthropic else OPENAI_MODEL
        target_provider = "anthropic" if anthropic else "openai"

        for key in model_fields:
            if data[key] != target_model:
                data[key] = target_model
                changed += 1

        for key in provider_fields:
            if data[key] != target_provider:
                data[key] = target_provider
                changed += 1

        for key, value in data.items():
            new_value, nested_changed = rewrite_models_in_json(value)
            data[key] = new_value
            changed += nested_changed
        return data, changed

    if isinstance(data, list):
        for idx, item in enumerate(data):
            new_item, nested_changed = rewrite_models_in_json(item)
            data[idx] = new_item
            changed += nested_changed
        return data, changed

    return data, 0


def normalize_provider(value: str | None) -> str | None:
    if not value:
        return None
    provider = str(value).strip().lower()
    if "openai" in provider:
        return "openai"
    if "anthropic" in provider:
        return "anthropic"
    return None


def rewrite_models_by_provider(data, inherited_provider: str | None = None):
    """Recursively rewrite model fields from explicit provider context.

    Rules:
      - provider openai    -> model gpt-5.4
      - provider anthropic -> model claude-opus-4
    """
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

        effective_provider = local_provider or inherited_provider
        if effective_provider == "openai":
            target_model = OPENAI_MODEL
        elif effective_provider == "anthropic":
            target_model = ANTHROPIC_MODEL_SESSIONS
        else:
            target_model = None

        if target_model:
            for key in model_fields:
                if data[key] != target_model:
                    data[key] = target_model
                    changed += 1

        for key, value in data.items():
            new_value, nested_changed = rewrite_models_by_provider(
                value, effective_provider
            )
            data[key] = new_value
            changed += nested_changed
        return data, changed

    if isinstance(data, list):
        for idx, item in enumerate(data):
            new_item, nested_changed = rewrite_models_by_provider(
                item, inherited_provider
            )
            data[idx] = new_item
            changed += nested_changed
        return data, changed

    return data, 0


def normalize_downloaded_sessions_models(sessions_dir: Path) -> dict:
    """Normalize models inside downloaded sessions JSON/JSONL using provider rules."""
    report = {
        "sessions_dir": None,
        "processed_files": 0,
        "updated_fields": 0,
        "files": [],
    }

    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return report

    report["sessions_dir"] = str(sessions_dir)
    candidates = [
        p
        for p in sorted(sessions_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in {".json", ".jsonl"}
    ]

    for file_path in candidates:
        file_updates = 0
        error = None
        try:
            if file_path.suffix.lower() == ".json":
                payload = read_json_with_fallback(file_path)
                updated, changed = rewrite_models_by_provider(payload)
                file_updates = changed
                if changed > 0:
                    file_path.write_text(
                        json.dumps(updated, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            else:
                raw_text = file_path.read_text(encoding="utf-8", errors="replace")
                lines = raw_text.splitlines()
                out_lines = []
                changed_lines = 0
                for line in lines:
                    if not line.strip():
                        out_lines.append(line)
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        out_lines.append(line)
                        continue
                    updated_item, changed = rewrite_models_by_provider(item)
                    file_updates += changed
                    if changed > 0:
                        changed_lines += 1
                    out_lines.append(json.dumps(updated_item, ensure_ascii=False))

                if changed_lines > 0:
                    file_path.write_text(
                        "\n".join(out_lines)
                        + ("\n" if raw_text.endswith("\n") else ""),
                        encoding="utf-8",
                    )

        except Exception as exc:
            error = str(exc)

        report["processed_files"] += 1
        report["updated_fields"] += file_updates
        report["files"].append(
            {
                "file": str(file_path),
                "updated_fields": file_updates,
                "error": error,
            }
        )

    return report


def normalize_trajectory_filenames_in_sessions(sessions_dir: Path) -> dict:
    """Rename bugfix-N.json files to develop-N+1.json based on max existing develop index."""
    stats = {"renamed": 0, "renames": []}
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return stats

    session_jsons = [p for p in sessions_dir.glob("*.json") if p.is_file()]
    max_develop_idx = 0
    bugfix_files = []

    for path in session_jsons:
        develop_match = DEVELOP_RE.match(path.name)
        if develop_match:
            max_develop_idx = max(max_develop_idx, int(develop_match.group(1)))
            continue

        bugfix_match = BUGFIX_RE.match(path.name)
        if bugfix_match:
            bugfix_files.append(path)

    def bugfix_index(path: Path) -> int:
        match = BUGFIX_RE.match(path.name)
        return int(match.group(1)) if match else 0

    for old_path in sorted(bugfix_files, key=bugfix_index):
        max_develop_idx += 1
        new_path = sessions_dir / f"develop-{max_develop_idx}.json"
        while new_path.exists():
            max_develop_idx += 1
            new_path = sessions_dir / f"develop-{max_develop_idx}.json"
        old_name = old_path.name
        old_path.rename(new_path)
        stats["renamed"] += 1
        stats["renames"].append({"from": old_name, "to": new_path.name})

    return stats


def normalize_repo_sessions(repo_dir: Path) -> dict:
    """Normalize repo sessions before zipping: rename bugfix files and rewrite models/providers."""
    report = {
        "sessions_dir": None,
        "renamed_bugfix_files": 0,
        "renames": [],
        "model_updates": [],
    }

    sessions_dir = repo_dir / "sessions"
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return report

    report["sessions_dir"] = str(sessions_dir)

    rename_stats = normalize_trajectory_filenames_in_sessions(sessions_dir)
    report["renamed_bugfix_files"] = rename_stats["renamed"]
    report["renames"] = rename_stats["renames"]

    for session_file in sorted(sessions_dir.glob("*.json")):
        try:
            payload = read_json_with_fallback(session_file)
        except Exception as exc:
            report["model_updates"].append(
                {
                    "file": session_file.name,
                    "updated_fields": 0,
                    "error": f"failed to parse JSON: {exc}",
                }
            )
            continue

        updated, changed = rewrite_models_in_json(payload)
        if changed > 0:
            session_file.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        report["model_updates"].append(
            {
                "file": session_file.name,
                "updated_fields": changed,
                "target_anthropic": ANTHROPIC_MODEL,
                "target_non_anthropic": OPENAI_MODEL,
            }
        )

    return report


def collect_existing_trajectories_from_repo_sessions(
    repo_dir: Path, existing_dir: Path
) -> dict:
    """Copy existing trajectories from repo root sessions folder into trajectories/existing."""
    report = {
        "source": None,
        "copied": 0,
        "files": [],
    }
    sessions_dir = repo_dir / "sessions"
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return report

    report["source"] = str(sessions_dir)
    existing_dir.mkdir(parents=True, exist_ok=True)

    for src in sorted(sessions_dir.glob("*.json")):
        if not (DEVELOP_RE.match(src.name) or BUGFIX_RE.match(src.name)):
            continue
        dst = existing_dir / src.name
        # Avoid overwrite collisions while preserving all references.
        if dst.exists():
            stem = src.stem
            suffix = src.suffix
            idx = 1
            while True:
                candidate = existing_dir / f"{stem}-repo-{idx}{suffix}"
                if not candidate.exists():
                    dst = candidate
                    break
                idx += 1
        shutil.copy2(src, dst)
        report["copied"] += 1
        report["files"].append(dst.name)

    return report


def compare_trajectories(generated_dir: Path, existing_dir: Path) -> list[dict]:
    generated_files = (
        sorted(generated_dir.glob("*.json")) if generated_dir.exists() else []
    )
    existing_files = (
        sorted(existing_dir.rglob("*.json")) if existing_dir.exists() else []
    )
    report = []

    existing_cache = []
    for existing in existing_files:
        obj = load_json_file(existing)
        if obj is None:
            continue
        existing_cache.append(
            {
                "path": existing,
                "exact": canonical_hash(obj),
                "normalized": canonical_hash(strip_meta(obj)),
            }
        )

    for generated in generated_files:
        obj = load_json_file(generated)
        if obj is None:
            report.append(
                {
                    "generated": str(generated),
                    "match": "invalid_generated_json",
                    "matched_with": None,
                }
            )
            continue

        g_exact = canonical_hash(obj)
        g_norm = canonical_hash(strip_meta(obj))
        matched_with = None
        match_type = "no_match"

        for candidate in existing_cache:
            if g_exact == candidate["exact"]:
                matched_with = str(candidate["path"])
                match_type = "exact_match"
                break

        if match_type == "no_match":
            for candidate in existing_cache:
                if g_norm == candidate["normalized"]:
                    matched_with = str(candidate["path"])
                    match_type = "normalized_match"
                    break

        report.append(
            {
                "generated": str(generated),
                "match": match_type,
                "matched_with": matched_with,
            }
        )

    return report


def cleanup_repo(repo_dir: Path) -> dict:
    removed_dirs = 0
    removed_files = 0

    if not repo_dir.exists() or not repo_dir.is_dir():
        return {"removed_dirs": 0, "removed_files": 0}

    for path in sorted(repo_dir.rglob("*"), reverse=True):
        if not path.exists():
            continue
        if path.is_dir() and path.name in TRANSIENT_DIR_NAMES:
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1
            continue

        if path.is_file():
            for pattern in TRANSIENT_FILE_PATTERNS:
                if path.match(pattern):
                    try:
                        path.unlink()
                        removed_files += 1
                    except OSError:
                        pass
                    break

    return {"removed_dirs": removed_dirs, "removed_files": removed_files}


def extract_task_number(task_id: str) -> str | None:
    if not task_id:
        return None
    match = re.search(r"wMt(\d+)$", task_id)
    if match:
        return match.group(1)
    trailing = re.search(r"(\d+)$", task_id)
    return trailing.group(1) if trailing else None


def create_repo_zip(repo_dir: Path, task_dir: Path, task_id: str) -> Path | None:
    if not repo_dir.exists() or not repo_dir.is_dir():
        return None

    task_num = extract_task_number(task_id) or "UNKNOWN"
    zip_base = task_dir / f"TASK-{task_num}"
    zip_path = zip_base.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()

    staging_root = task_dir / "_zip_staging"
    staging_repo = staging_root / repo_dir.name
    if staging_root.exists():
        shutil.rmtree(staging_root, ignore_errors=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        repo_dir,
        staging_repo,
        ignore=shutil.ignore_patterns(
            *ZIP_EXCLUDE_DIR_NAMES,
            *TRANSIENT_FILE_PATTERNS,
            *ZIP_EXCLUDE_FILE_NAMES,
        ),
    )
    archive = shutil.make_archive(
        str(zip_base), "zip", root_dir=str(staging_root), base_dir=repo_dir.name
    )
    shutil.rmtree(staging_root, ignore_errors=True)
    return Path(archive)


def process_task(task: dict, skip_clone_if_repo_exists: bool) -> dict:
    task_id = task.get("task_id") or task.get("id") or "unknown"
    print(f"\n{'='*72}")
    print(f"Processing task: {task_id}")
    print(f"{'='*72}")

    task_dir = OUTPUT_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "task_id": task_id,
        "repo": None,
        "downloads": {"sessions": 0},
        "downloaded_sessions_model_normalization": None,
        "conversion": [],
        "comparison": [],
        "session_normalization": None,
        "cleanup": None,
        "zip": None,
    }

    # 1) Clone/pull repo (preserve previous behavior)
    github_link = task.get("github_link")
    repo_dest = None
    if github_link:
        repo_name = github_link.rstrip("/").split("/")[-1]
        repo_dest = task_dir / repo_name
        summary["repo"] = str(repo_dest)
        if repo_dest.exists():
            if skip_clone_if_repo_exists:
                print(
                    f"    Repo already present at {repo_dest.name}, skipping clone/pull (--skip)."
                )
            else:
                git_pull_repo(repo_dest)
        else:
            clone_repo(github_link, repo_dest)
    else:
        print("    No github_link - skipping clone.")

    # 2) Download session files
    print("  [sessions]")
    sessions_dir = task_dir / "sessions"
    session_urls = normalize_links(task.get("session_files_links"))
    if session_urls:
        for url in session_urls:
            downloaded = download_gdrive(url, sessions_dir)
            summary["downloads"]["sessions"] += len(downloaded)
    else:
        print("    session_files_links is empty - skipping.")

    # 3) Normalize downloaded sessions JSON/JSONL model values by provider
    print("  [normalize downloaded sessions models]")
    downloaded_session_norm = normalize_downloaded_sessions_models(sessions_dir)
    summary["downloaded_sessions_model_normalization"] = downloaded_session_norm
    if downloaded_session_norm["sessions_dir"]:
        print(
            "    Processed "
            f"{downloaded_session_norm['processed_files']} file(s), "
            f"updated {downloaded_session_norm['updated_fields']} model field(s)"
        )
    else:
        print("    sessions directory not found - skipping.")

    # 4) Download existing trajectories for matching
    print("  [existing trajectories]")
    existing_traj_dir = task_dir / "trajectories" / "existing"
    if repo_dest and repo_dest.exists():
        existing_from_repo = collect_existing_trajectories_from_repo_sessions(
            repo_dest, existing_traj_dir
        )
        summary["downloads"]["existing_trajectories"] = existing_from_repo["copied"]
        if existing_from_repo["source"]:
            print(
                "    Copied "
                f"{existing_from_repo['copied']} existing trajectory file(s) "
                f"from repo sessions"
            )
        else:
            print(
                "    No repo/sessions folder found - skipping existing trajectory extraction."
            )
    else:
        print("    Repo unavailable - skipping existing trajectory extraction.")

    # 5) Convert sessions to trajectories
    print("  [convert sessions]")
    generated_dir = task_dir / "trajectories" / "generated"
    conversion = convert_sessions(sessions_dir, generated_dir)
    summary["conversion"] = conversion

    # 6) Compare converted trajectories against existing ones
    print("  [compare trajectories]")
    comparison = compare_trajectories(generated_dir, existing_traj_dir)
    summary["comparison"] = comparison
    for result in comparison:
        print(f"    {Path(result['generated']).name}: {result['match']}")

    # 7) Clean transient files and package repo zip
    if repo_dest and repo_dest.exists():
        print("  [normalize repo sessions]")
        normalization = normalize_repo_sessions(repo_dest)
        summary["session_normalization"] = normalization
        if normalization["sessions_dir"]:
            print(
                "    Sessions normalized: "
                f"renamed {normalization['renamed_bugfix_files']} bugfix file(s), "
                f"processed {len(normalization['model_updates'])} session file(s)"
            )
        else:
            print("    No repo/sessions directory found - skipping.")

        print("  [cleanup repo]")
        cleanup_stats = cleanup_repo(repo_dest)
        summary["cleanup"] = cleanup_stats
        print(
            "    Removed "
            f"{cleanup_stats['removed_dirs']} transient directorie(s), "
            f"{cleanup_stats['removed_files']} transient file(s)"
        )

        print("  [zip repo]")
        zip_path = create_repo_zip(repo_dest, task_dir, str(task_id))
        summary["zip"] = str(zip_path) if zip_path else None
        if zip_path:
            print(f"    Created zip: {zip_path.name}")

    # 8) Write task metadata and report
    print("  [info txt]")
    write_info_txt(task, task_dir)

    report_path = task_dir / "processing_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"    Written: {report_path.name}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare tasks: clone/download, convert sessions, compare trajectories, clean repos, and zip artifacts."
    )
    parser.add_argument(
        "--skip",
        action="store_true",
        help="If the clone target folder already exists, skip git clone and git pull.",
    )
    args = parser.parse_args()

    if not JSON_FILE.exists():
        print(f"ERROR: {JSON_FILE} not found.")
        sys.exit(1)

    with JSON_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    tasks = data.get("items", [])
    if not tasks:
        print("No items found in JSON.")
        sys.exit(0)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(tasks)} task(s). Output root: {OUTPUT_ROOT}")
    if args.skip:
        print("Mode: --skip (existing repo folders: no clone, no pull)")

    all_summaries = []
    for task in tasks:
        all_summaries.append(process_task(task, skip_clone_if_repo_exists=args.skip))

    global_report = OUTPUT_ROOT / "processing_summary.json"
    global_report.write_text(
        json.dumps(all_summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nDone. All tasks processed under: {OUTPUT_ROOT}")
    print(f"Summary report: {global_report}")


if __name__ == "__main__":
    main()
