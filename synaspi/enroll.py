"""Interactive enrolment: capture N natural finger presses, one per prompt.

Why this exists: the first 20-view enrolment set was captured with the finger
deliberately SHIFTED between views, so it covered placements the user never
actually uses — every rejected verify attempt then sat pinned at the edge of the
matcher's search window. This tool enrols from *natural* presses instead, and
grades each view as it is taken so a weak or duplicated press is retaken rather
than silently poisoning the template.

Run it yourself in a terminal so you can see the prompts:

    cd re/synafp/driver
    sudo PYTHONPATH=$PWD python3 -m synaspi.enroll --count 15

Follow the cadence: press when it says PRESS, hold still until it says LIFT,
lift fully between views. It runs on ONE open session, so each view costs ~1 s.
"""
import argparse
import logging
import os
import sys
import time

from synaspi.quality import MIN_CHANGE, MIN_PRESENCE, change, presence
from synaspi.session import Session, SessionError

log = logging.getLogger("synaspi.enroll")



def bar(v, full=60.0, width=28):
    n = min(width, int(width * v / full))
    return "[" + "#" * n + "." * (width - n) + "]"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=15,
                    help="good views to collect (default 15)")
    ap.add_argument("--outdir", default=None,
                    help="where to write (default ../captures next to the driver)")
    ap.add_argument("--prefix", default="nat",
                    help="filename prefix, e.g. nat-1.bin (default 'nat')")
    ap.add_argument("--press-secs", type=float, default=1.5,
                    help="seconds to settle after PRESS before capturing")
    ap.add_argument("--lift-secs", type=float, default=1.5,
                    help="seconds to stay lifted between views")
    ap.add_argument("--min-presence", type=float, default=MIN_PRESENCE,
                    help="reject a view below this mean |diff| (default %.0f)"
                         % MIN_PRESENCE)
    ap.add_argument("--min-change", type=float, default=MIN_CHANGE,
                    help="reject a view this close to the previous one, i.e. the "
                         "finger never lifted (default %.1f, noise floor 1.4)"
                         % MIN_CHANGE)
    ap.add_argument("--max-retries", type=int, default=30,
                    help="give up after this many rejected presses")
    ap.add_argument("--trace", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", "captures")
    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)

    def out(name):
        return os.path.join(outdir, name)

    print("bringing the sensor up (~12 s) — keep your finger OFF the sensor")
    sess = Session(trace=args.trace)
    try:
        sess.open()
    except SessionError as e:
        print("bring-up FAILED: %s" % e)
        return 2

    try:
        print("capturing the no-finger baseline — finger still OFF")
        time.sleep(1.0)
        baseline = sess.capture(out=out("%s-baseline.bin" % args.prefix))
        print("  baseline OK (%d B) -> %s-baseline.bin" % (len(baseline), args.prefix))

        good, tries, prev = 0, 0, None
        while good < args.count and tries < args.max_retries:
            print("\n  ... LIFT / stay off ...", flush=True)
            time.sleep(args.lift_secs)
            print("  >>> PRESS  (view %d of %d) — press as you normally would, hold still"
                  % (good + 1, args.count), flush=True)
            time.sleep(args.press_secs)
            tries += 1
            tmp = out(".%s-pending.bin" % args.prefix)
            try:
                blob = sess.capture(out=tmp)
            except SessionError as e:
                print("  capture failed: %s" % e)
                break
            p = presence(blob, baseline)
            if p < args.min_presence:
                print("  %s %5.1f  REJECTED — no finger detected, try again"
                      % (bar(p), p))
                continue
            if prev is not None:
                c = change(blob, prev)
                if c < args.min_change:
                    print("  %s %5.1f  REJECTED — identical to the last view; "
                          "lift fully between presses" % (bar(p), p))
                    continue
            good += 1
            path = out("%s-%d.bin" % (args.prefix, good))
            os.replace(tmp, path)
            prev = blob
            print("  %s %5.1f  view %d saved -> %s"
                  % (bar(p), p, good, os.path.basename(path)))

        if os.path.exists(out(".%s-pending.bin" % args.prefix)):
            os.unlink(out(".%s-pending.bin" % args.prefix))
        print("\ncollected %d/%d views in %d presses -> %s"
              % (good, args.count, tries, outdir))
        if good < args.count:
            print("stopped early — rerun to top up, or lower --min-presence")
        return 0 if good == args.count else 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    finally:
        sess.close()
        print("session closed, sensor rail released")


if __name__ == "__main__":
    sys.exit(main())
