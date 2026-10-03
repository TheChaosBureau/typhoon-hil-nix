# shellcheck shell=bash
#
# Single source of truth for the Typhoon HIL container contract.
#
# Four things have to agree, or a run fails in a way that looks like a broken
# model or a hardware problem: the image *builder* (libexec/build-image.sh),
# the image *checker* (libexec/check-image.sh), the *launcher* (bin/typhoon-hil)
# and the Containerfile. Sourcing this file is what makes them agree by
# construction rather than by coincidence.
#
# Every knob uses `: "${NAME:=default}"` so the caller's environment wins and
# this file only supplies the default. Source it, don't execute it.

# ---- versions and names ----------------------------------------------------

: "${TYPHOON_VERSION:=2026.3}"
: "${TYPHOON_BASE_IMAGE:=ubuntu:24.04}"
: "${TYPHOON_IMAGE:=typhoon-hil:${TYPHOON_VERSION}}"
: "${TYPHOON_IMAGE_TAR_NAME:=typhoon-hil-${TYPHOON_VERSION}.tar}"

# Installer file names as the vendor ships them. Globs, because service packs
# append a suffix (`..._v2026.1_sp1.gzip.run`); the version guard in
# build-image.sh is what actually pins the release.
: "${TYPHOON_CC_INSTALLER_GLOB:=typhoon_hil_control_center_v*.gzip.run}"
: "${TYPHOON_SIM_INSTALLER_GLOB:=typhoonsim_v*.gzip.run}"

# TyphoonSim is the standalone circuit simulator. Virtual HIL does not need it,
# and it makes the image bigger, so it is off by default.
: "${TYPHOON_INSTALL_TYPHOONSIM:=0}"

# ---- paths inside the image ------------------------------------------------

# HOME is a fixed, world-writable path rather than a real user's home: a CI run
# uses `--userns=keep-id`, so the uid inside the container is the caller's and
# is not known at build time. The workstation launcher replaces it with a
# persistent host directory.
: "${TYPHOON_CONTAINER_HOME:=/tmp/typhoon-home}"
: "${TYPHOON_LICENSE_SUBPATH:=.local/share/typhoon/license}"

# Read-only mount point for the operator's license files. See
# typhoon_stage_license_path() for why they are not mounted onto $HOME directly.
: "${TYPHOON_LICENSE_STAGE:=/licenses}"
: "${TYPHOON_CONTAINER_WORKDIR:=/work}"
: "${TYPHOON_PYTHON:=python3}"
: "${TYPHOON_INSTALLER_MOUNT:=/installers}"
: "${TYPHOON_BUILD_INFO_PATH:=/opt/typhoon/image-build.json}"

# Where the launcher mounts this tool's own helper scripts, read-only, in every
# container it starts. The smoke test lives there, so a consumer can gate on it
# without vendoring a copy:  python3 "$TYPHOON_HIL_SMOKE"
: "${TYPHOON_TOOL_MOUNT:=/opt/typhoon-hil}"

# License file names that must never end up in an image layer. Vendor `.lic`
# files that ship inside Control Center are reported but tolerated; these are
# the operator's own licenses and are a hard failure.
: "${TYPHOON_FORBIDDEN_LICENSE_NAMES:=license_v2.lic license_v3.lic instance_license_v2.lic instance_license_v3.lic}"

# ---- operator media (CI mode) ----------------------------------------------

# Read-only removable media carrying the licenses, and the image archive when
# it fits. Resolution tries each candidate and prefers whichever actually holds
# the archive, so two differently labelled drives never force a guess.
: "${TYPHOON_MEDIA_CANDIDATES:=/run/media/Typhoon /run/media/TYPHOON-RUNNER}"

# Where a locally staged image archive lives when it is too big for the media
# (the 2026.3 archive is about 9 GB).
#
# This is safe in a way the licenses are not: scan_image_archive.py proves no
# license is in any layer, so the archive carries nothing secret and may sit on
# the runner's own disk. The licenses still come from the read-only media.
#
# Keep it OUTSIDE any CI runner work directory: runners wipe that tree, and an
# archive cached there would not survive a restart.
: "${TYPHOON_LOCAL_ARCHIVE_DIR:=/var/lib/typhoon-vhil}"

# ---- helpers ---------------------------------------------------------------

typhoon_die() {
    printf 'typhoon-hil: prerequisite missing: %s\n' "$*" >&2
    exit 1
}

typhoon_say() { printf '\033[1;34m[typhoon]\033[0m %s\n' "$*"; }
typhoon_warn() { printf '\033[1;33m[typhoon]\033[0m %s\n' "$*" >&2; }

# Absolute path of the licenses as Control Center reads them in the container.
typhoon_container_license_path() {
    printf '%s/%s/instance_license_v%s.lic' \
        "$TYPHOON_CONTAINER_HOME" "$TYPHOON_LICENSE_SUBPATH" "$1"
}

# Where the operator's read-only license files are MOUNTED. They are not
# mounted straight onto the path above, because Control Center opens its
# license file for writing and a `:ro` mount makes every compile fail with
#   compile() error: License file (...instance_license_v3.lic) is not writable.
# while `load()` still succeeds -- a failure that reads like a broken compiler.
#
# So the host files are mounted read-only HERE and the entrypoint copies them to
# the path Control Center wants, writable. The operator's own files therefore
# cannot be modified by anything in the container, and nothing license-shaped
# enters a layer. The writable copy lives and dies with the container.
typhoon_stage_license_path() {
    printf '%s/license_v%s.lic' "$TYPHOON_LICENSE_STAGE" "$1"
}

# Echo the media directory to use. Explicit TYPHOON_MEDIA_DIR always wins.
# Otherwise prefer a candidate holding the image archive, then any that exists.
# Never guesses silently: an empty result is a hard failure naming every
# candidate tried.
typhoon_resolve_media_dir() {
    if [ -n "${TYPHOON_MEDIA_DIR:-}" ]; then
        printf '%s' "$TYPHOON_MEDIA_DIR"
        return 0
    fi
    local candidate first_existing=""
    for candidate in $TYPHOON_MEDIA_CANDIDATES; do
        [ -d "$candidate" ] || continue
        [ -n "$first_existing" ] || first_existing="$candidate"
        if [ -r "$candidate/$TYPHOON_IMAGE_TAR_NAME" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    if [ -n "$first_existing" ]; then
        printf '%s' "$first_existing"
        return 0
    fi
    typhoon_die "none of the candidate media directories are mounted:
       $TYPHOON_MEDIA_CANDIDATES
       (attach the license media, or set TYPHOON_MEDIA_DIR)"
}

# Assert a directory is a read-only mount, so a CI job can never write to the
# operator's license media.
typhoon_require_ro_mount() {
    local dir="$1" options
    options="$(findmnt -rn -T "$dir" -o OPTIONS 2>/dev/null || true)"
    case ",$options," in
        *,ro,*) return 0 ;;
    esac
    # Overlay and friends carry kilobytes of lowerdir= in their options; show
    # only the bare flags so the error stays readable in a CI log.
    local flags
    flags="$(printf '%s' "$options" | tr ',' '\n' | grep -v '=' | paste -sd, - || true)"
    typhoon_die "$dir is not a read-only mount (flags: ${flags:-unknown})"
}

# `--userns=keep-id` is rootless-only; rootful podman rejects it outright.
# Emitting it conditionally lets an operator verify a freshly built image as
# root on a build box while CI keeps the rootless mapping it needs.
typhoon_userns_args() {
    local rootless
    rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo unknown)"
    if [ "$rootless" = "true" ]; then
        printf '%s' "--userns=keep-id"
    fi
}

# Run a command inside the licensed image the way CI does: a throwaway home,
# the working tree at $TYPHOON_CONTAINER_WORKDIR, licenses staged read-only.
#
#   typhoon_run_in_image <image> <workdir> <license_v2> <license_v3> \
#       [podman run options...] -- cmd...
#
# The `ci` command and the image checker both go through this, so "what CI
# runs" and "what the builder verified" are one invocation and cannot drift.
typhoon_run_in_image() {
    local image="$1" workdir="$2" lic2="$3" lic3="$4"
    shift 4
    local -a extra=()
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do
        extra+=("$1")
        shift
    done
    [ "${1:-}" = "--" ] && shift
    local userns
    userns="$(typhoon_userns_args)"
    # shellcheck disable=SC2086  # $userns is one optional flag, intentionally split
    podman run --rm \
        $userns \
        --volume "$workdir:$TYPHOON_CONTAINER_WORKDIR:rw" \
        --volume "$lic2:$(typhoon_stage_license_path 2):ro" \
        --volume "$lic3:$(typhoon_stage_license_path 3):ro" \
        --volume "$TYPHOON_HIL_ROOT/libexec:$TYPHOON_TOOL_MOUNT:ro" \
        --env "TYPHOON_HIL_SMOKE=$TYPHOON_TOOL_MOUNT/smoke_test.py" \
        --workdir "$TYPHOON_CONTAINER_WORKDIR" \
        --env "HOME=$TYPHOON_CONTAINER_HOME" \
        "${extra[@]}" \
        "$image" "$@"
}
