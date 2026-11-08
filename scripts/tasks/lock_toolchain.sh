#!/usr/bin/env bash
set -euo pipefail

pin=$(sed -n 's/^min_version = "\(.*\)"$/\1/p' mise.toml)
if [ -z "$pin" ]; then
  echo "mise.toml sets no min_version." >&2
  exit 1
fi

installed_version=$(mise --version | cut -d' ' -f1)
if [ "$installed_version" != "$pin" ]; then
  echo "mise is $installed_version, but lockfiles are written with mise $pin. Run 'mise self-update $pin'." >&2
  exit 1
fi

case "$(uname -sm)" in
  "Darwin arm64" | "Linux x86_64") ;;
  *)
    echo "Lockfiles are written on macOS arm64 or Linux x64 only, not on $(uname -sm)." >&2
    exit 1
    ;;
esac

MISE_AUTO_ENV=0 MISE_ENV= mise lock --platform macos-arm64,linux-x64

while IFS= read -r environment_file; do
  grep -q '^\[tools\]' "$environment_file" || continue
  environment=$(sed 's/^mise\.\(.*\)\.toml$/\1/' <<<"$environment_file")
  MISE_AUTO_ENV=0 MISE_ENV=$environment mise plugins install --all
  MISE_AUTO_ENV=0 MISE_ENV=$environment mise lock --platform macos-arm64,linux-x64
done < <(git ls-files -- ':(glob)mise.*.toml')
