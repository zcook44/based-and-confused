"""Synthetic offline personalization contract; never touches real guests."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/quarterly-personalize.sh'
NAME = 'qa-rhel9-node1'


def make_lab(tmp_path):
    root = tmp_path / 'quarterly'
    clones = root / 'clones'
    clones.mkdir(parents=True, mode=0o700)
    clones.chmod(0o700)
    disk = clones / (NAME + '.qcow2')
    subprocess.run(['/usr/sbin/qemu-img', 'create', '-f', 'qcow2', str(disk), '1M'],
                   check=True, capture_output=True)
    disk.chmod(0o600)
    manifest = {
        'schema': 1, 'disk_root': str(root),
        'network': {'subnet': '192.168.164.0/24', 'prefix': 24,
                    'gateway': '192.168.164.1', 'domain': 'qa.stig.test'},
        'nodes': [{'name': NAME, 'fqdn': NAME + '.qa.stig.test',
                   'address': '192.168.164.102', 'disk': str(disk),
                   'base_disk': str(root / 'bases' / 'clean.qcow2')}],
        'releases': {'rhel9': {'base_disk': str(root / 'bases' / 'clean.qcow2'),
                                'build_disk': str(root / 'build' / 'clean.qcow2')}}
    }
    manifest['nodes'][0].update(release='rhel9', role='node', image_kind='clean')
    mpath = tmp_path / 'lab.json'
    mpath.write_text(json.dumps(manifest))
    bins = tmp_path / 'bin'
    bins.mkdir()
    stub = '''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps([name, args]) + '\\n')
if name == 'virsh':
    if 'list' in args:
        if os.environ.get('STUB_ATTACH_ON_LIST_CALL'):
            with open(os.environ['STUB_LOG']) as history:
                count = sum(1 for line in history if json.loads(line) == ['virsh', args])
            if count >= int(os.environ['STUB_ATTACH_ON_LIST_CALL']):
                sys.stdout.write('newly-attached-domain\\n')
        else: sys.stdout.write(os.environ.get('STUB_DOMAINS', ''))
    elif 'dumpxml' in args: print(os.environ.get('STUB_XML', '<domain><devices/></domain>'))
elif name == 'qemu-img':
    print(os.environ.get('STUB_QEMU_INFO', '{"format":"qcow2"}'))
elif name == 'virt-customize':
    if os.environ.get('STUB_CUSTOMIZE_FAIL'): sys.exit(3)
    upload = args[args.index('--upload') + 1]
    pathlib.Path(os.environ['STUB_CONFIG']).write_bytes(pathlib.Path(upload.split(':', 1)[0]).read_bytes())
elif name == 'guestfish':
    if os.environ.get('STUB_INSPECTION_FAIL') and '-i' in args: sys.exit(5)
    if 'statns' in args:
        print(os.environ.get('STUB_STAT', 'st_mode: 33152\\nst_uid: 0\\nst_gid: 0'))
    elif 'exists' in args: print(os.environ.get('STUB_AUTORELABEL', 'false'))
    elif 'ls' in args: print(os.environ.get('STUB_SSH_FILES', 'sshd_config'))
elif name == 'virt-cat':
    p = args[-1]
    if p == '/etc/hostname': print(os.environ.get('STUB_HOSTNAME', 'qa-rhel9-node1.qa.stig.test'))
    elif p == '/etc/hosts': sys.stdout.write(os.environ.get('STUB_HOSTS', '127.0.0.1 localhost\\n'))
    elif p.endswith('.nmconnection'):
        sys.stdout.write(os.environ.get('STUB_CONNECTION', pathlib.Path(os.environ['STUB_CONFIG']).read_text()))
    else: sys.exit(4)
'''
    for tool in ('virsh', 'qemu-img', 'virt-customize', 'virt-cat', 'guestfish'):
        path = bins / tool
        path.write_text(stub)
        path.chmod(0o755)
    log = tmp_path / 'calls.jsonl'
    env = dict(os.environ, PATH=str(bins) + ':' + os.environ['PATH'], STUB_LOG=str(log),
               TMPDIR=str(tmp_path), STUB_CONFIG=str(tmp_path / 'config'))
    def run(*args, **changes):
        return subprocess.run([str(SCRIPT), '--manifest', str(mpath), '--node', NAME, *args],
                              capture_output=True, text=True, env=env | changes)
    return root, disk, mpath, log, run


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


class QuarterlyPersonalizeTests(unittest.TestCase):
    def test_wrapper_is_strict_bash(self):
        lines = SCRIPT.read_text().splitlines()
        assert lines[0] == '#!/usr/bin/env bash'
        assert lines[2] == 'set -euo pipefail'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.lab = make_lab(Path(temporary.name))

    def test_check_is_read_only_and_requires_no_customization(self):
        root, disk, _, log, run = self.lab
        before = disk.read_bytes()
        metadata = disk.stat()
        result = run('--check')
        assert result.returncode == 0, result.stderr
        assert disk.read_bytes() == before
        assert disk.stat().st_mtime_ns == metadata.st_mtime_ns
        assert disk.stat().st_mode == metadata.st_mode
        assert sorted(p.name for p in (root / 'clones').iterdir()) == [disk.name]
        assert all(name != 'virt-customize' for name, _ in calls(log))

    def test_refuses_same_size_source_mutation_during_private_copy(self):
        root, disk, _, log, run = self.lab
        # Instrument the actual Python copy at a deterministic chunk boundary.
        # The source inode and size remain unchanged while its bytes change.
        hook = root.parent / 'sitecustomize.py'
        hook.write_text('''import os, shutil
original = shutil.copyfileobj
def mutate_while_copying(src, dst, length=0):
    path = os.environ.get('STUB_MUTATE_SOURCE')
    if path and os.fstat(src.fileno()).st_ino == os.stat(path).st_ino:
        chunk = src.read(4096)
        dst.write(chunk)
        with open(path, 'r+b', buffering=0) as source:
            source.write(b'CHANGED!')
    return original(src, dst, length)
shutil.copyfileobj = mutate_while_copying
''')
        result = run('--execute', '--confirm', NAME,
                     PYTHONPATH=str(root.parent), STUB_MUTATE_SOURCE=str(disk))
        assert result.returncode != 0, result.stdout
        assert 'source changed' in result.stderr
        assert disk.read_bytes().startswith(b'CHANGED!')
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        assert all(name != 'virt-customize' for name, _ in calls(log))

    def test_execute_publishes_separate_image_and_validates_static_connection(self):
        root, disk, _, log, run = self.lab
        result = run('--execute', '--confirm', NAME)
        assert result.returncode == 0, result.stderr
        output = root / 'clones' / (NAME + '.personalized.qcow2')
        assert output.exists() and output.read_bytes() == disk.read_bytes()
        assert str(output) in result.stdout
        assert any(name == 'virt-customize' for name, _ in calls(log))
        assert any(name == 'virt-cat' for name, _ in calls(log))
        config = (root.parent / 'config').read_text()
        assert 'address1=192.168.164.102/24,192.168.164.1' in config
        assert 'dns=192.168.164.1;' in config
        assert 'mac-address=' not in config
        assert run('--execute', '--confirm', NAME).returncode != 0


    def test_guestfish_readback_uses_read_only_inspection_mount(self):
        root, _, _, log, run = self.lab
        result = run('--execute', '--confirm', NAME)
        assert result.returncode == 0, result.stderr
        probes = [argv for name, argv in calls(log) if name == 'guestfish']
        assert len(probes) == 3
        assert all('--ro' in argv and '-i' in argv
                              and argv.index('-i') < argv.index(op)
                              for argv, op in zip(probes, ('statns', 'ls', 'exists')))
        assert all('run' not in argv for argv in probes)

    def test_inspection_failure_prevents_publication(self):
        root, _, _, log, run = self.lab
        result = run('--execute', '--confirm', NAME, STUB_INSPECTION_FAIL='1')
        assert result.returncode != 0
        assert any(name == 'guestfish' and '-i' in argv for name, argv in calls(log))
        assert not list((root / 'clones').glob('*.personalized.qcow2'))

    def test_refuses_existing_output_without_overwrite(self):
        root, disk, _, log, run = self.lab
        output = root / 'clones' / (NAME + '.personalized.qcow2')
        output.write_bytes(b'preexisting')
        assert run('--execute', '--confirm', NAME).returncode != 0
        assert output.read_bytes() == b'preexisting'
        assert disk.exists()
        assert all(name != 'virt-customize' for name, _ in calls(log))


    def test_refuses_active_domain_with_clone_attached(self):
        root, disk, _, log, run = self.lab
        result = run('--execute', '--confirm', NAME, STUB_DOMAINS='running-domain\n',
                     STUB_XML=f'<domain><devices><disk><source file="{disk}"/></disk></devices></domain>')
        assert result.returncode != 0
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        assert all(name != 'virt-customize' for name, _ in calls(log))


    def test_refuses_new_attachment_immediately_before_source_copy(self):
        root, disk, _, log, run = self.lab
        result = run('--execute', '--confirm', NAME, STUB_ATTACH_ON_LIST_CALL='2',
                     STUB_XML=f'<domain><devices><disk><source file="{disk}"/></disk></devices></domain>')
        assert result.returncode != 0, result.stdout
        assert 'disk attached' in result.stderr
        assert disk.exists()
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        assert all(name not in ('virt-customize', 'virt-cat', 'guestfish')
                   for name, _ in calls(log))
        scans = [argv for name, argv in calls(log) if name == 'virsh' and 'list' in argv]
        assert len(scans) == 2

    def test_refuses_clone_referenced_in_backing_store(self):
        root, disk, _, log, run = self.lab
        xml = (f'<domain><devices><disk><source file="/different-top.qcow2"/>'
               f'<backingStore><source file="{disk}"/></backingStore>'
               f'</disk></devices></domain>')
        result = run('--check', STUB_DOMAINS='backing-domain\n', STUB_XML=xml)
        assert result.returncode != 0, result.stdout
        assert not list((root / 'clones').glob('*.personalized.qcow2'))

    def test_refuses_wrong_confirmation_and_attached_disk(self):
        _, disk, _, log, run = self.lab
        assert run('--execute', '--confirm', 'wrong').returncode != 0
        assert run('--check', STUB_DOMAINS='existing',
                   STUB_XML=f'<domain><devices><disk><source file="{disk}"/></disk></devices></domain>').returncode != 0
        assert all(name != 'virt-customize' for name, _ in calls(log))


    def test_refuses_backing_and_snapshots(self):
        _, _, _, _, run = self.lab
        assert run('--check', STUB_QEMU_INFO='{"format":"qcow2","backing-filename":"base"}').returncode != 0
        assert run('--check', STUB_QEMU_INFO='{"format":"qcow2","snapshots":[{"id":"1"}]}').returncode != 0


    def test_refuses_external_data_file_before_customization(self):
        root, _, _, log, run = self.lab
        info = json.dumps({'format': 'qcow2', 'format-specific': {
            'type': 'qcow2', 'data': {'compat': '1.1', 'data-file': 'payload.raw'}}})
        for mode in ('--check', '--execute'):
            args = (mode, '--confirm', NAME) if mode == '--execute' else (mode,)
            result = run(*args, STUB_QEMU_INFO=info)
            assert result.returncode != 0, result.stdout
            assert 'standalone' in result.stderr
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        assert all(name != 'virt-customize' for name, _ in calls(log))

    def test_refuses_all_qcow2_external_dependencies(self):
        _, _, _, _, run = self.lab
        variants = (
            {'format': 'qcow2', 'children': [{'name': 'external'}]},
            {'format': 'qcow2', 'data-file': 'external.raw'},
            {'format': 'qcow2', 'format-specific': {'data': {'has-backing-file': True}}},
        )
        for info in variants:
            with self.subTest(info=info):
                result = run('--check', STUB_QEMU_INFO=json.dumps(info))
                assert result.returncode != 0, result.stdout

    def test_refuses_symlink_disk_or_parent(self):
        root, disk, _, _, run = self.lab
        disk.unlink()
        disk.symlink_to(root / 'bases' / 'clean.qcow2')
        assert run('--check').returncode != 0
        disk.unlink()
        (root / 'clones').rename(root / 'real')
        (root / 'clones').symlink_to(root / 'real')
        assert run('--check').returncode != 0


    def test_refuses_unsafe_guest_permissions_and_inherited_host_keys(self):
        root, _, _, _, run = self.lab
        assert run('--execute', '--confirm', NAME, STUB_STAT='st_mode: 33188\nst_uid: 0\nst_gid: 0').returncode != 0
        assert run('--execute', '--confirm', NAME, STUB_SSH_FILES='ssh_host_rsa_key').returncode != 0
        assert not list((root / 'clones').glob('*.personalized.qcow2'))


    def test_refuses_stale_hosts_and_customization_must_disable_network(self):
        root, _, _, log, run = self.lab
        assert run('--execute', '--confirm', NAME,
                   STUB_HOSTS='192.168.164.102 qa-rhel9-node1.qa.stig.test\n').returncode != 0
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        customize = [argv for name, argv in calls(log) if name == 'virt-customize']
        assert customize and '--no-network' in customize[0]


    def test_refuses_deferred_selinux_relabel(self):
        root, _, _, _, run = self.lab
        assert run('--execute', '--confirm', NAME, STUB_AUTORELABEL='true').returncode != 0
        assert not list((root / 'clones').glob('*.personalized.qcow2'))


    def test_refuses_missing_virt_customize(self):
        _, _, _, log, run = self.lab
        (log.parent / 'bin' / 'virt-customize').unlink()
        result = run('--check')
        assert result.returncode != 0
        assert 'virt-customize' in result.stderr


    def test_refuses_missing_tool_and_rejects_bad_verification(self):
        root, _, _, log, run = self.lab
        assert run('--check', STUB_HOSTNAME='wrong.example').returncode == 0
        assert run('--execute', '--confirm', NAME, STUB_HOSTNAME='wrong.example').returncode != 0
        assert not list((root / 'clones').glob('*.personalized.qcow2'))
        assert any(name == 'virt-customize' for name, _ in calls(log))
