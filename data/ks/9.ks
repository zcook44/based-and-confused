# RHEL 9 lab STIG baseline; destructive ONLY on /dev/vda.
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
%addon com_redhat_oscap
content-type = scap-security-guide
profile = xccdf_org.ssgproject.content_profile_stig
%end
# Expected datastream on the installed guest: ssg-rhel9-ds.xml
reboot --eject
