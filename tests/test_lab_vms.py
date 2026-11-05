import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'lab-vms.sh'
NAMES = (
    'rhel9-rheladm', 'rhel9-node1', 'rhel9-node2',
    'rhel10-rheladm', 'rhel10-node1', 'rhel10-node2',
    'rhel8-node1', 'rhel8-node2',
)


class LabVMsTests(unittest.TestCase):
    def run_lab(self, *args, existing='', env_mode=0o600, username='autolab', use_default_kernels=False):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            virsh = bin_dir / 'virsh'
            virsh.write_text('''#!/bin/bash
if [[ "$1" == -c ]]; then shift 2; fi
case "$1" in
  dominfo) [[ "$2" == "$LAB_TEST_EXISTING" ]] ;;
  domstate) echo 'shut off' ;;
  net-info) printf 'Active: yes\\n' ;;
  start|shutdown) printf '%s %s\\n' "$1" "$2" >> "$LAB_TEST_LOG" ;;
  *) echo "unexpected virsh invocation: $*" >&2; exit 97 ;;
esac
''')
            virsh.chmod(0o755)
            env_file = root / 'lab-vms.env'
            env_file.write_text(f'LAB_USERNAME={username}\nLAB_PASSWORD=TestUserPassphrase123!\nLAB_ROOT_PASSWORD=TestRootPassphrase456!\n')
            env_file.chmod(env_mode)
            env = os.environ.copy()
            env.update(PATH=str(bin_dir) + os.pathsep + env['PATH'],
                       LAB_VM_DIR=str(root / 'vms'),
                       LAB_ENV_FILE=str(env_file),
                       LAB_TEST_EXISTING=existing, LAB_TEST_LOG=str(root / 'calls'))
            if use_default_kernels:
                env.pop('LAB_KERNEL_DIR', None)
            else:
                env['LAB_KERNEL_DIR'] = str(root / 'kernels')
            result = subprocess.run(['bash', str(SCRIPT), *args], env=env, cwd=ROOT,
                                    capture_output=True, text=True)
            calls = (root / 'calls').read_text() if (root / 'calls').exists() else ''
            created = list((root / 'vms').rglob('*')) if (root / 'vms').exists() else []
            rendered = {p.name: p.read_text() for p in (root / 'vms').rglob('*.ks')} if (root / 'vms').exists() else {}
            return result, calls, created, rendered

    def test_up_all_dry_run_plans_eight_distinct_automatic_installs_without_writes(self):
        result, calls, created, rendered = self.run_lab('--dry-run', 'up', 'all')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in NAMES:
            self.assertIn(f'--name {name}', result.stdout)
            self.assertIn(f'inst.ks=file:/{name}.ks', result.stdout)
            self.assertIn(name + '.qcow2', result.stdout)
        self.assertEqual(result.stdout.count('virt-install --connect'), 8)
        self.assertNotIn('--name rhel8-rheladm', result.stdout)
        self.assertIn('inst.repo=http://192.168.150.1:8089/repo/rhel8', result.stdout)
        self.assertIn('inst.repo=http://192.168.150.1:8089/repo/rhel9', result.stdout)
        self.assertIn('RHEL-10-2-BaseOS-x86_64', result.stdout)
        self.assertIn('inst.repo=cdrom', result.stdout)
        self.assertNotIn('TestUserPassphrase123!', result.stdout + result.stderr)
        self.assertNotIn('TestRootPassphrase456!', result.stdout + result.stderr)
        self.assertEqual(calls, '')
        self.assertEqual(created, [])

    def test_default_kernel_cache_is_versioned_for_new_media(self):
        result, calls, created, rendered = self.run_lab('--dry-run', 'up', 'rhel9-node1', use_default_kernels=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('.installer-kernels-8.10-9.8-10.2/rhel9/vmlinuz', result.stdout)
        self.assertEqual(created, [])

    def test_prepare_renders_private_kickstart_with_env_identity_and_hashes_only(self):
        result, calls, created, rendered = self.run_lab('prepare', 'rhel9-node1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, '')
        ks = rendered['rhel9-node1.ks']
        self.assertIn('user --name=autolab --groups=wheel --iscrypted --password="$6$', ks)
        self.assertIn('rootpw --iscrypted "$6$', ks)
        self.assertIn('sshkey --username=autolab', ks)
        self.assertNotIn('TestUserPassphrase123!', ks + result.stdout + result.stderr)
        self.assertNotIn('TestRootPassphrase456!', ks + result.stdout + result.stderr)
        self.assertTrue(any(p.name == 'rhel9-node1.ks' for p in created))

    def test_insecure_env_file_is_refused_before_changes(self):
        result, calls, created, rendered = self.run_lab('prepare', 'rhel9-node1', env_mode=0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('0600', result.stderr)
        self.assertEqual(created, [])

    def test_invalid_username_is_refused_without_running_env_as_shell(self):
        result, calls, created, rendered = self.run_lab('prepare', 'rhel9-node1', username='bad;touch /scratch/vms/pwn')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('username', result.stderr.lower())
        self.assertEqual(created, [])

    def test_selection_rejects_unknown_name_before_changes(self):
        result, calls, created, rendered = self.run_lab('up', 'not-a-lab-node')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown node', result.stderr)
        self.assertEqual(calls, '')
        self.assertEqual(created, [])

    def test_start_existing_node_does_not_create_disk_or_reinstall(self):
        result, calls, created, rendered = self.run_lab('start', 'rhel9-node1', existing='rhel9-node1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('start rhel9-node1', calls)
        self.assertEqual(created, [])

    def test_start_missing_node_fails_instead_of_creating_it(self):
        result, calls, created, rendered = self.run_lab('start', 'rhel9-node1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not defined', result.stderr)
        self.assertEqual(calls, '')
        self.assertEqual(created, [])


if __name__ == '__main__':
    unittest.main()
