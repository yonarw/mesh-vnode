.PHONY: check fix

# Everything that should pass before a commit.
check:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run python -m pytest -q
	npm --prefix webui run lint

# Apply what the linters and formatters can fix by themselves.
fix:
	uv run ruff check --fix src tests
	uv run ruff format src tests
	npm --prefix webui run format
