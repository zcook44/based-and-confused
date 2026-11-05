#!/usr/bin/env bash
# Provision or power-manage the eight disposable RHEL STIG pipeline lab VMs.
set +x # never trace passwords supplied by the operator's env file
set -euo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ISO="${LAB_ISO:-/scratch/iso/rhel-stig-lab-menu-8.10-9.8-10.2-gui-login.iso}"
readonly VM_DIR="${LAB_VM_DIR:-/scratch/vms}"
readonly KERNEL_DIR="${LAB_KERNEL_DIR:-${VM_DIR}/.installer-kernels-8.10-9.8-10.2}"
readonly ENV_FILE="${LAB_ENV_FILE:-${SCRIPT_DIR}/lab-vms.env}"
readonly HOST_REPO='http://192.168.150.1:8089/repo'
readonly LABEL='RHEL-10-2-BaseOS-x86_64'
readonly -a ALL_NODES=(rhel9-rheladm rhel9-node1 rhel9-node2 rhel10-rheladm rhel10-node1 rhel10-node2 rhel8-node1 rhel8-node2)
readonly -a VIRT_INSTALL=(/usr/bin/python3 /usr/sbin/virt-install)

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
Usage: lab-vms.sh [--dry-run] <up|prepare|start|stop|status> <all|node...>
  up       Create missing VMs with their selected unattended Kickstart; start stopped VMs.
  prepare  Generate private per-node Kickstarts from the ISO; do not create VMs.
  start    Start already-defined VMs only (never reinstall).
  stop     Request graceful shutdown; never delete VMs or disks.
  status   Show each selected VM's current libvirt state.
  --dry-run  Show the exact VM commands without writing disks or starting VMs.
Nodes: rhel8-node1 rhel8-node2 rhel9-rheladm rhel9-node1 rhel9-node2 rhel10-rheladm rhel10-node1 rhel10-node2
Set LAB_ENV_FILE to an owner-only (0600) environment file; default: ./lab-vms.env.
HELP
}

lookup_node() {
  local name=$1
  case "$name" in
    rhel8-node1)   version=8; role=node1; ip=192.168.150.121 ;;
    rhel8-node2)   version=8; role=node2; ip=192.168.150.122 ;;
    rhel9-rheladm)  version=9; role=rheladm; ip=192.168.150.101 ;;
    rhel9-node1)   version=9; role=node1; ip=192.168.150.102 ;;
    rhel9-node2)   version=9; role=node2; ip=192.168.150.103 ;;
    rhel10-rheladm) version=10; role=rheladm; ip=192.168.150.111 ;;
    rhel10-node1)  version=10; role=node1; ip=192.168.150.112 ;;
    rhel10-node2)  version=10; role=node2; ip=192.168.150.113 ;;
    *) die "Unknown node: $name" ;;
  esac
}

read_env_file() {
  [[ -f $ENV_FILE && ! -L $ENV_FILE ]] || die "Missing regular environment file: $ENV_FILE"
  [[ $(stat -c '%a' -- "$ENV_FILE") == 600 && $(stat -c '%u' -- "$ENV_FILE") == "$(id -u)" ]] ||
    die "Environment file must be owned by this user with mode 0600: $ENV_FILE"
  username='' user_password='' root_password=''
  local line key value
  declare -A seen=()
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    [[ $line =~ ^(LAB_USERNAME|LAB_PASSWORD|LAB_ROOT_PASSWORD)=(.*)$ ]] || die "Unexpected environment file line (expected KEY=value)"
    key=${BASH_REMATCH[1]} value=${BASH_REMATCH[2]}
    [[ ! -v seen[$key] ]] || die "Duplicate environment key: $key"
    seen[$key]=1
    [[ $value != *$'\r'* ]] || die "Carriage returns are not permitted in environment values"
    case $key in
      LAB_USERNAME) username=$value ;;
      LAB_PASSWORD) user_password=$value ;;
      LAB_ROOT_PASSWORD) root_password=$value ;;
    esac
  done < "$ENV_FILE"
  [[ $username =~ ^[a-z_][a-z0-9_-]{0,31}$ && $username != root ]] || die 'Invalid username (lowercase Linux account, not root)'
  [[ ${#user_password} -ge 15 && ${#root_password} -ge 15 && $user_password != "$root_password" ]] ||
    die 'User/root passwords must differ and each have at least 15 characters'
}

hash_password() {
  # The plaintext is sent only to openssl stdin, never to argv, stdout or a Kickstart.
  printf '%s\n' "$1" | openssl passwd -6 -stdin
}

prepare_ks() {
  local name=$1 base tmp out user_hash root_hash line
  out="$VM_DIR/$name.ks"
  if ((dry_run)); then
    printf 'Would generate private Kickstart: %s (hashes only)\n' "$out"
    return
  fi
  mkdir -p -- "$VM_DIR"
  base=$(mktemp "$VM_DIR/.base-$name-XXXXXX")
  tmp=$(mktemp "$VM_DIR/.new-$name-XXXXXX")
  if ! xorriso -osirrox on -indev "$ISO" -extract "/ks/$name.ks" "$base" >/dev/null 2>&1; then
    rm -f -- "$base" "$tmp"
    die "Unable to read embedded Kickstart for $name from $ISO"
  fi
  user_hash=$(hash_password "$user_password")
  root_hash=$(hash_password "$root_password")
  local root_count=0 user_count=0 key_count=0
  while IFS= read -r line || [[ -n $line ]]; do
    case $line in
      'rootpw --lock')
        printf 'rootpw --iscrypted "%s"\n' "$root_hash" >> "$tmp"
        ((root_count+=1)) ;;
      'user --name=labadmin --groups=wheel --lock'|user\ --name=labadmin\ --groups=wheel\ --iscrypted\ --password=*)
        printf 'user --name=%s --groups=wheel --iscrypted --password="%s"\n' "$username" "$user_hash" >> "$tmp"
        ((user_count+=1)) ;;
      'sshkey --username=labadmin '* )
        printf '%s\n' "${line/--username=labadmin/--username=$username}" >> "$tmp"
        ((key_count+=1)) ;;
      *) printf '%s\n' "$line" >> "$tmp" ;;
    esac
  done < "$base"
  rm -f -- "$base"
  if ((root_count!=1 || user_count!=1 || key_count!=1)); then
    rm -f -- "$tmp"
    die "Unexpected Kickstart account structure for $name; refusing install"
  fi
  chmod 600 -- "$tmp"
  mv -f -- "$tmp" "$out"
  printf 'Prepared private Kickstart: %s (hashes only)\n' "$out"
}

ensure_kernels() {
  local version=$1 leaf target
  mkdir -p -- "$KERNEL_DIR/rhel$version"
  for leaf in vmlinuz initrd.img; do
    target="$KERNEL_DIR/rhel$version/$leaf"
    if [[ ! -s $target ]]; then
      xorriso -osirrox on -indev "$ISO" -extract "/rhel$version/$leaf" "$target" >/dev/null 2>&1 ||
        die "Cannot extract RHEL $version installer $leaf from $ISO"
      chmod 644 -- "$target"
    fi
  done
}

virt_command() {
  local name=$1 disk="$VM_DIR/$1.qcow2" ks="$VM_DIR/$1.ks" source memory cpus
  lookup_node "$name"
  memory=6144 cpus=2
  [[ $role != node2 ]] || { memory=8192; cpus=4; }
  if [[ $version == 10 ]]; then
    source="inst.repo=cdrom inst.stage2=hd:LABEL=$LABEL"
  else
    source="inst.repo=$HOST_REPO/rhel$version inst.stage2=$HOST_REPO/rhel$version"
  fi
  local os_release
  case $version in 8) os_release=8.10 ;; 9) os_release=9.8 ;; 10) os_release=10.2 ;; esac
  local args="ip=dhcp $source inst.ks=file:/$name.ks fips=1 lab.ip=$ip lab.hostname=$name.lab.test"
  cmd=("${VIRT_INSTALL[@]}" --connect qemu:///system --name "$name"
       --memory "$memory" --vcpus "$cpus"
       --disk "path=$disk,size=64,format=qcow2,bus=virtio"
       --disk "path=$ISO,device=cdrom,readonly=on"
       --network network=stig-lab,model=virtio --graphics spice
       --osinfo "rhel$os_release" --boot uefi
       --install "kernel=$KERNEL_DIR/rhel$version/vmlinuz,initrd=$KERNEL_DIR/rhel$version/initrd.img,kernel_args=$args"
       --initrd-inject "$ks" --noautoconsole --wait 0)
}

main() {
  dry_run=0
  if [[ ${1:-} == --dry-run ]]; then dry_run=1; shift; fi
  [[ $# -ge 2 ]] || { usage >&2; exit 2; }
  local action=$1; shift
  case $action in up|prepare|start|stop|status) ;; *) usage >&2; exit 2 ;; esac
  local -a selected=()
  if [[ $1 == all ]]; then
    [[ $# -eq 1 ]] || die "Use 'all' alone or specify individual nodes"
    selected=("${ALL_NODES[@]}")
  else
    local name
    declare -A seen=()
    for name in "$@"; do
      lookup_node "$name" # validate every name before any side effects
      [[ -v seen[$name] ]] && continue
      seen[$name]=1
      selected+=("$name")
    done
  fi
  if [[ $action == up || $action == prepare ]]; then
    [[ -r $ISO ]] || die "ISO not readable: $ISO"
    read_env_file
  fi
  if [[ $action == start || $action == stop ]]; then
    for name in "${selected[@]}"; do
      virsh -c qemu:///system dominfo "$name" >/dev/null 2>&1 || die "VM not defined: $name (use 'up' to create it)"
    done
  fi
  if [[ $action == up ]]; then
    if (( ! dry_run )); then
      mkdir -p -- "$VM_DIR"
      exec 9>"$VM_DIR/.lab-vms.lock"
      flock -n 9 || die 'Another lab bootstrap is running'
      [[ $(virsh -c qemu:///system net-info stig-lab | awk '/^Active:/{print $2}') == yes ]] || die 'stig-lab network is inactive'
      for name in "${selected[@]}"; do
        lookup_node "$name"
        if [[ $version != 10 ]] && ! curl -fsI --max-time 5 "$HOST_REPO/rhel$version/.treeinfo" >/dev/null; then
          die "RHEL $version repository is unavailable on the lab bridge"
        fi
      done
    fi
    for name in "${selected[@]}"; do
      if ! virsh -c qemu:///system dominfo "$name" >/dev/null 2>&1; then
        [[ ! -e $VM_DIR/$name.qcow2 && ! -L $VM_DIR/$name.qcow2 ]] ||
          die "Refusing to overwrite orphan disk: $VM_DIR/$name.qcow2"
      fi
    done
  fi
  local state disk
  for name in "${selected[@]}"; do
    case $action in
      status)
        state=$(virsh -c qemu:///system domstate "$name" 2>/dev/null || true)
        printf '%-20s %-12s %s\n' "$name" "${state:-absent}" "$VM_DIR/$name.qcow2" ;;
      prepare) prepare_ks "$name" ;;
      start|stop)
        state=$(virsh -c qemu:///system domstate "$name")
        if [[ $action == start && $state == 'shut off' ]]; then
          if ((dry_run)); then printf 'Would start %s\n' "$name"; else virsh -c qemu:///system start "$name"; fi
        elif [[ $action == stop && $state == running ]]; then
          if ((dry_run)); then printf 'Would shut down %s\n' "$name"; else virsh -c qemu:///system shutdown "$name"; fi
        else printf '%s already %s\n' "$name" "$state"; fi ;;
      up)
        if virsh -c qemu:///system dominfo "$name" >/dev/null 2>&1; then
          state=$(virsh -c qemu:///system domstate "$name")
          if [[ $state == 'shut off' ]]; then
            if ((dry_run)); then printf 'Would start %s\n' "$name"; else virsh -c qemu:///system start "$name"; fi
          else printf '%s already %s\n' "$name" "$state"; fi
          continue
        fi
        disk="$VM_DIR/$name.qcow2"
        [[ ! -e $disk && ! -L $disk ]] || die "Refusing to overwrite orphan disk: $disk"
        lookup_node "$name"
        if (( ! dry_run )); then
          prepare_ks "$name"
          ensure_kernels "$version"
        fi
        virt_command "$name"
        if ((dry_run)); then
          printf '%q ' "${cmd[@]}"; printf '\n'
        else
          "${cmd[@]}"
          virsh -c qemu:///system dominfo "$name" >/dev/null || die "virt-install did not define $name"
          printf 'Started installer for %s (%s). ISO Kickstart erases its NEW /dev/vda.\n' "$name" "$ip"
        fi ;;
    esac
  done
}
main "$@"
