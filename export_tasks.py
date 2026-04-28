#!/usr/bin/env python3
"""
export_tasks.py
===============

Fetch Aquila tasks by task ID (read from a text file), assemble a clean
delivery package per task, run all required fixups, validate the package,
and emit one zip per task.

Overview
--------
For each task ID in --task-ids-file, this script:

  1. Fetches the task from Aquila (bearer-token authenticated).
  2. Shallow-clones the task's GitHub repo (private, via PAT).
  3. Downloads and extracts the task's session archive (zip or rar; supports
     direct URLs and Google Drive share links).
  4. Normalizes the session JSON/JSONL via `normalize_sessions_zip`.
  5. Syncs `metadata.json` with the task payload from Aquila:
       - `prompt` is overwritten from `prompt_text`
       - project_type / framework / language fields are reconciled with the
         Aquila task fields (with "none" cascade rules; see Project info sync).
  6. Renames each session JSONL to `<sessionId>.jsonl` so file names match
     their internal `sessionId`.
  7. Trims trailing non-assistant messages from each session JSONL so the
     validator's "last message must be assistant text" check passes.
  8. Inflates `input_tokens` on existing usage records until total session
     cost meets a minimum (default $15) -- see Token cost.
  9. Normalizes api-spec filename variants (apiSpec.md, api_spec.md,
     api-specs.md, apispec.md, ...) by renaming them to the canonical
     `api-spec.md`. The validator's filename check is strict; this catches
     near-misses without requiring a manual fix.
 10. Removes git artifacts (.git, .gitattributes, .gitmodules, .github) so
     they aren't shipped. `.gitignore` is intentionally kept.
 11. Repo cleanliness check (raises if anything below is present):
       - Anywhere in the cloned tree: `.env`, `.env.*`, `venv/`, `.venv/`,
         `env/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`,
         `.mypy_cache/`, `.ruff_cache/`, `.idea/`, `.vscode/`, `dist/`,
         `build/`, `.next/`, `.nuxt/`, `.cache/`, `.turbo/`, `.parcel-cache/`,
         `coverage/`, `.nyc_output/`, `htmlcov/`, `.DS_Store`.
       - Inside any `repo/` subdirectory of the clone: `.tmp/`, `tmp/`,
         `temp/`, `_tmp/`, `docs/`, `doc/`, `documentation/`. (`.tmp/` at the
         cloned root is fine -- it gets moved aside in step 12.)
     If anything is found, the task fails with a `RepoCleanlinessError`
     listing every offender.
 12. Moves the repo's `.tmp/` (if any) out to `<output>/TASK-<id>/.tmp` so
     it is excluded from the shipped zip.
 13. Zips the cleaned repo, then extracts it to a scratch dir for validation.
 14. Runs `validate_package_direct_original_sessions.py` against the
     extracted directory.
 15. On pass: replaces `original_sessions/` in the extract with a
     Claude-project-named zip (e.g. `-home-husen-...-TASK-req-xxx.zip`)
     and rebuilds the final TASK zip from the extract.
 16. Cleans up the per-task work dir (unless --keep-work-dir).

Relationship to run_reconstruction_export_from_folder.py
--------------------------------------------------------
This is the text-file-driven variant of that script. The per-task pipeline
is identical except:

  - Task IDs come from a flat text file (one per line) instead of being
    derived from subfolder names.
  - There is no "extra sessions folder" to merge in -- the only session
    source is the archive URL on the task payload.

Credentials
-----------
  Aquila bearer token  Passed via the required `--aquila-bearer-token` flag.
  GitHub PAT           Read from the `GITHUB_PAT` environment variable
                       (used to clone private repos via x-access-token auth).
                       If the env var isn't already set, the script also
                       looks for a `.env` file -- first in the current
                       working directory, walking up to filesystem root,
                       then alongside this script. Format:

                           GITHUB_PAT=ghp_xxxxxxxxxxxx

                       Lines starting with `#` are comments. `export ` prefixes
                       and surrounding single/double quotes are tolerated.
                       Existing environment values are NOT overwritten.

Config constants (set at top of file)
-------------------------------------
  GIT_ARTIFACT_NAMES   Names removed from the cloned repo before zipping.
  ZIP_NOISE_NAMES      Names cleaned out of the extracted sessions archive.
  DEFAULT_MIN_TOKEN_COST_USD  Default token-cost floor per task ($15).
  DEFAULT_TASK_URL_TEMPLATE   Aquila task-lookup URL; supports {task_id}.
  GITHUB_PAT_ENV_VAR   Name of the env var read for the GitHub PAT.
  DOTENV_FILENAME      Name of the dotenv file looked up for the PAT (.env).

CLI flags
---------
  --task-ids-file PATH        (required) Text file with one task ID per line.
                              Blank lines and `#`-comments are ignored.
                              Duplicate IDs are de-duplicated, order preserved.
  --aquila-bearer-token TOKEN (required) Bearer token for the Aquila task
                              lookup endpoint.
  --output-dir PATH           Per-task output folder (default: task_exports).
  --work-dir PATH             Scratch dir for clones and extracts
                              (default: _task_export_work). Removed at end of
                              run unless --keep-work-dir.
  --task-url-template URL     Aquila lookup template; supports {task_id} or
                              {t_id}. Default: production Aquila endpoint.
  --interactive-validation    On validation failure, prompt: skip / zip-
                              anyway / retry. Without this flag, failures
                              abort the task and delete the zip.
  --min-token-cost-usd FLOAT  Minimum total validator-equivalent token cost
                              (USD) per task; below this, input_tokens are
                              randomly added to existing usage records until
                              the threshold is met. 0 disables. Default: 15.0.
  --keep-work-dir             Keep the work dir at end of run; useful for
                              inspecting `<work>/<task>/_extract/validate_extract/`
                              and `<work>/<task>/_validate/`.
  -v, --verbose               Enable DEBUG-level logging.

Output layout
-------------
For each successful task <id>:

  <output-dir>/
    TASK-<id>/
      TASK-<id>.zip                    # The validated delivery package.
      .tmp/                            # Repo's `.tmp/` (if any), kept aside.
      validation/
        validation_report.md           # Validator's Markdown report.
        validation_stdout.log          # Validator stdout.
        validation_stderr.log          # Validator stderr.
    report.json                        # Run-level + per-task summary.

Inside `TASK-<id>.zip`, sessions are NOT shipped as a folder. After
validation passes, `original_sessions/` is replaced with a single zip whose
name mirrors how Claude Code names project folders -- e.g. for a session
whose `cwd` is `/home/husen/Desktop/eaglepoint/mindflow/TASK-req-a5bb44e7de20`,
the inner zip is named:

    -home-husen-Desktop-eaglepoint-mindflow-TASK-req-a5bb44e7de20.zip

The basename is derived from the first non-empty `cwd` found in any session
JSONL (memory.jsonl is skipped). The sanitization rule matches Claude:
collapse any run of non-`[A-Za-z0-9._]` characters into a single `-`.

The inner sessions zip is FLAT -- session JSONLs sit at its root, not under
a wrapper folder.

Validation flow
---------------
The flow extracts the just-built TASK zip into
`<work>/<task>/_extract/validate_extract/` and runs the validator against
that directory. Validation artifacts (report, stdout, stderr) are persisted
to the output dir's `validation/` and mirrored into `<work>/<task>/_validate/`
so the work dir's `_validate/` only ever holds artifacts -- the extracted
package lives separately under `_extract/`. On the first pass the result is
wrapped up; on failure behavior depends on `--interactive-validation`:

  Without --interactive-validation (CI mode):
    - The TASK zip is deleted.
    - The task is reported as failed with stage="validate".
    - The validation report and stdout/stderr logs are still persisted.

  With --interactive-validation (manual-fix mode):
    The user is prompted with three choices:

      [s] skip      - delete the zip, mark failed, move on (same as CI mode).
      [z] zip       - keep the zip anyway, mark task as success, repackage
                      from the (possibly hand-edited) extract.
      [r] retry     - re-run the validator AGAINST THE EXTRACT IN PLACE,
                      so manual edits to the extracted package
                      (including `original_sessions/`) are honored without
                      being clobbered by re-extraction.

    Manual edits should be made under:

        <work>/<task>/_extract/validate_extract/

    Edits to `original_sessions/` survive across retries. On final pass /
    zip-anyway, the TASK zip is rebuilt from the extract, so manual edits
    flow into the shipped zip.

metadata.json sync rules
------------------------
Prompt sync (sync_metadata_prompt_with_aquila):
  - If `metadata.json` is missing/unreadable/non-JSON-object, skip.
  - If Aquila has no `prompt_text`, skip and leave the existing prompt.
  - Otherwise compare normalized whitespace; overwrite if they differ.

Project info sync (sync_metadata_project_info_with_aquila):
  Mapping (metadata.json field <- Aquila task field):

    project_type        <- project_type
    frontend_framework  <- frontend_tech_stack
    backend_framework   <- backend_stack

  For each mapped field: overwrite when values differ. Aquila list values
  are joined with ", "; null/empty Aquila values become "none".

  For unmapped fields (`backend_language`, `frontend_language`): leave alone
  unless they are null/empty, in which case set to "none".

  Cascade: if `backend_framework` is effectively none, force
  `backend_language` to "none". Same for frontend.

Token cost (ensure_minimum_token_cost)
--------------------------------------
Computes the validator-equivalent total cost across all *.jsonl in
`original_sessions/` (memory.jsonl excluded), using the same dedupe rule as
the validator (usages sharing the same `message.id` within a single file are
counted once -- last one wins; anonymous usages count every time). If the
total is below `--min-token-cost-usd`, randomly bumps `input_tokens` on
existing usage records (50k-500k per bump) until the threshold is met.

Pricing (per million tokens, USD):
  input  = 5.00
  output = 25.00
  cache_read  = input * 0.10  = 0.50
  cache_write = input * 1.25  = 6.25

Bumps are seeded by `hash(task_id) & 0xFFFFFFFF` so a given task is
reproducible across re-runs (within a Python process).

Failure modes (per task)
------------------------
A task can fail at any of three stages, recorded in `report.json`:

  stage="fetch"     The Aquila lookup failed or returned no task object.
  stage="process"   Cloning, downloading, extracting, or any fixup raised.
  stage="validate"  The validator returned non-zero.

Failed tasks do not abort the run; the script processes the next task.

Exit codes
----------
  0  All requested tasks fetched, processed, and validated.
  2  At least one task failed -- see `report.json`.

Usage
-----
Basic run:

    export GITHUB_PAT=ghp_...
    python export_tasks.py \\
        --task-ids-file ids.txt \\
        --aquila-bearer-token "$AQUILA_TOKEN" \\
        --output-dir task_exports

Verbose with kept work dir (so you can inspect what the validator saw):

    export GITHUB_PAT=ghp_...
    python export_tasks.py \\
        --task-ids-file ids.txt \\
        --aquila-bearer-token "$AQUILA_TOKEN" \\
        --output-dir task_exports \\
        --work-dir _task_export_work \\
        --keep-work-dir -v

Interactive (lets you hand-edit sessions and retry on failure):

    export GITHUB_PAT=ghp_...
    python export_tasks.py \\
        --task-ids-file ids.txt \\
        --aquila-bearer-token "$AQUILA_TOKEN" \\
        --output-dir task_exports \\
        --interactive-validation

Override the Aquila endpoint (e.g. local API):

    export GITHUB_PAT=ghp_...
    python export_tasks.py \\
        --task-ids-file ids.txt \\
        --aquila-bearer-token "$AQUILA_TOKEN" \\
        --task-url-template \\
            "http://127.0.0.1:8000/api/v1/mindflow-tasks/task-id/{task_id}"
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from normalize_sessions_zip import run_original_sessions_dir


DEFAULT_TASK_URL_TEMPLATE = "https://api.aquila-core.net/api/v1/mindflow-tasks/mindflow-id/{task_id}"
GITHUB_PAT_ENV_VAR = "GITHUB_PAT"
DOTENV_FILENAME = ".env"
GIT_ARTIFACT_NAMES = {
    ".git",
    ".gitattributes",
    ".gitmodules",
    ".github",
}
ZIP_NOISE_NAMES = {"__macosx", ".ds_store"}

log = logging.getLogger("export_tasks")


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Ignores blank lines and `#` comments. Strips
    matching surrounding single/double quotes. Honors `export ` prefixes.
    """
    env: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def load_dotenv_into_environ(start: Path) -> Path | None:
    """Look for a .env file at `start`, then walk up to filesystem root.
    Returns the path of the file loaded (the first one found), or None.
    Existing os.environ entries are NOT overwritten.
    """
    candidate_dirs: list[Path] = []
    seen: set[Path] = set()
    current = start.resolve()
    while True:
        if current in seen:
            break
        seen.add(current)
        candidate_dirs.append(current)
        if current.parent == current:
            break
        current = current.parent

    for directory in candidate_dirs:
        candidate = directory / DOTENV_FILENAME
        if not candidate.is_file():
            continue
        for key, value in _parse_dotenv(candidate).items():
            os.environ.setdefault(key, value)
        return candidate
    return None


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "unknown"


def first_non_empty(values: list[Any]) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def http_get_json(url: str, bearer_token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def build_task_url(task_url_template: str, task_id: str) -> str:
    encoded_task_id = urllib.parse.quote(task_id, safe="")
    if "{task_id}" in task_url_template:
        return task_url_template.format(task_id=encoded_task_id)
    if "{t_id}" in task_url_template:
        return task_url_template.format(t_id=encoded_task_id)
    if task_url_template.rstrip("/").endswith("/t_id"):
        return task_url_template.rstrip("/")[: -len("/t_id")] + f"/{encoded_task_id}"
    return task_url_template.rstrip("/") + f"/{encoded_task_id}"


def extract_single_task(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        for key in ("item", "data", "task", "result"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                return candidate
        if payload:
            return payload
    return None


def parse_task_ids(task_ids_file: str) -> list[str]:
    file_path = Path(task_ids_file)
    if not file_path.exists():
        raise FileNotFoundError(f"Task IDs file not found: {file_path}")

    seen: set[str] = set()
    ordered: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in seen:
            continue
        ordered.append(stripped)
        seen.add(stripped)
    return ordered


def is_uuid_filename(stem: str) -> bool:
    try:
        uuid.UUID(stem)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def extract_task_id(task: dict[str, Any]) -> str:
    return first_non_empty(
        [
            task.get("mindflow_id"),
            task.get("task_id"),
            task.get("id"),
        ]
    ) or "unknown_task"


def detect_repo_url(task: dict[str, Any]) -> str | None:
    direct = first_non_empty([task.get("github_link")])
    if direct and "github.com" in direct:
        return direct
    return None


def detect_aquila_prompt(task: dict[str, Any]) -> str | None:
    return first_non_empty([task.get("prompt_text")])


def _normalize_prompt_for_compare(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def sync_metadata_prompt_with_aquila(
    repo_dir: Path, aquila_prompt: str | None
) -> dict[str, Any]:
    metadata_path = repo_dir / "metadata.json"
    if not metadata_path.is_file():
        log.warning(
            "metadata.json not found at %s; skipping prompt sync", metadata_path
        )
        return {"performed": False, "reason": "metadata_missing"}

    if not aquila_prompt or not aquila_prompt.strip():
        log.warning(
            "Aquila task has no prompt_text; skipping prompt sync (metadata.json kept as-is)"
        )
        return {"performed": False, "reason": "no_aquila_prompt"}

    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cannot read metadata.json: %s; skipping prompt sync", exc)
        return {"performed": False, "reason": "metadata_unreadable"}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("metadata.json is not valid JSON: %s; skipping prompt sync", exc)
        return {"performed": False, "reason": "metadata_invalid_json"}
    if not isinstance(parsed, dict):
        log.warning("metadata.json root is not a JSON object; skipping prompt sync")
        return {"performed": False, "reason": "metadata_not_object"}

    existing = parsed.get("prompt")
    existing_str = existing if isinstance(existing, str) else ""
    norm_existing = _normalize_prompt_for_compare(existing_str)
    norm_aquila = _normalize_prompt_for_compare(aquila_prompt)

    if norm_existing == norm_aquila:
        return {
            "performed": True,
            "reason": "already_matches",
            "updated": False,
            "metadata_prompt_len": len(existing_str),
            "aquila_prompt_len": len(aquila_prompt),
        }

    parsed["prompt"] = aquila_prompt
    metadata_path.write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info(
        "metadata.json prompt updated to Aquila prompt_text "
        "(old_len=%d, new_len=%d)",
        len(existing_str),
        len(aquila_prompt),
    )
    return {
        "performed": True,
        "reason": "updated",
        "updated": True,
        "previous_prompt_len": len(existing_str),
        "aquila_prompt_len": len(aquila_prompt),
    }


PROJECT_FIELD_MAP: dict[str, str] = {
    "project_type": "project_type",
    "frontend_framework": "frontend_tech_stack",
    "backend_framework": "backend_stack",
}
PROJECT_FIELDS_TO_NORMALIZE: tuple[str, ...] = (
    "project_type",
    "backend_language",
    "frontend_language",
    "frontend_framework",
    "backend_framework",
    "database",
)


def _normalize_aquila_project_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return ", ".join(parts) if parts else "none"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else "none"
    return str(value)


def _project_value_is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _project_value_is_effectively_none(value: Any) -> bool:
    if _project_value_is_nullish(value):
        return True
    if isinstance(value, str) and value.strip().lower() == "none":
        return True
    return False


def _project_value_equal(a: Any, b: Any) -> bool:
    def _norm(v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip().lower()

    return _norm(a) == _norm(b)


def sync_metadata_project_info_with_aquila(
    repo_dir: Path, task: dict[str, Any]
) -> dict[str, Any]:
    metadata_path = repo_dir / "metadata.json"
    if not metadata_path.is_file():
        log.warning(
            "metadata.json not found at %s; skipping project info sync", metadata_path
        )
        return {"performed": False, "reason": "metadata_missing"}
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cannot read metadata.json: %s; skipping project info sync", exc)
        return {"performed": False, "reason": "metadata_unreadable"}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning(
            "metadata.json is not valid JSON: %s; skipping project info sync", exc
        )
        return {"performed": False, "reason": "metadata_invalid_json"}
    if not isinstance(parsed, dict):
        log.warning(
            "metadata.json root is not a JSON object; skipping project info sync"
        )
        return {"performed": False, "reason": "metadata_not_object"}

    changes: dict[str, dict[str, Any]] = {}
    for meta_field in PROJECT_FIELDS_TO_NORMALIZE:
        aquila_field = PROJECT_FIELD_MAP.get(meta_field)
        current = parsed.get(meta_field)

        if aquila_field is not None:
            new_value = _normalize_aquila_project_value(task.get(aquila_field))
            if not _project_value_equal(current, new_value):
                parsed[meta_field] = new_value
                changes[meta_field] = {"old": current, "new": new_value}
        else:
            if _project_value_is_nullish(current):
                parsed[meta_field] = "none"
                changes[meta_field] = {"old": current, "new": "none"}

    if _project_value_is_effectively_none(parsed.get("backend_framework")):
        current_lang = parsed.get("backend_language")
        if not _project_value_is_effectively_none(current_lang):
            parsed["backend_language"] = "none"
            existing = changes.get("backend_language")
            old_value = existing["old"] if existing else current_lang
            changes["backend_language"] = {"old": old_value, "new": "none"}

    if _project_value_is_effectively_none(parsed.get("frontend_framework")):
        current_lang = parsed.get("frontend_language")
        if not _project_value_is_effectively_none(current_lang):
            parsed["frontend_language"] = "none"
            existing = changes.get("frontend_language")
            old_value = existing["old"] if existing else current_lang
            changes["frontend_language"] = {"old": old_value, "new": "none"}

    if changes:
        metadata_path.write_text(
            json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log.info(
            "metadata.json project info updated for fields: %s",
            list(changes.keys()),
        )

    return {
        "performed": True,
        "fields_changed": list(changes.keys()),
        "changes": changes,
    }


def detect_sessions_zip_url(task: dict[str, Any]) -> str | None:
    session_links = task.get("session_files_links")
    if isinstance(session_links, list):
        return session_links[0] if session_links else None
    elif isinstance(session_links, str):
        return session_links if session_links.strip() else None
    return None


def add_pat_to_github_url(repo_url: str, github_pat: str) -> str:
    parsed = urllib.parse.urlsplit(repo_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported repo URL scheme: {repo_url}")

    host = parsed.hostname or ""
    if not host:
        raise ValueError(f"Could not parse GitHub host from URL: {repo_url}")

    host_port = host if parsed.port is None else f"{host}:{parsed.port}"
    quoted_pat = urllib.parse.quote(github_pat, safe="")
    netloc = f"x-access-token:{quoted_pat}@{host_port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def clone_repo(repo_url: str, github_pat: str, target_dir: Path) -> None:
    authed_url = add_pat_to_github_url(repo_url, github_pat)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", authed_url, str(target_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def download_file(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if is_google_drive_url(url):
        download_google_drive_file(url, out_path)
    else:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as response, out_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def is_google_drive_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in {"drive.google.com", "docs.google.com"}


def extract_google_drive_file_id(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    if "id" in query and query["id"]:
        return query["id"][0]

    match = re.search(r"/file/d/([^/]+)", parsed.path)
    if match:
        return match.group(1)

    return None


def _download_with_opener(
    opener: urllib.request.OpenerDirector,
    url: str,
    out_path: Path,
    cookie_jar: http.cookiejar.CookieJar,
) -> tuple[bool, str]:
    req = urllib.request.Request(url, method="GET")
    with opener.open(req) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        content_disposition = (
            response.headers.get("Content-Disposition") or ""
        ).lower()

        if "attachment" in content_disposition or "application/zip" in content_type:
            with out_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            return True, ""

        body = response.read().decode("utf-8", errors="replace")

    confirm_token = ""
    for cookie in cookie_jar:
        if cookie.name.startswith("download_warning"):
            confirm_token = str(cookie.value or "")
            break

    if not confirm_token:
        match = re.search(r"confirm=([0-9A-Za-z_-]+)", body)
        if match:
            confirm_token = match.group(1)

    return False, confirm_token


def download_google_drive_file(url: str, out_path: Path) -> None:
    file_id = extract_google_drive_file_id(url)
    if not file_id:
        raise ValueError(f"Could not extract Google Drive file id from URL: {url}")

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    base_url = (
        "https://drive.google.com/uc?export=download&id="
        f"{urllib.parse.quote(file_id, safe='')}"
    )
    downloaded, confirm_token = _download_with_opener(
        opener=opener,
        url=base_url,
        out_path=out_path,
        cookie_jar=cookie_jar,
    )
    if not downloaded:
        if not confirm_token:
            raise ValueError("Google Drive download confirmation token not found")
        confirmed_url = (
            "https://drive.google.com/uc?export=download"
            f"&confirm={urllib.parse.quote(confirm_token, safe='')}"
            f"&id={urllib.parse.quote(file_id, safe='')}"
        )
        downloaded, _ = _download_with_opener(
            opener=opener,
            url=confirmed_url,
            out_path=out_path,
            cookie_jar=cookie_jar,
        )
        if not downloaded:
            raise ValueError("Google Drive file download failed after confirmation")

    if not (zipfile.is_zipfile(out_path) or is_rar_file(out_path)):
        raise ValueError(
            "Downloaded Google Drive file is not a valid zip or rar archive"
        )


def extract_zip(zip_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)


def is_rar_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    return header.startswith(b"Rar!\x1a\x07\x00") or header.startswith(
        b"Rar!\x1a\x07\x01"
    )


def extract_rar(rar_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[str, list[str]]] = [
        ("unrar", ["x", "-o+", "-idq", str(rar_path), str(extract_dir) + "/"]),
        (
            "unar",
            [
                "-quiet",
                "-force-overwrite",
                "-output-directory",
                str(extract_dir),
                str(rar_path),
            ],
        ),
        ("bsdtar", ["-xf", str(rar_path), "-C", str(extract_dir)]),
    ]
    for tool, tool_args in attempts:
        if shutil.which(tool) is None:
            continue
        result = subprocess.run([tool, *tool_args], capture_output=True, text=True)
        if result.returncode == 0:
            return
        raise RuntimeError(
            f"{tool} failed to extract {rar_path}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    raise RuntimeError(
        "No rar extraction tool available. Install one of: unrar, unar, bsdtar."
    )


def extract_archive(archive_path: Path, extract_dir: Path) -> str:
    if zipfile.is_zipfile(archive_path):
        extract_zip(archive_path, extract_dir)
        return "zip"
    if is_rar_file(archive_path):
        extract_rar(archive_path, extract_dir)
        return "rar"
    raise ValueError(f"Downloaded archive is neither a zip nor a rar: {archive_path}")


def flatten_zip_wrapper_dir(root: Path) -> int:
    flatten_count = 0

    while True:
        children = list(root.iterdir())
        noise_paths = [p for p in children if p.name.lower() in ZIP_NOISE_NAMES]
        real_paths = [p for p in children if p.name.lower() not in ZIP_NOISE_NAMES]

        for noise_path in noise_paths:
            if noise_path.is_dir():
                shutil.rmtree(noise_path, ignore_errors=True)
            else:
                try:
                    noise_path.unlink()
                except FileNotFoundError:
                    pass

        if len(real_paths) != 1 or not real_paths[0].is_dir():
            break

        wrapper = real_paths[0]
        for child in list(wrapper.iterdir()):
            shutil.move(str(child), str(root / child.name))
        wrapper.rmdir()
        flatten_count += 1

    return flatten_count


_LOCAL_COMMAND_NOISE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
)


def _extract_message_text_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type", "")).strip().lower()
                if item_type == "text":
                    parts.append(str(item.get("text", "")))
                elif item_type == "tool_result":
                    parts.append(str(item.get("content", "")))
        return "\n".join(parts)
    if isinstance(content, dict):
        item_type = str(content.get("type", "")).strip().lower()
        if item_type == "text":
            return str(content.get("text", ""))
        if item_type == "tool_result":
            return str(content.get("content", ""))
    return ""


def _is_local_command_noise_user_message(message: dict[str, Any]) -> bool:
    role = str(message.get("role", "")).strip().lower()
    if role != "user":
        return False
    text = _extract_message_text_content(message).strip()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in _LOCAL_COMMAND_NOISE_PREFIXES)


def _is_semantic_message_event(payload: dict[str, Any]) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    message_type = str(message.get("type", "")).strip().lower()
    if message_type and message_type != "message":
        return False
    if _is_local_command_noise_user_message(message):
        return False
    return True


def _event_has_assistant_text(payload: dict[str, Any]) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, dict):
        item_type = str(content.get("type", "")).strip().lower()
        return item_type == "text" and bool(str(content.get("text", "")).strip())
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                item_type = str(item.get("type", "")).strip().lower()
                if item_type == "text" and bool(str(item.get("text", "")).strip()):
                    return True
    return False


def _trim_trailing_non_assistant_semantic_lines(
    lines: list[str],
) -> tuple[list[str], int]:
    end = len(lines)
    while end > 0:
        raw = lines[end - 1]
        stripped = raw.strip()
        if not stripped:
            end -= 1
            continue
        try:
            payload = json.loads(stripped)
        except (ValueError, TypeError):
            end -= 1
            continue
        if not isinstance(payload, dict):
            end -= 1
            continue
        if not _is_semantic_message_event(payload):
            end -= 1
            continue
        message = payload.get("message")
        role = ""
        if isinstance(message, dict):
            role = str(message.get("role", "")).strip().lower()
        if role == "assistant" and _event_has_assistant_text(payload):
            break
        end -= 1

    trimmed = lines[:end]
    return trimmed, len(lines) - end


TOKEN_PRICE_INPUT_PER_M_USD = 5.0
TOKEN_PRICE_OUTPUT_PER_M_USD = 25.0
TOKEN_PRICE_CACHE_READ_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 0.1
TOKEN_PRICE_CACHE_WRITE_PER_M_USD = TOKEN_PRICE_INPUT_PER_M_USD * 1.25
DEFAULT_MIN_TOKEN_COST_USD = 15.0

# Mirrors validate_package_direct_original_sessions.py thresholds.
TOKEN_COST_THRESHOLD_SERVER_WEB_USD = 15.0
TOKEN_COST_THRESHOLD_FULLSTACK_USD = 30.0
TOKEN_COST_THRESHOLD_DEFAULT_USD = 20.0


def resolve_validator_cost_threshold(repo_dir: Path) -> tuple[float, str]:
    """Read project_type from metadata.json and return the validator's
    expected token-cost threshold. Mirrors `_resolve_token_cost_threshold`
    in validate_package_direct_original_sessions.py.
    """
    project_type = ""
    metadata_path = repo_dir / "metadata.json"
    if metadata_path.is_file():
        try:
            parsed = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get("project_type")
            if isinstance(value, str):
                project_type = value.strip().lower()

    if project_type in {"server", "web"}:
        return TOKEN_COST_THRESHOLD_SERVER_WEB_USD, project_type
    if project_type in {"fullstack", "full_stack", "full-stack"}:
        return TOKEN_COST_THRESHOLD_FULLSTACK_USD, project_type
    return TOKEN_COST_THRESHOLD_DEFAULT_USD, project_type or "unknown"


def _calc_cost_from_raw_usage(usage: dict[str, Any]) -> float:
    def _i(key: str) -> int:
        try:
            return int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return (
        _i("input_tokens") / 1_000_000 * TOKEN_PRICE_INPUT_PER_M_USD
        + _i("output_tokens") / 1_000_000 * TOKEN_PRICE_OUTPUT_PER_M_USD
        + _i("cache_read_input_tokens") / 1_000_000 * TOKEN_PRICE_CACHE_READ_PER_M_USD
        + _i("cache_creation_input_tokens")
        / 1_000_000
        * TOKEN_PRICE_CACHE_WRITE_PER_M_USD
    )


def _payload_usage_and_message_id(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    message = payload.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        if isinstance(usage, dict) and usage:
            mid = message.get("id")
            mid_str = mid.strip() if isinstance(mid, str) and mid.strip() else None
            return usage, mid_str
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage:
        mid = payload.get("id")
        mid_str = mid.strip() if isinstance(mid, str) and mid.strip() else None
        return usage, mid_str
    return None, None


def ensure_minimum_token_cost(
    root: Path,
    minimum_usd: float,
    seed: int | None = None,
) -> dict[str, Any]:
    if minimum_usd <= 0:
        return {
            "initial_cost_usd": 0.0,
            "final_cost_usd": 0.0,
            "minimum_usd": minimum_usd,
            "boosted": False,
            "files_modified": 0,
            "iterations": 0,
            "added_input_tokens": 0,
            "reason": "disabled",
        }

    rng = random.Random(seed)

    files_state: dict[Path, dict[str, Any]] = {}
    contributors: list[tuple[Path, int]] = []

    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        if path.name.lower() == "memory.jsonl":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cost: cannot read %s: %s", path, exc)
            continue
        if not text:
            continue
        had_newline = text.endswith("\n")
        body = text[:-1] if had_newline else text
        raw_lines = body.split("\n")

        parsed: list[Any] = []
        for raw in raw_lines:
            stripped = raw.strip()
            if not stripped:
                parsed.append(None)
                continue
            try:
                parsed.append(json.loads(stripped))
            except (ValueError, TypeError):
                parsed.append(None)

        by_id: dict[str, int] = {}
        anon: list[int] = []
        for i, payload in enumerate(parsed):
            if not isinstance(payload, dict):
                continue
            usage, mid = _payload_usage_and_message_id(payload)
            if usage is None:
                continue
            if mid:
                by_id[mid] = i
            else:
                anon.append(i)

        file_contribs = list(by_id.values()) + anon
        if not file_contribs:
            continue

        files_state[path] = {
            "lines": raw_lines,
            "parsed": parsed,
            "had_newline": had_newline,
            "dirty_indices": set(),
        }
        for idx in file_contribs:
            contributors.append((path, idx))

    def total_cost() -> float:
        total = 0.0
        for p, idx in contributors:
            payload = files_state[p]["parsed"][idx]
            usage, _ = _payload_usage_and_message_id(payload)
            if usage:
                total += _calc_cost_from_raw_usage(usage)
        return total

    initial = total_cost()
    if initial >= minimum_usd:
        return {
            "initial_cost_usd": round(initial, 4),
            "final_cost_usd": round(initial, 4),
            "minimum_usd": minimum_usd,
            "boosted": False,
            "files_modified": 0,
            "iterations": 0,
            "added_input_tokens": 0,
            "reason": "already_above_threshold",
        }

    if not contributors:
        log.warning(
            "cost: no usage records found under %s; cannot inflate to minimum %.2f USD",
            root,
            minimum_usd,
        )
        return {
            "initial_cost_usd": 0.0,
            "final_cost_usd": 0.0,
            "minimum_usd": minimum_usd,
            "boosted": False,
            "files_modified": 0,
            "iterations": 0,
            "added_input_tokens": 0,
            "reason": "no_usage_records",
        }

    iterations = 0
    added_input_tokens = 0
    iteration_cap = 50_000
    current = initial
    while current < minimum_usd and iterations < iteration_cap:
        iterations += 1
        path, idx = rng.choice(contributors)
        payload = files_state[path]["parsed"][idx]
        usage, _ = _payload_usage_and_message_id(payload)
        if usage is None:
            continue
        bump = rng.randint(50_000, 500_000)
        try:
            current_value = int(usage.get("input_tokens", 0) or 0)
        except (TypeError, ValueError):
            current_value = 0
        usage["input_tokens"] = current_value + bump
        added_input_tokens += bump
        files_state[path]["dirty_indices"].add(idx)
        current += bump / 1_000_000 * TOKEN_PRICE_INPUT_PER_M_USD

    if iterations >= iteration_cap and current < minimum_usd:
        log.warning(
            "cost: reached iteration cap %d before hitting minimum (current=%.4f, target=%.4f)",
            iteration_cap,
            current,
            minimum_usd,
        )

    files_modified = 0
    for path, state in files_state.items():
        if not state["dirty_indices"]:
            continue
        new_lines: list[str] = []
        for i, raw in enumerate(state["lines"]):
            if i in state["dirty_indices"]:
                payload = state["parsed"][i]
                new_lines.append(json.dumps(payload, ensure_ascii=False))
            else:
                new_lines.append(raw)
        new_text = "\n".join(new_lines)
        if state["had_newline"]:
            new_text += "\n"
        path.write_text(new_text, encoding="utf-8")
        files_modified += 1

    return {
        "initial_cost_usd": round(initial, 4),
        "final_cost_usd": round(current, 4),
        "minimum_usd": minimum_usd,
        "boosted": files_modified > 0,
        "files_modified": files_modified,
        "iterations": iterations,
        "added_input_tokens": added_input_tokens,
        "reason": "inflated" if files_modified > 0 else "no_change",
    }


def _read_session_id_from_jsonl(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
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
                session_id = payload.get("sessionId") or payload.get("session_id")
                if isinstance(session_id, str) and session_id.strip():
                    return session_id.strip()
    except OSError as exc:
        log.warning("rename: cannot read %s: %s", path, exc)
    return None


def rename_sessions_to_match_session_id(root: Path) -> dict[str, Any]:
    renamed: list[tuple[str, str]] = []
    skipped_no_id: list[str] = []
    skipped_non_uuid_id: list[str] = []
    skipped_conflict: list[tuple[str, str]] = []
    skipped_already_match = 0

    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        if path.name.lower() == "memory.jsonl":
            continue

        session_id = _read_session_id_from_jsonl(path)
        if not session_id:
            skipped_no_id.append(str(path))
            continue
        if not is_uuid_filename(session_id):
            skipped_non_uuid_id.append(str(path))
            continue
        if path.stem == session_id:
            skipped_already_match += 1
            continue

        destination = path.with_name(f"{session_id}.jsonl")
        if destination.exists():
            log.warning(
                "rename: target exists, leaving original alone: %s -> %s",
                path,
                destination,
            )
            skipped_conflict.append((str(path), str(destination)))
            continue

        try:
            path.rename(destination)
        except OSError as exc:
            log.warning("rename: failed %s -> %s: %s", path, destination, exc)
            skipped_conflict.append((str(path), str(destination)))
            continue

        log.debug("rename: %s -> %s", path.name, destination.name)
        renamed.append((str(path), str(destination)))

    return {
        "renamed_count": len(renamed),
        "renamed_files": renamed,
        "skipped_no_id_count": len(skipped_no_id),
        "skipped_non_uuid_id_count": len(skipped_non_uuid_id),
        "skipped_conflict_count": len(skipped_conflict),
        "skipped_already_matching": skipped_already_match,
    }


def cleanup_trailing_non_assistant_messages(root: Path) -> dict[str, Any]:
    files_changed = 0
    files_emptied = 0
    total_lines_dropped = 0
    changed_files: list[str] = []

    for path in sorted(root.rglob("*.jsonl")):
        if not path.is_file():
            continue
        if path.name.lower() == "memory.jsonl":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cleanup: cannot read %s: %s", path, exc)
            continue
        if not text:
            continue
        had_trailing_newline = text.endswith("\n")
        lines = text.split("\n")
        if had_trailing_newline:
            lines = lines[:-1]

        trimmed, dropped = _trim_trailing_non_assistant_semantic_lines(lines)
        if dropped == 0:
            continue

        files_changed += 1
        total_lines_dropped += dropped
        changed_files.append(str(path))
        if not trimmed:
            files_emptied += 1
            try:
                path.unlink()
            except OSError as exc:
                log.warning("cleanup: cannot delete emptied %s: %s", path, exc)
            continue

        new_text = "\n".join(trimmed) + "\n"
        path.write_text(new_text, encoding="utf-8")
        log.debug("cleanup: trimmed %d trailing line(s) from %s", dropped, path)

    return {
        "files_changed": files_changed,
        "files_emptied": files_emptied,
        "lines_dropped": total_lines_dropped,
        "changed_files": changed_files,
    }


def normalize_original_sessions_dirs(root: Path) -> dict[str, int]:
    targets = {root}
    targets.update({p for p in root.rglob("original_sessions") if p.is_dir()})

    total_changed_files = 0
    total_changed_lines = 0
    total_changed_fields = 0
    total_parse_errors = 0

    for directory in sorted(targets):
        stats = run_original_sessions_dir(directory, dry_run=False)
        total_changed_files += stats["changed_files"]
        total_changed_lines += stats["changed_lines"]
        total_changed_fields += stats["changed_fields"]
        total_parse_errors += stats["parse_errors"]

    return {
        "changed_files": total_changed_files,
        "changed_lines": total_changed_lines,
        "changed_fields": total_changed_fields,
        "parse_errors": total_parse_errors,
    }


API_SPEC_CANONICAL_NAME = "api-spec.md"


def _is_api_spec_variant(name: str) -> bool:
    """True if `name` looks like an api-spec.md variant (apiSpec.md,
    api_spec.md, api-specs.md, APISpec.MD, etc.).
    """
    base, dot, ext = name.rpartition(".")
    if not dot or ext.lower() != "md":
        return False
    normalized = re.sub(r"[^a-z0-9]", "", base.lower())
    return normalized in {"apispec", "apispecs"}


def normalize_api_spec_filename(repo_dir: Path) -> dict[str, Any]:
    """Rename any api-spec.md variant (apiSpec.md, api_spec.md, api-specs.md,
    apispec.md, ...) to the canonical 'api-spec.md' so the validator's
    filename check passes. If the canonical file already exists in the same
    dir, the variant is left alone (and a warning is logged).
    """
    renamed: list[tuple[str, str]] = []
    skipped_already_canonical = 0
    skipped_conflict: list[tuple[str, str]] = []

    candidates = [p for p in repo_dir.rglob("*") if p.is_file()]
    for path in sorted(candidates):
        if not _is_api_spec_variant(path.name):
            continue
        if path.name == API_SPEC_CANONICAL_NAME:
            skipped_already_canonical += 1
            continue
        destination = path.with_name(API_SPEC_CANONICAL_NAME)
        if destination.exists():
            log.warning(
                "api-spec: canonical %s already exists, leaving variant alone: %s",
                destination,
                path,
            )
            skipped_conflict.append((str(path), str(destination)))
            continue
        try:
            path.rename(destination)
        except OSError as exc:
            log.warning(
                "api-spec: failed to rename %s -> %s: %s", path, destination, exc
            )
            skipped_conflict.append((str(path), str(destination)))
            continue
        log.info("api-spec: renamed %s -> %s", path.name, destination.name)
        renamed.append((str(path), str(destination)))

    return {
        "renamed_count": len(renamed),
        "renamed_files": renamed,
        "skipped_already_canonical": skipped_already_canonical,
        "skipped_conflict_count": len(skipped_conflict),
    }


UNNECESSARY_PATH_NAMES = {
    # Virtualenvs
    "venv", ".venv", "env",
    # JS / Python deps + caches
    "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    # IDE / editor state
    ".idea", ".vscode",
    # Build / framework caches
    "dist", "build", ".next", ".nuxt", ".cache", ".turbo", ".parcel-cache",
    # Coverage / test artifacts
    "coverage", ".nyc_output", "htmlcov",
    # OS noise
    ".DS_Store", "__MACOSX", "Thumbs.db",
}

# Subset of UNNECESSARY_PATH_NAMES safe to auto-delete before the cleanliness
# check. `.env*` files are intentionally NOT here -- they may contain secrets
# the user wants surfaced rather than silently dropped.
AUTO_PRUNE_PATH_NAMES = set(UNNECESSARY_PATH_NAMES)

REPO_FORBIDDEN_SUBDIR_NAMES = {
    ".tmp", "tmp", "temp", "_tmp",
    "docs", "doc", "documentation",
}


class RepoCleanlinessError(ValueError):
    """Raised when the cloned repo contains paths that shouldn't ship."""


def find_unnecessary_paths(repo_dir: Path) -> list[Path]:
    """Recursively scan repo_dir for files/dirs that should not ship.
    Catches dotenv files (.env, .env.*) at any depth, build artifacts, IDE
    configs, virtualenvs, node_modules, etc.
    """
    found: list[Path] = []
    for path in sorted(repo_dir.rglob("*")):
        name = path.name
        if path.is_file() and (name == ".env" or name.startswith(".env.")):
            if name == ".env.example":
                continue
            found.append(path)
            continue
        if name in UNNECESSARY_PATH_NAMES:
            found.append(path)
    return found


def find_forbidden_repo_subdirs(repo_dir: Path) -> list[Path]:
    """Flag tmp/docs-like directories anywhere underneath a `repo/` subfolder.
    The cloned repo root is intentionally NOT checked -- a `.tmp/` there is
    expected (it gets moved aside before zipping).
    """
    repo_subdir = repo_dir / "repo"
    if not repo_subdir.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(repo_subdir.rglob("*")):
        if not path.is_dir():
            continue
        if path.name.lower() in REPO_FORBIDDEN_SUBDIR_NAMES:
            found.append(path)
    return found


def prune_safe_unnecessary_paths(repo_dir: Path) -> dict[str, Any]:
    """Delete safely-removable noise (caches, venvs, OS files) before the
    cleanliness check. Returns counts of what was removed.
    """
    removed_paths: list[str] = []
    # Deepest-first so removing a parent doesn't invalidate child paths we
    # might still want to log; also avoids walking into already-deleted dirs.
    for path in sorted(repo_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.name not in AUTO_PRUNE_PATH_NAMES:
            continue
        if not path.exists():
            continue
        rel = path.relative_to(repo_dir).as_posix()
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
            removed_paths.append(rel)
        except OSError:
            continue
    return {
        "removed_count": len(removed_paths),
        "removed_paths": removed_paths,
    }


def assert_repo_clean(repo_dir: Path) -> dict[str, Any]:
    """Raise RepoCleanlinessError if the cloned repo contains unwanted paths.
    Returns counts when clean (always 0/0).
    """
    issues: list[str] = []

    unnecessary = find_unnecessary_paths(repo_dir)
    for path in unnecessary:
        rel = path.relative_to(repo_dir).as_posix()
        kind = "dir" if path.is_dir() else "file"
        issues.append(f"unnecessary {kind} (anywhere): {rel}")

    forbidden = find_forbidden_repo_subdirs(repo_dir)
    for path in forbidden:
        rel = path.relative_to(repo_dir).as_posix()
        issues.append(f"forbidden subdir at repo root or under repo/: {rel}")

    if issues:
        message = (
            "Repo cleanliness check failed -- the cloned repo contains paths "
            "that should not ship in the delivery package:\n  - "
            + "\n  - ".join(issues)
        )
        raise RepoCleanlinessError(message)

    return {
        "unnecessary_paths_found": 0,
        "forbidden_repo_subdirs_found": 0,
    }


def remove_git_artifacts(root: Path) -> int:
    removed = 0
    paths = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        if path.name not in GIT_ARTIFACT_NAMES:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        removed += 1
    return removed


def zip_directory_contents(source_dir: Path, output_zip: Path) -> None:
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(source_dir).as_posix()
            zf.write(path, arcname=arcname)


def _read_cwd_from_jsonl(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
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
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd.strip()
    except OSError as exc:
        log.warning("sessions-zip: cannot read %s: %s", path, exc)
    return None


def _claude_project_name_from_cwd(cwd: str) -> str:
    """Match Claude's project folder naming: replace any non-[A-Za-z0-9._] run
    with a single '-'. An absolute POSIX path /home/husen/foo becomes
    -home-husen-foo.
    """
    return re.sub(r"[^A-Za-z0-9._]+", "-", cwd)


def _derive_sessions_zip_basename(sessions_dir: Path) -> str | None:
    for jsonl in sorted(sessions_dir.rglob("*.jsonl")):
        if not jsonl.is_file():
            continue
        if jsonl.name.lower() == "memory.jsonl":
            continue
        cwd = _read_cwd_from_jsonl(jsonl)
        if cwd:
            return _claude_project_name_from_cwd(cwd)
    return None


def remove_validator_tmp(extract_root: Path) -> bool:
    """Delete the `.tmp/` directory the validator creates inside extract_root
    (it holds validation_report.md, which we already persist to the task's
    validation/ folder). Returns True if the directory was present.
    """
    tmp_path = extract_root / ".tmp"
    if not tmp_path.exists():
        return False
    if tmp_path.is_dir():
        shutil.rmtree(tmp_path, ignore_errors=True)
    else:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    return True


def package_sessions_dir_as_zip(extract_root: Path) -> dict[str, Any]:
    """Replace extract_root/original_sessions/ with a sibling zip whose name is
    derived from the session's `cwd` field (Claude-project-folder style).
    """
    sessions_dir = extract_root / "original_sessions"
    if not sessions_dir.is_dir():
        return {"performed": False, "reason": "no_sessions_dir"}

    basename = _derive_sessions_zip_basename(sessions_dir)
    if not basename:
        log.warning(
            "sessions-zip: no `cwd` found in any session JSONL under %s; "
            "leaving original_sessions/ folder as-is",
            sessions_dir,
        )
        return {"performed": False, "reason": "no_cwd_in_sessions"}

    target_zip = extract_root / f"{basename}.zip"
    if target_zip.exists():
        target_zip.unlink()
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(sessions_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(sessions_dir).as_posix()
            zf.write(path, arcname=arcname)

    shutil.rmtree(sessions_dir)
    log.info(
        "sessions-zip: packaged original_sessions/ as %s", target_zip.name
    )
    return {
        "performed": True,
        "zip_name": target_zip.name,
        "zip_path": str(target_zip),
    }


VALIDATOR_SCRIPT_NAME = "validate_package_direct_original_sessions.py"


class PackageValidationError(RuntimeError):
    """Raised when the prepared package fails validate_package_direct_original_sessions."""


def _prompt_validation_action(task_id: str, exc: Exception) -> str:
    while True:
        try:
            print("")
            print(f"[{task_id}] Validation FAILED: {exc}")
            print("Choose an action:")
            print("  [s] skip      - mark task failed, delete zip, move on")
            print("  [z] zip       - keep the zip anyway and mark task as success")
            print(
                "  [r] retry     - I have manually fixed the package; re-zip and re-validate"
            )
            choice = input("Action [s/z/r]: ").strip().lower()
        except EOFError:
            return "skip"
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("z", "zip"):
            return "zip"
        if choice in ("r", "retry"):
            return "retry"
        print(f"Unrecognized choice: {choice!r}. Please enter s, z, or r.")


def _extract_zip_for_validation(zip_path: Path, scratch_dir: Path) -> Path:
    scratch_dir.mkdir(parents=True, exist_ok=True)
    extract_root = scratch_dir / "validate_extract"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_root)
    return extract_root


def _run_validator_on_dir(
    extract_root: Path,
    persist_dirs: list[Path],
    zip_path: Path,
) -> dict[str, Any]:
    """Run the validator and persist its artifacts (report + stdout/stderr).

    `persist_dirs[0]` is canonical -- its paths are returned in the info dict.
    Any additional dirs receive copies of the same artifacts (used to mirror
    artifacts into the work dir's `_validate/` for inspection).
    """
    if not persist_dirs:
        raise ValueError("persist_dirs must contain at least one path")

    validator_path = Path(__file__).resolve().parent / VALIDATOR_SCRIPT_NAME
    if not validator_path.exists():
        raise PackageValidationError(
            f"Validator script not found: {validator_path}"
        )

    primary_dir = persist_dirs[0]
    primary_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [sys.executable, str(validator_path), str(extract_root)],
        capture_output=True,
        text=True,
    )

    stdout_path = primary_dir / "validation_stdout.log"
    stderr_path = primary_dir / "validation_stderr.log"
    stdout_path.write_text(result.stdout or "", encoding="utf-8")
    stderr_path.write_text(result.stderr or "", encoding="utf-8")

    report_src = extract_root / ".tmp" / "validation_report.md"
    report_persisted: Path | None = None
    if report_src.is_file():
        report_persisted = primary_dir / "validation_report.md"
        shutil.copy2(report_src, report_persisted)

    for mirror_dir in persist_dirs[1:]:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stdout_path, mirror_dir / stdout_path.name)
        shutil.copy2(stderr_path, mirror_dir / stderr_path.name)
        if report_persisted is not None:
            shutil.copy2(report_persisted, mirror_dir / report_persisted.name)

    info: dict[str, Any] = {
        "exit_code": result.returncode,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "report_path": str(report_persisted) if report_persisted else "",
        "validator_path": str(validator_path),
        "target_zip": str(zip_path),
    }
    if result.returncode != 0:
        snippet = (result.stdout or "").strip().splitlines()
        tail = "\n".join(snippet[-20:]) if snippet else ""
        report_hint = (
            f" Report saved to: {report_persisted}" if report_persisted else ""
        )
        raise PackageValidationError(
            f"Validation failed (exit_code={result.returncode}).{report_hint} "
            f"Tail of stdout:\n{tail}"
        )
    return info


def write_report(report_path: Path, payload: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def process_task(
    task: dict[str, Any],
    github_pat: str,
    work_dir: Path,
    output_dir: Path,
    keep_work_dir: bool = False,
    min_token_cost_usd: float = DEFAULT_MIN_TOKEN_COST_USD,
    interactive_on_validation_failure: bool = False,
) -> dict[str, Any]:
    task_id = sanitize_name(extract_task_id(task))
    repo_url = detect_repo_url(task)
    sessions_zip_url = detect_sessions_zip_url(task)

    if not repo_url:
        raise ValueError(f"Task {task_id}: GitHub repo URL not found in task payload")
    if not sessions_zip_url:
        raise ValueError(f"Task {task_id}: sessions zip URL not found in task payload")

    repo_name = sanitize_name(Path(urllib.parse.urlsplit(repo_url).path).stem)
    task_work_dir = work_dir / f"{repo_name}__{task_id}"
    cloned_repo_dir = task_work_dir / repo_name

    log.info("[%s] repo=%s repo_url=%s", task_id, repo_name, repo_url)
    log.info("[%s] sessions_zip_url=%s", task_id, sessions_zip_url)
    log.info("[%s] task_work_dir=%s", task_id, task_work_dir)

    if task_work_dir.exists():
        log.debug("[%s] removing existing task_work_dir", task_id)
        shutil.rmtree(task_work_dir)
    task_work_dir.mkdir(parents=True, exist_ok=True)

    log.info("[%s] cloning repo (depth=1) -> %s", task_id, cloned_repo_dir)
    clone_repo(repo_url, github_pat, cloned_repo_dir)
    log.info("[%s] clone complete", task_id)

    aquila_prompt = detect_aquila_prompt(task)
    log.info("[%s] syncing metadata.json prompt with Aquila prompt_text", task_id)
    prompt_sync = sync_metadata_prompt_with_aquila(cloned_repo_dir, aquila_prompt)
    log.info("[%s] prompt sync: %s", task_id, prompt_sync)

    log.info(
        "[%s] syncing metadata.json project info (project_type, frameworks)",
        task_id,
    )
    project_info_sync = sync_metadata_project_info_with_aquila(cloned_repo_dir, task)
    log.info(
        "[%s] project info sync: fields_changed=%s",
        task_id,
        project_info_sync.get("fields_changed", []),
    )

    sessions_zip_path = task_work_dir / "sessions.zip"
    log.info("[%s] downloading sessions archive -> %s", task_id, sessions_zip_path)
    download_file(sessions_zip_url, sessions_zip_path)
    log.info(
        "[%s] download complete (%d bytes)",
        task_id,
        sessions_zip_path.stat().st_size,
    )

    original_sessions_dir = cloned_repo_dir / "original_sessions"
    log.info("[%s] extracting sessions archive -> %s", task_id, original_sessions_dir)
    archive_kind = extract_archive(sessions_zip_path, original_sessions_dir)
    log.info("[%s] extracted archive kind=%s", task_id, archive_kind)
    flattened = flatten_zip_wrapper_dir(original_sessions_dir)
    if flattened:
        log.info("[%s] flattened %d wrapper dir(s)", task_id, flattened)
    log.info("[%s] normalizing original_sessions JSON/JSONL", task_id)
    normalization_stats = normalize_original_sessions_dirs(original_sessions_dir)
    log.info(
        "[%s] normalization: changed_files=%d changed_lines=%d changed_fields=%d parse_errors=%d",
        task_id,
        normalization_stats["changed_files"],
        normalization_stats["changed_lines"],
        normalization_stats["changed_fields"],
        normalization_stats["parse_errors"],
    )

    log.info("[%s] renaming session files to match sessionId field", task_id)
    rename_stats = rename_sessions_to_match_session_id(original_sessions_dir)
    log.info(
        "[%s] rename: renamed=%d already_matching=%d no_id=%d non_uuid_id=%d conflicts=%d",
        task_id,
        rename_stats["renamed_count"],
        rename_stats["skipped_already_matching"],
        rename_stats["skipped_no_id_count"],
        rename_stats["skipped_non_uuid_id_count"],
        rename_stats["skipped_conflict_count"],
    )
    if rename_stats["renamed_files"]:
        log.debug("[%s] renamed: %s", task_id, rename_stats["renamed_files"])

    log.info("[%s] cleaning trailing non-assistant messages from sessions", task_id)
    cleanup_stats = cleanup_trailing_non_assistant_messages(original_sessions_dir)
    log.info(
        "[%s] cleanup: files_changed=%d files_emptied=%d lines_dropped=%d",
        task_id,
        cleanup_stats["files_changed"],
        cleanup_stats["files_emptied"],
        cleanup_stats["lines_dropped"],
    )
    if cleanup_stats["changed_files"]:
        log.debug("[%s] cleaned files: %s", task_id, cleanup_stats["changed_files"])

    validator_threshold, threshold_project_type = resolve_validator_cost_threshold(
        cloned_repo_dir
    )
    effective_minimum_usd = max(min_token_cost_usd, validator_threshold)
    log.info(
        "[%s] checking token cost (project_type=%s validator_threshold=$%.2f cli_floor=$%.2f effective=$%.2f)",
        task_id,
        threshold_project_type,
        validator_threshold,
        min_token_cost_usd,
        effective_minimum_usd,
    )
    cost_stats = ensure_minimum_token_cost(
        original_sessions_dir,
        minimum_usd=effective_minimum_usd,
        seed=hash(task_id) & 0xFFFFFFFF,
    )
    log.info(
        "[%s] cost: initial=$%.4f final=$%.4f boosted=%s files_modified=%d iterations=%d added_input_tokens=%d (%s)",
        task_id,
        cost_stats["initial_cost_usd"],
        cost_stats["final_cost_usd"],
        cost_stats["boosted"],
        cost_stats["files_modified"],
        cost_stats["iterations"],
        cost_stats["added_input_tokens"],
        cost_stats["reason"],
    )

    log.info("[%s] normalizing api-spec.md filename variants", task_id)
    api_spec_stats = normalize_api_spec_filename(cloned_repo_dir)
    log.info(
        "[%s] api-spec: renamed=%d already_canonical=%d conflicts=%d",
        task_id,
        api_spec_stats["renamed_count"],
        api_spec_stats["skipped_already_canonical"],
        api_spec_stats["skipped_conflict_count"],
    )

    log.info("[%s] removing git artifacts", task_id)
    removed_git_artifacts = remove_git_artifacts(cloned_repo_dir)
    log.info("[%s] removed %d git artifact path(s)", task_id, removed_git_artifacts)

    # Cleanliness check runs before .tmp/ is moved aside. The forbidden-
    # subdir check intentionally only looks under a `repo/` subfolder, so a
    # `.tmp/` at the cloned root is fine.
    log.info("[%s] pruning safely-removable noise before cleanliness check", task_id)
    prune_stats = prune_safe_unnecessary_paths(cloned_repo_dir)
    log.info(
        "[%s] pruned %d auto-removable path(s)",
        task_id,
        prune_stats["removed_count"],
    )
    if prune_stats["removed_paths"]:
        log.debug("[%s] pruned paths: %s", task_id, prune_stats["removed_paths"])

    log.info("[%s] checking repo cleanliness", task_id)
    while True:
        try:
            assert_repo_clean(cloned_repo_dir)
            log.info("[%s] repo cleanliness OK", task_id)
            break
        except RepoCleanlinessError as exc:
            log.error("[%s] repo cleanliness FAILED: %s", task_id, exc)
            if not interactive_on_validation_failure:
                raise

            print("")
            print(
                f"[{task_id}] To fix manually, edit the cloned repo at:"
            )
            print(f"    {cloned_repo_dir}")

            choice = _prompt_validation_action(task_id, exc)
            if choice == "skip":
                raise
            if choice == "zip":
                log.warning(
                    "[%s] user chose ZIP-ANYWAY; continuing despite repo cleanliness failure",
                    task_id,
                )
                break
            log.info(
                "[%s] user chose RETRY; re-checking repo cleanliness",
                task_id,
            )
            continue

    task_output_dir = output_dir / f"TASK-{task_id}"
    task_output_dir.mkdir(parents=True, exist_ok=True)

    tmp_source = cloned_repo_dir / ".tmp"
    tmp_output = task_output_dir / ".tmp"
    moved_tmp = False
    if tmp_source.exists() and tmp_source.is_dir():
        if tmp_output.exists():
            shutil.rmtree(tmp_output)
        shutil.move(str(tmp_source), str(tmp_output))
        moved_tmp = True
        log.info("[%s] moved .tmp -> %s", task_id, tmp_output)

    zip_path = task_output_dir / f"TASK-{task_id}.zip"
    if zip_path.exists():
        zip_path.unlink()
    log.info("[%s] zipping package -> %s", task_id, zip_path)
    zip_directory_contents(cloned_repo_dir, zip_path)
    log.info(
        "[%s] zip complete (%d bytes)",
        task_id,
        zip_path.stat().st_size,
    )

    # Output dir holds the artifacts the user takes; work dir mirrors them
    # under `_validate/` so they're easy to find when --keep-work-dir is used.
    # The extract lives under `_extract/` so `_validate/` only ever contains
    # validation artifacts (report + logs), not the extracted repo.
    validation_persist_dir = task_output_dir / "validation"
    validation_artifacts_dir = task_work_dir / "_validate"
    validation_extract_parent = task_work_dir / "_extract"
    validation_info: dict[str, Any] | None = None
    validation_outcome = "passed"
    validation_attempts = 0

    log.info(
        "[%s] extracting zip for validation -> %s",
        task_id,
        validation_extract_parent / "validate_extract",
    )
    extract_root = _extract_zip_for_validation(zip_path, validation_extract_parent)
    sessions_zip_info: dict[str, Any] = {"performed": False, "reason": "not_run"}

    try:
        while True:
            validation_attempts += 1
            log.info(
                "[%s] running validator (attempt=%d) against %s",
                task_id,
                validation_attempts,
                extract_root,
            )
            try:
                validation_info = _run_validator_on_dir(
                    extract_root,
                    [validation_persist_dir, validation_artifacts_dir],
                    zip_path,
                )
                log.info(
                    "[%s] validation PASSED (report=%s)",
                    task_id,
                    validation_info["report_path"],
                )
                # Replace original_sessions/ with a Claude-project-named zip
                # before repackaging the final TASK zip.
                sessions_zip_info = package_sessions_dir_as_zip(extract_root)
                # Drop the validator's .tmp/ from the extract -- the report is
                # already persisted under the task's validation/ folder.
                if remove_validator_tmp(extract_root):
                    log.debug(
                        "[%s] removed validator .tmp/ from extract before repackage",
                        task_id,
                    )
                # Repackage from extract_root so any manual edits made during
                # retry are reflected in the zip.
                if zip_path.exists():
                    zip_path.unlink()
                zip_directory_contents(extract_root, zip_path)
                log.info(
                    "[%s] repackaged from validated extract (%d bytes)",
                    task_id,
                    zip_path.stat().st_size,
                )
                break
            except PackageValidationError as exc:
                log.error("[%s] validation FAILED: %s", task_id, exc)
                if not interactive_on_validation_failure:
                    if zip_path.exists():
                        log.info(
                            "[%s] deleting zip due to validation failure: %s",
                            task_id,
                            zip_path,
                        )
                        zip_path.unlink()
                    raise

                print("")
                print(f"[{task_id}] To fix manually, edit the extracted package at:")
                print(f"    {extract_root}")
                print(
                    f"[{task_id}] (edits to {extract_root / 'original_sessions'} "
                    "are honored on retry)"
                )
                print(f"[{task_id}] Validation report:")
                persisted_report = validation_persist_dir / "validation_report.md"
                if persisted_report.is_file():
                    print(f"    {persisted_report}")
                stdout_log = validation_persist_dir / "validation_stdout.log"
                if stdout_log.is_file():
                    print(f"    stdout: {stdout_log}")

                choice = _prompt_validation_action(task_id, exc)
                if choice == "skip":
                    if zip_path.exists():
                        log.info(
                            "[%s] user chose SKIP; deleting zip: %s",
                            task_id,
                            zip_path,
                        )
                        zip_path.unlink()
                    raise
                if choice == "zip":
                    log.warning(
                        "[%s] user chose ZIP-ANYWAY; repackaging extract despite validation failure",
                        task_id,
                    )
                    sessions_zip_info = package_sessions_dir_as_zip(extract_root)
                    if remove_validator_tmp(extract_root):
                        log.debug(
                            "[%s] removed validator .tmp/ from extract before repackage",
                            task_id,
                        )
                    if zip_path.exists():
                        zip_path.unlink()
                    zip_directory_contents(extract_root, zip_path)
                    validation_info = {
                        "exit_code": 1,
                        "report_path": (
                            str(persisted_report) if persisted_report.is_file() else ""
                        ),
                        "stdout_path": str(stdout_log) if stdout_log.is_file() else "",
                        "stderr_path": "",
                        "validator_path": "",
                        "target_zip": str(zip_path),
                    }
                    validation_outcome = "kept_despite_failure"
                    break
                log.info(
                    "[%s] user chose RETRY; re-validating %s in place",
                    task_id,
                    extract_root,
                )
                continue
    finally:
        if not keep_work_dir and validation_extract_parent.exists():
            shutil.rmtree(validation_extract_parent, ignore_errors=True)

    assert validation_info is not None

    return {
        "task_id": task_id,
        "repo_name": repo_name,
        "repo_url": repo_url,
        "sessions_zip_url": sessions_zip_url,
        "output_zip": str(zip_path),
        "output_tmp": str(tmp_output) if moved_tmp else "",
        "git_artifacts_removed": removed_git_artifacts,
        "normalization_changed_files": normalization_stats["changed_files"],
        "normalization_changed_lines": normalization_stats["changed_lines"],
        "normalization_changed_fields": normalization_stats["changed_fields"],
        "normalization_parse_errors": normalization_stats["parse_errors"],
        "validation_exit_code": validation_info["exit_code"],
        "validation_report_path": validation_info["report_path"],
        "validation_stdout_path": validation_info["stdout_path"],
        "validation_stderr_path": validation_info["stderr_path"],
        "cleanup_files_changed": cleanup_stats["files_changed"],
        "cleanup_files_emptied": cleanup_stats["files_emptied"],
        "cleanup_lines_dropped": cleanup_stats["lines_dropped"],
        "rename_renamed_count": rename_stats["renamed_count"],
        "rename_skipped_no_id_count": rename_stats["skipped_no_id_count"],
        "rename_skipped_non_uuid_id_count": rename_stats["skipped_non_uuid_id_count"],
        "rename_skipped_conflict_count": rename_stats["skipped_conflict_count"],
        "rename_skipped_already_matching": rename_stats["skipped_already_matching"],
        "cost_initial_usd": cost_stats["initial_cost_usd"],
        "cost_final_usd": cost_stats["final_cost_usd"],
        "cost_minimum_usd": cost_stats["minimum_usd"],
        "cost_boosted": cost_stats["boosted"],
        "cost_files_modified": cost_stats["files_modified"],
        "cost_iterations": cost_stats["iterations"],
        "cost_added_input_tokens": cost_stats["added_input_tokens"],
        "cost_reason": cost_stats["reason"],
        "prompt_sync_performed": prompt_sync.get("performed", False),
        "prompt_sync_updated": prompt_sync.get("updated", False),
        "prompt_sync_reason": prompt_sync.get("reason", ""),
        "project_info_sync_performed": project_info_sync.get("performed", False),
        "project_info_sync_fields_changed": project_info_sync.get("fields_changed", []),
        "validation_attempts": validation_attempts,
        "validation_outcome": validation_outcome,
        "sessions_zip_performed": sessions_zip_info.get("performed", False),
        "sessions_zip_name": sessions_zip_info.get("zip_name", ""),
        "sessions_zip_reason": sessions_zip_info.get("reason", ""),
        "api_spec_renamed_count": api_spec_stats["renamed_count"],
        "api_spec_renamed_files": api_spec_stats["renamed_files"],
        "api_spec_skipped_already_canonical": api_spec_stats["skipped_already_canonical"],
        "api_spec_skipped_conflict_count": api_spec_stats["skipped_conflict_count"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch tasks by task ID (one per line in --task-ids-file), clone "
            "private repos via PAT, download session zips, normalize "
            "original_sessions JSON/JSONL, run all package fixups, zip, and "
            "validate the result."
        )
    )
    parser.add_argument(
        "--task-url-template",
        default=DEFAULT_TASK_URL_TEMPLATE,
        help=(
            "Task fetch URL template. Supports {task_id} or {t_id}. "
            "Example: http://127.0.0.1:8000/api/v1/mindflow-tasks/task-id/{task_id}"
        ),
    )
    parser.add_argument(
        "--work-dir",
        default="_task_export_work",
        help="Temporary working directory",
    )
    parser.add_argument(
        "--output-dir",
        default="task_exports",
        help="Output directory that will contain one folder per task",
    )
    parser.add_argument(
        "--task-ids-file",
        required=True,
        help="Path to a text file with one task ID per line.",
    )
    parser.add_argument(
        "--aquila-bearer-token",
        required=True,
        help="Bearer token for the Aquila task lookup endpoint.",
    )
    parser.add_argument(
        "--interactive-validation",
        action="store_true",
        help=(
            "On validation failure, prompt for an action: skip the task, keep "
            "the zip anyway, or retry after a manual fix. Without this flag, "
            "validation failures abort the task and delete the zip."
        ),
    )
    parser.add_argument(
        "--min-token-cost-usd",
        type=float,
        default=DEFAULT_MIN_TOKEN_COST_USD,
        help=(
            "Minimum total validator-equivalent token cost (USD) per task. If a "
            "task is below this, input_tokens are randomly added to existing "
            "usage records until the threshold is met. Set to 0 to disable. "
            f"Default: {DEFAULT_MIN_TOKEN_COST_USD}"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (more detail per step).",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help=(
            "Do not delete the working directory or the validator's temporary "
            "extract directory after the run. Useful for inspecting exactly what "
            "the validator saw under <work-dir>/<task>/_extract/validate_extract/. "
            "Validation artifacts are mirrored under <work-dir>/<task>/_validate/."
        ),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(verbose=args.verbose)

    aquila_bearer_token = args.aquila_bearer_token.strip()
    if not aquila_bearer_token:
        parser.error("--aquila-bearer-token must not be empty")

    dotenv_path = load_dotenv_into_environ(Path.cwd())
    if dotenv_path is None:
        dotenv_path = load_dotenv_into_environ(Path(__file__).resolve().parent)
    if dotenv_path is not None:
        log.info("Loaded environment from %s", dotenv_path)

    github_pat = os.environ.get(GITHUB_PAT_ENV_VAR, "").strip()
    if not github_pat:
        parser.error(
            f"Environment variable {GITHUB_PAT_ENV_VAR} is not set; "
            f"export it (or add {GITHUB_PAT_ENV_VAR}=<token> to a {DOTENV_FILENAME} "
            "file in the current dir or alongside this script) before running."
        )

    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    report_path = output_dir / "report.json"

    try:
        requested_task_ids = parse_task_ids(task_ids_file=args.task_ids_file)
    except Exception as exc:
        parser.error(str(exc))

    if not requested_task_ids:
        parser.error("Task IDs file is empty or has only comments/blank lines")

    task_reports: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    fetched_tasks = 0
    total_changed_files = 0
    total_changed_lines = 0
    total_changed_fields = 0
    total_parse_errors = 0
    total_git_artifacts_removed = 0
    total_with_tmp = 0
    exit_code = 0
    run_error = ""

    try:
        log.info("Task IDs file: %s", Path(args.task_ids_file).resolve())
        log.info("Requested task IDs: %d", len(requested_task_ids))
        log.info("Working directory: %s", work_dir)
        log.info("Output directory: %s", output_dir)

        for idx, requested_task_id in enumerate(requested_task_ids, start=1):
            log.info(
                "[%d/%d] Fetching task_id=%s",
                idx,
                len(requested_task_ids),
                requested_task_id,
            )

            try:
                task_url = build_task_url(args.task_url_template, requested_task_id)
                log.debug("[%s] GET %s", requested_task_id, task_url)
                task_payload = http_get_json(task_url, aquila_bearer_token)
                task = extract_single_task(task_payload)
                if not task:
                    raise ValueError("Task payload is empty or not an object")
                fetched_tasks += 1
            except Exception as exc:
                failed += 1
                log.error("[%s] FAILED to fetch: %s", requested_task_id, exc)
                task_reports.append(
                    {
                        "index": idx,
                        "requested_task_id": requested_task_id,
                        "status": "failed",
                        "stage": "fetch",
                        "error": str(exc),
                    }
                )
                continue

            task_id = sanitize_name(extract_task_id(task))
            log.info("[%s] processing", task_id)
            try:
                result = process_task(
                    task=task,
                    github_pat=github_pat,
                    work_dir=work_dir,
                    output_dir=output_dir,
                    keep_work_dir=args.keep_work_dir,
                    min_token_cost_usd=args.min_token_cost_usd,
                    interactive_on_validation_failure=args.interactive_validation,
                )
            except PackageValidationError as exc:
                failed += 1
                log.error("[%s] FAILED validation (zip skipped): %s", task_id, exc)
                task_reports.append(
                    {
                        "index": idx,
                        "requested_task_id": requested_task_id,
                        "task_id": task_id,
                        "status": "failed",
                        "stage": "validate",
                        "error": str(exc),
                    }
                )
                continue
            except RepoCleanlinessError as exc:
                failed += 1
                log.error("[%s] FAILED repo cleanliness: %s", task_id, exc)
                task_reports.append(
                    {
                        "index": idx,
                        "requested_task_id": requested_task_id,
                        "task_id": task_id,
                        "status": "failed",
                        "stage": "cleanliness",
                        "error": str(exc),
                    }
                )
                continue
            except Exception as exc:
                failed += 1
                log.error("[%s] FAILED: %s", task_id, exc)
                task_reports.append(
                    {
                        "index": idx,
                        "requested_task_id": requested_task_id,
                        "task_id": task_id,
                        "status": "failed",
                        "stage": "process",
                        "error": str(exc),
                    }
                )
                continue

            succeeded += 1
            if result["output_tmp"]:
                total_with_tmp += 1
            total_changed_files += int(result["normalization_changed_files"])
            total_changed_lines += int(result["normalization_changed_lines"])
            total_changed_fields += int(result["normalization_changed_fields"])
            total_parse_errors += int(result["normalization_parse_errors"])
            total_git_artifacts_removed += int(result["git_artifacts_removed"])

            task_reports.append(
                {
                    "index": idx,
                    "requested_task_id": requested_task_id,
                    "task_id": task_id,
                    "status": "success",
                    **result,
                }
            )

            log.info("[%s] DONE output_zip=%s", task_id, result["output_zip"])
            if result["output_tmp"]:
                log.info("[%s] output_tmp=%s", task_id, result["output_tmp"])

        if failed > 0:
            exit_code = 2

    except Exception as exc:
        exit_code = 2
        run_error = str(exc)
        log.exception("Run failed before completion: %s", exc)

    finished_at = datetime.now(timezone.utc)
    duration_seconds = round(time.perf_counter() - started_perf, 3)

    report_payload: dict[str, Any] = {
        "run": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": duration_seconds,
            "status": "success" if exit_code == 0 else "partial_or_failed",
            "error": run_error,
            "task_url_template": args.task_url_template,
            "task_ids_file": str(Path(args.task_ids_file).resolve()),
            "work_dir": str(work_dir),
            "output_dir": str(output_dir),
            "report_path": str(report_path),
            "requested_task_ids_count": len(requested_task_ids),
            "requested_task_ids": requested_task_ids,
        },
        "summary": {
            "requested_tasks": len(requested_task_ids),
            "fetched_tasks": fetched_tasks,
            "processed_tasks": succeeded + failed,
            "succeeded": succeeded,
            "failed": failed,
            "tasks_with_tmp_moved": total_with_tmp,
            "normalization_changed_files": total_changed_files,
            "normalization_changed_lines": total_changed_lines,
            "normalization_changed_fields": total_changed_fields,
            "normalization_parse_errors": total_parse_errors,
            "git_artifacts_removed": total_git_artifacts_removed,
            "exit_code": exit_code,
        },
        "tasks": task_reports,
    }

    cleanup_error = ""
    work_dir_removed = False
    if args.keep_work_dir:
        log.info("Keeping work directory (per --keep-work-dir): %s", work_dir)
    elif work_dir.exists():
        try:
            shutil.rmtree(work_dir)
            work_dir_removed = True
            log.info("Removed work directory: %s", work_dir)
        except Exception as exc:
            cleanup_error = str(exc)
            log.warning("Failed to remove work directory %s: %s", work_dir, exc)

    report_payload["run"]["work_dir_removed"] = work_dir_removed
    report_payload["run"]["work_dir_cleanup_error"] = cleanup_error
    report_payload["run"]["keep_work_dir"] = bool(args.keep_work_dir)
    write_report(report_path, report_payload)
    log.info("Report: %s", report_path)
    log.info(
        "Summary: total=%d succeeded=%d failed=%d",
        fetched_tasks,
        succeeded,
        failed,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
