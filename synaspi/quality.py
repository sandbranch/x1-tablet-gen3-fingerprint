"""Frame quality gates shared by enrolment and the daemon.

Both need the same two questions answered about a raw capture: *is a finger on
the sensor at all*, and *is this the same physical contact as the last frame*.
Keeping them here stops the two callers drifting apart -- an enrolment that
accepts a view the daemon would then refuse to score is worse than useless.

The numbers are measured, not guessed (2026-08-18/19 capture sets):

    no finger        mean |diff| ~1.2 - 1.4      <- the sensor's noise floor
    real press       mean |diff| ~26 - 39
    same contact     consecutive |diff| ~1.4 - 2
    lift + repress   consecutive |diff| >= ~4
"""
from synaspi import reconstruct as R

#: below this mean |diff| against the no-finger baseline, nothing is touching it
MIN_PRESENCE = 10.0
#: consecutive views closer than this are the same contact re-read, not a new press
MIN_CHANGE = 2.5


def presence(blob, baseline):
    """mean |pixel - no-finger baseline| over the 144x40 image, unnormalised."""
    img = R.reconstruct(blob, baseline, clean=False)
    return sum(abs(v) for row in img for v in row) / (len(img) * len(img[0]))


def change(blob_a, blob_b):
    """mean |a - b| between two raw captures, unnormalised."""
    a = R.reconstruct(blob_a, None, clean=False)
    b = R.reconstruct(blob_b, None, clean=False)
    n = len(a) * len(a[0])
    return sum(abs(x - y) for ra, rb in zip(a, b) for x, y in zip(ra, rb)) / n
