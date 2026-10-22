#!/usr/bin/env bash
set -euo pipefail

hooks_directory=$(git rev-parse --path-format=absolute --git-path hooks)
expected_hooks_directory="$(git rev-parse --path-format=absolute --git-common-dir)/hooks"

if [ "$hooks_directory" != "$expected_hooks_directory" ]; then
  echo "Git reads hooks from $hooks_directory (core.hooksPath), but pre-commit installs into $expected_hooks_directory." >&2
  echo "Point core.hooksPath at $expected_hooks_directory or unset it." >&2
  exit 1
fi

GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0= pre-commit install --hook-type pre-commit --hook-type commit-msg
