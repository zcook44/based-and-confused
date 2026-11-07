#!/usr/bin/env python3
"""One RHEL 10.2 DVD boot menu for eight RHEL 8/9/10 STIG lab nodes."""
from dataclasses import dataclass
import argparse
import hmac
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile

from lab import extract, inspect_treeinfo, render_kickstart, xorriso

HOST_IP = '192.168.150.1'
REPO_URLS = {'8': f'http://{HOST_IP}:8089/repo/rhel8', '9': f'http://{HOST_IP}:8089/repo/rhel9'}
LABEL = 'RHEL-10-2-BaseOS-x86_64'


@dataclass(frozen=True)
class Node:
    version: str
    role: str
    ip: str

    @property
    def name(self):
        return f'rhel{self.version}-{self.role}'

    @property
    def fqdn(self):
        return self.name + '.lab.test'


NODES = (
    *(Node('9', role, f'192.168.150.{octet}') for role, octet in zip(('rheladm', 'node1', 'node2'), range(101, 104))),
    *(Node('10', role, f'192.168.150.{octet}') for role, octet in zip(('rheladm', 'node1', 'node2'), range(111, 114))),
    Node('8', 'node1', '192.168.150.121'), Node('8', 'node2', '192.168.150.122'),
)


def render_node(node: Node, key: str, gui_password_hash: str | None = None) -> str:
    if key.strip().startswith('ssh-ed25519 '):
        raise ValueError('Ed25519 public keys are not accepted by the FIPS/STIG guest SSH policy; use ECDSA or RSA')
    if node.role == 'node2' and not re.fullmatch(r'\$6\$[A-Za-z0-9./]{1,16}\$[A-Za-z0-9./]{86}', gui_password_hash or ''):
        raise ValueError('GUI password hash must be a SHA-512 crypt hash for a unique labadmin login')
    ks = render_kickstart(node.version, 'vda', key)
    if node.role == 'node2':
        ks = ks.replace('user --name=labadmin --groups=wheel --lock',
                        f'user --name=labadmin --groups=wheel --iscrypted --password="{gui_password_hash}"', 1)
    if node.role == 'rheladm':
        ks = ks.replace('shadow-utils\n%end', 'shadow-utils\nansible-core\n%end', 1)
    if node.role == 'node2':
        ks = ks.replace('@^minimal-environment', '@^graphical-server-environment', 1)
        ks = ks.replace('xccdf_org.ssgproject.content_profile_stig',
                        'xccdf_org.ssgproject.content_profile_stig_gui', 1)
        ks = ks.replace('reboot --eject', '''%post --nochroot --log=/tmp/lab-gui-target.log --erroronfail
systemctl --root=/mnt/sysroot set-default graphical.target
%end
reboot --eject''', 1)
    pre = f'''# Early network bootstrap uses DHCP (ip=dhcp) to fetch RHEL 8/9 packages.
# At the menu, press e to edit lab.ip and lab.hostname on the linux line.
%pre --log=/tmp/lab-network-pre.log --erroronfail
ip={node.ip}
hostname={node.fqdn}
for arg in $(cat /proc/cmdline); do
  case "$arg" in
    lab.ip=*) ip=${{arg#lab.ip=}} ;;
    lab.hostname=*) hostname=${{arg#lab.hostname=}} ;;
  esac
done
case "$ip" in 192.168.150.*) ;; *) echo 'IP must be 192.168.150.100-199' >&2; exit 1 ;; esac
octet=${{ip##*.}}
case "$octet" in ''|*[!0-9]*) exit 1 ;; esac
if [ "$octet" -lt 100 ] || [ "$octet" -gt 199 ]; then exit 1; fi
case "$hostname" in ''|*[!a-zA-Z0-9.-]*) echo 'Invalid hostname' >&2; exit 1 ;; esac
if [ "${{#hostname}}" -gt 63 ]; then exit 1; fi
printf 'network --bootproto=static --ip=%s --netmask=255.255.255.0 --gateway={HOST_IP} --nameserver={HOST_IP} --hostname=%s --device=link --activate --onboot=on\\n' "$ip" "$hostname" > /tmp/lab-network.ks
printf '%s %s\\n' "$ip" "$hostname" > /tmp/lab-identity
%end
%include /tmp/lab-network.ks'''
    ks = ks.replace('network --bootproto=dhcp --device=link --activate', pre)
    if node.version in REPO_URLS:
        ks = ks.replace('\ncdrom\n', f'\nurl --url={REPO_URLS[node.version]}\n')
    hosts = '\n'.join(f'{n.ip} {n.fqdn} {n.name}' for n in NODES if n != node)
    post = f'''%post --nochroot --log=/tmp/lab-hosts-post.log --erroronfail
cat >> /mnt/sysroot/etc/hosts <<'LAB_HOSTS'
# STIG test lab (default addresses; update peers if you change their boot-menu IPs)
{hosts}
LAB_HOSTS
cat /tmp/lab-identity >> /mnt/sysroot/etc/hosts
%end
'''
    ks = ks.replace('reboot --eject', post + 'reboot --eject')
    return finalize_kickstart(ks, 'labadmin', node.version)


def render_final_post(username: str, version: str) -> str:
    """Embed the shared repair, executed by the installed target's Bash/tools."""
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username) or username == 'root':
        raise ValueError('unsafe postinstall username')
    if version not in ('8', '9', '10'):
        raise ValueError('unsupported postinstall release')
    script = pathlib.Path(__file__).resolve().parent / 'scripts/lab-postinstall.sh'
    return ('# LAB_POSTINSTALL_BEGIN\n'
            '%post --log=/root/lab-postinstall.log --erroronfail --interpreter=/bin/bash\n'
            f'set -- {username} {version}\n' + script.read_text().rstrip() + '\n'
            '%end\n# LAB_POSTINSTALL_END\n')


def finalize_kickstart(source: str, username: str, version: str) -> str:
    """Refresh the embedded final repair after private/source transformations."""
    begin, end = '# LAB_POSTINSTALL_BEGIN\n', '# LAB_POSTINSTALL_END\n'
    counts = (source.count(begin), source.count(end))
    if counts not in ((0, 0), (1, 1)):
        raise ValueError('malformed or duplicate final postinstall markers')
    source, removed = re.subn(r'(?ms)^# LAB_POSTINSTALL_BEGIN\n.*?^# LAB_POSTINSTALL_END\n', '', source)
    if removed != counts[0]:
        raise ValueError('malformed final postinstall block')
    if len(re.findall(r'(?m)^reboot --eject$', source)) != 1 or not source.endswith('reboot --eject\n'):
        raise ValueError('source must end with exactly one reboot --eject directive')
    return re.sub(r'(?m)^reboot --eject$', lambda _: render_final_post(username, version) + 'reboot --eject', source)


def render_grub(firmware: str) -> str:
    if firmware not in ('bios', 'uefi'):
        raise ValueError('firmware must be bios or uefi')
    kernel_command = 'linux' if firmware == 'bios' else 'linuxefi'
    initrd_command = 'initrd' if firmware == 'bios' else 'initrdefi'
    entries = []
    for node in NODES:
        source = (f'inst.repo={REPO_URLS[node.version]} inst.stage2={REPO_URLS[node.version]}'
                  if node.version in REPO_URLS else f'inst.repo=cdrom inst.stage2=hd:LABEL={LABEL}')
        label = f' - RHEL {node.version}'
        if node.role == 'node2':
            label += ' GUI (STIG GUI)'
        entries.append(f'''menuentry '{node.name} ({node.ip}){label}' {{
  {kernel_command} /rhel{node.version}/vmlinuz ip=dhcp {source} inst.ks=cdrom:/ks/{node.name}.ks fips=1 lab.ip={node.ip} lab.hostname={node.fqdn} quiet
  {initrd_command} /rhel{node.version}/initrd.img
}}''')
    return ('set default="0"\nset timeout=-1\ninsmod gzio\ninsmod part_gpt\ninsmod iso9660\n'
            f"search --no-floppy --set=root -l '{LABEL}'\n\n" + '\n'.join(entries) + '\n')


def build_menu_iso(source: pathlib.Path, output: pathlib.Path,
                   kernels: dict[str, tuple[pathlib.Path, pathlib.Path]], key: str,
                   gui_password_hashes: dict[str, str]) -> None:
    source, output = source.resolve(), output.resolve()
    if source == output or output.exists():
        raise ValueError('never overwrite a vendor ISO or existing output')
    if set(kernels) != {'8', '9', '10'}:
        raise ValueError('all three vendor kernel/initrd pairs required')
    report = xorriso('-indev', source, '-report_el_torito', 'plain')
    if 'El Torito boot img :   1  BIOS' not in report or 'El Torito boot img :   2  UEFI' not in report:
        raise ValueError('source must have both BIOS and UEFI boot images')
    with tempfile.TemporaryDirectory(prefix='rhel-menu-') as d:
        d = pathlib.Path(d)
        inspect_treeinfo(extract(source, '/.treeinfo', d / 'treeinfo').decode(), '10')
        # Only the matching 10.2 full DVD can provide local installer packages.
        info = xorriso('-indev', source, '-pvd_info')
        if LABEL not in info:
            raise ValueError('base DVD volume label does not match RHEL 10.2')
        mappings = []
        for firmware, path in (('bios', '/boot/grub2/grub.cfg'), ('uefi', '/EFI/BOOT/grub.cfg')):
            extract(source, path, d / (firmware + '.orig'))  # fail closed on unexpected layout
            cfg = d / (firmware + '.cfg')
            cfg.write_text(render_grub(firmware))
            mappings += ['-map', cfg, path]
        for v in ('8', '9', '10'):
            for i, leaf in enumerate(('vmlinuz', 'initrd.img')):
                file = pathlib.Path(kernels[v][i])
                if not file.is_file() or file.stat().st_size == 0:
                    raise ValueError(f'missing installer {v} {leaf}')
                mappings += ['-map', file, f'/rhel{v}/{leaf}']
        for node in NODES:
            path = d / (node.name + '.ks')
            path.write_text(render_node(node, key, gui_password_hashes.get(node.name)))
            mappings += ['-map', path, '/ks/' + path.name]
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + '.partial')
        if partial.exists():
            raise ValueError(f'stale partial output {partial}')
        try:
            xorriso('-indev', source, '-outdev', partial, '-overwrite', 'on',
                    *mappings, '-boot_image', 'any', 'replay', '-commit')
            for firmware, path in (('bios', '/boot/grub2/grub.cfg'), ('uefi', '/EFI/BOOT/grub.cfg')):
                if extract(partial, path, d / (firmware + '.check')).decode() != render_grub(firmware):
                    raise ValueError(f'{firmware} boot menu mismatch')
            for node in NODES:
                if extract(partial, '/ks/' + node.name + '.ks', d / (node.name + '.check')).decode() != render_node(node, key, gui_password_hashes.get(node.name)):
                    raise ValueError(f'embedded {node.name} Kickstart mismatch')
            if 'El Torito boot img :   2  UEFI' not in xorriso('-indev', partial, '-report_el_torito', 'plain'):
                raise ValueError('UEFI boot metadata lost')
            partial.rename(output)
        finally:
            partial.unlink(missing_ok=True)


def load_gui_credentials(path: pathlib.Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError('GUI credentials must be a regular, non-symlink file with mode 0600')
    data = json.loads(path.read_text())
    expected = {node.name for node in NODES if node.role == 'node2'}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError('GUI credentials must contain exactly the two node2 identities')
    hashes = {}
    for name in sorted(expected):
        item = data[name]
        if not isinstance(item, dict) or set(item) != {'password', 'hash'}:
            raise ValueError(f'GUI credential entry is malformed: {name}')
        password, digest = item['password'], item['hash']
        if not isinstance(password, str) or len(password) < 24 or '\n' in password:
            raise ValueError(f'GUI password must be a strong, single-line secret: {name}')
        if not isinstance(digest, str) or not re.fullmatch(r'\$6\$[A-Za-z0-9./]{1,16}\$[A-Za-z0-9./]{86}', digest):
            raise ValueError(f'GUI password hash must be SHA-512 crypt: {name}')
        salt = digest.split('$')[2]
        computed = subprocess.run(['openssl', 'passwd', '-6', '-salt', salt, '-stdin'],
                                  input=password + '\n', text=True, capture_output=True, check=True).stdout.strip()
        if not hmac.compare_digest(computed, digest):
            raise ValueError(f'GUI password hash does not match password: {name}')
        hashes[name] = digest
    return hashes


def main():
    if sys.argv[1:2] == ['postinstall']:
        p = argparse.ArgumentParser(description='Refresh a source Kickstart final repair; no ISO or VM actions')
        p.add_argument('--source', required=True, type=pathlib.Path)
        p.add_argument('--out', required=True, type=pathlib.Path)
        p.add_argument('--username', required=True)
        p.add_argument('--version', required=True)
        a = p.parse_args(sys.argv[2:])
        try:
            if a.source.is_symlink() or a.out.is_symlink() or a.source.resolve() == a.out.resolve():
                raise ValueError('source/output must be distinct non-symlink paths')
            data = finalize_kickstart(a.source.read_text(), a.username, a.version)
            fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
        except (ValueError, OSError) as e:
            p.exit(1, f'error: {e}\n')
        return
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', required=True, type=pathlib.Path, help='official RHEL 10.2 full DVD')
    p.add_argument('--rhel8', required=True, type=pathlib.Path, help='official RHEL 8.10 full DVD')
    p.add_argument('--rhel9', required=True, type=pathlib.Path, help='official RHEL 9.8 full DVD')
    p.add_argument('--out', required=True, type=pathlib.Path)
    p.add_argument('--ssh-key', required=True, type=pathlib.Path)
    p.add_argument('--gui-credentials', required=True, type=pathlib.Path,
                   help='0600 JSON with three GUI labadmin passwords and SHA-512 crypt hashes')
    a = p.parse_args()
    try:
        gui_password_hashes = load_gui_credentials(a.gui_credentials)
        with tempfile.TemporaryDirectory(prefix='rhel-kernels-') as d:
            d = pathlib.Path(d)
            kernels = {}
            for version, source in (('8', a.rhel8), ('9', a.rhel9), ('10', a.source)):
                kernels[version] = tuple(d / f'{version}-{name}' for name in ('vmlinuz', 'initrd.img'))
                for name, dest in zip(('vmlinuz', 'initrd.img'), kernels[version]):
                    extract(source, f'/images/pxeboot/{name}', dest)
            build_menu_iso(a.source, a.out, kernels, a.ssh_key.read_text(), gui_password_hashes)
        print(a.out.resolve())
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        p.exit(1, f'error: {e}\n')


if __name__ == '__main__':
    main()
