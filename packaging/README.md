# Wiring the driver into PAM

`libpam-fprintd` and stock `fprintd` both talk to one bus name,
`net.reactivated.Fprint`. `synaspi.fprintd_adapter` implements that interface, so
taking the name over makes `pam_fprintd.so`, GNOME Settings and `fprintd-verify`
work against the SYNA8002 **with no client-side changes** — no PAM edits, no
patched fprintd, nothing in `/etc/pam.d`.

This is running on the author's tablet: GDM greeter, screen unlock and `su` all
accept a thumb. It is still unreviewed code in the login path. Read the warning
below before you enable it.

## ⚠️ Before you enable it

Owning that bus name means sitting **in the login path**. If the adapter hangs
while PAM is waiting on it, your login prompt hangs with it.

1. **Have a second way in** — a root TTY already logged in, or SSH from another
   machine. Do not skip this.
2. Confirm `auth sufficient pam_fprintd.so` still falls through to the password
   on failure (Debian/Ubuntu's `common-auth` ordering does; check yours).
3. Try it on the **session** bus first, which touches nothing:
   ```
   python3 -m synaspi.fprintd_adapter --session-bus --store <dir>
   gdbus call --session --dest net.reactivated.Fprint ...
   ```

Backing out is one command and is tested: `./uninstall-local.sh`.

## Install

```bash
sudo SYNASPI_PV_DIR=~/src/python-validity \
     SYNASPI_RECOVERED=~/syna-key \
     ./install-local.sh
```

Those two variables are the things this repository cannot ship — a
python-validity checkout, and your own sensor's pairing material. See the top
level README.

What it does: masks stock `fprintd`, copies the runtime to
`/usr/local/lib/synaspi` (root-owned, out of `/home`), installs the unit,
repoints D-Bus activation, and waits for bring-up.

## Why the activation file matters (measured)

Masking stock fprintd leaves the packaged
`/usr/share/dbus-1/system-services/net.reactivated.Fprint.service` pointing at a
masked unit, so every request arriving before our daemon owns the name is a
**hard error, not a wait**:

```
12:35:47.548 dbus: Activation via systemd failed for unit 'fprintd.service': Unit fprintd.service is masked.
12:35:50.844 pam_fprintd(gdm-password:auth): GetDevices failed: Unit fprintd.service is masked.
12:36:00.749 synaspi-fprintd: owning net.reactivated.Fprint     <-- 13 s after gdm asked
```

The symptom on the greeter is subtle and easy to misdiagnose: **no fingerprint
option on the first user-select**, appearing only if you go back and select the
user again. With activation pointed at `synaspi-fprintd.service`, a cold
`GetDevices` blocks for the bring-up and then succeeds (measured 12.3 s).

Headroom note: dbus's default `service_start_timeout` is 25 s, so bring-up has
roughly 2× margin. If bring-up ever grows or gains retries, that is the limit to
raise.

## Four protocol details fprintd clients depend on

Found the hard way, each one a silent failure rather than an error:

| | |
|---|---|
| `VerifyFingerSelected` | must carry the **finger name**, not the device path |
| signal ordering | emit the result signal **after** the method reply, never before |
| retries | do not emit `verify-retry` before a decision is reached — clients treat it as the decision |
| errors | dbus-python needs the **`net.reactivated.Fprint.Error.*`** names exactly, or the client sees a generic failure |

There is also a **stuck-claim** failure worth knowing about: a client that exits
without calling `Release` strands `claimed_by` forever. The service still looks
healthy, `Claim` quietly fails, and the thumb simply stops working. The adapter
now tracks the claimant's bus name, watches `NameOwnerChanged`, and reaps a dead
claim on every poll.

## What still has to be built

* **Enrolment over D-Bus** (`EnrollStart` currently errors). Enrolment runs
  through `python3 -m synaspi.enroll`, which *grades and can reject* a press —
  fprintd's model is a fixed count of stages that only ever advance. How a
  rejected press should be reported is a design question, not plumbing.
* **Resume**: the adapter re-runs bring-up when the session dies (the
  `_ensure_open` path), but nothing listens for logind's `PrepareForSleep`, so
  the first verify after a resume pays the ~12 s reopen.
* **Template adaptation.** Not currently needed — a fixed threshold survived a
  22-hour gap and a cold boot (see the top level README) — but it is the standard
  answer if drift ever shows up over weeks rather than days.
