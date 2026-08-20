"""synaspi — Linux driver bring-up for the Synaptics SYNA8002 SPI fingerprint
sensor (Lenovo ThinkPad X1 Tablet Gen 3), by porting python-validity's TLS and
capture layer onto our reverse-engineered SPI transport.

See re/synafp/WRITEUP.md and the memory note `synaspi-port-plan` for the full
provenance. The seam is one primitive: transport.SpiUsb.cmd(out)->resp, which
replaces python-validity's usb.Usb.cmd over the same wire semantics.
"""
