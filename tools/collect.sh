#!/usr/bin/env bash
# Offline only: run sqlglot's test suite (pinned clone in sqlglot-src/) under
# coverage, inside the same base image the scorer uses, and write raw results
# to tools/out/ (git-ignored). Then run: python3 tools/build_map.py
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
IMAGE="python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"

test -d sqlglot-src/sqlglot || { echo "clone sqlglot into sqlglot-src/ first" >&2; exit 1; }
mkdir -p tools/out

docker run --rm --platform linux/amd64 \
  -v "$ROOT/sqlglot-src:/src" \
  -v "$ROOT/tools:/tools:ro" \
  -v "$ROOT/tools/out:/out" \
  -v "$ROOT/requirements.lock:/requirements.lock:ro" \
  -w /src \
  -e SKIP_INTEGRATION=0 -e PYTHONPATH=/src:/tools -e OUT=/out -e SRC=/src \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" bash -c '
    set -e
    pip install -q --root-user-action=ignore -r /requirements.lock coverage==7.10.6
    rm -f /out/.coverage*
    python -m pytest tests -p cov_plugin -p no:cacheprovider -o timeout=0 -q \
      --junitxml=/out/junit.xml 2>&1 | tail -30 || true
  '
ls -la tools/out
