"""One open sensor session: bring-up, repeated capture, clean teardown.

`bringup` is the staged diagnostic — every step gated so a failure localises.
This is the operational counterpart: open once, capture many times, close.
The enrolment tool and the daemon both drive the sensor through here.

Bring-up costs ~12 s (an 8 s power-low plus the handshake); each capture after
that is ~1.0 s. Two rules learned the hard way (see the project notes):

  * never send a plaintext transport command into an open secure session — it
    desynchronises the channel permanently. `capture()` IS the liveness check.
  * the config blob goes up exactly once per session. Re-sending it after a
    capture is refused with 0x04aa.
"""
import logging
import os
import time

import synaspi._pv as pv
from synaspi import capture as _capture
from synaspi import configblob, identity, sensorsetup
from synaspi.transport import SpiUsb

log = logging.getLogger(__name__)


class SessionError(Exception):
    pass


class Session:
    """An open, authenticated, capture-ready sensor session."""

    def __init__(self, trace: bool = False):
        self.trace = trace
        self.usb = None
        self.tls = None
        self.sensor = None
        self.rom = None
        self.device_info = None
        self._n = 0

    # ---- lifecycle ----
    def open(self):
        t0 = time.time()
        self.usb = SpiUsb()
        self.usb.trace_enabled = self.trace
        self.usb.open()
        info = self.usb.cmd(bytes.fromhex("01"), resp_len=0x26)
        if not info or set(info) <= {0xFF}:
            raise SessionError("sensor not answering (all 0xFF) — stale gpio "
                               "holder, or it needs a power cycle")

        self.tls = pv.tls_mod.Tls(self.usb)
        self.tls.trace_enabled = self.trace
        identity.inject(self.tls)
        try:
            self.usb.send_init()
        except Exception as e:
            log.debug("send_init raised (%r) — continuing", e)

        saw_finish = []
        _orig = self.tls.handle_finish
        self.tls.handle_finish = lambda b: (_orig(b), saw_finish.append(True))
        self.tls.open()
        if not (self.tls.secure_tx and self.tls.secure_rx):
            raise SessionError("handshake returned but channel is not secure")
        if not saw_finish:
            raise SessionError("server Finished never verified — reply was "
                               "truncated, channel is not trustworthy")

        import validitysensor.flash as F
        import validitysensor.sensor as S
        S.usb, S.tls, F.tls = self.usb, self.tls, self.tls
        self.device_info = S.identify_sensor()
        self.rom = S.RomInfo.get()
        if not configblob.send(self.tls, self.rom):
            raise SessionError("config blob refused")
        self.sensor = sensorsetup.prepare(calibrate=False)
        log.info("session open in %.1fs (%s)", time.time() - t0,
                 self.device_info.name.strip())
        return self

    def close(self):
        if self.usb is not None:
            try:
                self.usb.close()
            except Exception as e:
                log.debug("close raised (%r)", e)
            self.usb = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- use ----
    def capture(self, out: str = None, frames: int = 3, max_lines: int = 2,
                image_timeout: float = 1.0, timeout: float = 20.0) -> bytes:
        """Run one capture and return the raw image blob.

        Raises SessionError if the sensor rejects it or the session has died
        (which is what a suspend/resume looks like: the first capture after
        resume throws immediately).
        """
        if self.sensor is None:
            raise SessionError("session is not open")
        self._n += 1
        path = out or "/tmp/synaspi-capture-%d.bin" % self._n
        try:
            rc = _capture.probe(self.sensor, frames=frames, timeout=timeout,
                                max_lines=max_lines, image_timeout=image_timeout,
                                out=path)
        except Exception as e:
            raise SessionError("capture failed (session dead?): %r" % (e,))
        if rc != 0:
            raise SessionError("sensor rejected the capture: rc=%s" % (rc,))
        if not os.path.exists(path):
            raise SessionError("capture accepted but no image data came back")
        with open(path, "rb") as f:
            return f.read()
