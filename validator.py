from pathlib import Path
import sys
import re
import json


class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def get_validation_root():
    """Detects whether we are inside the task folder or one level above."""
    cwd = Path.cwd()

    if (cwd / "metadata.json").exists():
        return cwd

    subdirs = [d for d in cwd.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if len(subdirs) == 1:
        if (subdirs[0] / "metadata.json").exists():
            return subdirs[0]

    return None


def is_temp_entry(path: Path) -> bool:
    """Return True for common temporary files/folders."""
    name = path.name
    lower_name = name.lower()

    temp_exact_names = {
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
    temp_suffixes = {".temp", ".swp", ".swo", ".bak", ".old", ".orig", ".rej"}

    if lower_name in temp_exact_names:
        return True

    if lower_name.endswith("~") or lower_name.startswith("~$"):
        return True

    if any(lower_name.endswith(suffix) for suffix in temp_suffixes):
        return True

    if lower_name.startswith("tmp-") or lower_name.startswith("temp-"):
        return True

    if lower_name.endswith("-tmp") or lower_name.endswith("-temp"):
        return True

    return False


def validate_structure():
    root = get_validation_root()

    if not root:
        print(
            f"{Colors.FAIL}Error: Could not automatically detect project root.{Colors.ENDC}"
        )
        print("Ensure you are inside the TASK folder (containing metadata.json).")
        return False

    print(
        f"{Colors.HEADER}🚀 Validating Structure for: {Colors.BOLD}{root.name}/{Colors.ENDC}\n"
    )

    # 1. Mandatory Directories
    mandatory_dirs = ["docs", "repo", "sessions", ".tmp"]

    # 2. Mandatory Files (Strictly from image + requirements)
    mandatory_files = [
        "docs/design.md",
        "docs/questions.md",
        "metadata.json",
        "repo/docker-compose.yml",
        "repo/README.md",
    ]

    errors = []

    # Validate Directories
    for d in mandatory_dirs:
        if (root / d).is_dir():
            print(f"{Colors.OKGREEN} [✓] Directory: {d}/{Colors.ENDC}")
        else:
            errors.append(f"Missing directory: {d}/")
            print(f"{Colors.FAIL} [✗] Missing directory: {d}/{Colors.ENDC}")

    # Validate Files
    for f in mandatory_files:
        if (root / f).is_file():
            print(f"{Colors.OKGREEN} [✓] File: {f}{Colors.ENDC}")
        else:
            errors.append(f"Missing file: {f}")
            print(f"{Colors.FAIL} [✗] Missing file: {f}{Colors.ENDC}")

    # Validate metadata.json contents
    metadata_path = root / "metadata.json"
    if metadata_path.is_file():
        required_metadata_fields = {
            "prompt",
            "project_type",
            "frontend_language",
            "backend_language",
            "frontend_framework",
            "backend_framework",
            "database",
        }

        try:
            with metadata_path.open("r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON in metadata.json: {exc.msg}")
            print(f"{Colors.FAIL} [✗] Invalid JSON in metadata.json{Colors.ENDC}")
        else:
            if not isinstance(metadata, dict):
                errors.append("metadata.json must contain a JSON object")
                print(
                    f"{Colors.FAIL} [✗] metadata.json must contain a JSON object{Colors.ENDC}"
                )
            else:
                missing_fields = sorted(required_metadata_fields - metadata.keys())
                extra_fields = sorted(metadata.keys() - required_metadata_fields)

                if missing_fields:
                    errors.append(
                        "metadata.json is missing required fields: "
                        + ", ".join(missing_fields)
                    )
                    print(
                        f"{Colors.FAIL} [✗] metadata.json is missing required fields: {', '.join(missing_fields)}{Colors.ENDC}"
                    )

                if extra_fields:
                    errors.append(
                        "metadata.json has unexpected fields: "
                        + ", ".join(extra_fields)
                    )
                    print(
                        f"{Colors.FAIL} [✗] metadata.json has unexpected fields: {', '.join(extra_fields)}{Colors.ENDC}"
                    )

                if not missing_fields and not extra_fields:
                    print(f"{Colors.OKGREEN} [✓] metadata.json is valid{Colors.ENDC}")

    # 3. Validate strict root entries
    allowed_root_entries = {
        "docs",
        "repo",
        "sessions",
        ".tmp",
        "metadata.json",
        ".git",
        ".gitignore",
        ".github",
    }
    for entry in root.iterdir():
        if entry.name not in allowed_root_entries:
            entry_type = "directory" if entry.is_dir() else "file"
            errors.append(f"Unexpected {entry_type} in root: {entry.name}")
            print(
                f"{Colors.FAIL} [✗] Unexpected {entry_type} in root: {entry.name}{Colors.ENDC}"
            )

    # 4. Validate docs contains only markdown files
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for entry in docs_dir.iterdir():
            if entry.is_dir() or entry.suffix.lower() != ".md":
                entry_type = "directory" if entry.is_dir() else "file"
                errors.append(f"Invalid {entry_type} in docs/: {entry.name}")
                print(
                    f"{Colors.FAIL} [✗] Invalid {entry_type} in docs/: {entry.name}{Colors.ENDC}"
                )

    # 5. Validate Session Files
    sessions_dir = root / "sessions"
    if sessions_dir.is_dir():
        develop_re = re.compile(r"^develop-\d+\.json$")
        bugfix_re = re.compile(r"^bugfix-\d+\.json$")

        develop_files = [
            p
            for p in sessions_dir.iterdir()
            if p.is_file() and develop_re.match(p.name)
        ]
        bugfix_files = [
            p for p in sessions_dir.iterdir() if p.is_file() and bugfix_re.match(p.name)
        ]

        if len(develop_files) < 1:
            errors.append("Missing session trace (sessions/develop-N.json)")
            print(f"{Colors.FAIL} [✗] Missing session trace in sessions/{Colors.ENDC}")
        else:
            print(
                f"{Colors.OKGREEN} [✓] develop-N.json files found: {len(develop_files)}{Colors.ENDC}"
            )

        if len(bugfix_files) < 2:
            errors.append("Expected at least 2 bugfix traces (sessions/bugfix-N.json)")
            print(
                f"{Colors.FAIL} [✗] Expected at least 2 bugfix traces in sessions/, found {len(bugfix_files)}{Colors.ENDC}"
            )
        else:
            print(
                f"{Colors.OKGREEN} [✓] bugfix-N.json files found: {len(bugfix_files)}{Colors.ENDC}"
            )

    # 6. Validate .tmp strictly: exactly 4 files, only allowed names,
    # and mandatory pairs of audit_report-N.md + audit_report-N-fix_check.md
    tmp_dir = root / ".tmp"
    if tmp_dir.is_dir():
        tmp_entries = list(tmp_dir.iterdir())
        tmp_files = [e for e in tmp_entries if e.is_file()]
        tmp_dirs = [e for e in tmp_entries if e.is_dir()]

        if tmp_dirs:
            for entry in tmp_dirs:
                errors.append(f"Invalid directory in .tmp/: {entry.name}")
                print(
                    f"{Colors.FAIL} [✗] Invalid directory in .tmp/: {entry.name}{Colors.ENDC}"
                )

        audit_re = re.compile(r"^audit_report-(\d+)\.md$")
        fix_re = re.compile(r"^audit_report-(\d+)-fix_check\.md$")

        audit_nums = []
        fix_nums = []

        for file in tmp_files:
            name = file.name
            m_audit = audit_re.match(name)
            m_fix = fix_re.match(name)

            if m_fix:
                fix_nums.append(m_fix.group(1))
            elif m_audit:
                audit_nums.append(m_audit.group(1))
            else:
                errors.append(f"Invalid file in .tmp/: {name}")
                print(f"{Colors.FAIL} [✗] Invalid file in .tmp/: {name}{Colors.ENDC}")

        if len(tmp_files) != 4:
            errors.append(f".tmp must contain exactly 4 files, found {len(tmp_files)}")
            print(
                f"{Colors.FAIL} [✗] .tmp must contain exactly 4 files, found {len(tmp_files)}{Colors.ENDC}"
            )

        if len(audit_nums) != 2:
            errors.append(
                f".tmp must contain exactly 2 audit reports (audit_report-N.md), found {len(audit_nums)}"
            )
            print(
                f"{Colors.FAIL} [✗] Expected 2 audit reports, found {len(audit_nums)}{Colors.ENDC}"
            )

        if len(fix_nums) != 2:
            errors.append(
                f".tmp must contain exactly 2 fix-check reports (audit_report-N-fix_check.md), found {len(fix_nums)}"
            )
            print(
                f"{Colors.FAIL} [✗] Expected 2 fix-check reports, found {len(fix_nums)}{Colors.ENDC}"
            )

        if sorted(audit_nums) != sorted(fix_nums):
            errors.append(
                ".tmp audit report numbers must match fix-check report numbers"
            )
            print(
                f"{Colors.FAIL} [✗] audit_report-N.md and audit_report-N-fix_check.md numbers do not match{Colors.ENDC}"
            )
        elif len(audit_nums) == 2 and len(fix_nums) == 2:
            print(
                f"{Colors.OKGREEN} [✓] .tmp contains required 4 audit files with matching report numbers{Colors.ENDC}"
            )

    # 7. Validate Test Script (Strict check for run_test.sh OR run_tests.sh)
    test_options = ["repo/run_test.sh", "repo/run_tests.sh"]
    found_test = next((opt for opt in test_options if (root / opt).is_file()), None)

    if found_test:
        print(f"{Colors.OKGREEN} [✓] Test script: {found_test}{Colors.ENDC}")
    else:
        errors.append("Missing test script (run_test.sh or run_tests.sh)")
        print(f"{Colors.FAIL} [✗] Missing test script{Colors.ENDC}")

    # 8. Validate no temporary files/folders anywhere in project
    for entry in root.rglob("*"):
        if is_temp_entry(entry):
            entry_type = "directory" if entry.is_dir() else "file"
            relative_path = entry.relative_to(root)
            errors.append(f"Temporary {entry_type} found: {relative_path}")
            print(
                f"{Colors.FAIL} [✗] Temporary {entry_type} found: {relative_path}{Colors.ENDC}"
            )

    # Final Results
    print("\n" + "─" * 45)
    if not errors:
        print(
            f"{Colors.BOLD}{Colors.OKGREEN}PASSED: Structure is strictly compliant.{Colors.ENDC}"
        )
        sys.exit(0)
    else:
        print(
            f"{Colors.BOLD}{Colors.FAIL}FAILED: {len(errors)} structural issues found.{Colors.ENDC}"
        )
        for error in errors:
            print(f"  • {error}")
        sys.exit(1)


if __name__ == "__main__":
    validate_structure()
