import os
import pathlib
import re
import subprocess
import tempfile
import unittest

from menu_lab import NODES, render_node

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = 'ecdsa-sha2-nistp256 AAAATEST lab@test'
GUI_HASH = '$6$testsalt$' + 'a' * 86
BLOCK = r'(?ms)^# LAB_POSTINSTALL_BEGIN\n.*?^# LAB_POSTINSTALL_END\n'


class PostSourceTests(unittest.TestCase):
    def prepare(self, source, version='9', username='operator'):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = pathlib.Path(tmp)
            ks = root / 'source.ks'; ks.write_text(source)
            output = root / 'output.ks'
            result = subprocess.run(['python3', str(ROOT / 'menu_lab.py'), 'postinstall',
                                     '--username', username, '--version', version,
                                     '--source', str(ks), '--out', str(output)],
                                    capture_output=True, text=True, cwd=root)
            return result, output.read_text() if output.exists() else None

    def test_source_render_adds_or_replaces_final_repair_after_late_source_edits(self):
        for node in NODES:
            current = render_node(node, KEY, GUI_HASH if node.role == 'node2' else None)
            old = re.sub(BLOCK, '', current)
            for source in (old, current, current.replace('reboot --eject', '%post\necho source-edit\n%end\nreboot --eject')):
                with self.subTest(node=node.name, existing=source != old):
                    result, output = self.prepare(source, node.version)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    assert output is not None
                    self.assertEqual(output.count('# LAB_POSTINSTALL_BEGIN\n'), 1)
                    self.assertIn(f'set -- operator {node.version}\n', output)
                    self.assertTrue(output.endswith('# LAB_POSTINSTALL_END\nreboot --eject\n'))
                    self.assertEqual(re.sub(BLOCK, '', output), re.sub(BLOCK, '', source))
                    again, repeated = self.prepare(output, node.version)
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertEqual(output, repeated)

    def test_render_rejects_unsafe_identity_and_malformed_source_before_output(self):
        source = render_node(NODES[0], KEY)
        cases = [(source, '9', 'bad;id'), (source, '9', 'root'), (source, '11', 'operator'),
                 (source.replace('reboot --eject', ''), '9', 'operator'),
                 (source + 'reboot --eject\n', '9', 'operator'),
                 (source.replace('# LAB_POSTINSTALL_END', '# broken'), '9', 'operator'),
                 (source.replace('# LAB_POSTINSTALL_BEGIN', '# broken'), '9', 'operator'),
                 (source + '%post\necho too-late\n%end\n', '9', 'operator')]
        for source, version, username in cases:
            with self.subTest(version=version, username=username):
                result, output = self.prepare(source, version, username)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(output)

    def test_source_cli_rejects_symlink_or_in_place_outputs_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            root = pathlib.Path(tmp)
            source = root / 'source.ks'; source.write_text(render_node(NODES[0], KEY))
            victim = root / 'victim'; victim.write_text('must stay unchanged\n')
            link = root / 'output.ks'; link.symlink_to(victim)
            for output in (link, source):
                result = subprocess.run(['python3', str(ROOT / 'menu_lab.py'), 'postinstall',
                                         '--source', str(source), '--out', str(output),
                                         '--version', '9', '--username', 'operator'], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
            self.assertEqual(victim.read_text(), 'must stay unchanged\n')
            self.assertIn('set -- labadmin 9\n', source.read_text())

    def test_lab_prepare_integrates_shared_repair_into_old_and_new_iso_sources(self):
        node = next(n for n in NODES if n.name == 'rhel9-node1')
        current = render_node(node, KEY)
        for source in (re.sub(BLOCK, '', current), current):
            with self.subTest(existing=source == current), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                root = pathlib.Path(tmp)
                bindir = root / 'bin'; bindir.mkdir()
                fixture = root / 'embedded.ks'; fixture.write_text(source)
                iso = root / 'source.iso'; iso.write_bytes(b'not an ISO; extraction is stubbed')
                tool = bindir / 'xorriso'
                tool.write_text('#!/bin/bash\ncp -- "$TEST_SOURCE_KS" "${@: -1}"\n')
                tool.chmod(0o755)
                envfile = root / 'lab.env'
                envfile.write_text('LAB_USERNAME=operator\nLAB_PASSWORD=Synthetic-user-credential-123\nLAB_ROOT_PASSWORD=Synthetic-root-credential-456\n')
                envfile.chmod(0o600)
                env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                       'LAB_ENV_FILE': str(envfile), 'LAB_ISO': str(iso),
                       'LAB_VM_DIR': str(root / 'vms'), 'TEST_SOURCE_KS': str(fixture)}
                result = subprocess.run(['bash', str(ROOT / 'lab-vms.sh'), 'prepare', node.name],
                                        env=env, cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = root / 'vms' / (node.name + '.ks')
                text = output.read_text()
                self.assertIn('set -- operator 9\n', text)
                self.assertEqual(text.count('# LAB_POSTINSTALL_BEGIN\n'), 1)
                self.assertIn('sshkey --username=operator', text)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                for secret in ('Synthetic-user-credential-123', 'Synthetic-root-credential-456'):
                    self.assertNotIn(secret, text + result.stdout + result.stderr)
