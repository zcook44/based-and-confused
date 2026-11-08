import pathlib
import unittest
from rhel7_lab import render_node

KEY = 'ecdsa-sha2-nistp256 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== lab@test'


class RHEL7LabTests(unittest.TestCase):
    def test_headless_uses_dvd_stig_addon_and_installs_scanner(self):
        ks = render_node('rhel7-node1', KEY)
        self.assertIn('%addon org_fedora_oscap', ks)
        self.assertIn('profile = xccdf_org.ssgproject.content_profile_stig', ks)
        self.assertIn('openscap-scanner\n', ks)
        self.assertIn('scap-security-guide\n', ks)
        self.assertIn('cdrom\n', ks)
        self.assertIn('ignoredisk --only-use=vda', ks)
        self.assertIn('clearpart --all --initlabel --drives=vda', ks)
        self.assertIn('192.168.150.131', ks)
        self.assertIn('rootpw --lock', ks)
        self.assertIn('user --name=labadmin --groups=wheel --lock', ks)
        self.assertIn('LAB_PUBLIC_KEY', ks)
        self.assertIn(KEY, ks)
        self.assertNotIn('@^graphical-server-environment', ks)

    def test_gui_retains_xorg_with_explicit_stig_tailoring_and_remediates(self):
        ks = render_node('rhel7-node2', KEY)
        self.assertIn('@^graphical-server-environment', ks)
        self.assertIn('graphical.target', ks)
        self.assertIn('192.168.150.132', ks)
        self.assertIn('xccdf_org.ssgproject.content_rule_package_xorg-x11-server-common_removed', ks)
        self.assertIn('selected="false"', ks)
        self.assertIn('oscap xccdf eval --remediate', ks)
        self.assertIn('id="xccdf_org.lab_tailoring_rhel7_gui"', ks)
        self.assertIn('<xccdf:version time="2024-01-01T00:00:00Z">1</xccdf:version>', ks)
        self.assertIn('id="xccdf_org.lab_profile_rhel7_gui"', ks)
        self.assertIn('--tailoring-file /root/rhel7-gui-tailoring.xml', ks)
        self.assertNotIn('%addon org_fedora_oscap', ks)

    def test_both_rhel7_kickstarts_install_only_labadmin_sudo_exception_last(self):
        for name in ('rhel7-node1', 'rhel7-node2'):
            with self.subTest(name=name):
                ks = render_node(name, KEY)
                self.assertEqual(ks.count('labadmin ALL=(ALL) NOPASSWD: ALL'), 1)
                self.assertIn('visudo -cf "$sudo_tmp"', ks)
                self.assertIn('visudo -c', ks)
                self.assertIn('chmod 0440 "$sudo_tmp"', ks)
                self.assertIn('restorecon /etc/sudoers.d/90-labadmin', ks)
                self.assertIn('if [ "$LAB_ACCOUNT" != labadmin ]; then', ks)
                self.assertIn('useradd --create-home --shell /bin/bash labadmin', ks)
                self.assertNotIn('%wheel ALL=(ALL) NOPASSWD', ks)
                self.assertLess(ks.index('LAB_HOSTS\n'), ks.index('labadmin ALL=(ALL) NOPASSWD: ALL'))
                self.assertLess(ks.index('labadmin ALL=(ALL) NOPASSWD: ALL'), ks.index('reboot --eject'))

    def test_rejects_unknown_nodes_and_non_fips_key(self):
        for name, key in [('rhel7-unknown', KEY), ('rhel7-node1', 'ssh-ed25519 abc user')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_node(name, key)


if __name__ == '__main__':
    unittest.main()
