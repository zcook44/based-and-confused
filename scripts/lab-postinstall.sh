#!/usr/bin/env bash
# Run ONLY inside the installed target (Anaconda's chrooted final post script).
# Installer/SCAP copies and atomic replacements can leave root_t in /etc.
# Never use the installer's file_contexts or hard-coded type names.
set +x
set -euo pipefail
umask 077

die() { printf 'lab-postinstall: %s\n' "$*" >&2; exit 1; }
[[ $# == 2 && $EUID == 0 ]] || die 'requires target root, username and RHEL major'
user=$1
major=$2
[[ $user =~ ^[a-z_][a-z0-9_-]{0,31}$ && $user != root ]] || die 'unsafe username'
[[ $major == 8 || $major == 9 || $major == 10 ]] || die 'unsupported target release'
grep -Eq '^ID="?rhel"?$' /etc/os-release || die 'not an installed RHEL target'
grep -Eq "^VERSION_ID=\"?${major}([.][0-9]+)?\"?$" /etc/os-release || die 'target release mismatch'
grep -Eq '^SELINUX=enforcing[[:space:]]*$' /etc/selinux/config || die 'SELinux must remain enforcing'
grep -Eq '^SELINUXTYPE=targeted[[:space:]]*$' /etc/selinux/config || die 'targeted policy required'

contexts=/etc/selinux/targeted/contexts/files/file_contexts
policy=$(printf '%s\n' /etc/selinux/targeted/policy/policy.[0-9]* | sort -V | tail -n 1)
[[ $policy =~ /policy\.[0-9]+$ && -s $policy && -s $contexts ]] || die 'missing target policy'
for path in /etc /home; do
  [[ -d $path && ! -L $path ]] || die 'unsafe target directory'
done
critical=(/etc/fstab /etc/resolv.conf /etc/hosts /etc/passwd /etc/shadow
          /etc/group /etc/gshadow /etc/sudoers /etc/ssh/sshd_config)
for path in "${critical[@]}"; do
  [[ -e $path ]] || die "missing critical file: $path"
done
entry=$(getent passwd "$user")
IFS=: read -r account unused uid gid gecos home shell <<< "$entry"
[[ $account == "$user" && $uid =~ ^[0-9]+$ && $uid != 0 && $gid =~ ^[0-9]+$ &&
   $home == "/home/$user" && $entry != *$'\n'* ]] || die 'unsafe account home or identity'
for path in "$home" "$home/.ssh"; do
  [[ -d $path && ! -L $path ]] || die 'unsafe SSH directory'
done
key="$home/.ssh/authorized_keys"
[[ -s $key && -f $key && ! -L $key && $(stat -c %h "$key") == 1 ]] || die 'unsafe or missing authorized_keys'

# Validate site customization against the installer menu's requested identity.
# Anaconda's chroot still exposes the installer kernel command line.
requested_hostname='' requested_ip='' hostname_count=0 ip_count=0
read -r -a cmdline <<< "$(cat /proc/cmdline)"
for arg in "${cmdline[@]}"; do
  case $arg in
    lab.hostname=*) requested_hostname=${arg#*=}; ((hostname_count+=1)) ;;
    lab.ip=*) requested_ip=${arg#*=}; ((ip_count+=1)) ;;
  esac
done
[[ $hostname_count == 1 && $ip_count == 1 &&
   $requested_hostname =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$ &&
   $requested_ip =~ ^192\.168\.150\.[0-9]+$ ]] || die 'missing or unsafe requested lab identity'
octet=${requested_ip##*.}
((10#$octet >= 100 && 10#$octet <= 199)) || die 'requested IP outside lab range'
[[ -s /etc/hostname && $(cat /etc/hostname) == "$requested_hostname" ]] || die 'hostname customization missing or mismatched'
[[ -s /etc/default/grub ]] || die 'missing installed GRUB defaults'

# Inspect persistent profiles without starting NetworkManager in the installer.
# RHEL 8 may use ifcfg; RHEL 9/10 normally use NetworkManager keyfiles.
shopt -s nullglob
connections=()
for path in /etc/NetworkManager/system-connections/*; do
  [[ -f $path && ! -L $path ]] || continue
  if awk -v address="$requested_ip/24" '
    substr($0, 1, 1) == "[" { ipv4 = ($0 == "[ipv4]") }
    /^autoconnect=(false|0)$/ { off = 1 }
    ipv4 && /^method=manual$/ { manual = 1 }
    ipv4 && /^address[0-9]+=/ {
      split($0, fields, "="); split(fields[2], values, ",")
      if (values[1] == address) matched = 1
    }
    END { exit !(manual && matched && !off) }
  ' "$path"; then connections+=("$path"); fi
done
if [[ $major != 10 ]]; then
  for path in /etc/sysconfig/network-scripts/ifcfg-*; do
    [[ -f $path && ! -L $path ]] || continue
    if awk -F= -v address="$requested_ip" '
      { value=$2; gsub(/"/, "", value) }
      $1 == "BOOTPROTO" && (value == "none" || value == "static") { manual=1 }
      $1 == "ONBOOT" && value == "yes" { onboot=1 }
      ($1 == "IPADDR" || $1 == "IPADDR0") && value == address { matched=1 }
      (($1 == "PREFIX" || $1 == "PREFIX0") && value == "24") ||
        ($1 == "NETMASK" && value == "255.255.255.0") { subnet=1 }
      END { exit !(manual && onboot && matched && subnet) }
    ' "$path"; then connections+=("$path"); fi
  done
fi
[[ ${#connections[@]} == 1 ]] || die 'missing or ambiguous persistent static lab connection'
critical+=(/etc/hostname /etc/default/grub /etc/selinux/config "${connections[0]}")

# This is persistence repair for a FIPS installation, NEVER post-install
# FIPS enablement. RHEL 10 removed /etc/system-fips and fips-mode-setup.
[[ $(cat /proc/sys/crypto/fips_enabled) == 1 ]] || die 'installer was not booted in FIPS mode'
crypto_policy=$(update-crypto-policies --show)
[[ $crypto_policy == FIPS || $crypto_policy == FIPS:* ]] || die 'installed crypto policy is not FIPS'
if [[ $major != 10 ]]; then
  [[ -f /etc/system-fips ]] || die 'missing installed FIPS marker'
fi
check_boot_entries() {
  local info=$1 require_fips=$2 line args arg found entries=0
  local -a tokens
  while IFS= read -r line; do
    [[ $line == args=* ]] || continue
    args=${line#args=}
    args=${args#\"}
    args=${args%\"}
    read -r -a tokens <<< "$args"
    found=0
    for arg in "${tokens[@]}"; do
      case $arg in
        fips=1) found=1 ;;
        fips=*) die 'boot entry weakens FIPS' ;;
        selinux=*|enforcing=*) [[ ${arg#*=} == 1 ]] || die 'boot entry weakens SELinux' ;;
      esac
    done
    [[ $require_fips == 0 || $found == 1 ]] || die 'FIPS missing from a boot entry'
    ((entries+=1))
  done <<< "$info"
  ((entries > 0)) || die 'no kernel boot entries found'
}
boot_info=$(grubby --info=ALL)
check_boot_entries "$boot_info" 0

chown "$uid:$gid" "$home" "$home/.ssh" "$key"
chmod 0700 "$home" "$home/.ssh"
chmod 0600 "$key"
for path in "$home" "$home/.ssh" "$key"; do
  [[ $(stat -c %u:%g "$path") == "$uid:$gid" ]] || die 'SSH ownership verification failed'
done
[[ $(stat -c %a "$home") == 700 && $(stat -c %a "$home/.ssh") == 700 &&
   $(stat -c %a "$key") == 600 ]] || die 'SSH permission verification failed'

# The Bash bootstrap may choose a different, password-bearing operator account.
# Retain a separate locked-password, key-only labadmin on every such guest.
# Do not silently give the operator account passwordless sudo.
if [[ $user != labadmin ]]; then
  if getent passwd labadmin >/dev/null; then die 'unexpected existing labadmin account'; fi
  [[ ! -e /home/labadmin && ! -L /home/labadmin ]] || die 'unsafe labadmin home'
  useradd --create-home --shell /bin/bash labadmin
  usermod --lock labadmin
  shadow_entry=$(getent shadow labadmin)
  [[ $shadow_entry == labadmin:!* || $shadow_entry == labadmin:\** ]] || die 'labadmin password is not locked'
  admin_entry=$(getent passwd labadmin)
  IFS=: read -r admin_name unused admin_uid admin_gid gecos admin_home shell <<< "$admin_entry"
  [[ $admin_name == labadmin && $admin_uid =~ ^[0-9]+$ && $admin_uid != 0 &&
     $admin_gid =~ ^[0-9]+$ && $admin_home == /home/labadmin &&
     -d $admin_home && ! -L $admin_home && ! -e $admin_home/.ssh && ! -L $admin_home/.ssh ]] ||
    die 'unsafe new labadmin identity'
  mkdir -m 0700 -- "$admin_home/.ssh"
  admin_key=$admin_home/.ssh/authorized_keys
  cp -- "$key" "$admin_key"
  chown "$admin_uid:$admin_gid" "$admin_home" "$admin_home/.ssh" "$admin_key"
  chmod 0700 "$admin_home" "$admin_home/.ssh"
  chmod 0600 "$admin_key"
  cmp -s -- "$key" "$admin_key" || die 'labadmin key copy differs'
  for path in "$admin_home" "$admin_home/.ssh" "$admin_key"; do
    [[ $(stat -c %u:%g "$path") == "$admin_uid:$admin_gid" ]] || die 'labadmin SSH ownership verification failed'
  done
  [[ $(stat -c %a "$admin_home") == 700 && $(stat -c %a "$admin_home/.ssh") == 700 &&
     $(stat -c %a "$admin_key") == 600 ]] || die 'labadmin SSH permission verification failed'
  critical+=("$admin_home" "$admin_home/.ssh" "$admin_key")
fi

# Persist the install-time FIPS boot flag with Red Hat's documented grubby
# all-entry interface (BLS-aware); do not hand-edit GRUB/BLS or change crypto policy.
# RHEL 10 has no fips-mode-setup and cannot be switched after installation.
grubby --update-kernel=ALL --args=fips=1
boot_info=$(grubby --info=ALL)
check_boot_entries "$boot_info" 1

# Explicit, lab-only exception: key-authenticated labadmin may sudo without
# a password. This fails the STIG password-prompt control; never ship as a
# compliant production baseline. Do not grant the whole wheel group NOPASSWD.
sudo_dir=/etc/sudoers.d
sudo_rule=$sudo_dir/90-labadmin
[[ -d $sudo_dir && ! -L $sudo_dir && ! -e $sudo_rule && ! -L $sudo_rule ]] || die 'unsafe lab sudoers destination'
grep -Eq '^[[:space:]]*[#@]includedir[[:space:]]+/etc/sudoers.d[[:space:]]*$' /etc/sudoers || die 'sudoers.d is not included'
sudo_tmp=$(mktemp "$sudo_dir/.labadmin-XXXXXX")
printf '# Disposable lab exception: violates password-required sudo STIG control.\nlabadmin ALL=(ALL) NOPASSWD: ALL\n' > "$sudo_tmp"
chown root:root "$sudo_tmp"
chmod 0440 "$sudo_tmp"
visudo -cf "$sudo_tmp" >/dev/null || die 'invalid labadmin sudo rule'
mv -- "$sudo_tmp" "$sudo_rule"
visudo -cf /etc/sudoers >/dev/null || die 'invalid effective sudo policy'
[[ $(stat -c %u:%g "$sudo_rule") == 0:0 && $(stat -c %a "$sudo_rule") == 440 ]] || die 'unsafe labadmin sudo rule permissions'
critical+=("$sudo_rule")

# setfiles(8): -c validates against THIS target's binary policy; -m avoids
# installer /proc/mounts exclusions. Walk all persistent files, not just fstab.
setfiles -F -m -c "$policy" -e /dev -e /proc -e /sys -e /run "$contexts" /
matchpathcon -N -V -f "$contexts" "${critical[@]}" "$home" "$home/.ssh" "$key"
