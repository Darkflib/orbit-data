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

Install Quadlet sources and native systemd timers in their respective search
paths. Systemd does not discover `.timer` files from the Quadlet source path:

```bash
install -d -m 0755 /etc/orbit-data /etc/containers/systemd /etc/systemd/system
install -m 0644 deploy/Caddyfile /etc/orbit-data/Caddyfile
install -m 0644 deploy/quadlet/*.container /etc/containers/systemd/
install -m 0644 deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
```

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

Initialize and populate the volume before exposing it:

```bash
systemctl start orbit-data-gp.service
systemctl start orbit-data-catalog.service
systemctl start orbit-data-web.service
systemctl enable --now orbit-data-gp.timer orbit-data-catalog.timer
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/v1/status/gp.json
curl --fail http://127.0.0.1:8080/v1/status/catalog.json
```

Quadlet services are transient generated units and cannot be enabled with
`systemctl enable`. The web Quadlet's `[Install]` section is applied by the
generator during boot and `daemon-reload`, so starting it explicitly is enough
for the initial deployment. The timers are native persistent systemd units and
are enabled normally.

The GP timer waits at least 2 hours 10 minutes after each completed run, safely
above the service's persisted 2-hour-5-minute request floor. The catalogue runs
daily at 06:17 UTC with up to 30 minutes of jitter and catches up after downtime.
When the volume provides cross-host advisory locking, the application lock files
prevent two hosts from writing the same stream concurrently during failover.

Caddy serves the full `/srv/orbit-data` mount read-only because the public
catalogue path is an atomic relative symlink into `releases/`. It exposes only
`127.0.0.1:8080`; terminate TLS at the host's existing proxy or load balancer.
Change `PublishPort` deliberately if Caddy must be directly reachable.

## Operations and failover

Useful checks:

```bash
systemctl list-timers 'orbit-data-*'
journalctl -u orbit-data-gp.service -u orbit-data-catalog.service --since today
systemctl status orbit-data-web.service
curl --fail http://127.0.0.1:8080/v1/data/manifest.json
```

For failover, stop the two timers and web service on the old host, move or
remount the network volume at the same path, then start the web service and
enable the timers on the replacement.
If the old host cannot be stopped, cross-host volume locks still prevent
concurrent writers when supported, but traffic should not be switched until the
replacement health and status endpoints are good.

The Quadlets use `Pull=newer`, so every start checks for a newer image. If GHCR
is temporarily unavailable, the updater start can fail while the static server
continues serving its last-known-good files; the next timer activation retries.
