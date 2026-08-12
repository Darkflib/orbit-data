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
  two-hour minimum request interval and a rolling daily byte budget;
- response-size, schema, physical-range, duplicate-ID, record-floor and
  record-drop validation;
- last-known-good behaviour for HTTP, network and validation failures;
- a slow-cadence SATCAT/GCAT enrichment build with vendored magnitude and sky data;
- deterministic catalogue artifacts with conditional source requests and
  atomic last-known-good publication;
- an hourly freshness and free-space check that fails a systemd unit when
  published data ages out;
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

### Upstream failure handling

CelesTrak asks that machine-to-machine clients stop querying on any non-`200`
response, and firewalls IP addresses that ignore that. The updater distinguishes
three kinds of failure, because they need opposite handling:

- **An `HTTP 403` whose body says the data has not updated** is the healthy
  steady state, not a fault. `gp.php` serves no `ETag` and no `Last-Modified`,
  so under CelesTrak's one-download-per-update policy this refusal *is* the
  conditional request. The run continues to the next dataset.
- **Any other non-`200`** — a different `403`, a redirect, a `404`, a `5xx`, an
  oversized body, or an invalid OMM payload — stops the run immediately without
  replacing the last-known-good file. There are no retries: CelesTrak is
  explicit that repeating such a request is what gets an IP firewalled. The
  first 4 KB of the response body is recorded in the dataset's status document,
  because that body is the only thing that says *which* limit was hit.
- **A network failure** (a connect timeout, a reset) means the request never
  reached CelesTrak at all, so it carries no instruction and spent none of their
  budget. One does not stop the run; two in a row does, and marks the run
  `blocked` so `check-health` fails the unit rather than waiting for the data to
  age out. A network failure also retries after
  `gp.network_retry_interval_seconds` rather than surrendering a full cycle.

Datasets are attempted least-recently-attempted first. Configuration order is
otherwise a fixed queue, and a dataset that stops the run at position zero
starves everything behind it on every subsequent run.

### Staying inside CelesTrak's limits

CelesTrak firewalls IP addresses that pull more than 100 MB/day, and `gp.php`
serves no compression, so the whole allowance is spent in uncompressed
responses. Three things keep this service well under that:

- the timer fires every 6h, not at the 2h floor — the underlying 18 SDS GP data
  only updates 2-3 times a day, so faster polling downloads identical bytes;
- the `starlink` GROUP is not fetched, because it is a strict subset of `active`
  and CelesTrak names the pair as redundant. Derive it downstream;
- `gp.maximum_daily_bytes` is a hard backstop. Bytes fetched are recorded in a
  rolling 24-hour ledger at `/data/state/gp-bandwidth.json`, on the persistent
  volume so a restart or a volume failover cannot hand the process an allowance
  it has already spent. Datasets are skipped once the budget is gone.

The current dataset list costs roughly 7.9 MB per run, or about 32 MB/day.

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

Report on the published tree without touching it:

```bash
uv run orbit-data --config config/orbit-data.toml check-health
```

This reads only the status documents and the filesystem. It reports free space,
whether the public catalogue still resolves, and how stale each GP dataset and
the catalogue job have become. Warnings exit `0`; anything critical exits `1` so
`orbit-data-check.service` fails and systemd's `OnFailure=` can page. Thresholds
come from the optional `[health]` config table.

## Container

Build with Podman:

```bash
podman build -t orbit-data:dev .
podman run --rm orbit-data:dev --help
```

Images built from `main` are published to
`ghcr.io/darkflib/orbit-data:latest` and an immutable `sha-<commit>` tag.
