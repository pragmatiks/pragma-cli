#!/bin/bash
set -e

# If no files provided, exit cleanly
if [ $# -eq 0 ]; then
  exit 0
fi

FILES=()
for file in "$@"; do
  if [[ "$file" == src/* ]]; then
    FILES+=("$file")
  fi
done

# Exit if no relevant files to check
if [ ${#FILES[@]} -eq 0 ]; then
  exit 0
fi

TEMP_FILE=$(mktemp)
trap "rm -f $TEMP_FILE" EXIT

# Check each file for disallowed comments
for file in "${FILES[@]}"; do
  rg '^\s*#\s+' "$file" 2>/dev/null | \
    grep -vE '#\s*(TODO|FIXME|BUG|HACK|type:|noqa|fmt:|pragma:)' | \
    sed "s|^|$file:|" >> "$TEMP_FILE" || true
done

if [ -s "$TEMP_FILE" ]; then
  cat "$TEMP_FILE"
  echo ""
  echo "❌ POLICY VIOLATION: Disallowed comments found"
  echo ""
  echo "Policy: Code must be self-documenting. Extract methods instead of comments."
  echo "Allowed comment types: # TODO, # FIXME, # BUG, # HACK"
  echo ""
  exit 1
fi
