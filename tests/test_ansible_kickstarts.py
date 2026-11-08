"""Fresh-install Kickstarts explicitly include Ansible managed-node prerequisites."""
import pathlib
import unittest

from lab import render_kickstart
from menu_lab import NODES, render_node as render_menu_node
from quarterly import vm
from rhel7_lab import render_node as render_rhel7_node

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEY = 'ecdsa-sha2-nistp256 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== lab@test'
GUI_HASH = '$6$testsalt$' + 'a' * 86


def packages(ks):
    return ks.split('%packages\n', 1)[1].split('%end', 1)[0].splitlines()


class AnsibleKickstartPackages(unittest.TestCase):
    def assert_target_packages(self, ks, version):
        names = packages(ks)
        for package in ('sudo', 'openssh-server', 'shadow-utils'):
            self.assertEqual(names.count(package), 1, (version, package, names))
        python_package = 'python3.11' if version == '8' else 'python3'
        self.assertEqual(names.count(python_package), 1, (version, python_package, names))
        if version == '8':
            self.assertNotIn('python3', names)  # No standalone python3 RPM on 8.10 DVD.
        if version in ('9', '10'):
            for binding in ('python3-dnf', 'python3-libselinux'):
                self.assertEqual(names.count(binding), 1, (version, binding, names))
        self.assertEqual(len(names), len(set(names)))

    def test_single_dvd_renderers_and_checked_in_ks(self):
        for version in ('8', '9', '10'):
            with self.subTest(version=version):
                rendered = render_kickstart(version, 'vda', KEY)
                self.assert_target_packages(rendered, version)
                if version in ('9', '10'):
                    self.assert_target_packages((ROOT / f'data/ks/{version}.ks').read_text(), version)

    def test_every_menu_role_includes_target_runtime(self):
        for node in NODES:
            with self.subTest(node=node.name):
                ks = render_menu_node(node, KEY, GUI_HASH if node.role == 'node2' else None)
                self.assert_target_packages(ks, node.version)
                self.assertEqual('ansible-core' in packages(ks), node.role == 'rheladm')
                if node.role == 'rheladm':
                    self.assertIn('openssh-clients', packages(ks))

    def test_quarterly_clean_baselines_include_target_runtime(self):
        lab = vm.load_manifest()
        for node in lab['nodes']:
            with self.subTest(node=node['name']):
                ks = vm.render_kickstart(lab, node['name'], username='operator',
                                         password_hash='$6$salt$' + 'a' * 86,
                                         root_password_hash='$6$salt$' + 'b' * 86,
                                         ssh_public_key=KEY)
                version = node['release'].removeprefix('rhel')
                self.assert_target_packages(ks, version)
                if node['role'] == 'controller':
                    self.assertIn('openssh-clients', packages(ks))

    def test_rhel7_dvd_has_python_but_not_current_controller_compatibility(self):
        for name in ('rhel7-node1', 'rhel7-node2'):
            with self.subTest(node=name):
                self.assert_target_packages(render_rhel7_node(name, KEY), '7')


if __name__ == '__main__':
    unittest.main()
