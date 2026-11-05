# RHEL 10 lab STIG baseline; destructive ONLY on /dev/vda.
# Review vendor profile and update the ISO to your intended minor release.
text
lang en_US.UTF-8
keyboard --vckeymap=us --xlayouts='us'
timezone UTC --utc
network --bootproto=dhcp --device=link --activate
cdrom
rootpw --lock
user --name=labadmin --groups=wheel --lock
sshkey --username=labadmin "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEHQ/8uSAqv3RNDRpz0OY+7LiP/9J2zdl9zkmLQRAglb zrc@omarchy"
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
@^minimal-environment
openscap-scanner
scap-security-guide
openscap-utils
aide
audit
chrony
%end
# RHEL 10 removed oscap-anaconda-addon. This is post-install remediation,
# not equivalent to a vendor-generated, reviewed install-time STIG Kickstart.
%post --log=/root/ks-stig-remediation.log
oscap xccdf eval --remediate --profile xccdf_org.ssgproject.content_profile_stig \
  --results /root/stig-install-results.xml \
  --report /root/stig-install-report.html \
  /usr/share/xml/scap/ssg/content/ssg-rhel10-ds.xml
result=$?
# OpenSCAP exit 2 means rules remain noncompliant; retain evidence for review.
if [ "$result" -ne 0 ] && [ "$result" -ne 2 ]; then exit "$result"; fi
%end
# Expected datastream on the installed guest: ssg-rhel10-ds.xml
reboot --eject
