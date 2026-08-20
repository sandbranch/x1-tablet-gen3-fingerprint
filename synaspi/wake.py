"""Touch-to-wake: light the screen, and unlock it, when a finger lands.

The problem this solves is the one the D-Bus adapter cannot: while the session
is locked and the screen is blank, *nobody is asking us anything*. gnome-shell
only starts a fingerprint verify once its unlock dialog is up, and the unlock
dialog only comes up once some input has woken the screen. So the reader sits
there, live and held open, and completely ignored -- the user has to reach for
the power button first.

So this watches the sensor itself while the session is locked, and does two
things when a finger lands:

  * taps a synthetic key, which is what actually turns the screen on. There is
    no "wake the display" call to make: the compositor's idle watch wakes on
    input events, so the honest way to say "a human is here" is to be an input
    device. Hence the uinput keyboard below.
  * if the press also *matches*, unlocks the session through logind, so one
    press does the whole job rather than lighting the screen for a second press.

⚠️ The unlock path deliberately steps outside PAM. It is the same matcher, the
same template and the same threshold that `pam_fprintd` already trusts on this
machine -- but PAM's own policy (attempt counting, `pam_faillock`) is not in the
loop, so this is a decision to make on purpose. `--no-wake-unlock` keeps the
screen-waking half and leaves the unlock to gnome-shell's own verify, which
takes the sensor from us the moment its dialog appears.

Two things are deliberately conservative:

  * it only polls while the session is *locked*. An unlocked session is being
    used, and capturing fingerprints of whoever touches the reader all day --
    to throw them away -- is not something a login helper should do.
  * it never fights a client for the sensor. `probe_when_free()` returns None
    while anything holds a claim, and the watcher just waits.
"""
import fcntl
import logging
import os
import struct
import threading
import time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# the synthetic keyboard
# ---------------------------------------------------------------------------
# ioctl numbers from linux/uinput.h -- _IOW('U', n, int) / _IO('U', n). Written
# out rather than recomputed: they are ABI, and the arithmetic is more
# error-prone than the constants.
_UI_SET_EVBIT = 0x40045564
_UI_SET_KEYBIT = 0x40045565
_UI_DEV_CREATE = 0x5501
_UI_DEV_DESTROY = 0x5502

_EV_SYN, _EV_KEY = 0x00, 0x01
_SYN_REPORT = 0
_KEY_LEFTCTRL = 29
_KEY_WAKEUP = 143

#: struct input_event: struct timeval (two longs) + type + code + value
_EVENT = struct.Struct("llHHi")


class WakeKey:
    """A one-key virtual keyboard whose only job is to say "a human is here".

    It reports KEY_LEFTCTRL, not KEY_WAKEUP alone: a device carrying only
    KEY_WAKEUP is classified as a power switch rather than a keyboard, and what
    we need is for the compositor's *idle* watch to see ordinary input. Ctrl is
    the safest key to press at a lock screen -- on its own it does nothing
    anywhere, while any letter would land in the password field.
    """

    DEVICE = "/dev/uinput"

    def __init__(self, name="synaspi touch-to-wake"):
        self.name = name
        self._fd = None

    def open(self):
        fd = os.open(self.DEVICE, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, _UI_SET_EVBIT, _EV_KEY)
            for key in (_KEY_LEFTCTRL, _KEY_WAKEUP):
                fcntl.ioctl(fd, _UI_SET_KEYBIT, key)
            # legacy setup: struct uinput_user_dev is name[80] + input_id +
            # ff_effects_max + four 64-entry abs arrays we have no use for
            os.write(fd, struct.pack("80s4HI", self.name.encode()[:79],
                                     0x06, 0x1217, 0x8002, 1, 0) + b"\0" * 1024)
            fcntl.ioctl(fd, _UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        log.info("touch-to-wake: virtual keyboard %r created", self.name)
        return self

    def tap(self):
        """Press and release. Cheap enough to fire on every detected touch."""
        if self._fd is None:
            return
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        buf = b"".join(
            _EVENT.pack(sec, usec, t, c, v) for t, c, v in
            ((_EV_KEY, _KEY_LEFTCTRL, 1), (_EV_SYN, _SYN_REPORT, 0),
             (_EV_KEY, _KEY_LEFTCTRL, 0), (_EV_SYN, _SYN_REPORT, 0)))
        try:
            os.write(self._fd, buf)
        except OSError as e:
            log.warning("touch-to-wake: key tap failed (%s)", e)

    def close(self):
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, _UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------------------
# logind
# ---------------------------------------------------------------------------
class SeatSession:
    """The graphical session on the local seat, as logind sees it.

    On its own private bus connection on purpose: dbus-python's shared
    SystemBus belongs to the GLib main loop thread, and polling it from this
    thread is exactly the sort of thing that surfaces later as an intermittent
    "Did not receive a reply" -- in the login path, which is the worst place to
    debug one.
    """

    BUS = "org.freedesktop.login1"
    MANAGER = "/org/freedesktop/login1"
    IFACE_MANAGER = "org.freedesktop.login1.Manager"
    IFACE_SESSION = "org.freedesktop.login1.Session"

    def __init__(self, cache_for=1.0):
        import dbus
        self._dbus = dbus
        self._bus = dbus.SystemBus(private=True)
        self._mgr = dbus.Interface(
            self._bus.get_object(self.BUS, self.MANAGER), self.IFACE_MANAGER)
        self._cache_for = cache_for
        self._cached = (0.0, (None, False))

    def _state(self):
        """(session id, locked) for the active graphical session."""
        age, cached = self._cached
        if time.monotonic() - age < self._cache_for:
            return cached
        state = (None, False)
        try:
            for sid, _uid, _user, seat, path in self._mgr.ListSessions():
                if not seat:
                    continue
                props = self._dbus.Interface(
                    self._bus.get_object(self.BUS, path),
                    "org.freedesktop.DBus.Properties")
                p = props.GetAll(self.IFACE_SESSION)
                if p.get("Class") != "user" or not p.get("Active"):
                    continue
                state = (str(sid), bool(p.get("LockedHint")))
                break
        except Exception as e:
            log.warning("touch-to-wake: cannot read logind state (%s)", e)
        self._cached = (time.monotonic(), state)
        return state

    def locked(self):
        return self._state()[1]

    def unlock(self):
        sid = self._state()[0]
        if sid is None:
            log.warning("touch-to-wake: no active session to unlock")
            return False
        self._mgr.UnlockSession(sid)
        self._cached = (0.0, (None, False))     # the hint is about to change
        log.info("touch-to-wake: unlocked session %s", sid)
        return True


# ---------------------------------------------------------------------------
# the watcher
# ---------------------------------------------------------------------------
class TouchToWake(threading.Thread):
    """Polls the sensor while the session is locked. Owns no hardware itself.

    `device` is the fprintd adapter's Device: the watcher borrows the sensor
    through `probe_when_free()` and gives it up the instant a real client wants
    it -- gnome-shell's unlock dialog claims within a second of the screen
    lighting up, and its verify is the better path when ours has not already
    decided.
    """

    def __init__(self, device, interval=0.4, idle_interval=2.0, unlock=True):
        super().__init__(name="touch-to-wake", daemon=True)
        self.device = device
        self.interval = interval            # between polls while locked
        self.idle_interval = idle_interval  # between checks while unlocked
        self.unlock = unlock
        self.key = WakeKey()
        self.session = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self.key.open()
            self.session = SeatSession()
        except Exception as e:
            log.error("touch-to-wake: disabled -- %s", e)
            return
        log.info("touch-to-wake: watching (poll %.1fs, unlock %s)",
                 self.interval, "on" if self.unlock else "off")
        try:
            self._loop()
        finally:
            self.key.close()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not self.session.locked():
                    self._stop.wait(self.idle_interval)
                    continue
                a = self.device.probe_when_free()
                if a is None:               # a client has the sensor, or it is
                    self._stop.wait(0.5)    # not up yet
                    continue
                if not a.present:
                    self._stop.wait(self.interval)
                    continue
                self._on_touch(a)
            except Exception:
                # a dead sensor must not take the whole service with it: the
                # D-Bus half still has to answer PAM
                log.exception("touch-to-wake: poll failed, backing off")
                self._stop.wait(5.0)

    def _on_touch(self, a):
        # Light the screen first, decide second. The tap costs microseconds and
        # the user is standing there: waiting for the score to come back before
        # showing any sign of life is what makes a reader feel broken.
        self.key.tap()
        threshold = self.device.daemon.finger.threshold
        log.info("touch-to-wake: press while locked, presence %.1f score %+.4f "
                 "(threshold %.3f)", a.presence, a.score, threshold)
        if self.unlock and a.score >= threshold:
            if self.session.unlock():
                self._stop.wait(3.0)        # let the shield come down
                return
        # No match: leave the lock screen up and stand off, so gnome-shell's own
        # verify -- which is now on screen and about to claim -- gets the sensor
        # instead of racing us for it.
        self._stop.wait(2.0)
