#!/usr/bin/env bash
# Offline, no-overwrite personalization. The clone produced by quarterly.cli is never modified.
set -euo pipefail
exec /usr/bin/python3 - "$0" "$@" <<'PY'
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

CONNECTION = '/etc/NetworkManager/system-connections/quarterly-static.nmconnection'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=600).stdout


def safe_dir(path):
    """Reject every symlink component, including parents of the owned root."""
    require(path.is_absolute() and '..' not in path.parts, 'unsafe directory path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def detached(disk, output):
    prefix = ['virsh', '-c', 'qemu:///system']
    domains = command(prefix + ['list', '--all', '--name']).splitlines()
    for name in domains:
        require(name.strip() == name and name, 'invalid domain list')
        for suffix in ([], ['--inactive']):
            # Inactive XML can differ from a running domain live XML.
            xml = ET.fromstring(command(prefix + ['dumpxml', name, *suffix]))
            for source in xml.findall('.//devices/disk//source'):
                filename = source.get('file')
                if filename:
                    candidate = Path(filename)
                    if (filename in (str(disk), str(output))
                            or (candidate.exists() and (os.path.samefile(candidate, disk)
                                or (output.exists() and os.path.samefile(candidate, output))))):
                        raise ValueError('disk attached to domain: ' + name)


def image_ok(path):
    info = json.loads(command(['qemu-img', 'info', '--output=json', str(path)]))
    # qemu-img ImageInfo stores external qcow2 payloads under format-specific.data.
    # https://www.qemu.org/docs/master/interop/qemu-storage-daemon-qmp-ref.html#object-QSD-block-core.ImageInfoSpecificQCow2
    specific = info.get('format-specific') or {}
    data = specific.get('data') or {}
    require(info.get('format') == 'qcow2'
            and not any(key in info for key in ('backing-filename', 'full-backing-filename',
                                                 'backing-filename-format', 'backing-image',
                                                 'data-file', 'children'))
            and not any(key in data for key in ('backing-filename', 'data-file'))
            and not data.get('has-backing-file')
            and not info.get('snapshots'),
            'image is not standalone snapshot-free qcow2')


def inputs(args):
    lab = json.loads(Path(args.manifest).read_text())
    require(lab.get('schema') == 1, 'unknown manifest schema')
    nodes = [n for n in lab['nodes'] if n['name'] == args.node]
    require(len(nodes) == 1, 'node must match exactly one manifest entry')
    node = nodes[0]
    root = Path(lab['disk_root'])
    disk = Path(node['disk'])
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', args.node) is not None, 'invalid node name')
    require(disk == root / 'clones' / (args.node + '.qcow2')
            and node['base_disk'] != str(disk)
            and node.get('image_kind') == 'clean', 'not an owned clone disk')
    forbidden = {r[k] for r in lab['releases'].values() for k in
                 ('base_disk', 'build_disk', 'gui_base_disk', 'gui_build_disk',
                  'controller_base_disk', 'controller_build_disk') if k in r}
    require(str(disk) not in forbidden, 'base/build disk cannot be personalized')
    net = lab['network']
    subnet = ipaddress.ip_network(net['subnet'], strict=True)
    addr = ipaddress.ip_address(node['address'])
    gateway = ipaddress.ip_address(net['gateway'])
    require(isinstance(addr, ipaddress.IPv4Address) and addr in subnet and gateway in subnet
            and addr != gateway and net['prefix'] == subnet.prefixlen,
            'invalid static IPv4 network')
    fqdn = node['fqdn']
    require(fqdn == args.node + '.' + net['domain']
            and re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', fqdn) is not None,
            'invalid FQDN')
    output = root / 'clones' / (args.node + '.personalized.qcow2')
    directory = safe_dir(root / 'clones')
    try:
        st = os.fstat(directory)
        require(st.st_uid == os.getuid() and not st.st_mode & 0o077,
                'clones directory must be owner-only')
        fd = os.open(disk.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            source = os.fstat(fd)
            require(stat.S_ISREG(source.st_mode) and source.st_uid == os.getuid()
                    and source.st_nlink == 1 and not source.st_mode & 0o077,
                    'clone must be owner-only regular unlinked image')
        finally:
            os.close(fd)
        require(not output.exists() and not output.is_symlink(), 'personalized output exists')
        for tool in ('virsh', 'qemu-img', 'virt-customize', 'virt-cat', 'guestfish'):
            require(shutil.which(tool) is not None, 'missing offline tool: ' + tool)
        detached(disk, output)
        image_ok(disk)
    finally:
        os.close(directory)
    content = (f'[connection]\nid=quarterly-static\n'
               f'uuid={uuid.uuid5(uuid.NAMESPACE_DNS, fqdn)}\ntype=ethernet\n'
               f'autoconnect=true\n\n[ipv4]\nmethod=manual\n'
               f'address1={addr}/{subnet.prefixlen},{gateway}\n'
               f'dns={gateway};\ndns-search={net["domain"]};\n'
               f'never-default=false\n\n[ipv6]\nmethod=disabled\n')
    return lab, node, disk, output, content


def execute(args):
    lab, node, disk, output, connection = inputs(args)
    if args.check:
        print('Ready (read-only): ' + str(disk) + ' -> ' + str(output))
        return
    require(args.confirm == node['name'], 'exact --confirm NAME required')
    directory = safe_dir(output.parent)
    try:
        # Private workspace on same filesystem; link(2) publishes without ever replacing.
        with tempfile.TemporaryDirectory(prefix='.personalize-', dir=output.parent) as work:
            os.chmod(work, 0o700)
            work = Path(work)
            candidate = work / 'image.qcow2'
            # Recheck attachments at the point of source access, not only --check.
            detached(disk, output)
            source_fd = os.open(disk.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                source_stat = os.fstat(source_fd)
                require(stat.S_ISREG(source_stat.st_mode) and source_stat.st_nlink == 1,
                        'source changed')
                with os.fdopen(source_fd, 'rb', closefd=False) as src, open(candidate, 'xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                copied_source = os.fstat(source_fd)
                require(copied_source.st_size == candidate.stat().st_size
                        and copied_source.st_mtime_ns == source_stat.st_mtime_ns
                        and copied_source.st_ctime_ns == source_stat.st_ctime_ns
                        and os.stat(disk.name, dir_fd=directory, follow_symlinks=False).st_ino == source_stat.st_ino,
                        'source changed while copying')
            finally:
                os.close(source_fd)
            candidate.chmod(0o600)
            config = work / 'quarterly-static.nmconnection'
            config.write_text(connection)
            config.chmod(0o600)
            # This runs against the private copy only. Remove inherited static profiles
            # before upload so the original install IP cannot win NM autoconnect.
            cleanup = ('set -e; find /etc/NetworkManager/system-connections -maxdepth 1 -type f '
                       '-name "*.nmconnection" -delete; '
                       'if test -d /etc/sysconfig/network-scripts; then '
                       'find /etc/sysconfig/network-scripts -maxdepth 1 -type f '
                       '-name "ifcfg-*" ! -name ifcfg-lo -delete; fi; '
                       'find /home /root -type f -path "*/.ssh/id_*" -delete; '
                       'rm -f /etc/ssh/ssh_host_*key* /var/lib/dbus/machine-id; '
                       ': > /etc/machine-id')
            # Other manifest identities on this same sealed base can be stale in /etc/hosts.
            old = [n for n in lab['nodes'] if n.get('base_disk') == node['base_disk']]
            for sibling in old:
                for token in (sibling['fqdn'], sibling['address']):
                    require(re.fullmatch(r'[a-zA-Z0-9.:-]+', token) is not None,
                            'unsafe inherited identity')
                    cleanup += '; sed -i ' + shlex.quote(r'/' + token.replace('.', r'\.') + r'/d') + ' /etc/hosts'
            command(['virt-customize', '--no-network', '--format', 'qcow2', '-a', str(candidate), '--run-command', cleanup,
                     '--upload', str(config) + ':' + CONNECTION, '--hostname', node['fqdn'],
                     '--run-command', 'set -e; chmod 600 ' + CONNECTION + '; chown root:root ' + CONNECTION,
                     '--selinux-relabel'])
            image_ok(candidate)
            hostname = command(['virt-cat', '--format=qcow2', '-a', str(candidate), '/etc/hostname'])
            observed = command(['virt-cat', '--format=qcow2', '-a', str(candidate), CONNECTION])
            require(hostname == node['fqdn'] + '\n' and observed == connection,
                    'offline hostname or static NM verification failed')
            # -i inspects and mounts guest filesystems; failed inspection aborts readback.
            # https://libguestfs.org/guestfish.1.html#mount-disks-automatically
            guest = ['guestfish', '--ro', '--format=qcow2', '-a', str(candidate), '-i']
            metadata = command(guest + ['statns', CONNECTION])
            fields = dict(re.findall(r'(?m)^\s*(st_mode|st_uid|st_gid):\s*(\d+)\s*$', metadata))
            require(set(fields) == {'st_mode', 'st_uid', 'st_gid'}
                    and stat.S_ISREG(int(fields['st_mode']))
                    and stat.S_IMODE(int(fields['st_mode'])) == 0o600
                    and fields['st_uid'] == '0' and fields['st_gid'] == '0',
                    'guest NM file must be root-owned mode 0600')
            ssh_files = command(guest + ['ls', '/etc/ssh']).splitlines()
            require(not any(re.fullmatch(r'ssh_host_.*key(?:\.pub)?', f) for f in ssh_files),
                    'inherited SSH host keys remain')
            require(command(guest + ['exists', '/.autorelabel']).strip() == 'false',
                    'SELinux relabel was deferred; refusing unlabelled network configuration')
            hosts = command(['virt-cat', '--format=qcow2', '-a', str(candidate), '/etc/hosts'])
            require(not any(token in hosts for sibling in old
                            for token in (sibling['fqdn'], sibling['address'])),
                    'inherited network identity remains in hosts')
            detached(disk, output)
            current_source = os.stat(disk.name, dir_fd=directory, follow_symlinks=False)
            require(current_source.st_ino == source_stat.st_ino
                    and current_source.st_size == source_stat.st_size
                    and current_source.st_mtime_ns == source_stat.st_mtime_ns
                    and current_source.st_ctime_ns == source_stat.st_ctime_ns,
                    'source replaced before publication')
            # hard-link is atomic and fails if output exists (no overwrite, even a symlink).
            os.link(candidate, output.name, dst_dir_fd=directory, follow_symlinks=False)
            print('Personalized offline image: ' + str(output))
            print('Define a domain with this image explicitly; original clone remains unchanged.')
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description='Offline personalize a quarterly cloned qcow2')
    parser.add_argument('--manifest', default=str(Path(sys.argv[1]).resolve().parent.parent / 'quarterly/lab.json'))
    parser.add_argument('--node', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm')
    args = parser.parse_args(sys.argv[2:])
    try:
        execute(args)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.SubprocessError, ET.ParseError) as exc:
        print('quarterly-personalize: ' + str(exc), file=sys.stderr)
        return 2
    return 0


sys.exit(main())
PY
