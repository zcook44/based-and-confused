#!/usr/bin/env python3
"""Render RHEL 7.9 DVD-backed, disk-scoped lab Kickstarts."""
import argparse
import pathlib
import re

NODES = {'rhel7-node1': '192.168.150.131', 'rhel7-node2': '192.168.150.132'}
PROFILE = 'xccdf_org.ssgproject.content_profile_stig'
DATASTREAM = '/usr/share/xml/scap/ssg/content/ssg-rhel7-ds.xml'


# Executed last, after installer SCAP remediation and site/account changes.
# Disposable lab exception only: passwordless sudo is a scored STIG finding.
SUDO_POSTINSTALL = '''if [ "$LAB_ACCOUNT" != labadmin ]; then
  ! getent passwd labadmin >/dev/null
  test ! -e /home/labadmin && test ! -L /home/labadmin
  useradd --create-home --shell /bin/bash labadmin
  usermod --lock labadmin
  install -d -m 0700 -o labadmin -g labadmin /home/labadmin/.ssh
  install -m 0600 -o labadmin -g labadmin "/home/$LAB_ACCOUNT/.ssh/authorized_keys" /home/labadmin/.ssh/authorized_keys
  chmod 0700 /home/labadmin
  cmp -s "/home/$LAB_ACCOUNT/.ssh/authorized_keys" /home/labadmin/.ssh/authorized_keys
  restorecon -RF /home/labadmin
fi
test -d /etc/sudoers.d && test ! -L /etc/sudoers.d
grep -Eq '^[[:space:]]*[#@]includedir[[:space:]]+/etc/sudoers.d[[:space:]]*$' /etc/sudoers
test ! -e /etc/sudoers.d/90-labadmin && test ! -L /etc/sudoers.d/90-labadmin
visudo -c >/dev/null
sudo_tmp=$(mktemp /etc/sudoers.d/.labadmin-XXXXXX)
trap 'rm -f -- "$sudo_tmp"' EXIT
printf '%s\\n' '# Disposable RHEL 7 lab exception: violates password-required sudo STIG control.' 'labadmin ALL=(ALL) NOPASSWD: ALL' > "$sudo_tmp"
chown root:root "$sudo_tmp"
chmod 0440 "$sudo_tmp"
visudo -cf "$sudo_tmp" >/dev/null
mv -- "$sudo_tmp" /etc/sudoers.d/90-labadmin
trap - EXIT
restorecon /etc/sudoers.d/90-labadmin
visudo -c >/dev/null
test "$(stat -c '%a %u:%g' /etc/sudoers.d/90-labadmin)" = '440 0:0'
'''


def render_node(name: str, key: str) -> str:
    if name not in NODES:
        raise ValueError(f'unknown RHEL 7 node: {name}')
    key = key.strip()
    if not re.fullmatch(r'(?:ecdsa-sha2-nistp\d+|ssh-rsa) [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._+-]+)?', key):
        raise ValueError('use a single FIPS-compatible ECDSA/RSA public key')
    gui = name == 'rhel7-node2'
    ip = NODES[name]
    # The DVD's SSG 0.1.49 has no stig_gui profile. Its stig profile explicitly
    # removes Xorg, so the GUI node needs one documented, selected-rule exception.
    hardening = (f'''%post --log=/root/ks-stig-remediation.log --erroronfail --interpreter=/bin/bash
cat > /root/rhel7-gui-tailoring.xml <<'TAILORING'
<?xml version="1.0" encoding="UTF-8"?>
<xccdf:Tailoring xmlns:xccdf="http://checklists.nist.gov/xccdf/1.2" id="xccdf_org.lab_tailoring_rhel7_gui">
  <xccdf:benchmark href="{DATASTREAM}"/>
  <xccdf:version time="2024-01-01T00:00:00Z">1</xccdf:version>
  <xccdf:Profile id="xccdf_org.lab_profile_rhel7_gui" extends="{PROFILE}">
    <xccdf:title>RHEL 7 STIG lab GUI; Xorg removal exception</xccdf:title>
    <xccdf:select idref="xccdf_org.ssgproject.content_rule_package_xorg-x11-server-common_removed" selected="false"/>
  </xccdf:Profile>
</xccdf:Tailoring>
TAILORING
chmod 0600 /root/rhel7-gui-tailoring.xml
oscap xccdf eval --remediate --tailoring-file /root/rhel7-gui-tailoring.xml \\
  --profile xccdf_org.lab_profile_rhel7_gui \\
  --results /root/stig-install-results.xml --report /root/stig-install-report.html \\
  {DATASTREAM}
result=$?
if [ "$result" -ne 0 ] && [ "$result" -ne 2 ]; then exit "$result"; fi
systemctl set-default graphical.target
%end''' if gui else f'''%addon org_fedora_oscap
content-type = scap-security-guide
profile = {PROFILE}
%end''')
    return f'''# RHEL 7.9 {name}; installer destroys ONLY its new /dev/vda.
text
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts='us'
timezone UTC --utc
network --bootproto=static --ip={ip} --netmask=255.255.255.0 --gateway=192.168.150.1 --nameserver=192.168.150.1 --hostname={name}.lab.test --device=link --activate --onboot=on
cdrom
rootpw --lock
user --name=labadmin --groups=wheel --lock
selinux --enforcing
firewall --enabled --service=ssh
services --enabled=sshd,chronyd,auditd
bootloader --append="fips=1"
ignoredisk --only-use=vda
zerombr
clearpart --all --initlabel --drives=vda
reqpart --add-boot
part pv.01 --fstype=lvmpv --size=1 --grow --ondisk=vda
volgroup vg_lab pv.01
logvol / --vgname=vg_lab --name=root --fstype=xfs --size=16384 --grow
logvol /home --vgname=vg_lab --name=home --fstype=xfs --size=4096
logvol /tmp --vgname=vg_lab --name=tmp --fstype=xfs --size=4096
logvol /var --vgname=vg_lab --name=var --fstype=xfs --size=8192
logvol /var/tmp --vgname=vg_lab --name=var_tmp --fstype=xfs --size=4096
logvol /var/log --vgname=vg_lab --name=var_log --fstype=xfs --size=4096
logvol /var/log/audit --vgname=vg_lab --name=audit --fstype=xfs --size=4096
logvol swap --vgname=vg_lab --name=swap --fstype=swap --size=2048
%packages
@^{'graphical-server-environment' if gui else 'minimal'}
openscap-scanner
scap-security-guide
openscap-utils
aide
audit
chrony
sudo
shadow-utils
openssh-server
python3
%end
{hardening}
%post --log=/root/lab-postinstall.log --erroronfail --interpreter=/bin/bash
set -e
LAB_ACCOUNT=labadmin
command -v oscap
test -s {DATASTREAM}
install -d -m 0700 -o "$LAB_ACCOUNT" -g "$LAB_ACCOUNT" "/home/$LAB_ACCOUNT/.ssh"
cat > "/home/$LAB_ACCOUNT/.ssh/authorized_keys" <<'LAB_PUBLIC_KEY'
{key}
LAB_PUBLIC_KEY
chown "$LAB_ACCOUNT:$LAB_ACCOUNT" "/home/$LAB_ACCOUNT/.ssh/authorized_keys"
chmod 0700 "/home/$LAB_ACCOUNT" "/home/$LAB_ACCOUNT/.ssh"
chmod 0600 "/home/$LAB_ACCOUNT/.ssh/authorized_keys"
cat >> /etc/hosts <<'LAB_HOSTS'
{NODES['rhel7-node1']} rhel7-node1.lab.test rhel7-node1
{NODES['rhel7-node2']} rhel7-node2.lab.test rhel7-node2
LAB_HOSTS
{SUDO_POSTINSTALL}%end
reboot --eject
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node', choices=NODES, required=True)
    p.add_argument('--ssh-key', type=pathlib.Path, required=True)
    p.add_argument('--out', type=pathlib.Path, required=True)
    a = p.parse_args()
    if a.out.exists() or a.out.is_symlink():
        p.error('refusing existing output')
    try:
        a.out.write_text(render_node(a.node, a.ssh_key.read_text()))
    except (OSError, ValueError) as e:
        p.error(str(e))


if __name__ == '__main__':
    main()
