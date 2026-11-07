import pathlib
import os
import contextlib
import shutil
import tempfile
import subprocess
import unittest

from menu_lab import NODES, render_node

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = 'ecdsa-sha2-nistp256 AAAATEST lab@test'
GUI_HASH = '$6$testsalt$' + 'a' * 86


@contextlib.contextmanager
def target_sandbox(version='9', username='labadmin'):
    """Real Bash/chmod in a user namespace; vendor tools are explicit stubs."""
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
        root = pathlib.Path(tmp)
        for directory in ('etc/selinux/targeted/contexts/files', 'etc/selinux/targeted/policy',
                          'etc/ssh', 'etc/sudoers.d', 'etc/default', 'etc/NetworkManager/system-connections',
                          'etc/sysconfig/network-scripts', f'home/{username}/.ssh', 'root', 'var', 'boot',
                          'proc/sys/crypto', 'sys', 'run', 'test-bin'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        files = {'etc/os-release': f'ID="rhel"\nVERSION_ID="{version}.8"\n',
                 'etc/selinux/config': 'SELINUX=enforcing\nSELINUXTYPE=targeted\n',
                 'etc/selinux/targeted/contexts/files/file_contexts': 'synthetic context fixture\n',
                 'etc/selinux/targeted/policy/policy.31': 'old fixture',
                 'etc/selinux/targeted/policy/policy.33': 'new fixture',
                 'etc/fstab': 'fixture fstab\n', 'etc/resolv.conf': 'nameserver 192.168.150.1\n',
                 'etc/hosts': '127.0.0.1 localhost\n', 'etc/passwd': 'fixture',
                 'etc/shadow': 'fixture', 'etc/group': 'fixture', 'etc/gshadow': 'fixture',
                 'etc/sudoers': '%wheel ALL=(ALL) ALL\n#includedir /etc/sudoers.d\n',
                 'etc/ssh/sshd_config': 'StrictModes yes\nPermitRootLogin no\n',
                 f'home/{username}/.ssh/authorized_keys': KEY + '\n',
                 'proc/sys/crypto/fips_enabled': '1\n',
                 'proc/cmdline': 'fips=1 lab.ip=192.168.150.102 lab.hostname=rhel9-node1.lab.test\n',
                 'etc/hostname': 'rhel9-node1.lab.test\n',
                 'etc/default/grub': 'GRUB_CMDLINE_LINUX="root=/dev/mapper/vg_lab-root fips=1"\n',
                 'etc/NetworkManager/system-connections/lab.nmconnection': '[connection]\nid=lab\nautoconnect=true\n[ipv4]\nmethod=manual\naddress1=192.168.150.102/24,192.168.150.1\n'}
        for path, content in files.items():
            (root / path).write_text(content)
        (root / 'etc/system-fips').touch()
        (root / 'root/grubby-info').write_text('index=0\nargs="root=/dev/mapper/vg_lab-root ro fips=1"\nindex=1\nargs="root=/dev/mapper/vg_lab-root ro fips=1"\n')
        for name, body in {
            'setfiles': 'exit "${LABEL_EXIT:-0}"',
            'matchpathcon': 'exit "${VERIFY_EXIT:-0}"',
            'chown': '[[ ${CHOWN_NOOP:-0} == 1 ]] || printf "%s\\n" "$1" > /root/key-owner',
            'chmod': '[[ ${CHMOD_NOOP:-0} == 1 ]] || exec /usr/bin/chmod "$@"',
            'stat': '''if [[ $* == '-c %u:%g '* ]]; then
  if [[ $3 == /etc/sudoers.d/* ]]; then echo '0:0'
  elif [[ -f /root/key-owner ]]; then cat /root/key-owner; else echo '0:0'; fi
else
  exec /usr/bin/stat "$@"
fi''',
            'update-crypto-policies': 'printf "%s\\n" "${TEST_CRYPTO_POLICY:-FIPS}"',
            'visudo': '''[[ $1 == -cf && -s $2 ]] || exit 3
if [[ $2 == /etc/sudoers.d/* ]]; then
  grep -qxF 'labadmin ALL=(ALL) NOPASSWD: ALL' "$2" || exit 4
fi''',
            'grubby': '''if [[ $1 == --info=ALL ]]; then
  cat /root/grubby-info
else
  [[ $* == '--update-kernel=ALL --args=fips=1' ]] || exit 99
  if [[ ${GRUBBY_NOOP:-0} == 0 ]]; then
    while IFS= read -r line; do
      if [[ $line == args=* && $line != *' fips=1'* ]]; then line=${line%\\\"}' fips=1"'; fi
      printf '%s\\n' "$line"
    done < /root/grubby-info > /root/grubby-new
    mv /root/grubby-new /root/grubby-info
  fi
fi''',
            'getent': '''if [[ $1 == passwd ]]; then
  if [[ $2 == "$TEST_ACCOUNT" ]]; then
    printf '%s:x:1000:1000::%s:/bin/bash\\n' "$2" "${TEST_HOME:-/home/$2}"
  elif [[ $2 == labadmin && -f /root/labadmin-created ]]; then
    printf 'labadmin:x:1001:1001::/home/labadmin:/bin/bash\\n'
  else exit 2; fi
elif [[ $1 == shadow && $2 == labadmin && -f /root/labadmin-created ]]; then
  printf 'labadmin:!!:1:0:99999:7:::\\n'
else exit 2; fi''',
            'useradd': '''[[ $* == '--create-home --shell /bin/bash labadmin' ]] || exit 3
mkdir -p /home/labadmin
touch /root/labadmin-created''',
            'usermod': '[[ $* == "--lock labadmin" ]]',
        }.items():
            tool = root / 'test-bin' / name
            tool.write_text('#!/bin/bash\nprintf "%s" "' + name + '" >> /root/calls\nprintf " <%s>" "$@" >> /root/calls\nprintf "\\n" >> /root/calls\n' + body + '\n')
            tool.chmod(0o755)
        script = root / 'repair.sh'
        script.write_text((ROOT / 'scripts/lab-postinstall.sh').read_text())
        command = ['bwrap', '--unshare-user', '--uid', '0', '--gid', '0',
                   '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                   '--symlink', 'usr/bin', '/sbin', '--symlink', 'usr/lib', '/lib',
                   '--symlink', 'usr/lib', '/lib64', '--dev', '/dev']
        for directory in ('etc', 'home', 'root', 'var', 'boot', 'proc', 'sys', 'run', 'test-bin'):
            command += ['--bind', str(root / directory), '/' + directory]
        command += ['--ro-bind', str(script), '/repair.sh', '--setenv', 'PATH', '/test-bin:/usr/bin',
                    '/bin/bash', '/repair.sh', username, version]
        def run(**variables):
            return subprocess.run(command, env={**os.environ, 'TEST_ACCOUNT': username, **variables},
                                  capture_output=True, text=True)
        yield root, run


class FinalPostExecutionTests(unittest.TestCase):
    def test_context_verification_uses_explicit_target_specs_and_propagates_errors(self):
        with target_sandbox() as (root, run):
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('matchpathcon <-N> <-V> <-f> </etc/selinux/targeted/contexts/files/file_contexts>',
                          (root / 'root/calls').read_text())
        for variable, code in (('LABEL_EXIT', '255'), ('VERIFY_EXIT', '1')):
            with self.subTest(variable=variable), target_sandbox() as (root, run):
                result = run(**{variable: code})
                self.assertEqual(result.returncode, int(code))
                if variable == 'LABEL_EXIT':
                    self.assertNotIn('matchpathcon', (root / 'root/calls').read_text())
                self.assertEqual((root / 'etc/sudoers').read_text(), '%wheel ALL=(ALL) ALL\n#includedir /etc/sudoers.d\n')
                self.assertEqual((root / 'etc/ssh/sshd_config').read_text(), 'StrictModes yes\nPermitRootLogin no\n')

    def test_successful_permission_commands_that_make_no_change_are_rejected(self):
        for variable in ('CHOWN_NOOP', 'CHMOD_NOOP'):
            with self.subTest(variable=variable), target_sandbox() as (root, run):
                result = run(**{variable: '1'})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('setfiles', (root / 'root/calls').read_text())

    def test_rhel8_legacy_ifcfg_profile_is_validated_and_context_checked(self):
        with target_sandbox('8') as (root, run):
            (root / 'etc/NetworkManager/system-connections/lab.nmconnection').unlink()
            path = root / 'etc/sysconfig/network-scripts/ifcfg-ens3'
            path.write_text('BOOTPROTO="none"\nONBOOT="yes"\nIPADDR="192.168.150.102"\nPREFIX="24"\n')
            result = run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('</etc/sysconfig/network-scripts/ifcfg-ens3>', (root / 'root/calls').read_text())

    def test_incomplete_hostname_connection_or_boot_customization_is_not_certified(self):
        for case in ('missing-hostname', 'wrong-hostname', 'missing-connection', 'wrong-ip',
                     'dhcp-only', 'no-autoconnect', 'missing-grub-defaults', 'missing-request'):
            with self.subTest(case=case), target_sandbox() as (root, run):
                if case == 'missing-hostname':
                    (root / 'etc/hostname').unlink()
                elif case == 'wrong-hostname':
                    (root / 'etc/hostname').write_text('localhost.localdomain\n')
                elif case == 'missing-grub-defaults':
                    (root / 'etc/default/grub').unlink()
                elif case == 'missing-request':
                    (root / 'proc/cmdline').write_text('fips=1\n')
                else:
                    path = root / 'etc/NetworkManager/system-connections/lab.nmconnection'
                    if case == 'missing-connection':
                        path.unlink()
                    else:
                        text = path.read_text()
                        text = text.replace({'wrong-ip': '192.168.150.102', 'dhcp-only': 'method=manual',
                                             'no-autoconnect': 'autoconnect=true'}[case],
                                            {'wrong-ip': '192.168.150.103', 'dhcp-only': 'method=auto',
                                             'no-autoconnect': 'autoconnect=false'}[case])
                        path.write_text(text)
                result = run()
                self.assertNotEqual(result.returncode, 0)
                calls = (root / 'root/calls').read_text() if (root / 'root/calls').exists() else ''
                self.assertNotIn('chown', calls)
                self.assertNotIn('--update-kernel', calls)
                self.assertNotIn('setfiles', calls)

    def test_fips_loss_or_weakened_boot_controls_fail_closed(self):
        for case in ('inactive', 'wrong-policy', 'missing-marker', 'fips-zero', 'selinux-zero',
                     'permissive-boot', 'empty-boot-info', 'grubby-noop'):
            with self.subTest(case=case), target_sandbox() as (root, run):
                variables = {}
                if case == 'inactive':
                    (root / 'proc/sys/crypto/fips_enabled').write_text('0\n')
                elif case == 'wrong-policy':
                    variables['TEST_CRYPTO_POLICY'] = 'DEFAULT'
                elif case == 'missing-marker':
                    (root / 'etc/system-fips').unlink()
                elif case in ('fips-zero', 'selinux-zero', 'permissive-boot'):
                    flag = {'fips-zero': 'fips=0', 'selinux-zero': 'selinux=0', 'permissive-boot': 'enforcing=0'}[case]
                    (root / 'root/grubby-info').write_text(f'index=0\nargs="root=/dev/mapper/vg_lab-root ro {flag}"\n')
                elif case == 'empty-boot-info':
                    (root / 'root/grubby-info').write_text('')
                elif case == 'grubby-noop':
                    (root / 'root/grubby-info').write_text('index=0\nargs="root=/dev/mapper/vg_lab-root ro"\n')
                    variables['GRUBBY_NOOP'] = '1'
                result = run(**variables)
                self.assertNotEqual(result.returncode, 0)
                calls = (root / 'root/calls').read_text() if (root / 'root/calls').exists() else ''
                self.assertNotIn('setfiles', calls)
                if case != 'grubby-noop':
                    self.assertNotIn('--update-kernel', calls)
                    self.assertNotIn('chown', calls)

    def test_missing_fips_boot_arguments_are_restored_on_every_entry_before_relabel(self):
        for version in ('8', '9', '10'):
            with self.subTest(version=version), target_sandbox(version) as (root, run):
                before = 'index=0\nargs="root=/dev/mapper/vg_lab-root ro crashkernel=auto"\nindex=1\nargs="root=/dev/mapper/vg_lab-root ro fips=1"\n'
                (root / 'root/grubby-info').write_text(before)
                if version == '10':
                    (root / 'etc/system-fips').unlink()
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                after = (root / 'root/grubby-info').read_text()
                self.assertEqual(after.count('fips=1'), 2)
                self.assertIn('root=/dev/mapper/vg_lab-root ro crashkernel=auto fips=1', after)
                calls = (root / 'root/calls').read_text()
                self.assertIn('grubby <--update-kernel=ALL> <--args=fips=1>', calls)
                self.assertLess(calls.index('--update-kernel'), calls.index('setfiles'))
                self.assertGreaterEqual(calls.count('grubby <--info=ALL>'), 2)
                self.assertNotIn('fips-mode-setup', calls)

    def test_unsafe_paths_or_target_configuration_fail_before_mutation(self):
        cases = ('unsafe-home', 'home-symlink', 'ssh-symlink', 'key-symlink', 'key-hardlink',
                 'missing-policy', 'permissive', 'wrong-release', 'missing-fstab')
        for case in cases:
            with self.subTest(case=case), target_sandbox() as (root, run):
                variables = {}
                if case == 'unsafe-home':
                    variables['TEST_HOME'] = '/etc'
                elif case in ('home-symlink', 'ssh-symlink', 'key-symlink'):
                    path = root / {'home-symlink': 'home/labadmin', 'ssh-symlink': 'home/labadmin/.ssh',
                                   'key-symlink': 'home/labadmin/.ssh/authorized_keys'}[case]
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    path.symlink_to('/etc')
                elif case == 'key-hardlink':
                    os.link(root / 'home/labadmin/.ssh/authorized_keys', root / 'root/linked-key')
                elif case == 'missing-policy':
                    for path in (root / 'etc/selinux/targeted/policy').glob('*'):
                        path.unlink()
                elif case == 'permissive':
                    (root / 'etc/selinux/config').write_text('SELINUX=permissive\nSELINUXTYPE=targeted\n')
                elif case == 'wrong-release':
                    (root / 'etc/os-release').write_text('ID=rhel\nVERSION_ID=10.2\n')
                elif case == 'missing-fstab':
                    (root / 'etc/fstab').unlink()
                result = run(**variables)
                self.assertNotEqual(result.returncode, 0)
                calls = (root / 'root/calls').read_text() if (root / 'root/calls').exists() else ''
                self.assertNotIn('chown', calls)
                self.assertNotIn('setfiles', calls)

    def test_all_releases_use_target_policy_and_secure_keys_before_full_relabel(self):
        for version in ('8', '9', '10'):
            with self.subTest(version=version), target_sandbox(version) as (root, run):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / 'home/labadmin').stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / 'home/labadmin/.ssh').stat().st_mode & 0o777, 0o700)
                self.assertEqual((root / 'home/labadmin/.ssh/authorized_keys').stat().st_mode & 0o777, 0o600)
                calls = (root / 'root/calls').read_text()
                self.assertIn('chown <1000:1000>', calls)
                self.assertLess(calls.index('chown'), calls.index('setfiles'))
                self.assertIn('setfiles <-F> <-m> <-c> </etc/selinux/targeted/policy/policy.33>', calls)
                self.assertIn('</etc/selinux/targeted/contexts/files/file_contexts> </>', calls)
                for path in ('/etc/fstab', '/etc/resolv.conf', '/etc/hosts', '/etc/passwd', '/etc/shadow',
                             '/etc/sudoers', '/etc/ssh/sshd_config', '/home/labadmin/.ssh/authorized_keys'):
                    self.assertIn('<' + path + '>', calls)
                self.assertNotIn('/mnt/sysroot', calls)


class FinalPostRenderingTests(unittest.TestCase):
    def test_bootstrap_custom_user_retains_separate_key_only_labadmin_with_sudo(self):
        for version in ('8', '9', '10'):
            with self.subTest(version=version), target_sandbox(version, username='operator') as (root, run):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                admin_key = root / 'home/labadmin/.ssh/authorized_keys'
                self.assertTrue(admin_key.is_file(), f'missing labadmin SSH key on RHEL {version}')
                self.assertEqual(admin_key.read_text(), KEY + '\n')
                self.assertEqual(admin_key.stat().st_mode & 0o777, 0o600)
                self.assertIn('labadmin ALL=(ALL) NOPASSWD: ALL',
                              (root / 'etc/sudoers.d/90-labadmin').read_text())
                calls = (root / 'root/calls').read_text()
                self.assertIn('useradd <--create-home> <--shell> </bin/bash> <labadmin>', calls)
                self.assertIn('usermod <--lock> <labadmin>', calls)
                self.assertIn('</home/labadmin/.ssh/authorized_keys>', calls)
                self.assertNotIn('operator ALL=(ALL) NOPASSWD',
                                 (root / 'etc/sudoers.d/90-labadmin').read_text())

    def test_every_menu_node_grants_passwordless_sudo_only_to_labadmin(self):
        for node in NODES:
            with self.subTest(node=node.name), target_sandbox(node.version) as (root, run):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                rule = root / 'etc/sudoers.d/90-labadmin'
                self.assertTrue(rule.is_file(), f'missing passwordless sudo rule for {node.name}')
                self.assertEqual(rule.read_text().splitlines()[-1], 'labadmin ALL=(ALL) NOPASSWD: ALL')
                self.assertEqual(rule.stat().st_mode & 0o777, 0o440)
                calls = (root / 'root/calls').read_text()
                self.assertIn('visudo <-cf>', calls)
                self.assertIn('</etc/sudoers.d/90-labadmin>', calls)

    def test_every_menu_node_installs_sudo_and_account_tools(self):
        for node in NODES:
            with self.subTest(node=node.name):
                ks = render_node(node, KEY, GUI_HASH if node.role == 'node2' else None)
                packages = ks.split('%packages\n', 1)[1].split('%end', 1)[0].splitlines()
                self.assertIn('sudo', packages)
                self.assertIn('shadow-utils', packages)

    def test_repair_is_last_post_after_all_role_and_scap_edits(self):
        script = ROOT / 'scripts/lab-postinstall.sh'
        for node in NODES:
            with self.subTest(node=node.name):
                ks = render_node(node, KEY, GUI_HASH if node.role == 'node2' else None)
                self.assertIn('# LAB_POSTINSTALL_BEGIN\n', ks)
                final = ks.split('# LAB_POSTINSTALL_BEGIN\n')[1]
                self.assertIn(script.read_text().rstrip(), final)
                self.assertIn(f'set -- labadmin {node.version}\n', final)
                self.assertIn('--erroronfail --interpreter=/bin/bash', final)
                self.assertEqual(final.count('%post'), 1)
                self.assertTrue(ks.endswith('# LAB_POSTINSTALL_END\nreboot --eject\n'))
                self.assertLess(ks.index('cat /tmp/lab-identity'), ks.index('# LAB_POSTINSTALL_BEGIN'))
                if node.role == 'node2':
                    self.assertLess(ks.index('set-default graphical.target'), ks.index('# LAB_POSTINSTALL_BEGIN'))
                if node.version == '10':
                    self.assertLess(ks.index('oscap xccdf eval'), ks.index('# LAB_POSTINSTALL_BEGIN'))
                self.assertIn('selinux --enforcing', ks)
                self.assertIn('bootloader --append="fips=1"', ks)
                self.assertEqual(ks.count('labadmin ALL=(ALL) NOPASSWD: ALL'), 1)
                for weakened in ('setenforce 0', 'selinux=0', '%wheel ALL=(ALL) NOPASSWD',
                                 'StrictModes no', 'StrictHostKeyChecking=no'):
                    self.assertNotIn(weakened, ks)
                posts = [part.split('%end', 1)[0] for part in ks.split('%post')[1:]]
                for post in posts:
                    body = post.split('\n', 1)[1]
                    result = subprocess.run(['bash', '-n'], input=body, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
