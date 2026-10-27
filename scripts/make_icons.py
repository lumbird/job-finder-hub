"""Draw the app icons (a white briefcase on blue) with the standard library only.

Usage: python scripts/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = (1, 114, 173)  # Pico's azure primary
FG = (255, 255, 255)


def in_rrect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def glyph(x: float, y: float) -> bool:
    """Briefcase in unit coordinates centred on (0.5, 0.5)."""
    body = in_rrect(x, y, 0.22, 0.36, 0.78, 0.76, 0.06)
    handle = in_rrect(x, y, 0.38, 0.24, 0.62, 0.40, 0.05) and not in_rrect(x, y, 0.44, 0.30, 0.56, 0.42, 0.02)
    clasp_gap = 0.22 <= x <= 0.78 and 0.53 <= y <= 0.56 and not (0.46 <= x <= 0.54)
    return (body or handle) and not clasp_gap


def pixel(x: float, y: float, maskable: bool):
    if maskable:
        inside = True
        gx, gy = 0.5 + (x - 0.5) / 0.72, 0.5 + (y - 0.5) / 0.72  # keep glyph inside the safe zone
    else:
        inside = in_rrect(x, y, 0, 0, 1, 1, 0.22)
        gx, gy = x, y
    if not inside:
        return None
    return FG if glyph(gx, gy) else BG


def render(size: int, maskable: bool) -> bytes:
    ss = 3  # supersampling for smooth edges
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = pixel((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size, maskable)
                    if c:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            n = ss * ss
            alpha = acc[3] // n
            if alpha:
                hits = acc[3] // 255
                row += bytes((acc[0] // hits, acc[1] // hits, acc[2] // hits, alpha))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="22" fill="#0172ad"/>
<path fill="#fff" fill-rule="evenodd" d="M43 24h14a5 5 0 0 1 5 5v7h10a6 6 0 0 1 6 6v11H54v-2h-8v2H22V42a6 6 0 0 1 6-6h10v-7a5 5 0 0 1 5-5zm1 6v6h12v-6z"/>
<path fill="#fff" d="M22 56h24v3h8v-3h24v14a6 6 0 0 1-6 6H28a6 6 0 0 1-6-6z"/>
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon-192.png").write_bytes(render(192, maskable=False))
    (OUT / "icon-512.png").write_bytes(render(512, maskable=False))
    (OUT / "icon-maskable-512.png").write_bytes(render(512, maskable=True))
    (OUT / "icon.svg").write_text(SVG)
    print(f"Wrote icons to {OUT}")


if __name__ == "__main__":
    main()
