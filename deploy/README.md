# deploy/

Deployment is wwff-tech/gitops, `quadlet/apps/orbit/`: the Quadlet units, the timers, the
Caddyfile and front page that hosts install, the nginx vhost, and the image pin all live there
and nowhere else. Nothing in this repository touches a host.

What is left here:

| File | What | Status |
| --- | --- | --- |
| `Caddyfile` | The static server's configuration. CI validates it and smoke-tests a read-only Caddy against it; `tests/test_deployment.py` holds its contract. | A second copy: hosts install gitops' `files/Caddyfile`, which was byte-identical when the units were removed from here. Change both together. |
| `orbit-egress.nft` | nftables SNAT fragment that sends the updaters' traffic out through a secondary address. | No gitops equivalent, and not in use: gitops' `app.toml` records that the fixed-egress SNAT was not carried over. |

| Concern | In wwff-tech/gitops |
| --- | --- |
| Units, network, timers | `quadlet/apps/orbit/units/` |
| Caddyfile, front page (`index.html`, `site.css`, `favicon.svg`) | `quadlet/apps/orbit/files/` |
| nginx vhost and TLS | `quadlet/apps/orbit/files/nginx.conf`, `certs/certs.toml` |
| Installing, enabling timers, restarting | `quadlet/bin/reconcile.py` on the host |
| Image pin and signature check | `quadlet/bin/promote.py` against `quadlet/policy.toml`; the units pin a digest with `Pull=missing` |
| Failure alerts | `OnFailure=gitops-alert@%n.service` (`quadlet/bin/alert.py`) |

A push to `main` publishes `ghcr.io/darkflib/orbit-data:sha-<commit>` and signs it with cosign
(keyless). The workflow's `promote` job then opens the pin as a PR in wwff-tech/gitops and
merges it once that repository's checks pass; hosts pick it up on their next reconcile. It
needs the `GITOPS_TOKEN` secret (contents and pull-requests write on wwff-tech/gitops) and
fails saying so without it.

To promote by hand (the fallback if that job fails, and how to roll back to an older commit),
from a gitops checkout:

```bash
python3 quadlet/bin/promote.py orbit ghcr.io/darkflib/orbit-data sha-<commit> --pr
```

The rest of this file is what an operator needs to know about the service itself, whichever
units run it.

## The data volume

The network volume must be mounted at `/srv/orbit-data` on every candidate host.
Create its root once with ownership `10001:10001` and mode `0755`. All hosts
must see the same numeric ownership; do not use Podman's `:U` option on a shared
filesystem because it recursively changes ownership.

Use a POSIX-like volume that preserves numeric ownership, symbolic links,
atomic same-filesystem rename, durable `fsync`, and advisory locks across hosts.
NFSv4 with locking enabled is a typical fit; verify those semantics for the
actual storage product before relying on automatic overlap protection.

On SELinux hosts, configure the network mount for container access according to
the filesystem driver and distribution policy. Do not append `:Z` to a shared
NFS/CIFS mount: relabelling a shared tree can affect other hosts, and NFS commonly
cannot store SELinux labels.

## Schedule

The GP timer waits 6 hours after each completed run, with up to 15 minutes of jitter. That is
far above the service's persisted 2-hour-5-minute request floor, deliberately:
the underlying 18 SDS GP data only updates 2-3 times a day, so polling at the
floor re-downloaded identical bytes and pushed this host past CelesTrak's
100 MB/day firewall threshold. The floor stays where it is so an out-of-band
`systemctl start orbit-data-gp.service` cannot undercut it. The catalogue runs
daily at 06:17 UTC with up to 30 minutes of jitter and catches up after downtime.

If CelesTrak stops answering, `journalctl -u orbit-data-gp.service` now carries
their refusal text verbatim, and `/v1/status/gp.json` reports `blocked` plus
`daily_bytes`, `budget_bytes` and `budget_remaining_bytes` for the trailing 24
hours. A temporary block clears itself within
two hours of the queries stopping (`systemctl stop orbit-data-gp.timer`); a
firewall entry earned by sustained abuse needs a mail to `TS.Kelso@celestrak.org`
quoting the IP address. See <https://celestrak.org/usage-policy.php>.
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
the message, so a page says `37.0h old; last error: HTTP 503` rather than just
reporting an age.

The `gp-run` check reads `/v1/status/gp.json` and is the one that fires *fast*.
Dataset ages cannot report a block: a refused run leaves every last-known-good
file exactly where it was, so staleness only crosses a threshold many hours
later — long after the two-hour window in which simply stopping clears a
temporary CelesTrak block. `gp-run` goes critical on the first run that comes
back `blocked`, and also catches a timer that has stopped firing at all, which
per-dataset ages report only indirectly.

Bandwidth is visible without the journal. `/v1/status/gp.json` carries
`daily_bytes` against `budget_bytes` and `budget_remaining_bytes`, and each
`/v1/status/gp/<name>.json` carries that dataset's `last_response_bytes` and its
configured `maximum_bytes`, so the GROUP responsible for a spent allowance can
be identified from the served tree alone:

```bash
curl -s http://127.0.0.1:8080/v1/status/gp.json | jq '{daily_bytes, budget_bytes, budget_remaining_bytes}'
curl -s http://127.0.0.1:8080/v1/status/gp/active.json | jq '{last_result, last_response_bytes, maximum_bytes}'
```

A dataset whose remembered size will not fit in what is left of the allowance is
skipped before the connection is opened and records `last_result:
"budget-skipped"`; the run summary reports `budget_exhausted` and `gp-run` warns.
One that exceeds its own `maximum_bytes` records `last_result:
"over-dataset-cap"` and keeps doing so on every run until an operator raises the
cap — the shared allowance is not the problem there, and the run is not marked
`budget_exhausted` for it. Both are visible immediately in
`journalctl -u orbit-data-gp.service -p warning`.

Warnings are logged and exit zero. A critical failure in the GP, catalogue, or health-check
unit starts gitops' `gitops-alert@.service`, which sends the failed run's last journal lines to
the notification hub. The image still carries `orbit-data alert-slack`, which the unit that
used to live here ran to reduce that journal to the failing check records before posting to
Slack; nothing calls it under gitops.

Thresholds live in the optional `[health]` table of `/etc/orbit-data.toml`
(18h/36h for GP, 36h/72h for the catalogue, 2 GiB/512 MiB free). The GP
thresholds are looser than the 6-hour timer implies on purpose: `last_success`
only advances when CelesTrak actually has new data, so a healthy dataset can sit
at 12 hours old. `gp-run` is the check that notices a fast failure. Every key
defaults, so a config file predating this job still monitors correctly rather
than refusing to start — a monitor that fails closed on its own configuration
goes quiet exactly when it is needed.

The check container mounts the volume read-only, so a monitor can never repair,
rotate, or truncate the tree it is judging.

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

## Optional secondary outbound IP

Not in use (see the table above); kept until it is either moved into gitops or dropped.

`orbit-egress.network` gives the updater containers a predictable bridge subnet, but it does
not select a host source address by itself. Without an additional host firewall rule, traffic
from that subnet uses the host's normal outbound address.

To send updater traffic through a secondary IP, first configure that address on
the host's external interface using the distribution's persistent network
configuration. The address must be present after reboot and the upstream
network must route it to the host. Identify the external interface and confirm
the address before changing nftables, for example:

```bash
ip route get 1.1.1.1
ip -brief address show dev ens3
```

Then install the supplied nftables fragment and edit it for the host:

```bash
sudo install -d -m 0755 /etc/nftables.d
sudo install -m 0644 deploy/orbit-egress.nft /etc/nftables.d/orbit-egress.nft
sudoedit /etc/nftables.d/orbit-egress.nft
```

Replace `oifname "ens3"` with the external interface and replace the address
after `snat to` with the secondary IP. The `ip saddr` subnet must match
`Subnet` in gitops' `units/orbit-egress.network` (`10.89.60.0/24`).

Make the rule persistent by including the fragment once from the host's main
nftables configuration, normally `/etc/nftables.conf`:

```nftables
include "/etc/nftables.d/orbit-egress.nft"
```

Validate the complete ruleset, then enable and reload it using the host's
normal nftables procedure. On a systemd host where `nftables.service` owns
`/etc/nftables.conf`, that is typically:

```bash
sudo nft --check --file /etc/nftables.conf
sudo systemctl enable nftables.service
sudo systemctl restart nftables.service
sudo nft list table ip orbit_egress
```

Review the host firewall configuration before restarting it, because loading
the main ruleset can replace active rules. If firewalld or another firewall
manager owns nftables, add the equivalent SNAT rule through that manager rather
than enabling a competing `nftables.service`.

After an updater has made a request, the counter in the `postrouting` chain should increase
and the remote service should observe the secondary IP:

```bash
sudo nft list chain ip orbit_egress postrouting
```
