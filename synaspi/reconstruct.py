"""Decode a raw SYNA8002 capture blob (transport opcode 0x05) into an image.

Frame format, established from hardware captures on 2026-08-18
(`re/synafp/captures/`), not from the driver:

    line stride = 152 bytes = 8-byte header + 144 pixels
    header      = 01 fe <u16 line counter, LE> e0 f7 03 00
                  the last line of a blob instead carries 01 01 00 00 ..
                  as a terminator, and its counter is 0
    the counter increments by exactly 1 within a frame and jumps between
    frames, which is how frame boundaries are found
    a full frame is 80 lines (`lines_per_frame`)
    lines are interleaved in PAIRS -- (1,2), (3,4), ... NOT (0,1), (2,3):
    the two members of a pair are redundant samples of the same row (mean
    |diff| 1.9 vs 99.1 for the wrong parity), so a frame yields 40 rows,
    which is `lines_per_calibration_data`

So one frame decodes to a 144 x 40 image.
"""
import struct
from typing import List

LINE = 152
HDR = 8
PIXELS = LINE - HDR          # 144
LINES_PER_FRAME = 80


def split_lines(blob: bytes):
    """-> list of (counter, is_terminator, pixels)."""
    out = []
    for i in range(len(blob) // LINE):
        h = blob[i * LINE:i * LINE + HDR]
        out.append((struct.unpack_from("<H", h, 2)[0], h[:2] != b"\x01\xfe",
                    blob[i * LINE + HDR:(i + 1) * LINE]))
    return out


def frames(blob: bytes) -> List[List[bytes]]:
    """Split into runs of consecutively-numbered lines, keeping only whole
    frames. A capture usually starts mid-frame, so the first run is short."""
    lines = [l for l in split_lines(blob) if not l[1]]
    runs, cur = [], []
    for ctr, _, px in lines:
        if cur and ctr != cur[-1][0] + 1:
            runs.append([p for _, p in cur]); cur = []
        cur.append((ctr, px))
    if cur:
        runs.append([p for _, p in cur])
    return [r for r in runs if len(r) >= LINES_PER_FRAME]


def pair_parity(frame: List[bytes]) -> int:
    """Which index the interleaved pairs start on. Detected, not assumed: the
    two members of a pair are near-identical (mean |diff| ~1.9) while the wrong
    parity straddles pair boundaries (~99), so the split is unambiguous."""
    def cost(off):
        tot = cnt = 0
        for i in range(off, len(frame) - 1, 2):
            a, b = frame[i], frame[i + 1]
            tot += sum(abs(p - q) for p, q in zip(a, b)); cnt += len(a)
        return tot / max(cnt, 1)
    return 0 if cost(0) <= cost(1) else 1


def deinterleave(frame: List[bytes]) -> List[List[float]]:
    """Average the interleaved pairs into one row each."""
    off = pair_parity(frame)
    f = frame[off:off + LINES_PER_FRAME]
    return [[(a + b) / 2 for a, b in zip(f[i], f[i + 1])]
            for i in range(0, len(f) - 1, 2)]


def subtract(img, ref):
    return [[a - b for a, b in zip(p, q)] for p, q in zip(img, ref)]


def normalize_columns(img):
    """Remove per-column fixed-pattern offset."""
    h = len(img)
    for c in range(len(img[0])):
        m = sum(img[r][c] for r in range(h)) / h
        for r in range(h):
            img[r][c] -= m
    return img


def smooth(img, k=1):
    h, w = len(img), len(img[0])
    out = [[0.0] * w for _ in range(h)]
    for r in range(h):
        for c in range(w):
            vs = [img[y][x] for y in range(max(0, r - k), min(h, r + k + 1))
                  for x in range(max(0, c - k), min(w, c + k + 1))]
            out[r][c] = sum(vs) / len(vs)
    return out


def to_png(img, path, sx=6, sy=12):
    import zlib
    flat = [v for row in img for v in row]
    lo, hi = min(flat), max(flat)
    rng = (hi - lo) or 1
    w = len(img[0])
    rows = []
    for row in img:
        e = bytes(int((v - lo) * 255 / rng) for v in row for _ in range(sx))
        rows += [e] * sy
    raw = b"".join(b"\0" + r for r in rows)

    def ck(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + ck(b"IHDR", struct.pack(">IIBBBBB", w * sx, len(rows),
                                          8, 0, 0, 0, 0))
                + ck(b"IDAT", zlib.compress(raw, 9)) + ck(b"IEND", b""))
    return w * sx, len(rows)


def average_frames(fr) -> List[List[float]]:
    """Mean of every complete frame, pixel by pixel.

    Frames are TEMPORAL REPEATS of the same 144 x 40 view -- consecutive frames
    differ by ~1.4, i.e. the noise floor -- so they must be averaged, not
    stacked. Asking for more frames buys SNR, not image area.
    """
    per = [deinterleave(f) for f in fr]
    n = len(per)
    return [[sum(p[r][c] for p in per) / n for c in range(len(per[0][0]))]
            for r in range(len(per[0]))]


def reconstruct(capture: bytes, baseline: bytes = None, clean=True):
    """Raw blob (+ optional no-finger baseline) -> 144 x 40 float image."""
    fr = frames(capture)
    if not fr:
        raise ValueError("no complete 80-line frame in this capture")
    img = average_frames(fr)
    if baseline:
        bf = frames(baseline)
        if bf:
            img = subtract(img, average_frames(bf))
    if clean:
        img = smooth(normalize_columns(img))
    return img


if __name__ == "__main__":
    import sys
    cap = open(sys.argv[1], "rb").read()
    ref = open(sys.argv[2], "rb").read() if len(sys.argv) > 2 else None
    out = sys.argv[3] if len(sys.argv) > 3 else "out.png"
    img = reconstruct(cap, ref)
    print("frames:", len(frames(cap)), "->", len(img), "x", len(img[0]))
    print("wrote %s %dx%d" % ((out,) + to_png(img, out)))
