#!/bin/bash
# Undo install-local.sh completely: stock fprintd back, our unit gone.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run me with sudo"; exit 1; }
systemctl disable --now synaspi-fprintd.service 2>/dev/null || true
rm -f /etc/systemd/system/synaspi-fprintd.service
systemctl daemon-reload
systemctl unmask fprintd.service
rm -f /etc/dbus-1/system.d/net.reactivated.Fprint.synaspi.conf
rm -f /usr/local/share/dbus-1/system-services/net.reactivated.Fprint.service
systemctl reload dbus || true
rm -rf /usr/local/lib/synaspi
echo "done -- stock fprintd is unmasked (inactive/static, as it was)."
echo "templates are left in /var/lib/synaspi/templates; rm -rf that to remove them."
