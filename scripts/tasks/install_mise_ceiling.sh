#!/usr/bin/env bash
set -euo pipefail

primary_checkout=$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd -P)
worktrees_directory="$primary_checkout/.claude/worktrees"
ceiling_line="ceiling_paths = [\"$worktrees_directory\"]"

if [ -f .miserc.toml ] && grep -qF "\"$worktrees_directory\"" .miserc.toml; then
  exit 0
fi

if [ -f .miserc.toml ] && grep -v '^[[:space:]]*$' .miserc.toml | grep -qvx 'ceiling_paths = \["[^"]*"\]'; then
  echo ".miserc.toml holds other settings, so it was left unchanged. Add $worktrees_directory to its ceiling_paths, for example: $ceiling_line" >&2
  exit 1
fi

printf '%s\n' "$ceiling_line" > .miserc.toml
