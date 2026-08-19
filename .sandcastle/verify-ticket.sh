#!/usr/bin/env bash
set -euo pipefail

test -f pyproject.toml
test -f uv.lock
test -x scripts/verify

npm ci
npm test

uv lock --check
uv sync --frozen --all-groups

VERIFY_TMP_DIR=$(mktemp -d)
trap 'rm -rf "$VERIFY_TMP_DIR"' EXIT
uv build --out-dir "$VERIFY_TMP_DIR/dist"
uv run python -m pytest

while IFS= read -r PACKAGE_LOCK; do
  PACKAGE_DIR=$(dirname "$PACKAGE_LOCK")
  if [[ "$PACKAGE_DIR" == "." ]]; then
    continue
  fi
  npm ci --prefix "$PACKAGE_DIR"
  npm test --prefix "$PACKAGE_DIR" --if-present
  npm run build --prefix "$PACKAGE_DIR" --if-present
done < <(
  find . \
    -path './.git' -prune -o \
    -path './node_modules' -prune -o \
    -path './.sandcastle' -prune -o \
    -name package-lock.json -print \
    | sort
)

scripts/verify
