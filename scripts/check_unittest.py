#!/usr/bin/env python3
"""Pre-commit hook to enforce test conventions.

Checks for:
1. unittest.mock usage (should use pytest-mock)
2. Test* class definitions (should use standalone functions)
"""

import re
import sys
from pathlib import Path


def check_file_for_unittest(file_path: Path) -> list[tuple[int, str]]:
    """Check a file for unittest module usage.

    Args:
        file_path: Path to the test file to check.

    Returns:
        List of (line_number, line_content) tuples where unittest was found.
    """
    violations = []

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        patterns = [
            r"^\s*from\s+unittest",
            r"^\s*import\s+unittest",
        ]

        for line_num, line in enumerate(lines, start=1):
            for pattern in patterns:
                if re.search(pattern, line):
                    violations.append((line_num, line.strip()))
                    break

    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return []

    return violations


def check_file_for_test_classes(file_path: Path) -> list[tuple[int, str]]:
    """Check a file for Test* class definitions.

    Args:
        file_path: Path to the test file to check.

    Returns:
        List of (line_number, line_content) tuples where Test classes were found.
    """
    violations = []

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        pattern = r"^class\s+Test\w*"

        for line_num, line in enumerate(lines, start=1):
            if re.search(pattern, line):
                violations.append((line_num, line.strip()))

    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        return []

    return violations


def main(file_paths: list[str]) -> int:
    """Check provided test files for policy violations.

    Args:
        file_paths: List of file paths provided by pre-commit.

    Returns:
        0 if no violations found, 1 otherwise.
    """
    if not file_paths:
        return 0

    test_files = [Path(p) for p in file_paths if "tests" in Path(p).parts]

    if not test_files:
        return 0

    unittest_violations = {}
    class_violations = {}

    for file_path in test_files:
        unittest_results = check_file_for_unittest(file_path)
        if unittest_results:
            unittest_violations[file_path] = unittest_results

        class_results = check_file_for_test_classes(file_path)
        if class_results:
            class_violations[file_path] = class_results

    if unittest_violations:
        for file_path, violations in unittest_violations.items():
            print(f"\n❌ Found unittest usage in {file_path}:")
            for line_num, line_content in violations:
                print(f"  Line {line_num}: {line_content}")

        print(
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "POLICY VIOLATION: unittest.mock usage is forbidden\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "This project uses pytest-mock exclusively for better test isolation\n"
            "and automatic cleanup. Please replace unittest.mock with pytest-mock:\n"
            "\n"
            "  ❌ WRONG:\n"
            "     from unittest.mock import Mock, patch\n"
            "\n"
            "  ✅ CORRECT:\n"
            "     from pytest_mock import MockerFixture\n"
            "     def test_example(mocker: MockerFixture):\n"
            "         mock_obj = mocker.Mock()\n"
            "         mock_func = mocker.patch('module.function')\n"
            "\n"
        )

    if class_violations:
        for file_path, violations in class_violations.items():
            print(f"\n❌ Found Test class in {file_path}:")
            for line_num, line_content in violations:
                print(f"  Line {line_num}: {line_content}")

        print(
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "POLICY VIOLATION: Test classes are forbidden\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "This project uses standalone test functions, not test classes.\n"
            "Classes add unnecessary indentation and make fixtures harder to use.\n"
            "\n"
            "  ❌ WRONG:\n"
            "     class TestUserService:\n"
            "         def test_create_user(self, mock_db):\n"
            "             ...\n"
            "\n"
            "  ✅ CORRECT:\n"
            "     def test_user_service_create_user(mock_db):\n"
            "         ...\n"
            "\n"
        )

    if unittest_violations or class_violations:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
