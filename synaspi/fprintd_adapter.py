"""D-Bus surface: speak `net.reactivated.Fprint` so PAM can use this sensor.

`libpam-fprintd` is already installed on this box and talks to exactly one bus
name, `net.reactivated.Fprint`, owned by stock `fprintd`. Implementing that
interface here means `pam_fprintd.so`, GNOME Settings and `fprintd-verify` all
work unmodified -- the same trick open-fprintd uses for python-validity.

⚠️ STATUS: structurally complete, NOT yet exercised against PAM. It has never
owned the bus name, because doing so requires stopping and masking stock
fprintd, and that is a change to a login path -- it needs a second way into the
machine open before it is switched on. See `packaging/` for the unit files and
the ordering.

Design notes:

  * verification runs on a worker thread. The GLib main loop must stay
    responsive: a verify can take seconds and `VerifyStop` has to be able to
    interrupt it.
  * the bus name is owned FIRST and the ~12 s bring-up runs behind it on a
    thread. gdm's greeter blocks on the name, so bringing up first put the
    whole 12 s between "select user" and the login box appearing. Everything
    the greeter asks for early -- GetDevices, ListEnrolledFingers, the
    properties, Claim -- is answerable from the template store alone; only the
    match loop needs the sensor, and it waits on `_ready`.
  * the session is opened **at service start and held**, not on `Claim`.
    `libpam-fprintd` ships `/usr/share/pam-configs/fprintd` with
    `timeout=10`, and bring-up alone measures ~10.8 s -- claiming on demand
    would fail every login on a stopwatch rather than on a match. The cost is
    that the sensor rail stays powered for the service's lifetime.
    With captures at ~0.32 s that budget is worth ~30 attempts.
  * fprintd's result strings are a fixed vocabulary. `verify-match` /
    `verify-no-match` are decisions; `verify-retry-scan-too-short` is the
    "nothing was on the sensor" case and does NOT end the attempt.
"""
import logging
import signal
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from synaspi.daemon import Daemon, SensorSource
from synaspi.template import DEFAULT_STORE, Finger
from synaspi.wake import TouchToWake

log = logging.getLogger(__name__)

class _FprintError(dbus.DBusException):
    """dbus-python wraps a bare DBusException as
    `org.freedesktop.DBus.Python.dbus.exceptions.DBusException`, which no
    fprintd client recognises -- every one of our errors then reads to PAM as
    "An unknown error occurred". The name has to come from the class."""
    _dbus_error_name = "net.reactivated.Fprint.Error.Internal"


class AlreadyInUse(_FprintError):
    _dbus_error_name = "net.reactivated.Fprint.Error.AlreadyInUse"


class ClaimDevice(_FprintError):
    _dbus_error_name = "net.reactivated.Fprint.Error.ClaimDevice"


class NoEnrolledPrints(_FprintError):
    _dbus_error_name = "net.reactivated.Fprint.Error.NoEnrolledPrints"


BUS_NAME = "net.reactivated.Fprint"
MANAGER_PATH = "/net/reactivated/Fprint/Manager"
DEVICE_PATH = "/net/reactivated/Fprint/Device/0"
IFACE_MANAGER = "net.reactivated.Fprint.Manager"
IFACE_DEVICE = "net.reactivated.Fprint.Device"


class Device(dbus.service.Object):
    """One sensor. fprintd's Device interface, backed by our match loop."""

    def __init__(self, bus, store=DEFAULT_STORE):
        super().__init__(bus, DEVICE_PATH)   # exported before the name is owned:
                                             # no client can see it until then
        self.bus = bus
        self.store = store
        self.claimed_by = None
        self.claimed_sender = None    # the claimant's unique bus name
        self._claim_watch = None
        self.daemon = None
        self._worker = None
        self._stop = threading.Event()
        self._ready = threading.Event()   # bring-up finished, one way or the other
        self._bringup = None
        self._bringup_error = None
        self._closing = False
        self._sensor = threading.Lock()   # one user of the hardware at a time
        self._watcher = None

    #: how long a verify waits for bring-up before giving up. Bring-up measures
    #: ~12 s; past that, waiting only turns a failure into a slower one.
    READY_TIMEOUT = 25.0

    def start(self, background=False):
        """Bring the sensor up once, for the lifetime of the service."""
        fingers = Finger.list(self.store)
        if not fingers:
            log.warning("no templates in %s -- device will report no enrolled "
                        "prints and PAM will fall through", self.store)
            self._ready.set()
            return self
        if background:
            self._bringup = threading.Thread(target=self._bring_up,
                                             name="bringup", daemon=True)
            self._bringup.start()
        else:
            self._bring_up()
        return self

    def start_watcher(self, **kw):
        """Touch-to-wake. Started before bring-up finishes: the watcher waits
        on `probe_when_free()` like any other client."""
        self._watcher = TouchToWake(self, **kw)
        self._watcher.start()
        return self._watcher

    def probe_when_free(self):
        """One scored frame, or None if the sensor is spoken for.

        A claim always wins. The watcher is a background convenience; PAM is
        the login path, and it must never wait on us.
        """
        if self.claimed_by is not None:
            self._reap_dead_claim()   # self-healing: never wait on a dead client
        if self.claimed_by is not None or (self._worker and self._worker.is_alive()):
            return None
        if not self._ready.is_set() or self.daemon is None:
            return None
        if not self._sensor.acquire(blocking=False):
            return None
        try:
            if self.claimed_by is not None:      # re-checked under the lock
                return None
            return self.daemon.probe()
        finally:
            self._sensor.release()

    def _bring_up(self):
        try:
            self.daemon = Daemon(Finger.load(Finger.list(self.store)[0],
                                             self.store), SensorSource()).start()
        except Exception as e:              # a verify must fail loudly, not hang
            self._bringup_error = e
            log.exception("bring-up failed -- device will refuse to verify")
        finally:
            self._ready.set()
        if self._closing and self.daemon is not None:
            # SIGTERM landed mid-bring-up: release the rail we just took, or the
            # next power cycle is a no-op.
            self.daemon.stop()
            self.daemon = None

    def _await_ready(self, stop=None):
        """Block until the sensor is usable. False if `stop` fired meanwhile;
        raises the reason the sensor is unusable otherwise.

        Polled rather than a plain `wait()` so VerifyStop stays instant: PAM
        calls it synchronously, and a worker parked on the full timeout would
        make every cancelled prompt hang for as long as the join allows.
        """
        if not self._ready.is_set():
            log.info("waiting for bring-up to finish")
        deadline = time.monotonic() + self.READY_TIMEOUT
        while not self._ready.wait(0.1):
            if stop is not None and stop():
                return False
            if time.monotonic() >= deadline:
                raise _FprintError("sensor bring-up did not finish in %.0fs"
                                   % self.READY_TIMEOUT)
        if self._bringup_error is not None:
            raise _FprintError("sensor bring-up failed: %s" % self._bringup_error)
        if self.daemon is None:
            raise NoEnrolledPrints()
        return True

    def _enrolled_name(self):
        """The enrolled finger, read from the store -- no sensor needed."""
        if self.daemon is not None:
            return self.daemon.finger.finger
        names = Finger.list(self.store)
        return Finger.load(names[0], self.store).finger if names else None

    def shutdown(self):
        """Release the rail. systemd sends SIGTERM; leaving the gpio held is
        the failure that makes the next power cycle a no-op."""
        self._closing = True
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self.VerifyStop()
        if self._bringup is not None and self._bringup.is_alive():
            # never abandon an in-flight bring-up: it ends holding the gpio
            self._bringup.join(timeout=self.READY_TIMEOUT)
            self._bringup = None
        if self.daemon is not None:
            self.daemon.stop()
            self.daemon = None

    # ---- properties fprintd clients read ----
    PROPS = {"name": "SYNA8002 (synaspi)",
             "num-enroll-stages": dbus.Int32(15),
             "scan-type": "press"}

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        # An unknown property must not raise KeyError: dbus-python would send it
        # as a Python error name and the client would give up on the device.
        if prop not in self.PROPS:
            log.warning("Get(%s, %s) -- unknown property", iface, prop)
            raise _FprintError("no such property: %s" % prop)
        log.info("Get(%s)", prop)
        return self.PROPS[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        log.info("GetAll(%s)", iface)
        return dict(self.PROPS)

    # ---- claim / release ----
    def _reap_dead_claim(self):
        """Drop a claim whose owner has left the bus.

        Without this the device wedges permanently, and silently: a client that
        exits without calling Release -- a crashed greeter, a dismissed unlock
        dialog, anything gnome-shell decides to tear down -- leaves `claimed_by`
        set forever. Every later Claim then fails AlreadyInUse, so PAM never
        reaches VerifyStart and the wake watcher stands off waiting for a client
        that no longer exists. The reader looks dead while the service looks
        healthy. Measured on this machine: stuck from 13:34 to 14:31 with no
        error anywhere except a Claim that quietly failed.
        """
        if self.claimed_sender is None:
            return
        try:
            alive = self.bus.name_has_owner(self.claimed_sender)
        except Exception as e:
            log.debug("name_has_owner(%s) raised (%r)", self.claimed_sender, e)
            return
        if not alive:
            log.warning("reaping claim held by %s (%s) -- owner left the bus",
                        self.claimed_by, self.claimed_sender)
            self._drop_claim()

    def _drop_claim(self):
        if self._claim_watch is not None:
            try:
                self._claim_watch.remove()
            except Exception as e:
                log.debug("removing claim watch raised (%r)", e)
            self._claim_watch = None
        self.claimed_by = None
        self.claimed_sender = None
        self.VerifyStop()

    def _on_claimant_gone(self, name, old_owner, new_owner):
        if not new_owner and name == self.claimed_sender:
            log.warning("claimant %s disconnected -- releasing", name)
            self._drop_claim()

    @dbus.service.method(IFACE_DEVICE, in_signature="s", sender_keyword="sender")
    def Claim(self, username, sender=None):
        # Instant by design: the session is already open. See the module note.
        log.info("Claim(%r) from %s", username, sender)
        self._reap_dead_claim()
        if self.claimed_by is not None:
            if sender is not None and sender == self.claimed_sender:
                return                         # re-claim by the same client
            raise AlreadyInUse()
        if self._enrolled_name() is None:      # deliberately does not wait for
            raise NoEnrolledPrints()           # bring-up: the greeter blocks on this
        self.claimed_by = username or sender
        self.claimed_sender = sender
        if sender is not None:
            # belt and braces over the reaper above: notice the moment they go,
            # rather than only when the next client happens to ask
            self._claim_watch = self.bus.add_signal_receiver(
                self._on_claimant_gone, signal_name="NameOwnerChanged",
                dbus_interface="org.freedesktop.DBus", arg0=sender)
        log.info("claimed by %s (%s)", self.claimed_by, sender)

    @dbus.service.method(IFACE_DEVICE, sender_keyword="sender")
    def Release(self, sender=None):
        # Releases the claim, NOT the sensor -- the session outlives claims.
        # Only the holder may release: gnome-shell claims from two connections
        # and the loser gets AlreadyInUse, so an unconditional Release let the
        # loser hand the sensor away from the client actually using it.
        if (sender is not None and self.claimed_sender is not None
                and sender != self.claimed_sender):
            log.info("Release from %s ignored -- claim belongs to %s",
                     sender, self.claimed_sender)
            return
        self._drop_claim()

    # ---- verify ----
    @dbus.service.method(IFACE_DEVICE, in_signature="s")
    def VerifyStart(self, finger_name):
        if self._worker and self._worker.is_alive():
            raise AlreadyInUse()
        self._stop.clear()
        # Resolve to a REAL finger before announcing it. Clients (pam_fprintd
        # included) ask for "any", and the signal is the daemon's answer to
        # "which one did you pick?" -- echoing "any" back makes pam_fprintd
        # abort with "Unexpected VerifyFingerSelected any signal".
        enrolled = self._enrolled_name()
        if enrolled is None:
            raise ClaimDevice()
        if finger_name and finger_name != "any" and finger_name != enrolled:
            raise NoEnrolledPrints()
        # Deferred, not emitted inline: a signal sent from inside the method
        # reaches the client BEFORE the method's own reply, and pam_fprintd
        # discards it as "Unexpected VerifyFingerSelected ... signal" because
        # it has not entered its verifying state yet.
        self._emit(self.VerifyFingerSelected, enrolled)
        log.info("VerifyStart(%s) -> verifying %s", finger_name, enrolled)
        self._worker = threading.Thread(target=self._verify_loop, daemon=True)
        self._worker.start()

    @dbus.service.method(IFACE_DEVICE)
    def VerifyStop(self):
        log.info("VerifyStop")
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None

    def _verify_loop(self):
        def report(a):
            # Log, but emit NOTHING until the loop has decided.
            #
            # fprintd's retry codes mean "that scan failed, re-arm", and
            # pam_fprintd acts on them: VerifyStop then VerifyStart. We were
            # emitting a retry for a press before scoring it, so the winning
            # press was announced as a failure a fraction of a second before
            # `verify-match` -- which then arrived while PAM was between calls
            # and was ignored ("An unknown error occurred", two VerifyStops in
            # the log). Our loop does its own retrying, so a single terminal
            # result is both correct and better UX: no flashing messages.
            if a.present:
                log.info("press: presence %.1f score %+.4f (threshold %.3f)",
                         a.presence, a.score, self.daemon.finger.threshold)

        try:
            if not self._await_ready(self._stop.is_set):
                return                       # cancelled before the sensor was up
            with self._sensor:               # the watcher backs off, never blocks
                r = self.daemon.verify(timeout=30.0, on_attempt=report,
                                       stop=self._stop.is_set)
            result = "verify-match" if r.accepted else "verify-no-match"
            log.info("verify -> %s (%r)", result, r)
        except Exception as e:                       # a dead sensor, not a bad finger
            log.exception("verify failed: %r", e)
            result = "verify-disconnected"
        if not self._stop.is_set():
            self._emit(self.VerifyStatus, result, True)

    def _emit(self, signal, *args):
        GLib.idle_add(lambda: (signal(*args), False)[1])

    # ---- enrolment: deliberately not wired up yet ----
    @dbus.service.method(IFACE_DEVICE, in_signature="s")
    def EnrollStart(self, finger_name):
        # Enrolment happens through `python3 -m synaspi.enroll`, which grades
        # each press and can reject one. Mapping that onto fprintd's
        # stage-counting model is a design question, not a plumbing one.
        raise _FprintError()

    @dbus.service.method(IFACE_DEVICE)
    def EnrollStop(self):
        pass

    @dbus.service.method(IFACE_DEVICE, in_signature="s", out_signature="as")
    def ListEnrolledFingers(self, username):
        # PAM reads this before it prompts: an empty list means "no prints", and
        # pam_fprintd falls straight through to the password.
        fingers = [Finger.load(n, self.store).finger for n in Finger.list(self.store)]
        log.info("ListEnrolledFingers(%r) -> %s", username, fingers)
        return fingers

    @dbus.service.method(IFACE_DEVICE, in_signature="s")
    def DeleteEnrolledFingers(self, username):
        raise _FprintError()

    # ---- signals ----
    @dbus.service.signal(IFACE_DEVICE, signature="sb")
    def VerifyStatus(self, result, done):
        pass

    @dbus.service.signal(IFACE_DEVICE, signature="s")
    def VerifyFingerSelected(self, finger):
        pass

    @dbus.service.signal(IFACE_DEVICE, signature="sb")
    def EnrollStatus(self, result, done):
        pass


class Manager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, MANAGER_PATH)

    @dbus.service.method(IFACE_MANAGER, out_signature="ao", sender_keyword="sender")
    def GetDevices(self, sender=None):
        log.info("GetDevices (from %s)", sender)
        return [dbus.ObjectPath(DEVICE_PATH)]

    @dbus.service.method(IFACE_MANAGER, out_signature="o", sender_keyword="sender")
    def GetDefaultDevice(self, sender=None):
        log.info("GetDefaultDevice (from %s)", sender)
        return dbus.ObjectPath(DEVICE_PATH)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--no-sensor", action="store_true",
                    help="own the bus name without touching the hardware -- "
                         "for exercising the D-Bus surface alone")
    ap.add_argument("--session-bus", action="store_true",
                    help="own the name on the session bus instead of the system "
                         "bus -- the safe way to try this without touching PAM")
    ap.add_argument("--no-wake", action="store_true",
                    help="do not watch the sensor while the session is locked "
                         "(disables touch-to-wake entirely)")
    ap.add_argument("--no-wake-unlock", action="store_true",
                    help="touch lights the screen but never unlocks -- leaves "
                         "the decision to gnome-shell's own PAM verify")
    ap.add_argument("--wake-interval", type=float, default=0.4,
                    help="seconds between sensor polls while locked")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus() if args.session_bus else dbus.SystemBus()

    # Order matters, the other way round from what it looks like. Anything
    # asking for this name -- gdm's greeter above all -- is BLOCKED by dbus
    # until the name exists, so taking it last spent the whole bring-up with
    # the login screen frozen on "select user". Take the name first and bring
    # the sensor up behind it; a verify that arrives early waits on `_ready`.
    dev = Device(bus, args.store)
    name = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
    mgr = Manager(bus)
    if not args.no_sensor:
        dev.start(background=True)
        if not args.no_wake:
            dev.start_watcher(interval=args.wake_interval,
                              unlock=not args.no_wake_unlock)
    log.info("owning %s on the %s bus (%d template(s) in %s)", BUS_NAME,
             "session" if args.session_bus else "system",
             len(Finger.list(args.store)), args.store)
    loop = GLib.MainLoop()
    try:                                   # pygobject >= 3.52
        from gi.repository import GLibUnix
        add_signal = GLibUnix.signal_add
    except ImportError:
        add_signal = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGINT):
        add_signal(GLib.PRIORITY_HIGH, sig,
                   lambda: (dev.shutdown(), loop.quit(), False)[2])
    try:
        loop.run()
    finally:
        dev.shutdown()


if __name__ == "__main__":
    main()
