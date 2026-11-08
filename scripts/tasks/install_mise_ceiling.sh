#!/usr/bin/env bash
set -euo pipefail

expected_miserc=$(bash "$(dirname "$0")/compute_expected_miserc.sh")
foreign_settings=""

if [ -f .miserc.toml ]; then
  foreign_settings=$(grep -vxE 'ceiling_paths = \["[^"]*"\]' .miserc.toml | grep -v '^[[:space:]]*$' | grep -vxF "$expected_miserc" || true)
fi

if [ -n "$foreign_settings" ]; then
  echo ".miserc.toml holds other settings, so it was left unchanged. Delete .miserc.toml and rerun mise run install, or replace its content with:" >&2
  printf '%s\n' "$expected_miserc" >&2
  exit 1
fi

printf '%s\n' "$expected_miserc" > .miserc.toml
