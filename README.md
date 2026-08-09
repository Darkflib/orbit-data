# Orbit Data

`orbit-data` builds and publishes the static data consumed by
[Darkflib/orbit](https://github.com/Darkflib/orbit). It is designed to run as
rootless Podman Quadlets on a Linux host, with scheduled one-shot updater
containers writing to a persistent volume and a separate static web server
mounting the published tree read-only.

The repository is being implemented in stages. The current foundation provides:

- a Python 3.13 package managed with `uv`;
- structured JSON logging;
- TOML configuration;
- atomic static-file and release-directory publication;
- bounded release retention;
- an unprivileged OCI image; and
- CI for formatting, linting, typing, tests, security checks, and image builds.

## Development

```bash
uv sync --locked --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pylint src tests
uv run bandit -c pyproject.toml -r src
```

Initialize a local storage tree with:

```bash
uv run orbit-data --config config/orbit-data.toml init-storage
```

The default production configuration expects the persistent volume at `/data`.

## Container

Build with Podman:

```bash
podman build -t orbit-data:dev .
podman run --rm orbit-data:dev --help
```

Images built from `main` are published to
`ghcr.io/darkflib/orbit-data:latest` and an immutable `sha-<commit>` tag.
