#!/usr/bin/env python3
"""End-to-end bring-up test for the SYNA8002 over SPI.

Run as root on a FRESH BOOT (the sensor answers only after a >=8 s power-low and
only once per boot; a stale gpioset holder pins the line and every read is 0xFF).

Stages, each gated so a failure localises the problem:
  1. transport   — power-cycle, bind spidev, cmd 0x01 device-info (must be
                   byte-identical to the Windows capture: starts 41 ...)
  2. inject      — load + wire the recovered host credential (offline-verified)
  3. handshake   — tls.open(): ClientHello -> ServerHello/Cert/CertReq/Done ->
                   Certificate(ours)+CKE+CertificateVerify(signed by d)+Finished.
                   Success here PROVES the recovered key authenticates the host.
  4. identify    — cmd 0x75 over the secure channel -> sensor type/name.
  4b. config     — upload the sensor CONFIG BLOB as cmd 0x06 (python-validity's
                   `init_hardcoded` equivalent, 7364 B for our ROM). Windows
                   sends exactly this, once, between the handshake and the first
                   capture; we used to skip it entirely. --no-config to skip.
  5. setup       — hardware tables + capture program (read-only). --calibrate
                   additionally runs calibrate(), which REWRITES sensor flash.

Usage:  sudo -E python3 -m synaspi.bringup [--trace]
"""
import argparse
import logging
import sys

import synaspi._pv as pv
from synaspi import identity
from synaspi.transport import SpiUsb

log = logging.getLogger("synaspi.bringup")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="store_true", help="log every SPI frame")
    ap.add_argument("--no-handshake", action="store_true",
                    help="stop after transport sanity (stage 1)")
    ap.add_argument("--handshake-only", action="store_true",
                    help="stop after the TLS handshake (stage 3)")
    ap.add_argument("--no-config", action="store_true",
                    help="skip stage 4b (the cmd 0x06 config blob upload)")
    ap.add_argument("--config-mask", type=lambda x: int(x, 0), default=None,
                    help="feature mask for stage 4b (default 0x400000, what "
                         "Windows uses on this tablet)")
    ap.add_argument("--config-group", type=int, default=None,
                    help="config table group for stage 4b (default 2)")
    ap.add_argument("--no-capture", action="store_true",
                    help="run identify (stage 4) but skip sensor setup (stage 5)")
    ap.add_argument("--capture", action="store_true",
                    help="stage 6: build and send capture command 0x02 and read "
                         "back a frame. Writes nothing to flash.")
    ap.add_argument("--frames", type=int, default=3,
                    help="frames to request in stage 6 (default 3)")
    ap.add_argument("--capture-repeat", type=int, default=1,
                    help="stage 6: captures to attempt in ONE session. The "
                         "capture re-arms itself; the config blob is sent once.")
    ap.add_argument("--repeat-config", action="store_true",
                    help="stage 6: re-send the cmd 0x06 config blob before each "
                         "repeat (what Windows does). The sensor refuses it with "
                         "0x04aa once a capture has run, so this is off.")
    ap.add_argument("--flash-probe", action="store_true",
                    help="after stage 5, probe the flash layer (read-only)")
    ap.add_argument("--image-timeout", type=float, default=10.0,
                    help="stage 6: seconds to wait for image data after the "
                         "command is accepted (finger window)")
    ap.add_argument("--max-lines", type=int, default=None,
                    help="debug: truncate the line update to N lines")
    ap.add_argument("--capture-timeout", type=float, default=20.0,
                    help="seconds to wait for the stage 6 capture response "
                         "(default 20)")
    ap.add_argument("--calibrate", action="store_true",
                    help="stage 5 also runs calibrate(), which ERASES AND REWRITES "
                         "the sensor's flash partition 6. Off by default.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.trace else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    usb = SpiUsb()
    usb.trace_enabled = args.trace

    # ---- stage 1: transport ----
    log.info("=== stage 1: transport (power-cycle + spidev bind + device info) ===")
    usb.open()
    info = usb.cmd(bytes.fromhex("01"), resp_len=0x26)
    log.info("device-info (cmd 0x01) [%d B]: %s", len(info), info.hex())
    if not info or set(info) <= {0xFF}:
        log.error("no device-info returned (all 0xFF). Sensor not answering — "
                  "was this a fresh boot with no stale gpio holder?")
        usb.close(); return 2
    log.info("stage 1 OK: sensor answers on the command layer")

    if args.no_handshake:
        usb.close(); return 0

    # ---- stage 2: identity ----
    log.info("=== stage 2: inject recovered host credential ===")
    tls = pv.tls_mod.Tls(usb)
    tls.trace_enabled = args.trace
    identity.inject(tls)
    log.info("stage 2 OK: priv_key / tls_cert / ecdh_q set")

    # optional minimal init (no blobs); tolerate failure and continue to handshake
    try:
        usb.send_init()
        log.info("send_init OK")
    except Exception as e:
        log.warning("send_init raised (%s) — continuing to handshake anyway", e)

    # ---- stage 3: TLS handshake ----
    log.info("=== stage 3: TLS mutual-auth handshake (tls.open) ===")
    # A truncated read can drop the server's Finished record: parse_tls_response
    # would then set secure_rx off the ChangeCipherSpec alone and tls.open()
    # returns "successfully" with the handshake never verified. Watch for it.
    saw_finish = []
    _orig_finish = tls.handle_finish
    def _finish(b, _o=_orig_finish, _s=saw_finish):
        _o(b)
        _s.append(True)
    tls.handle_finish = _finish
    try:
        tls.open()
    except Exception as e:
        log.error("handshake FAILED: %r", e)
        usb.close()
        return 3
    if not (tls.secure_tx and tls.secure_rx):
        log.error("handshake returned but channel not secure "
                  "(secure_tx=%s secure_rx=%s)", tls.secure_tx, tls.secure_rx)
        usb.close()
        return 4
    if not saw_finish:
        log.error("handshake never verified the server Finished record — the "
                  "response was truncated. Channel is NOT trustworthy.")
        usb.close()
        return 4
    log.info("*** HANDSHAKE COMPLETE — secure channel established. ***")
    log.info("The recovered host key authenticates the host to the sensor.")

    if args.handshake_only:
        usb.close(); return 0

    # route python-validity's module singletons to our instances so sensor.py runs
    import validitysensor.sensor as S
    import validitysensor.flash as F
    S.usb = usb
    S.tls = tls
    F.tls = tls

    # ---- stage 4: identify ----
    log.info("=== stage 4: identify sensor (cmd 0x75) ===")
    try:
        di = S.identify_sensor()
        log.info("device_info: %r", di)
        log.info("  -> type=0x%x name=%r", di.type, di.name.strip())
        if di.type != 0x580:
            log.warning("sensor type 0x%x is not the 0x580 we extracted tables "
                        "for — stage 5 will not find its hardware tables.", di.type)
    except Exception as e:
        log.error("identify FAILED: %r", e)
        usb.close(); return 5

    # ---- stage 4b: config blob (cmd 0x06) ----
    if not args.no_config:
        log.info("=== stage 4b: upload sensor config blob (cmd 0x06) ===")
        try:
            from synaspi import configblob
            rom = S.RomInfo.get()
            log.info("rom: %r", rom)
            kw = {}
            if args.config_mask is not None:
                kw["mask"] = args.config_mask
            if args.config_group is not None:
                kw["group"] = args.config_group
            if not configblob.send(tls, rom, **kw):
                log.error("config blob upload FAILED — capture will not run")
                usb.close(); return 8
        except Exception as e:
            log.error("stage 4b failed at: %r", e)
            usb.close(); return 8
    else:
        log.warning("stage 4b SKIPPED (--no-config): Windows always sends this "
                    "blob before capturing, so expect capture to do nothing")

    if args.no_capture:
        usb.close(); return 0

    # ---- stage 5: sensor setup (read-only unless --calibrate) ----
    log.info("=== stage 5: sensor setup%s ===",
             " + CALIBRATE (writes sensor flash!)" if args.calibrate else " (read-only)")
    try:
        from synaspi import sensorsetup
        sensor = sensorsetup.prepare(calibrate=args.calibrate)
        log.info("*** sensor ready: tables resolved, capture program loaded. ***")
        if not args.calibrate:
            log.info("calibration flash (partition 6): %s",
                     sensorsetup.calibration_flash_status())
            log.info("(stopped before calibrate() on purpose — it erases and "
                     "rewrites sensor flash partition 6. Pass --calibrate to run it.)")
        rc = 0
    except Exception as e:
        log.error("stage 5 failed at: %r", e)
        usb.close()
        return 6

    if args.flash_probe:
        log.info("=== flash probe (read-only) ===")
        try:
            from synaspi import flashprobe
            flashprobe.probe(tls)
        except Exception as e:
            log.error("flash probe failed: %r", e)

    # ---- stage 6: capture probe (no flash writes) ----
    if args.capture:
        log.info("=== stage 6: capture command 0x02 ===")
        try:
            from synaspi import capture
            import time as _t
            for _n in range(args.capture_repeat):
                if _n and args.repeat_config:
                    log.info("--- repeat %d: re-sending config blob ---", _n + 1)
                    try:
                        configblob.send(tls, rom, **kw)
                    except Exception as _e:
                        log.warning("  blob re-upload refused (%r) — capture "
                                    "re-arms itself, continuing", _e)
                _t0 = _t.time()
                _rc = capture.probe(sensor, frames=args.frames,
                                    timeout=args.capture_timeout,
                                    max_lines=args.max_lines,
                                    image_timeout=args.image_timeout)
                log.info("--- capture %d of %d: rc=%s in %.1fs ---",
                         _n + 1, args.capture_repeat, _rc, _t.time() - _t0)
                if _rc != 0:
                    break
            rc = 0 if _rc == 0 else 7
        except Exception as e:
            log.error("stage 6 failed at: %r", e)
            rc = 7

    usb.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
