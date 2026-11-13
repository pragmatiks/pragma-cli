#!/usr/bin/env bash
set -euo pipefail

shared_files=(
  scripts/git_hooks/hook.sh
  scripts/tasks/check_mise_pins.sh
  scripts/tasks/check_toolchain_scripts.sh
  scripts/tasks/compute_expected_miserc.sh
  scripts/tasks/install_hooks.sh
  scripts/tasks/install_mise_ceiling.sh
  scripts/tasks/lock_toolchain.sh
)
public_repositories=(pragma-sdk pragma-cli pragma-providers pragma-lint pragma-docs)

download_directory=$(mktemp -d)
trap 'rm -rf "$download_directory"' EXIT

drift=0
for shared_file in "${shared_files[@]}"; do
  [ -f "$shared_file" ] || continue
  for repository in "${public_repositories[@]}"; do
    remote_copy="$download_directory/$repository-${shared_file//\//-}"
    status=404
    for reference in ${GITHUB_HEAD_REF:+"$GITHUB_HEAD_REF"} main; do
      url="https://raw.githubusercontent.com/pragmatiks/$repository/refs/heads/$reference/$shared_file"
      status=$(curl -sS -o "$remote_copy" -w '%{http_code}' "$url") || {
        echo "Fetching $url failed." >&2
        exit 1
      }
      case "$status" in
        200) break ;;
        404) ;;
        *)
          echo "Fetching $url answered HTTP $status." >&2
          exit 1
          ;;
      esac
    done
    [ "$status" = 200 ] || continue
    if ! diff -u --label "$repository/$shared_file ($reference)" --label "$shared_file (local)" "$remote_copy" "$shared_file"; then
      drift=1
    fi
  done
done

if [ "$drift" -ne 0 ]; then
  echo "Shared toolchain scripts differ from the public repositories' copies (the pull request's branch name where it exists, else main). Make every copy byte-identical (docs/runbooks/local-dev-setup.md, Shared toolchain scripts)." >&2
fi
exit "$drift"
