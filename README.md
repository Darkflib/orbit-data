# Orbit Data

`orbit-data` builds and publishes the static data consumed by
[Darkflib/orbit](https://github.com/Darkflib/orbit). It is designed to run as
Podman Quadlets on a Linux host, with scheduled one-shot updater
containers writing to a persistent volume and a separate static web server
mounting the published tree read-only.

The repository is being implemented in stages. It currently provides:

- a Python 3.13 package managed with `uv`;
- structured JSON logging;
- TOML configuration;
- atomic static-file and release-directory publication;
- bounded release retention;
- a sequential, allow-listed CelesTrak OMM/JSON cache with a persistent
  two-hour minimum request interval;
- response-size, schema, physical-range, duplicate-ID, record-floor and
  record-drop validation;
- last-known-good behaviour for HTTP, network and validation failures;
- a slow-cadence SATCAT/GCAT enrichment build with vendored magnitude and sky data;
- deterministic catalogue artifacts with conditional source requests and
  atomic last-known-good publication;
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
See [`deploy/README.md`](deploy/README.md) for the Caddy, Quadlet, timer, and
network-volume failover deployment.

Run the due GP queries with:

```bash
uv run orbit-data --config config/orbit-data.toml sync-gp
```

The job writes static OMM files below `/data/public/v1/gp/`, per-dataset status
below `/data/public/v1/status/gp/`, and a run summary at
`/data/public/v1/status/gp.json`. A request attempt is persisted before network
I/O, so restarting the process or moving the volume to another host cannot
accidentally bypass the minimum interval.

`HTTP 403` is treated as an unchanged or rate-limited dataset. Any `5xx`,
redirect, unexpected HTTP response, network failure, oversized body, or invalid
OMM response stops the run without replacing the last-known-good file. There
are no immediate retries. Two consecutive `403` responses also stop the run to
avoid hammering CelesTrak if the host IP has been firewalled.

Run the slow-moving catalogue refresh with:

```bash
uv run orbit-data --config config/orbit-data.toml sync-catalog
```

This fetches CelesTrak SATCAT and Jonathan McDowell's GCAT using conditional
requests, merges them with the vendored magnitude and sky sources documented in
[`SOURCES.md`](SOURCES.md), and publishes a complete tree at
`/data/public/v1/data/`. The tree contains `catalog-index.json`, NORAD-prefix
files below `enrichment/`, `manifest.json`, and the `sky/` artifacts.

Remote source bodies, validators, and fetch metadata live below
`/data/state/catalog/sources/`, so moving the persistent volume to another host
also moves the conditional-request state and last-known-good inputs. A network
or upstream HTTP failure reuses those cached inputs and marks the source stale.
The job will not publish if a required source has never been cached, a parser or
record-count safety gate fails, or the merged catalogue drops unexpectedly. If
the normalized content has not changed, no new release is created.

## Container

Build with Podman:

```bash
podman build -t orbit-data:dev .
podman run --rm orbit-data:dev --help
```

Images built from `main` are published to
`ghcr.io/darkflib/orbit-data:latest` and an immutable `sha-<commit>` tag.
