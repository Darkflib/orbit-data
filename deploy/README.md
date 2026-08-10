# Linux deployment

The reference deployment uses system (rootful) Quadlets. The application still
runs as UID/GID `10001`, Caddy runs as UID/GID `65532`, both images have
read-only root filesystems, and Caddy only binds to loopback. Rootful Podman is
intentional here: numeric ownership of a bind-mounted network filesystem is
predictable across failover hosts without subordinate-ID mappings.

## Host preparation

The network volume must be mounted at `/srv/orbit-data` on every candidate host.
Create its root once with ownership `10001:10001` and mode `0755`. All hosts
must see the same numeric ownership; do not use Podman's `:U` option on a shared
filesystem because it recursively changes ownership.

Use a POSIX-like volume that preserves numeric ownership, symbolic links,
atomic same-filesystem rename, durable `fsync`, and advisory locks across hosts.
NFSv4 with locking enabled is a typical fit; verify those semantics for the
actual storage product before relying on automatic overlap protection.

Install the Quadlet sources, native systemd timers, Caddy configuration, and
the small static front page served at `/`. The installer is idempotent and
removes obsolete timer files from the Quadlet source path. By default it only
installs files and reloads systemd:

```bash
sudo deploy/install.sh
```

Use `sudo deploy/install.sh --start` to also enable both updater timers and
restart the static web service. The tracked Caddyfile and front-page files are
replaced on each run; keep intentional changes in the repository rather than
editing the installed copies.

Ensure the GHCR package is public, or log the rootful Podman service account in
with a read-only package credential before starting the updater units. Confirm
both images are available with:

```bash
podman pull ghcr.io/darkflib/orbit-data:latest
podman pull docker.io/library/caddy:2.11.4-alpine
```

Quadlet requires cgroup v2. Check the generated units before enabling them:

```bash
podman info --format '{{.Host.CgroupsVersion}}'
QUADLET_UNIT_DIRS=/etc/containers/systemd \
  /usr/lib/systemd/system-generators/podman-system-generator --dryrun
```

On SELinux hosts, configure the network mount for container access according to
the filesystem driver and distribution policy. Do not append `:Z` to a shared
NFS/CIFS mount: relabelling a shared tree can affect other hosts, and NFS commonly
cannot store SELinux labels.

## First start

Install and start the scheduled services, then initialize and populate the
volume before exposing it:

```bash
sudo deploy/install.sh --start
systemctl start orbit-data-gp.service
systemctl start orbit-data-catalog.service
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/v1/status/gp.json
curl --fail http://127.0.0.1:8080/v1/status/catalog.json
```

Run the population commands promptly. `--start` also enables the hourly health
check, which reports an empty tree as critical; because a newly enabled timer
has no stamp file, the first check lands on the next hour boundary rather than
immediately.

Quadlet services are transient generated units and cannot be enabled with
`systemctl enable`. The web Quadlet's `[Install]` section is applied by the
generator during boot and `daemon-reload`, so starting it explicitly is enough
for the initial deployment. The timers are native persistent systemd units and
are enabled normally.

The explicit first-start commands own the initial refresh. The GP timer then
waits at least 2 hours 10 minutes after each completed run, safely above the
service's persisted 2-hour-5-minute request floor. The catalogue runs daily at
06:17 UTC with up to 30 minutes of jitter and catches up after downtime.
When the volume provides cross-host advisory locking, the application lock files
prevent two hosts from writing the same stream concurrently during failover.

Caddy serves the full `/srv/orbit-data` mount read-only because the public
catalogue path is an atomic relative symlink into `releases/`. It exposes only
`127.0.0.1:8080`; terminate TLS at the host's existing proxy or load balancer.
Change `PublishPort` deliberately if Caddy must be directly reachable.

## Monitoring

`orbit-data-check.timer` runs `check-health` hourly. The job exists because
every other failure signal here is a negative: the GP updater deliberately stops
and reuses last-known-good data on an upstream 5xx, and the catalogue job
deliberately reports `unchanged` when nothing moved. Both are correct, both exit
zero, and both are indistinguishable from a service that quietly stopped
updating days ago. Age is the only thing that separates them.

Each pass checks free space on the volume, that `public/v1/data/manifest.json`
still resolves through the release symlink and reports records, how long ago the
catalogue job last *ran* (`checkedAt`, not `generatedAt` — an unchanged
catalogue is healthy), and the age of every configured GP dataset's
`last_success`. A stale dataset's last recorded upstream error is included in
the message, so a page says `13.0h old; last error: CelesTrak returned HTTP 503`
rather than just reporting an age.

Warnings are logged and exit zero. Only a critical fails the unit, so alerting
is an `OnFailure=` drop-in on `orbit-data-check.service`:

```bash
systemctl edit orbit-data-check.service   # [Unit] OnFailure=your-notifier.service
journalctl -u orbit-data-check.service -p warning --since today
systemctl start orbit-data-check.service  # run one pass now
```

Thresholds live in the optional `[health]` table of `/etc/orbit-data.toml`
(6h/12h for GP, 36h/72h for the catalogue, 2 GiB/512 MiB free). Every key
defaults, so a config file predating this job still monitors correctly rather
than refusing to start — a monitor that fails closed on its own configuration
goes quiet exactly when it is needed.

The check container mounts the volume read-only, so a monitor can never repair,
rotate, or truncate the tree it is judging. It uses `Pull=newer`, the same
policy as the updaters, so it never runs a different build of the code it is
monitoring. If GHCR is unreachable the unit may fail, which reaches `OnFailure=`
as an alert — the correct direction to fail, because an alert naming the
registry is recoverable information whereas a monitor frozen on a stale image
reports "healthy" for data it cannot actually evaluate.

`Pull=missing` is only appropriate against a version-pinned tag, as on the web
container. Pairing it with a floating tag like `:latest` pins the unit to
whatever image happens to be cached on that host.

## Operations and failover

Useful checks:

```bash
systemctl list-timers 'orbit-data-*'
journalctl -u orbit-data-gp.service -u orbit-data-catalog.service --since today
systemctl status orbit-data-web.service
curl --fail http://127.0.0.1:8080/v1/data/manifest.json
```

The updater containers set `LogDriver=none`. systemd already captures the
container's stdout into the journal under its own unit, so podman's journald
driver only added a second copy of every structured line. The web container
keeps `LogDriver=journald` deliberately: it is long-running, so `podman logs
orbit-data-web` is worth retaining.

For failover, stop the two timers and web service on the old host, move or
remount the network volume at the same path, then start the web service and
enable the timers on the replacement.
If the old host cannot be stopped, cross-host volume locks still prevent
concurrent writers when supported, but traffic should not be switched until the
replacement health and status endpoints are good.

The updater Quadlets use `Pull=newer`, so each scheduled start checks GHCR for a
newer application image. If GHCR is temporarily unavailable, an updater start
can fail while the static server continues serving its last-known-good files;
the next timer activation retries. Caddy is version-pinned and uses
`Pull=missing`, so a web restart uses the local image without depending on
Docker Hub. Pull a newly pinned Caddy image deliberately during an upgrade.
