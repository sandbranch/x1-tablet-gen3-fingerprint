"""SPI transport for the SYNA8002, exposing a python-validity `Usb`-compatible
interface so `validitysensor.tls` / `.sensor` / `.flash` run unchanged on top.

Wire protocol (recovered in re/synafp/PROTOCOL.md, proven live):
    STATUS  TX [01][seq][00*4]      RX [FF][FF][status u32 LE]
    WRITE   TX [02][payload...]
    READ    TX [03][seq2][FF*n]     RX [FF][FF][payload*n]
Status bits: 0x80000000 BUSY, 0x40000000 ERROR, 0x10000000 RESPONSE-AVAIL,
0x08000000 READY.

The one adaptation vs USB: the SPI status word never reports the response length
(always 0xFFFF in this firmware), so cmd() OVER-READS a fixed window and TRIMS.
TLS record streams are trimmed exactly by walking records (trailing 0xFF idle-fill
would otherwise be parsed as a bogus TLS record); control replies are left as-is
because their only consumer, assert_status, reads just the first two bytes.
"""
import ctypes
import fcntl
import logging
import os
import struct
import signal
import subprocess
import time

DEV = "/dev/spidev0.0"
SPEED = 10_000_000
GPIO_CHIP = "gpiochip0"
GPIO_PWR = 42                       # host-driven power/reset (active high)

# spidev ioctls
SPI_IOC_MESSAGE_1 = 0x40206B00
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

OP_STATUS, OP_WRITE, OP_READ = 0x01, 0x02, 0x03
OP_IMAGE = 0x05          # raw (non-TLS) bulk image path; Windows never uses it
BUSY, ERROR = 0x80000000, 0x40000000
RESP, READY = 0x10000000, 0x08000000

# Default over-read window for a command response. Every SPI transfer is
# [op][seq] + n bytes, and spidev's default per-message buffer is 4096, so the
# whole frame (n + 2) must stay <= 4096 -> n <= 4094. Handshake records fit
# easily in this. Bigger reads (image capture) raise spidev bufsiz first.
DEFAULT_RESP = 0x0FF0
SPI_BUF_MAX = 0x1000 - 2

log = logging.getLogger("synaspi.transport")


def _tls_scan(buf: bytes):
    """Walk the TLS records in buf.

    Returns (end, is_tls, complete):
      end       offset just past the last COMPLETE record
      is_tls    buf starts with a plausible TLS record header (else it is a
                plain control reply and must be handed back untouched)
      complete  the bytes after `end` are pure 0xFF idle-fill, i.e. nothing was
                cut off mid-record. False means the response was only partly
                staged when we read it and the rest is still on the sensor.
    """
    if len(buf) < 5 or buf[0] not in (0x14, 0x15, 0x16, 0x17) \
            or buf[1] != 0x03 or buf[2] != 0x03:
        return 0, False, True
    off, n = 0, len(buf)
    while off + 5 <= n:
        t, mj, mn, sz = struct.unpack(">BBBH", buf[off:off + 5])
        if t not in (0x14, 0x15, 0x16, 0x17) or mj != 0x03 or mn != 0x03:
            break
        end = off + 5 + sz
        if end > n:
            return off, True, False          # truncated mid-record
        off = end
    # a complete stream is followed only by idle-fill
    return off, True, set(buf[off:]) <= {0xFF}


def _tls_trim(buf: bytes) -> bytes:
    end, is_tls, _ = _tls_scan(buf)
    return buf[:end] if is_tls and end else buf


class SpiUsb:
    """Drop-in for validitysensor.usb.Usb over SPI."""

    def __init__(self):
        self.trace_enabled = False
        self.fd = None
        self.seq = 0          # STATUS counter (.data 0x180203368)
        self.seq2 = 0         # READ counter   (.data 0x180203369)
        self._pwr = None
        self.buf_max = SPI_BUF_MAX
        # Response window for cmd(). A capture command runs a program on the
        # sensor and can take far longer to answer than a control command, so
        # callers raise this around such a call.
        self.resp_timeout = 3.0
        self.dev = object()   # non-None sentinel; init.close() checks `usb.dev is not None`

    # ---- low-level spidev ----
    def _xfer(self, tx: bytes) -> bytes:
        t = ctypes.create_string_buffer(tx, len(tx))
        r = ctypes.create_string_buffer(len(tx))
        fcntl.ioctl(self.fd, SPI_IOC_MESSAGE_1, struct.pack(
            "QQIIHBBBBBB", ctypes.addressof(t), ctypes.addressof(r), len(tx),
            SPEED, 0, 8, 0, 0, 0, 0, 0))
        return r.raw

    def _status(self) -> int:
        rx = self._xfer(bytes([OP_STATUS, self.seq & 0xFF, 0, 0, 0, 0]))
        self.seq += 1
        return struct.unpack("<I", rx[2:6])[0]

    def _wait(self, mask: int, secs: float, label: str) -> int:
        t0 = time.monotonic()
        st = 0xFFFFFFFF
        while time.monotonic() - t0 < secs:
            st = self._status()
            if st != 0xFFFFFFFF and not st & BUSY and st & mask:
                return st
            time.sleep(0.001)
        raise TimeoutError(f"{label}: timeout (last 0x{st:08x})")

    def _write(self, payload: bytes):
        self._xfer(bytes([OP_WRITE]) + payload)

    def _read(self, n: int) -> bytes:
        if n > self.buf_max:
            n = self.buf_max
        rx = self._xfer(bytes([OP_READ, self.seq2 & 0xFF]) + b"\xff" * n)
        self.seq2 += 1
        return rx[2:2 + n]

    def _drain(self):
        """If a response is pending but the sensor is not READY (e.g. a prior
        aborted read left it buffered), read and discard so it returns to READY."""
        st = self._status()
        if st != 0xFFFFFFFF and not st & BUSY and st & RESP and not st & READY:
            log.warning("draining stale response (status 0x%08x)", st)
            self._read(self.buf_max)

    # ---- power / device binding ----
    def _ensure_bufsiz(self, want=65536):
        """Raise spidev's per-message buffer so image reads fit in one transfer.
        bufsiz is a load-time module param, so if it is too small we unbind and
        reload the module. Returns the effective bufsiz."""
        try:
            cur = int(open("/sys/module/spidev/parameters/bufsiz").read())
        except OSError:
            cur = -1
        if cur >= want:
            self.buf_max = cur - 2
            return cur
        # unbind our device (if bound) then reload spidev with a bigger buffer
        try:
            with open("/sys/bus/spi/drivers/spidev/unbind", "w") as f:
                f.write("spi-SYNA8002:00")
        except OSError:
            pass
        subprocess.run(["modprobe", "-r", "spidev"], check=False)
        subprocess.run(["modprobe", "spidev", "bufsiz=%d" % want], check=False)
        try:
            cur = int(open("/sys/module/spidev/parameters/bufsiz").read())
        except OSError:
            cur = 4096
        self.buf_max = cur - 2
        log.info("spidev bufsiz = %d", cur)
        return cur

    def _bind_spidev(self):
        self._ensure_bufsiz()
        base = "/sys/bus/spi/devices/spi-SYNA8002:00"
        try:
            with open(base + "/driver_override", "w") as f:
                f.write("spidev")
        except OSError:
            pass
        try:
            with open("/sys/bus/spi/drivers/spidev/bind", "w") as f:
                f.write("spi-SYNA8002:00")
        except OSError:
            pass

    def _gpio(self, value: int, hold: bool):
        args = ["gpioset", "--chip", GPIO_CHIP, "--consumer", "synaspi"]
        if hold:
            args += ["--hold-period", "3600s"]
        args += [f"{GPIO_PWR}={value}"]
        return subprocess.Popen(args)

    def _reap_stale_holders(self):
        """Kill any gpioset left over from a previous run.

        A holder that outlives its parent (SIGKILL, crash, ctrl-C) keeps line
        42 driven HIGH, so the power-low below never actually removes power:
        the sensor keeps its session state and the next config-blob upload is
        refused with 0x04aa. Silently reaping them makes bring-up idempotent.
        """
        pat = f"gpioset --chip {GPIO_CHIP} --consumer synaspi"
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        pids = [int(x) for x in out.stdout.split() if x.isdigit()
                and int(x) != os.getpid()]
        if not pids:
            return
        log.warning("reaping %d stale gpioset holder(s): %s", len(pids), pids)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.2)

    def power_cycle(self, off_secs: float = 8.0, settle: float = 2.0):
        """>=8 s power-low then high, per the proven bring-up sequence. Leaves a
        held gpioset process keeping the line high; released in close()."""
        self._reap_stale_holders()
        p = self._gpio(0, hold=False)
        time.sleep(off_secs)
        p.terminate(); p.wait()
        self._pwr = self._gpio(1, hold=True)
        time.sleep(settle)

    # ---- Usb-compatible API ----
    def open(self, vendor=None, product=None):
        self._bind_spidev()
        self.power_cycle()
        self.fd = os.open(DEV, os.O_RDWR)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPEED))
        # sanity: sensor should be READY on a fresh boot
        self._wait(READY, 3.0, "initial READY")

    def open_dev(self, dev):
        self.open()

    def open_devpath(self, busnum, address):
        self.open()

    def close(self):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            if self._pwr is not None:
                self._pwr.terminate()
                self._pwr = None

    def cmd(self, out, resp_len: int = DEFAULT_RESP) -> bytes:
        """Send one command payload, return the response body (TLS-trimmed)."""
        if callable(out):
            out = out()
            if not out:
                return b""
        self._trace(">cmd> %s" % out.hex())
        self._drain()
        self._wait(READY, 3.0, "cmd READY")
        self._write(out)
        st = self._wait(RESP, self.resp_timeout, "cmd RESPONSE")
        if st & ERROR:
            log.warning("sensor ERROR bit set on response (status 0x%08x)", st)
        raw = self._read(resp_len)
        # A response can be only partly staged when RESPONSE-AVAILABLE goes up:
        # we then read a truncated record, the rest stays queued on the sensor,
        # and every later reply is off by that much. (Seen once at the
        # handshake->app-data boundary as "Unexpected TLS version 4 255".)
        # Keep pulling until the record stream ends on an idle-fill boundary.
        for _ in range(3):
            end, is_tls, complete = _tls_scan(raw)
            if not is_tls or complete:
                break
            log.warning("partial TLS read (%d B, %d complete) — reading the rest",
                        len(raw), end)
            try:
                self._wait(RESP, 1.0, "cmd RESPONSE (continuation)")
            except TimeoutError:
                log.warning("no continuation available; reply stays truncated")
                break
            raw += self._read(resp_len)
        body = _tls_trim(raw)
        if body is raw:
            # not a TLS record stream: a plain control reply, whose only
            # consumer (assert_status) reads just the first two bytes.
            self._trace("raw reply (untrimmed) head: %s" % raw[:64].hex())
        self._trace("<cmd< %s" % body.hex())
        return body

    image_timeout = 10.0

    def image_status(self):
        """-> (busy, read_id, pending_bytes) decoded from the status word.

        After an accepted capture the status carries a different layout from
        the command path: rx[2] is a read id with 0x80 = still filling, and
        rx[3:5] is a staged-byte count. RESPONSE (0x10 in rx[5]) is never set
        for image data, which is why waiting on it hangs forever.
        """
        st = self._status()
        if st == 0xFFFFFFFF:
            return True, 0, 0
        return bool(st & 0x80), st & 0x7F, (st >> 8) & 0xFFFF

    def read_image(self, timeout=10.0, max_bytes=1 << 20, idle_ms=200):
        """Drain staged image frames over opcode 0x05.

        Windows reads images TLS-wrapped over opcode 0x03; 0x05 is a raw path
        that returns the frames in the clear. The read id must be exactly the
        one the status reports -- neighbouring ids return all-0xFF.

        What ends the read is `idle_ms` of nothing pending, NOT `timeout` --
        the timeout is only a backstop. That distinction is worth 0.7 s per
        capture: the sensor stages the whole image in ~0.3 s and then goes
        quiet, so waiting out a 1 s timeout tripled the cost of every frame
        (measured 1.02 s -> 0.33 s, byte-identical output). Frame count does
        not change the drain time; the dead tail was the whole difference.

        Do not size `idle_ms` down to nothing: a genuinely slow stage would
        then look like the end of the image and truncate it.
        """
        idle_limit = max(1, int(idle_ms / 25))
        out = b""
        t0 = time.monotonic()
        idle = 0
        while time.monotonic() - t0 < timeout and len(out) < max_bytes:
            busy, rid, pending = self.image_status()
            if busy or not pending:
                idle += 1
                if idle > idle_limit and out:
                    break
                time.sleep(0.025)
                continue
            n = min(pending, self.buf_max)
            rx = self._xfer(bytes([OP_IMAGE, rid]) + b"\xff" * n)
            chunk = rx[2:2 + n]
            if set(chunk) <= {0xFF}:
                log.debug("empty chunk at id 0x%02x, stopping", rid)
                break
            idle = 0
            out += chunk
            log.info("  image chunk: %d B (id 0x%02x, total %d)",
                     len(chunk), rid, len(out))
        return out or None

    def read_82(self) -> bytes:
        """Bulk image/data read. Larger window; RESPONSE may already be pending.

        Logs every status change while waiting: the Windows trace shows the
        image arrives ~1.4 s AFTER the command, once a finger is present, so a
        silent fixed-timeout wait hides whether the sensor ever reacts at all.
        """
        t0 = time.monotonic()
        last = None
        while time.monotonic() - t0 < self.image_timeout:
            st = self._status()
            if st != last:
                log.info("  [%6.1fs] status 0x%08x", time.monotonic() - t0, st)
                last = st
            if st != 0xFFFFFFFF and not st & BUSY and st & RESP:
                log.info("  RESPONSE up after %.1fs — reading", time.monotonic() - t0)
                return self._read(self.buf_max)
            time.sleep(0.05)
        log.warning("  no RESPONSE within %.0fs (status never left 0x%08x)",
                    self.image_timeout, last)
        return None

    def wait_int(self):
        """Wait for a sensor interrupt/event. On SPI, poll status for a change."""
        self._wait(RESP | READY, 120.0, "wait_int")
        return b""

    def send_init(self):
        """Minimal pre-TLS sanity init: confirm the command layer answers.

        No blob is uploaded here on purpose. python-validity's send_init sends
        `init_hardcoded` (cmd 0x06) at this point, but the trip-5 Windows trace
        shows this driver sends its config blob INSIDE the secure channel, after
        the handshake -- see synaspi.configblob, run as bring-up stage 4b.
        (The blob that must never be sent is `reset_blob`, which wipes the
        pairing; it is a different, 12 KB blob.)"""
        from validitysensor.util import assert_status
        assert_status(self.cmd(bytes.fromhex("01")))   # RomInfo.get()
        # 0x19 sometimes returns an error prefix on cold boot; probe, don't assert
        self.cmd(bytes.fromhex("19"))

    def usb_dev(self):
        return self.dev

    def _trace(self, s):
        if self.trace_enabled:
            log.debug(s)
