# Synaptics SYNA8002 (SPI) — reverse engineering and a working Linux driver

The fingerprint reader in the ThinkPad X1 Tablet Gen 3 is a **Synaptics SYNA8002 on SPI**.
No Linux driver exists for it, and no SPI Synaptics driver exists in libfprint at all.

This is the protocol, and a userspace driver built on it that **logs a real user into GDM
with a thumb**.

It is published as reverse engineering, not as a finished product. Read
[the caveats](#honest-caveats) before you get excited — in particular, it currently needs a
key that only exists if the machine has run Windows.

## Status

| | |
|---|---|
| SPI transport | ✅ solved, confirmed on hardware |
| Mutual-auth TLS handshake to the sensor | ✅ completes |
| Session setup (config blob) | ✅ accepted |
| Image capture | ✅ 144×40, ~0.3 s per frame |
| Enrolment | ✅ own tool, graded per press |
| Matching | ⚠️ host-side correlation, ours, not minutiae |
| PAM / GDM login | ✅ works |
| Touch-to-wake and unlock while locked | ✅ works |
| Survives suspend/resume and cold boot | ✅ works |
| Same template accepted a day later | ✅ measured, see [Results](#results) |
| Pairing a virgin sensor from Linux | ❌ **not implemented — needs a Windows-derived key** |
| libfprint driver | ❌ not attempted |

## What is here

```
PROTOCOL.md         the recovered protocol — transport, secure channel, capture, image format
synaspi/            the driver (Python, ~3.5k lines)
packaging/          systemd unit, D-Bus policy, install/uninstall — this is what puts it in the login path
eval_enroll.py      offline scoring of a template set against probe and impostor sets
outreach-draft.txt  drafts of the messages accompanying this release
```

Inside `synaspi/`:

| module | |
|---|---|
| `transport.py` | SPI framing: status/write/read, and the `0x05` bulk image path |
| `identity.py` | injects the recovered host key into the handshake |
| `bringup.py` | power cycle → handshake → identify → session setup |
| `configblob.py` | selects and sends the vendor config blob (blob not included) |
| `lineupdate.py`, `capture.py` | the product-0x33 capture command, and reading frames |
| `reconstruct.py` | raw lines → a 144×40 image |
| `matcher.py`, `template.py`, `quality.py` | host-side matching and enrolment templates |
| `session.py`, `daemon.py` | open once, capture many; the verify loop |
| `fprintd_adapter.py` | `net.reactivated.Fprint` on the system bus, so PAM works unmodified |
| `wake.py` | watches the sensor while the session is locked; press to wake and unlock |
| `enroll.py` | graded enrolment: scores each press and can reject it |
| `enhance.py` | Gabor ridge enhancement (see the caveats — it did **not** help) |

## Running it

Talk to the sensor once, without touching anything system-wide:

```bash
sudo modprobe spidev bufsiz=65536          # the image reads are large
sudo PYTHONPATH=$PWD python3 -m synaspi.bringup
sudo PYTHONPATH=$PWD python3 -m synaspi.enroll --prefix me    # then score it with eval_enroll.py
```

To actually log in with it, `packaging/` installs the adapter as the system's
`net.reactivated.Fprint` provider, which makes PAM, GDM and GNOME Settings use the
sensor with no client-side changes:

```bash
sudo SYNASPI_PV_DIR=~/src/python-validity SYNASPI_RECOVERED=~/syna-key \
     ./packaging/install-local.sh
```

**Read `packaging/README.md` first** — that command puts unreviewed code in your
login path, and the one thing you must have ready is a second way into the machine.
`packaging/uninstall-local.sh` reverts all of it.

You need two things this repository does **not** contain:

1. **Your sensor's host private key.** Recovered from your own Windows install's registry,
   under `SYNA8002\...\Device Data\PairingData`. `PROTOCOL.md` §4 describes the format.
   Point `SYNASPI_RECOVERED` at a directory holding `host_priv.hex`.
2. **The session config blob**, extracted from `synaWudfBioSpi.dll` on your machine.
   `PROTOCOL.md` §5 describes the catalogue. Place it in `synaspi/blobs/`.

Both are per-device or vendor-owned. Neither is ours to ship.

It also depends on [python-validity](https://github.com/uunicorn/python-validity) for the
TLS and command layer — point `SYNASPI_PV_DIR` at a checkout.

## Results

Numbers from this tablet, one enrolled right thumb, threshold 0.400.

**Separation** (natural-placement enrolment, `eval_enroll.py`, alpha 0.0):

| | |
|---|---|
| genuine floor | +0.4307 |
| impostor max | +0.3402 (21 impostor views, 5 fingers) |
| margin | **+0.0905** |
| FRR / FAR at thr 0.35–0.40 | 0% / 0% |
| template self-consistency (leave-one-out) | min +0.5963, mean +0.7847 |

**Cross-day, in production.** The interesting question for a fixed threshold is not how a
press scores two minutes after enrolment — it is how it scores tomorrow. Template enrolled
one morning; the machine then spent 7 hours powered off; the next morning, at the GDM
greeter on a cold boot:

```
06:50:16  VerifyStart(any) -> verifying right-thumb
06:50:25  press: presence 22.2  score +0.3356  (thr 0.400)   reject
06:50:25  press: presence 35.1  score +0.5117  (thr 0.400)   ACCEPT
06:50:25  verify -> verify-match   best +0.5117, presses 2/11, 3.5 s
```

+0.5117 is **above the genuine floor of the same-session probes**, so a day of drift did
not eat the margin. Two secondary observations from the same log: `presence` is a usable
press-quality signal (a weak placement at 22.2 scored below threshold, a solid one at 35.1
scored well above), and a conservative threshold plus cheap retries is the right trade —
the first press failing cost 1 second, not a fallback to the password.

One data point is one data point. It is evidence that the approach is sound, not a
stability claim over weeks.

## Honest caveats

**It needs Windows to have been there first.** Everything here reuses an existing pairing.
Nothing re-pairs a virgin sensor, and we do not know whether that is possible.

**The matcher is not authentication-grade, and we can prove it.** The image is 144×40 —
too small for minutiae — so matching is correlation-based. Measured against real impostor
fingers, the first version separated genuine from impostor by **+0.0005**, which is not
separation at all. Natural-placement enrolment moved that to **+0.0905**, with FRR 0% /
FAR 0% at threshold 0.35–0.40. That is a real separation, and it holds across days — but
it rests on **small numbers and one enrolled finger**.

The number that should stop you using this as your only authenticator is the **false
accept bound**. Zero false accepts out of 21 impostor views bounds FAR at roughly **14%**
with 95% confidence (rule of three) — that is what 21 samples can tell you, no more.
Windows Hello's small-area bar is FAR < 0.002%. Closing that gap is a question of
collecting several orders of magnitude more impostor data, and quite possibly of a better
matcher; it is not something this repository has done.

Two things that turned out to be false, recorded because they cost real time: more
enrolment views does *not* fix this — impostor scores rise faster than the genuine floor —
and Gabor ridge enhancement raises impostor scores more than genuine ones.

**The unlock-on-touch path steps outside PAM.** Same matcher and threshold, but PAM's
attempt counting is not in the loop. There is a flag to disable it.

**Timing.** Bring-up is ~10.8 s, of which 8 s is the mandatory power-low. Captures are
~0.3 s once the session is open, so retries are cheap.

## What would need to change for libfprint

Matching would move to NBIS, which deletes our weakest component — we have not yet tested
whether `mindtct`/`bozorth3` can extract usable minutiae from a 144×40 image. `elanspi` is
the only SPI precedent in libfprint and would define the shape. The pairing and blob
questions above have to be answered first; see `outreach-draft.txt`.

## Credit

The secure channel is a port of [python-validity](https://github.com/uunicorn/python-validity)
over an SPI transport. Its `tls.py` and command layer are what made this tractable at all.
[Validity90](https://github.com/nmikhailov/Validity90) is the protocol work underneath that.

## Licence

MIT — see `LICENSE`. Chosen to match python-validity, which this builds on.
