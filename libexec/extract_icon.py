"""Write Control Center's own icon to stdout as a PNG.

The icon is the vendor's artwork, so this repository does not ship it; the
launcher pulls it out of the installed image instead. Runs inside the image
with nothing but the standard library: the .ico holds uncompressed 32-bit
bitmaps, and re-encoding one as a PNG needs only zlib.

    python3 extract_icon.py [ICO]      (default: $TYPHOONPATH/icons/cc.ico)
"""

from __future__ import annotations

import os
import struct
import sys
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def largest_image(ico: bytes) -> bytes:
    """The payload of the largest image in an .ico directory."""
    reserved, kind, count = struct.unpack_from("<HHH", ico, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise ValueError("not an .ico file")
    best = None
    for index in range(count):
        width, _h, _c, _r, _planes, _bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", ico, 6 + 16 * index)
        width = width or 256  # a zero byte means 256
        if best is None or width > best[0]:
            best = (width, ico[offset:offset + size])
    return best[1]


def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def bitmap_to_png(dib: bytes) -> bytes:
    """A 32-bit BGRA bitmap, as stored in an .ico, re-encoded as RGBA PNG."""
    header_size, width, height, _planes, bpp, compression = struct.unpack_from(
        "<IiiHHI", dib, 0)
    # The stored height covers the colour image plus the 1-bit mask below it.
    height //= 2
    if bpp != 32 or compression != 0:
        raise ValueError(f"unsupported icon bitmap ({bpp} bpp, compression {compression})")
    stride = width * 4
    pixels = dib[header_size:header_size + stride * height]
    rows = bytearray()
    for y in range(height - 1, -1, -1):  # bitmaps are stored bottom-up
        row = bytearray(pixels[y * stride:(y + 1) * stride])
        row[0::4], row[2::4] = row[2::4], row[0::4]  # BGRA -> RGBA
        rows += b"\x00" + row
    return (PNG_MAGIC
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def main() -> int:
    default = os.path.join(os.environ.get("TYPHOONPATH", "/opt/typhoon/current"),
                           "icons", "cc.ico")
    path = sys.argv[1] if len(sys.argv) > 1 else default
    with open(path, "rb") as handle:
        image = largest_image(handle.read())
    png = image if image.startswith(PNG_MAGIC) else bitmap_to_png(image)
    sys.stdout.buffer.write(png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
