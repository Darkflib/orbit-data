# Orbit Data

`orbit-data` builds and publishes the static data consumed by
[Darkflib/orbit](https://github.com/Darkflib/orbit). It is designed to run as
Podman Quadlets on a Linux host, with scheduled one-shot updater
containers writing to a persistent volume and a separate static web server
mounting the published tree read-only.

## Why this exists

[Orbit](https://darkflib.github.io/orbit) should not require every user to
query CelesTrak directly. `orbit-data` is a shared, responsibly refreshed data
layer: it retrieves the upstream data within CelesTrak's
[usage policy](https://celestrak.org/usage-policy.php) and GP update guidance,
then keeps a static, cached copy available for Orbit clients.

I intend to keep the public service online for the foreseeable future, so
Orbit users can rely on it instead of adding their own load to CelesTrak. The
[Orbit source code](https://github.com/Darkflib/orbit) and this repository are
both open source.

The repository is being implemented in stages. It currently provides:

- a Python 3.13 package managed with `uv`;
- structured JSON logging;
- TOML configuration;
- atomic static-file and release-directory publication;
- bounded release retention;
- a sequential, allow-listed CelesTrak OMM cache, fetched as CSV and published
  as OMM JSON, with a persistent two-hour minimum request interval, a rolling
  daily byte budget with pre-flight size estimates, and optional per-dataset
  byte caps;
- derived datasets filtered out of an already-fetched one, so a constellation
  that is a strict subset of `active` is published without being downloaded;
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
responses — which leaves the wire format itself as one of the few levers there
are. Four things keep this service well under the threshold:

- the request asks for CSV, not JSON. The same records rendered as JSON are
  about three times the size, and that alone was most of this service's traffic
  — see [Wire format](#wire-format-csv-in-json-out);
- the timer fires every 6h, not at the 2h floor — the underlying 18 SDS GP data
  only updates 2-3 times a day, so faster polling downloads identical bytes;
- only three queries are made at all. Every constellation GROUP this service
  used to fetch was confirmed a strict subset of `active`, so it is now filtered
  out of `active` locally instead — see [Derived datasets](#derived-datasets);
- `gp.maximum_daily_bytes` is a hard backstop. Bytes fetched are recorded in a
  rolling 24-hour ledger at `/data/state/gp-bandwidth.json`, on the persistent
  volume so a restart or a volume failover cannot hand the process an allowance
  it has already spent. Datasets are skipped once the budget is gone.

The current dataset list costs roughly 2.5 MB per run, or about 10 MB/day. It
was 6.9 MB per run and about 28 MB/day while the fetch asked for JSON.

The allowance is spent *before* the request, not during it. Each dataset
remembers the size of its last complete response in
`/data/state/gp/<name>.json`, and a dataset whose next response would not fit in
what is left is skipped without opening the connection — being cut off
mid-stream throws away every byte already pulled from a service that is
rationing us. The estimate carries a small margin because catalogues grow
between runs. A dataset that has never been measured is attempted: refusing the
unknown would deadlock a fresh deployment, and the mid-stream ceiling still
bounds it to exactly the allowance that remains.

A skipped dataset does not record an attempt, because it never made one. It
therefore keeps its place at the front of the least-recently-attempted queue,
which is deliberate: skipping costs no network and does not stop the run, so it
cannot wedge the datasets behind it, and holding the front means the dataset
that has waited longest gets first claim on the allowance when the 24-hour
window rolls.

`[[gp.datasets]]` also takes an optional `maximum_bytes`, a ceiling for that one
query so a single oversized GROUP cannot spend the shared allowance on its own.
`active` is capped at 4 MiB against a current wire size of about 2.5 MB. Every
byte figure in the configuration and in the status tree is a wire figure: the
JSON document published from a response is roughly three times larger, and none
of it crosses CelesTrak's wire. The key is optional and absent means "only the
shared allowance applies", so a deployed configuration predating it still loads.
Breaching a per-dataset cap fails that dataset only; it neither stops the run nor
reports the shared budget as spent.

`/data/public/v1/status/gp.json` reports `daily_bytes`, `budget_bytes` and
`budget_remaining_bytes` for the trailing 24 hours, and each
`/data/public/v1/status/gp/<name>.json` carries that dataset's
`last_response_bytes` and its configured `maximum_bytes`, so "which GROUP is
eating the allowance" is answerable from the served tree without journal access.

### Wire format: CSV in, JSON out

`gp.php` will render the same GP records as TLE, CSV or JSON, and the JSON
rendering is about three times the size of the other two — roughly 420 bytes per
OMM record against 152. With no compression on offer that difference is paid in
full, every run, and it was the single largest component of this service's
traffic. The fetch therefore requests `FORMAT=CSV`.

Only the request changed. `src/orbit_data/omm_csv.py` converts the response into
exactly the records `FORMAT=JSON` would have returned — same field names, same
JSON types, same order — and it is that JSON document which is validated,
hashed, and written to `/data/public/v1/gp/<name>.json`. Every consumer parses
the same records out of the same fields, so nothing downstream changes.

The *bytes* of the three fetched datasets do change once, and only in
formatting. `active.json`, `stations.json` and `special-decaying.json` used to
be CelesTrak's response body written straight to disk; they are now serialized
by `orjson` here. No value differs — but the whitespace does, so their `sha256`
changes on the first run after the switch for reasons that have nothing to do
with the data. Every later change to it means what it always meant: CelesTrak
published new elements. This is
not a new class of behaviour: the ten [derived datasets](#derived-datasets)
have always been `orjson` output, so the switch makes all thirteen published
files consistent rather than introducing an inconsistency. A consumer that
pins a hash rather than parsing the file will need to re-pin once; the Orbit
frontend does not. `sha256` in the state and status documents
still means "the hash of the file we published", not the hash of the response
that carried it, and `last_response_bytes` still means the opposite: the size of
what crossed CelesTrak's wire, because that is the number the budget forecast is
about.

The one thing the switch genuinely costs is truncation detection. A JSON body
cut off in flight does not parse, and the fetch fails loudly with the
last-known-good file untouched; a CSV body cut off in flight parses perfectly
well as fewer rows. Three guards replace what JSON gave for free:

- the header must contain every field the OMM validator requires, and columns
  are read by name, so an upstream reordering moves values with their names
  rather than shifting a catalogue one column to the left;
- every data row must have exactly the header's field count, which is the shape
  a body cut off mid-line actually has;
- a body cut off exactly at a line ending is structurally perfect, and nothing
  in the parse can distinguish it from a genuinely short answer. That case is
  caught downstream by `minimum_records` and `maximum_count_drop_fraction`,
  which is why those two settings matter more than they used to.

Anything that fails is an `OmmValidationError`, which reaches the same handling
an unparseable JSON body did: the run stops and the last-known-good file stays
where it is.

### Derived datasets

CelesTrak enforces one download per update on the Active and Starlink GROUPs,
and states plainly that fetching a GROUP alongside the Active list containing it
is the waste that policy exists to stop. Comparing NORAD catalogue numbers
across a full pull of all twelve GROUPs this service used to fetch confirmed the
overlap exactly: `starlink`, `oneweb`, `kuiper`, `qianfan`, `hulianwang`, `geo`
and all four GNSS groups were each a strict subset of `active`, down to the last
object. Only `stations` (2 debris objects) and `SPECIAL=DECAYING` (12 rocket
bodies and debris) held anything `active` did not.

So three queries are made, and everything else is filtered out of `active`
locally. What `active` does not carry is *membership* — an OMM record does not
say which constellation it belongs to — so each `[[gp.derived]]` rule
reconstructs it from `OBJECT_NAME`, from `MEAN_MOTION`, or both:

```toml
[[gp.derived]]
name = "starlink"
source = "active"
pattern = "^STARLINK"
minimum_records = 5000
maximum_count_drop_fraction = 0.20
```

Each derived dataset publishes to `/v1/gp/<name>.json` exactly where the fetched
GROUP did, so consumers see no difference. It is validated with the same record
and count guards as a fetched response, so a rule that stops matching — upstream
renaming a family of objects — fails loudly instead of silently emptying a
layer. A derived failure never fails its source: no CelesTrak request is at
stake, so the run continues, the last-known-good file is retained, and
`derived_failed` in the run summary drives a `check-health` warning — the
per-dataset age checks cannot catch it, because a retained file keeps its
previous `last_success`.

Derivation is a reconciliation against the volume rather than a step hung off a
successful fetch, and that distinction matters more than it looks. The three
commonest states of a healthy run supply no response to filter: a source inside
its request floor is skipped, a routine "not updated" `403` is the steady state
under one-download-per-update, and a fresh deployment starts with neither. In
all three a perfectly good `active.json` is already on disk. Each run therefore
re-reads the published source and rebuilds any derived dataset whose recorded
source publication differs from the source's current one. A newly added
`[[gp.derived]]` rule is filled in on the next run rather than waiting for
CelesTrak to update, and a derived dataset's `last_success` is its source's, so
its age is honest and the check is idempotent.

The reconstruction is deliberately approximate, and the shipped configuration
documents the measured difference against CelesTrak's own grouping for each
rule. Recall is complete — nothing CelesTrak lists is missing — but some rules
select a few extra: 8 decommissioned NAVSTARs that `gps-ops` excludes as
non-operational, 2 retired BeiDou-2 GEO craft, 18 objects near the
geosynchronous belt, and 23 Guowang-related objects. Those `-ops` distinctions
encode an operational-status judgement that no field in the OMM record carries.
The frontend resolves an object claimed by two layers by priority, so an extra
shows up as a colour rather than a duplicate.

`/data/public/v1/status/gp/<name>.json` names the source and the rule for a
derived dataset, so the served tree distinguishes "CelesTrak is stale" from "our
own filter stopped matching" without access to the configuration.

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

### Objects with no element set

CelesTrak publishes no GP data at all for some catalogued objects, and in most
of those cases never will: the US government withholds elements for its
classified payloads — USA 224 (NORAD 37348) is one of roughly 700 — and a
heliocentric probe such as Mariner 4 has no Earth orbit to publish. An empty GP
track is otherwise indistinguishable from a broken fetch, so every enrichment
record states what SATCAT knows:

- `dataStatus` gives SATCAT's reason for the absence — `no-elements-available`,
  `no-current-elements`, or `no-initial-elements` — and is `null` when elements
  are available;
- `orbitCenter` names the body the object orbits, `earth` for all but a few
  hundred. Objects docked to another catalogued object carry that object's NORAD
  ID, and any code CelesTrak adds later is published raw rather than dropped;
- `approximateOrbit` carries SATCAT's own `periodMinutes`, `inclinationDeg`,
  `apogeeKm` and `perigeeKm`, which survive for most withheld Earth-orbiting
  payloads even though their element set does not, and is `null` when SATCAT
  describes no orbit.

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
