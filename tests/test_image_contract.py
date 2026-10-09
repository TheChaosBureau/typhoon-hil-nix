"""
Guard the Typhoon HIL container contract against drift.

The contract is spread over artefacts that have to agree:

  * ``lib/contract.sh``            -- the single source of truth
  * ``image/Containerfile``        -- builds an image matching it
  * ``libexec/build-image.sh``     -- builds, verifies, saves
  * ``libexec/check-image.sh``     -- asserts a built image matches it
  * ``bin/typhoon-hil``            -- the consumer, on a workstation and in CI

Drift between them is invisible until someone runs a licensed job, which is
exactly the kind of failure a test that runs everywhere should catch.

Nothing here needs podman, Typhoon or a license -- these are text and syntax
assertions about repository files, plus the layer scanner run on miniature
archives.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIR = REPO_ROOT / "image"
LIBEXEC = REPO_ROOT / "libexec"
CONTRACT = REPO_ROOT / "lib" / "contract.sh"
CONTAINERFILE = IMAGE_DIR / "Containerfile"
LAUNCHER = REPO_ROOT / "bin" / "typhoon-hil"
IMAGE_DOC = REPO_ROOT / "docs" / "image.md"

SHELL_SCRIPTS = [
    CONTRACT,
    LAUNCHER,
    LIBEXEC / "build-image.sh",
    LIBEXEC / "check-image.sh",
    IMAGE_DIR / "container-entrypoint.sh",
]

# `: "${NAME:=value}"` -- the defaults-only form the contract file uses so the
# caller's environment always wins.
_DEFAULT_RE = re.compile(r'^:\s*"\$\{([A-Z0-9_]+):=(.*)\}"\s*$', re.MULTILINE)


def contract_defaults() -> dict[str, str]:
    """Parse contract.sh, resolving references to earlier defaults."""
    values: dict[str, str] = {}
    for name, raw in _DEFAULT_RE.findall(CONTRACT.read_text()):
        resolved = raw
        for known, known_value in values.items():
            resolved = resolved.replace("${" + known + "}", known_value)
        values[name] = resolved
    return values


@pytest.fixture(scope="module")
def defaults() -> dict[str, str]:
    return contract_defaults()


def test_contract_file_parses(defaults):
    assert defaults, "no `: \"${NAME:=value}\"` defaults found in contract.sh"


@pytest.mark.parametrize("name", [
    "TYPHOON_VERSION",
    "TYPHOON_BASE_IMAGE",
    "TYPHOON_IMAGE",
    "TYPHOON_IMAGE_TAR_NAME",
    "TYPHOON_CONTAINER_HOME",
    "TYPHOON_LICENSE_SUBPATH",
    "TYPHOON_LICENSE_STAGE",
    "TYPHOON_CONTAINER_WORKDIR",
    "TYPHOON_PYTHON",
    "TYPHOON_INSTALLER_MOUNT",
    "TYPHOON_BUILD_INFO_PATH",
    "TYPHOON_FORBIDDEN_LICENSE_NAMES",
    "TYPHOON_MEDIA_CANDIDATES",
])
def test_contract_defines(defaults, name):
    assert name in defaults, f"{name} is not defined in contract.sh"


def test_tag_and_archive_follow_the_version(defaults):
    """The tag, the archive name and the version move together or not at all.

    An archive built for one tag and loaded expecting another produces
    `archive did not provide image ...` on the runner, days later.
    """
    version = defaults["TYPHOON_VERSION"]
    assert defaults["TYPHOON_IMAGE"] == f"typhoon-hil:{version}"
    assert defaults["TYPHOON_IMAGE_TAR_NAME"] == f"typhoon-hil-{version}.tar"


def test_licenses_are_mounted_not_baked(defaults):
    """The forbidden list must cover both the media names and the mounted names."""
    forbidden = set(defaults["TYPHOON_FORBIDDEN_LICENSE_NAMES"].split())
    assert {"license_v2.lic", "license_v3.lic"} <= forbidden
    assert {"instance_license_v2.lic", "instance_license_v3.lic"} <= forbidden


def test_license_mount_path_is_under_home(defaults):
    subpath = defaults["TYPHOON_LICENSE_SUBPATH"]
    assert not subpath.startswith("/"), "license subpath must be relative to $HOME"
    # Control Center names this exact location in a startup warning; the
    # container has to use the same one.
    assert subpath == ".local/share/typhoon/license"


def test_containerfile_declares_every_build_arg(defaults):
    """Every knob the build script passes must be an ARG the image accepts.

    A `--build-arg` for an undeclared ARG is a warning, not an error, so the
    image would quietly build with the default instead.
    """
    containerfile = CONTAINERFILE.read_text()
    declared = set(re.findall(r"^ARG\s+([A-Z0-9_]+)", containerfile, re.MULTILINE))
    passed = set(re.findall(r'--build-arg\s+"([A-Z0-9_]+)=',
                            (LIBEXEC / "build-image.sh").read_text()))
    assert passed, "build-image.sh passes no build args"
    assert passed <= declared, f"passed but not declared: {sorted(passed - declared)}"


def test_containerfile_bakes_no_license():
    """No COPY/ADD of anything license-shaped, and no license in the context.

    The build context is assembled by build-image.sh from two files; if a
    COPY ever reached for a third, this is where it gets noticed.
    """
    text = CONTAINERFILE.read_text()
    copies = re.findall(r"^(?:COPY|ADD)\s+(.*)$", text, re.MULTILINE)
    assert copies, "expected at least the entrypoint COPY"
    for line in copies:
        assert ".lic" not in line, f"Containerfile copies a license: {line}"
        assert "gzip.run" not in line, (
            f"Containerfile copies an installer into a layer: {line}")


def test_containerfile_entrypoint_execs_arguments():
    """CI passes a command and arguments after the image name.

    ENTRYPOINT in exec form + the wrapper's `exec "$@"` is what makes
    `podman run IMAGE bash -c '...' bash a b` reach bash intact.
    """
    assert 'ENTRYPOINT ["/usr/local/bin/typhoon-entrypoint"]' \
        in CONTAINERFILE.read_text()
    assert 'exec "$@"' in (IMAGE_DIR / "container-entrypoint.sh").read_text()


def test_containerfile_installs_the_schematic_editors_wayland_server():
    """The bundled libgbm.so.1 links libwayland-server.so.0, even on X11.

    Without it the Schematic Editor dies on import and the GUI waits on its
    "Working..." dialog forever. No API path loads it, so only this and the
    `-sc` start in check-image.sh would notice.
    """
    assert re.search(r"^\s+libwayland-server0\b", CONTAINERFILE.read_text(),
                     re.MULTILINE)


def test_check_image_starts_the_schematic_editor():
    text = (LIBEXEC / "check-image.sh").read_text()
    assert '"$TYPHOONPATH/typhoon_hil.exe" -sc' in text
    assert "errlog.txt" in text, (
        "the import traceback is only in Control Center's errlog.txt")


def test_home_skeleton_is_world_writable():
    """`--userns=keep-id` means the container uid is unknown at build time."""
    text = CONTAINERFILE.read_text()
    assert "chmod -R 0777" in text, (
        "the HOME skeleton must be world-writable or podman's bind-mount "
        "parents leave $HOME unwritable under keep-id")


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_scripts_parse(script):
    """`bash -n` on every shell artefact.

    These scripts run on the operator's box and on a self-hosted runner,
    both places where a syntax error costs a physical round trip.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH")
    result = subprocess.run([bash, "-n", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, f"{script.name}: {result.stderr}"


def test_scan_image_archive_flags_a_license(tmp_path):
    """The layer scanner has to actually find a license in a nested layer.

    Builds a miniature two-layer docker-archive: one clean layer, one with a
    license. A scanner that silently passes everything is the worst possible
    outcome for a check nobody can easily eyeball on a 10 GB tarball.
    """
    import tarfile

    def make_layer(path: Path, member_name: str) -> Path:
        payload = tmp_path / f"payload-{path.stem}"
        payload.write_text("x")
        with tarfile.open(path, "w") as tar:
            tar.add(payload, arcname=member_name)
        return path

    clean = make_layer(tmp_path / "clean.tar", "opt/typhoon/notes.txt")
    dirty = make_layer(tmp_path / "dirty.tar", "opt/typhoon/license_v2.lic")

    def make_archive(name: str, layers: list[Path]) -> Path:
        archive = tmp_path / name
        with tarfile.open(archive, "w") as tar:
            for index, layer in enumerate(layers):
                tar.add(layer, arcname=f"layer{index}/layer.tar")
        return archive

    scanner = str(LIBEXEC / "scan_image_archive.py")

    ok = subprocess.run([sys.executable, scanner, str(make_archive("ok.tar", [clean]))],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    bad = subprocess.run(
        [sys.executable, scanner, str(make_archive("bad.tar", [clean, dirty]))],
        capture_output=True, text=True)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "license_v2.lic" in bad.stderr


def test_build_info_template_is_valid_json():
    """The provenance file the image writes must parse.

    It is assembled by a printf in the Containerfile, so a stray comma is
    only discovered when someone tries to read it. Rebuild the same shape
    here with placeholder values and parse it.
    """
    text = CONTAINERFILE.read_text()
    match = re.search(r"printf '(\{\\n.*?\}\\n)'", text, re.DOTALL)
    assert match, "could not find the build-info printf in the Containerfile"
    template = match.group(1).replace("\\n", "\n")
    # %s -> a quoted-safe token, the single %s that is a bare JSON literal
    # (typhoonsim_installed) -> true.
    filled = template.replace('"%s"', '"x"').replace("%s", "true")
    json.loads(filled)


def test_scan_image_archive_reads_the_podman_5_layout(tmp_path):
    """The scanner must find layers in the archive `podman save` really writes.

    podman >= 5 puts the real layer blobs at the archive ROOT as `<hex>.tar`
    and makes each legacy `<id>/layer.tar` a *symlink* to one of them. A
    predicate that only matched `<id>/layer.tar` therefore counted zero layers
    on a genuine 8.4 GB archive and exited 2 -- while the old fixture, which
    wrote `layer0/layer.tar` as a real file, stayed green. Assert on the
    scanned-layer COUNT, not just the exit status: a scanner that finds nothing
    also "passes".
    """
    import tarfile

    def make_layer(path: Path, member_name: str) -> Path:
        payload = tmp_path / f"payload-{path.stem}"
        payload.write_text("x")
        with tarfile.open(path, "w") as tar:
            tar.add(payload, arcname=member_name)
        return path

    clean = make_layer(tmp_path / "p5-clean.tar", "opt/typhoon/notes.txt")
    dirty = make_layer(tmp_path / "p5-dirty.tar", "opt/typhoon/license_v3.lic")

    def make_podman5_archive(name: str, layers: list[Path]) -> Path:
        """Root-level `<64hex>.tar` real blobs + `<64hex>/layer.tar` symlinks."""
        archive = tmp_path / name
        with tarfile.open(archive, "w") as tar:
            for index, layer in enumerate(layers):
                digest = chr(ord("a") + index) * 64
                tar.add(layer, arcname=f"{digest}.tar")
                link = tarfile.TarInfo(name=f"{digest}/layer.tar")
                link.type = tarfile.SYMTYPE
                link.linkname = f"../{digest}.tar"
                tar.addfile(link)
        return archive

    scanner = str(LIBEXEC / "scan_image_archive.py")

    ok = subprocess.run(
        [sys.executable, scanner, str(make_podman5_archive("p5-ok.tar", [clean]))],
        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # The point of the test: it actually opened the layer.
    assert "scanned 1 layer(s)" in ok.stdout, ok.stdout + ok.stderr

    bad = subprocess.run(
        [sys.executable, scanner,
         str(make_podman5_archive("p5-bad.tar", [clean, dirty]))],
        capture_output=True, text=True)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "license_v3.lic" in bad.stderr

    # An archive with no readable layer blob is UNPROVEN (2), never clean (0).
    empty = tmp_path / "p5-empty.tar"
    with tarfile.open(empty, "w") as tar:
        info = tarfile.TarInfo(name="a" * 64 + "/layer.tar")
        info.type = tarfile.SYMTYPE
        info.linkname = "../" + "a" * 64 + ".tar"
        tar.addfile(info)
    none = subprocess.run([sys.executable, scanner, str(empty)],
                          capture_output=True, text=True)
    assert none.returncode == 2, none.stdout + none.stderr


def test_check_image_distinguishes_scan_failure_from_a_license_hit():
    """`--archive` must not report an unreadable archive as a license leak.

    The scanner's exit 1 ("found one") and exit 2 ("could not read the
    layers") mean opposite things to an operator. Collapsing them sent the
    first real build hunting for a license that was never there.
    """
    text = (LIBEXEC / "check-image.sh").read_text()
    assert 'scan_rc' in text, "check-image.sh must capture the scanner's exit code"
    assert re.search(r'case\s+"\$scan_rc"', text), \
        "check-image.sh must branch on the scanner's exit code, not just if/else"
    assert "UNPROVEN" in text, \
        "an unreadable archive must be reported as UNPROVEN, not as a clean pass"


def test_image_ships_a_writable_run_user_and_entrypoint_makes_the_per_uid_dir():
    """Control Center locks /var/run/user/<uid>/config.lock at startup.

    It does not honour XDG_RUNTIME_DIR, and `/run` in the image is root-owned
    0755, so a keep-id uid cannot create the directory itself. The image has to
    ship /run/user world-writable and the entrypoint has to make the per-uid
    subdirectory -- the uid is not knowable at build time. Without both halves
    the binary dies at startup and the smoke reports layer timeouts that name
    nothing.
    """
    containerfile = CONTAINERFILE.read_text()
    assert "mkdir -p /run/user" in containerfile, \
        "the image must create /run/user at build time"
    assert re.search(r"chmod\s+0777\s+/run/user", containerfile), \
        "/run/user must ship world-writable; the runtime uid is unknown at build time"

    entrypoint = (IMAGE_DIR / "container-entrypoint.sh").read_text()
    assert re.search(r'runtime_dir="/run/user/\$\(id -u\)"', entrypoint), \
        "the entrypoint must create the per-uid runtime dir"
    assert "mkdir -p \"$runtime_dir\"" in entrypoint


def test_licenses_are_staged_read_only_then_copied_writable(defaults):
    """Control Center writes to its license file, so `:ro` onto $HOME is fatal.

    Mounted read-only at the path Control Center uses, `load()` succeeds and
    `compile()` fails with "License file (...) is not writable", leaving a
    Target files directory and no .cpd -- a permissions fault wearing a
    compiler's clothes. The contract therefore mounts the operator's files
    read-only at a staging path and the entrypoint copies them writable.

    The host-side guarantee this protects: nothing in the container can modify
    the operator's own license files, and none of them ever enter a layer.
    """
    stage = defaults["TYPHOON_LICENSE_STAGE"]
    assert stage.startswith("/"), "the license stage is a mount point, so absolute"
    assert stage.rstrip("/") != defaults["TYPHOON_CONTAINER_HOME"].rstrip("/")

    contract = CONTRACT.read_text()
    # The one podman invocation must mount to the stage, not onto $HOME.
    run_block = contract[contract.index("typhoon_run_in_image()"):]
    assert "typhoon_stage_license_path 2" in run_block
    assert "typhoon_stage_license_path 3" in run_block
    assert "typhoon_container_license_path 2" not in run_block, \
        "mounting :ro onto the path Control Center writes to is the bug this fixes"

    entrypoint = (IMAGE_DIR / "container-entrypoint.sh").read_text()
    assert "TYPHOON_LICENSE_STAGE" in entrypoint, \
        "the entrypoint must read the staging path"
    assert 'cp -f "$staged" "$target"' in entrypoint, \
        "the entrypoint must copy the staged license to a writable target"
    # And it must notice the old shape rather than failing later at compile time.
    assert "is not writable" in entrypoint


def test_build_is_rerunnable_over_an_existing_archive():
    """A second build into the same --out directory must not die on the save.

    `podman save` into an existing docker-archive fails with "docker-archive
    doesn't support modifying existing images" -- an error that names neither
    the archive nor the real cause. Writing to a temp name and moving it into
    place also means a failed save cannot leave a truncated file wearing the
    real archive name, which the layer scan would then report as unreadable.
    """
    text = (LIBEXEC / "build-image.sh").read_text()
    assert 'podman save --output "$archive.partial"' in text, \
        "save must write to a temp name, not straight onto an existing archive"
    assert 'mv -f "$archive.partial" "$archive"' in text, \
        "the temp archive must be moved into place"


def test_image_supplies_the_32bit_runtime_the_cross_compiler_needs():
    """The bundled Xilinx cross-compiler is a 32-bit binary.

    ELF class 1 / machine 3, needing /lib/ld-linux.so.2. A 64-bit-only noble image has neither that
    loader nor i386 as a dpkg architecture, and the shell reports a missing ELF
    interpreter exactly like a missing file:

        /bin/sh: 1: arm-xilinx-eabi-gcc: not found

    which reads as a PATH fault and is not one. Both halves are required --
    the PATH entry so `make` can name it, and the 32-bit runtime so it can
    actually exec -- so assert both, and assert the build proves the compiler
    runs rather than trusting that the packages landed.
    """
    text = CONTAINERFILE.read_text()
    assert "dpkg --add-architecture i386" in text, \
        "i386 must be an enabled architecture for the bundled cross-compiler"
    for pkg in ("libc6:i386", "zlib1g:i386", "libstdc++6:i386"):
        assert pkg in text, f"{pkg} is needed by the 32-bit cross-compiler"
    assert "test -e /lib/ld-linux.so.2" in text, \
        "the build must assert the 32-bit loader exists, not assume apt worked"
    assert 'arm_gcc' in text and '--version >/dev/null' in text, \
        "the build must actually execute the cross-compiler once"

    # And the PATH half, which is necessary but NOT sufficient on its own.
    assert "compilers/linux/z7/arm/lin/bin" in text
    assert "compilers/linux/z7/microblaze/lin/bin" in text

    # The i386 layer must come AFTER Control Center is installed, or the
    # verification runs against a compiler that does not exist yet and silently
    # passes -- the check would be dead code.
    assert text.index("dpkg --add-architecture i386") > text.index("ENV TYPHOONPATH="), \
        "the 32-bit layer must follow the Control Center install so its check is real"


def test_documented_tag_matches_the_contract(defaults):
    """docs/image.md is what an operator follows; it must not name a stale tag."""
    doc = IMAGE_DOC.read_text()
    assert defaults["TYPHOON_IMAGE"] in doc
    assert defaults["TYPHOON_IMAGE_TAR_NAME"] in doc


def test_launcher_does_not_rehardcode_the_contract():
    """The launcher must take the tag, archive name and paths from contract.sh."""
    text = LAUNCHER.read_text()
    assert "lib/contract.sh" in text
    for literal in ("typhoon-hil:2026", "typhoon-hil-2026", "/tmp/typhoon-home",
                    ".local/share/typhoon/license"):
        assert literal not in text, (
            f"bin/typhoon-hil hardcodes {literal!r}; it should come from contract.sh")


def test_archive_may_be_staged_locally_when_the_media_cannot_hold_it(defaults):
    """The archive is several times the size of typical license media.

    Unlike the licenses, it carries nothing secret -- the layer scan proves no
    license is in it -- so it may live on the runner's own disk. Resolution
    order is explicit override, then media, then local stage.
    """
    stage = defaults["TYPHOON_LOCAL_ARCHIVE_DIR"]
    assert stage.startswith("/"), "the local archive stage is an absolute path"
    # It must not sit inside a runner work directory: that tree is wiped on
    # runner start, which would silently discard the archive.
    assert "github-runner" not in stage

    ci = LAUNCHER.read_text()
    assert ci.index('$media_dir/$TYPHOON_IMAGE_TAR_NAME') < \
        ci.index('$TYPHOON_LOCAL_ARCHIVE_DIR/$TYPHOON_IMAGE_TAR_NAME'), \
        "removable media must win over the local stage"
    # Licenses are NOT given the same treatment: they stay on read-only media.
    assert 'license_v2="${TYPHOON_LICENSE_V2:-$media_dir/license_v2.lic}"' in ci


def test_nothing_vendor_owned_is_committed():
    """This repository is public. The installer, licenses, image archives and
    the vendor's artwork are the operator's to supply, never ours to ship."""
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run([git, "-C", str(REPO_ROOT), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.split("\n")
    offenders = [name for name in tracked
                 if name.endswith((".lic", ".gzip.run", ".tar", ".ico", ".png", ".tse"))]
    assert not offenders, f"vendor-owned or operator-owned files are tracked: {offenders}"
