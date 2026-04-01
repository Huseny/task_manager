import sys
from pathlib import Path


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

    # Scenario 1: Already in the root (metadata.json is here)
    if (cwd / "metadata.json").exists():
        return cwd

    # Scenario 2: Parent folder (should only contain one task folder)
    subdirs = [d for d in cwd.iterdir() if d.is_dir() and not d.name.startswith(".")]

    if len(subdirs) == 1:
        # Check if the single subdirectory looks like the root
        if (subdirs[0] / "metadata.json").exists():
            return subdirs[0]

    return None


def validate_structure():
    root = get_validation_root()

    if not root:
        print(
            f"{Colors.FAIL}Error: Could not automatically detect project root.{Colors.ENDC}"
        )
        print(
            "Ensure you are either inside the TASK folder or in a directory containing ONLY the TASK folder."
        )
        sys.exit(1)

    print(
        f"{Colors.HEADER}🚀 Validation Root Identified: {Colors.BOLD}{root.name}/{Colors.ENDC}\n"
    )

    # Configuration of strict requirements
    mandatory_dirs = ["docs", "repo", "sessions"]
    mandatory_files = [
        "docs/design.md",
        "docs/api-spec.md",
        "docs/questions.md",
        "metadata.json",
        "docker-compose.yml",
    ]

    errors = []

    # 1. Validate Directories
    for d in mandatory_dirs:
        if (root / d).is_dir():
            print(f"{Colors.OKGREEN} [✓] Directory: {d}{Colors.ENDC}")
        else:
            errors.append(f"Missing directory: {d}/")
            print(f"{Colors.FAIL} [✗] Missing directory: {d}{Colors.ENDC}")

    # 2. Validate Files
    for f in mandatory_files:
        if (root / f).is_file():
            print(f"{Colors.OKGREEN} [✓] File: {f}{Colors.ENDC}")
        else:
            errors.append(f"Missing file: {f}")
            print(f"{Colors.FAIL} [✗] Missing file: {f}{Colors.ENDC}")

    # 3. Validate Test Script (run_test.sh OR run_tests.sh)
    test_options = ["run_test.sh", "run_tests.sh"]
    found_test = next((opt for opt in test_options if (root / opt).is_file()), None)

    if found_test:
        print(f"{Colors.OKGREEN} [✓] Test script: {found_test}{Colors.ENDC}")
    else:
        errors.append("Missing test script (run_test.sh or run_tests.sh)")
        print(f"{Colors.FAIL} [✗] Missing test script{Colors.ENDC}")

    # Results
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
