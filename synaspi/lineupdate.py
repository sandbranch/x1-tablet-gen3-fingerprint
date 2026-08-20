"""The `line update` payload of capture command 0x02, for ROM product 0x33.

python-validity implements this only for ROM product 0x30, in two variants
(`line_update_type_1` / `line_update_type_2`) selected by sensor type. Our ROM
reports product **0x33**, which in the Windows driver takes a third builder
entirely (`0x1800a3130` in `scsWindsorLineUpdate.c`), and inside that a second
fork on `major==6 && minor>=0x0b` — our 6.21 takes the "modern" side. This module
is that path. Full derivation, with addresses: ../../LINE-UPDATE.md.

Differences from python-validity that actually change bytes on the wire:

* Line data is **bit-packed**, not raw. Each array is reduced to
  `(value - min)` packed at `bit_width(max - min)` bits per sample, LSB first;
  `min` and the bit count ride in the line's 4-byte tag.
* Line sequence numbers start at **2**, which routes every line's data out of
  chunk 0x30 and into a second chunk **0x43**. Chunk 0x30 keeps only the
  mask/flags table. python-validity emits the 0x30 half alone.
* An extra chunk **0x44** carries `repeat_multiplier - 1`.
"""
import logging
from struct import pack

from validitysensor import timeslot as prg

log = logging.getLogger("synaspi.lineupdate")

# Ports whose first "Write Register" instruction anchors a calibration line.
PORT_20FC = 0x800020fc
PORT_203C = 0x8000203c
PORT_2040 = 0x80002040


def bit_width(v: int) -> int:
    """Bits needed to represent v (0x180096940). bit_width(0) == 0."""
    if v == 0:
        return 0
    n = 1
    while v >> 1:
        v >>= 1
        n += 1
    return n


def pack_samples(data: bytes):
    """The 0x180095520 transform: bit-pack `data` as offsets from its minimum.

    Returns (payload, bits, base). Mirrors the DLL exactly, including the
    degenerate all-equal case, where bits == 0 and the payload is empty.
    """
    base, top = min(data), max(data)
    bits = bit_width(top - base)
    if bits == 0:
        # Every sample equal: nothing to encode, `base` alone carries the value.
        # (The DLL still calls its packer here, writing into a zero-length
        # allocation — don't reproduce that.)
        return b"", 0, base
    out = bytearray((bits * len(data) + 7) // 8)
    for i, v in enumerate(data):
        val, off = v - base, i * bits
        byte, rem = off // 8, off % 8
        out[byte] |= (val << rem) & 0xFF
        if 8 - rem < bits:                      # spills into the next byte
            out[byte + 1] |= val >> (8 - rem)
    return bytes(out), bits, base


class Line:
    """One line-update entry (the 24-byte struct the DLL builds in memory)."""

    def __init__(self, mask, flags, tags, data):
        self.mask, self.flags, self.tags, self.data = mask, flags, tags, data

    @property
    def seq(self):
        return (self.flags >> 20) & 0xF

    def __repr__(self):
        return ("Line(mask=0x%x, flags=0x%08x, seq=%d, tags=%s, data=%d B)"
                % (self.mask, self.flags, self.seq,
                   "%02x %02x %04x" % self.tags, len(self.data)))


def build_lines(type_info, tst: bytes, cal_values: bytes):
    """The line list for product 0x33 / ROM >= 6.11.

    `tst` is the timeslot program (chunk 0x34 payload); `cal_values` is the
    per-unit calibration array — factory bit 3 if the sensor reports it, else
    the per-type constant table the DLL falls back to.

    Only lines whose anchor actually exists in `tst` are emitted, which is what
    the DLL does and what keeps the missing factory bit 7 harmless: the line it
    would feed (port 0x800020fc) has no anchor in our capture program.
    """
    lines, seq = [], 2

    # 1. the type's calibration blob, at the 2nd "Enable Rx" instruction.
    hit = prg.find_nth_insn(tst, 6, 2)
    if hit is not None:
        pc, _ = hit
        lines.append(Line(0xFF, (seq << 20) | 0x7000000 | (pc + 1),
                          (0x0F, 0x00, 0x0000), type_info.calibration_blob))
        seq += 1

    # 2./3. the calibration array, bit-packed, at two register-write anchors.
    for port in (PORT_203C, PORT_2040):
        hit = prg.find_nth_regwrite(tst, port, 1)
        if hit is None:
            log.debug("no anchor for port 0x%08x — line skipped", port)
            continue
        pc, _ = hit
        payload, bits, base = pack_samples(cal_values)
        lines.append(Line(0xFF, (seq << 20) | 0x7000000 | (pc + 1),
                          ((bits - 1) & 0xFF | 8, base, 0x0000), payload))
        seq += 1

    # The port-0x800020fc line needs factory bit 7, which this sensor does not
    # report, and 0x580 has no constant fallback for it. The DLL would skip it
    # for the same reason; assert that the anchor is absent so a future capture
    # program with one does not silently lose the line.
    if prg.find_nth_regwrite(tst, PORT_20FC, 1) is not None:
        log.warning("capture program HAS a 0x800020fc anchor — that line needs "
                    "factory bit 7 and is not implemented")
    return lines


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def serialize(lines, mult_minus_1: int) -> bytes:
    """Lay the lines out as command chunks (serializer `0x1800a5ab0`).

    Lines split on `seq`: <= 1 puts its data inline in chunk 0x30, > 1 puts a
    4-byte tag plus the data in chunk 0x43. The modern path starts at seq 2, so
    in practice every line lands in 0x43 and 0x30 carries only the table.
    """
    lo = [l for l in lines if l.seq <= 1]
    hi = [l for l in lines if l.seq > 1]

    out = b""
    if mult_minus_1:
        out += pack("<HHI", 0x44, 4, mult_minus_1)

    body = pack("<I", len(lines))
    body += b"".join(pack("<II", l.mask, l.flags) for l in lines)
    body += b"".join(_pad4(l.data) for l in lo if l.data)
    out += pack("<HH", 0x30, len(body)) + body

    if hi:
        body = b"".join(pack("<BBH", *l.tags) + _pad4(l.data) for l in hi)
        # The DLL sizes this chunk as n*4 + align4(sum of lengths) but writes each
        # line's data padded individually. Those agree only when at most one line
        # has a length that is not a multiple of 4 — with three lines of 40/25/25
        # they differ by 4, and declaring the written length (rather than this one)
        # is what made the sensor fall off the bus.
        declared = len(hi) * 4 + (sum(len(l.data) for l in hi) + 3) // 4 * 4
        out += pack("<HH", 0x43, declared) + body
    return out
