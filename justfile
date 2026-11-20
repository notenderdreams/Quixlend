default:
    @just --list

# lint + format check + tests
check: lint fmt-check typecheck test

lint:
    uvx ruff check .

fmt-check:
    uvx ruff format --check .

# needs `just setup` once (bpy stubs)
typecheck:
    uvx basedpyright

# auto-fix lint, then format
fix:
    uvx ruff check . --fix
    uvx ruff format .

test:
    python3 -m unittest discover -s tests -t .

# reload the extension in the running Blender (via the Blender MCP socket)
reload:
    python3 scripts/reload.py

# remove ruff cache and __pycache__ dirs
clean:
    rm -rf .ruff_cache
    find . -name __pycache__ -type d -prune -not -path './.git/*' -exec rm -rf {} +

# create .venv with bpy stubs for editor/LSP support (Zed, pyright)
setup:
    uv venv .venv
    uv pip install --python .venv fake-bpy-module numpy

# build extension zip + static-repo index.json into dist/ (needs `blender` on PATH)
release:
    rm -rf dist && mkdir dist
    blender --factory-startup --command extension build --source-dir . --output-dir dist
    blender --factory-startup --command extension server-generate --repo-dir dist
    # stable asset name so one `latest/download/quixlend.zip` link works for drag and drop
    python3 -c "import json,pathlib as p;z=next(p.Path('dist').glob('*.zip'));z.rename('dist/quixlend.zip');f=p.Path('dist/index.json');d=json.loads(f.read_text());d['data'][0]['archive_url']='./quixlend.zip';f.write_text(json.dumps(d,indent=2))"
