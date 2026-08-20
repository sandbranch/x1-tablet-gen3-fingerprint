#!/bin/bash
# Copy everything the service needs into /usr/local/lib/synaspi, so that
# fingerprint login does not depend on a checkout under /home.
#
# Why this matters beyond tidiness: the unit runs as ROOT. Running root code out
# of a directory the desktop user can write to is a local privilege escalation
# waiting to happen, and it also means a `git pull` can silently break login.
#
# Two things this repository cannot ship have to be pointed at explicitly --
# see the README for what they are and where to get them:
#
#   SYNASPI_PV_DIR      a python-validity checkout      (vendor-owned code)
#   SYNASPI_RECOVERED   YOUR sensor's pairing material  (per-device secret)
#
#   sudo SYNASPI_PV_DIR=~/src/python-validity \
#        SYNASPI_RECOVERED=~/syna-key ./install-runtime.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run me with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"          # the repository root
DEST=/usr/local/lib/synaspi

PV="${SYNASPI_PV_DIR:-}"
KEY="${SYNASPI_RECOVERED:-}"
[[ -n "$PV"  && -f "$PV/tls.py" ]] || {
    echo "SYNASPI_PV_DIR must point at a python-validity checkout (no tls.py found)"; exit 1; }
[[ -n "$KEY" && -f "$KEY/host_priv.hex" ]] || {
    echo "SYNASPI_RECOVERED must point at a directory holding host_priv.hex"
    echo "  (also expected: tls_cert.bin, ecdh.bin -- see PROTOCOL.md section 4)"; exit 1; }

echo "== installing runtime to $DEST"
install -d -m 0755 "$DEST"
rm -rf "$DEST/synaspi" "$DEST/python-validity"
cp -r "$SRC/synaspi" "$DEST/synaspi"
find "$DEST/synaspi" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
cp -r "$PV" "$DEST/python-validity"
install -m 0644 "$SRC/packaging/README.md" "$DEST/PACKAGING.md"   # the unit's Documentation=
chmod -R go-w "$DEST"

# this machine's pairing key: root-only, never world-readable
echo "== installing private key material to $DEST/private (0700)"
install -d -m 0700 "$DEST/private"
for f in host_priv.hex tls_cert.bin ecdh.bin; do
    [[ -f "$KEY/$f" ]] && cp "$KEY/$f" "$DEST/private/"
done
chmod 0600 "$DEST"/private/*

echo "== installing D-Bus policy (independent of the fprintd package)"
install -m 0644 "$SRC/packaging/net.reactivated.Fprint.synaspi.conf" \
    /etc/dbus-1/system.d/
systemctl reload dbus 2>/dev/null || true

echo "== done. Contents:"
ls -la "$DEST"
