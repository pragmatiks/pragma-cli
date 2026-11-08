#!/usr/bin/env bash
set -euo pipefail

shared_scripts=(check_mise_pins.sh check_toolchain_scripts.sh compute_expected_miserc.sh install_hooks.sh install_mise_ceiling.sh lock_toolchain.sh)
public_repositories=(pragma-sdk pragma-cli pragma-providers pragma-lint pragma-docs)
script_directory=scripts/tasks

download_directory=$(mktemp -d)
trap 'rm -rf "$download_directory"' EXIT

drift=0
for script in "${shared_scripts[@]}"; do
  [ -f "$script_directory/$script" ] || continue
  for repository in "${public_repositories[@]}"; do
    remote_copy="$download_directory/$repository-$script"
    status=404
    for reference in ${GITHUB_HEAD_REF:+"$GITHUB_HEAD_REF"} main; do
      url="https://raw.githubusercontent.com/pragmatiks/$repository/refs/heads/$reference/$script_directory/$script"
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
    if ! diff -u --label "$repository/$script_directory/$script ($reference)" --label "$script_directory/$script (local)" "$remote_copy" "$script_directory/$script"; then
      drift=1
    fi
  done
done

if [ "$drift" -ne 0 ]; then
  echo "Shared toolchain scripts differ from the public repositories' copies (the pull request's branch name where it exists, else main). Make every copy byte-identical (docs/runbooks/local-dev-setup.md, Shared toolchain scripts)." >&2
fi
exit "$drift"
