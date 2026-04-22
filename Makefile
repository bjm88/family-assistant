# Family Assistant — convenience targets.
#
# Tests are deliberately NOT auto-discovered by bare `pytest` from the
# repo root (no `testpaths` in pyproject); use these explicit targets
# so integration tests only run when asked.

.PHONY: test-integration test-integration-fast install-test

install-test:
	uv sync --group test

# Run the full integration suite. Idempotent: bootstraps the
# `family_assistant_test` Postgres database on first run (or surfaces
# a clear error with the one-time fix command if the configured DB
# user lacks CREATEDB).
test-integration:
	uv run pytest tests/integration -v

# Same suite but without per-test verbose output — handy in a
# tight refactor loop where you only care about the summary.
test-integration-fast:
	uv run pytest tests/integration
