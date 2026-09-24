MAKEFLAGS += --no-print-directory

.PHONY: sync lint format test-fast test-integration test acceptance sandbox build ci

sync:
	uv sync --all-extras --group dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

test-fast:
	uv run pytest -q tests/unit tests/contract

test-integration:
	uv run pytest -q -m "integration and not acceptance"

test:
	uv run pytest -q -m "not acceptance and not sandbox"

acceptance:
	uv run pytest -q -m "acceptance and not sandbox"

sandbox:
	@test -n "$$INVOICE_SANDBOX_ENV_FILE" || { echo "make sandbox: set INVOICE_SANDBOX_ENV_FILE" >&2; exit 2; }
	uv run pytest -q -m sandbox

build: export SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct)
build:
	@git diff --quiet && git diff --cached --quiet || { \
		echo "make build: tracked files have uncommitted changes; commit or stash them so the build matches HEAD" >&2; \
		exit 1; }
	rm -rf dist
	uv build
	cd dist && shasum -a 256 *.whl *.tar.gz > SHA256SUMS

ci:
	$(MAKE) lint
	$(MAKE) test
