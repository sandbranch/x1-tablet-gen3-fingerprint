"""Build and send capture command 0x02 for this sensor (product 0x33).

`validitysensor.sensor.Sensor.build_cmd_02` raises `Not implemented` for any ROM
product other than 0x30, so the command is assembled here instead, reusing
python-validity for everything that is product-independent (chunk split/merge,
the timeslot-table patch) and `synaspi.lineupdate` for the part that is not.

`probe()` sends one command and reports what the sensor answers. It writes
nothing to flash — the destructive path is `Sensor.calibrate()`, which this
deliberately does not touch.
"""
import logging
from struct import pack, unpack

from validitysensor import timeslot as prg
from validitysensor.sensor import CaptureMode
from validitysensor.util import assert_status

from . import lineupdate, tables

log = logging.getLogger("synaspi.capture")

# The per-type constant array the DLL substitutes when the sensor reports no
# factory calibration values (VA 0x180132958, shared by 0x130/0x518/0x580/...).
FALLBACK_CAL_VALUES = bytes([0x80] * 40)


def calibration_values(sensor, factory_bits=None):
    """The array both calibration lines carry.

    The DLL prefers the sensor's own factory values and falls back to a
    per-type constant. Factory subtag 3 carries a 4-byte header, as
    python-validity's `factory_calibration_values` already assumes.
    """
    want = sensor.type_info.lines_per_calibration_data
    vals = getattr(sensor, "factory_calibration_values", None)
    if vals and len(vals) == want:
        log.info("calibration values: factory bit 3 (%d B)", len(vals))
        return bytes(vals)
    if vals:
        log.warning("factory bit 3 is %d B but the type wants %d — using the "
                    "constant fallback instead", len(vals), want)
    else:
        log.info("no factory calibration values; using the constant fallback")
    return FALLBACK_CAL_VALUES[:want] if want <= len(FALLBACK_CAL_VALUES) \
        else bytes([0x80] * want)


def build_cmd_02(sensor, mode: CaptureMode, req_lines: int, patch_again: bool = False,
                 max_lines: int = None):
    """Assemble the 0x02 payload. Returns (command_bytes, lines) for logging.

    `req_lines` is how many image lines the sensor is asked to return — it is a
    parameter of the request, not a property of the device, which is why the
    `calibration_frames` constant we could not find in the driver does not
    actually block a capture: it only has to agree with whatever the averaging
    step later assumes.

    `patch_again` is python-validity's non-CALIBRATE tweak to the timeslot table
    (`patch_timeslot_again`), inferred from the product-0x30 driver. Whether the
    0x33 path does it is unverified, so it is off by default.
    """
    chunks = [c for c in prg.split_chunks(sensor.hardcoded_prog)]

    tst = None
    for c in chunks:
        if c[0] == 0x34:
            # Same patch python-validity applies; it rewrites operand bytes
            # only, so instruction offsets — and therefore our anchors — are
            # unaffected.
            tst = sensor.patch_timeslot_table(c[1], True,
                                              sensor.type_info.repeat_multiplier)
            if patch_again:
                tst = sensor.patch_timeslot_again(tst)
            c[1] = tst
    if tst is None:
        raise Exception("capture program has no timeslot table (chunk 0x34)")

    chunks += [[0x17, b""]]                       # reply configuration

    lines = lineupdate.build_lines(sensor.type_info, tst,
                                   calibration_values(sensor))
    if max_lines is not None and len(lines) > max_lines:
        log.warning("truncating line update from %d to %d lines (debug)",
                    len(lines), max_lines)
        lines = lines[:max_lines]
    # repeat_multiplier - 1 rides in its own chunk on this path
    tail = lineupdate.serialize(lines, sensor.type_info.repeat_multiplier - 1)

    cmd = pack("<BHH", 2, sensor.bytes_per_line, req_lines)
    cmd += prg.merge_chunks(chunks) + tail
    # Serializer rule from 0x1800878c0: the payload is never left a multiple of
    # 0x40; four zero bytes are appended when it would be.
    if len(cmd) % 0x40 == 0:
        log.info("payload is a multiple of 64 (%d B) — appending the 4-byte pad",
                 len(cmd))
        cmd += b"\0" * 4
    return cmd, lines


def probe(sensor, frames: int = 3, read: bool = True, timeout: float = 20.0,
          max_lines: int = None, image_timeout: float = 10.0, out: str = None):
    """Send one capture command and report what the sensor answers.

    Uses CaptureMode.CALIBRATE deliberately: it is the only mode that adds no
    extra hardcoded chunks. python-validity's IDENTIFY/ENROLL modes splice in
    0x4e/0x26/0x2e blobs captured from a different (USB, product 0x30) device,
    which there is no reason to believe apply here.

    Writes nothing to flash.
    """
    from validitysensor import sensor as S

    req_lines = frames * sensor.lines_per_frame + 1
    cmd, lines = build_cmd_02(sensor, CaptureMode.CALIBRATE, req_lines,
                              max_lines=max_lines)
    log.info("cmd 0x02: %d bytes, %d line(s), req_lines=%d (%d frames x %d lines)",
             len(cmd), len(lines), req_lines, frames, sensor.lines_per_frame)
    for l in lines:
        log.info("   %r", l)

    prev, S.usb.resp_timeout = S.usb.resp_timeout, timeout
    try:
        rsp = S.tls.app(cmd)
    finally:
        S.usb.resp_timeout = prev
    log.info("response [%d B]: %s", len(rsp), rsp[:64].hex())
    if len(rsp) < 2:
        log.error("no status in response")
        return -1
    status, = unpack("<H", rsp[:2])
    if status != 0:
        log.error("sensor REJECTED the capture command: status 0x%04x", status)
        return status
    log.info("*** sensor ACCEPTED the capture command (status 0) ***")

    if not read:
        return 0
    expect = frames * sensor.lines_per_frame * sensor.bytes_per_line
    log.info("draining image frames (opcode 0x05); ends on 200 ms idle, "
             "%.1fs backstop", image_timeout)
    data = S.usb.read_image(timeout=image_timeout)
    if not data:
        log.warning("no image data came back")
        return 0
    import os
    if out is None:
        # legacy behaviour: env-selected path, auto-numbered after the first
        out = os.environ.get("SYNASPI_CAPTURE_OUT", "/tmp/forced_read.bin")
        globals()["_seq"] = globals().get("_seq", 0) + 1
        if globals()["_seq"] > 1:
            root, ext = os.path.splitext(out)
            out = "%s-%d%s" % (root, globals()["_seq"], ext)
    with open(out, "wb") as f:
        f.write(data)
    log.info("saved %d B to %s", len(data), out)
    log.info("*** IMAGE DATA: %d bytes (expected about %d) ***", len(data), expect)
    log.info("   first 64: %s", data[:64].hex())
    log.info("   last  64: %s", data[-64:].hex())
    return 0
