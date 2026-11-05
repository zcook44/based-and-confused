#!/usr/bin/env python3
"""Build the eight-node Kickstart menu ISO from licensed local RHEL DVDs.

Uses menu_lab.py for the actual remaster. No VM, repository service, or network
access is required to build; RHEL 8/9 installations still need their HTTP trees.
"""

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

from lab import extract, inspect_treeinfo
from menu_lab import NODES, load_gui_credentials

ROOT = Path(__file__).resolve().parent


def create_gui_credentials(path: Path) -> None:
    """Create a private, non-overwriting credential sidecar for GUI logins."""
    path = path.expanduser()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError('put GUI credentials outside the Git repository')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    for node in NODES:
        if node.role != 'node2':
            continue
        password = secrets.token_urlsafe(32)
        digest = subprocess.run(['openssl', 'passwd', '-6', '-stdin'], input=password + '\n',
                                text=True, capture_output=True, check=True).stdout.strip()
        data[node.name] = {'password': password, 'hash': digest}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
    except BaseException:
        path.unlink()
        raise


def validate_inputs(args: argparse.Namespace) -> None:
    paths = (args.rhel8, args.rhel9, args.rhel10)
    for path in (*paths, args.ssh_key):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'missing or empty input: {path}')
    if len({p.resolve() for p in paths}) != 3:
        raise ValueError('use three distinct, version-matched RHEL DVDs')
    key = args.ssh_key.read_text().strip()
    if not key.startswith(('ecdsa-sha2-', 'ssh-rsa ')):
        raise ValueError('use a FIPS-compatible ECDSA or RSA public SSH key')
    if args.gui_credentials.resolve().is_relative_to(ROOT):
        raise ValueError('put GUI credentials outside the Git repository')
    if args.gui_credentials.stat().st_uid != os.geteuid():
        raise ValueError('GUI credentials must be owned by the current user')
    load_gui_credentials(args.gui_credentials)
    out = args.out.resolve()
    if out in {p.resolve() for p in paths} or out.exists():
        raise ValueError('output already exists or would overwrite an input DVD')
    if out.with_name(out.name + '.partial').exists():
        raise ValueError('stale partial output exists; inspect before retrying')
    if out.with_name(out.name + '.sha256').exists():
        raise ValueError('checksum sidecar already exists; choose a fresh output name')


def check_dvd_versions(rhel8: Path, rhel9: Path, rhel10: Path) -> None:
    """Reject wrong or boot-only source media before creating an output ISO."""
    with tempfile.TemporaryDirectory(prefix='rhel-media-check-') as tmp:
        for path, version in ((rhel8, '8.10'), (rhel9, '9.8'), (rhel10, '10.2')):
            info = extract(path, '/.treeinfo', Path(tmp) / (version + '.treeinfo')).decode()
            inspect_treeinfo(info, version.split('.')[0])
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(info)
            if config['general'].get('version') != version:
                raise ValueError(f'expected RHEL {version} full DVD: {path}')


def write_checksum(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    sidecar = path.with_name(path.name + '.sha256')
    with sidecar.open('x') as stream:
        stream.write(f'{digest.hexdigest()}  {path.name}\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('rhel8', 'rhel9', 'rhel10', 'out', 'ssh-key', 'gui-credentials'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--generate-credentials', action='store_true',
                        help='create a new private GUI credential file; refuses an existing file')
    parser.add_argument('--check-only', action='store_true',
                        help='check paths, key, credential hashes and output safety; do not build')
    args = parser.parse_args()
    try:
        if args.check_only and args.generate_credentials:
            raise ValueError('--check-only cannot generate credentials')
        if args.generate_credentials:
            create_gui_credentials(args.gui_credentials)
        validate_inputs(args)
        if args.check_only:
            print('Inputs and output path validated (DVD contents and bootability not checked).')
            return 0
        check_dvd_versions(args.rhel8, args.rhel9, args.rhel10)
        command = [sys.executable, str(ROOT / 'menu_lab.py'),
                   '--source', str(args.rhel10), '--rhel8', str(args.rhel8),
                   '--rhel9', str(args.rhel9), '--out', str(args.out),
                   '--ssh-key', str(args.ssh_key),
                   '--gui-credentials', str(args.gui_credentials)]
        subprocess.run(command, cwd=ROOT, check=True)
        write_checksum(args.out)
        print(f'Build complete: {args.out.resolve()}')
        print(f'Checksum: {args.out.resolve()}.sha256')
        print(f'GUI login credentials (private; do not upload): {args.gui_credentials.resolve()}')
        return 0
    except (ValueError, FileExistsError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'error: {exc}\n')


if __name__ == '__main__':
    sys.exit(main())
