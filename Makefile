# Family Assistant — convenience targets.
#
# Tests are deliberately NOT auto-discovered by bare `pytest` from the
# repo root (no `testpaths` in pyproject); use these explicit targets
# so integration tests only run when asked.

.PHONY: test test-integration test-integration-fast install-test

install-test:
	uv sync --group test

# Preferred entry point: forces FA_DB_NAME=family_assistant_test and
# scrubs FA_STORAGE_ROOT before pytest sees them, so a stray env var
# in your shell can't accidentally route the suite at the live DB.
# See scripts/run_tests.sh for the full safety rationale.
test:
	./scripts/run_tests.sh

# Run the full integration suite. Idempotent: bootstraps the
# `family_assistant_test` Postgres database on first run (or surfaces
# a clear error with the one-time fix command if the configured DB
# user lacks CREATEDB). Routes through run_tests.sh so the same
# triple-lock safety applies.
test-integration:
	./scripts/run_tests.sh -v

# Same suite but without per-test verbose output — handy in a
# tight refactor loop where you only care about the summary.
test-integration-fast:
	./scripts/run_tests.sh
