#!/usr/bin/env zsh
# macOS-friendly wrapper that runs the test suite inside a Docker container.
# Usage: ./bin/run-tests.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${(%):-%x}")/.." && pwd)"
cd "$ROOT"

if command -v docker-compose >/dev/null 2>&1; then
  docker-compose build tests
  docker-compose run --rm tests
else
  docker build -t revision-finder-tests:latest .
  docker run --rm revision-finder-tests:latest
fi
