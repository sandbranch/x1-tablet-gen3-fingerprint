"""The always-on half of the driver: hold a session, watch for a finger, decide.

This is deliberately transport-agnostic. The matching loop takes a
`CaptureSource`, so the same code runs against the sensor or against recorded
blobs -- which is how the loop gets tested without asking anyone to press a
finger 200 times. The D-Bus surface that PAM eventually talks to is a separate,
thin adapter (`fprintd_adapter.py`); nothing in here knows about D-Bus.

Three facts from the hardware shape the design:

  * bring-up costs ~12 s (8 s power-low + handshake), a capture costs ~1.1 s.
    So the session is opened once and held, and `verify()` is a loop of cheap
    captures -- never a loop of bring-ups.
  * a suspend/resume kills the TLS session and nothing else: the gpio holder,
    the rail and spidev all survive, and a fresh bring-up works with no reboot.
    So session death is recoverable in-place, and `_ensure_open()` treats it as
    routine rather than fatal.
  * a probe must be baselined against a *no-finger reference from its own
    session*, because the baseline carries a per-session sensor offset. The
    baseline is therefore captured at open() -- with nothing touching the
    sensor -- and re-captured on every reopen.
"""
import logging
import os
import time

from synaspi import quality
from synaspi.template import Finger
from synaspi.session import Session, SessionError

log = logging.getLogger(__name__)


class SourceExhausted(Exception):
    """A replay source ran out of frames. Distinct from a dead session: there
    is nothing to re-open, so the loop must stop rather than bring up the
    sensor three times over."""


class CaptureSource:
    """Where frames come from. `open()` must leave `baseline` populated."""

    baseline = None

    def open(self):
        raise NotImplementedError

    def capture(self):
        """One raw capture blob. Raise SessionError if the session has died."""
        raise NotImplementedError

    def close(self):
        pass


class SensorSource(CaptureSource):
    """Live sensor. Assumes nothing is touching it while open() runs.

    Every frame is a raw image of a real finger, so nothing is left on disk:
    `Session.capture()` has to write a file, but this hands it a 0600 path in a
    private directory and unlinks it as soon as the bytes are in hand. The
    enrolment tool keeps its captures deliberately; the daemon must not.
    """

    #: tmpfs, so frames never reach persistent storage
    SPOOL = "/run/synaspi"

    def __init__(self, trace=False, settle=1.0):
        self.trace = trace
        self.settle = settle
        self.session = None
        self._spool = None

    def open(self):
        os.makedirs(self.SPOOL, mode=0o700, exist_ok=True)
        os.chmod(self.SPOOL, 0o700)
        self._spool = os.path.join(self.SPOOL, "frame.bin")
        self.session = Session(trace=self.trace).open()
        time.sleep(self.settle)          # let the rail settle before the reference
        self.baseline = self.capture()
        return self

    def capture(self):
        try:
            return self.session.capture(out=self._spool)
        finally:
            if self._spool and os.path.exists(self._spool):
                os.unlink(self._spool)

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None


class ReplaySource(CaptureSource):
    """Recorded blobs, replayed in order then repeated. For tests and tuning."""

    def __init__(self, baseline_path, paths, loop=True):
        self.baseline_path = baseline_path
        self.paths = list(paths)
        self.loop = loop
        self._i = 0

    def open(self):
        with open(self.baseline_path, "rb") as f:
            self.baseline = f.read()
        return self

    def capture(self):
        if self._i >= len(self.paths):
            if not self.loop:
                raise SourceExhausted("replay exhausted after %d frames"
                                      % len(self.paths))
            self._i = 0
        p = self.paths[self._i]
        self._i += 1
        with open(p, "rb") as f:
            return f.read()


class Attempt:
    """One scored frame."""

    def __init__(self, present, presence, score=None):
        self.present = present
        self.presence = presence
        self.score = score

    def __repr__(self):
        return ("<no finger %.1f>" % self.presence if not self.present
                else "<press %.1f -> %+.4f>" % (self.presence, self.score))


class VerifyResult:
    def __init__(self, accepted, best, attempts, elapsed, reason=""):
        self.accepted = accepted
        self.best = best
        self.attempts = attempts
        self.elapsed = elapsed
        self.reason = reason

    @property
    def presses(self):
        return [a for a in self.attempts if a.present]

    def __repr__(self):
        return ("<VerifyResult %s best %s presses %d/%d in %.1fs%s>"
                % ("ACCEPT" if self.accepted else "reject",
                   "%+.4f" % self.best if self.best is not None else "n/a",
                   len(self.presses), len(self.attempts), self.elapsed,
                   " (%s)" % self.reason if self.reason else ""))


class Daemon:
    """Holds one open session and answers verify requests against one finger."""

    #: a reopen costs ~12 s; refuse to thrash if the sensor is genuinely gone
    MAX_REOPENS = 3

    def __init__(self, finger: Finger, source: CaptureSource = None,
                 min_presence=quality.MIN_PRESENCE, trace=False):
        self.finger = finger
        self.source = source or SensorSource(trace=trace)
        self.min_presence = min_presence
        self._reopens = 0

    # ---- lifecycle ----
    def start(self):
        self.source.open()
        log.info("daemon ready: %r", self.finger)
        return self

    def stop(self):
        self.source.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def _ensure_open(self):
        """Re-establish the session after a resume (or any other death)."""
        if self._reopens >= self.MAX_REOPENS:
            raise SessionError("session keeps dying (%d reopens)" % self._reopens)
        self._reopens += 1
        log.warning("session died -- re-running bring-up (%d/%d)",
                    self._reopens, self.MAX_REOPENS)
        try:
            self.source.close()
        except Exception as e:
            log.debug("close during reopen raised (%r)", e)
        self.source.open()

    # ---- one frame ----
    def probe(self, reopen=True):
        """One capture, scored only if something is actually on the sensor.

        The polling half of the driver (`wake.TouchToWake`) needs a frame at a
        time rather than a loop it cannot interrupt, and it runs for the life of
        the service -- so a session that dies at 03:00 has to heal here, exactly
        as it does inside `verify()`.
        """
        try:
            blob = self.source.capture()
        except SessionError:
            if not reopen:
                raise
            self._ensure_open()
            return self.probe(reopen=False)
        self._reopens = 0                    # a good frame clears the budget
        p = quality.presence(blob, self.source.baseline)
        if p < self.min_presence:
            return Attempt(False, p)
        return Attempt(True, p, self.finger.score(blob, self.source.baseline))

    # ---- the loop ----
    def verify(self, timeout=10.0, on_attempt=None, stop=None) -> VerifyResult:
        """Watch for a finger until it matches or `timeout` expires.

        Rejection is not final here: a press that scores below the threshold
        just costs another ~0.32 s frame. That is the whole reason this is a
        loop -- worn ridges give a high false-reject rate per press, and the
        metric that matters is time-to-unlock, not first-press accuracy.

        `stop` is a callable polled each pass so a caller can cut the loop
        short. PAM does exactly this: `pam_fprintd` gives up at its own
        `timeout=10` and calls VerifyStop, and without this the loop would keep
        driving the sensor for its full timeout afterwards.
        """
        t0 = time.time()
        attempts, best = [], None
        while time.time() - t0 < timeout:
            if stop is not None and stop():
                return VerifyResult(False, best, attempts, time.time() - t0,
                                    reason="stopped by caller")
            try:
                blob = self.source.capture()
            except SourceExhausted as e:
                return VerifyResult(False, best, attempts, time.time() - t0,
                                    reason=str(e))
            except SessionError as e:
                log.debug("capture raised: %s", e)
                try:
                    self._ensure_open()
                    continue
                except SessionError as fatal:
                    return VerifyResult(False, best, attempts, time.time() - t0,
                                        reason=str(fatal))
            self._reopens = 0            # a good frame clears the budget
            p = quality.presence(blob, self.source.baseline)
            if p < self.min_presence:
                a = Attempt(False, p)
                attempts.append(a)
                if on_attempt:
                    on_attempt(a)
                continue
            s = self.finger.score(blob, self.source.baseline)
            best = s if best is None else max(best, s)
            a = Attempt(True, p, s)
            attempts.append(a)
            if on_attempt:
                on_attempt(a)
            if self.finger.accepts(s):
                self._reopens = 0
                return VerifyResult(True, best, attempts, time.time() - t0)
        return VerifyResult(False, best, attempts, time.time() - t0,
                            reason="timeout")


# ---------------------------------------------------------------------------
# CLI -- exercise the loop without any of the D-Bus plumbing
# ---------------------------------------------------------------------------
def _cmd_build(args):
    """Turn a captured enrolment set into a stored template."""
    import glob, re
    d = os.path.abspath(args.dir)
    files = sorted(glob.glob(os.path.join(d, "%s-[0-9]*.bin" % args.prefix)),
                   key=lambda p: int(re.search(r'-(\d+)\.bin$', p).group(1)))
    files = [f for f in files if not f.endswith("-baseline.bin")]
    if not files:
        print("no %s-*.bin in %s" % (args.prefix, d))
        return 2
    with open(os.path.join(d, "%s-baseline.bin" % args.prefix), "rb") as f:
        baseline = f.read()
    blobs = [open(p, "rb").read() for p in files]
    fng = Finger.from_captures(blobs, baseline, name=args.name,
                               alpha=args.alpha, threshold=args.threshold,
                               finger=args.finger)
    lo, mean = fng.self_consistency()
    path = fng.save(args.store)
    print("built %r from %d views" % (fng, len(files)))
    print("  self-consistency (leave-one-out): floor %+.4f  mean %+.4f" % (lo, mean))
    print("  -> %s" % path)
    return 0


def _cmd_verify(args):
    fng = Finger.load(args.name, args.store)
    if args.replay:
        import glob
        src = ReplaySource(os.path.join(args.replay, "%s-baseline.bin" % args.replay_prefix),
                           sorted(glob.glob(os.path.join(
                               args.replay, "%s-[0-9]*.bin" % args.replay_prefix))),
                           loop=False)
    else:
        src = SensorSource(trace=args.trace)
    def show(a):
        print("  %r" % a, flush=True)
    with Daemon(fng, src) as d:
        if not args.replay:
            print("ready -- press your finger (timeout %.0fs)" % args.timeout)
        r = d.verify(timeout=args.timeout, on_attempt=show)
    print(r)
    return 0 if r.accepted else 1


def main(argv=None):
    import argparse
    from synaspi.template import DEFAULT_ALPHA, DEFAULT_STORE, DEFAULT_THRESHOLD
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--verbose", "-v", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build a template from captured views")
    b.add_argument("--dir", default="../captures")
    b.add_argument("--prefix", default="nat")
    b.add_argument("--name", default="finger")
    b.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    b.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    b.add_argument("--finger", default="right-thumb",
                   help="which finger this is, in fprintd's vocabulary "
                        "(right-thumb, left-index-finger, ...)")
    b.set_defaults(func=_cmd_build)

    v = sub.add_parser("verify", help="run the match loop")
    v.add_argument("--name", default="finger")
    v.add_argument("--timeout", type=float, default=10.0)
    v.add_argument("--trace", action="store_true")
    v.add_argument("--replay", default=None,
                   help="score recorded blobs from this directory instead of "
                        "the sensor")
    v.add_argument("--replay-prefix", default="natver")
    v.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    import sys
    sys.exit(main())
