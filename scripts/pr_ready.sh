#!/usr/bin/env bash
# Run before opening a PR: refuses to run on main, then lints and tests.
set -euo pipefail
cd "$(dirname "$0")/.."

branch="$(git branch --show-current)"
if [[ "$branch" == "main" || "$branch" == "master" ]]; then
  echo "On $branch: switch to a feature branch first." >&2
  exit 1
fi

make check
