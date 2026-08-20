"""Enrolled fingers on disk: build, save, load, score.

A template is just the preprocessed enrolment views plus the metadata needed to
score a probe the same way it was built -- the enhancement blend, the decision
threshold, and the number of views. Storing the blend with the template matters:
the right value flipped once already (alpha 0.2 helped a shifted template and
*hurt* a natural one by lifting the worst impostor from +0.340 to +0.442), so a
template scored with the wrong blend silently changes its own FAR.

The stored views are preprocessed 144x40 float images, not raw captures. They
are still biometric data derived from a real finger: treat the store exactly
like `captures/` -- never commit it, never copy it off the machine.
"""
import json
import os
import time

from synaspi import matcher as M
from synaspi import reconstruct as R

#: where the daemon looks unless told otherwise. Root-owned; 0700.
DEFAULT_STORE = "/var/lib/synaspi/templates"

#: plain contrast-normalisation. Measured better than any Gabor blend against a
#: natural template -- see the note in the module docstring.
DEFAULT_ALPHA = 0.0
#: conservative default: 0/21 impostor views reached it, genuine floor was +0.43.
#: The daemon re-captures roughly once a second, so per-attempt FAR compounds
#: over a login; buy the safety with retries rather than with a lower bar.
DEFAULT_THRESHOLD = 0.40


def blend(alpha):
    """Preprocessing function for a given enhancement blend (0 = none, 1 = full)."""
    if alpha <= 0:
        return M.preprocess
    from synaspi import enhance as E
    if alpha >= 1:
        return E.enhance
    def f(img):
        a, b = M.preprocess(img), E.enhance(img)
        return [[(1 - alpha) * x + alpha * y for x, y in zip(ra, rb)]
                for ra, rb in zip(a, b)]
    return f


class Finger:
    """One enrolled finger: N preprocessed views + how to score against them."""

    #: fprintd's fixed vocabulary -- clients and PAM only understand these
    FINGERS = ("left-thumb", "left-index-finger", "left-middle-finger",
               "left-ring-finger", "left-little-finger", "right-thumb",
               "right-index-finger", "right-middle-finger", "right-ring-finger",
               "right-little-finger")

    def __init__(self, name="finger", views=None, alpha=DEFAULT_ALPHA,
                 threshold=DEFAULT_THRESHOLD, created=None, finger="right-thumb"):
        if finger not in self.FINGERS:
            raise ValueError("not an fprintd finger name: %r" % finger)
        self.finger = finger
        self.name = name
        self.views = views or []
        self.alpha = alpha
        self.threshold = threshold
        self.created = created or time.strftime("%Y-%m-%dT%H:%M:%S")

    # ---- construction ----
    @classmethod
    def from_captures(cls, blobs, baseline, name="finger", alpha=DEFAULT_ALPHA,
                      threshold=DEFAULT_THRESHOLD, finger="right-thumb"):
        """Build from raw enrolment captures and the session's no-finger baseline."""
        prep = blend(alpha)
        views = [prep(R.reconstruct(b, baseline)) for b in blobs]
        return cls(name=name, views=views, alpha=alpha, threshold=threshold,
                   finger=finger)

    # ---- use ----
    def score(self, blob, baseline):
        """Best match of a raw capture against any enrolled view, in [-1, 1]."""
        probe = blend(self.alpha)(R.reconstruct(blob, baseline))
        return max(M.best_match_fft(v, probe)[0] for v in self.views)

    def accepts(self, score):
        return score >= self.threshold

    def self_consistency(self):
        """Leave-one-out floor and mean over the enrolled views.

        A weak template shows up here before it ever costs a false rejection:
        the natural enrolment reads ~+0.60 floor / +0.78 mean, the earlier
        deliberately-shifted one read +0.33 / +0.64.
        """
        loo = [max(M.best_match_fft(self.views[j], self.views[k])[0]
                   for j in range(len(self.views)) if j != k)
               for k in range(len(self.views))]
        return min(loo), sum(loo) / len(loo)

    # ---- storage ----
    def save(self, store=DEFAULT_STORE):
        os.makedirs(store, exist_ok=True)
        os.chmod(store, 0o700)
        path = os.path.join(store, "%s.json" % self.name)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"name": self.name, "finger": self.finger, "alpha": self.alpha,
                       "threshold": self.threshold, "created": self.created,
                       "views": [[[round(v, 4) for v in row] for row in view]
                                 for view in self.views]}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, name="finger", store=DEFAULT_STORE):
        with open(os.path.join(store, "%s.json" % name)) as f:
            d = json.load(f)
        return cls(name=d["name"], views=d["views"], alpha=d["alpha"],
                   threshold=d["threshold"], created=d.get("created"),
                   finger=d.get("finger", "right-thumb"))

    @staticmethod
    def list(store=DEFAULT_STORE):
        if not os.path.isdir(store):
            return []
        return sorted(f[:-5] for f in os.listdir(store) if f.endswith(".json"))

    def __repr__(self):
        return ("<Finger %s (%s): %d views, alpha %.1f, thr %.3f, %s>"
                % (self.name, self.finger, len(self.views), self.alpha,
                   self.threshold, self.created))
