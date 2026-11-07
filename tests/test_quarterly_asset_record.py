"""Synthetic, offline lab-to-private-pipeline handoff tests."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from quarterly import cli
from quarterly import vm


class AssetRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.disk_root = self.root / 'disks'
        self.base = self.disk_root / 'bases' / 'qa-rhel9-node1-baseline.qcow2'
        self.base.parent.mkdir(parents=True)
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(self.base), '1M'],
                       check=True, capture_output=True)
        self.base.chmod(0o400)
        self.sha = hashlib.sha256(self.base.read_bytes()).hexdigest()
        self.pre = self.root / 'pre.xml'
        self.pre.write_bytes(b'<synthetic-pre-scan/>')
        self.pre.chmod(0o600)
        self.out = self.root / 'asset.json'
        lab = json.loads(vm.MANIFEST.read_text())
        lab['disk_root'] = str(self.disk_root)
        lab['nodes'] = [n for n in lab['nodes'] if n['name'] == 'qa-rhel9-node1']
        lab['nodes'][0]['base_disk'] = str(self.base)
        lab['nodes'][0]['disk'] = str(self.disk_root / 'clones' / 'qa-rhel9-node1.qcow2')
        release = lab['releases']['rhel9']
        release['base_disk'] = str(self.base)
        release['build_disk'] = str(self.disk_root / 'build' / 'qa-rhel9-node1.qcow2')
        self.manifest = self.root / 'lab.json'
        self.manifest.write_text(json.dumps(lab))
        self.lab = lab
        self.args = ['asset-record', '--node', 'qa-rhel9-node1',
                     '--cycle-id', 'Q4-2026', '--base-sha256', self.sha,
                     '--pre', str(self.pre), '--out', str(self.out)]

    def run_cli(self, args=None):
        # The in-progress lab repository is intentionally dirty until reviewed/committed.
        with patch.object(cli, '_lab_commit', return_value='a' * 40), \
             patch.object(cli, '_load_asset_manifest', side_effect=lambda path: self._fixture_manifest(path)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(args if args is not None else self.args)

    def _fixture_manifest(self, path):
        if os.path.abspath(path) != str(cli._CANONICAL_MANIFEST):
            raise cli.LabError('asset-record requires canonical quarterly/lab.json')
        return self.lab, 'a' * 40

    def test_asset_record_rejects_external_manifest_before_export(self):
        with patch.object(cli, '_lab_commit', return_value='a' * 40), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--manifest', str(self.manifest), *self.args]), 2)
        self.assertFalse(self.out.exists())

    def test_asset_record_rejects_symlink_alias_of_canonical_manifest(self):
        alias = self.root / 'canonical-alias.json'
        alias.symlink_to(cli._CANONICAL_MANIFEST)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--manifest', str(alias), *self.args]), 2)
        self.assertFalse(self.out.exists())

    def test_manifest_bytes_must_match_head_even_if_worktree_claims_clean(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            data = (b'other manifest' if 'show' in argv else
                    ('a' * 40 + '\n' if 'rev-parse' in argv else ''))
            return subprocess.CompletedProcess(argv, 0, stdout=data)
        with self.assertRaisesRegex(cli.LabError, 'manifest'):
            cli._lab_commit(manifest_bytes=b'current manifest', git_run=runner)
        self.assertTrue(any('show' in call for call in calls))

    def test_canonical_loader_rejects_symlink_before_reading(self):
        alias = self.root / 'alias.json'
        alias.symlink_to(self.manifest)
        with patch.object(cli, '_CANONICAL_MANIFEST', alias):
            with self.assertRaises((cli.LabError, OSError)):
                cli._load_asset_manifest(alias)

    def test_canonical_loader_pins_bytes_to_commit_before_validation(self):
        canonical = cli._CANONICAL_MANIFEST
        with patch.object(cli, '_lab_commit', return_value='b' * 40) as provenance:
            lab, commit = cli._load_asset_manifest(canonical)
        self.assertEqual(commit, 'b' * 40)
        self.assertEqual(lab['nodes'][0]['name'], vm.load_manifest(canonical)['nodes'][0]['name'])
        self.assertEqual(provenance.call_args.kwargs['manifest_bytes'], canonical.read_bytes())

    def test_exports_exact_contract_from_real_inputs_and_git_head(self):
        self.assertEqual(self.run_cli(), 0)
        head = 'a' * 40
        self.assertEqual(json.loads(self.out.read_text()), {
            'schema_version': 1, 'cycle_id': 'Q4-2026',
            'asset_fqdn': 'qa-rhel9-node1.qa.stig.test', 'os_release': 'rhel9.8',
            'role': 'node', 'baseline_state': 'clean', 'baseline_sha256': self.sha,
            'pre_scan_sha256': hashlib.sha256(self.pre.read_bytes()).hexdigest(),
            'lab_commit': head})
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_cli(), 2)
        self.assertEqual(json.loads(self.out.read_text())['lab_commit'], head)

    def test_rejects_missing_or_wrong_base_and_never_writes_output(self):
        for digest in ('0' * 64, self.sha.upper(), 'not-a-digest'):
            with self.subTest(digest=digest):
                self.assertEqual(self.run_cli(self.args[:self.args.index('--base-sha256') + 1] +
                    [digest] + self.args[self.args.index('--pre'):]), 2)
                self.assertFalse(self.out.exists())
        self.base.unlink()
        self.assertEqual(self.run_cli(), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_symlink_base_or_pre_and_nonregular_pre(self):
        real = self.root / 'original.qcow2'
        self.base.rename(real)
        self.base.symlink_to(real)
        self.assertEqual(self.run_cli(), 2)
        self.base.unlink()
        real.rename(self.base)
        scan = self.root / 'original.xml'
        self.pre.rename(scan)
        self.pre.symlink_to(scan)
        self.assertEqual(self.run_cli(), 2)
        self.pre.unlink()
        self.pre.mkdir()
        self.assertEqual(self.run_cli(), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_wrong_node_cycle_and_unsafe_output_parent(self):
        self.assertEqual(self.run_cli([v if v != 'qa-rhel9-node1' else 'qa-rhel9-node2' for v in self.args]), 2)
        self.assertEqual(self.run_cli([v if v != 'Q4-2026' else '../bad' for v in self.args]), 2)
        self.assertFalse(self.out.exists())
        bad = self.root / 'writable'
        bad.mkdir(mode=0o777)
        bad.chmod(0o777)
        self.assertEqual(self.run_cli(self.args[:-1] + [str(bad / 'asset.json')]), 2)
        self.assertFalse((bad / 'asset.json').exists())

    def test_rejects_manifest_base_path_with_symlink_parent(self):
        actual = self.root / 'actual'
        self.base.parent.rename(actual)
        self.base.parent.symlink_to(actual, target_is_directory=True)
        self.assertEqual(self.run_cli(), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_public_or_foreign_owned_inputs(self):
        for path, mode in ((self.base, 0o404), (self.pre, 0o644), (self.pre, 0o640)):
            with self.subTest(path=path.name, mode=mode):
                original = path.stat().st_mode & 0o777
                path.chmod(mode)
                try:
                    self.assertEqual(self.run_cli(), 2)
                    self.assertFalse(self.out.exists())
                finally:
                    path.chmod(original)
        with patch.object(cli.os, 'geteuid', return_value=os.geteuid() + 1):
            self.assertEqual(self.run_cli(), 2)
            self.assertFalse(self.out.exists())

    def test_rejects_non_qcow2_even_when_digest_matches(self):
        self.base.chmod(0o600)
        self.base.write_bytes(b'not a qcow2 image')
        self.base.chmod(0o400)
        digest = hashlib.sha256(self.base.read_bytes()).hexdigest()
        args = [digest if value == self.sha else value for value in self.args]
        self.assertEqual(self.run_cli(args), 2)
        self.assertFalse(self.out.exists())

    def test_rejects_external_data_file_baseline(self):
        self.base.unlink()
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2',
                        '-o', 'data_file=' + str(self.root / 'payload.raw'),
                        str(self.base), '1M'], check=True, capture_output=True)
        self.base.chmod(0o400)
        digest = hashlib.sha256(self.base.read_bytes()).hexdigest()
        args = [digest if value == self.sha else value for value in self.args]
        self.assertEqual(self.run_cli(args), 2)
        self.assertFalse(self.out.exists())

    def test_real_dirty_worktree_cannot_publish(self):
        repo = Path(cli.__file__).resolve().parents[1]
        if not subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain=v1',
                                        '--untracked-files=all'], text=True).strip():
            self.skipTest('checkout is clean')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(self.args), 2)
        self.assertFalse(self.out.exists())

    def test_git_provenance_ignores_overrides_and_rejects_dirty_status(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            output = 'a' * 40 + '\n' if 'rev-parse' in argv else ''
            return subprocess.CompletedProcess(argv, 0, stdout=output)
        with patch.dict(os.environ, {'GIT_DIR': '/elsewhere', 'GIT_WORK_TREE': '/elsewhere',
                                     'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare',
                                     'GIT_CONFIG_VALUE_0': 'true'}):
            self.assertEqual(cli._lab_commit(git_run=runner), 'a' * 40)
        self.assertEqual(len(calls), 2)
        for argv, kwargs in calls:
            self.assertEqual(argv[:3], ['git', '-C', str(Path(cli.__file__).resolve().parents[1])])
            self.assertFalse(any(key.startswith('GIT_') for key in kwargs['env']))
        self.assertIn('--untracked-files=all', calls[1][0])
        for change in ('?? new.txt\n', ' M quarterly/cli.py\n', 'M  quarterly/cli.py\n'):
            with self.subTest(change=change):
                def dirty(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, stdout='a' * 40 + '\n' if 'rev-parse' in argv else change)
                with self.assertRaises(cli.LabError):
                    cli._lab_commit(git_run=dirty)


if __name__ == '__main__':
    unittest.main()
