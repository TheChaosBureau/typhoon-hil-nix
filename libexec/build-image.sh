#!/usr/bin/env bash
# Build and verify the Typhoon HIL container image from the vendor's offline
# installer. Normally reached as `typhoon-hil build-image`.
#
#   typhoon-hil build-image --installers ~/typhoon-installers
#   typhoon-hil build-image --installers ~/typhoon-installers --save /var/lib/typhoon-vhil
#
# The only input is the `*.gzip.run` installer. Licenses are neither read nor
# copied: they are supplied at run time, never to a build.
#
# What it does, in order:
#   1. resolve and version-check the installer (clear failure, never a 2026.2
#      image wearing a 2026.3 tag);
#   2. assemble a two-file build context in a temp dir and assert nothing
#      license- or installer-shaped is in it;
#   3. `podman build`, bind-mounting the installer directory read-only so the
#      multi-gigabyte installer never enters a layer;
#   4. run the image contract checks (check-image.sh);
#   5. with --save, `podman save` an archive for a CI runner, scan its layers
#      for a license, and record its size and sha256.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${TYPHOON_HIL_ROOT:=$(cd "$SCRIPT_DIR/.." && pwd)}"
export TYPHOON_HIL_ROOT
# shellcheck source=lib/contract.sh
. "$TYPHOON_HIL_ROOT/lib/contract.sh"

INSTALLER_DIR="${TYPHOON_INSTALLER_DIR:-}"
save_dir=""
do_check=1
do_smoke=0
license_v2="${TYPHOON_LICENSE_V2:-}"
license_v3="${TYPHOON_LICENSE_V3:-}"

usage() {
    cat <<USAGE
usage: typhoon-hil build-image --installers DIR [options]

  --installers DIR   directory holding the offline *.gzip.run installer
                     (default: \$TYPHOON_INSTALLER_DIR). Keep license files
                     out of it: the whole directory is mounted into the build.
  --save DIR         also write $TYPHOON_IMAGE_TAR_NAME, its sha256 and a
                     manifest to DIR, for a CI runner to \`podman load\`
  --tag TAG          image tag to build (default: $TYPHOON_IMAGE)
  --with-typhoonsim  also install TyphoonSim (bigger image; Virtual HIL does
                     not need it)
  --smoke            after building, run the Virtual HIL smoke gate; needs
                     --license-v2 and --license-v3
  --license-v2 FILE  license file for --smoke
  --license-v3 FILE  license file for --smoke
  --no-check         skip the image contract checks
  -h, --help         this message
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --installers) INSTALLER_DIR="$2"; shift 2 ;;
        --save) save_dir="$2"; shift 2 ;;
        --tag) TYPHOON_IMAGE="$2"; shift 2 ;;
        --with-typhoonsim) TYPHOON_INSTALL_TYPHOONSIM=1; shift ;;
        --smoke) do_smoke=1; shift ;;
        --license-v2) license_v2="$2"; shift 2 ;;
        --license-v3) license_v3="$2"; shift 2 ;;
        --no-check) do_check=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v podman >/dev/null || typhoon_die "podman is not installed"

# ---- 1. installers ---------------------------------------------------------

[ -n "$INSTALLER_DIR" ] || { usage >&2; typhoon_die "no --installers directory given"; }
[ -d "$INSTALLER_DIR" ] || typhoon_die "installer directory $INSTALLER_DIR does not exist"

# shellcheck disable=SC2012,SC2086  # the glob is meant to expand; names are vendor-fixed
cc_installer="$(ls -1 "$INSTALLER_DIR"/$TYPHOON_CC_INSTALLER_GLOB 2>/dev/null | sort -V | tail -n1 || true)"
if [ -z "$cc_installer" ]; then
    # shellcheck disable=SC2012
    typhoon_die "no Control Center installer matching $TYPHOON_CC_INSTALLER_GLOB in $INSTALLER_DIR
       found instead: $(ls -1 "$INSTALLER_DIR" 2>/dev/null | tr '\n' ' ')"
fi
case "$(basename "$cc_installer")" in
    *"_v${TYPHOON_VERSION}"*) ;;
    *) typhoon_die "installer $(basename "$cc_installer") is not version $TYPHOON_VERSION
       Building it would produce an image tagged $TYPHOON_IMAGE that does not
       contain $TYPHOON_VERSION. Set TYPHOON_VERSION to the release you
       actually have (the tag and archive name follow it)." ;;
esac
typhoon_say "Control Center installer: $(basename "$cc_installer")"

if [ "$TYPHOON_INSTALL_TYPHOONSIM" = "1" ]; then
    # shellcheck disable=SC2012,SC2086
    sim_installer="$(ls -1 "$INSTALLER_DIR"/$TYPHOON_SIM_INSTALLER_GLOB 2>/dev/null | sort -V | tail -n1 || true)"
    [ -n "$sim_installer" ] || typhoon_die "--with-typhoonsim but no installer matching $TYPHOON_SIM_INSTALLER_GLOB in $INSTALLER_DIR"
    typhoon_say "TyphoonSim installer: $(basename "$sim_installer")"
fi

# The installer directory is mounted into the build whole. Nothing copies a
# license out of it, but "the build never sees a license" should be true rather
# than merely harmless, so say so when one is sitting there.
stray_license="$(find "$INSTALLER_DIR" -maxdepth 1 -name '*.lic' -print 2>/dev/null || true)"
[ -z "$stray_license" ] || typhoon_warn "license file(s) in the installer directory are visible to the build:
$stray_license
       Move the installer into a directory of its own."

# ---- 2. build context ------------------------------------------------------
#
# A temp directory holding exactly the Containerfile and the entrypoint, so
# nothing else -- above all, no license -- can ride into a layer.

context="$(mktemp -d -t typhoon-build-ctx.XXXXXX)"
cleanup() { rm -rf "$context"; }
trap cleanup EXIT

cp "$TYPHOON_HIL_ROOT/image/Containerfile" "$TYPHOON_HIL_ROOT/image/container-entrypoint.sh" "$context/"
chmod u+w "$context"/*

stray="$(find "$context" \( -name '*.lic' -o -name '*.gzip.run' \) -print 2>/dev/null || true)"
[ -z "$stray" ] || typhoon_die "build context is contaminated:
$stray"
typhoon_say "build context: $context ($(find "$context" -type f | wc -l) files, $(du -sh "$context" | cut -f1))"

# ---- 3. build --------------------------------------------------------------

typhoon_say "building $TYPHOON_IMAGE (the Control Center install takes several minutes)"
podman build \
    --file "$context/Containerfile" \
    --tag "$TYPHOON_IMAGE" \
    --volume "$INSTALLER_DIR:$TYPHOON_INSTALLER_MOUNT:ro" \
    --build-arg "TYPHOON_BASE_IMAGE=$TYPHOON_BASE_IMAGE" \
    --build-arg "TYPHOON_VERSION=$TYPHOON_VERSION" \
    --build-arg "TYPHOON_INSTALLER_MOUNT=$TYPHOON_INSTALLER_MOUNT" \
    --build-arg "TYPHOON_CC_INSTALLER_GLOB=$TYPHOON_CC_INSTALLER_GLOB" \
    --build-arg "TYPHOON_SIM_INSTALLER_GLOB=$TYPHOON_SIM_INSTALLER_GLOB" \
    --build-arg "TYPHOON_INSTALL_TYPHOONSIM=$TYPHOON_INSTALL_TYPHOONSIM" \
    --build-arg "TYPHOON_CONTAINER_HOME=$TYPHOON_CONTAINER_HOME" \
    --build-arg "TYPHOON_LICENSE_SUBPATH=$TYPHOON_LICENSE_SUBPATH" \
    --build-arg "TYPHOON_CONTAINER_WORKDIR=$TYPHOON_CONTAINER_WORKDIR" \
    --build-arg "TYPHOON_BUILD_INFO_PATH=$TYPHOON_BUILD_INFO_PATH" \
    --build-arg "TYPHOON_FORBIDDEN_LICENSE_NAMES=$TYPHOON_FORBIDDEN_LICENSE_NAMES" \
    "$context"

image_id="$(podman image inspect --format '{{.Id}}' "$TYPHOON_IMAGE")"
image_bytes="$(podman image inspect --format '{{.Size}}' "$TYPHOON_IMAGE")"
typhoon_say "built $TYPHOON_IMAGE ($image_id), on-disk size $(numfmt --to=iec "$image_bytes")"

# ---- 4. contract checks ----------------------------------------------------

if [ "$do_check" = "1" ]; then
    "$SCRIPT_DIR/check-image.sh" --image "$TYPHOON_IMAGE"
else
    typhoon_warn "--no-check: image contract not verified"
fi

# ---- 5. save, size, checksum ----------------------------------------------

if [ -n "$save_dir" ]; then
    mkdir -p "$save_dir"
    archive="$save_dir/$TYPHOON_IMAGE_TAR_NAME"
    typhoon_say "podman save -> $archive"
    # `podman save` into an existing docker-archive fails with
    #   Error: docker-archive doesn't support modifying existing images
    # which says nothing about the real cause. Write to a temp name and move it
    # into place: the destination is replaced atomically, and a failed save
    # cannot leave a truncated archive wearing the real name.
    rm -f "$archive.partial"
    podman save --output "$archive.partial" "$TYPHOON_IMAGE"
    mv -f "$archive.partial" "$archive"
    archive_bytes="$(stat -c %s "$archive")"
    typhoon_say "computing sha256 of $(numfmt --to=iec "$archive_bytes")..."
    archive_sha="$(sha256sum "$archive" | cut -d' ' -f1)"

    if [ "$do_check" = "1" ]; then
        "$SCRIPT_DIR/check-image.sh" --archive "$archive"
    fi

    manifest="$save_dir/${TYPHOON_IMAGE_TAR_NAME%.tar}.manifest.json"
    printf '{\n  "image": "%s",\n  "image_id": "%s",\n  "image_bytes": %s,\n  "archive": "%s",\n  "archive_bytes": %s,\n  "archive_sha256": "%s",\n  "typhoon_version": "%s",\n  "typhoonsim_installed": %s,\n  "cc_installer": "%s",\n  "built_utc": "%s",\n  "built_on": "%s"\n}\n' \
        "$TYPHOON_IMAGE" "$image_id" "$image_bytes" \
        "$(basename "$archive")" "$archive_bytes" "$archive_sha" \
        "$TYPHOON_VERSION" \
        "$([ "$TYPHOON_INSTALL_TYPHOONSIM" = 1 ] && echo true || echo false)" \
        "$(basename "$cc_installer")" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(uname -n)" \
        > "$manifest"
    printf '%s  %s\n' "$archive_sha" "$(basename "$archive")" > "$archive.sha256"
    typhoon_say "archive  $archive ($(numfmt --to=iec "$archive_bytes"))"
    typhoon_say "checksum $archive.sha256"
    typhoon_say "manifest $manifest"
    typhoon_say "\`typhoon-hil ci\` looks for it on the license media, then in $TYPHOON_LOCAL_ARCHIVE_DIR"
fi

# ---- 6. optional local smoke ----------------------------------------------

if [ "$do_smoke" = "1" ]; then
    [ -r "$license_v2" ] || typhoon_die "--smoke needs a readable --license-v2"
    [ -r "$license_v3" ] || typhoon_die "--smoke needs a readable --license-v3"
    typhoon_say "running the Virtual HIL smoke gate against the freshly built image"
    TYPHOON_LICENSE_V2="$license_v2" TYPHOON_LICENSE_V3="$license_v3" \
        "$SCRIPT_DIR/check-image.sh" --image "$TYPHOON_IMAGE" --smoke
fi

typhoon_say "done."
