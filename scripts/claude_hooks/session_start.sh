#!/bin/sh
[ "${CLAUDE_CODE_REMOTE:-}" = true ] || exit 0
cd "$(dirname "$0")/../.." || exit 1

mise run -q install </dev/null >&2 && exit 0

printf '%s\n' '{"systemMessage":"Installing the dependencies or the git hooks failed, so commits may skip the pre-commit and commit-msg checks; run `mise run install` here.","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Installing the dependencies or the git hooks failed, so commits may skip the pre-commit and commit-msg checks; run `mise run install` here."}}'
exit 0
