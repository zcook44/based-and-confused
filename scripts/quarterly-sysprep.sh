#!/usr/bin/env bash
# Offline candidate only. No base promotion, boot, registration, or guest network.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
QUARTERLY_DEFAULT_MANIFEST="$script_dir/../quarterly/lab.json" /usr/bin/python3 - "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

OPS = ('machine-id', 'ssh-hostkeys', 'rh-subscription-manager', 'net-nmconn',
       'net-hwaddr', 'dhcp-client-state', 'logfiles')
ROLE_KEYS = {'node': 'build_disk', 'gui': 'gui_build_disk', 'controller': 'controller_build_disk'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tool(env_key, default):
    executable = os.environ.get(env_key, default)
    found = shutil.which(executable)
    require(found is not None and os.access(found, os.X_OK), 'required tool unavailable: ' + default)
    return found


def run(args, *, pass_fds=(), timeout=30):
    # Never echo third-party stderr: it can contain guest data.
    try:
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=timeout,
                              pass_fds=pass_fds).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('inspection or offline operation failed: ' + args[0].rsplit('/', 1)[-1]) from exc


def safe_path(path):
    p = Path(path)
    require(p.is_absolute() and str(p) == os.path.normpath(str(p)), 'noncanonical disk path')
    for component in (p, *p.parents):
        if component == Path('/'):
            break
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(mode), 'symlink in disk path')
    return p


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def standalone_qcow2(info):
    details = info.get('format-specific') or {}
    data = details.get('data') or {}
    return (info.get('format') == 'qcow2'
            and not any(key in info for key in ('backing-filename', 'full-backing-filename',
                                                 'backing-filename-format', 'backing-image',
                                                 'data-file', 'children'))
            and not any(key in data for key in ('backing-filename', 'data-file'))
            and not data.get('has-backing-file')
            and not info.get('snapshots'))


def attached_to(path, target):
    if not path or not os.path.isabs(path):
        return False
    return (os.path.normpath(path) == str(target)
            or (os.path.exists(path) and target.exists() and os.path.samefile(path, target)))


def verify_domain_isolation(prefix, build_name, source, candidate):
    domains = run(prefix + ['list', '--all', '--name']).splitlines()
    require(domains.count(build_name) == 1, 'build domain not found uniquely')
    require(run(prefix + ['domstate', build_name]).strip() == 'shut off', 'build domain not shut off')
    for name in domains:
        require(name.strip() == name and name, 'invalid domain list')
        for xml_args in (['dumpxml', name], ['dumpxml', name, '--inactive']):
            definition = ET.fromstring(run(prefix + xml_args))
            disk_sources = definition.findall('./devices/disk')
            if name == build_name:
                disks = [disk for disk in disk_sources if disk.get('device') == 'disk']
                require(len(disks) == 1, 'build domain must have exactly one disk')
                disk = disks[0]
                disk_source = disk.find('source')
                driver = disk.find('driver')
                require(disk.get('type') == 'file' and disk_source is not None
                        and disk_source.get('file') == str(source) and driver is not None
                        and driver.get('type') == 'qcow2', 'build disk does not match manifest')
                for extra_disk in disk_sources:
                    for extra_source in extra_disk.findall('.//source'):
                        if extra_source is disk_source:
                            continue
                        path = extra_source.get('file')
                        require(not attached_to(path, source) and not attached_to(path, candidate),
                                'source or candidate attached outside primary build disk')
            else:
                for disk in disk_sources:
                    for disk_source in disk.findall('.//source'):
                        path = disk_source.get('file')
                        require(not attached_to(path, source) and not attached_to(path, candidate),
                                'source or candidate attached to another domain')


def main():
    parser = argparse.ArgumentParser(description='Inspect or create a private, unbootable offline sanitized candidate; never promote a base')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='read-only inspection')
    mode.add_argument('--execute', action='store_true', help='requires exact build domain confirmation')
    parser.add_argument('--node', required=True)
    parser.add_argument('--confirm')
    args = parser.parse_args()
    require(not args.execute or args.confirm == 'build-' + args.node,
            'exact --confirm build-NAME required')
    require(not args.check or args.confirm is None, '--check does not accept confirmation')
    manifest_path = os.environ.get('QUARTERLY_LAB_MANIFEST', os.environ['QUARTERLY_DEFAULT_MANIFEST'])
    lab = json.loads(Path(manifest_path).read_text())
    require(lab.get('schema') == 1, 'unsupported manifest')
    matches = [node for node in lab['nodes'] if node['name'] == args.node]
    require(len(matches) == 1, 'unknown or duplicate node')
    node = matches[0]
    role = node['role']
    require(role in ROLE_KEYS, 'unsupported role')
    release = lab['releases'][node['release']]
    root = safe_path(lab['disk_root'])
    source = safe_path(release[ROLE_KEYS[role]])
    base = safe_path(release[{'node': 'base_disk', 'gui': 'gui_base_disk',
                              'controller': 'controller_base_disk'}[role]])
    require(source.parent == root / 'build' and source.suffix == '.qcow2', 'build source outside canonical build directory')
    require(base.parent == root / 'bases' and base == safe_path(node['base_disk']), 'invalid base map')
    require(node.get('image_kind', 'clean') == 'clean', 'not a clean build')
    candidate = safe_path(root / 'candidates' / (source.stem + '-' + role + '-candidate.qcow2'))
    require(candidate != source and candidate != base and candidate != safe_path(node['disk']), 'path collision')
    require(not candidate.exists() and not candidate.is_symlink(), 'candidate already exists')
    build_dir = source.parent
    require(build_dir.is_dir() and build_dir.stat().st_uid == os.getuid()
            and not build_dir.stat().st_mode & 0o077,
            'build directory must exist and be owner-only')
    directory = candidate.parent
    require(directory.is_dir() and directory.stat().st_uid == os.getuid()
            and not directory.stat().st_mode & 0o077, 'candidate directory must exist and be owner-only')
    st = source.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
            and not st.st_mode & 0o077, 'source must be an owned owner-only regular disk')
    # Open without following links; the descriptor pins the checked source inode.
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        require(os.fstat(source_fd).st_ino == st.st_ino and os.fstat(source_fd).st_dev == st.st_dev,
                'source changed during inspection')
        virsh = tool('QUARTERLY_VIRSH', 'virsh')
        qemu = tool('QUARTERLY_QEMU_IMG', 'qemu-img')
        sysprep = tool('QUARTERLY_VIRT_SYSPREP', 'virt-sysprep')
        prefix = [virsh, '-c', 'qemu:///system']
        build_name = 'build-' + args.node
        verify_domain_isolation(prefix, build_name, source, candidate)
        source_ref = '/proc/self/fd/' + str(source_fd)
        info = json.loads(run([qemu, 'info', '--output=json', source_ref], pass_fds=(source_fd,)))
        require(standalone_qcow2(info), 'source is not standalone snapshot-free qcow2')
        supported = set()
        for line in run([sysprep, '--list-operations']).splitlines():
            token = line.split()
            if token and re.fullmatch(r'[a-z][a-z0-9-]*', token[0]):
                supported.add(token[0])
        require(set(OPS) <= supported, 'virt-sysprep does not support exact required operations')
        source_hash = hash_file(source_ref)
        report = {'node': args.node, 'build_domain': build_name, 'source_disk': str(source),
                  'source_sha256': source_hash, 'candidate_disk': str(candidate),
                  'operations': list(OPS), 'bootable': False,
                  'next_gate': 'NOT bootable: static /etc/hostname may persist; offline personalization and two-canary validation required; no base promotion'}
        if args.check:
            print(json.dumps(report, sort_keys=True))
            return
        # All intermediate guest data stays inside a new owner-only directory.
        with tempfile.TemporaryDirectory(prefix='.sysprep-', dir=directory) as work:
            temp = Path(work) / 'candidate.qcow2'
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            run([qemu, 'convert', '-f', 'qcow2', '-O', 'qcow2', source_ref, str(temp)],
                pass_fds=(source_fd,), timeout=600)
            require(hash_file(source_ref) == source_hash, 'source changed while copying')
            temp.chmod(0o600)
            run([sysprep, '--operations', ','.join(OPS), '-a', str(temp)], timeout=1200)
            temp.chmod(0o600)
            cleaned = json.loads(run([qemu, 'info', '--output=json', str(temp)]))
            require(standalone_qcow2(cleaned), 'candidate failed format inspection')
            report['candidate_sha256'] = hash_file(temp)
            # Refuse a build resumed or source attached elsewhere during offline work.
            verify_domain_isolation(prefix, build_name, source, candidate)
            require(hash_file(source_ref) == source_hash, 'source changed before publication')
            require(not candidate.exists() and not candidate.is_symlink(), 'candidate appeared during operation')
            # link(2) fails if candidate exists: no rename replacement window.
            os.link(temp, candidate, follow_symlinks=False)
        print(json.dumps(report, sort_keys=True))
    finally:
        os.close(source_fd)


try:
    main()
except (ValueError, KeyError, TypeError, OSError, ET.ParseError, json.JSONDecodeError) as exc:
    print('quarterly-sysprep: refused: ' + str(exc), file=sys.stderr)
    sys.exit(1)
PY
