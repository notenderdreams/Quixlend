default:
    @just --list

# lint + format check + tests
check: lint fmt-check test

lint:
    uvx ruff check .

fmt-check:
    uvx ruff format --check .

# auto-fix lint, then format
fix:
    uvx ruff check . --fix
    uvx ruff format .

test:
    python3 -m unittest discover -s tests -t .
