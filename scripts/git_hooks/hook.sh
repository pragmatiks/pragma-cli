#!/bin/sh
# pragmatiks-git-hook
hook_type=$(basename "$0")

if [ ! -f .pre-commit-config.yaml ]; then
  exit 0
fi

if ! command -v mise >/dev/null 2>&1; then
  echo "The $hook_type hook needs mise on PATH to run pre-commit; install mise, then run \`mise run install\` in $(pwd)." >&2
  exit 1
fi

if [ "$hook_type" = commit-msg ]; then
  exec mise exec -- pre-commit run --hook-stage commit-msg --commit-msg-filename "$1"
fi

exec mise exec -- pre-commit run --hook-stage "$hook_type"
