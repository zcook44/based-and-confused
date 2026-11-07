"""Synthetic offline candidate tests; no live libvirt or guest operation."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/quarterly-sysprep.sh'
OPS = 'machine-id,ssh-hostkeys,rh-subscription-manager,net-nmconn,net-hwaddr,dhcp-client-state,logfiles'


class SysprepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.diskroot = self.root / 'quarterly'
        for part in ('build', 'bases', 'candidates', 'bin'):
            (self.diskroot / part).mkdir(parents=True, mode=0o700)
        self.src = self.diskroot / 'build' / 'clean-rhel9.qcow2'
        subprocess.run(['/usr/sbin/qemu-img', 'create', '-f', 'qcow2', str(self.src), '1M'], check=True, capture_output=True)
        self.src.chmod(0o600)
        self.base = self.diskroot / 'bases' / 'clean-rhel9.qcow2'
        self.base.write_bytes(b'untouched base')
        self.candidate = self.diskroot / 'candidates' / 'clean-rhel9-node-candidate.qcow2'
        self.lab = self.root / 'lab.json'
        self.lab.write_text(json.dumps({'schema': 1, 'disk_root': str(self.diskroot),
            'releases': {'rhel9': {'build_disk': str(self.src), 'base_disk': str(self.base)}},
            'nodes': [{'name': 'qa-rhel9-node1', 'release': 'rhel9', 'role': 'node',
                       'disk': str(self.diskroot / 'clones' / 'qa-rhel9-node1.qcow2'),
                       'base_disk': str(self.base)}]}))
        self.log = self.root / 'calls'
        self.stub('virsh', '''#!/usr/bin/python3
import os,sys
from pathlib import Path
with open(os.environ['STUB_LOG'],'a') as f: f.write('virsh '+ ' '.join(sys.argv[1:])+'\\n')
a=sys.argv[1:]
if a[:2]!=['-c','qemu:///system']: sys.exit(2)
if a[2:]==['list','--all','--name']: print('build-qa-rhel9-node1\\nother-domain')
elif a[2:]==['domstate','build-qa-rhel9-node1']:
 with open(os.environ['STUB_LOG']) as f: count=sum(line.startswith('virsh -c qemu:///system domstate build-qa-rhel9-node1') for line in f)
 print(os.environ.get('STUB_LATE_STATE','running') if count > 1 and os.environ.get('STUB_LATE_STATE') else os.environ.get('STUB_STATE','shut off'))
elif a[2:]==['domstate','other-domain']: print(os.environ.get('STUB_OTHER_STATE','shut off'))
elif a[2:4]==['dumpxml','build-qa-rhel9-node1']:
 print('<domain><devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="'+(os.environ.get('STUB_BUILD_INACTIVE',os.environ['STUB_SRC']) if '--inactive' in a else os.environ['STUB_SRC'])+'"/></disk>'+('<disk type="file" device="cdrom"><source file="'+os.environ['STUB_BUILD_EXTRA']+'"/></disk>' if os.environ.get('STUB_BUILD_EXTRA') else '')+'</devices></domain>')
elif a[2:4]==['dumpxml','other-domain']:
 print('<domain><devices><disk type="file" device="disk"><source file="'+(os.environ.get('STUB_OTHER_INACTIVE',os.environ.get('STUB_OTHER','/not/our/disk')) if '--inactive' in a else os.environ.get('STUB_OTHER','/not/our/disk'))+'"/></disk></devices></domain>')
else: sys.exit(2)
''')
        self.stub('virt-sysprep', '''#!/usr/bin/python3
import os,sys
with open(os.environ['STUB_LOG'],'a') as f: f.write('sysprep '+ ' '.join(sys.argv[1:])+'\\n')
if sys.argv[1:]==['--list-operations']:
 for op in os.environ.get('STUB_OPS','machine-id,ssh-hostkeys,rh-subscription-manager,net-nmconn,net-hwaddr,dhcp-client-state,logfiles').split(','): print(op+'  available')
elif os.environ.get('STUB_FAIL')=='sysprep': sys.exit(1)
elif sys.argv[1:4]==['--operations',os.environ['STUB_EXPECT_OPS'],'-a']: pass
else: sys.exit(2)
''')
        self.stub('qemu-img', '''#!/usr/bin/python3
import os,subprocess,sys
with open(os.environ['STUB_LOG'],'a') as f: f.write('qemu-img '+ ' '.join(sys.argv[1:])+'\\n')
if sys.argv[1:3]==['info','--output=json']:
 import json
 info={'format':'qcow2'}
 if os.environ.get('STUB_BACKING'): info['backing-filename']='bad'
 if os.environ.get('STUB_SNAP'): info['snapshots']=[{'id':'1'}]
 if os.environ.get('STUB_INFO_EXTRA'): info.update(json.loads(os.environ['STUB_INFO_EXTRA']))
 print(json.dumps(info))
elif sys.argv[1]=='convert':
 if os.environ.get('STUB_FAIL')=='convert': sys.exit(1)
 result=subprocess.run(['/usr/sbin/qemu-img']+sys.argv[1:],capture_output=True,text=True,pass_fds=(int(sys.argv[-2].rsplit('/',1)[-1]),))
 if result.returncode:
  with open(os.environ['STUB_LOG'],'a') as f: f.write('convert-error '+result.stderr+'\\n')
 sys.exit(result.returncode)
else: sys.exit(2)
''')
        self.env = dict(os.environ, QUARTERLY_LAB_MANIFEST=str(self.lab),
                        QUARTERLY_VIRSH=str(self.diskroot / 'bin' / 'virsh'),
                        QUARTERLY_QEMU_IMG=str(self.diskroot / 'bin' / 'qemu-img'),
                        QUARTERLY_VIRT_SYSPREP=str(self.diskroot / 'bin' / 'virt-sysprep'),
                        STUB_LOG=str(self.log), STUB_SRC=str(self.src), STUB_EXPECT_OPS=OPS)

    def stub(self, name, text):
        path = self.diskroot / 'bin' / name
        path.write_text(text)
        path.chmod(0o700)

    def run_script(self, *args, **env):
        return subprocess.run(['bash', str(SCRIPT), *args], env=dict(self.env, **env),
                              capture_output=True, text=True)

    def test_check_is_read_only_and_reports_plan(self):
        result = self.run_script('--check', '--node', 'qa-rhel9-node1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['candidate_disk'], str(self.candidate))
        self.assertFalse(self.candidate.exists())
        self.assertNotIn('convert', self.log.read_text())
        self.assertNotIn('sysprep --operations', self.log.read_text())

    def test_execute_publishes_private_standalone_candidate_without_source_or_base_changes(self):
        original = self.src.read_bytes()
        result = self.run_script('--execute', '--node', 'qa-rhel9-node1', '--confirm', 'build-qa-rhel9-node1')
        self.assertEqual(result.returncode, 0, result.stderr + '\n' + self.log.read_text())
        self.assertEqual(self.src.read_bytes(), original)
        self.assertEqual(self.base.read_bytes(), b'untouched base')
        self.assertTrue(self.candidate.is_file())
        self.assertEqual(self.candidate.stat().st_mode & 0o777, 0o600)
        report = json.loads(result.stdout)
        self.assertEqual(report['operations'], OPS.split(','))
        self.assertIn('/etc/hostname', report['next_gate'])
        self.assertEqual(len(report['candidate_sha256']), 64)
        self.assertIn('sysprep --operations '+ OPS +' -a ', self.log.read_text())

    def test_execute_requires_exact_confirmation(self):
        for confirm in ([], ['--confirm', 'qa-rhel9-node1'], ['--confirm', 'build-qa-rhel9-node2']):
            with self.subTest(confirm=confirm):
                result = self.run_script('--execute', '--node', 'qa-rhel9-node1', *confirm)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.log.exists())

    def test_rejects_running_domain_and_other_domain_using_source(self):
        for env in ({'STUB_STATE': 'running'}, {'STUB_OTHER': str(self.src)}):
            with self.subTest(env=env):
                result = self.run_script('--check', '--node', 'qa-rhel9-node1', **env)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.candidate.exists())

    def test_rejects_backing_snapshots_missing_operation_and_existing_target(self):
        for env in ({'STUB_BACKING': '1'}, {'STUB_SNAP': '1'},
                    {'STUB_OPS': 'machine-id,ssh-hostkeys'}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_script('--check', '--node', 'qa-rhel9-node1', **env).returncode, 0)
        self.candidate.write_bytes(b'do not replace')
        self.assertNotEqual(self.run_script('--execute', '--node', 'qa-rhel9-node1', '--confirm', 'build-qa-rhel9-node1').returncode, 0)
        self.assertEqual(self.candidate.read_bytes(), b'do not replace')

    def test_rejects_external_qcow2_dependencies(self):
        for extra in ({'format-specific': {'type': 'qcow2', 'data': {'data-file': '/outside'}}},
                      {'format-specific': {'type': 'qcow2', 'data': {'has-backing-file': True}}},
                      {'children': [{'name': 'data-file', 'info': {'format': 'raw'}}]}):
            with self.subTest(extra=extra):
                result = self.run_script('--check', '--node', 'qa-rhel9-node1',
                                         STUB_INFO_EXTRA=json.dumps(extra))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.candidate.exists())

    def test_inspects_live_and_inactive_xml_of_every_domain(self):
        result = self.run_script('--check', '--node', 'qa-rhel9-node1',
                                 STUB_OTHER_STATE='running')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        for name in ('build-qa-rhel9-node1', 'other-domain'):
            self.assertIn('virsh -c qemu:///system dumpxml ' + name + '\n', calls)
            self.assertIn('virsh -c qemu:///system dumpxml ' + name + ' --inactive\n', calls)

    def test_rejects_inactive_xml_source_attachment_and_build_mismatch(self):
        for env in ({'STUB_OTHER_INACTIVE': str(self.src)},
                    {'STUB_OTHER_INACTIVE': str(self.src), 'STUB_OTHER_STATE': 'running'},
                    {'STUB_BUILD_INACTIVE': '/another/build.qcow2'}):
            with self.subTest(env=env):
                result = self.run_script('--check', '--node', 'qa-rhel9-node1', **env)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_alternate_and_hardlink_disk_attachments(self):
        alias = self.diskroot / 'build' / 'another.qcow2'
        os.link(self.src, alias)
        for other in (str(alias), str(self.src.parent / '..' / 'build' / self.src.name),
                      str(self.candidate.parent / '..' / 'candidates' / self.candidate.name)):
            with self.subTest(other=other):
                result = self.run_script('--check', '--node', 'qa-rhel9-node1',
                                         STUB_OTHER=other, STUB_OTHER_STATE='running')
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_build_domain_extra_attachment_to_candidate(self):
        alias = str(self.candidate.parent / '..' / 'candidates' / self.candidate.name)
        result = self.run_script('--check', '--node', 'qa-rhel9-node1', STUB_BUILD_EXTRA=alias)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_rejects_group_or_world_readable_build_source(self):
        for mode in (0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                self.src.chmod(mode)
                result = self.run_script('--check', '--node', 'qa-rhel9-node1')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.log.exists(), 'no external inspection on insecure source')

    def test_rejects_insecure_build_directory(self):
        for mode in (0o750, 0o705):
            with self.subTest(mode=oct(mode)):
                (self.diskroot / 'build').chmod(mode)
                result = self.run_script('--check', '--node', 'qa-rhel9-node1')
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.log.exists(), 'no external inspection on insecure directory')

    def test_rejects_symlink_source_and_parent_escape(self):
        target = self.root / 'outside.qcow2'
        self.src.rename(target)
        self.src.symlink_to(target)
        self.assertNotEqual(self.run_script('--check', '--node', 'qa-rhel9-node1').returncode, 0)
        self.src.unlink()
        target.rename(self.src)
        self.lab.write_text(self.lab.read_text().replace(str(self.src), str(self.root / 'outside.qcow2')))
        self.assertNotEqual(self.run_script('--check', '--node', 'qa-rhel9-node1').returncode, 0)

    def test_refuses_group_execute_source_permissions(self):
        self.src.chmod(0o610)
        result = self.run_script('--check', '--node', 'qa-rhel9-node1')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.candidate.exists())

    def test_refuses_build_domain_started_before_candidate_publication(self):
        original = self.src.read_bytes()
        result = self.run_script('--execute', '--node', 'qa-rhel9-node1',
                                 '--confirm', 'build-qa-rhel9-node1', STUB_LATE_STATE='running')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(self.src.read_bytes(), original)
        self.assertIn('sysprep --operations', self.log.read_text())

    def test_failure_leaves_no_candidate_or_partial_temporary_image(self):
        result = self.run_script('--execute', '--node', 'qa-rhel9-node1', '--confirm', 'build-qa-rhel9-node1', STUB_FAIL='sysprep')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.candidate.exists())
        self.assertEqual(list((self.diskroot / 'candidates').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
