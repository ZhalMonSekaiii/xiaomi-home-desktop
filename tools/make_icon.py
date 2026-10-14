"""Draws the XHome app icon (orange rounded square + white house) without any image library.

    python tools/make_icon.py   ->  xhome/static/xhome.ico
"""
import struct
import zlib
from pathlib import Path

ORANGE = (255, 105, 0)
WHITE = (255, 255, 255)
OUT = Path(__file__).resolve().parent.parent / "xhome" / "static" / "xhome.ico"


def in_rounded_rect(x, y, r=0.22):
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    return 0 <= x <= 1 and 0 <= y <= 1 and (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_triangle(x, y, a, b, c):
    def side(p, q):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])
    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def in_house(x, y):
    roof = in_triangle(x, y, (0.17, 0.50), (0.50, 0.21), (0.83, 0.50))
    body = 0.28 <= x <= 0.72 and 0.46 <= y <= 0.79
    door = 0.435 <= x <= 0.565 and 0.585 <= y <= 0.79
    return (roof or body) and not door


def render(size, ss=4):
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            bg = fg = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = (px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size
                    if in_rounded_rect(x, y):
                        bg += 1
                        fg += in_house(x, y)
            n = ss * ss
            if bg == 0:
                row += bytes(4)
                continue
            t = fg / bg
            rgb = [round(o * (1 - t) + w * t) for o, w in zip(ORANGE, WHITE)]
            row += bytes(rgb + [round(255 * bg / n)])
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size, raw):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    OUT.write_bytes(header + entries + b"".join(images))
    (OUT.parent / "xhome-256.png").write_bytes(images[-1])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
