#!/bin/sh

set -eu

usage() {
    cat <<'EOF'
Usage: deploy/install.sh [--start]

Install the Orbit Data Quadlets, native timers, and Caddy configuration.

  --start  Enable the updater and health-check timers and restart the static
           web service. This does not run the updater jobs for initial
           population.
EOF
}

start_services=false
case "${1:-}" in
    "") ;;
    --start) start_services=true ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
install_root=${DESTDIR:-}

if [ -n "$install_root" ]; then
    if [ "$start_services" = true ]; then
        echo "error: --start cannot be used with DESTDIR" >&2
        exit 2
    fi
elif [ "$(id -u)" -ne 0 ]; then
    echo "error: run this installer as root" >&2
    exit 1
fi

quadlet_dir="$install_root/etc/containers/systemd"
systemd_dir="$install_root/etc/systemd/system"
config_dir="$install_root/etc/orbit-data"
site_dir="$config_dir/site"

install -d -m 0755 "$quadlet_dir" "$systemd_dir" "$config_dir" "$site_dir"

# Clean up timer files installed by versions before native timer packaging.
rm -f \
    "$quadlet_dir/orbit-data-gp.timer" \
    "$quadlet_dir/orbit-data-catalog.timer"

install -m 0644 "$script_dir/Caddyfile" "$config_dir/Caddyfile"
install -m 0644 "$script_dir/site/"* "$site_dir/"
install -m 0644 "$script_dir/quadlet/"*.network "$quadlet_dir/"
install -m 0644 "$script_dir/quadlet/"*.container "$quadlet_dir/"
install -m 0644 "$script_dir/systemd/"*.timer "$systemd_dir/"

if [ -n "$install_root" ]; then
    echo "Orbit Data deployment files staged below $install_root"
    exit 0
fi

systemctl daemon-reload

if [ "$start_services" = true ]; then
    if [ ! -d /srv/orbit-data ]; then
        echo "error: /srv/orbit-data must be mounted before services start" >&2
        exit 1
    fi

    # The health-check timer has no stamp file on a first install, so enabling
    # it schedules the next hourly boundary rather than firing immediately —
    # leaving room for the documented initial population run.
    systemctl enable --now \
        orbit-data-gp.timer \
        orbit-data-catalog.timer \
        orbit-data-check.timer
    systemctl reset-failed orbit-data-web.service
    systemctl restart orbit-data-web.service
fi

echo "Orbit Data deployment files installed"
if [ "$start_services" = false ]; then
    echo "Run $0 --start to enable timers and start the web service"
fi
