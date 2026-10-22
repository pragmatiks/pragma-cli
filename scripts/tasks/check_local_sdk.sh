#!/usr/bin/env bash
set -euo pipefail

sdk_directory="${PRAGMA_SDK_DIRECTORY:-}"
remedy="Set it to the absolute path of a pragma-sdk checkout, or unset it to use the locked SDK."

if [ -z "$sdk_directory" ]; then
  exit 0
fi

if [ "${sdk_directory#/}" = "$sdk_directory" ]; then
  echo "PRAGMA_SDK_DIRECTORY is '$sdk_directory', a relative path. $remedy" >&2
  exit 1
fi

if [ ! -d "$sdk_directory/src/pragma_sdk" ]; then
  echo "PRAGMA_SDK_DIRECTORY is '$sdk_directory', which holds no src/pragma_sdk. $remedy" >&2
  exit 1
fi

echo "Using the local SDK checkout at $sdk_directory (PRAGMA_SDK_DIRECTORY)."
