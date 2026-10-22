#!/usr/bin/env bash
set -euo pipefail

uvx ty check --python .venv ${PRAGMA_SDK_DIRECTORY:+--extra-search-path "$PRAGMA_SDK_DIRECTORY/src"}
