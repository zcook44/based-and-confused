"""Lab-only operator entry point tests."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from quarterly import cli


class QuarterlyCLITests(unittest.TestCase):
    def test_inventory_and_read_only_network_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['inventory']), 0)
        nodes = json.loads(output.getvalue())
        self.assertEqual(len(nodes), 8)
        self.assertEqual(nodes[0]['name'], 'qa-rhel8-node1')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(['network-xml']), 0)
        self.assertIn('<name>stigqa-lab</name>', output.getvalue())

    def test_preflight_reports_existing_lab_collisions_without_mutation(self):
        with patch.object(cli.vm, 'preflight', return_value=['domain name collision: qa-rhel9-node1']):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['preflight']), 2)

    def test_private_env_is_literal_strict_and_not_shell_evaluated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'quarterly.env'
            path.write_text('QUARTERLY_USERNAME=operator\nQUARTERLY_PASSWORD=$(id)\nQUARTERLY_ROOT_PASSWORD=root secret\nQUARTERLY_LABADMIN_PUBLIC_KEY=ecdsa-sha2-nistp256 AAAA\n')
            path.chmod(0o600)
            self.assertEqual(cli.load_credentials(path)['QUARTERLY_PASSWORD'], '$(id)')
            path.chmod(0o644)
            with self.assertRaises(cli.LabError):
                cli.load_credentials(path)
            path.chmod(0o600)
            link = Path(td) / 'link.env'; link.symlink_to(path)
            with self.assertRaises(cli.LabError):
                cli.load_credentials(link)
            path.write_text(path.read_text() + 'QUARTERLY_USERNAME=other\n')
            with self.assertRaises(cli.LabError):
                cli.load_credentials(path)

    def test_password_hash_uses_stdin_not_arguments(self):
        self.assertTrue(cli.hash_password('not logged').startswith('$6$'))

    def test_git_ignores_quarterly_secrets_and_generated_handoff(self):
        repo = Path(__file__).resolve().parents[1]
        for path in ('quarterly.env', 'quarterly/private/node.ks', 'quarterly/evidence/asset.json'):
            with self.subTest(path=path):
                result = subprocess.run(['git', 'check-ignore', '-q', path], cwd=repo)
                self.assertEqual(result.returncode, 0)

    def test_network_xml_validates_against_local_libvirt_schema(self):
        import shutil
        if shutil.which('virt-xml-validate') is None:
            self.skipTest('libvirt schema validator not installed')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'network.xml'
            path.write_text(cli.vm.render_network_xml(cli.vm.load_manifest()))
            result = subprocess.run(['virt-xml-validate', str(path), 'network'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_kickstart_command_uses_private_env_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = root / 'quarterly.env'
            env.write_text('QUARTERLY_USERNAME=operator\nQUARTERLY_PASSWORD=local-user-secret\nQUARTERLY_ROOT_PASSWORD=local-root-secret\nQUARTERLY_LABADMIN_PUBLIC_KEY=ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= lab\n')
            env.chmod(0o600)
            target = root / 'node.ks'
            args = ['kickstart', '--node', 'qa-rhel9-node2', '--env', str(env), '--out', str(target)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(args), 0)
            text = target.read_text()
            self.assertIn('@^graphical-server-environment', text)
            self.assertIn('labadmin ALL=(ALL) NOPASSWD: ALL', text)
            self.assertIn('rootpw --iscrypted $6$', text)
            self.assertNotIn('local-user-secret', text)
            self.assertNotIn('local-root-secret', text)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(args), 2)

    def test_inert_install_and_clone_plans_require_exact_names(self):
        output = io.StringIO()
        with patch.object(cli.vm, 'plan_install', return_value=['/usr/bin/python3', '/usr/sbin/virt-install', '--name', 'build-qa-rhel9-node1']) as mocked:
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['install-plan', '--node', 'qa-rhel9-node1', '--kickstart', '/safe/node.ks', '--confirm', 'build-qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'build-qa-rhel9-node1')
            self.assertIn('virt-install', output.getvalue())
        with patch.object(cli.vm, 'plan_clone', return_value={'node_name': 'qa-rhel9-node1', 'base_disk': '/sealed/base.qcow2', 'disk': '/new/clone.qcow2', 'base_sha256': 'a' * 64}) as mocked:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(['clone-plan', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'qa-rhel9-node1')
            self.assertEqual(json.loads(output.getvalue())['disk'], '/new/clone.qcow2')
        with patch.object(cli.vm, 'execute_clone', return_value='/new/clone.qcow2') as mocked:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['clone', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 0)
            self.assertEqual(mocked.call_args.kwargs['confirm'], 'qa-rhel9-node1')
        with patch.object(cli.vm, 'execute_clone', side_effect=subprocess.CalledProcessError(1, ['qemu-img', 'convert'])):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['clone', '--node', 'qa-rhel9-node1', '--base-sha256', 'a' * 64, '--confirm', 'qa-rhel9-node1']), 2)


if __name__ == '__main__':
    unittest.main()
