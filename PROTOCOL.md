# Synaptics SYNA8002 over SPI — recovered protocol

Reverse engineered from Lenovo's Windows driver (`synaWudfBioSpi.dll`, the non-SGX one)
and confirmed against real hardware on a ThinkPad X1 Tablet Gen 3. Sensor reports itself
as **`88B0 FM-3310-001`, type `0x580`**, ROM 6.21 build 170, product 51.

Everything below is implemented in `synaspi/` and runs.

## 1. Transport

The sensor is an SPI device (`spi-SYNA8002:00`, ACPI `\_SB.PCI0.SPI1.FPNT`) driven
through `spidev`. Three frame types, all full-duplex. **Every response body starts at RX
byte 2** — the first two MISO bytes are turnaround and always `0xFF`. No CRC, no length
field, no framing beyond the opcode.

### `0x01` STATUS

```
TX:  01 [seq] 00 00 00 00
RX:  FF  FF   [status u32, little-endian]
```

`seq` is a byte counter, post-incremented per status read. Status bits:

| bit | meaning |
|---|---|
| `0x80000000` | BUSY — status invalid, poll again |
| `0x40000000` | ERROR |
| `0x20000000` | set on our hardware, meaning unknown |
| `0x10000000` | RESPONSE AVAILABLE — a reply is waiting |
| `0x08000000` | READY — sensor will accept a command |

Easy to get backwards, and we did: the send path waits on READY (`0x08000000`), the
receive path on RESPONSE (`0x10000000`).

### `0x02` WRITE

```
TX:  02 [payload ...]
```

No length, no checksum, no sequence byte. `len(payload) + 1 > 0x7000` is rejected.

### `0x03` READ

```
TX:  03 [seq2] FF x n
RX:  FF  FF    [payload x n]
```

A **separate** sequence counter from STATUS.

### `0x05` BULK IMAGE READ

This was the blocker for a long time, and it is not documented anywhere else we found.
**Image data does not come back through `0x03`** — `0x03` returns all-`0xFF` for it. It
comes through opcode `0x05`, and the id byte is taken from **`rx[2]` of the status word**,
not from a fixed constant. Two further traps:

- the RESPONSE-AVAILABLE bit is **never set** for image data, so a reader that waits for
  it hangs forever;
- `rx[3:5]` of the status word is a staged-byte counter, not a length.

The payload is **plaintext**, outside the secure channel.

### Transaction

```
wait for READY -> write [0x02]+payload -> wait for RESPONSE -> read [0x03]+seq -> parse
```

Error codes seen: `0x6E` generic, `0x6F` bad length, `0x70` null arg, `0x75` low-level
timeout, `0xC9` sensor error bit, `0xCA` transaction timeout.

## 2. Command layer

Above the transport sits the versioned command layer Synaptics calls `scsFwV4`, a
55-command table. Payload encoding is minimal — mostly a command byte and packed
arguments. Command `0x01` returns a 38-byte device identity that is **byte-identical**
between Linux and Windows at cold boot; that result retired our "the sensor is in base
ROM" theory.

## 3. The secure channel — the real gate

Every privileged command is refused until the host completes a **Synaptics-custom
mutual-auth TLS 1.2 handshake**. The sensor chooses `C005` (ECDH_ECDSA) and demands a
`0x40 ecdsa_sign` client certificate. The host sends:

- a **184-byte, non-X.509 certificate** — the format is exactly python-validity's
  `make_cert`, header `pack('<LL', 0x17, 0x20)`, the two fields reversed giving a valid
  on-curve P-256 point;
- a P-256 ClientKeyExchange;
- a bare-DER ECDSA CertificateVerify.

The certificate is **signed by a constant key** (`hs_key`, which python-validity derives
publicly — that is why it looked like the host key was constant). The **subject** is a
per-pairing key, and that is the one that matters.

The key derivation is a BCrypt TLS-PRF: `KDF(secret, label, seed, n) =
tls_prf_sha256(secret, label || seed)[:n]`.

## 4. Where the host key lives — and why this part is different

On the USB Prometheus parts python-validity handles, the host private key sits
AES-encrypted in the sensor's own flash, under a DMI-derived PSK.

**This part has no flash at all.** Command `0x3e` *succeeds* and reports JEDEC `ff:ff`
with **zero partitions** — so `0x04af` from every flash read means "there is no flash",
not "permission denied". That single result closed a week of trying to unlock storage.

It also means the pairing data is **host-side only**. On Windows it lives in the registry
under `SYNA8002\...\Device Data\PairingData`, as a 4 KB plaintext cache. Entry id 4 is
`AES-256-CBC(K1, iv)` over `0*32 || 0*32 || d[::-1]` — the X and Y fields are zeroed in
current driver versions, and the private scalar is byte-reversed in the last block.

Recovering it and checking `d·G` against the public key the sensor presents in its own
handshake gives an exact match. That is how this driver authenticates.

**Consequence: this driver currently requires the machine to have run Windows.** Nothing
here re-pairs a virgin sensor. See the open questions.

The Windows trace also settles where matching happens: enrolment runs `CEohMohEIV` —
Enrol-on-Host, Match-on-Host — with **zero** `CEocMoc*` and zero `*StorageOnFlash` calls.
Templates and matching are host-side on Windows too.

## 5. Session setup — `cmd 0x06` and the config blob

After the handshake the sensor still refuses to capture until it is sent a **7,364-byte
configuration blob** via command `0x06` (7,430 bytes on the wire). The blob is selected
from a **48-entry catalogue inside `synaWudfBioSpi.dll`** by ROM revision, mask and group
— ours is `cfg_g2_6_15_m00400000.bin` (ROM 6.15, mask `0x400000`, group 2).

This is vendor data and is **not included here**. `tools/extract_config_blobs.py` in the
original tree pulls the catalogue out of the DLL.

Note the historical trap: an older rule in this project said "never send blobs", which was
correct — but about `reset_blob`, a *different* 12 KB blob that wipes the Windows
enrolment. It wrongly blocked this one for days.

## 6. Capture

Product `0x33` uses a **third line-update builder** that python-validity does not have
(it has type_1 and type_2). Wire format is `0x30` + `0x43` + `0x44` records with a
bit-packed payload; the sensor's own capture program is 2,372 bytes, 80 lines per frame,
160 bytes per line, `key_calibration_line = 20`, factory bits `[2,3,5,8,9,10,12,15]`.

**The capture command is only accepted as the first `cmd 0x02` of a session.** Any later
one returns `0x0450` regardless of content. This rule invalidated several earlier
bisections before we noticed it, and it is worth knowing before you debug anything here.

Do **not** re-send the config blob between captures — it is refused with `0x04aa`.

## 7. Image format

Raw readout is 152-byte lines: an 8-byte header (carrying a line counter, which is how
frames are found) followed by 144 pixels. 80 lines per frame, marker every 20 lines.

Lines interleave: pairs at `(1,2)`, `(3,4)` parity de-interleave into a **144 × 40**
image. Multiple frames are **temporal repeats** — consecutive frames differ by ~1.4, the
noise floor — so they must be *averaged* for SNR, not stacked for height. 144 × 40 is the
native size.

Presence detection is done host-side: mean |pixel − no-finger baseline| is ~1.2–1.4 with
nothing on the sensor and 26–39 for a real press.

## 8. Timings

| | |
|---|---|
| Bring-up (8 s power-low + handshake + setup) | ~10.8 s |
| One capture, session already open | ~0.3–1.1 s |

Bring-up cost is why the driver opens one session and holds it, rather than opening per
verify. Across a suspend/resume only the TLS session dies — the rail, the GPIO holder and
`spidev` all survive, and a fresh bring-up works with no reboot.

## 9. What is still unknown

- Whether a **virgin sensor can be paired from Linux**. Everything here reuses an existing
  Windows pairing.
- Whether the config blob is derivable rather than extracted.
- Why the capture command is only accepted first in a session.
- Whether the sensor can signal finger presence on its interrupt line (ACPI declares
  `GpioInt` on `GPI0` pin 41) instead of being polled. Nothing here uses it.
- `opcode 0x2A` is very likely an ETW trace record, not a sensor opcode.
