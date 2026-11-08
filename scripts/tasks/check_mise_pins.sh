#!/usr/bin/env bash
set -euo pipefail

pin=$(sed -n 's/^min_version = "\(.*\)"$/\1/p' mise.toml)
if [ -z "$pin" ]; then
  echo "mise.toml sets no min_version." >&2
  exit 1
fi

installed_version=$(mise --version | cut -d' ' -f1)
if [ "$installed_version" != "$pin" ]; then
  echo "mise is $installed_version, but mise.toml pins min_version $pin." >&2
  exit 1
fi

checksums=$(curl -fsSL "https://github.com/jdx/mise/releases/download/v$pin/SHASUMS256.txt")
linux_sha256=$(awk -v asset="./mise-v$pin-linux-x64" '$2 == asset { print $1 }' <<<"$checksums")
macos_sha256=$(awk -v asset="./mise-v$pin-macos-arm64" '$2 == asset { print $1 }' <<<"$checksums")
if [ -z "$linux_sha256" ] || [ -z "$macos_sha256" ]; then
  echo "SHASUMS256.txt of mise $pin has no linux-x64 or macos-arm64 binary row." >&2
  exit 1
fi

mise_action_steps=$(find .github/workflows -type f \( -name '*.yml' -o -name '*.yaml' \) -exec yq --no-doc '
  .jobs[] as $job
  | $job.steps[]
  | select(.uses == "jdx/mise-action@*")
  | [filename + ":" + (line | tostring), .with.version // "", .with.sha256 // "", $job."runs-on" // ""]
  | @tsv
' {} +)
if [ -z "$mise_action_steps" ]; then
  echo "No jdx/mise-action step found under .github/workflows." >&2
  exit 1
fi

mismatched_steps=$(awk -F '\t' -v pin="$pin" -v linux="$linux_sha256" -v macos="$macos_sha256" '
  { expected = $4 ~ /^ubuntu-/ ? linux : ($4 ~ /^macos-/ ? macos : "") }
  $2 != pin || expected == "" || $3 != expected
' <<<"$mise_action_steps")
if [ -n "$mismatched_steps" ]; then
  echo "Every jdx/mise-action step must set version: $pin (min_version in mise.toml) and sha256: the SHASUMS256.txt row of the plain mise binary for its runner (ubuntu: linux-x64 $linux_sha256, macos: macos-arm64 $macos_sha256). Mismatched steps (file:line, version, sha256, runs-on):" >&2
  printf '%s\n' "$mismatched_steps" >&2
  exit 1
fi
