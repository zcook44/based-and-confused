"""Quarterly VM planning and explicitly confirmed, no-overwrite disk cloning."""

import hashlib
from contextlib import ExitStack
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
import xml.etree.ElementTree as ET

MANIFEST = Path(__file__).with_name('lab.json')

BASE_KEYS = {'gui': 'gui_base_disk', 'controller': 'controller_base_disk', 'node': 'base_disk'}
BUILD_KEYS = {'gui': 'gui_build_disk', 'controller': 'controller_build_disk', 'node': 'build_disk'}


def load_manifest(path=MANIFEST):
    """Load and validate the canonical isolated lab identity and path map."""
    return validate_manifest(json.loads(Path(path).read_text()))


def validate_manifest(lab):
    """Validate already-read manifest bytes without reopening a path."""
    if lab['schema'] != 1:
        raise ValueError('unknown lab manifest schema')
    net = lab['network']
    subnet = ipaddress.ip_network(net['subnet'], strict=True)
    if str(ipaddress.ip_address(net['gateway'])) not in {str(ip) for ip in subnet.hosts()}:
        raise ValueError('gateway is outside network')
    if len({n['name'] for n in lab['nodes']}) != len(lab['nodes']):
        raise ValueError('duplicate node name')
    if len({n['address'] for n in lab['nodes']}) != len(lab['nodes']):
        raise ValueError('duplicate node address')
    root = Path(lab['disk_root'])
    builds, bases, clones = set(), set(), set()
    for node in lab['nodes']:
        release = lab['releases'][node['release']]
        if node['fqdn'] != node['name'] + '.' + net['domain']:
            raise ValueError('invalid FQDN')
        if ipaddress.ip_address(node['address']) not in subnet or node['address'] == net['gateway']:
            raise ValueError('node address is outside network')
        base_key = BASE_KEYS.get(node['role'])
        if base_key is None:
            raise ValueError('unknown node role')
        build = release[BUILD_KEYS[node['role']]]
        expected_base = str(root / 'bases' / (node['name'] + '-baseline.qcow2'))
        if (node['image_kind'] != 'clean' or node['base_disk'] != expected_base
                or release[base_key] != expected_base):
            raise ValueError('node must use its own clean baseline disk')
        if build != str(root / 'build' / (node['name'] + '.qcow2')):
            raise ValueError('node must use its own build disk')
        expected_clone = str(root / 'clones' / (node['name'] + '.qcow2'))
        if node['disk'] != expected_clone or expected_clone in clones:
            raise ValueError('node must use its own clone disk')
        if build in builds or expected_base in bases:
            raise ValueError('duplicate build or baseline disk across nodes')
        builds.add(build)
        bases.add(expected_base)
        clones.add(expected_clone)
        for disk in (node['disk'], node['base_disk']):
            target = Path(disk)
            if not target.is_absolute() or target != Path(str(target.resolve(strict=False))) or not target.is_relative_to(root) or target.suffix != '.qcow2':
                raise ValueError('disk path outside owned root')
        if node['disk'] == node['base_disk']:
            raise ValueError('clone cannot overwrite base')
    return lab


def render_network_xml(lab):
    """Return libvirt NAT network definition; caller reviews before applying."""
    net = lab['network']
    root = ET.Element('network')
    ET.SubElement(root, 'name').text = net['name']
    ET.SubElement(root, 'forward', mode='nat')
    ET.SubElement(root, 'bridge', name=net['bridge'])
    ET.SubElement(root, 'domain', name=net['domain'])
    ET.SubElement(root, 'ip', address=net['gateway'], netmask=str(ipaddress.ip_network(net['subnet']).netmask))
    return ET.tostring(root, encoding='unicode')


class PreflightError(RuntimeError):
    """Inspection was incomplete; never infer that absent output means safe."""


def _read_command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20).stdout


def preflight(lab, *, run=_read_command):
    """Read host routes, every libvirt network/domain and owned clone paths.

    Return collision descriptions; inspection failures raise PreflightError. No writes.
    Base images may exist, but a domain using one is a collision.
    """
    net = lab['network']
    intended = ipaddress.ip_network(net['subnet'])
    problems = []
    try:
        routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
        if not isinstance(routes, list):
            raise ValueError('route response is not a list')
        for route in routes:
            dst = route.get('dst', 'default')
            if dst != 'default' and ipaddress.ip_network(dst, strict=False).overlaps(intended):
                problems.append('route overlap: ' + dst)
        prefix = ['virsh', '-c', 'qemu:///system']
        networks = run(prefix + ['net-list', '--all', '--name']).splitlines()
        for name in networks:
            if not name.strip():
                continue
            definition = ET.fromstring(run(prefix + ['net-dumpxml', name]))
            if name == net['name']:
                problems.append('network name collision: ' + name)
            bridge = definition.find('bridge')
            if bridge is not None and bridge.get('name') == net['bridge']:
                problems.append('bridge collision: ' + name)
            for ip in definition.findall('ip'):
                if 'address' not in ip.attrib or 'netmask' not in ip.attrib:
                    raise ValueError('incomplete libvirt network IP definition')
                other = ipaddress.ip_network(ip.get('address') + '/' + ip.get('netmask'), strict=False)
                if other.overlaps(intended):
                    problems.append('network subnet collision: ' + name)
        domains = run(prefix + ['list', '--all', '--name']).splitlines()
        owned = {n['name'] for n in lab['nodes']}
        disks = {n['disk'] for n in lab['nodes']} | {n['base_disk'] for n in lab['nodes']}
        for name in domains:
            if not name.strip():
                continue
            definition = ET.fromstring(run(prefix + ['dumpxml', name]))
            if name in owned:
                problems.append('domain name collision: ' + name)
            for source in definition.findall('.//devices/disk/source'):
                if source.get('file') in disks:
                    problems.append('domain disk collision: ' + name)
        for node in lab['nodes']:
            path = Path(node['disk'])
            if path.exists() or path.is_symlink():
                problems.append('disk exists: ' + str(path))
    except (OSError, subprocess.SubprocessError, KeyError, ValueError, TypeError, ET.ParseError, IndexError) as exc:
        raise PreflightError('incomplete host inspection; refusing to plan: ' + str(exc)) from exc
    return problems


def render_kickstart(lab, node_name, *, username, password_hash, root_password_hash,
                     ssh_public_key, labadmin_ssh_public_key=None):
    """Render clean pre-remediation install; credentials supplied privately by caller.

    Never persist rendered Kickstarts to Git or a world-readable directory.
    Caller must verify media, ksvalidator output and a real disposable boot.
    """
    node = next((n for n in lab['nodes'] if n['name'] == node_name), None)
    if node is None:
        raise ValueError('unknown node')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', username) or username in ('root', 'labadmin'):
        raise ValueError('invalid username')
    for pwd_hash in (password_hash, root_password_hash):
        if not isinstance(pwd_hash, str) or not re.fullmatch(r'\$6\$(?:rounds=[0-9]+\$)?[A-Za-z0-9./]{1,32}\$[A-Za-z0-9./]{8,}', pwd_hash):
            raise ValueError('SHA-512 crypt password hash required')
    if labadmin_ssh_public_key is None:
        labadmin_ssh_public_key = ssh_public_key
    for key in (ssh_public_key, labadmin_ssh_public_key):
        if not isinstance(key, str) or not re.fullmatch(r'(?:ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.@+-]+)?', key):
            raise ValueError('RSA/ECDSA public key required')
    release = lab['releases'][node['release']]
    fips = release['fips']
    if fips is not True:
        raise ValueError('FIPS must be explicitly enabled for installer and installed guest')
    graphical = node['role'] in ('controller', 'gui')
    python_package = 'python3.11' if node['release'] == 'rhel8' else 'python3'
    packages = (['@^graphical-server-environment'] if graphical else ['@^minimal-environment']) + ['openscap-scanner', 'scap-security-guide', 'sudo', 'shadow-utils', 'openssh-server', python_package]
    if node['release'] != 'rhel8':
        packages += ['python3-dnf', 'python3-libselinux']
    if node['role'] == 'controller':
        packages += ['git', 'openssh-clients']
    if node['role'] not in ('controller', 'node', 'gui'):
        raise ValueError('unknown role')
    network = lab['network']
    lines = [
        '# Clean quarterly baseline: NO STIG remediation applied',
        'cdrom', 'text', 'eula --agreed', 'reboot', 'lang en_US.UTF-8',
        'keyboard --xlayouts=us', 'timezone UTC --utc', 'selinux --enforcing',
        'firewall --enabled --service=ssh', 'services --enabled=sshd',
        'rootpw --iscrypted ' + root_password_hash,
        'user --name=' + username + ' --groups=wheel --iscrypted --password=' + password_hash,
        'sshkey --username=' + username + ' "' + ssh_public_key + '"',
        'user --name=labadmin --lock',
        'sshkey --username=labadmin "' + labadmin_ssh_public_key + '"',
        'network --bootproto=static --device=link --activate --onboot=on'
        + ' --ip=' + node['address'] + ' --netmask=' + str(ipaddress.ip_network(network['subnet']).netmask)
        + ' --gateway=' + network['gateway'] + ' --nameserver=' + network['gateway']
        + ' --hostname=' + node['fqdn'],
        'ignoredisk --only-use=vda', 'zerombr', 'clearpart --all --initlabel --drives=vda',
        'autopart --type=lvm',
    ]
    lines.append('bootloader --append="fips=1"')
    lines += ['%packages', *packages, '%end']
    lines += [
        '# Explicit scored exception: labadmin passwordless sudo on all lab machines.',
        '%post --log=/root/quarterly-post.log --erroronfail',
        'install -o root -g root -m 0440 /dev/null /etc/sudoers.d/90-labadmin',
        "printf '%s\\n' 'labadmin ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-labadmin",
        'visudo -cf /etc/sudoers', '%end',
    ]
    if graphical:
        lines += ['%post --nochroot --erroronfail', 'systemctl --root=/mnt/sysroot set-default graphical.target', '%end']
    return '\n'.join(lines) + '\n'


def plan_install(lab, node_name, *, kickstart, confirm=None, media_run=_read_command):
    """Return inert direct-DVD virt-install argv for a fresh per-node baseline.

    This is NOT a readiness signal. Caller must run preflight(), extract kernel
    and initrd from this exact DVD, then inspect generated XML before execution.
    No command from this function is executed here. The caller must externally
    gate against concurrent build/clone/use of this same node identity.
    """
    node = next((n for n in lab['nodes'] if n['name'] == node_name), None)
    if node is None:
        raise ValueError('unknown node')
    if confirm != 'build-' + node['name']:
        raise ValueError('explicit exact build domain confirmation required')
    release = lab['releases'][node['release']]
    root = Path(lab['disk_root'])
    build_key = BUILD_KEYS.get(node['role'])
    if build_key is None:
        raise ValueError('unknown node role')
    disk = Path(release[build_key])
    if disk != root / 'build' / (node['name'] + '.qcow2') or disk.exists() or disk.is_symlink():
        raise ValueError('same-node baseline build disk must be new and owned')
    if disk.resolve(strict=False) != disk:
        raise ValueError('build disk resolves outside owned path')
    ks = Path(kickstart)
    if not ks.is_file() or ks.is_symlink() or ks.stat().st_mode & 0o077 or ks.stat().st_uid != os.getuid():
        raise ValueError('Kickstart must be owner-only regular private file')
    iso = Path(release['dvd'])
    if not iso.is_file() or iso.is_symlink():
        raise ValueError('vendor DVD missing or unsafe')
    for key in ('kernel', 'initrd'):
        source = Path(release[key])
        if not source.is_file() or source.is_symlink() or not source.is_relative_to(root / 'installers'):
            raise ValueError('installer kernel/initrd absent or outside owned cache')
    try:
        info = media_run(['xorriso', '-indev', str(iso), '-pvd_info'])
        entries = media_run(['xorriso', '-indev', str(iso), '-ls', '/images/pxeboot'])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('unable to inspect DVD') from exc
    ids = re.findall(r'^Volume Id\s*:\s*(\S+)\s*$', info, flags=re.MULTILINE)
    if ids != [release['volume_id']] or not {'vmlinuz', 'initrd.img'} <= set(re.findall(r"^'([^']+)'$", entries, flags=re.MULTILINE)):
        raise ValueError('DVD volume ID or installer payload mismatch')
    if release['fips'] is not True:
        raise ValueError('FIPS must be enabled for installer and installed guest')
    args = 'inst.ks=file:/' + ks.name + ' inst.stage2=hd:LABEL=' + release['volume_id'] + ' inst.repo=cdrom fips=1'
    return [
        '/usr/bin/python3', '/usr/sbin/virt-install', '--connect', 'qemu:///system',
        '--name', 'build-' + node['name'], '--memory', '4096', '--vcpus', '2',
        '--os-variant', release['os_variant'], '--network', 'network=' + lab['network']['name'] + ',model=virtio',
        '--disk', 'path=' + str(disk) + ',size=40,format=qcow2,bus=virtio',
        '--disk', 'path=' + str(iso) + ',device=cdrom,readonly=on',
        '--install', 'kernel=' + release['kernel'] + ',initrd=' + release['initrd'] + ',kernel_args=' + args,
        '--initrd-inject', str(ks), '--noautoconsole',
    ]


def _standalone_qcow2(info):
    details = info.get('format-specific') or {}
    data = details.get('data') or {}
    children = info.get('children', [])
    # qemu-img reports the qcow2's own file-protocol node as a child.
    # Anything else (notably a data-file child) is an external dependency.
    own_file = (len(children) == 1 and children[0].get('name') == 'file'
                and children[0].get('info', {}).get('format') == 'file'
                and not children[0].get('info', {}).get('children'))
    return (info.get('format') == 'qcow2'
            and not any(info.get(k) for k in ('backing-filename', 'full-backing-filename',
                                              'backing-filename-format', 'backing-image'))
            and 'data-file' not in info and (not children or own_file)
            and not any(k in data for k in ('backing-filename', 'data-file'))
            and not data.get('has-backing-file')
            and not info.get('snapshots'))


def plan_clone(lab, node_name, *, base_sha256, confirm=None, info_run=_read_command):
    """Return inert metadata for a same-node clone of its sealed clean baseline.

    No identity scrubbing or sysprep: baseline and clone retain the same node
    identity. Caller must externally verify installation, seal and ensure the
    build domain or another guest of this identity is not running concurrently.
    This function does not inspect domain state. Never execute a plan as a shell command.
    """
    node = next((n for n in lab['nodes'] if n['name'] == node_name), None)
    if node is None or node['image_kind'] != 'clean':
        raise ValueError('unknown clean node')
    if confirm != node['name']:
        raise ValueError('explicit exact clone domain confirmation required')
    base, clone = Path(node['base_disk']), Path(node['disk'])
    root = Path(lab['disk_root'])
    base_key = BASE_KEYS.get(node['role'])
    if base_key is None:
        raise ValueError('unknown node role')
    if (base != Path(lab['releases'][node['release']][base_key])
            or base != root / 'bases' / (node['name'] + '-baseline.qcow2')
            or clone != root / 'clones' / (node['name'] + '.qcow2')
            or base.suffix != '.qcow2' or clone.suffix != '.qcow2'
            or base == clone or base.resolve(strict=False) != base
            or clone.resolve(strict=False) != clone):
        raise ValueError('unowned or unsafe base/clone path')
    if not base.is_file() or base.is_symlink() or base.stat().st_mode & 0o222:
        raise ValueError('base missing or not immutable')
    if clone.exists() or clone.is_symlink():
        raise ValueError('clone disk already exists')
    if not re.fullmatch(r'[0-9a-f]{64}', base_sha256):
        raise ValueError('expected base sha256 required')
    digest = hashlib.sha256()
    with base.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != base_sha256:
        raise ValueError('base SHA-256 mismatch')
    try:
        info = json.loads(info_run(['qemu-img', 'info', '--output=json', str(base)]))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('cannot inspect base image') from exc
    if not _standalone_qcow2(info):
        raise ValueError('base must be standalone qcow2 without snapshots or external dependencies')
    return {'node_name': node_name, 'base_disk': str(base), 'disk': str(clone),
            'base_sha256': base_sha256}


def _open_directory_without_symlinks(path):
    """Walk from / with O_NOFOLLOW; return an open directory descriptor."""
    if not path.is_absolute():
        raise ValueError('disk root must be absolute')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def execute_clone(lab, node_name, *, base_sha256, confirm=None, convert_run=subprocess.run):
    """Revalidate and copy into an owned directory; publish atomically without replacing.

    Returns the final disk path. No libvirt or guest operation occurs. The caller
    must verify the externally sealed same-node baseline and gate against any
    concurrently running build or guest of this identity. The pre-creation
    preflight intentionally reports collisions once this lab is established.
    """
    plan = plan_clone(lab, node_name, base_sha256=base_sha256, confirm=confirm)
    root = Path(lab['disk_root'])
    base, clone = Path(plan['base_disk']), Path(plan['disk'])
    if base.parent != root / 'bases' or clone.parent != root / 'clones':
        raise ValueError('clone requires direct owned bases/clones directories')
    with ExitStack() as stack:
        root_fd = _open_directory_without_symlinks(root)
        stack.callback(os.close, root_fd)
        base_dir = os.open('bases', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=root_fd)
        try:
            target_dir = os.open('clones', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=root_fd)
            try:
                directory = os.fstat(target_dir)
                if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
                    raise ValueError('clone directory must be owner-only and owned by caller')
                source_fd = os.open(base.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=base_dir)
                try:
                    source = os.fstat(source_fd)
                    if not stat.S_ISREG(source.st_mode) or source.st_mode & 0o222:
                        raise ValueError('base missing or not immutable')
                    digest = hashlib.sha256()
                    offset = 0
                    while chunk := os.pread(source_fd, 1024 * 1024, offset):
                        digest.update(chunk)
                        offset += len(chunk)
                    if digest.hexdigest() != base_sha256:
                        raise ValueError('base SHA-256 mismatch at execution')
                    source_path = f'/proc/self/fd/{source_fd}'
                    info = json.loads(subprocess.run(
                        ['qemu-img', 'info', '--output=json', source_path],
                        check=True, capture_output=True, text=True, timeout=20,
                        pass_fds=(source_fd,)).stdout)
                    if not _standalone_qcow2(info):
                        raise ValueError('base must be standalone qcow2 without snapshots or external dependencies')
                    # The target is never passed to qemu-img. It can only be
                    # installed by link(2), which fails if any entry already exists.
                    temp_name = '.clone-' + uuid.uuid4().hex + '.qcow2'
                    temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=target_dir)
                    os.close(temp_fd)
                    try:
                        convert_run(['qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2',
                                     source_path, f'/proc/self/fd/{target_dir}/{temp_name}'],
                                    check=True, capture_output=True, timeout=600,
                                    pass_fds=(source_fd, target_dir))
                        os.link(temp_name, clone.name, src_dir_fd=target_dir,
                                dst_dir_fd=target_dir, follow_symlinks=False)
                    finally:
                        os.unlink(temp_name, dir_fd=target_dir)
                    return str(clone)
                finally:
                    os.close(source_fd)
            finally:
                os.close(target_dir)
        finally:
            os.close(base_dir)
