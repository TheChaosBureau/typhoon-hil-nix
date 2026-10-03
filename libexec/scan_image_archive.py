#!/usr/bin/env python3
"""
Scan a ``podman save`` archive for files that must never ship in the image.

Why scan the archive and not the running container: a license that was
copied in and then deleted is *gone from the merged filesystem* but still
present in the layer that added it, and `podman save` ships every layer.
The definition of done for the VHIL image says "no license is present in
any image layer", so the check has to look at layers.

Single streaming pass. The outer archive is opened in stream mode
(``r|``) and each layer blob is itself opened as a nested stream, so a
~10 GB archive is read once and never materialised on disk.

Exit status:
  0  no forbidden name found
  1  a forbidden name was found (names and layers are printed)
  2  the archive could not be read as a container archive

Usage:
    python3 scan_image_archive.py ARCHIVE [--forbid NAME ...]
                                                      [--report-other-lic]
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
import tarfile


DEFAULT_FORBIDDEN = (
    "license_v2.lic",
    "license_v3.lic",
    "instance_license_v2.lic",
    "instance_license_v3.lic",
)

# Members of the outer archive that are themselves layer tarballs.
# Three layouts are in circulation and all three have to be recognised:
#
#   "<id>/layer.tar"      legacy docker-archive, the layer is a real file;
#   "blobs/sha256/<hex>"  oci-archive, every blob (layers, config, manifest)
#                         lives here -- non-tar blobs are skipped when the
#                         nested open fails;
#   "<hex>.tar"           podman >= 5 docker-archive: the real layer blobs sit
#                         at the archive root and each legacy "<id>/layer.tar"
#                         is a *symlink* to one of them.
#
# That last shape is why this predicate cannot be "<id>/layer.tar" alone.
# podman 5.8.2 emits both, but only the root-level "<hex>.tar" members carry
# data; the "<id>/layer.tar" symlinks are size 0 and `isfile()` rejects them,
# so matching only the legacy name counted ZERO layers on a real 8.4 GB
# archive and exited 2. A scanner that cannot find the layers must never be
# read as "no license found" -- see the exit-status contract above.
_ROOT_LAYER_RE = re.compile(r"^[0-9a-f]{64}\.tar$")


def _is_layer_member(name: str) -> bool:
    return (
        name.endswith("/layer.tar")
        or name.startswith("blobs/sha256/")
        or _ROOT_LAYER_RE.match(name) is not None
    )


def scan(archive: str, forbidden: set[str], report_other_lic: bool) -> int:
    hits: list[tuple[str, str]] = []
    other_lic: list[tuple[str, str]] = []
    layers = 0

    try:
        outer = tarfile.open(archive, mode="r|")
    except tarfile.TarError as exc:
        print(f"error: {archive} is not readable as a tar archive: {exc}",
              file=sys.stderr)
        return 2

    with outer:
        for member in outer:
            if not member.isfile() or not _is_layer_member(member.name):
                continue
            handle = outer.extractfile(member)
            if handle is None:
                continue
            try:
                inner = tarfile.open(fileobj=handle, mode="r|*")
            except tarfile.TarError:
                # config / manifest blob, not a layer
                continue
            layers += 1
            with inner:
                try:
                    for entry in inner:
                        base = posixpath.basename(entry.name.rstrip("/"))
                        # Overlay whiteouts mark a *deletion* in this layer;
                        # the payload itself lives in a lower layer, which
                        # this same pass also visits.
                        if base.startswith(".wh."):
                            base = base[len(".wh."):]
                        if base in forbidden:
                            hits.append((member.name, entry.name))
                        elif report_other_lic and base.endswith(".lic"):
                            other_lic.append((member.name, entry.name))
                except tarfile.TarError as exc:
                    print(f"error: unreadable layer {member.name}: {exc}",
                          file=sys.stderr)
                    return 2

    if layers == 0:
        print(f"error: no layers found in {archive}; is it a podman-save "
              f"archive?", file=sys.stderr)
        return 2

    print(f"scanned {layers} layer(s) in {archive}")

    if other_lic:
        print(f"note: {len(other_lic)} vendor .lic file(s) present "
              f"(allowed -- Control Center ships its own):")
        for layer, name in other_lic[:20]:
            print(f"  {layer}: {name}")
        if len(other_lic) > 20:
            print(f"  ... and {len(other_lic) - 20} more")

    sys.stdout.flush()

    if hits:
        print("FAIL: operator license files present in image layers:",
              file=sys.stderr)
        for layer, name in hits:
            print(f"  {layer}: {name}", file=sys.stderr)
        return 1

    print("PASS: no operator license in any layer "
          f"({', '.join(sorted(forbidden))})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="path to the podman save archive")
    parser.add_argument("--forbid", action="append", default=None,
                        metavar="NAME",
                        help="file name that must not appear "
                             "(repeatable; defaults to the operator licenses)")
    parser.add_argument("--report-other-lic", action="store_true",
                        help="also list vendor .lic files (informational)")
    args = parser.parse_args(argv)
    forbidden = set(args.forbid) if args.forbid else set(DEFAULT_FORBIDDEN)
    return scan(args.archive, forbidden, args.report_other_lic)


if __name__ == "__main__":
    sys.exit(main())
