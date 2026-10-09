# Contributing

## Development

```bash
git clone https://github.com/NeLy-EPFL/octacam && cd octacam
uv sync
uv run pytest                                          # warnings are errors
uv run ruff check && uv run ruff format --check
uv run pyrefly check
uv run --group frontend pytest tests/test_frontend.py  # browser GUI tests, on their own
uv run --group docs mkdocs serve                       # the docs site, live at http://127.0.0.1:8000
```

The `fake` camera backend runs the whole suite without hardware. [CLAUDE.md](CLAUDE.md) holds the detailed notes on the recording pipeline, camera backends, triggers and releases. CI runs the same checks on every push and pull request.

## Conventions shared with deeperfly, octacam and spintrack

The three tools are each installed on their own with `uv tool install`, and a user of one should find the others familiar. Keep these conventions the same across the repositories: change them in all three or not at all.

**Command line.** Typer, with Markdown help (`rich_markup_mode="markdown"`), no shell completion, and `-h` as well as `--help`.

- The root takes only `--version`. Every command takes `-v`/`--verbose`: debug logging and a full traceback; without it an error is one line. Exit codes: 0 success, 1 failure, 2 usage error, 130 on Ctrl-C.
- Positional arguments are what the command works on: videos, projects, recordings, a rig directory. A settings file is `-c`/`--config`. `--set KEY=VALUE`, repeatable, overrides one config key by its dotted path: the value is read as TOML, a list may drop its brackets, `none` restores the default, and an unknown key is an error that names it. Named shortcuts (`--device`, `--fps`) cover the keys changed every day.
- `-o`/`--out` says where outputs go, `--force` replaces existing ones, and `--dry-run` checks without running.
- A command that serves a page takes `--host` (default 127.0.0.1), `--port` (the next free port when it is taken) and `--no-browser`. The page a long-running command serves while it works is its live view; `--no-live` turns it off.
- `doctor` reports what the installation can do, `--json` for scripts and `--check` to fail on warnings too. It never opens hardware or downloads anything.
- `docs/cli.md` is generated from the app, and a test fails when it is stale.

**Documentation.** MkDocs with Material: the same theme block (light and dark, indigo, the same features), mkdocstrings for a Python API, versions published with mike to GitHub Pages by `.github/workflows/docs.yml`. The nav follows Home, Installation, Quickstart, Guide, Configuration, Output, CLI reference, API, Citing and credits, and `mkdocs build --strict` must pass. READMEs are short and share one outline, getting started first: header and badges, a one-to-two sentence pitch, Installation, Quickstart, Documentation, Citation, Acknowledgments, Use of AI, License. Anything longer lives in the docs, and development notes in CONTRIBUTING.md.

**Toolchain.** uv with a committed `uv.lock`; ruff (88 columns, comments and docstrings included; `E501`, `I`, `UP`, `B` and `D` with Google docstrings); pyrefly with zero errors; pytest with `-q` and warnings as errors. FastAPI and `uvicorn[standard]` serve pages, pydantic models configs and documents, polars holds tables, Typer builds the command line. Development runs Python 3.14 (`.python-version`), and `requires-python` is capped below the next minor version so `uv tool install` picks a Python every dependency has wheels for. Dependencies track their latest releases: raise the floors and run `uv lock --upgrade` together. CI runs `uv lock --check`, ruff, an ASCII check, pyrefly and pytest.

**Style.** American English. ASCII only in `.py` files; a glyph users see is written as a `\N{...}` escape. Docstrings in Markdown with single backticks and Google sections. Commit subjects in the imperative mood, without `feat:`-style prefixes.
