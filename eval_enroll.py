#!/usr/bin/env python3
"""Score a template set against probe and impostor sets, offline.

Generalises eval_full.py, which hardcoded the enroll-*/verify-*/imp-* prefixes
and a single baseline. Every set here carries its OWN no-finger baseline,
because the baseline is a per-session sensor offset: subtracting yesterday's
baseline from today's capture leaves a session-dependent bias in the image.

  ./eval_enroll.py --template nat --probe natver --impostor imp --alpha 0.2
"""
import argparse, glob, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synaspi import reconstruct as R, matcher as M, enhance as E

C = os.environ.get("SYNASPI_CAPTURES") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "captures")


def num(p):
    m = re.search(r'-(\d+)\.bin$', p)
    return int(m.group(1)) if m else 0


def load_set(prefix, baseline=None, pattern=None):
    """All files matching <prefix>-*.bin, reconstructed against a baseline.

    `pattern` overrides the default `<prefix>-[0-9]*.bin` -- impostor captures
    are named by finger (imp-index-1.bin), not by index alone.
    """
    base_path = baseline or os.path.join(C, "%s-baseline.bin" % prefix)
    base = open(base_path, "rb").read() if os.path.exists(base_path) else None
    pat = pattern or "%s-[0-9]*.bin" % prefix
    files = [f for f in sorted(glob.glob(os.path.join(C, pat)), key=num)
             if not f.endswith("-baseline.bin")]   # the baseline is the no-finger
                                                   # reference, never a probe
    out = []
    for f in files:
        try:
            out.append((os.path.basename(f),
                        R.reconstruct(open(f, "rb").read(), base)))
        except ValueError as e:
            print("  skipping %s: %s" % (os.path.basename(f), e))
    return out, os.path.basename(base_path) if base else "(none)"


def blend(alpha):
    """alpha=0 -> plain contrast norm; alpha=1 -> full Gabor enhancement."""
    if alpha <= 0:
        return M.preprocess
    if alpha >= 1:
        return E.enhance
    def f(img):
        a, b = M.preprocess(img), E.enhance(img)
        return [[(1 - alpha) * x + alpha * y for x, y in zip(ra, rb)]
                for ra, rb in zip(a, b)]
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="nat")
    ap.add_argument("--probe", default="natver")
    ap.add_argument("--template-baseline", default=None,
                    help="baseline .bin for the template set (default <prefix>-baseline.bin)")
    ap.add_argument("--probe-baseline", default=None,
                    help="baseline .bin for the probe set (default <prefix>-baseline.bin). "
                         "Sets captured before the tool existed have no baseline of "
                         "their own -- pass the one from their own session.")
    ap.add_argument("--impostor", action="append", default=None,
                    help="impostor prefix + its baseline, as PREFIX:BASELINE.bin "
                         "(repeatable; default imp:enroll-baseline.bin)")
    ap.add_argument("--alpha", type=float, action="append", default=None,
                    help="enhancement blend to test (repeatable, default 0 and 0.2)")
    args = ap.parse_args()
    alphas = args.alpha or [0.0, 0.2]
    imps = args.impostor or ["imp:enroll-baseline.bin"]

    def _bl(v):
        return os.path.join(C, v) if v else None
    T, tb = load_set(args.template, _bl(args.template_baseline))
    P, pb = load_set(args.probe, _bl(args.probe_baseline))
    I = []
    for spec in imps:
        pre, _, bl = spec.partition(":")
        s, b = load_set(pre, os.path.join(C, bl) if bl else None,
                        pattern="%s-*.bin" % pre)
        I += s
        print("impostor set %-8s %2d views (baseline %s)" % (pre, len(s), b))
    print("template %-10s %2d views (baseline %s)" % (args.template, len(T), tb))
    print("probe    %-10s %2d views (baseline %s)" % (args.probe, len(P), pb))
    if not T or not P:
        return 2

    for alpha in alphas:
        t0 = time.time()
        prep = blend(alpha)
        tpl = [prep(i) for _, i in T]

        def score(img):
            p = prep(img)
            return max(M.best_match_fft(t, p)[0] for t in tpl)

        # leave-one-out over the template: the self-consistency floor
        loo = []
        for k in range(len(tpl)):
            loo.append(max(M.best_match_fft(tpl[j], tpl[k])[0]
                           for j in range(len(tpl)) if j != k))
        gen = [(n, score(i)) for n, i in P]
        imp = [(n, score(i)) for n, i in I]
        gfloor = min(v for _, v in gen)
        imax = max(v for _, v in imp) if imp else float("nan")
        print("\n=== alpha %.1f  (%.0fs) ===" % (alpha, time.time() - t0))
        print("  template self-consistency (leave-one-out): min %+.4f  mean %+.4f"
              % (min(loo), sum(loo) / len(loo)))
        print("  genuine  : %s" % " ".join("%+.3f" % v for _, v in sorted(gen, key=lambda x: -x[1])))
        print("  impostor : %s" % " ".join("%+.3f" % v for _, v in sorted(imp, key=lambda x: -x[1])))
        print("  worst impostor: %s" % max(imp, key=lambda x: x[1])[0] if imp else "")
        print("  genuine floor %+.4f   impostor max %+.4f   MARGIN %+.4f"
              % (gfloor, imax, gfloor - imax))
        thr = 0.5 * (gfloor + imax)
        for t in sorted({thr, 0.25, 0.30, 0.35}):
            acc = sum(1 for _, v in gen if v >= t)
            fa = sum(1 for _, v in imp if v >= t)
            print("    thr %.3f -> FRR %3.0f%% (%d/%d accepted)   FAR %3.0f%% (%d/%d impostors)"
                  % (t, 100 * (len(gen) - acc) / len(gen), acc, len(gen),
                     100 * fa / len(imp) if imp else 0, fa, len(imp)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
