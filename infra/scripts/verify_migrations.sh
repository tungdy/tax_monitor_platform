#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/artifacts/acceptance/phase-4"
PYTHON="${PYTHON:-${ROOT_DIR}/backend/.venv/bin/python}"
PYTEST="${PYTEST:-${ROOT_DIR}/backend/.venv/bin/pytest}"
mkdir -p "${OUTPUT_DIR}"

cd "${ROOT_DIR}/backend"
"${PYTEST}" \
  tests/integration/persistence/test_schema.py::test_alembic_check_and_round_trip_stay_in_the_isolated_schema \
  --junitxml="${OUTPUT_DIR}/migrations.xml" -q
"${PYTHON}" -m alembic -c alembic.ini heads > "${OUTPUT_DIR}/migration-head.txt"
EXPECTED_HEAD="$("${PYTHON}" -c 'from tax_risk.config import EXPECTED_MIGRATION_HEAD; print(EXPECTED_MIGRATION_HEAD)')"
grep -Fxq "${EXPECTED_HEAD} (head)" "${OUTPUT_DIR}/migration-head.txt"
"${PYTHON}" -c 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({"empty_database_upgrade": True, "legacy_upgrade": True, "downgrade_reupgrade": True, "migration_head": sys.argv[2]}, ensure_ascii=False, indent=2) + "\n")' "${OUTPUT_DIR}/migrations.json" "${EXPECTED_HEAD}"

test -s "${OUTPUT_DIR}/migrations.xml"
test -s "${OUTPUT_DIR}/migrations.json"
