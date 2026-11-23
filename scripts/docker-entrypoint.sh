#!/bin/sh
# Startup script of the production image (its ENTRYPOINT). It runs the image's
# CMD (uvicorn) as the unprivileged user lectern (uid 1000, gid 1000), so
# Lectern and every Minecraft server it starts never run as root.
#
# Started as root (the default), it first hands the data folder over to
# lectern when the folder or one of its direct children belongs to anyone
# else: an install from before this image wrote everything as root. Then it
# drops root for good with setpriv and replaces itself with the command, so
# uvicorn stays process 1 and gets the stop signal directly.
#
# Started as another user (Docker's `user:`), it changes no ownership and only
# checks that the data folder is writable.
#
# Any failure stops the container with a message; no path runs the command as
# root.
set -eu

readonly LECTERN_UID=1000
readonly LECTERN_GID=1000
readonly DATA="${LECTERN_DATA:-/data}"

say() {
    printf 'docker-entrypoint: %s\n' "$*" >&2
}

fail() {
    say "$*"
    say "Lectern was not started."
    exit 1
}

# The walk that runs as root over a tree that mods and server jars could write
# to. It works on directory file descriptors, never on path names: no symbolic
# link is followed (a link gets its own owner changed, not its target), and a
# renamed parent cannot redirect it. It stays inside the data folder's own
# mount: an entry on another device, or any mount point inside the folder
# (a volume or bind mount there can share the device), is neither looked at
# nor changed.
#   check:    print "needed" when the folder or one of its direct children is
#             not owned by uid:gid, else "clean"
#   handover: change the owner of every entry that is not uid:gid, deepest
#             first, so the direct children change last and an interrupted
#             handover is seen and finished by the next start
# -I keeps PYTHON* variables, the user site and the working directory out of
# a program that runs as root.
ownership() {
    python3 -I - "$1" "$DATA" "$LECTERN_UID" "$LECTERN_GID" <<'PYTHON'
import os
import re
import stat
import sys

mode, top, uid, gid = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def foreign(st):
    return st.st_uid != uid or st.st_gid != gid


def mount_points(root):
    """Every mount point strictly inside root, from the kernel's mount table
    (which writes space, tab, newline and backslash as a backslash and three
    octal digits)."""
    prefix = os.fsencode(root.rstrip("/") + "/")
    with open("/proc/self/mountinfo", "rb") as table:
        points = {
            re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), line.split(b" ")[4])
            for line in table
        }
    return {os.fsdecode(point) for point in points if point.startswith(prefix)}


class Walk:
    def __init__(self, root, root_fd):
        self.root = root
        self.root_fd = root_fd
        self.device = os.fstat(root_fd).st_dev
        self.mounts = mount_points(root)

    def entries(self, dir_fd, path):
        """(name, lstat) of each entry of the directory open on dir_fd, without
        those on another device or mounted there."""
        with os.scandir(dir_fd) as it:
            found = [(entry.name, entry.stat(follow_symlinks=False)) for entry in it]
        return [
            (name, st)
            for name, st in found
            if st.st_dev == self.device and os.path.join(path, name) not in self.mounts
        ]

    def needed(self):
        return foreign(os.fstat(self.root_fd)) or any(
            foreign(st) for _, st in self.entries(self.root_fd, self.root)
        )

    def open_dir(self, name, parent_fd, path):
        fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        if os.fstat(fd).st_dev != self.device:
            os.close(fd)
            raise OSError(f"{path!r} moved to another filesystem during the handover")
        return fd

    def handover(self):
        # Iterative post-order walk: one open directory per level, no
        # recursion limit. Each frame is (fd, path, entries still to visit).
        stack = [(self.root_fd, self.root, self.entries(self.root_fd, self.root))]
        while stack:
            fd, path, pending = stack[-1]
            if pending:
                name, st = pending.pop()
                where = os.path.join(path, name)
                if stat.S_ISDIR(st.st_mode):
                    child = self.open_dir(name, fd, where)
                    stack.append((child, where, self.entries(child, where)))
                elif foreign(st):
                    try:
                        os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)
                    except OSError as exc:
                        raise OSError(f"cannot change the owner of {where!r}: {exc.strerror}")
                continue
            stack.pop()
            if foreign(os.fstat(fd)):
                try:
                    os.fchown(fd, uid, gid)
                except OSError as exc:
                    raise OSError(f"cannot change the owner of {path!r}: {exc.strerror}")
            os.close(fd)


try:
    walk = Walk(os.path.realpath(top), os.open(top, DIR_FLAGS))
    if mode == "check":
        print("needed" if walk.needed() else "clean")
    else:
        walk.handover()
except OSError as exc:
    print(f"docker-entrypoint: {exc}", file=sys.stderr)
    sys.exit(1)
PYTHON
}

if [ "$#" -eq 0 ]; then
    fail "no command to run."
fi

if [ "$(id -u)" -ne 0 ]; then
    # Docker's `user:` chose the id: its ownership is the admin's call.
    if [ ! -d "$DATA" ] && ! mkdir -p "$DATA" 2>/dev/null; then
        fail "running as uid $(id -u), gid $(id -g), which cannot create the data folder $DATA. Mount a volume or a folder writable by uid $(id -u) there."
    fi
    if [ ! -w "$DATA" ] || [ ! -x "$DATA" ]; then
        fail "running as uid $(id -u), gid $(id -g), which cannot write to the data folder $DATA. Make that folder writable by uid $(id -u) (for a bind mount: chown it on the host), or remove the user: setting so the container hands it over to uid $LECTERN_UID itself."
    fi
    exec "$@"
fi

# Root from here on: only long enough to hand the data folder over.
command -v setpriv >/dev/null 2>&1 || fail "setpriv is missing from the image, so root cannot be dropped."
home="$(getent passwd "$LECTERN_UID" | cut -d: -f6)"
[ -n "$home" ] || fail "the image has no user with uid $LECTERN_UID."

# Drop root for good: lectern's ids, no supplementary groups, no capability
# that a child could inherit or regain, and no_new_privs so no setuid binary
# leads back to root. setpriv exits non-zero without running the command when
# any of this fails (for example without CAP_SETUID, CAP_SETGID or
# CAP_SETPCAP); the dry run first turns that into a message that says what to
# do, before anything changes owner.
exec_as_lectern() {
    exec setpriv --reuid="$LECTERN_UID" --regid="$LECTERN_GID" --clear-groups \
        --inh-caps=-all --ambient-caps=-all --bounding-set=-all --no-new-privs \
        -- "$@"
}
if ! (exec_as_lectern true); then
    fail "cannot switch to uid $LECTERN_UID (see above): the container lacks the capabilities to change user (SETUID, SETGID, SETPCAP), for example because of cap_drop. Keep those, or set user: \"$LECTERN_UID:$LECTERN_GID\" so the container never starts as root."
fi

mkdir -p "$DATA" || fail "cannot create the data folder $DATA. Check the volume or bind mount."

if ! state="$(ownership check)"; then
    fail "cannot read the data folder $DATA (see above). Check the volume or bind mount."
fi
if [ "$state" = needed ]; then
    say "handing the data folder $DATA over to user lectern (uid $LECTERN_UID, gid $LECTERN_GID); this happens once and can take a while on a large folder."
    if ! ownership handover; then
        fail "could not hand the data folder $DATA over to uid $LECTERN_UID (see above). Mount it writable on a filesystem that allows ownership changes, chown it to $LECTERN_UID:$LECTERN_GID on the host, or set user: to an id that can already write to it."
    fi
    # Some filesystems report success for an ownership change they ignore
    # (FAT and exFAT mounted with the quiet option): look again rather than
    # trust that chown returned 0.
    if ! state="$(ownership check)"; then
        fail "cannot read the data folder $DATA after the handover (see above). Check the volume or bind mount."
    fi
    if [ "$state" != clean ]; then
        fail "the data folder $DATA still does not belong to uid $LECTERN_UID although every ownership change reported success: its filesystem ignores owners (FAT or exFAT, for example). Mount it with uid=$LECTERN_UID,gid=$LECTERN_GID, move the data to a filesystem that keeps owners, or set user: to the id that can write to it."
    fi
    say "the data folder $DATA now belongs to user lectern."
fi

# Owning the folder is not enough to use it (permission bits, an ACL, a
# read-only mount): ask as lectern itself, so a folder lectern cannot write
# stops the container here instead of failing inside Lectern.
if ! (exec_as_lectern test -w "$DATA") || ! (exec_as_lectern test -x "$DATA"); then
    fail "uid $LECTERN_UID cannot write to the data folder $DATA although it owns it. Make the folder writable by its owner (chmod u+rwx on the host) and mount it read-write."
fi

# HOME is lectern's own, as su would set it: root's is closed to lectern.
export HOME="$home"
exec_as_lectern "$@"
