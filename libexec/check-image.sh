#!/usr/bin/env bash
# Verify a built Typhoon HIL image against the contract the launcher assumes.
# Normally reached as `typhoon-hil check-image`.
#
#   typhoon-hil check-image --image typhoon-hil:2026.3
#   typhoon-hil check-image --archive /var/lib/typhoon-vhil/typhoon-hil-2026.3.tar
#   typhoon-hil check-image --image typhoon-hil:2026.3 --smoke \
#       --license-v2 /path/license_v2.lic --license-v3 /path/license_v3.lic
#
# The point is to fail *here*, on the build machine, rather than on a CI runner
# three days later with a log that says "Control Center down?". Each check
# names the thing that is wrong and where it is configured.
#
# --image   checks the image: provenance file, Python API, TYPHOONPATH, the
#           writable HOME skeleton, an empty license directory, no operator
#           license in the merged filesystem, and that the entrypoint passes
#           arguments through the way `typhoon-hil ci` calls it.
# --archive checks the shipped artifact: every layer, for a license that was
#           added and later deleted (invisible to --image, still in the tar).
# --smoke   runs the real gate: the L5 Virtual HIL compile/load/start/read/
#           stop on Control Center's bundled basic model. Needs licenses.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${TYPHOON_HIL_ROOT:=$(cd "$SCRIPT_DIR/.." && pwd)}"
export TYPHOON_HIL_ROOT
# shellcheck source=lib/contract.sh
. "$TYPHOON_HIL_ROOT/lib/contract.sh"

image=""
archive=""
do_smoke=0
license_v2="${TYPHOON_LICENSE_V2:-}"
license_v3="${TYPHOON_LICENSE_V3:-}"
failures=0

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image) image="$2"; shift 2 ;;
        --archive) archive="$2"; shift 2 ;;
        --smoke) do_smoke=1; shift ;;
        --license-v2) license_v2="$2"; shift 2 ;;
        --license-v3) license_v3="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

# No target given: check the image the contract names.
if [ -z "$image" ] && [ -z "$archive" ]; then
    image="$TYPHOON_IMAGE"
fi

pass() { printf '  \033[1;32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*" >&2; failures=$((failures + 1)); }

# ---- image checks ----------------------------------------------------------

if [ -n "$image" ]; then
    command -v podman >/dev/null || typhoon_die "podman is not installed"
    podman image exists "$image" || typhoon_die "image $image does not exist locally"

    typhoon_say "checking image $image against the contract"

    entrypoint="$(podman image inspect --format '{{json .Config.Entrypoint}}' "$image")"
    case "$entrypoint" in
        *typhoon-entrypoint*) pass "entrypoint is the typhoon wrapper ($entrypoint)" ;;
        *) fail "entrypoint is $entrypoint, expected the typhoon-entrypoint wrapper" ;;
    esac

    workdir="$(podman image inspect --format '{{.Config.WorkingDir}}' "$image")"
    if [ "$workdir" = "$TYPHOON_CONTAINER_WORKDIR" ]; then
        pass "workdir is $workdir"
    else
        fail "workdir is '$workdir', contract says $TYPHOON_CONTAINER_WORKDIR"
    fi

    # One container, every filesystem-level assertion. Runs through the real
    # entrypoint so a broken Xvfb bring-up fails here too.
    probe_output="$(podman run --rm \
        --env "HOME=$TYPHOON_CONTAINER_HOME" \
        "$image" \
        bash -euo pipefail -c '
            printf "argv-passthrough=%s,%s\n" "$1" "$2"
            printf "build-info=%s\n" "$(test -r "$3" && echo present || echo missing)"
            printf "typhoonpath=%s\n" "$(test -d "$TYPHOONPATH" && echo ok || echo missing)"
            printf "api-install=%s\n" "$(ls -1 "$TYPHOONPATH"/api_install/typhoon_hil_api-*.whl >/dev/null 2>&1 && echo ok || echo missing)"
            printf "basic-model=%s\n" "$(ls -1 /opt/typhoon/typhoon_hil_control_center_*/examples/tests/10_basic_model/model.tse >/dev/null 2>&1 && echo ok || echo missing)"
            printf "python=%s\n" "$(command -v python3 || echo missing)"
            printf "api-version=%s\n" "$(python3 -c "from importlib.metadata import version; print(version(\"Typhoon-HIL-API\"))" 2>/dev/null || echo missing)"
            printf "api-import=%s\n" "$(python3 -c "import typhoon.api" >/dev/null 2>&1 && echo ok || echo failed)"
            printf "home-mode=%s\n" "$(stat -c %a "$HOME" 2>/dev/null || echo missing)"
            printf "license-dir-mode=%s\n" "$(stat -c %a "$HOME/$4" 2>/dev/null || echo missing)"
            printf "license-dir-entries=%s\n" "$(find "$HOME/$4" -type f 2>/dev/null | wc -l)"
            printf "display=%s\n" "$(xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && echo ok || echo missing)"
            hits=""
            for name in $5; do
                found="$(find / -xdev -name "$name" -type f 2>/dev/null || true)"
                [ -z "$found" ] || hits="$hits $found"
            done
            printf "operator-licenses=%s\n" "${hits:-none}"
        ' bash first second "$TYPHOON_BUILD_INFO_PATH" "$TYPHOON_LICENSE_SUBPATH" \
          "$TYPHOON_FORBIDDEN_LICENSE_NAMES" 2>&1 || true)"

    probe() { printf '%s\n' "$probe_output" | sed -n "s/^$1=//p" | tail -n1; }

    if [ "$(probe argv-passthrough)" = "first,second" ]; then
        pass "entrypoint passes argv through (\`typhoon-hil ci\` calls it that way)"
    else
        fail "entrypoint mangles argv; got '$(probe argv-passthrough)', expected 'first,second'
        full container output:
$probe_output"
    fi
    [ "$(probe build-info)" = "present" ] \
        && pass "provenance file $TYPHOON_BUILD_INFO_PATH present" \
        || fail "no provenance file at $TYPHOON_BUILD_INFO_PATH"
    [ "$(probe typhoonpath)" = "ok" ] \
        && pass "TYPHOONPATH resolves to a directory" \
        || fail "TYPHOONPATH does not resolve inside the image"
    [ "$(probe api-install)" = "ok" ] \
        && pass "api_install/ wheel present under TYPHOONPATH" \
        || fail "no typhoon_hil_api wheel under TYPHOONPATH/api_install/"
    [ "$(probe basic-model)" = "ok" ] \
        && pass "CC-bundled 10_basic_model/model.tse present (L5 needs it)" \
        || fail "no 10_basic_model/model.tse; smoke_test.py L5 would SKIP, not run"
    [ "$(probe python)" != "missing" ] \
        && pass "$TYPHOON_PYTHON on PATH at $(probe python)" \
        || fail "$TYPHOON_PYTHON is not on PATH"
    [ "$(probe api-import)" = "ok" ] \
        && pass "import typhoon.api works (version $(probe api-version))" \
        || fail "import typhoon.api failed inside the image"
    case "$(probe home-mode)" in
        777) pass "\$HOME ($TYPHOON_CONTAINER_HOME) is world-writable (keep-id needs it)" ;;
        missing) fail "\$HOME ($TYPHOON_CONTAINER_HOME) does not exist in the image" ;;
        *) fail "\$HOME mode is $(probe home-mode), not 777; an arbitrary keep-id uid could not write there" ;;
    esac
    case "$(probe license-dir-mode)" in
        777) pass "license directory pre-created world-writable" ;;
        missing) fail "license directory $TYPHOON_CONTAINER_HOME/$TYPHOON_LICENSE_SUBPATH missing; podman would create it root-owned and \$HOME would be unwritable" ;;
        *) fail "license directory mode is $(probe license-dir-mode), not 777" ;;
    esac
    [ "$(probe license-dir-entries)" = "0" ] \
        && pass "license directory ships empty" \
        || fail "license directory ships $(probe license-dir-entries) file(s); licenses must be mounted, not baked"
    [ "$(probe display)" = "ok" ] \
        && pass "entrypoint brought up a headless display" \
        || fail "no X display inside the container; Control Center will not start"
    if [ "$(probe operator-licenses)" = "none" ]; then
        pass "no operator license in the merged filesystem"
    else
        fail "operator license present in the image: $(probe operator-licenses)"
    fi
fi

# ---- archive checks --------------------------------------------------------

if [ -n "$archive" ]; then
    [ -r "$archive" ] || typhoon_die "$archive is not readable"
    typhoon_say "checking archive $archive"
    bytes="$(stat -c %s "$archive")"
    printf '  size    %s (%s bytes)\n' "$(numfmt --to=iec "$bytes")" "$bytes"
    # Three outcomes, not two. The scanner exits 1 for "found a license" and 2
    # for "could not read the layers"; collapsing both into the first reports a
    # tooling failure as a license leak, which sends the operator hunting for a
    # file that is not there. An unreadable archive is UNPROVEN, not clean.
    scan_rc=0
    forbid_args=()
    for n in $TYPHOON_FORBIDDEN_LICENSE_NAMES; do forbid_args+=(--forbid "$n"); done
    python3 "$SCRIPT_DIR/scan_image_archive.py" "$archive" \
        --report-other-lic "${forbid_args[@]}" \
        || scan_rc=$?
    case "$scan_rc" in
        0) pass "no operator license in any layer" ;;
        1) fail "operator license found in an image layer (see scan output above)" ;;
        *) fail "archive layer scan could not read $archive (scanner exit $scan_rc) -- UNPROVEN, not clean" ;;
    esac
fi

# ---- optional live smoke ---------------------------------------------------

if [ "$do_smoke" = "1" ]; then
    [ -n "$image" ] || typhoon_die "--smoke needs --image"
    [ -r "$license_v2" ] || typhoon_die "--smoke needs a readable license: --license-v2"
    [ -r "$license_v3" ] || typhoon_die "--smoke needs a readable license: --license-v3"

    # A scratch working tree: the smoke compiles a copy of Control Center's own
    # bundled model, so it needs nothing from any repository.
    smoke_dir="$(mktemp -d -t typhoon-smoke.XXXXXX)"
    chmod 0777 "$smoke_dir"
    trap 'rm -rf "$smoke_dir"' EXIT

    typhoon_say "L5 Virtual HIL smoke inside $image"
    # TYPHOON_SMOKE_REQUIRE=L5 makes the smoke a gate rather than a probe:
    # without it the script prints "L5 FAIL" and still exits 0.
    if typhoon_run_in_image "$image" "$smoke_dir" "$license_v2" "$license_v3" \
        --env TYPHOON_SMOKE_TIMEOUT=180 --env TYPHOON_SMOKE_REQUIRE=L5 -- \
        bash -euo pipefail -c 'python3 "$TYPHOON_HIL_SMOKE"'; then
        pass "L5 Virtual HIL smoke"
    else
        fail "L5 Virtual HIL smoke failed inside $image"
    fi
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
    typhoon_say "image contract: all checks passed"
    exit 0
fi
printf '\033[1;31m[typhoon]\033[0m image contract: %d check(s) failed\n' "$failures" >&2
exit 1
