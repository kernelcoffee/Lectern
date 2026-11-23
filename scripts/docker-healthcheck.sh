#!/bin/sh
# Health probe of the production image (its HEALTHCHECK): asks the API whether
# Lectern answers.
#
# The engine runs the probe as the container's own user, which is root unless
# `user:` is set (the image has no USER line; see the Dockerfile). The probe
# therefore drops to lectern first, the same way docker-entrypoint.sh does for
# Lectern itself, so not even the probe's curl runs as root.
set -eu

readonly LECTERN_UID=1000
readonly LECTERN_GID=1000

# 127.0.0.1 passes the Host check (an IP address) and the image's CMD pins
# port 8000. curl gives up after 4 s, before the HEALTHCHECK's 5 s timeout:
# podman does not count a probe it had to kill as a failure, so a frozen
# Lectern would stay healthy there.
# The probe runs with the container's environment, so curl would send this
# loopback request to any http_proxy or ALL_PROXY given to the container for
# Lectern's downloads (Docker adds them itself from ~/.docker/config.json):
# --noproxy '*' ignores them all. -q, which must come first, skips .curlrc,
# which under `user:` sits in a home that Lectern and its mods can write to.
set -- curl -q --noproxy '*' -fsS --max-time 4 http://127.0.0.1:8000/api/health

if [ "$(id -u)" -eq 0 ]; then
    exec setpriv --reuid="$LECTERN_UID" --regid="$LECTERN_GID" --clear-groups \
        --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
        -- "$@"
fi
# Docker's `user:` chose the id; the probe runs as that user.
exec "$@"
