#!/usr/bin/env bash
# Entrypoint for typhoon-hil:<version>.
#
# Control Center is a Qt application: even the API-only paths used by the
# smoke gate start CC processes that want an X display. This brings up a
# throwaway Xvfb, checks the three things that silently break a licensed
# run under `--userns=keep-id`, and then gets out of the way with
# `exec "$@"` so the caller's command (and its arguments) arrive intact.
#
# Everything here fails loudly. A VHIL job that dies at layer L2 with
# "Control Center down?" is nearly unattributable from CI logs; a job that
# dies here says which precondition was missing.

set -euo pipefail

say()  { printf '[typhoon-entrypoint] %s\n' "$*"; }
warn() { printf '[typhoon-entrypoint] WARNING: %s\n' "$*" >&2; }
die()  { printf '[typhoon-entrypoint] ERROR: %s\n' "$*" >&2; exit 1; }

: "${HOME:=/tmp/typhoon-home}"
: "${DISPLAY:=:99}"
: "${TYPHOONPATH:=/opt/typhoon/current}"
: "${TYPHOON_LICENSE_SUBPATH:=.local/share/typhoon/license}"
: "${TYPHOON_XVFB_TIMEOUT:=20}"
export HOME DISPLAY TYPHOONPATH

# ---- 1. HOME must be writable ---------------------------------------------
#
# The image ships a world-writable skeleton. If HOME was redirected to a
# path podman created as a bind-mount parent, it is root-owned 0755 and the
# keep-id uid cannot write there -- which surfaces much later as a Control
# Center startup failure.
mkdir -p "$HOME" 2>/dev/null || true
if ! ( : > "$HOME/.typhoon-writable-probe" ) 2>/dev/null; then
    die "\$HOME ($HOME) is not writable by uid $(id -u).
       Control Center writes settings and logs there. Either keep
       HOME=/tmp/typhoon-home (the image ships it world-writable) or
       point HOME at a directory the container uid owns."
fi
rm -f "$HOME/.typhoon-writable-probe"

# ---- 1b. The per-uid runtime directory Control Center locks in --------------
#
# Control Center's process mutex opens `/var/run/user/<uid>/config.lock` and
# ignores XDG_RUNTIME_DIR. The image ships /run/user
# world-writable precisely so this uid -- unknown until now, under
# `--userns=keep-id` -- can make its own subdirectory. Fail loudly here: the
# alternative is Control Center dying at startup and the smoke reporting five
# layer timeouts that say nothing about the cause.
runtime_dir="/run/user/$(id -u)"
if ! mkdir -p "$runtime_dir" 2>/dev/null; then
    die "cannot create $runtime_dir (uid $(id -u)).
       Control Center locks \$runtime_dir/config.lock at startup and does not
       honour XDG_RUNTIME_DIR. The image ships /run/user mode 0777 for exactly
       this; a /run mounted over at run time would defeat it."
fi
chmod 0700 "$runtime_dir" 2>/dev/null || true
export XDG_RUNTIME_DIR="$runtime_dir"

# ---- 2. Licenses: mounted read-only, staged writable ------------------------
#
# Control Center opens its license file for WRITING. Mounted `:ro` -- which is
# how this image shipped -- `sch.load()` succeeds and `sch.compile()` fails with
#   License file (...instance_license_v3.lic) is not writable.
# and produces a Target files directory with no .cpd, which reads like a broken
# compiler rather than a permissions problem.
#
# So the operator's files arrive read-only under $TYPHOON_LICENSE_STAGE and are
# copied here to the path Control Center wants, writable. The host's own files
# stay immutable, nothing license-shaped is ever in a layer, and the writable
# copy dies with the container.
#
# Still a warning and not an error: `render_schematic.py` needs no license, so a
# license-free run is legitimate. `typhoon-hil ci` hard-fails on missing
# licenses before it ever starts a container.
: "${TYPHOON_LICENSE_STAGE:=/licenses}"
license_dir="$HOME/$TYPHOON_LICENSE_SUBPATH"
mkdir -p "$license_dir" 2>/dev/null || true
missing_licenses=()
for v in 2 3; do
    staged="$TYPHOON_LICENSE_STAGE/license_v${v}.lic"
    target="$license_dir/instance_license_v${v}.lic"
    if [ -s "$staged" ]; then
        if ! cp -f "$staged" "$target" 2>/dev/null; then
            die "could not stage $staged -> $target
       Control Center needs a WRITABLE license file; \$HOME must be writable by
       uid $(id -u) (the image ships $HOME world-writable for this reason)."
        fi
        chmod u+rw "$target" 2>/dev/null || true
    elif [ -s "$target" ]; then
        # Someone mounted or placed it directly on the old path. Honour it, but
        # it must be writable or the compile will fail later for no clear reason.
        [ -w "$target" ] || warn "$target is not writable.
         Control Center writes to its license file; a :ro mount here makes every
         compile fail. Mount into $TYPHOON_LICENSE_STAGE instead."
    else
        missing_licenses+=("v$v")
    fi
done
if [ "${#missing_licenses[@]}" -gt 0 ]; then
    warn "no license staged for: ${missing_licenses[*]}
         expected read-only bind mounts at $TYPHOON_LICENSE_STAGE/license_v<N>.lic
         (schematic rendering still works; Virtual HIL will not)"
fi

# ---- 3. Headless display ---------------------------------------------------
if [ "${TYPHOON_NO_XVFB:-0}" = "1" ]; then
    say "TYPHOON_NO_XVFB=1; not starting Xvfb (DISPLAY=$DISPLAY)"
elif xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    say "display $DISPLAY already available; not starting Xvfb"
else
    say "starting Xvfb on $DISPLAY"
    Xvfb "$DISPLAY" -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    xvfb_pid=$!
    deadline=$(( SECONDS + TYPHOON_XVFB_TIMEOUT ))
    until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
        if ! kill -0 "$xvfb_pid" 2>/dev/null; then
            warn "Xvfb exited; log follows"
            cat /tmp/xvfb.log >&2 || true
            die "Xvfb died before $DISPLAY came up"
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            die "Xvfb did not bring up $DISPLAY within ${TYPHOON_XVFB_TIMEOUT}s"
        fi
        sleep 0.2
    done
    say "display $DISPLAY ready (Xvfb pid $xvfb_pid)"
fi

exec "$@"
