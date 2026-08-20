"""Bring `validitysensor.sensor.Sensor` up for sensor type 0x580.

python-validity's `Sensor.open()` does three things we cannot use as-is:

  * it looks up two hardware tables that have no 0x580 row  -> `synaspi.tables`
    supplies them, extracted from the Windows SPI driver and cross-validated
    against every type python-validity already knows;
  * it hardcodes three calibration constants in an if/elif over the sensor type,
    with an `else: raise` that is exactly where our bring-up stopped;
  * it ends by calling `calibrate()`, which ERASES AND REWRITES flash partition 6
    when the sensor has no locally-cached calibration. That is a destructive
    write to a sensor still holding its Windows calibration, so it must never
    happen as a side effect of probing.

So `prepare()` reproduces the read-only prologue of `Sensor.open()` and stops
before `calibrate()`. Pass calibrate=True to go the whole way, deliberately.
"""
import logging

from validitysensor import timeslot as prg
from validitysensor.sensor import Sensor, RomInfo, identify_sensor, get_factory_bits
from validitysensor.table_types import SensorTypeInfo, SensorCaptureProg
from struct import unpack

from . import tables  # noqa: F401  (registers the 0x580 rows on import)

log = logging.getLogger("synaspi.sensorsetup")

# key_calibration_line is lines_per_calibration_data/2 — the rule holds for both
# types python-validity hardcodes (0x199: 112/2 = 0x38, 0xdb: 144/2 = 0x48), and
# for 0x580 (lpcd 40) it gives 20.
#
# calibration_frames and calibration_iterations are NOT known for our type.
# python-validity has 3/3 for 0x199 and 6/0 for 0xdb with a "TODO: work out where
# it's really coming from" against both, and neither value appears in either DLL
# table we decoded. The defaults below are 0x199's; they matter only once
# calibration/capture actually runs, so treat them as the next thing to pin down.
CALIBRATION_FRAMES = 3
CALIBRATION_ITERATIONS = 3


def prepare(calibrate: bool = False) -> Sensor:
    """Read-only prologue of Sensor.open(). Returns a ready Sensor.

    With calibrate=False (default) nothing is written to the sensor.
    """
    s = Sensor()
    s.device_info = identify_sensor()
    log.info("sensor: %s (type 0x%x)", s.device_info.name.strip(), s.device_info.type)

    s.type_info = SensorTypeInfo.get_by_type(s.device_info.type)
    if s.type_info is None:
        raise Exception("no SensorTypeInfo for sensor type 0x%x" % s.device_info.type)

    s.key_calibration_line = s.type_info.lines_per_calibration_data // 2
    s.calibration_frames = CALIBRATION_FRAMES
    s.calibration_iterations = CALIBRATION_ITERATIONS

    s.rom_info = RomInfo.get()
    log.info("rom: %r", s.rom_info)

    s.hardcoded_prog = SensorCaptureProg.get(s.rom_info, s.device_info.type, 0x18, 0x19)
    if s.hardcoded_prog is None:
        raise Exception("no capture program for rom %r and sensor type 0x%x"
                        % (s.rom_info, s.device_info.type))

    lines_2d = [unpack('<L', v)[0] for [k, v] in prg.split_chunks(s.hardcoded_prog)
                if k == 0x2f][0]
    s.lines_per_frame = lines_2d * s.type_info.repeat_multiplier
    s.bytes_per_line = s.type_info.bytes_per_line

    factory_bits = get_factory_bits(0x0e00)
    s.factory_calibration_values = factory_bits[3][4:]
    if 7 in factory_bits:
        s.factory_calib_data = factory_bits[7][4:]

    log.info("capture program %d B, %d lines/frame, %d bytes/line, "
             "key_calibration_line=%d, factory bits %s",
             len(s.hardcoded_prog), s.lines_per_frame, s.bytes_per_line,
             s.key_calibration_line, sorted(factory_bits))

    if calibrate:
        log.warning("calibrating — this ERASES AND REWRITES sensor flash partition 6")
        s.calibrate()
    return s


def calibration_flash_status():
    """Read-only: report what flash partition 6 already holds.

    `Sensor.calibrate()` only skips the erase+rewrite when a locally cached
    calib-data file exists AND the partition validates. The sensor came out of a
    working Windows install, so the partition should already hold a valid
    clean-slate image — worth knowing before anyone runs --calibrate.

    Returns 'valid' / 'blank' / 'other', or 'unreadable: <err>'.
    """
    from hashlib import sha256
    from struct import unpack
    from validitysensor.flash import read_flash, read_flash_all
    try:
        start = read_flash(6, 0, 0x44)
    except Exception as e:
        return "unreadable: %r" % (e,)
    if start == b'\xff' * 0x44:
        return "blank"
    magic, l = unpack('<HH', start[:4])
    if magic != 0x5002:
        return "other (magic 0x%04x)" % magic
    hs, zeroes = start[4:0x24], start[0x24:0x44]
    if zeroes != b'\0' * 0x20:
        return "other (unexpected padding)"
    try:
        img = read_flash_all(6, 0x44, l)
    except Exception as e:
        return "header ok (%d B image) but body unreadable: %r" % (l, e)
    return "valid (%d B image)" % l if hs == sha256(img).digest() \
        else "other (hash mismatch, %d B)" % l
