#!/usr/bin/env bash
set -euo pipefail

primary_checkout=$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd -P)

if [ "$(uname -s)" = Darwin ] && [ -f mise.deploy.toml ]; then
  selected_environments='["deploy"]'
else
  selected_environments='[]'
fi

printf 'ceiling_paths = ["%s/.claude/worktrees"]\nauto_env = true\nenv = %s\n' "$primary_checkout" "$selected_environments"
