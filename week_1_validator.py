from pathlib import Path
import sys
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


ALLOWED_PROJECT_TYPES = {
    "web",
    "server",
    "fullstack",
    "android",
    "ios",
    "desktop",
}


def validate_structure():
    root = get_validation_root()

    if not root:
        print(
            f"{Colors.FAIL}Error: Could not automatically detect project root.{Colors.ENDC}"
        )
        print("Ensure you are inside the TASK folder (containing metadata.json).")
        sys.exit(1)
        return False

    print(
        f"{Colors.HEADER}🚀 Validating Week 1 Structure for: {Colors.BOLD}{root.name}/{Colors.ENDC}\n"
    )

    mandatory_dirs = ["docs", "repo", ".tmp"]
    mandatory_files = [
        "docs/design.md",
        "docs/questions.md",
        "metadata.json",
        "repo/docker-compose.yml",
        "repo/README.md",
    ]

    errors = []

    for d in mandatory_dirs:
        if (root / d).is_dir():
            print(f"{Colors.OKGREEN} [✓] Directory: {d}/{Colors.ENDC}")
        else:
            errors.append(f"Missing directory: {d}/")
            print(f"{Colors.FAIL} [✗] Missing directory: {d}/{Colors.ENDC}")

    for f in mandatory_files:
        if (root / f).is_file():
            print(f"{Colors.OKGREEN} [✓] File: {f}{Colors.ENDC}")
        else:
            errors.append(f"Missing file: {f}")
            print(f"{Colors.FAIL} [✗] Missing file: {f}{Colors.ENDC}")

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
                project_type = metadata.get("project_type")

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

                if "project_type" not in missing_fields:
                    if (
                        not isinstance(project_type, str)
                        or project_type not in ALLOWED_PROJECT_TYPES
                    ):
                        errors.append(
                            "metadata.json project_type must be one of: "
                            + ", ".join(sorted(ALLOWED_PROJECT_TYPES))
                        )
                        print(
                            f"{Colors.FAIL} [✗] metadata.json project_type must be one of: {', '.join(sorted(ALLOWED_PROJECT_TYPES))}{Colors.ENDC}"
                        )

                if (
                    not missing_fields
                    and not extra_fields
                    and isinstance(project_type, str)
                    and project_type in ALLOWED_PROJECT_TYPES
                ):
                    print(f"{Colors.OKGREEN} [✓] metadata.json is valid{Colors.ENDC}")

    allowed_root_entries = {
        "docs",
        "repo",
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

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for entry in docs_dir.iterdir():
            if entry.is_dir() or entry.suffix.lower() != ".md":
                entry_type = "directory" if entry.is_dir() else "file"
                errors.append(f"Invalid {entry_type} in docs/: {entry.name}")
                print(
                    f"{Colors.FAIL} [✗] Invalid {entry_type} in docs/: {entry.name}{Colors.ENDC}"
                )

    # Week 1 .tmp policy: five markdown files total, including the required
    # test_coverage_and_readme_audit_report.md file, and nothing else.
    tmp_dir = root / ".tmp"
    if tmp_dir.is_dir():
        tmp_entries = list(tmp_dir.iterdir())
        tmp_dirs = [e for e in tmp_entries if e.is_dir()]
        tmp_files = [e for e in tmp_entries if e.is_file()]
        md_files = [e for e in tmp_files if e.suffix.lower() == ".md"]
        required_report_file = "test_coverage_and_readme_audit_report.md"

        if tmp_dirs:
            for entry in tmp_dirs:
                errors.append(f"Invalid directory in .tmp/: {entry.name}")
                print(
                    f"{Colors.FAIL} [✗] Invalid directory in .tmp/: {entry.name}{Colors.ENDC}"
                )

        non_md_files = [e for e in tmp_files if e.suffix.lower() != ".md"]
        if non_md_files:
            for entry in non_md_files:
                errors.append(f"Invalid file in .tmp/: {entry.name}")
                print(f"{Colors.FAIL} [✗] Invalid file in .tmp/: {entry.name}{Colors.ENDC}")

        required_report_matches = [
            e for e in md_files if e.name == required_report_file
        ]
        if not required_report_matches:
            errors.append(f"Missing required file in .tmp/: {required_report_file}")
            print(
                f"{Colors.FAIL} [✗] Missing required file in .tmp/: {required_report_file}{Colors.ENDC}"
            )

        if len(md_files) != 5:
            errors.append(
                f".tmp must contain exactly 5 .md report files, found {len(md_files)}"
            )
            print(
                f"{Colors.FAIL} [✗] .tmp must contain exactly 5 .md report files, found {len(md_files)}{Colors.ENDC}"
            )

        if len(tmp_entries) != 5:
            errors.append(
                f".tmp must contain only 5 .md files and nothing else, found {len(tmp_entries)} entries"
            )
            print(
                f"{Colors.FAIL} [✗] .tmp must contain only 5 .md files and nothing else, found {len(tmp_entries)} entries{Colors.ENDC}"
            )

        if (
            len(md_files) == 5
            and len(tmp_entries) == 5
            and not tmp_dirs
            and not non_md_files
            and required_report_matches
        ):
            print(
                f"{Colors.OKGREEN} [✓] .tmp contains the required audit report plus four additional .md files with no extra entries{Colors.ENDC}"
            )

    test_options = ["repo/run_test.sh", "repo/run_tests.sh"]
    found_test = next((opt for opt in test_options if (root / opt).is_file()), None)

    if found_test:
        print(f"{Colors.OKGREEN} [✓] Test script: {found_test}{Colors.ENDC}")
    else:
        errors.append("Missing test script (run_test.sh or run_tests.sh)")
        print(f"{Colors.FAIL} [✗] Missing test script{Colors.ENDC}")

    for entry in root.rglob("*"):
        if is_temp_entry(entry):
            entry_type = "directory" if entry.is_dir() else "file"
            relative_path = entry.relative_to(root)
            errors.append(f"Temporary {entry_type} found: {relative_path}")
            print(
                f"{Colors.FAIL} [✗] Temporary {entry_type} found: {relative_path}{Colors.ENDC}"
            )

    print("\n" + "─" * 45)
    if not errors:
        print(
            f"{Colors.BOLD}{Colors.OKGREEN}PASSED: Week 1 structure is strictly compliant.{Colors.ENDC}"
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
