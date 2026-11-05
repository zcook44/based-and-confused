#!/usr/bin/env bash
set -euo pipefail
# Restrict guest-to-host DHCP, DNS and RHEL 9 DVD mirror to the lab bridge.
ufw allow in on virbr-stig to any port 67 proto udp comment 'STIG lab DHCP'
ufw allow in on virbr-stig to any port 53 proto udp comment 'STIG lab DNS UDP'
ufw allow in on virbr-stig to any port 53 proto tcp comment 'STIG lab DNS TCP'
ufw allow in on virbr-stig to any port 8089 proto tcp comment 'STIG lab RHEL9 mirror'
ufw status verbose
