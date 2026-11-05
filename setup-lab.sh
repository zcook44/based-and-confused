#!/usr/bin/env bash
# Fresh-host setup for the eight-node Kickstart lab. Never creates or deletes VMs.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
media_dir=/scratch/iso
repo_dir=''
checksums="$ROOT/vendor-checksums.sha256"
output=''
credentials=/scratch/vms/menu-gui-credentials-team.json
ssh_key=''
generate_credentials=0

usage() {
  cat <<'HELP'
Usage: setup-lab.sh [options] <check|prepare|build-iso|verify>
  check      Read-only: dependencies, exact vendor DVD checksums/versions, network conflict.
  prepare    Define/start the lab network if needed; stage RHEL 8/9 trees; serve via Docker.
  build-iso  Build a new eight-entry ISO (never overwrites); requires --ssh-key.
  verify     Check served repositories, ISO SHA-256, eight entries and boot metadata.
Options:
  --media-dir DIR       Full vendor DVDs and generated ISO (default /scratch/iso)
  --repo-dir DIR        Extracted DVD trees (default MEDIA_DIR/repo)
  --checksums FILE      Vendor DVD SHA-256 file (default ./vendor-checksums.sha256)
  --out FILE            Generated ISO (default MEDIA_DIR/rhel-stig-lab-menu-team.iso)
  --ssh-key FILE        Authorized ECDSA/RSA PUBLIC key (build-iso only)
  --credentials FILE    Private GUI credential JSON (default /scratch/vms/menu-gui-credentials-team.json)
  --generate-credentials  Create new GUI credentials during build; refuses existing file
No phase downloads vendor media, changes host firewall, creates VMs, or uploads artifacts.
HELP
}
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"; }

while (($#)); do
  case $1 in
    --media-dir|--repo-dir|--checksums|--out|--ssh-key|--credentials)
      (($# >= 2)) || die "Missing value for $1"
      case $1 in
        --media-dir) media_dir=$2 ;;
        --repo-dir) repo_dir=$2 ;;
        --checksums) checksums=$2 ;;
        --out) output=$2 ;;
        --ssh-key) ssh_key=$2 ;;
        --credentials) credentials=$2 ;;
      esac
      shift 2 ;;
    --generate-credentials) generate_credentials=1; shift ;;
    -h|--help) usage; exit 0 ;;
    check|prepare|build-iso|verify)
      phase=$1; shift
      (($# == 0)) || die 'Put the phase last and specify it only once'
      break ;;
    *) usage >&2; die "Unknown option or phase: $1" ;;
  esac
done
[[ -n ${phase:-} ]] || { usage >&2; exit 2; }
[[ -n $repo_dir ]] || repo_dir="$media_dir/repo"
[[ -n $output ]] || output="$media_dir/rhel-stig-lab-menu-team.iso"
# Normalize paths before changing directory or passing them to Compose.
need realpath
media_dir=$(realpath -m -- "$media_dir")
repo_dir=$(realpath -m -- "$repo_dir")
checksums=$(realpath -m -- "$checksums")
output=$(realpath -m -- "$output")
credentials=$(realpath -m -- "$credentials")
[[ -z $ssh_key ]] || ssh_key=$(realpath -m -- "$ssh_key")

validate_tree() {
  local target=$1 version=$2
  [[ -f $target/.treeinfo && -s $target/images/install.img &&
     -s $target/BaseOS/repodata/repomd.xml &&
     -s $target/AppStream/repodata/repomd.xml ]] || die "Incomplete repository tree: $target"
  python3 - "$target/.treeinfo" "$version" <<'PY'
import configparser, pathlib, sys
c = configparser.ConfigParser(interpolation=None)
c.read(sys.argv[1])
if c['general'].get('version') != sys.argv[2]:
    raise SystemExit(f'Unexpected repository version: {sys.argv[1]}')
PY
}

check_media() {
  local version
  need python3; need openssl; need xorriso; need sha256sum
  [[ -d $media_dir && -r $checksums ]] || die 'Missing media directory or vendor checksum file'
  for version in 8.10 9.8 10.2; do
    [[ -s $media_dir/rhel-$version-x86_64-dvd.iso ]] || die "Missing full RHEL $version DVD in $media_dir"
  done
  (cd "$media_dir" && sha256sum --check --status "$checksums") || die 'Vendor DVD SHA-256 mismatch; compare with Red Hat downloads'
  python3 - "$media_dir" "$ROOT" <<'PY'
import pathlib, sys
sys.path.insert(0, sys.argv[2])
from build_kickstart_iso import check_dvd_versions
p = pathlib.Path(sys.argv[1])
check_dvd_versions(*(p / f'rhel-{v}-x86_64-dvd.iso' for v in ('8.10', '9.8', '10.2')))
PY
  printf 'Vendor DVD SHA-256, minor releases and full installation trees: OK\n'
  df -h "$media_dir"
}

check_space() {
  # Account for sparse lab disks separately; this covers only extraction and ISO output.
  local scope=$1 repo_bytes=0 iso_bytes=0 root_repo root_out available
  need df; need awk; need stat
  if [[ $scope == check || $scope == prepare ]]; then
    [[ -e $repo_dir/rhel8 ]] || ((repo_bytes+=$(stat -c %s "$media_dir/rhel-8.10-x86_64-dvd.iso")))
    [[ -e $repo_dir/rhel9 ]] || ((repo_bytes+=$(stat -c %s "$media_dir/rhel-9.8-x86_64-dvd.iso")))
  fi
  if [[ $scope == check || $scope == build-iso ]]; then
    [[ -e $output ]] || ((iso_bytes+=$(stat -c %s "$media_dir/rhel-10.2-x86_64-dvd.iso")+1073741824))
  fi
  root_repo=$repo_dir; while [[ ! -d $root_repo ]]; do root_repo=$(dirname -- "$root_repo"); done
  root_out=$(dirname -- "$output"); while [[ ! -d $root_out ]]; do root_out=$(dirname -- "$root_out"); done
  if [[ $(stat -c %d "$root_repo") == "$(stat -c %d "$root_out")" ]]; then
    available=$(df -PB1 "$root_repo" | awk 'NR==2 {print $4}')
    ((available >= repo_bytes + iso_bytes)) || die 'Insufficient free space for missing repository trees and/or ISO'
  else
    available=$(df -PB1 "$root_repo" | awk 'NR==2 {print $4}')
    ((available >= repo_bytes)) || die 'Insufficient free space for repository trees'
    available=$(df -PB1 "$root_out" | awk 'NR==2 {print $4}')
    ((available >= iso_bytes)) || die 'Insufficient free space for the output ISO'
  fi
  printf 'Available space checked for this phase (ISO reserve includes 1 GiB overhead)\n'
}

network_status() {
  need virsh; need ip
  if virsh -c qemu:///system net-info stig-lab >/dev/null 2>&1; then
    virsh -c qemu:///system net-dumpxml stig-lab | python3 -c '
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
bridge, ip, dhcp = root.find("bridge"), root.find("ip"), root.find("ip/dhcp/range")
if (bridge is None or bridge.get("name") != "virbr-stig" or ip is None
    or ip.get("address") != "192.168.150.1" or ip.get("netmask") != "255.255.255.0"
    or dhcp is None or dhcp.get("start") != "192.168.150.10" or dhcp.get("end") != "192.168.150.99"):
    raise SystemExit("Existing stig-lab network differs from this project; inspect it manually")
' || die 'Lab network conflict; refusing to alter it'
    printf 'Existing stig-lab network matches expected bridge, gateway and DHCP range\n'
  else
    [[ -z $(ip -4 route show exact 192.168.150.0/24) ]] || die '192.168.150.0/24 already routed by another network'
    printf 'Lab network absent; subnet has no exact conflicting route\n'
  fi
}

check_host() {
  need docker; need curl
  docker compose version >/dev/null || die 'Docker Compose plugin unavailable'
  docker info >/dev/null 2>&1 || die 'Cannot use Docker daemon'
  virsh -c qemu:///system list --all >/dev/null || die 'Cannot access system libvirt'
  network_status
}

preflight_repo() {
  local major target
  [[ ! -L $repo_dir ]] || die "Repository root is a symlink: $repo_dir"
  for major in 8 9; do
    target="$repo_dir/rhel$major"
    [[ ! -e $target.partial && ! -L $target.partial ]] || die "Stale partial tree: $target.partial"
    if [[ -e $target || -L $target ]]; then
      [[ -d $target && ! -L $target ]] || die "Not a regular repository directory: $target"
      case $major in 8) validate_tree "$target" 8.10 ;; 9) validate_tree "$target" 9.8 ;; esac
    fi
  done
}

prepare_lab() {
  check_media; check_space prepare; check_host; preflight_repo
  if ! virsh -c qemu:///system net-info stig-lab >/dev/null 2>&1; then
    need sudo
    sudo virsh -c qemu:///system net-define "$ROOT/stig-lab-network.xml"
  fi
  if ! virsh -c qemu:///system net-info stig-lab | grep -Eq '^Active:[[:space:]]+yes'; then
    need sudo
    sudo virsh -c qemu:///system net-start stig-lab
  fi
  if ! virsh -c qemu:///system net-info stig-lab | grep -Eq '^Autostart:[[:space:]]+yes'; then
    need sudo
    sudo virsh -c qemu:///system net-autostart stig-lab >/dev/null
  fi
  network_status
  mkdir -p -- "$repo_dir" "$ROOT/data/menu-ks"
  local major target version
  for major in 8 9; do
    case $major in 8) version=8.10 ;; 9) version=9.8 ;; esac
    target="$repo_dir/rhel$major"
    if [[ -d $target ]]; then
      printf 'Keeping verified repository tree: %s\n' "$target"
      continue
    fi
    mkdir -- "$target.partial"
    printf 'Extracting RHEL %s to %s (interruptions leave staging for inspection)\n' "$version" "$target.partial"
    xorriso -osirrox on -indev "$media_dir/rhel-$version-x86_64-dvd.iso" -extract / "$target.partial" >/dev/null
    validate_tree "$target.partial" "$version"
    mv -- "$target.partial" "$target"
  done
  LAB_REPO_DIR="$repo_dir" docker compose -p rhel-stig-lab-repo -f "$ROOT/compose.repo.yaml" up -d --build
  check_http
  printf 'Lab network and repositories ready. Review host bridge-scoped firewall policy before guest installs.\n'
}

check_http() {
  local major path
  need curl
  for major in 8 9; do
    for path in .treeinfo images/install.img BaseOS/repodata/repomd.xml AppStream/repodata/repomd.xml; do
      curl -fsSI --retry 6 --retry-delay 2 --retry-connrefused --max-time 10 \
        "http://192.168.150.1:8089/repo/rhel$major/$path" >/dev/null || die "Repository HTTP unavailable: RHEL$major/$path"
    done
  done
  printf 'RHEL 8/9 repository metadata and installer stage2 respond on the lab gateway\n'
}

build_iso() {
  [[ -n $ssh_key ]] || die 'build-iso requires --ssh-key with the team PUBLIC key'
  [[ ! -e $output && ! -L $output ]] || die "Refusing existing ISO: $output"
  check_media; check_space build-iso
  local -a command=(python3 "$ROOT/build_kickstart_iso.py"
    --rhel8 "$media_dir/rhel-8.10-x86_64-dvd.iso"
    --rhel9 "$media_dir/rhel-9.8-x86_64-dvd.iso"
    --rhel10 "$media_dir/rhel-10.2-x86_64-dvd.iso"
    --ssh-key "$ssh_key" --gui-credentials "$credentials" --out "$output")
  ((generate_credentials == 0)) || command+=(--generate-credentials)
  "${command[@]}"
}

verify_iso() {
  [[ -s $output && -s $output.sha256 ]] || die "Missing ISO/checksum: $output"
  (cd "$(dirname -- "$output")" && sha256sum --check --status "$(basename -- "$output").sha256") || die 'Output ISO checksum mismatch'
  python3 - "$output" "$ROOT" <<'PY'
import pathlib, sys, tempfile
sys.path.insert(0, sys.argv[2])
from lab import extract, xorriso
from menu_lab import NODES
iso = pathlib.Path(sys.argv[1])
report = xorriso('-indev', iso, '-report_el_torito', 'plain')
for firmware in ('BIOS', 'UEFI'):
    if f'El Torito boot img :   {1 if firmware == "BIOS" else 2}  {firmware}' not in report:
        raise SystemExit(f'Missing {firmware} boot metadata')
with tempfile.TemporaryDirectory(prefix='lab-iso-check-') as d:
    d = pathlib.Path(d)
    for node in NODES:
        if not extract(iso, f'/ks/{node.name}.ks', d / f'{node.name}.ks'):
            raise SystemExit(f'Empty Kickstart: {node.name}')
    for firmware, path in (('bios', '/boot/grub2/grub.cfg'), ('uefi', '/EFI/BOOT/grub.cfg')):
        menu = extract(iso, path, d / f'{firmware}.cfg').decode()
        if menu.count('menuentry ') != len(NODES):
            raise SystemExit(f'Expected eight {firmware} menu entries')
    for major in ('8', '9', '10'):
        for leaf in ('vmlinuz', 'initrd.img'):
            if not extract(iso, f'/rhel{major}/{leaf}', d / f'{major}-{leaf}'):
                raise SystemExit(f'Missing RHEL {major} {leaf}')
print('ISO SHA-256, eight Kickstarts, three kernel pairs, BIOS/UEFI menus and boot metadata: OK')
PY
}

case $phase in
  check) check_media; check_space check; check_host; preflight_repo ;;
  prepare) prepare_lab ;;
  build-iso) build_iso ;;
  verify) check_http; verify_iso ;;
esac
