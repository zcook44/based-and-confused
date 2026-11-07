"""Read-only quarterly VM planning contract; no hypervisor mutations."""
import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from quarterly import vm

MANIFEST = Path(__file__).resolve().parents[1] / 'quarterly' / 'lab.json'


class QuarterlyVMTests(unittest.TestCase):
    def clone_fixture(self, root):
        lab = vm.load_manifest(MANIFEST)
        lab['disk_root'] = str(root)
        node = next(n for n in lab['nodes'] if n['name'] == 'qa-rhel9-node1')
        base = root / 'bases' / (node['name'] + '-baseline.qcow2')
        target = root / 'clones' / (node['name'] + '.qcow2')
        base.parent.mkdir(mode=0o700)
        target.parent.mkdir(mode=0o700)
        subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(base), '1M'],
                       check=True, capture_output=True)
        base.chmod(0o400)
        node['base_disk'] = str(base)
        node['disk'] = str(target)
        lab['releases'][node['release']]['base_disk'] = str(base)
        return lab, node, base, target, hashlib.sha256(base.read_bytes()).hexdigest()

    def test_manifest_has_eight_node_owned_build_baseline_and_clone_paths(self):
        lab = vm.load_manifest(MANIFEST)
        root = Path(lab['disk_root'])
        self.assertEqual(len(lab['nodes']), 8)
        builds, bases = [], []
        for node in lab['nodes']:
            release = lab['releases'][node['release']]
            build_key = {'node': 'build_disk', 'gui': 'gui_build_disk',
                         'controller': 'controller_build_disk'}[node['role']]
            base_key = {'node': 'base_disk', 'gui': 'gui_base_disk',
                        'controller': 'controller_base_disk'}[node['role']]
            builds.append(release[build_key])
            bases.append(node['base_disk'])
            self.assertEqual(release[build_key], str(root / 'build' / (node['name'] + '.qcow2')))
            self.assertEqual(release[base_key], node['base_disk'])
            self.assertEqual(node['base_disk'], str(root / 'bases' / (node['name'] + '-baseline.qcow2')))
            self.assertEqual(node['disk'], str(root / 'clones' / (node['name'] + '.qcow2')))
        self.assertEqual(len(set(builds)), len(builds))
        self.assertEqual(len(set(bases)), len(bases))

    def test_manifest_rejects_shared_or_misnamed_build_and_baseline_paths(self):
        original = json.loads(MANIFEST.read_text())
        for key in ('build_disk', 'base_disk'):
            lab = json.loads(json.dumps(original))
            first = next(n for n in lab['nodes'] if n['name'] == 'qa-rhel9-node1')
            second = next(n for n in lab['nodes'] if n['name'] == 'qa-rhel10-node1')
            lab['releases'][second['release']][key] = lab['releases'][first['release']][key]
            if key == 'base_disk':
                second['base_disk'] = first['base_disk']
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'lab.json'
                path.write_text(json.dumps(lab))
                with self.assertRaises(ValueError):
                    vm.load_manifest(path)
        for key in ('build_disk', 'base_disk'):
            lab = json.loads(json.dumps(original))
            node = next(n for n in lab['nodes'] if n['name'] == 'qa-rhel9-node1')
            wrong = str(Path(lab['disk_root']) / ('build' if key == 'build_disk' else 'bases') / 'shared.qcow2')
            lab['releases']['rhel9'][key] = wrong
            if key == 'base_disk':
                node['base_disk'] = wrong
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'lab.json'
                path.write_text(json.dumps(lab))
                with self.assertRaises(ValueError):
                    vm.load_manifest(path)

    def test_manifest_rejects_shared_or_misnamed_clone_path(self):
        original = json.loads(MANIFEST.read_text())
        for disk in (next(n for n in original['nodes'] if n['name'] == 'qa-rhel10-node1')['disk'],
                     str(Path(original['disk_root']) / 'clones' / 'shared.qcow2')):
            lab = json.loads(json.dumps(original))
            node = next(n for n in lab['nodes'] if n['name'] == 'qa-rhel9-node1')
            node['disk'] = disk
            with self.subTest(disk=disk), tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'lab.json'
                path.write_text(json.dumps(lab))
                with self.assertRaises(ValueError):
                    vm.load_manifest(path)

    def test_execute_clone_real_qcow2_copy_and_no_leftover_temp(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, target, digest = self.clone_fixture(Path(d))
            self.assertEqual(vm.execute_clone(lab, node['name'], base_sha256=digest,
                                              confirm=node['name']), str(target))
            info = json.loads(subprocess.run(['qemu-img', 'info', '--output=json', str(target)],
                                             check=True, capture_output=True, text=True).stdout)
            self.assertEqual(info['format'], 'qcow2')
            self.assertFalse(info.get('backing-filename'))
            self.assertEqual(list(target.parent.iterdir()), [target])
            self.assertEqual(base.stat().st_mode & 0o222, 0)

    def test_execute_clone_delayed_destination_collision_preserves_existing_disk(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, target, digest = self.clone_fixture(Path(d))
            vm.plan_clone(lab, node['name'], base_sha256=digest, confirm=node['name'])
            def collide(args, **kwargs):
                result = subprocess.run(args, **kwargs)
                target.write_bytes(b'preexisting disk after conversion began')
                return result
            with self.assertRaises(FileExistsError):
                vm.execute_clone(lab, node['name'], base_sha256=digest,
                                 confirm=node['name'], convert_run=collide)
            self.assertEqual(target.read_bytes(), b'preexisting disk after conversion began')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_execute_clone_delayed_symlink_collision_preserves_link_and_victim(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, target, digest = self.clone_fixture(Path(d))
            victim = Path(d) / 'victim.qcow2'
            victim.write_bytes(b'untouched victim')
            vm.plan_clone(lab, node['name'], base_sha256=digest, confirm=node['name'])
            def collide(args, **kwargs):
                result = subprocess.run(args, **kwargs)
                target.symlink_to(victim)
                return result
            with self.assertRaises(FileExistsError):
                vm.execute_clone(lab, node['name'], base_sha256=digest,
                                 confirm=node['name'], convert_run=collide)
            self.assertTrue(target.is_symlink())
            self.assertEqual(victim.read_bytes(), b'untouched victim')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_execute_clone_rejects_symlink_parent_and_shared_directory(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, target, digest = self.clone_fixture(Path(d))
            target.parent.chmod(0o755)
            with self.assertRaises(ValueError):
                vm.execute_clone(lab, node['name'], base_sha256=digest, confirm=node['name'])
            target.parent.chmod(0o700)
            target.parent.rename(Path(d) / 'real-clones')
            target.parent.symlink_to(Path(d) / 'real-clones', target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                vm.execute_clone(lab, node['name'], base_sha256=digest, confirm=node['name'])
            self.assertFalse(target.exists())

    def test_execute_clone_failed_convert_removes_only_its_temp(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, target, digest = self.clone_fixture(Path(d))
            def fail(args, **kwargs):
                raise subprocess.CalledProcessError(1, args)
            with self.assertRaises(subprocess.CalledProcessError):
                vm.execute_clone(lab, node['name'], base_sha256=digest,
                                 confirm=node['name'], convert_run=fail)
            self.assertEqual(list(target.parent.iterdir()), [])
            self.assertFalse(target.exists())

    def test_gui_node2_each_release_and_explicit_fips(self):
        lab = vm.load_manifest(MANIFEST)
        for release in lab['releases']:
            self.assertIs(lab['releases'][release]['fips'], True)
            gui = [n for n in lab['nodes'] if n['release'] == release and n['name'].endswith('-node2')]
            self.assertEqual(len(gui), 1)
            self.assertEqual(gui[0]['role'], 'gui')
            self.assertNotEqual(gui[0]['base_disk'], lab['releases'][release]['base_disk'])
            self.assertEqual(gui[0]['base_disk'], lab['releases'][release]['gui_base_disk'])
            self.assertNotEqual(lab['releases'][release]['gui_build_disk'], lab['releases'][release]['build_disk'])

    def test_graphical_controller_keeps_own_base_and_build_disk(self):
        lab = vm.load_manifest(MANIFEST)
        for node in lab['nodes']:
            if node['role'] == 'controller':
                release = lab['releases'][node['release']]
                self.assertEqual(node['base_disk'], release['controller_base_disk'])
                self.assertNotEqual(node['base_disk'], release['base_disk'])
                self.assertNotEqual(release['controller_build_disk'], release['build_disk'])

    def test_kickstart_operator_root_and_separate_labadmin(self):
        lab = vm.load_manifest(MANIFEST)
        for node in lab['nodes']:
            ks = vm.render_kickstart(lab, node['name'], username='operator',
                                     password_hash='$6$salt$operatorhash',
                                     root_password_hash='$6$rootsalt$roothash',
                                     ssh_public_key='ssh-rsa AAAA operator',
                                     labadmin_ssh_public_key='ssh-rsa BBBB lab')
            self.assertIn('rootpw --iscrypted $6$rootsalt$roothash', ks)
            self.assertIn('user --name=operator --groups=wheel --iscrypted --password=$6$salt$operatorhash', ks)
            self.assertIn('user --name=labadmin --lock', ks)
            self.assertIn('sshkey --username=labadmin "ssh-rsa BBBB lab"', ks)
            self.assertIn('sshkey --username=operator "ssh-rsa AAAA operator"', ks)
            self.assertIn('bootloader --append="fips=1"', ks)
            self.assertIn('install -o root -g root -m 0440', ks)
            self.assertIn('/etc/sudoers.d/90-labadmin', ks)
            self.assertIn('labadmin ALL=(ALL) NOPASSWD: ALL', ks)
            self.assertIn('visudo -cf', ks)
            self.assertNotIn('oscap xccdf eval', ks)
            if node['name'].endswith('-node2') or node['role'] == 'controller':
                self.assertIn('@^graphical-server-environment', ks)
                self.assertIn('graphical.target', ks)
            else:
                self.assertIn('@^minimal-environment', ks)
                self.assertNotIn('graphical.target', ks)

    def test_canonical_isolated_manifest_and_network(self):
        lab = vm.load_manifest(MANIFEST)
        self.assertEqual((lab['network']['name'], lab['network']['bridge'], lab['network']['subnet'], lab['network']['gateway']),
                         ('stigqa-lab', 'virbr-stigqa', '192.168.164.0/24', '192.168.164.1'))
        self.assertEqual({n['name']: n['address'] for n in lab['nodes']}, {
            'qa-rhel8-node1': '192.168.164.121', 'qa-rhel8-node2': '192.168.164.122',
            'qa-rhel9-rheladm': '192.168.164.101', 'qa-rhel9-node1': '192.168.164.102',
            'qa-rhel9-node2': '192.168.164.103', 'qa-rhel10-rheladm': '192.168.164.111',
            'qa-rhel10-node1': '192.168.164.112', 'qa-rhel10-node2': '192.168.164.113'})
        self.assertEqual({n['fqdn'] for n in lab['nodes']}, {n['name'] + '.qa.stig.test' for n in lab['nodes']})
        self.assertEqual({n['image_kind'] for n in lab['nodes']}, {'clean'})
        self.assertTrue(all(n['disk'].startswith('/scratch/vms/quarterly/') for n in lab['nodes']))
        self.assertTrue(all(n['disk'] != n['base_disk'] for n in lab['nodes']))
        xml = ET.fromstring(vm.render_network_xml(lab))
        self.assertEqual(xml.findtext('name'), 'stigqa-lab')
        self.assertEqual(xml.find('forward').attrib['mode'], 'nat')
        self.assertEqual(xml.find('bridge').attrib['name'], 'virbr-stigqa')
        self.assertEqual(xml.find('ip').attrib['address'], '192.168.164.1')

    def test_preflight_refuses_route_network_domain_and_disk_collisions(self):
        lab = vm.load_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as d:
            lab['disk_root'] = d
            for node in lab['nodes']:
                node['disk'] = str(Path(d) / 'clones' / (node['name'] + '.qcow2'))
                node['base_disk'] = str(Path(d) / 'bases' / (node['name'] + '-baseline.qcow2'))
            outputs = {
                ('ip', '-j', '-4', 'route', 'show', 'table', 'all'): '[]',
                ('virsh', '-c', 'qemu:///system', 'net-list', '--all', '--name'): 'stig-lab\n',
                ('virsh', '-c', 'qemu:///system', 'net-dumpxml', 'stig-lab'): '<network><name>stig-lab</name><bridge name="virbr-stig"/><ip address="192.168.150.1" netmask="255.255.255.0"/></network>',
                ('virsh', '-c', 'qemu:///system', 'list', '--all', '--name'): 'rhel9-node1\n',
                ('virsh', '-c', 'qemu:///system', 'dumpxml', 'rhel9-node1'): '<domain><name>rhel9-node1</name><devices><disk><source file="/scratch/vms/old.qcow2"/></disk></devices></domain>',
            }
            calls = []
            def run(args):
                calls.append(tuple(args))
                return outputs[tuple(args)]
            self.assertEqual(vm.preflight(lab, run=run), [])
            self.assertEqual(len(calls), len(outputs))
            outputs[('ip', '-j', '-4', 'route', 'show', 'table', 'all')] = '[{"dst":"192.168.164.128/25"}]'
            self.assertTrue(any('route' in x for x in vm.preflight(lab, run=run)))
            outputs[('ip', '-j', '-4', 'route', 'show', 'table', 'all')] = '[]'
            outputs[('virsh', '-c', 'qemu:///system', 'net-dumpxml', 'stig-lab')] = '<network><name>stig-lab</name><bridge name="virbr-stigqa"/><ip address="192.168.150.1" netmask="255.255.255.0"/></network>'
            self.assertTrue(any('bridge' in x for x in vm.preflight(lab, run=run)))
            outputs[('virsh', '-c', 'qemu:///system', 'net-dumpxml', 'stig-lab')] = '<network><name>stig-lab</name><bridge name="virbr-stig"/><ip address="192.168.150.1" netmask="255.255.255.0"/></network>'
            outputs[('virsh', '-c', 'qemu:///system', 'list', '--all', '--name')] = 'qa-rhel9-node1\n'
            outputs[('virsh', '-c', 'qemu:///system', 'dumpxml', 'qa-rhel9-node1')] = '<domain><name>qa-rhel9-node1</name></domain>'
            self.assertTrue(any('domain' in x for x in vm.preflight(lab, run=run)))
            Path(lab['nodes'][0]['disk']).parent.mkdir()
            Path(lab['nodes'][0]['disk']).touch()
            self.assertTrue(any('disk' in x for x in vm.preflight(lab, run=run)))

    def test_clean_kickstarts_have_role_specific_packages_and_no_stig_baking(self):
        lab = vm.load_manifest(MANIFEST)
        key = 'ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY= lab'
        for node in lab['nodes']:
            ks = vm.render_kickstart(lab, node['name'], username='qauser', password_hash='$6$salt$hashvalue', root_password_hash='$6$rootsalt$hashvalue', ssh_public_key=key)
            self.assertIn('cdrom\n', ks)
            self.assertIn('ignoredisk --only-use=vda', ks)
            self.assertIn('clearpart --all --initlabel --drives=vda', ks)
            self.assertIn('network --bootproto=static --device=link --activate --onboot=on', ks)
            self.assertIn('--ip=' + node['address'], ks)
            self.assertIn('--hostname=' + node['fqdn'], ks)
            self.assertIn('--gateway=192.168.164.1', ks)
            self.assertIn('user --name=qauser --groups=wheel --iscrypted --password=$6$salt$hashvalue', ks)
            self.assertIn('sshkey --username=qauser', ks)
            self.assertIn('rootpw --iscrypted $6$rootsalt$hashvalue', ks)
            self.assertNotIn('%addon org_fedora_oscap', ks)
            self.assertNotIn('oscap xccdf eval', ks)
            self.assertIn('fips=1', ks)
            if node['role'] == 'controller' or node['role'] == 'gui':
                self.assertIn('@^graphical-server-environment', ks)
                if node['role'] == 'controller':
                    self.assertIn('git\n', ks)
                self.assertIn('graphical.target', ks)
            else:
                self.assertIn('@^minimal-environment', ks)
                self.assertNotIn('graphical.target', ks)
        lab['releases']['rhel9']['fips'] = False
        with self.assertRaises(ValueError):
            vm.render_kickstart(lab, 'qa-rhel9-node1', username='qauser', password_hash='$6$salt$hashvalue', root_password_hash='$6$rootsalt$hashvalue', ssh_public_key=key)

    def test_kickstart_rejects_plaintext_ed25519_and_unknown_nodes(self):
        lab = vm.load_manifest(MANIFEST)
        for kwargs in ({'username': 'bad;id', 'password_hash': '$6$salt$hashvalue', 'ssh_public_key': 'ssh-rsa AAAA'},
                       {'username': 'qauser', 'password_hash': 'plaintext', 'ssh_public_key': 'ssh-rsa AAAA'},
                       {'username': 'qauser', 'password_hash': '$6$salt$hashvalue', 'ssh_public_key': 'ssh-ed25519 AAAA'}):
            with self.assertRaises(ValueError):
                vm.render_kickstart(lab, 'qa-rhel9-node1', root_password_hash='$6$rootsalt$hashvalue', **kwargs)
        with self.assertRaises(ValueError):
            vm.render_kickstart(lab, 'rhel9-node1', username='qauser', password_hash='$6$salt$hashvalue', root_password_hash='$6$rootsalt$hashvalue', ssh_public_key='ssh-rsa AAAA')

    def test_install_plan_uses_matching_dvd_and_private_ks_not_http(self):
        lab = vm.load_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab['disk_root'] = d
            for release in lab['releases'].values():
                release['dvd'] = str(root / 'vendor.iso')
                release['kernel'] = str(root / 'installers' / 'vmlinuz')
                release['initrd'] = str(root / 'installers' / 'initrd.img')
            lab['releases']['rhel9']['build_disk'] = str(root / 'build' / 'qa-rhel9-node1.qcow2')
            lab['releases']['rhel9']['gui_build_disk'] = str(root / 'build' / 'qa-rhel9-node2.qcow2')
            (root / 'vendor.iso').touch()
            (root / 'installers').mkdir()
            for name in ('vmlinuz', 'initrd.img'):
                (root / 'installers' / name).touch()
            ks = root / 'node.ks'
            ks.write_text('cdrom\n')
            ks.chmod(0o600)
            volume = lab['releases']['rhel9']['volume_id']
            def media_run(args):
                self.assertEqual(args[:2], ['xorriso', '-indev'])
                return 'Volume Id    : ' + volume + '\n' if '-pvd_info' in args else "'initrd.img'\n'vmlinuz'\n"
            with self.assertRaises(ValueError):
                vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=media_run)
            cmd = vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=media_run, confirm='build-qa-rhel9-node1')
            self.assertEqual(cmd[:2], ['/usr/bin/python3', '/usr/sbin/virt-install'])
            self.assertIn('device=cdrom,readonly=on', ' '.join(cmd))
            self.assertIn(str(root / 'vendor.iso'), ' '.join(cmd))
            self.assertEqual(cmd[cmd.index('--os-variant') + 1], 'rhel9.8')
            self.assertIn('kernel=' + str(root / 'installers' / 'vmlinuz'), ' '.join(cmd))
            self.assertIn('initrd=' + str(root / 'installers' / 'initrd.img'), ' '.join(cmd))
            self.assertIn('inst.ks=file:/node.ks', ' '.join(cmd))
            self.assertIn('inst.stage2=hd:LABEL=' + volume, ' '.join(cmd))
            self.assertIn('inst.repo=cdrom', ' '.join(cmd))
            self.assertIn('path=' + str(root / 'build' / 'qa-rhel9-node1.qcow2'), ' '.join(cmd))
            gui_cmd = vm.plan_install(lab, 'qa-rhel9-node2', kickstart=ks, media_run=media_run, confirm='build-qa-rhel9-node2')
            self.assertIn('path=' + str(root / 'build' / 'qa-rhel9-node2.qcow2'), ' '.join(gui_cmd))
            self.assertNotIn('http:', ' '.join(cmd))
            self.assertNotIn('https:', ' '.join(cmd))
            self.assertIn('fips=1', ' '.join(cmd))
            lab['releases']['rhel9']['fips'] = False
            with self.assertRaises(ValueError):
                vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=media_run, confirm='build-qa-rhel9-node1')
            lab['releases']['rhel9']['fips'] = True
            self.assertIn('fips=1', ' '.join(vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=media_run, confirm='build-qa-rhel9-node1')))
            with self.assertRaises(ValueError):
                vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=lambda a: 'Volume Id    : WRONG\n' if '-pvd_info' in a else "'initrd.img'\n'vmlinuz'\n", confirm='build-qa-rhel9-node1')
            ks.chmod(0o644)
            with self.assertRaises(ValueError):
                vm.plan_install(lab, 'qa-rhel9-node1', kickstart=ks, media_run=media_run, confirm='build-qa-rhel9-node1')

    def test_clone_plan_requires_sealed_verified_base_and_new_owned_target(self):
        import hashlib
        lab = vm.load_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            lab['disk_root'] = d
            node = lab['nodes'][0]
            base = root / 'bases' / (node['name'] + '-baseline.qcow2')
            clone = root / 'clones' / (node['name'] + '.qcow2')
            base.parent.mkdir()
            base.write_bytes(b'qcow2 fixture, not a VM')
            base.chmod(0o444)
            node['base_disk'] = str(base)
            lab['releases']['rhel8']['base_disk'] = str(base)
            node['disk'] = str(clone)
            digest = hashlib.sha256(base.read_bytes()).hexdigest()
            def info(args):
                self.assertEqual(args, ['qemu-img', 'info', '--output=json', str(base)])
                return '{"format":"qcow2","backing-filename":null}'
            with self.assertRaises(ValueError):
                vm.plan_clone(lab, node['name'], base_sha256=digest, info_run=info, confirm='wrong')
            cmd = vm.plan_clone(lab, node['name'], base_sha256=digest, info_run=info, confirm=node['name'])
            self.assertEqual(cmd, {'node_name': node['name'], 'base_disk': str(base),
                                   'disk': str(clone), 'base_sha256': digest})
            self.assertNotIn('qemu-img', str(cmd))
            with self.assertRaises(ValueError):
                vm.plan_clone(lab, node['name'], base_sha256='0' * 64, info_run=info, confirm=node['name'])
            base.chmod(0o644)
            with self.assertRaises(ValueError):
                vm.plan_clone(lab, node['name'], base_sha256=digest, info_run=info, confirm=node['name'])
            base.chmod(0o444)
            clone.parent.mkdir()
            clone.touch()
            with self.assertRaises(ValueError):
                vm.plan_clone(lab, node['name'], base_sha256=digest, info_run=info, confirm=node['name'])
            clone.unlink()
            with self.assertRaises(ValueError):
                vm.plan_clone(lab, node['name'], base_sha256=digest, info_run=lambda a: '{"format":"qcow2","backing-filename":"untrusted.qcow2"}', confirm=node['name'])

    def test_clone_plan_rejects_external_qcow2_dependencies(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, _, _, digest = self.clone_fixture(Path(d))
            variants = (
                {'format': 'qcow2', 'format-specific': {'data': {'data-file': 'payload.raw'}}},
                {'format': 'qcow2', 'children': [{'name': 'external'}]},
                {'format': 'qcow2', 'format-specific': {'data': {'has-backing-file': True}}},
            )
            for info in variants:
                with self.subTest(info=info), self.assertRaises(ValueError):
                    vm.plan_clone(lab, node['name'], base_sha256=digest,
                                  confirm=node['name'], info_run=lambda _args: json.dumps(info))

    def test_clone_plan_rejects_real_external_data_file(self):
        with tempfile.TemporaryDirectory() as d:
            lab, node, base, _, _ = self.clone_fixture(Path(d))
            base.unlink()
            subprocess.run(['qemu-img', 'create', '-f', 'qcow2',
                            '-o', 'data_file=' + str(Path(d) / 'payload.raw'),
                            str(base), '1M'], check=True, capture_output=True)
            base.chmod(0o400)
            digest = hashlib.sha256(base.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'external dependencies'):
                vm.plan_clone(lab, node['name'], base_sha256=digest, confirm=node['name'])

    def test_preflight_fails_closed_when_inspection_fails(self):
        with self.assertRaises(vm.PreflightError):
            vm.preflight(vm.load_manifest(MANIFEST), run=lambda args: (_ for _ in ()).throw(OSError('virsh unavailable')))


if __name__ == '__main__':
    unittest.main()
