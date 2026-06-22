# Contributing

Thanks for your interest in improving Nalanda.

## Development setup

Nalanda is a Python 3.14 application managed with [uv](https://docs.astral.sh/uv/). There is no
build step; it runs from source as `python -m nalanda`.

```sh
uv sync                 # install dependencies, including the dev tools
uv run pytest           # run the test suite
uv run ruff check       # lint
uv run basedpyright     # type-check
```

## Before opening a pull request

- The three checks above must pass. CI runs the same ones on every PR.
- `config.py` is the single source of truth for the config schema. If you change the config
  models, regenerate the schema with `uv run python -m nalanda schema` and commit the updated
  `config.schema.json`.
- Update `CHANGELOG.md` and the README when behaviour or configuration changes.
- Pull requests are squash-merged, so the PR title becomes the commit subject. Use a
  conventional-commit prefix (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Reporting bugs and requesting features

Use the issue templates. For security issues, see [SECURITY.md](SECURITY.md).
