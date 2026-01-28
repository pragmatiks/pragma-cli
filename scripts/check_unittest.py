#!/usr/bin/env python3
"""Pre-commit hook to prevent unittest.mock usage in test files.

This script checks staged test files for forbidden unittest.mock imports,
enforcing the project's policy to use pytest-mock exclusively.
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

        # Patterns to detect unittest usage
        patterns = [
            r"^\s*from\s+unittest",  # from unittest ...
            r"^\s*import\s+unittest",  # import unittest
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


def main(file_paths: list[str]) -> int:
    """Check provided test files for unittest usage.

    Args:
        file_paths: List of file paths provided by pre-commit.

    Returns:
        0 if no violations found, 1 otherwise.
    """
    if not file_paths:
        return 0

    # Filter for test files only
    test_files = [Path(p) for p in file_paths if "tests" in Path(p).parts]

    if not test_files:
        return 0

    found_violations = False

    for file_path in test_files:
        violations = check_file_for_unittest(file_path)

        if violations:
            found_violations = True
            print(f"\n❌ Found unittest usage in {file_path}:")
            for line_num, line_content in violations:
                print(f"  Line {line_num}: {line_content}")

    if found_violations:
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
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
