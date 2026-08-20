"""The sensor CONFIG BLOB — the command our port was missing.

python-validity hardcodes four of these per USB device (`init_hardcoded`,
`init_hardcoded_clean_slate`, `db_write_enable`, `reset_blob`) and sends them as
sensor command **0x06**; every one starts with the magic `02 00 00 01`.  The SPI
driver does not hardcode them: `synaWudfBioSpi.dll` carries a 48-entry catalogue
selected by ROM version and a feature mask, extracted here by
`re/synafp/tools/extract_config_blobs.py`.

Windows sends exactly one of them, ONCE per session, immediately after the TLS
handshake and before the first capture -- the 7,430-byte write in the trip-5
trace (`windows-handover/trip5/results/spi-transfers-run2.csv` @18,564.9 ms):

    blob 7364 B  ->  payload 1+7364 = 7365
                 ->  TLS record 16 + roundup16(7365+32) = 7424 = 0x1D00
                 ->  SPI frame 1 + 5 + 7424 = 7430      (observed exactly)

Our bring-up skipped it (transport.send_init deliberately sent no blob), which
is the likeliest reason the capture command is accepted and then nothing runs.
The "never send blobs" rule came from `reset_blob`, a *different*, 12 KB blob
that wipes the pairing -- not from this one.

Selection, from the DLL (lookup `0x180099a50`, scsSensorConfig.c):
  * group  = 2 when rom.major == 6, else 0            (`0x180099de0`)
  * major/minor must match the record (0xffff = any)
  * (record.mask & wanted) == wanted, and record.mask must not hit the
    caller's exclude mask
Our sensor is ROM 6.21 (major 6, minor 0x15) -> group 2, and the mask Windows
used is 0x400000 (the only 7364-byte record for this ROM).
"""
import json
import logging
import os

log = logging.getLogger("synaspi.configblob")

BLOB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blobs")

# What Windows sends on this tablet. Group 2 is what the DLL selects for
# rom.major == 6; the group-3 record for the same ROM/mask is a different 7364-byte
# blob (older table kept in the DLL) -- switch GROUP to 3 to try it.
GROUP = 2
MASK = 0x400000


def _index():
    with open(os.path.join(BLOB_DIR, "index.json")) as f:
        return json.load(f)


def select(major: int, minor: int, mask: int = MASK, group: int = GROUP):
    """Return (metadata, blob bytes) for this ROM, or (None, None)."""
    for r in _index():
        if r["group"] != group or r["major"] != major or r["minor"] != minor:
            continue
        if (r["mask"] & mask) != mask:
            continue
        with open(os.path.join(BLOB_DIR, r["file"]), "rb") as f:
            return r, f.read()
    return None, None


def send(tls, rom_info, mask: int = MASK, group: int = GROUP) -> bool:
    """Upload the config blob over the secure channel as cmd 0x06.

    Returns True if a blob was found and the sensor accepted it."""
    from validitysensor.util import assert_status

    meta, blob = select(rom_info.major, rom_info.minor, mask, group)
    if blob is None:
        log.error("no config blob for ROM %d.%02x group %d mask 0x%x",
                  rom_info.major, rom_info.minor, group, mask)
        return False
    if blob[:4] != b"\x02\x00\x00\x01":
        log.error("blob %s has the wrong magic %s", meta["file"], blob[:4].hex())
        return False

    log.info("config blob %s: %d B (rom 6.%02x, mask 0x%x, group %d)",
             meta["file"], len(blob), meta["minor"], meta["mask"], meta["group"])
    log.info("  expected SPI write frame: %d bytes", 6 + 16 + (((len(blob) + 1 + 32) // 16 + 1) * 16))
    rsp = tls.cmd(b"\x06" + blob)
    assert_status(rsp)
    log.info("config blob ACCEPTED (status 0x0000)")
    return True
