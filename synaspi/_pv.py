"""Locate the vendored python-validity source and put it on sys.path, then import
its modules with our pyusb stub already installed."""
import os
import sys

from . import _compat  # noqa: F401  (installs usb stub before validitysensor loads)

# SYNASPI_PV_DIR lets an installed copy point elsewhere; the relative path is
# the in-tree default so running from a checkout keeps working unchanged.
_PV = os.environ.get("SYNASPI_PV_DIR") or os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "prior-art", "python-validity-master"))
if _PV not in sys.path:
    sys.path.insert(0, _PV)

from validitysensor import tls as tls_mod      # noqa: E402
from validitysensor import usb as usb_mod       # noqa: E402
