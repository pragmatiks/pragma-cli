#!/usr/bin/env bash
set -euo pipefail

uv run --locked ty check ${PRAGMA_SDK_DIRECTORY:+--extra-search-path "$PRAGMA_SDK_DIRECTORY/src"}
