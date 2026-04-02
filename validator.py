from pathlib import Path
import sys


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
    mandatory_dirs = ["docs", "repo", "sessions"]

    # 2. Mandatory Files (Strictly from image + requirements)
    mandatory_files = [
        "docs/design.md",
        "docs/api-spec.md",
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

    # 3. Validate Session Files (Strict check for develop-N.json)
    sessions_dir = root / "sessions"
    if sessions_dir.is_dir():
        session_files = list(sessions_dir.glob("develop-*.json"))
        if session_files:
            print(
                f"{Colors.OKGREEN} [✓] Session trace found: {session_files[0].name}{Colors.ENDC}"
            )
        else:
            errors.append("Missing session trace (sessions/develop-N.json)")
            print(f"{Colors.FAIL} [✗] Missing session trace in sessions/{Colors.ENDC}")

    # 4. Validate Test Script (Strict check for run_test.sh OR run_tests.sh)
    test_options = ["repo/run_test.sh", "repo/run_tests.sh"]
    found_test = next((opt for opt in test_options if (root / opt).is_file()), None)

    if found_test:
        print(f"{Colors.OKGREEN} [✓] Test script: {found_test}{Colors.ENDC}")
    else:
        errors.append("Missing test script (run_test.sh or run_tests.sh)")
        print(f"{Colors.FAIL} [✗] Missing test script{Colors.ENDC}")

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
