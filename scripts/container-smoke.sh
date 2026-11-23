#!/usr/bin/env bash
# Container smoke test of the production image: proves with real containers
# that Lectern runs as uid 1000 with no capabilities, that an existing
# root-owned /data is handed over at startup (named volume and bind mount,
# never through a symbolic link or into another filesystem), that `user:` is
# respected, that every failure stops the container without running Lectern
# as root, that the health check reports healthy and unhealthy, and that
# `docker stop` still shuts Lectern down cleanly. No Minecraft download.
#
#   scripts/container-smoke.sh [--build] IMAGE
#
#   --build   build IMAGE from the repository first (in Docker format, which
#             podman needs to keep the HEALTHCHECK); otherwise IMAGE must exist
#
# The engine comes from CONTAINER_ENGINE (default docker; podman works too).
# Everything it creates is named lectern-smoke-<random>-* and removed on exit,
# also on failure. No host port is published; the API is reached with exec.
#
# Exit codes: 0 every check passed, 1 a check failed (named on stderr),
# 2 misuse or a missing tool or image.
set -euo pipefail

readonly ENGINE="${CONTAINER_ENGINE:-docker}"
readonly API=http://127.0.0.1:8000
readonly HANDOVER_START="handing the data folder /data over to user lectern"
readonly HANDOVER_DONE="the data folder /data now belongs to user lectern"
readonly NOT_STARTED="Lectern was not started."
readonly UVICORN_STARTED="Started server process"
readonly EXPECTED_CMD='["uvicorn","lectern.main:app","--host","0.0.0.0","--port","8000","--timeout-graceful-shutdown","15"]'
# Health checks every 2 s in the test instead of every 30 s; the command and
# its timeout are still the image's own. The start period is shorter too:
# podman ignores every failure inside it, even after a check has passed.
readonly FAST_HEALTH=(--health-interval 2s --health-retries 2 --health-start-period 15s)
# curl inside a container, deaf to proxy variables and .curlrc, as the probe is.
readonly CURL="curl -q --noproxy '*' -fsS -m 3"
# An address nobody answers inside a container, as a proxy or as a URL: a
# curl sent there fails at once.
readonly NOBODY_THERE=http://127.0.0.1:9

# Inside a container: one "root: NAME" line per process with any uid 0, then
# "processes: N". A process that ends between the glob and cat is skipped.
readonly ROOT_SCAN='for f in /proc/[0-9]*/status; do cat "$f" 2>/dev/null; done | awk '\''
  /^Name:/ { name = $2 }
  /^Uid:/ { n++; for (i = 2; i <= 5; i++) if ($i == 0) { print "root:", name; break } }
  END { print "processes:", n + 0 }'\'

usage() {
  printf 'usage: %s [--build] IMAGE\n' "${0##*/}" >&2
  exit 2
}

BUILD=0
IMAGE=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) BUILD=1 ;;
    -h | --help) usage ;;
    -*) usage ;;
    *) [[ -z $IMAGE ]] || usage; IMAGE=$1 ;;
  esac
  shift
done
[[ -n $IMAGE ]] || usage
command -v "$ENGINE" >/dev/null || { printf '%s not found\n' "$ENGINE" >&2; exit 2; }

PREFIX="lectern-smoke-$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
readonly PREFIX
WORK="$(mktemp -d -t lectern-smoke.XXXXXX)"
readonly WORK
ERR="$WORK/engine.err"
PREP_SEQ=0

# ---------------------------------------------------------------- helpers ---

say() { printf '%s\n' "$*" >&2; }
pass() { say "ok    $*"; }

# The engine, with podman's "Emulate Docker CLI" notice dropped from stderr.
eng() {
  local status=0
  "$ENGINE" "$@" 2>"$ERR" || status=$?
  sed '/^Emulate Docker CLI using podman/d' "$ERR" >&2
  return "$status"
}

logs_of() { eng logs "$1" 2>&1; }

# fail CHECK [CONTAINER]: name the failed check, show the container's log.
fail() {
  say "FAIL  $1"
  if [[ -n ${2:-} ]]; then
    say "----- log of ${2#"$PREFIX-"}"
    logs_of "$2" | tail -n 40 >&2
  fi
  exit 1
}

cleanup() {
  local status=$? ids=()
  if [[ -d $WORK/bind ]]; then
    # The bind-mounted folder holds files of uid 1000 and root that the host
    # user may not delete; remove them from a container.
    "$ENGINE" run --rm --name "$PREFIX-cleanup" --user 0:0 --entrypoint rm \
      -v "$WORK:/work:z" "$IMAGE" -rf /work/bind >/dev/null 2>&1 \
      || say "warning: could not empty $WORK/bind"
  fi
  # The random prefix is unique; no anchor, as engines differ on matching a
  # container name with or without its leading slash.
  mapfile -t ids < <("$ENGINE" ps -aq --filter "name=$PREFIX-" 2>/dev/null)
  if ((${#ids[@]})); then
    "$ENGINE" rm -f "${ids[@]}" >/dev/null 2>&1 || say "warning: could not remove containers $PREFIX-*"
  fi
  mapfile -t ids < <("$ENGINE" volume ls -q --filter "name=$PREFIX-" 2>/dev/null)
  if ((${#ids[@]})); then
    "$ENGINE" volume rm -f "${ids[@]}" >/dev/null 2>&1 || say "warning: could not remove volumes $PREFIX-*"
  fi
  rm -rf "${WORK:?}" || say "warning: could not remove $WORK"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

volume() {
  eng volume create "$PREFIX-$1" >/dev/null || fail "setup: volume $1 could not be created"
  printf '%s' "$PREFIX-$1"
}

# prep MOUNT... -- SCRIPT: run SCRIPT as root in a throwaway container with the
# startup script bypassed, the way an install from before this image wrote.
prep() {
  local mounts=()
  while [[ $1 != -- ]]; do mounts+=(-v "$1"); shift; done
  shift
  PREP_SEQ=$((PREP_SEQ + 1))
  eng run --rm --name "$PREFIX-prep$PREP_SEQ" --user 0:0 --entrypoint sh \
    "${mounts[@]}" "$IMAGE" -c "set -eu; $1" || fail "setup: a preparation step failed"
}

# start NAME RUN-ARGS... [-- COMMAND...]: a detached container from IMAGE.
start() {
  local name="$PREFIX-$1" args=()
  shift
  while [[ $# -gt 0 && $1 != -- ]]; do args+=("$1"); shift; done
  if [[ $# -gt 0 ]]; then shift; fi
  eng run -d --name "$name" "${args[@]}" "$IMAGE" "$@" >/dev/null \
    || fail "${name#"$PREFIX-"}: the container could not be created"
}

# as USER CONTAINER COMMAND: run a shell command inside CONTAINER.
as() { eng exec -u "$1" "$PREFIX-$2" sh -c "$3"; }
api() { as 1000:1000 "$1" "$CURL $2"; }

running() { [[ "$(eng inspect -f '{{.State.Running}}' "$PREFIX-$1")" == true ]]; }

wait_api() {
  local deadline=$((SECONDS + 90))
  until api "$1" "-o /dev/null $API/api/health" 2>/dev/null; do
    running "$1" || fail "$1: the container stopped before the API answered" "$PREFIX-$1"
    ((SECONDS < deadline)) || fail "$1: the API did not answer within 90 s" "$PREFIX-$1"
    sleep 1
  done
}

# wait_exit NAME: wait for the container to stop by itself; print its exit code.
wait_exit() {
  local deadline=$((SECONDS + 60))
  while running "$1"; do
    ((SECONDS < deadline)) || fail "$1: the container was expected to stop on its own but kept running" "$PREFIX-$1"
    sleep 1
  done
  eng inspect -f '{{.State.ExitCode}}' "$PREFIX-$1"
}

health_status() { eng inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$PREFIX-$1"; }

wait_health() {
  local deadline=$((SECONDS + $3)) status
  while status="$(health_status "$1")"; [[ $status != "$2" ]]; do
    ((SECONDS < deadline)) || fail "$1: health status is '$status', expected '$2'" "$PREFIX-$1"
    sleep 1
  done
}

# expect_probe_unprivileged NAME: run the image's health probe the way the
# engine does, as the container's own user (root), and check the curl it ends
# in. Call it while uvicorn is frozen: that curl then waits out its 4 s, long
# enough to be found. In /proc a process is named curl only once the probe has
# dropped root and replaced itself with curl.
expect_probe_unprivileged() {
  local status line
  eng exec -d "$PREFIX-$1" docker-healthcheck.sh >/dev/null || fail "$1: the health probe could not be started"
  status="$(as 0:0 "$1" 'tries=0
    while [ "$tries" -lt 40 ]; do
      for f in /proc/[0-9]*/status; do
        if [ "$(sed -n "s/^Name:[[:space:]]*//p" "$f" 2>/dev/null)" = curl ]; then
          cat "$f" 2>/dev/null && exit 0
        fi
      done
      tries=$((tries + 1))
      sleep 0.1
    done
    exit 1')" || fail "$1: no curl of the health probe was found within 4 s" "$PREFIX-$1"
  for line in $'Uid:\t1000\t1000\t1000\t1000' $'Gid:\t1000\t1000\t1000\t1000' \
    $'CapPrm:\t0000000000000000' $'CapEff:\t0000000000000000' \
    $'CapBnd:\t0000000000000000' $'NoNewPrivs:\t1'; do
    grep -qxF -- "$line" <<<"$status" || fail "$1: the health probe's curl lacks '$line'"$'\n'"$status"
  done
}

# expect_no_root_process NAME: the container lists its processes and none of
# them has uid 0 (call it with the health check disabled: the engine starts
# the probe as root for the instant before it drops).
expect_no_root_process() {
  local scan
  scan="$(as 1000:1000 "$1" "$ROOT_SCAN")"
  [[ $scan =~ processes:\ ([0-9]+) ]] && ((BASH_REMATCH[1] > 0)) \
    || fail "$1: could not list the container's processes: $scan"
  [[ $scan != *"root:"* ]] || fail "$1: processes running as root:"$'\n'"$scan"
}

# expect_ran_as_lectern NAME: the container ran `sh -c 'id -u; id -g'` to the
# end with exit code 0, and its output (the log without the startup script's
# own lines) is uid 1000, gid 1000.
readonly PRINT_IDS=(sh -c 'id -u; id -g')
expect_ran_as_lectern() {
  local code ids
  code="$(wait_exit "$1")"
  [[ $code == 0 ]] || fail "$1: exit code $code, expected 0" "$PREFIX-$1"
  ids="$(logs_of "$PREFIX-$1" | grep -vF 'docker-entrypoint:' || true)" # none left is a failure below
  [[ $ids == 1000$'\n'1000 ]] || fail "$1: the command did not run as uid 1000, gid 1000: '$ids'" "$PREFIX-$1"
}

count() { awk -v s="$1" 'index($0, s) { n++ } END { print n + 0 }' <<<"$2"; }

# not_lectern NAME [EXCLUDED]: every entry under /data on the data folder's
# filesystem that is not owned by 1000:1000.
not_lectern() {
  as 0:0 "$1" "find /data -xdev ${2:+-path $2 -prune -o} \\( ! -uid 1000 -o ! -gid 1000 \\) -print"
}

owner() { as 0:0 "$1" "stat -c %u:%g $2"; }

setting() {
  as 1000:1000 "$1" "$CURL $API/api/settings | python3 -c 'import json, sys; print({s[\"key\"]: s[\"value\"] for s in json.load(sys.stdin)}[\"default_memory_mb\"])'"
}

set_setting() {
  as 1000:1000 "$1" "$CURL -X PATCH -H 'Content-Type: application/json' -d '{\"default_memory_mb\": $2}' $API/api/settings" >/dev/null
}

# old_install NAME SETTING MOUNT: Lectern as root with the startup script
# bypassed (the image before this change), a setting changed, then stopped.
old_install() {
  start "$1" --user 0:0 --no-healthcheck --entrypoint uvicorn -v "$3" \
    -- lectern.main:app --host 0.0.0.0 --port 8000
  wait_api "$1"
  grep -qxF $'Uid:\t0\t0\t0\t0' <<<"$(as 0:0 "$1" 'cat /proc/1/status')" \
    || fail "$1: the old install does not run as root"
  set_setting "$1" "$2"
  eng stop -t 30 "$PREFIX-$1" >/dev/null
  eng rm "$PREFIX-$1" >/dev/null
}

# expect_handover NAME: both handover lines, once each.
expect_handover() {
  local logs
  logs="$(logs_of "$PREFIX-$1")"
  [[ "$(count "$HANDOVER_START" "$logs")" == 1 && "$(count "$HANDOVER_DONE" "$logs")" == 1 ]] \
    || fail "$1: expected the two handover lines once each" "$PREFIX-$1"
}

expect_no_handover() {
  [[ "$(count "$HANDOVER_START" "$(logs_of "$PREFIX-$1")")" == 0 ]] \
    || fail "$1: a handover line was printed although nothing needed handing over" "$PREFIX-$1"
}

expect_all_lectern() {
  local wrong
  wrong="$(not_lectern "$1" "${2:-}")"
  [[ -z $wrong ]] || fail "$1: entries under /data not owned by 1000:1000:"$'\n'"$wrong"
}

# expect_refused NAME TEXT...: the container stopped non-zero, said each TEXT,
# and uvicorn never started.
expect_refused() {
  local name=$1 code logs
  shift
  code="$(wait_exit "$name")"
  [[ $code != 0 ]] || fail "$name: exited with code 0, expected a failure" "$PREFIX-$name"
  logs="$(logs_of "$PREFIX-$name")"
  for text in "$NOT_STARTED" "$@"; do
    [[ "$(count "$text" "$logs")" -gt 0 ]] || fail "$name: the log does not say '$text'" "$PREFIX-$name"
  done
  [[ "$(count "$UVICORN_STARTED" "$logs")" == 0 ]] || fail "$name: Lectern started anyway" "$PREFIX-$name"
}

# ------------------------------------------------------------------ setup ---

if ((BUILD)); then
  say "building $IMAGE"
  BUILDAH_FORMAT=docker eng build -q -t "$IMAGE" "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)" >/dev/null
fi
eng image inspect "$IMAGE" >/dev/null 2>&1 || { say "image $IMAGE not found (build it, or pass --build)"; exit 2; }
say "smoke-testing $IMAGE with $ENGINE as $PREFIX-*"

# ------------------------------------------------------ the image itself ---

[[ "$(eng image inspect -f '{{json .Config.Entrypoint}}' "$IMAGE")" == '["docker-entrypoint.sh"]' ]] \
  || fail "image: the entrypoint is not docker-entrypoint.sh"
[[ "$(eng image inspect -f '{{json .Config.Cmd}}' "$IMAGE")" == "$EXPECTED_CMD" ]] \
  || fail "image: the CMD changed"
[[ -z "$(eng image inspect -f '{{.Config.User}}' "$IMAGE")" ]] \
  || fail "image: a USER is set; the startup script needs root to hand /data over"
HEALTH=engine
healthcheck="$(eng image inspect -f '{{json .Config.Healthcheck}}' "$IMAGE")"
if [[ $healthcheck == null || -z $healthcheck ]]; then
  if "$ENGINE" --version 2>/dev/null | grep -qi podman; then
    say "note  podman reports no health check for this image (built in OCI format?);"
    say "      checking the API directly instead. Build with BUILDAH_FORMAT=docker to test it."
    HEALTH=api
  else
    fail "image: no HEALTHCHECK"
  fi
else
  [[ $healthcheck == *'"Test":["CMD","docker-healthcheck.sh"]'* \
    && $healthcheck == *'"Interval":30000000000'* && $healthcheck == *'"Timeout":5000000000'* \
    && $healthcheck == *'"StartPeriod":60000000000'* && $healthcheck == *'"Retries":3'* ]] \
    || fail "image: unexpected HEALTHCHECK $healthcheck"
fi
if [[ $HEALTH == engine ]]; then
  pass "image: entrypoint, unchanged CMD, no USER, health check"
else
  pass "image: entrypoint, unchanged CMD, no USER (health check not reported by the engine)"
fi

# ---------------------------------------------- default start, new volume ---

data="$(volume data)"
start default --no-healthcheck -v "$data:/data"
wait_api default
status="$(as 1000:1000 default 'cat /proc/1/status')"
cmdline="$(as 1000:1000 default 'tr "\0" " " </proc/1/cmdline')"
[[ $cmdline == *"/uvicorn lectern.main:app "* ]] || fail "default: process 1 is not uvicorn: $cmdline"
for line in $'Uid:\t1000\t1000\t1000\t1000' $'Gid:\t1000\t1000\t1000\t1000' \
  $'CapInh:\t0000000000000000' $'CapPrm:\t0000000000000000' $'CapEff:\t0000000000000000' \
  $'CapBnd:\t0000000000000000' $'CapAmb:\t0000000000000000' $'NoNewPrivs:\t1'; do
  grep -qxF -- "$line" <<<"$status" || fail "default: process 1 lacks '$line'"$'\n'"$status"
done
grep -qE $'^Groups:[ \t]*$' <<<"$status" || fail "default: process 1 keeps supplementary groups"
expect_no_root_process default
pass "default start: process 1 is uvicorn as 1000:1000, no groups, no capabilities, no_new_privs, nothing as root"
# Java and installers write to the home directory: Lectern must get lectern's
# own, writable one, not root's (closed to it) or /.
home="$(as 1000:1000 default 'getent passwd 1000 | cut -d: -f6')"
[[ -n $home && "$(as 1000:1000 default 'tr "\0" "\n" </proc/1/environ | sed -n "s/^HOME=//p"')" == "$home" ]] \
  || fail "default: process 1's HOME is not lectern's home directory"
as 1000:1000 default "test -d '$home' && test -w '$home'" \
  || fail "default: lectern's home directory is missing or not writable by uid 1000"
pass "default start: HOME is lectern's home directory and uid 1000 can write there"
# A new volume copies the owner of the image's own /data, which the image
# gives to 1000:1000: a new install is not an upgrade and hands nothing over.
expect_no_handover default
pass "new install: no handover line on a fresh volume"
expect_all_lectern default
[[ "$(as 1000:1000 default 'ls /data')" == *servers* ]] || fail "default: Lectern did not create its data tree"
pass "default start: Lectern created /data and all of it belongs to 1000:1000"
[[ "$(api default "-o /dev/null -w %{http_code} $API/api/health")" == 200 ]] \
  || fail "default: /api/health did not answer 200 with the default allowed hosts"
pass "health endpoint answers 200 behind the Host check"

eng stop -t 30 "$PREFIX-default" >/dev/null
code="$(eng inspect -f '{{.State.ExitCode}}' "$PREFIX-default")"
[[ $code == 0 ]] || fail "stop: exit code $code, expected 0" "$PREFIX-default"
logs="$(logs_of "$PREFIX-default")"
for text in "Waiting for application shutdown." "Application shutdown complete." \
  "Finished server process [1]"; do
  [[ "$(count "$text" "$logs")" == 1 ]] || fail "stop: the log lacks '$text'" "$PREFIX-default"
done
pass "stop: exit code 0 and Lectern's shutdown lines"

# ------------------------------------------- second start, health status ---

# The proxy variables an admin sets for Lectern's downloads must not reach
# the probe's request to 127.0.0.1.
start again "${FAST_HEALTH[@]}" -e "http_proxy=$NOBODY_THERE" -e "ALL_PROXY=$NOBODY_THERE" \
  -v "$data:/data"
wait_api again
expect_no_handover again
expect_all_lectern again
pass "second start: no handover, nothing changed owner"
eng exec "$PREFIX-again" docker-healthcheck.sh >/dev/null \
  || fail "health probe: fails when the container has http_proxy and ALL_PROXY set" "$PREFIX-again"
pass "health probe: ignores http_proxy and ALL_PROXY"
if [[ $HEALTH == engine ]]; then
  wait_health again healthy 60
  pass "health: healthy once the API answers"
  eng kill --signal STOP "$PREFIX-again" >/dev/null
  wait_health again unhealthy 60
  pass "health: unhealthy once uvicorn stops answering"
else
  eng kill --signal STOP "$PREFIX-again" >/dev/null
  ! api again "-o /dev/null $API/api/health" 2>/dev/null || fail "health: the API answered while frozen"
  pass "health (API checked directly): the API stops answering when uvicorn is frozen"
fi
expect_probe_unprivileged again
eng kill --signal CONT "$PREFIX-again" >/dev/null
pass "health probe: started as root by the engine, its curl runs as 1000:1000 with no capabilities"
eng rm -f "$PREFIX-again" >/dev/null

# ------------------------------------------------- upgrade, named volume ---

old="$(volume old)"
other="$(volume other)"
# The image before this change had a root-owned /data, and so did its volumes.
# The engines give an empty volume the image's owner again on every mount, so
# each root-owned setup below also leaves something in the volume.
prep "$old:/data" -- 'chown 0:0 /data; mkdir /data/servers'
old_install old 3072 "$old:/data"
prep "$old:/data" -- '
  mkdir -p /data/servers/s1/world /data/elsewhere
  echo level > /data/servers/s1/world/level.dat
  ln -s /etc/passwd /data/trap
  ln -s /etc /data/servers/s1/etc-link
  # What a mod can leave in its world: names a root walk must not trust
  # (a leading dash, a newline, a byte that is not UTF-8, a backslash
  # escape as in the mount table), a pipe, a relative link out of /data and
  # a link to nowhere.
  cd /data/servers/s1/world
  touch -- -rf "two
lines" "$(printf "bad\377byte")" "\\040 escaped"
  mkfifo pipe
  ln -s ../../../../etc/group relative-link
  ln -s /nowhere dangling'
prep "$other:/o" -- 'chown 0:0 /o; echo foreign > /o/file'
[[ "$(prep "$old:/data" -- 'find /data ! -uid 0 -print' 2>&1)" == "" ]] || fail "upgrade: the old install is not root-owned"
start upgraded --no-healthcheck -v "$old:/data" -v "$other:/data/elsewhere"
wait_api upgraded
for target in /etc/passwd /etc /etc/hostname /etc/group; do
  [[ "$(owner upgraded "$target")" == 0:0 ]] || fail "upgrade: the handover followed a link to $target"
done
[[ "$(owner upgraded /data/trap)" == 1000:1000 ]] || fail "upgrade: the link /data/trap was not handed over"
[[ "$(owner upgraded /data/servers/s1/world/relative-link)" == 1000:1000 ]] \
  || fail "upgrade: the relative link out of /data was not handed over"
pass "symbolic links: the links themselves handed over, /etc, /etc/passwd and /etc/group still root's"
expect_handover upgraded
expect_all_lectern upgraded /data/elsewhere
pass "upgrade, named volume: handed over once, two log lines, all of /data belongs to 1000:1000"
pass "handover: names with a dash, a newline, a non-UTF-8 byte or an escape, and a pipe, handed over too"
# The handover is the one path where root does real work: once it is over,
# process 1 is still uvicorn as uid 1000 and nothing is left running as root.
status="$(as 1000:1000 upgraded 'cat /proc/1/status')"
[[ "$(as 1000:1000 upgraded 'tr "\0" " " </proc/1/cmdline')" == *"/uvicorn lectern.main:app "* ]] \
  || fail "upgrade: process 1 is not uvicorn after the handover"
for line in $'Uid:\t1000\t1000\t1000\t1000' $'Gid:\t1000\t1000\t1000\t1000' \
  $'CapEff:\t0000000000000000' $'CapBnd:\t0000000000000000' $'NoNewPrivs:\t1'; do
  grep -qxF -- "$line" <<<"$status" || fail "upgrade: process 1 lacks '$line' after the handover"$'\n'"$status"
done
expect_no_root_process upgraded
pass "upgrade: after the handover process 1 is uvicorn as 1000:1000 and nothing runs as root"
for target in /data/elsewhere /data/elsewhere/file; do
  [[ "$(owner upgraded "$target")" == 0:0 ]] || fail "upgrade: the handover changed $target on another filesystem"
done
pass "filesystem boundary: a volume mounted inside /data kept its owner"
[[ "$(setting upgraded)" == 3072 ]] || fail "upgrade: the setting changed before the upgrade is lost"
pass "upgrade, named volume: the setting changed before is still there"
eng rm -f "$PREFIX-upgraded" >/dev/null
start upgraded-again --no-healthcheck -v "$old:/data" -v "$other:/data/elsewhere"
wait_api upgraded-again
expect_no_handover upgraded-again
pass "upgrade: the next start hands nothing over (a root-owned mount inside /data is not counted)"
eng rm -f "$PREFIX-upgraded-again" >/dev/null

# --------------------------------------------------- upgrade, bind mount ---

mkdir "$WORK/bind"
bind="$WORK/bind:/data:z"
prep "$bind" -- 'chown 1000:1000 /data'
old_install bound 4096 "$bind"
[[ "$(prep "$bind" -- 'stat -c %u:%g /data; find /data -mindepth 1 -maxdepth 1 -uid 0 | head -n 1')" == 1000:1000$'\n'/data/* ]] \
  || fail "upgrade, bind mount: the setup is not a 1000-owned folder with root-owned content"
start bound-new --no-healthcheck -v "$bind"
wait_api bound-new
expect_handover bound-new
expect_all_lectern bound-new
[[ "$(setting bound-new)" == 4096 ]] || fail "upgrade, bind mount: the setting changed before is lost"
pass "upgrade, bind mount: handed over, all of /data belongs to 1000:1000, the setting is still there"
eng rm -f "$PREFIX-bound-new" >/dev/null

# ------------------------------------------------- interrupted handover ---

cut="$(volume cut)"
# Deepest first means an interruption leaves the deep entries handed over and
# some direct children (and /data) still root's.
prep "$cut:/data" -- '
  chown 0:0 /data
  mkdir -p /data/servers/s1/world /data/java/jre/bin /data/cache
  touch /data/servers/s1/world/level.dat /data/java/jre/bin/java /data/lectern.sqlite
  chown -R 1000:1000 /data/servers
  chown 1000:1000 /data/java/jre/bin/java'
start resumed --no-healthcheck -v "$cut:/data"
wait_api resumed
expect_handover resumed
expect_all_lectern resumed
pass "interrupted handover: the next start finishes it"
eng rm -f "$PREFIX-resumed" >/dev/null

# ------------------------------------------- ownership cannot be changed ---

frozen="$(volume frozen)"
prep "$frozen:/data" -- 'touch /data/lectern.sqlite'
start readonly --no-healthcheck -v "$frozen:/data:ro"
expect_refused readonly "could not hand the data folder /data over" "user:"
pass "read-only /data: exit non-zero with what to do, Lectern never started"

# FAT or exFAT mounted with the quiet option report success for an ownership
# change and keep root as the owner. A seccomp profile makes every chown do
# the same here: return 0 and change nothing.
ignored="$(volume ignored)"
prep "$ignored:/data" -- 'chown 0:0 /data; touch /data/lectern.sqlite'
printf '%s\n' '{"defaultAction": "SCMP_ACT_ALLOW", "syscalls": [{"names":' \
  '["chown", "fchown", "fchownat", "lchown"], "action": "SCMP_ACT_ERRNO", "errnoRet": 0}]}' \
  >"$WORK/ignore-chown.json"
start ignored-chown --no-healthcheck --security-opt "seccomp=$WORK/ignore-chown.json" \
  -v "$ignored:/data"
expect_refused ignored-chown "still does not belong to uid 1000" "user:"
[[ "$(count "$HANDOVER_DONE" "$(logs_of "$PREFIX-ignored-chown")")" == 0 ]] \
  || fail "ignored-chown: the log claims the handover worked" "$PREFIX-ignored-chown"
pass "filesystem that ignores owners: exit non-zero with what to do, no claim that the handover worked"

# Owned by 1000:1000 but closed to its owner: nothing to hand over, and still
# Lectern could not write there.
closed="$(volume closed)"
prep "$closed:/data" -- 'touch /data/lectern.sqlite; chown -R 1000:1000 /data; chmod 0500 /data'
start closed --no-healthcheck -v "$closed:/data"
expect_refused closed "uid 1000 cannot write to the data folder /data although it owns it"
expect_no_handover closed
pass "/data owned by 1000 but not writable by it: exit non-zero with what to do"

# --------------------------------------------------------- user: override ---

theirs="$(volume theirs)"
prep "$theirs:/data" -- 'chown 1234:1234 /data; touch /data/root-file'
start other-user --no-healthcheck --user 1234:1234 -v "$theirs:/data"
wait_api other-user
grep -qxF $'Uid:\t1234\t1234\t1234\t1234' <<<"$(as 1234:1234 other-user 'cat /proc/1/status')" \
  || fail "user: override: process 1 does not run as 1234"
expect_no_handover other-user
[[ "$(owner other-user /data/root-file)" == 0:0 && "$(owner other-user /data)" == 1234:1234 ]] \
  || fail "user: override: the startup script changed ownership"
pass "user: override, writable: runs as 1234, no ownership changed"
eng rm -f "$PREFIX-other-user" >/dev/null

rootonly="$(volume rootonly)"
prep "$rootonly:/data" -- 'chown 0:0 /data; touch /data/root-file'
start other-user-refused --no-healthcheck --user 1234:1234 -v "$rootonly:/data"
expect_refused other-user-refused "uid 1234" "/data"
pass "user: override, not writable: exit non-zero naming the id and the folder"

# The fully rootless way the startup script itself recommends (user: 1000:1000)
# must work on a new install with a named volume, as with docker compose.
rootless="$(volume rootless)"
start rootless --no-healthcheck --user 1000:1000 -v "$rootless:/data"
wait_api rootless
expect_all_lectern rootless
pass "user: \"1000:1000\" on a fresh named volume: Lectern starts"
# Under user: the probe runs as 1000 with lectern's home, which Lectern and
# its mods can write to: a .curlrc there must not steer the probe. Its line
# sends every connection to an address nobody answers, so a curl that reads
# the file fails. (A proxy line would not show it, as --noproxy overrides it.)
home="$(as 1000:1000 rootless 'getent passwd 1000 | cut -d: -f6')"
[[ -n $home && "$(eng exec "$PREFIX-rootless" sh -c 'printf %s "$HOME"')" == "$home" ]] \
  || fail "rootless: the probe's HOME is not lectern's home '$home'"
as 1000:1000 rootless "printf 'connect-to = \"::%s\"\\n' ${NOBODY_THERE#http://} >$home/.curlrc"
eng exec "$PREFIX-rootless" docker-healthcheck.sh >/dev/null \
  || fail "health probe: a .curlrc in lectern's home steers it" "$PREFIX-rootless"
pass "health probe under user: ignores a .curlrc in lectern's home"
eng rm -f "$PREFIX-rootless" >/dev/null

# ------------------------------------------------------ cannot drop root ---

nodrop="$(volume nodrop)"
prep "$nodrop:/data" -- 'chown 0:0 /data; touch /data/lectern.sqlite'
start nodrop --no-healthcheck --cap-drop SETUID --cap-drop SETGID -v "$nodrop:/data" -- id -u
expect_refused nodrop "cannot switch to uid 1000" "user:"
! grep -qx 0 <<<"$(logs_of "$PREFIX-nodrop")" || fail "cannot drop root: the command ran as root" "$PREFIX-nodrop"
[[ "$(prep "$nodrop:/data" -- 'stat -c %u:%g /data /data/lectern.sqlite')" == 0:0$'\n'0:0 ]] \
  || fail "cannot drop root: /data changed owner although Lectern could not start"
pass "cannot drop root: exit non-zero with what to do, nothing changed owner, the command never ran as root"

# ------------------------------------------- edges of the handover rule ---

# These containers run a short command instead of Lectern: the startup script
# is what is under test, and the command shows the ids it ended up with.

# Owned by uid 1000 is not enough: a direct child with the right uid and
# another gid is handed over too.
gidonly="$(volume gidonly)"
prep "$gidonly:/data" -- 'chown 1000:1000 /data; touch /data/lectern.sqlite; chown 1000:0 /data/lectern.sqlite'
start gid-only --no-healthcheck -v "$gidonly:/data" -- "${PRINT_IDS[@]}"
expect_ran_as_lectern gid-only
expect_handover gid-only
[[ "$(prep "$gidonly:/data" -- 'stat -c %u:%g /data/lectern.sqlite')" == 1000:1000 ]] \
  || fail "gid-only: a direct child owned by 1000:0 was not handed over"
pass "handover rule: a direct child owned by uid 1000 with another gid is handed over"

# The capabilities the README names for a cap_drop: ALL setup: CHOWN for a
# handover, SETUID, SETGID and SETPCAP to drop root. With those alone a
# root-owned volume is handed over and the command runs as lectern.
mincaps="$(volume mincaps)"
prep "$mincaps:/data" -- 'chown 0:0 /data; mkdir -p /data/servers/s1; touch /data/servers/s1/server.jar /data/lectern.sqlite'
start min-caps --no-healthcheck --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --cap-add SETPCAP -v "$mincaps:/data" -- "${PRINT_IDS[@]}"
expect_ran_as_lectern min-caps
expect_handover min-caps
[[ -z "$(prep "$mincaps:/data" -- 'find /data \( ! -uid 1000 -o ! -gid 1000 \) -print')" ]] \
  || fail "min-caps: entries under /data still not owned by 1000:1000"
pass "cap_drop ALL with CHOWN, SETUID, SETGID, SETPCAP added back: handed over, runs as 1000:1000"

# CHOWN is needed only for a handover: without it a volume with nothing to
# hand over still starts.
nochown="$(volume nochown)"
start no-chown --no-healthcheck --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
  -v "$nochown:/data" -- "${PRINT_IDS[@]}"
expect_ran_as_lectern no-chown
expect_no_handover no-chown
pass "cap_drop ALL with SETUID, SETGID, SETPCAP added back: nothing to hand over, runs as 1000:1000"

# A data folder that does not exist yet is created, and belongs to lectern.
nested="$(volume nested)"
start nested --no-healthcheck -e LECTERN_DATA=/data/new/lectern -v "$nested:/data" \
  -- sh -c 'touch "$LECTERN_DATA/written"; id -u; id -g'
expect_ran_as_lectern nested
[[ "$(prep "$nested:/data" -- 'stat -c %u:%g /data/new/lectern /data/new/lectern/written')" == 1000:1000$'\n'1000:1000 ]] \
  || fail "nested: the data folder the startup script created does not belong to 1000:1000"
pass "missing data folder: created, handed to 1000:1000, writable by the command"

# Under user: a data folder the id cannot create is refused with the id and
# the folder named (/srv belongs to root).
nofolder="$(volume nofolder)"
start no-folder --no-healthcheck --user 1234:1234 -e LECTERN_DATA=/srv/lectern -v "$nofolder:/data"
expect_refused no-folder "uid 1234" "/srv/lectern"
pass "user: override, data folder cannot be created: exit non-zero naming the id and the folder"

# ------------------------------------- a really interrupted handover ---

# The interrupted-handover check above fakes the state an interruption leaves.
# Here a real handover is interrupted: a tree deeper than the open-file limit
# makes the walk fail part-way, in the bind-mount shape (/data already 1000's).
# /data/deep is the only root-owned direct child, so the next start notices the
# unfinished handover only if that child is still root's. Deepest first means
# it is; a walk that changed a directory before its content would leave
# /data/deep handed over, its content root's, and no later start would notice.
deep="$(volume deep)"
prep "$deep:/data" -- '
  chown 1000:1000 /data
  d=/data/deep; i=0
  while [ "$i" -lt 150 ]; do d="$d/d"; i=$((i + 1)); done
  mkdir -p "$d"; touch "$d/level.dat"'
start cut-short --no-healthcheck --ulimit nofile=48:48 -v "$deep:/data"
expect_refused cut-short "could not hand the data folder /data over"
start cut-finished --no-healthcheck -v "$deep:/data"
wait_api cut-finished
expect_handover cut-finished
expect_all_lectern cut-finished
pass "interrupted handover (real interruption): the next start finishes it"
eng rm -f "$PREFIX-cut-finished" >/dev/null

say "all container checks passed"
