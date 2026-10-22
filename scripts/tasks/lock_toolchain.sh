#!/usr/bin/env bash
set -euo pipefail

pin=$(sed -n 's/^min_version = "\(.*\)"$/\1/p' mise.toml)
if [ -z "$pin" ]; then
  echo "mise.toml sets no min_version." >&2
  exit 1
fi

if [ "$(mise --version | cut -d' ' -f1)" != "$pin" ]; then
  echo "mise must be exactly $pin. Run 'mise self-update $pin'." >&2
  exit 1
fi

case "$(uname -sm)" in
  "Darwin arm64" | "Linux x86_64") ;;
  *)
    echo "Run 'mise run lock:toolchain' on macOS arm64 or Linux x64 only." >&2
    exit 1
    ;;
esac

mise lock --platform macos-arm64,linux-x64
