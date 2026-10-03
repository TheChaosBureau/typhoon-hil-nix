# The `typhoon-hil` container image

How the image is built and verified, the contract the launcher depends on, and
the things Control Center needs from a container that nothing tells you.

Verified with Control Center **2026.3** (`typhoon_hil_api` 1.32.1) on
`ubuntu:24.04` under rootless podman 5.

## Build

```sh
typhoon-hil build-image --installers ~/typhoon-installers
```

The script:

1. picks the newest matching installer and **refuses to continue** unless its
   file name carries the version the tag claims, so a 2026.2 installer can
   never produce an image tagged 2026.3;
2. assembles a two-file build context (the Containerfile and the entrypoint) in
   a temporary directory and asserts nothing license- or installer-shaped is in
   it;
3. runs `podman build` with the installer directory bind-mounted **read-only**
   at `/installers`. The installer is therefore never copied into a layer. That
   is why this is a podman build and not a portable `docker build`, and why the
   Containerfile alone is not enough to build the image;
4. runs the contract checks below;
5. with `--save DIR`, writes `typhoon-hil-2026.3.tar` with its sha256 and a
   manifest, and scans every layer of the archive for a license.

`--smoke` (with `--license-v2` and `--license-v3`) runs the real Virtual HIL
gate against the image before it leaves the build machine. `--with-typhoonsim`
also installs TyphoonSim; Virtual HIL does not need it.

The build runs the vendor's installer with `--accept_license_agreement`.
Building the image means you have accepted Typhoon HIL's license agreement.

## Verify

```sh
typhoon-hil check-image --image typhoon-hil:2026.3
typhoon-hil check-image --archive /var/lib/typhoon-vhil/typhoon-hil-2026.3.tar
```

`--image` inspects the built image. `--archive` streams every layer of a saved
archive looking for a license that was added and later deleted: invisible in
the merged filesystem, still shipped in the tar.

| Check | The failure it prevents |
|---|---|
| installer version guard | an image of one release wearing another's tag |
| build-context scan | a license left beside the Containerfile shipping in a layer |
| in-image license scan | a license baked in by an installer step |
| archive layer scan | a license added then deleted: gone from the filesystem, still in the tar |
| `import typhoon.api` | an API wheel that does not match Control Center |
| bundled `10_basic_model/model.tse` present | the smoke test skipping its simulation instead of running it |
| `$HOME` mode 777 | Control Center dying on an unwritable home under `--userns=keep-id` |
| empty license directory | licenses baked instead of mounted |
| display available | "Control Center down?" that is really a missing X server |
| argv round-trip | an entrypoint that mangles the command it is given |
| `TYPHOON_SMOKE_REQUIRE=L5` | a green CI check over a failed simulation |

The smoke test is a diagnostic by default: it prints every layer's verdict and
exits 0 even when Control Center is down. That is useless as a gate, so a CI job
sets `TYPHOON_SMOKE_REQUIRE=L5`, and `typhoon-hil smoke` does so for you.

## The contract

These values live in one place, [`lib/contract.sh`](../lib/contract.sh), which
the builder, the checker and the launcher all source.

| Thing | Value |
|---|---|
| Image tag | `typhoon-hil:2026.3` |
| Archive name | `typhoon-hil-2026.3.tar` |
| Base image | `ubuntu:24.04` |
| Python | `python3`, the system one, with the API wheel from the install tree |
| `TYPHOONPATH` | `/opt/typhoon/current`, a symlink to the real install directory |
| `HOME` (CI) | `/tmp/typhoon-home`, world-writable, shipped pre-created |
| License mount (CI) | `/licenses/license_v{2,3}.lic`, read-only |
| License path Control Center reads | `$HOME/.local/share/typhoon/license/instance_license_v{2,3}.lic`, **writable** |
| Workdir (CI) | `/work`, the checkout, mounted read-write |
| Entrypoint | `/usr/local/bin/typhoon-entrypoint`: stages licenses, starts Xvfb, then `exec "$@"` |
| Tool scripts | `/opt/typhoon-hil`, mounted read-only by the launcher |
| Provenance | `/opt/typhoon/image-build.json` |

The workstation mode replaces `HOME` with a persistent host directory and puts
the licenses there directly; see the header of
[`bin/typhoon-hil`](../bin/typhoon-hil).

## What Control Center needs from a container

Each of these cost a diagnostic round, because the symptom points somewhere
else. They are recorded so the next release, or the next base image, does not
cost them again.

**1. A writable home, at a path known before the uid is.** Under
`--userns=keep-id` the uid inside the container is the caller's, unknown at
build time. Podman creates any *missing* parent of a bind mount as `root:root
0755`, so if the home skeleton were absent, mounting a license would silently
leave `$HOME` unwritable and Control Center would fail to start with an error
that looks nothing like a permission problem. The image ships the skeleton mode
0777.

**2. A writable license file.** Control Center opens its license file for
writing. Mounted read-only, loading a model succeeds and compiling it fails with
`License file (...) is not writable`, leaving a `Target files` directory with no
`.cpd`: a permissions fault that reads like a broken compiler. In CI the
operator's files are mounted read-only at `/licenses` and the entrypoint copies
them to the path Control Center wants. Nothing in the container can modify the
originals, and no license ever enters a layer.

Three messages on the way are harmless: `License file not found, trying to
migrate license from older SW versions`, `There was an error while downloading
license`, and `Could not find license file`. All three appear at startup on a
correctly licensed run.

**3. `/var/run/user/<uid>`.** Control Center takes a lock at
`/var/run/user/<uid>/config.lock` when it starts and does not honour
`XDG_RUNTIME_DIR`. If the directory is missing the binary dies with `Failed to
execute script`, and every API call then times out without naming the cause.
The image ships `/run/user` mode 0777 and the entrypoint creates the per-uid
directory.

**4. Two compilers.** Compiling a schematic generates C and runs `make`, twice,
for two targets:

- *Device firmware, cross-compiled.* The compiler ships inside the install, at
  `compilers/linux/z7/{arm,microblaze}/lin/bin`, and has to be on `PATH`.
- *The native solver, host-compiled.* Control Center builds a solver library on
  first use and bundles no native compiler, so `build-essential` is a **runtime**
  dependency. Without it startup logs `Unable to create sp_lib!`.

In both cases `compile()` returns False after writing a plausible `Target
files` directory, minus the `.cpd`.

**5. A 32-bit loader.** The bundled cross-compiler is a 32-bit binary. Without
`/lib/ld-linux.so.2` the shell reports it as `arm-xilinx-eabi-gcc: not found`,
which reads as a `PATH` problem and is not one: `command -v` finds the file,
and executing it fails. The image adds `libc6:i386`, `zlib1g:i386` and
`libstdc++6:i386`, and the build runs the compiler once to prove it.

**6. A display, always.** Even API-only use starts Control Center processes
that want an X server. The entrypoint starts Xvfb unless a display is already
reachable. The launcher's GUI mode sets `TYPHOON_NO_XVFB=1`, so a desktop that
cannot be reached is an error rather than an invisible Control Center.

Two notes on the tooling itself:

- **`podman save` layout.** podman 5 writes layer blobs at the archive root as
  `<hex>.tar` and makes each legacy `<id>/layer.tar` a symlink. A scanner that
  only matches the legacy path finds zero layers and "passes". The scanner
  reports how many layers it read, and an archive it cannot read is reported as
  **unproven**, not clean.
- **`podman save` over an existing archive fails** with `docker-archive doesn't
  support modifying existing images`. The builder writes to a temporary name
  and moves it into place.
