"""
The launcher's decisions, exercised against a stand-in ``podman``.

What a container is given -- which directories, which home, whether the
licenses are writable, whether a display is passed -- is the whole difference
between the workstation and CI modes, and a wrong answer shows up as a compile
that fails for no visible reason. So the launcher runs here for real, with
``podman`` and ``findmnt`` replaced by scripts that record how they were called.

Nothing here needs podman, Typhoon or a license.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "bin" / "typhoon-hil"
EXTRACT_ICON = REPO_ROOT / "libexec" / "extract_icon.py"

IMAGE = "typhoon-hil:2026.3"
ARCHIVE = "typhoon-hil-2026.3.tar"

# Run every script through this bash by absolute path. A Nix build sandbox has
# no /usr/bin/env, so nothing here may depend on an `env` shebang resolving.
BASH = shutil.which("bash")

STUB_PODMAN = r"""#!@BASH@
# Record the call, one argument per line, then answer the few queries the
# launcher makes. Behaviour is steered by STUB_* variables.
{ printf 'CALL\n'; printf '%s\n' "$@"; } >>"$STUB_LOG"
case "$1 ${2:-}" in
    "image exists") exit "${STUB_IMAGE_EXISTS:-0}" ;;
    "image inspect") echo "0123456789abcdef0123456789abcdef"; exit 0 ;;
    "container inspect")
        [ -n "${STUB_STATE:-}" ] || exit 125
        case "$*" in
            *State.Status*) echo "$STUB_STATE" ;;
            *typhoon-hil.mode*) echo "${STUB_MODE:-headless}" ;;
        esac
        exit 0 ;;
    "info --format") echo true; exit 0 ;;
esac
exit 0
"""

STUB_FINDMNT = r"""#!@BASH@
echo "${STUB_MOUNT_OPTIONS:-rw,relatime}"
"""


class Harness:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "podman.log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (("podman", STUB_PODMAN), ("findmnt", STUB_FINDMNT)):
            stub = self.bin / name
            stub.write_text(body.replace("@BASH@", BASH))
            stub.chmod(0o755)
        self.state = self.home / ".local" / "share" / "typhoon-hil"

    def run(self, *args: str, cwd: Path | None = None, **env: str):
        full_env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "STUB_LOG": str(self.log),
        }
        full_env.update(env)
        return subprocess.run(
            [BASH, str(LAUNCHER), *args], cwd=cwd or self.home, env=full_env,
            capture_output=True, text=True, timeout=60)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        chunks = self.log.read_text().split("CALL\n")[1:]
        return [chunk.rstrip("\n").split("\n") for chunk in chunks]

    def call(self, verb: str) -> list[str]:
        """The single recorded `podman <verb> ...` call."""
        matches = [c for c in self.calls() if c[0] == verb]
        assert len(matches) == 1, f"expected one `podman {verb}`, got {self.calls()}"
        return matches[0]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def volumes(call: list[str]) -> list[str]:
    return [call[i + 1] for i, word in enumerate(call) if word == "--volume"]


def envs(call: list[str]) -> list[str]:
    return [call[i + 1] for i, word in enumerate(call) if word == "--env"]


def option(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


# ---- workstation mode --------------------------------------------------------

def test_help_needs_no_podman(tmp_path):
    result = subprocess.run([BASH, str(LAUNCHER), "--help"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "typhoon-hil run CMD" in result.stdout


def test_help_lists_every_subcommand():
    """The usage block is a fixed line range of this script's own header, so a
    new subcommand is easy to add and easy to leave undocumented."""
    text = LAUNCHER.read_text()
    dispatch = text[text.index('sub="${1:-gui}"'):]
    names = set()
    for line in dispatch.splitlines():
        match = re.match(r"\s{4}([a-z][a-z|-]*)\)", line)
        if match:
            names.update(match.group(1).split("|"))
    names -= {"help"}
    assert {"gui", "run", "ci", "smoke", "smoke-path"} <= names, names

    result = subprocess.run([BASH, str(LAUNCHER), "--help"], capture_output=True, text=True,
                            env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"})
    for name in sorted(names):
        # `gui` is written as the default, `typhoon-hil [gui]`.
        assert re.search(rf"typhoon-hil \[?{re.escape(name)}\]?\b", result.stdout), \
            f"{name} is missing from --help"


def test_smoke_path_prints_the_shipped_gate_and_starts_nothing(harness):
    """A NATIVE Control Center install runs the same gate as the container, so
    it has to be able to find it without a container being involved at all."""
    result = harness.run("smoke-path")
    assert result.returncode == 0, result.stderr
    printed = Path(result.stdout.strip())
    assert printed.is_file(), printed
    assert "TYPHOON_SMOKE_REQUIRE" in printed.read_text(), "that is not the smoke test"
    assert harness.calls() == [], "smoke-path must not invoke podman"


def test_unbuilt_image_is_named_with_the_way_to_build_it(harness):
    result = harness.run("run", "true", STUB_IMAGE_EXISTS="1")
    assert result.returncode == 1
    assert f"no {IMAGE} image" in result.stderr
    assert "typhoon-hil build-image --installers" in result.stderr


def test_run_outside_the_shares_mounts_only_the_current_directory(harness, tmp_path):
    work = tmp_path / "elsewhere"
    work.mkdir()
    result = harness.run("run", "python3", "-m", "pytest", cwd=work)
    assert result.returncode == 0, result.stderr
    call = harness.call("run")

    assert f"{work}:/work:rw" in volumes(call)
    assert option(call, "--workdir") == "/work"
    # The uid inside is the operator's, so files written to the tree are theirs.
    assert "--userns=keep-id" in call
    # A persistent home, mounted where the operator's home is.
    assert f"{harness.state}/home:{harness.home}:rw" in volumes(call)
    assert f"HOME={harness.home}" in envs(call)
    assert "typhoon-hil.mode=headless" in call
    assert call[-4:] == [IMAGE, "python3", "-m", "pytest"]


def test_workstation_licenses_live_in_the_home_and_are_never_staged(harness):
    """Control Center writes to its license file. On a workstation it is placed
    once in the persistent home; the read-only staging mount is CI's."""
    harness.run("run", "true")
    call = harness.call("run")
    assert not any(v.split(":")[1].startswith("/licenses") for v in volumes(call))
    assert (harness.state / "home/.local/share/typhoon/license").is_dir()


def test_run_inside_a_share_keeps_the_host_path(harness):
    project = harness.home / "Projects" / "inverter"
    project.mkdir(parents=True)
    result = harness.run("run", "true", cwd=project)
    assert result.returncode == 0, result.stderr
    call = harness.call("run")
    shares = harness.home / "Projects"
    assert f"{shares}:{shares}:rw" in volumes(call)
    assert option(call, "--workdir") == str(project)
    assert not any(v.endswith(":/work:rw") for v in volumes(call))


def test_run_joins_a_container_that_is_already_up(harness):
    """With Control Center open, a test attaches to it instead of starting a
    second instance over the same home."""
    project = harness.home / "Projects" / "inverter"
    project.mkdir(parents=True)
    result = harness.run("run", "--env", "K=v", "--", "pytest", "-k", "x", cwd=project,
                         STUB_STATE="running", STUB_MODE="gui")
    assert result.returncode == 0, result.stderr
    assert not [c for c in harness.calls() if c[0] == "run"], "must not start a second container"
    call = harness.call("exec")
    assert option(call, "--workdir") == str(project)
    assert "K=v" in envs(call)
    assert call[-4:] == ["typhoon-hil", "pytest", "-k", "x"]


def test_run_refuses_a_directory_the_running_container_cannot_see(harness, tmp_path):
    work = tmp_path / "elsewhere"
    work.mkdir()
    result = harness.run("run", "true", cwd=work, STUB_STATE="running", STUB_MODE="gui")
    assert result.returncode == 1
    assert "is not inside a shared directory" in result.stderr
    assert not [c for c in harness.calls() if c[0] in ("run", "exec")]


def test_run_passes_env_options_through(harness):
    harness.run("run", "--env", "A=1", "--env=B", "--", "cmd", "--env", "not-ours")
    call = harness.call("run")
    assert "A=1" in envs(call) and "B" in envs(call)
    # Everything after `--` belongs to the command.
    assert call[-4:] == [IMAGE, "cmd", "--env", "not-ours"]


def test_smoke_is_a_gate_and_needs_no_working_tree(harness, tmp_path):
    work = tmp_path / "elsewhere"
    work.mkdir()
    result = harness.run("smoke", cwd=work)
    assert result.returncode == 0, result.stderr
    call = harness.call("run")
    assert "TYPHOON_SMOKE_REQUIRE=L5" in envs(call)
    assert call[-2:] == ["python3", "/opt/typhoon-hil/smoke_test.py"]
    assert f"{REPO_ROOT}/libexec:/opt/typhoon-hil:ro" in volumes(call)
    assert not any(v.endswith(":/work:rw") for v in volumes(call))


def test_gui_is_given_the_desktop_display_and_never_a_private_one(harness, tmp_path):
    cookie = tmp_path / "xauth"
    cookie.write_text("cookie")
    result = harness.run("gui", DISPLAY=":7", XAUTHORITY=str(cookie),
                         XDG_DATA_HOME=str(tmp_path / "data"))
    assert result.returncode == 0, result.stderr
    call = [c for c in harness.calls() if c[0] == "run" and "typhoon-hil.mode=gui" in c]
    assert len(call) == 1, harness.calls()
    call = call[0]
    assert "DISPLAY=:7" in envs(call)
    # Without this the entrypoint would start Xvfb when the desktop is
    # unreachable, and Control Center would run where nobody can see it.
    assert "TYPHOON_NO_XVFB=1" in envs(call)
    assert "/tmp/.X11-unix:/tmp/.X11-unix:ro" in volumes(call)
    assert f"{cookie}:/run/host-xauthority:ro" in volumes(call)
    assert "XAUTHORITY=/run/host-xauthority" in envs(call)


def test_gui_refuses_to_start_a_second_instance(harness):
    result = harness.run("gui", DISPLAY=":7", STUB_STATE="running", STUB_MODE="gui")
    assert result.returncode == 1
    assert "already running" in result.stderr


# ---- CI mode -----------------------------------------------------------------

def media(tmp_path: Path, *, licenses: bool = True, archive: bool = True) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    if licenses:
        (directory / "license_v2.lic").write_text("v2")
        (directory / "license_v3.lic").write_text("v3")
    if archive:
        (directory / ARCHIVE).write_text("tar")
    return directory


def test_ci_names_every_media_candidate_when_none_is_mounted(harness, tmp_path):
    result = harness.run("ci", "true",
                         TYPHOON_MEDIA_CANDIDATES=f"{tmp_path}/a {tmp_path}/b")
    assert result.returncode == 1
    assert "none of the candidate media directories are mounted" in result.stderr
    assert f"{tmp_path}/a {tmp_path}/b" in result.stderr
    assert not [c for c in harness.calls() if c[0] in ("load", "run")]


def test_ci_refuses_writable_license_media(harness, tmp_path):
    """Read-only is the contract: a CI job must not be able to alter the
    operator's licenses."""
    result = harness.run("ci", "true", TYPHOON_MEDIA_DIR=str(media(tmp_path)),
                         STUB_MOUNT_OPTIONS="rw,relatime")
    assert result.returncode == 1
    assert "is not a read-only mount" in result.stderr
    assert not [c for c in harness.calls() if c[0] in ("load", "run")]


def test_ci_fails_on_a_missing_license_before_starting_anything(harness, tmp_path):
    result = harness.run("ci", "true",
                         TYPHOON_MEDIA_DIR=str(media(tmp_path, licenses=False)),
                         STUB_MOUNT_OPTIONS="ro,relatime")
    assert result.returncode == 1
    assert "license_v2.lic is not readable" in result.stderr
    assert not [c for c in harness.calls() if c[0] in ("load", "run")]


def test_ci_names_both_archive_locations_on_a_miss(harness, tmp_path):
    stage = tmp_path / "stage"
    result = harness.run("ci", "true",
                         TYPHOON_MEDIA_DIR=str(media(tmp_path, archive=False)),
                         TYPHOON_LOCAL_ARCHIVE_DIR=str(stage),
                         STUB_MOUNT_OPTIONS="ro,relatime")
    assert result.returncode == 1
    assert f"no {ARCHIVE}" in result.stderr
    assert str(stage) in result.stderr and str(tmp_path / "media") in result.stderr


def test_ci_loads_the_archive_then_runs_with_read_only_licenses(harness, tmp_path):
    directory = media(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = harness.run("ci", "--env", "PYTEST_K=fast", "--", "bash", "-c", "pytest",
                         TYPHOON_MEDIA_DIR=str(directory),
                         STUB_MOUNT_OPTIONS="ro,nosuid,relatime",
                         GITHUB_WORKSPACE=str(workspace))
    assert result.returncode == 0, result.stderr

    verbs = [c[0] for c in harness.calls()]
    assert verbs.index("load") < verbs.index("run"), "the image must be reloaded before the run"
    assert option(harness.call("load"), "--input") == f"{directory}/{ARCHIVE}"

    call = harness.call("run")
    assert f"{workspace}:/work:rw" in volumes(call)
    assert f"{directory}/license_v2.lic:/licenses/license_v2.lic:ro" in volumes(call)
    assert f"{directory}/license_v3.lic:/licenses/license_v3.lic:ro" in volumes(call)
    # The throwaway home from the image, never the operator's persistent one.
    assert "HOME=/tmp/typhoon-home" in envs(call)
    assert not any(str(harness.state) in v for v in volumes(call))
    assert "PYTEST_K=fast" in envs(call)
    assert "--name" not in call, "CI runs must not collide on a fixed container name"
    assert call[-4:] == [IMAGE, "bash", "-c", "pytest"]


def test_ci_prefers_the_media_archive_over_the_local_stage(harness, tmp_path):
    directory = media(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / ARCHIVE).write_text("tar")
    harness.run("ci", "true", TYPHOON_MEDIA_DIR=str(directory),
                TYPHOON_LOCAL_ARCHIVE_DIR=str(stage), STUB_MOUNT_OPTIONS="ro")
    assert option(harness.call("load"), "--input") == f"{directory}/{ARCHIVE}"


def test_ci_falls_back_to_the_local_stage(harness, tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / ARCHIVE).write_text("tar")
    result = harness.run("ci", "true",
                         TYPHOON_MEDIA_DIR=str(media(tmp_path, archive=False)),
                         TYPHOON_LOCAL_ARCHIVE_DIR=str(stage), STUB_MOUNT_OPTIONS="ro")
    assert result.returncode == 0, result.stderr
    assert option(harness.call("load"), "--input") == f"{stage}/{ARCHIVE}"


# ---- the icon ----------------------------------------------------------------

def test_extract_icon_reencodes_the_largest_bitmap_as_png(tmp_path):
    """A 2x2 icon whose four pixels are distinct, so a swapped channel or a
    flipped row cannot pass."""
    # Bitmap rows are bottom-up and BGRA.
    bottom = bytes([1, 2, 3, 255]) + bytes([4, 5, 6, 255])
    top = bytes([7, 8, 9, 128]) + bytes([10, 11, 12, 0])
    header = struct.pack("<IiiHHIIiiII", 40, 2, 4, 1, 32, 0, 0, 0, 0, 0, 0)
    dib = header + bottom + top + b"\x00" * 8
    small = struct.pack("<IiiHHIIiiII", 40, 1, 2, 1, 32, 0, 0, 0, 0, 0, 0) + b"\x00" * 8
    directory = struct.pack("<HHH", 0, 1, 2)
    offset = 6 + 16 * 2
    entries = struct.pack("<BBBBHHII", 1, 1, 0, 0, 1, 32, len(small), offset)
    entries += struct.pack("<BBBBHHII", 2, 2, 0, 0, 1, 32, len(dib), offset + len(small))
    ico = tmp_path / "icon.ico"
    ico.write_bytes(directory + entries + small + dib)

    png = subprocess.run([sys.executable, str(EXTRACT_ICON), str(ico)],
                         capture_output=True, check=True).stdout
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
    assert (width, height, depth, colour) == (2, 2, 8, 6)
    idat_length = struct.unpack(">I", png[33:37])[0]
    rows = zlib.decompress(png[41:41 + idat_length])
    # Top row first, RGBA, each row behind a zero filter byte.
    assert rows == (b"\x00" + bytes([9, 8, 7, 128, 12, 11, 10, 0])
                    + b"\x00" + bytes([3, 2, 1, 255, 6, 5, 4, 255]))
