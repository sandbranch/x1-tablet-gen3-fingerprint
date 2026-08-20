"""Inject the recovered host credential into a python-validity `Tls` instance,
bypassing the gated flash read and the machine-bound decrypt path.

On this sensor generation the host key blob (flash id=4) is AES-CBC(K1) with X,Y
zeroed, so python-validity's handle_priv (which decrypts under psk_encryption_key
and HMACs under psk_validation_key) does not apply. We already recovered and
verified d off the Windows registry cache (d.G == the on-wire pubkey), so we set
the identity fields directly:
    tls.priv_key  <- derive_private_key(d)
    tls.tls_cert  <- recovered/tls_cert.bin   (flash id=3, the make_cert blob)
    tls.ecdh_q    <- pubkey parsed from recovered/ecdh.bin (flash id=6)

We parse ecdh_q manually rather than calling handle_ecdh(), because handle_ecdh
verifies the blob's signature against python-validity's hardcoded USB firmware
public key, which is a different fw key than this SPI part uses.
"""
import logging
import os
from binascii import hexlify

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger("synaspi.identity")

# SYNASPI_RECOVERED lets an installed copy keep this machine's pairing key
# somewhere root-only, instead of in a user-writable working tree.
RECOVERED = os.environ.get("SYNASPI_RECOVERED") or os.path.join(
    os.path.dirname(__file__), "..", "..", "recovered")


def _load(name: str) -> bytes:
    with open(os.path.join(RECOVERED, name), "rb") as f:
        return f.read()


def load_private_key():
    d_hex = _load_text("host_priv.hex").strip()
    d = int(d_hex, 16)
    return d, ec.derive_private_key(d, ec.SECP256R1(), backend=default_backend())


def _load_text(name: str) -> str:
    with open(os.path.join(RECOVERED, name)) as f:
        return f.read()


def parse_ecdh_q(ecdh_blob: bytes):
    """ecdh_q from flash id=6: key = blob[:0x90]; x @ +0x08, y @ +0x4c, each a
    32-byte little-endian field element (same layout as tls.handle_ecdh)."""
    key = ecdh_blob[:0x90]
    x = int(hexlify(key[0x08:0x08 + 0x20][::-1]), 16)
    y = int(hexlify(key[0x4c:0x4c + 0x20][::-1]), 16)
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key(default_backend())


def inject(tls):
    """Populate the identity fields tls.open() needs: priv_key, tls_cert, ecdh_q."""
    d, priv = load_private_key()
    tls.priv_key = priv

    cert = _load("tls_cert.bin")
    tls.handle_cert(cert)                       # just stores tls.tls_cert; no crypto

    ecdh_blob = _load("ecdh.bin")
    tls.ecdh_blob = ecdh_blob
    tls.ecdh_q = parse_ecdh_q(ecdh_blob)

    # sanity: d.G must equal the cert's embedded pubkey (already verified offline)
    pub = priv.public_key().public_numbers()
    log.info("injected host key d=0x%064x", d)
    log.info("host pubkey X=0x%064x", pub.x)
    log.info("host pubkey Y=0x%064x", pub.y)
    return d
