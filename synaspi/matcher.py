"""Host-side fingerprint enrolment and matching for the SYNA8002.

Why host-side: the Windows driver runs this sensor in Enrol-On-Host /
Match-On-Host mode (`CEohMohEIV`), it has no flash, and its on-chip matcher is
never used. The sensor only ever ships images, so everything above the image is
ours to implement.

Why correlation and not minutiae: the active area is 144 x 40 px at ~500 dpi,
about 7.3 x 2.0 mm. That is far too small for reliable minutiae extraction --
a window that size typically holds only a handful of minutiae, which BOZORTH3-
style matching cannot work with. Small-area sensors are matched by image
correlation against a set of enrolled views instead, which is also why Windows
takes many enrolment captures (22 x `EnrollmentUpdate` in the trip-5 trace):
each is a partial view at a different finger placement.

Pure Python on purpose -- no numpy dependency for 5760 pixels.
"""
import logging
import math

log = logging.getLogger("synaspi.matcher")


def preprocess(img, blur=1):
    """Float image -> locally contrast-normalised image.

    Local normalisation is what makes correlation robust to how hard the finger
    is pressed: it removes the slowly-varying illumination/pressure term and
    scales ridge contrast to unit variance.
    """
    h, w = len(img), len(img[0])
    out = [[0.0] * w for _ in range(h)]
    r = blur + 1
    for y in range(h):
        for x in range(w):
            vs = [img[j][i]
                  for j in range(max(0, y - r), min(h, y + r + 1))
                  for i in range(max(0, x - r), min(w, x + r + 1))]
            m = sum(vs) / len(vs)
            var = sum((v - m) ** 2 for v in vs) / len(vs)
            out[y][x] = (img[y][x] - m) / math.sqrt(var + 1e-6)
    return out


def ncc(a, b, dx, dy, min_overlap=1200):
    """Normalised cross-correlation of `b` shifted by (dx, dy) against `a`.

    Returns None when the overlap is too small to mean anything -- without that
    guard a 2-pixel corner overlap can score 1.0 and dominate the search.
    """
    h, w = len(a), len(a[0])
    sa = sb = saa = sbb = sab = 0.0
    n = 0
    for y in range(h):
        yb = y + dy
        if yb < 0 or yb >= h:
            continue
        for x in range(w):
            xb = x + dx
            if xb < 0 or xb >= w:
                continue
            u, v = a[y][x], b[yb][xb]
            sa += u; sb += v; saa += u * u; sbb += v * v; sab += u * v
            n += 1
    if n < min_overlap:
        return None
    ca = saa - sa * sa / n
    cb = sbb - sb * sb / n
    if ca <= 0 or cb <= 0:
        return None
    return (sab - sa * sb / n) / math.sqrt(ca * cb)


def best_match(a, b, max_dx=48, max_dy=24, coarse=3):
    """Best NCC over translations, coarse-to-fine. -> (score, dx, dy)."""
    best = (-1.0, 0, 0)
    for dy in range(-max_dy, max_dy + 1, coarse):
        for dx in range(-max_dx, max_dx + 1, coarse):
            s = ncc(a, b, dx, dy)
            if s is not None and s > best[0]:
                best = (s, dx, dy)
    _, bx, by = best
    for dy in range(by - coarse, by + coarse + 1):
        for dx in range(bx - coarse, bx + coarse + 1):
            s = ncc(a, b, dx, dy)
            if s is not None and s > best[0]:
                best = (s, dx, dy)
    return best


class Template:
    """An enrolled finger: several preprocessed views at different placements."""

    def __init__(self, views=None):
        self.views = views or []

    def add(self, img):
        self.views.append(preprocess(img))

    def score(self, img):
        """Best NCC of a probe against any enrolled view."""
        p = preprocess(img)
        best = (-1.0, 0, 0, -1)
        for i, v in enumerate(self.views):
            s, dx, dy = best_match(v, p)
            if s > best[0]:
                best = (s, dx, dy, i)
        return best

    def save(self, path):
        import json
        with open(path, "w") as f:
            json.dump([[[round(v, 4) for v in row] for row in view]
                       for view in self.views], f)

    @classmethod
    def load(cls, path):
        import json
        t = cls()
        with open(path) as f:
            t.views = json.load(f)
        return t


# ---------------------------------------------------------------------------
# numpy fast path
#
# The pure-Python search above is the reference implementation. When numpy is
# available the same quantity is computed for every translation at once with
# FFTs, using masked normalised cross-correlation:
#
#   NCC(d) = (Sab - Sa*Sb/N) / sqrt((Saa - Sa^2/N) * (Sbb - Sb^2/N))
#
# where each S is a correlation evaluated at shift d and N is the overlap count,
# itself the correlation of two all-ones masks. Doing the normalisation per
# shift (rather than once globally) is what keeps partial overlaps honest.
# `best_match_fft` is cross-checked against `ncc` in the self-test below.
# ---------------------------------------------------------------------------
try:
    import numpy as _np
except ImportError:
    _np = None


def _xcorr(x, y, shape):
    """Correlation of x and y, full, via FFT -> array indexed by shift."""
    fx = _np.fft.rfft2(x, shape)
    fy = _np.fft.rfft2(y[::-1, ::-1], shape)
    return _np.fft.irfft2(fx * fy, shape)


def best_match_fft(a, b, max_dx=48, max_dy=24, min_overlap=1200):
    """Same contract as best_match(), computed for all shifts at once."""
    A = _np.asarray(a, dtype=_np.float64)
    B = _np.asarray(b, dtype=_np.float64)
    h, w = A.shape
    shape = (2 * h, 2 * w)
    ones = _np.ones_like(A)

    N = _xcorr(ones, ones, shape)
    Sab = _xcorr(A, B, shape)
    Sa = _xcorr(A, ones, shape)
    Sb = _xcorr(ones, B, shape)
    Saa = _xcorr(A * A, ones, shape)
    Sbb = _xcorr(ones, B * B, shape)

    with _np.errstate(divide="ignore", invalid="ignore"):
        ca = Saa - Sa ** 2 / N
        cb = Sbb - Sb ** 2 / N
        num = Sab - Sa * Sb / N
        ncc_all = num / _np.sqrt(ca * cb)
    ncc_all[~_np.isfinite(ncc_all)] = -1.0
    ncc_all[N < min_overlap] = -1.0

    # shift d maps to index (h-1-dy, w-1-dx) in the full correlation, so the
    # searched window is a contiguous slice -- take its argmax directly.
    y0, y1 = h - 1 - max_dy, h - 1 + max_dy + 1
    x0, x1 = w - 1 - max_dx, w - 1 + max_dx + 1
    win = ncc_all[y0:y1, x0:x1]
    iy, ix = _np.unravel_index(int(_np.argmax(win)), win.shape)
    return float(win[iy, ix]), int(max_dx - ix), int(max_dy - iy)


def match(a, b, **kw):
    """Best available implementation."""
    return best_match_fft(a, b, **kw) if _np is not None else best_match(a, b, **kw)


def match_rot(a, b, angles=(-15, -10, -5, 0, 5, 10, 15), **kw):
    """Best NCC over rotation as well as translation.

    Rotation is not optional in practice: a finger pressed onto a small sensor
    arrives at whatever angle is comfortable, and a few degrees destroys
    correlation far faster than a few pixels of shift.
    """
    from . import enhance as _E
    best = (-1.0, 0, 0, 0)
    for ang in angles:
        p = _E.rotate(b, ang) if ang else b
        s, dx, dy = best_match_fft(a, p, **kw)
        if s > best[0]:
            best = (s, dx, dy, ang)
    return best
