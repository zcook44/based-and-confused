"""Quarterly lab planning, cloning and hash-only asset handoff (no guest boot)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys

from . import vm


class LabError(ValueError):
    """Invalid lab input; no handoff is published."""


_REQUIRED_CREDENTIALS = frozenset({'QUARTERLY_USERNAME', 'QUARTERLY_PASSWORD',
                                   'QUARTERLY_ROOT_PASSWORD', 'QUARTERLY_LABADMIN_PUBLIC_KEY'})
_CANONICAL_MANIFEST = Path(__file__).resolve().with_name('lab.json')


def load_credentials(path):
    """Parse an owner-only literal KEY=value file; never source shell code."""
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid()
            or path.stat().st_mode & 0o077):
        raise LabError('private credentials file must be owned and mode 0600')
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError) as exc:
        raise LabError('unable to read private credentials') from exc
    result = {}
    for line in lines:
        if not line or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or key not in _REQUIRED_CREDENTIALS or key in result or not value or '\x00' in value:
            raise LabError('invalid, duplicate or unknown credential entry')
        result[key] = value
    if result.keys() != _REQUIRED_CREDENTIALS:
        raise LabError('missing credential entry')
    return result


def hash_password(password):
    """SHA512-crypt through stdin; plaintext is never placed in argv or logs."""
    if not password or '\n' in password or '\x00' in password:
        raise LabError('invalid password input')
    try:
        proc = subprocess.run(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n',
                              capture_output=True, text=True, timeout=20, check=True)
        hashed = proc.stdout.strip()
        if not hashed.startswith('$6$'):
            raise LabError('unexpected password hash output')
        return hashed
    except (OSError, subprocess.SubprocessError) as exc:
        raise LabError('password hashing unavailable') from exc


def _create_private(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _hash_regular(path, *, immutable=False):
    """Hash an owner-private regular file through a no-follow descriptor."""
    path = Path(path)
    parent_fd = vm._open_directory_without_symlinks(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or (immutable and info.st_mode & 0o222)):
                raise LabError('input must be an owner-private regular file; sealed base must be read-only')
            if immutable:
                try:
                    inspected = subprocess.run(
                        ['qemu-img', 'info', '--output=json', f'/proc/self/fd/{fd}'],
                        check=True, capture_output=True, text=True, timeout=20,
                        pass_fds=(fd,))
                    image_info = json.loads(inspected.stdout)
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    raise LabError('cannot inspect baseline qcow2') from exc
                if not vm._standalone_qcow2(image_info):
                    raise LabError('baseline must be standalone qcow2 without snapshots or external dependencies')
            digest = hashlib.sha256()
            with os.fdopen(fd, 'rb', closefd=False) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _lab_commit(*, manifest_bytes=None, git_run=subprocess.run):
    """Identify HEAD only when this repository has no tracked or untracked changes."""
    repo = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    def run(*args):
        return git_run(['git', '-C', str(repo), *args], env=env,
                       capture_output=True, text=True, check=True, timeout=20).stdout.strip()
    commit = run('rev-parse', '--verify', 'HEAD')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise LabError('cannot identify lab commit')
    if run('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none'):
        raise LabError('lab worktree is dirty; review and commit changes before handoff')
    if manifest_bytes is not None:
        committed = git_run(['git', '-C', str(repo), 'show', f'{commit}:quarterly/lab.json'],
                            env=env, capture_output=True, check=True, timeout=20).stdout
        if committed != manifest_bytes:
            raise LabError('canonical manifest bytes differ from committed HEAD')
    return commit


def _load_asset_manifest(path):
    """Read the exact tracked manifest through a no-follow descriptor."""
    if os.path.abspath(path) != str(_CANONICAL_MANIFEST):
        raise LabError('asset-record requires canonical quarterly/lab.json')
    parent_fd = vm._open_directory_without_symlinks(_CANONICAL_MANIFEST.parent)
    try:
        fd = os.open(_CANONICAL_MANIFEST.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LabError('canonical manifest must be a regular file')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                data = source.read()
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    commit = _lab_commit(manifest_bytes=data)
    return vm.validate_manifest(json.loads(data)), commit


def export_asset_record(lab, node_name, cycle_id, base_sha256, pre, out, *, commit=None):
    """Export manifest identity and checked file digests, never scan/guest contents."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', cycle_id):
        raise LabError('cycle-id must be a nonempty identifier')
    if not re.fullmatch(r'[0-9a-f]{64}', base_sha256):
        raise LabError('expected lowercase base SHA-256 required')
    node = next((n for n in lab['nodes'] if n['name'] == node_name), None)
    if node is None or node['image_kind'] != 'clean':
        raise LabError('unknown clean node')
    base = Path(node['base_disk'])
    root = Path(lab['disk_root'])
    if (base != root / 'bases' / (node_name + '-baseline.qcow2')
            or base != Path(lab['releases'][node['release']][vm.BASE_KEYS[node['role']]])):
        raise LabError('base is not the selected node-owned baseline')
    if _hash_regular(base, immutable=True) != base_sha256:
        raise LabError('base SHA-256 mismatch')
    pre_sha256 = _hash_regular(pre)
    if commit is None:
        commit = _lab_commit()
    record = {'schema_version': 1, 'cycle_id': cycle_id, 'asset_fqdn': node['fqdn'],
              'os_release': lab['releases'][node['release']]['os_variant'],
              'role': node['role'], 'baseline_state': 'clean',
              'baseline_sha256': base_sha256, 'pre_scan_sha256': pre_sha256,
              'lab_commit': commit}
    destination = Path(out)
    parent_fd = vm._open_directory_without_symlinks(destination.parent)
    try:
        parent = os.fstat(parent_fd)
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise LabError('output parent must be owned and private (0700)')
        fd = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write((json.dumps(record, indent=2) + '\n').encode())
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(destination.name, dir_fd=parent_fd)
            raise
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description='Isolated quarterly RHEL lab and asset handoff')
    parser.add_argument('--manifest', type=Path, default=vm.MANIFEST)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inventory')
    sub.add_parser('network-xml')
    sub.add_parser('preflight')
    ks = sub.add_parser('kickstart')
    ks.add_argument('--node', required=True)
    ks.add_argument('--env', type=Path, required=True)
    ks.add_argument('--out', type=Path, required=True)
    install = sub.add_parser('install-plan', help='print command only; no VM is created')
    install.add_argument('--node', required=True)
    install.add_argument('--kickstart', type=Path, required=True)
    install.add_argument('--confirm', required=True)
    clone = sub.add_parser('clone-plan', help='print inert clone metadata only')
    clone.add_argument('--node', required=True)
    clone.add_argument('--base-sha256', required=True)
    clone.add_argument('--confirm', required=True)
    create_clone = sub.add_parser('clone', help='atomically copy a verified sealed base disk, but do not boot it')
    create_clone.add_argument('--node', required=True)
    create_clone.add_argument('--base-sha256', required=True)
    create_clone.add_argument('--confirm', required=True)
    handoff = sub.add_parser('asset-record', help='hash owner-private qcow2 and pre-scan; export private JSON (manual seal/assessment required)')
    handoff.add_argument('--node', required=True)
    handoff.add_argument('--cycle-id', required=True)
    handoff.add_argument('--base-sha256', required=True)
    handoff.add_argument('--pre', type=Path, required=True)
    handoff.add_argument('--out', type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == 'inventory':
            print(json.dumps(vm.load_manifest(args.manifest)['nodes'], indent=2))
        elif args.command == 'network-xml':
            print(vm.render_network_xml(vm.load_manifest(args.manifest)))
        elif args.command == 'preflight':
            problems = vm.preflight(vm.load_manifest(args.manifest))
            if problems:
                raise LabError('; '.join(problems))
            print('Preflight: no currently detectable route/network/domain/clone-disk collision')
        elif args.command == 'kickstart':
            env = load_credentials(args.env)
            hashed = hash_password(env['QUARTERLY_PASSWORD'])
            root_hash = hash_password(env['QUARTERLY_ROOT_PASSWORD'])
            rendered = vm.render_kickstart(vm.load_manifest(args.manifest), args.node,
                username=env['QUARTERLY_USERNAME'], password_hash=hashed,
                root_password_hash=root_hash,
                ssh_public_key=env['QUARTERLY_LABADMIN_PUBLIC_KEY'],
                labadmin_ssh_public_key=env['QUARTERLY_LABADMIN_PUBLIC_KEY'])
            _create_private(args.out, rendered.encode())
            print(args.out)
        elif args.command == 'install-plan':
            command = vm.plan_install(vm.load_manifest(args.manifest), args.node,
                                      kickstart=args.kickstart, confirm=args.confirm)
            print(shlex.join(command))
        elif args.command == 'clone-plan':
            plan = vm.plan_clone(vm.load_manifest(args.manifest), args.node,
                                 base_sha256=args.base_sha256, confirm=args.confirm)
            print(json.dumps(plan, indent=2))
        elif args.command == 'clone':
            path = vm.execute_clone(vm.load_manifest(args.manifest), args.node,
                                    base_sha256=args.base_sha256, confirm=args.confirm)
            print(path)
        elif args.command == 'asset-record':
            lab, commit = _load_asset_manifest(args.manifest)
            print(export_asset_record(lab, args.node, args.cycle_id, args.base_sha256,
                                      args.pre, args.out, commit=commit))
        return 0
    except (LabError, vm.PreflightError, OSError, ValueError,
            subprocess.SubprocessError) as exc:
        print(f'quarterly: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
