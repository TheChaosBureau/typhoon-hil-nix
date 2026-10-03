# typhoon-hil-nix

[![ci](https://github.com/TheChaosBureau/typhoon-hil-nix/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TheChaosBureau/typhoon-hil-nix/actions/workflows/ci.yml?query=branch%3Amain)

Run [Typhoon HIL](https://www.typhoon-hil.com/) Control Center and Virtual HIL
from a container: on a Linux workstation the vendor does not support (NixOS
included), and on a self-hosted CI runner.

Control Center ships as an installer for Ubuntu. This repository runs that
installer, unmodified, inside an Ubuntu container image, and gives you one
command, `typhoon-hil`, that starts the image the right way for a desktop or
for a CI job.

**This repository contains none of Typhoon HIL's software.** You supply the
vendor's installer to build the image and your own licenses to run it. It is
not affiliated with or endorsed by Typhoon HIL, Inc.

## What you need

- x86_64 Linux with rootless [podman](https://podman.io/)
- the Control Center installer, `typhoon_hil_control_center_v2026.3.gzip.run`,
  from Typhoon HIL's customer portal
- your `license_v2.lic` and `license_v3.lic`
- about 20 GB free while building (the image is about 9 GB)

## Install the command

With Nix:

```sh
nix profile install github:TheChaosBureau/typhoon-hil-nix
```

That puts `typhoon-hil` on your PATH and a *Typhoon HIL Control Center* entry
in your desktop's application list. To try it without installing, use
`nix run github:TheChaosBureau/typhoon-hil-nix -- status`.

Without Nix, clone the repository and run `bin/typhoon-hil`. It needs bash,
coreutils, findutils, util-linux and python3.

## Set up a workstation

```sh
# 1. Build the image. Put the installer in a directory of its own, so no
#    license is visible to the build.
typhoon-hil build-image --installers ~/typhoon-installers

# 2. Place your licenses, once, under the names Control Center looks for.
d=~/.local/share/typhoon-hil/home/.local/share/typhoon/license
install -D -m 0600 license_v2.lic "$d/instance_license_v2.lic"
install -D -m 0600 license_v3.lic "$d/instance_license_v3.lic"

# 3. Check it end to end: compile, simulate on Virtual HIL, read a probe.
typhoon-hil smoke
```

Then:

```sh
typhoon-hil                    # Control Center on your desktop
cd ~/Projects/my-models
typhoon-hil run python3 -m pytest tests
typhoon-hil shell
typhoon-hil status
```

Things worth knowing:

- **One container at a time.** If Control Center is open, `run` attaches to it,
  as an API script would on a native install. Otherwise it starts a headless
  container.
- **Only shared directories are visible inside**, at the same path as on the
  host. The default is `~/Projects`. A `run` from anywhere else mounts just
  that directory at `/work`.
- **Settings, caches and licenses persist** in
  `~/.local/share/typhoon-hil/home`. The first compile after a fresh home takes
  a couple of minutes while Control Center builds its library cache.
- **Tests run on the image's Python**, which carries the `typhoon-hil-api`
  wheel that matches the installed Control Center. Do not install that package
  from PyPI on top of it: a mismatched client fails in ways that look like a
  broken model. Install your other test dependencies with
  `python3 -m pip install --user --break-system-packages ...`; they persist in
  the home.

Optional settings go in `~/.config/typhoon-hil/config`, as shell assignments:

| Variable | Default | Meaning |
|---|---|---|
| `TYPHOON_HIL_SHARE` | `$HOME/Projects` | colon-separated directories to share |
| `TYPHOON_HIL_SCALE` | unset | Qt scale factor for the GUI, e.g. `1.5` |
| `TYPHOON_HIL_GPU` | `1` | set to `0` to render in software |
| `TYPHOON_VERSION` | `2026.3` | Control Center release; the image tag follows it |

## Use it in CI

`typhoon-hil ci CMD...` is for a self-hosted runner. It differs from the
workstation mode on purpose:

- licenses are read from **read-only** removable media and re-staged for each
  run, so a job cannot alter them;
- the home is thrown away after each run;
- the image is loaded from a saved archive on every run.

On the runner you need podman, the license media mounted read-only at
`/run/media/Typhoon` or `/run/media/TYPHOON-RUNNER`, and the archive either on
that media or in `/var/lib/typhoon-vhil`. Build the archive once:

```sh
typhoon-hil build-image --installers ~/typhoon-installers --save /var/lib/typhoon-vhil
```

A workflow step then looks like this, with the release pinned:

```yaml
- name: Virtual HIL tests
  run: |
    nix run github:TheChaosBureau/typhoon-hil-nix/v0.1.0 -- ci -- \
      bash -euo pipefail -c '
        python3 -m pip install --quiet --break-system-packages pytest
        python3 -m pytest tests
      '
```

The checkout is mounted at `/work`. Pass variables through with
`--env NAME[=VALUE]` before the `--`. The smoke test is available inside every
container as `python3 "$TYPHOON_HIL_SMOKE"`; set `TYPHOON_SMOKE_REQUIRE=L5` to
make it fail the job when Virtual HIL does not run.

Every prerequisite is checked before a container starts, and each failure names
what is missing. The media paths, archive location and license paths can all be
overridden; see [`lib/contract.sh`](lib/contract.sh).

## Use it from another flake

```nix
{
  inputs.typhoon-hil-nix.url = "github:TheChaosBureau/typhoon-hil-nix";

  outputs = { nixpkgs, typhoon-hil-nix, ... }:
    let pkgs = nixpkgs.legacyPackages.x86_64-linux; in {
      devShells.x86_64-linux.default = pkgs.mkShell {
        packages = [ typhoon-hil-nix.packages.x86_64-linux.default ];
      };
    };
}
```

The package is only the launcher and the image recipe. podman is taken from the
host, because rootless podman depends on the host's own setup.

## Moving to a new Control Center release

1. Put the new installer in your installer directory.
2. Build with the new version: `TYPHOON_VERSION=2026.4 typhoon-hil build-image --installers ...`.
   The build refuses an installer whose file name does not carry that version.
3. Run `typhoon-hil smoke` with the same `TYPHOON_VERSION`.
4. Change the default in [`lib/contract.sh`](lib/contract.sh) and open a pull
   request.

[`docs/image.md`](docs/image.md) describes the image contract, what the checks
catch, and the non-obvious things Control Center needs from a container.

## Development

```sh
nix flake check          # shellcheck, plus the test suite
nix develop -c python3 -m pytest tests
```

The tests need no podman, no Typhoon install and no license: they check the
contract files against each other and run the launcher against a stand-in
`podman`.

## License

Apache-2.0 for everything in this repository. Typhoon HIL Control Center is
proprietary software under its own license agreement. The image build runs the
vendor's installer with `--accept_license_agreement`, so building the image
means you have accepted that agreement.
