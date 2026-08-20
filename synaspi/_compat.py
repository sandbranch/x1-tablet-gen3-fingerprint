"""Stub out pyusb so validitysensor's modules import on a system with no USB
fingerprint stack. We never use the USB transport (validitysensor.usb.Usb) — it
is replaced by synaspi.transport.SpiUsb — but importing validitysensor.tls pulls
`from .usb import usb, Usb`, and usb.py does `import usb.core`. This registers a
minimal stub for `usb` / `usb.core` in sys.modules if the real pyusb is absent.

Import this BEFORE importing anything from validitysensor.
"""
import sys
import types

if "usb" not in sys.modules:
    try:
        import usb.core  # noqa: F401  (real pyusb present)
    except Exception:
        usb_mod = types.ModuleType("usb")
        core_mod = types.ModuleType("usb.core")

        class USBError(Exception):
            pass

        class Device:  # placeholder type for annotations
            pass

        def find(*a, **k):
            return None

        core_mod.USBError = USBError
        core_mod.Device = Device
        core_mod.find = find
        usb_mod.core = core_mod
        sys.modules["usb"] = usb_mod
        sys.modules["usb.core"] = core_mod
