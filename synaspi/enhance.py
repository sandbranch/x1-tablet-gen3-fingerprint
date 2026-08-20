"""Gabor ridge enhancement for worn fingerprints.

Local contrast normalisation (what `matcher.preprocess` does on its own)
amplifies whatever is present, noise included. On worn ridges -- the author's are
badly worn from manual work, ~80% FRR even on his phone -- the true ridge signal
is weak and that is exactly the wrong behaviour.

Gabor enhancement instead estimates the local ridge ORIENTATION and then filters
along it, so structure that runs like a ridge is reinforced and everything else
is suppressed. Standard fingerprint-processing practice (Hong/Wan/Jain), adapted
to a 144x40 window.

Pipeline: orientation field (structure tensor, angle-doubled smoothing) ->
oriented Gabor bank -> per-pixel selection of the response whose kernel best
matches the local orientation.
"""
import numpy as np
from scipy import ndimage

RIDGE_PERIOD = 9.0      # px, measured from our captures (~500 dpi)
N_ORIENT = 12


def orientation_field(img, block=5, smooth=3):
    """Per-pixel ridge orientation in [0, pi)."""
    gx = ndimage.sobel(img, axis=1, mode="nearest")
    gy = ndimage.sobel(img, axis=0, mode="nearest")
    gxx = ndimage.uniform_filter(gx * gx, block)
    gyy = ndimage.uniform_filter(gy * gy, block)
    gxy = ndimage.uniform_filter(gx * gy, block)
    # Angles are mod pi, so average the DOUBLED angle to avoid the wrap at 0/pi
    s2 = ndimage.gaussian_filter(2 * gxy, smooth)
    c2 = ndimage.gaussian_filter(gxx - gyy, smooth)
    # gradient direction -> ridge direction is perpendicular
    return (0.5 * np.arctan2(s2, c2) + np.pi / 2) % np.pi


def gabor_kernel(theta, period=RIDGE_PERIOD, sigma=None, size=None):
    sigma = sigma or period * 0.6
    size = size or int(period * 2) | 1
    r = size // 2
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    # rotate so the kernel oscillates ACROSS the ridge, constant along it
    xr = x * np.cos(theta) + y * np.sin(theta)
    yr = -x * np.sin(theta) + y * np.cos(theta)
    g = np.exp(-(xr ** 2 + yr ** 2) / (2 * sigma ** 2))
    k = g * np.cos(2 * np.pi * xr / period)
    return k - k.mean()


def enhance(img):
    """2-D float array -> Gabor-enhanced, zero-mean unit-variance array."""
    a = np.asarray(img, dtype=np.float64)
    a = (a - a.mean()) / (a.std() + 1e-9)
    theta = orientation_field(a)
    # quantise orientation and take each pixel's matching filter response
    idx = np.round(theta / np.pi * N_ORIENT).astype(int) % N_ORIENT
    out = np.zeros_like(a)
    for k in range(N_ORIENT):
        # theta is the RIDGE direction; the Gabor must oscillate ACROSS it, so
        # the kernel is built at theta + pi/2. Getting this wrong smears the
        # image along the ridges instead of sharpening them.
        resp = ndimage.convolve(a, gabor_kernel(k * np.pi / N_ORIENT + np.pi / 2),
                                mode="nearest")
        out[idx == k] = resp[idx == k]
    return (out - out.mean()) / (out.std() + 1e-9)


def rotate(img, degrees):
    """Rotate about the centre, keeping the frame size."""
    return ndimage.rotate(np.asarray(img, dtype=np.float64), degrees,
                          reshape=False, mode="nearest", order=1)
