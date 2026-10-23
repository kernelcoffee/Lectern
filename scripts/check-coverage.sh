#!/usr/bin/env bash
# Coverage gate for a change: lines added or modified relative to a base ref
# must be covered at least FAIL_UNDER percent.
#
#   scripts/check-coverage.sh [base-ref] [fail-under]     # default origin/main 95
#
# Backend: pytest-cov over backend/lectern, judged by diff-cover.
# Frontend: vitest v8 coverage over the logic modules (src/lib, src/hooks,
# src/api — the dirs vitest can see; .tsx components are covered by Playwright,
# which produces no coverage), judged by diff-cover.
set -uo pipefail
BASE="${1:-origin/main}"
MIN="${2:-95}"
cd "$(dirname "$0")/.."
status=0

echo "== backend: pytest --cov (diff vs $BASE, fail-under $MIN%)"
backend/.venv/bin/python -m pytest -q backend/tests \
  --cov=backend/lectern --cov-report=xml:backend/coverage.xml --cov-report= \
  | tail -n 3
backend/.venv/bin/diff-cover backend/coverage.xml --compare-branch="$BASE" \
  --fail-under="$MIN" --src-root=. || status=1

echo
echo "== frontend: vitest --coverage (diff vs $BASE, fail-under $MIN%)"
npm --prefix frontend run -s test:coverage >/dev/null 2>&1 || { echo "vitest coverage run failed"; status=1; }
if [ -f frontend/coverage/cobertura-coverage.xml ]; then
  # One --include with all patterns: a repeated flag keeps only the last one.
  backend/.venv/bin/diff-cover frontend/coverage/cobertura-coverage.xml --compare-branch="$BASE" \
    --fail-under="$MIN" --src-root=frontend \
    --include 'frontend/src/lib/**' 'frontend/src/hooks/**' 'frontend/src/api/**' \
    || status=1
else
  echo "no frontend coverage report produced"; status=1
fi
exit $status
