import pathlib
import tempfile
import unittest
from menu_lab import NODES, render_node, render_grub, build_menu_iso
from lab import extract, xorriso

KEY = 'ecdsa-sha2-nistp256 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== lab@test'
GUI_HASH = '$6$testsalt$' + 'a' * 86
GUI_HASHES = {'rhel8-node2': GUI_HASH, 'rhel9-node2': GUI_HASH, 'rhel10-node2': GUI_HASH}

def render_test_node(n):
    return render_node(n, KEY, GUI_HASHES.get(n.name))


class MenuLabTests(unittest.TestCase):
    def test_rhel8_has_two_stig_test_nodes_on_unique_lab_ips(self):
        nodes = [n for n in NODES if n.version == '8']
        self.assertEqual([(n.name, n.ip) for n in nodes],
                         [('rhel8-node1', '192.168.150.121'),
                          ('rhel8-node2', '192.168.150.122')])
        self.assertNotIn('rhel8-rheladm', {n.name for n in NODES})
        self.assertEqual(len({n.ip for n in NODES}), 8)
        self.assertIn('rhel8-node1.lab.test', render_test_node(NODES[0]))

    def test_rhel8_kickstarts_apply_stig_and_gui_profile_only_on_node2(self):
        for n in (n for n in NODES if n.version == '8'):
            ks = render_test_node(n)
            self.assertIn('%addon com_redhat_oscap', ks)
            self.assertIn('ssg-rhel8-ds.xml', ks)
            self.assertIn('url --url=http://192.168.150.1:8089/repo/rhel8', ks)
            self.assertIn('clearpart --all --initlabel --drives=vda', ks)
            self.assertNotIn('ansible-core', ks)
            if n.role == 'node2':
                self.assertIn('@^graphical-server-environment', ks)
                self.assertIn('content_profile_stig_gui', ks)
            else:
                self.assertIn('@^minimal-environment', ks)
                self.assertNotIn('content_profile_stig_gui', ks)

    def test_new_menu_has_eight_entries_and_matching_install_sources(self):
        from menu_lab import LABEL
        self.assertEqual(LABEL, 'RHEL-10-2-BaseOS-x86_64')
        for firmware in ('bios', 'uefi'):
            grub = render_grub(firmware)
            self.assertEqual(grub.count('menuentry '), 8)
            for n in NODES:
                self.assertIn(f'inst.ks=cdrom:/ks/{n.name}.ks', grub)
                self.assertIn(f'/rhel{n.version}/vmlinuz', grub)
            self.assertIn('inst.repo=http://192.168.150.1:8089/repo/rhel8', grub)
            self.assertIn('inst.repo=http://192.168.150.1:8089/repo/rhel9', grub)
            self.assertIn('inst.stage2=hd:LABEL=RHEL-10-2-BaseOS-x86_64', grub)

    def test_rejects_ed25519_key_for_fips_stig_guests(self):
        ed25519 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== lab@test'
        with self.assertRaisesRegex(ValueError, 'FIPS'):
            render_node(NODES[0], ed25519)

    def test_eight_nodes_have_unique_same_subnet_ips_and_names(self):
        self.assertEqual(len(NODES), 8)
        self.assertEqual(len({n.ip for n in NODES}), 8)
        self.assertEqual(len({n.name for n in NODES}), 8)
        self.assertEqual({n.version for n in NODES}, {'8', '9', '10'})
        self.assertEqual(len([n for n in NODES if n.version == '8']), 2)
        for version in ('9', '10'):
            self.assertEqual(len([n for n in NODES if n.version == version]), 3)
        for n in NODES:
            self.assertTrue(n.ip.startswith('192.168.150.'))
            self.assertTrue(n.name.startswith('rhel' + n.version + '-'))

    def test_kickstarts_set_static_address_and_allow_valid_menu_overrides(self):
        for n in NODES:
            with self.subTest(n=n.name):
                ks = render_test_node(n)
                self.assertIn('%include /tmp/lab-network.ks', ks)
                self.assertIn('lab.ip=', ks)
                self.assertIn(n.ip, ks)
                self.assertIn(n.name + '.lab.test', ks)
                self.assertIn('clearpart --all --initlabel --drives=vda', ks)
                self.assertIn('192.168.150.1', ks)
                self.assertIn('rhel9-node1.lab.test', ks)
                self.assertIn('rhel10-node1.lab.test', ks)
                self.assertIn('cdrom\n', ks) if n.version == '10' else self.assertIn(f'url --url=http://192.168.150.1:8089/repo/rhel{n.version}', ks)

    def test_gui_login_requires_hash_and_non_gui_remains_key_only(self):
        gui = next(n for n in NODES if n.version == '9' and n.role == 'node2')
        with self.assertRaisesRegex(ValueError, 'GUI password hash'):
            render_node(gui, KEY)
        hashed = '$6$testsalt$' + 'a' * 86
        ks = render_node(gui, KEY, hashed)
        self.assertIn(f'user --name=labadmin --groups=wheel --iscrypted --password="{hashed}"', ks)
        self.assertIn('rootpw --lock', ks)
        self.assertIn('user --name=labadmin --groups=wheel --lock', render_node(NODES[0], KEY))

    def test_gui_credential_file_must_be_private_and_match_password_hashes(self):
        import json
        import os
        import subprocess
        from menu_lab import load_gui_credentials
        with tempfile.TemporaryDirectory() as d:
            credentials = {}
            for name in ('rhel8-node2', 'rhel9-node2', 'rhel10-node2'):
                secret = 'ABcdef12345!LabPasswordFor' + name
                digest = subprocess.run(['openssl', 'passwd', '-6', '-salt', 'testsalteight', '-stdin'],
                                        input=secret + '\n', text=True, capture_output=True, check=True).stdout.strip()
                credentials[name] = {'password': secret, 'hash': digest}
            path = pathlib.Path(d) / 'gui-credentials.json'
            path.write_text(json.dumps(credentials))
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ValueError, '0600'):
                load_gui_credentials(path)
            os.chmod(path, 0o600)
            self.assertEqual(load_gui_credentials(path), {name: data['hash'] for name, data in credentials.items()})
            credentials['rhel9-node2']['password'] = 'IncorrectPassword12345!LabTest'
            path.write_text(json.dumps(credentials))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                load_gui_credentials(path)

    def test_node2_uses_gui_package_set_and_matching_stig_gui_profile(self):
        for n in NODES:
            with self.subTest(n=n.name):
                ks = render_test_node(n)
                packages = ks.split('%packages\n', 1)[1].split('%end', 1)[0]
                if n.role == 'node2':
                    self.assertIn('@^graphical-server-environment', packages.splitlines())
                    self.assertNotIn('@^minimal-environment', packages.splitlines())
                    self.assertIn('xccdf_org.ssgproject.content_profile_stig_gui', ks)
                    self.assertIn('systemctl --root=/mnt/sysroot set-default graphical.target', ks)
                else:
                    self.assertIn('@^minimal-environment', packages.splitlines())
                    self.assertNotIn('content_profile_stig_gui', ks)

    def test_ansible_control_nodes_have_ansible_core_package(self):
        for n in NODES:
            with self.subTest(n=n.name):
                packages = render_test_node(n).split('%packages\n', 1)[1].split('%end', 1)[0]
                self.assertEqual('ansible-core' in packages.splitlines(), n.role == 'rheladm')

    def test_gui_node_is_labeled_in_both_boot_menus(self):
        for firmware in ('bios', 'uefi'):
            grub = render_grub(firmware)
            for version in ('8', '9', '10'):
                self.assertIn(f"menuentry 'rhel{version}-node2 (192.168.150.", grub)
                self.assertIn(f"- RHEL {version} GUI (STIG GUI)'", grub)

    def test_grub_has_eight_version_specific_entries_and_no_autoboot(self):
        for firmware in ('bios', 'uefi'):
            grub = render_grub(firmware)
            self.assertEqual(grub.count('menuentry '), 8)
            self.assertIn('set timeout=-1', grub)
            for n in NODES:
                self.assertIn(f'{n.name} ({n.ip})', grub)
                self.assertIn(f'lab.ip={n.ip} lab.hostname={n.name}.lab.test', grub)
                self.assertIn(f'inst.ks=cdrom:/ks/{n.name}.ks', grub)
            self.assertIn('/rhel8/vmlinuz', grub)
            self.assertIn('/rhel9/vmlinuz', grub)
            self.assertIn('/rhel10/vmlinuz', grub)
            self.assertIn('inst.repo=http://192.168.150.1:8089/repo/rhel9', grub)
            self.assertIn('fips=1', grub)

    def test_build_cli_requires_gui_credentials_file(self):
        import subprocess
        result = subprocess.run(['python', 'menu_lab.py', '--help'], capture_output=True, text=True, check=True)
        self.assertIn('--gui-credentials', result.stdout)
        self.assertIn('--rhel8', result.stdout)

    def test_iso_build_embeds_eight_kickstarts_three_kernels_and_boot_menus(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            source = d / 'src'; (source / 'images').mkdir(parents=True)
            (source / 'EFI/BOOT').mkdir(parents=True)
            (source / 'boot/grub2').mkdir(parents=True)
            (source / 'images/eltorito.img').write_bytes(b'B' * 2048)
            (source / 'images/efiboot.img').write_bytes(b'E' * 2048)
            (source / 'EFI/BOOT/grub.cfg').write_text('set default="1"\n')
            (source / 'boot/grub2/grub.cfg').write_text('set default="1"\n')
            (source / '.treeinfo').write_text('[general]\nfamily = Red Hat Enterprise Linux\nversion = 10.2\n[variant-BaseOS]\nrepository = BaseOS\n[variant-AppStream]\nrepository = AppStream\n')
            base = d / 'base.iso'; output = d / 'menu.iso'
            xorriso('-as', 'mkisofs', '-o', base, '-V', 'RHEL-10-2-BaseOS-x86_64', '-b', 'images/eltorito.img', '-no-emul-boot', '-boot-load-size', '4', '-eltorito-alt-boot', '-e', 'images/efiboot.img', '-no-emul-boot', source)
            kernels = {}
            for v in ('8', '9', '10'):
                kernels[v] = (d / f'{v}.vmlinuz', d / f'{v}.initrd')
                kernels[v][0].write_bytes(v.encode() + b'kernel')
                kernels[v][1].write_bytes(v.encode() + b'initrd')
            build_menu_iso(base, output, kernels, KEY, GUI_HASHES)
            for n in NODES:
                embedded = extract(output, f'/ks/{n.name}.ks', d / f'{n.name}.out')
                self.assertIn(n.ip.encode(), embedded)
                if n.role == 'node2':
                    self.assertIn(GUI_HASH.encode(), embedded)
            for v in ('8', '9', '10'):
                self.assertEqual(extract(output, f'/rhel{v}/vmlinuz', d / f'kernel{v}.out'), kernels[v][0].read_bytes())
            for firmware, cfg in (('bios', '/boot/grub2/grub.cfg'), ('uefi', '/EFI/BOOT/grub.cfg')):
                grub = extract(output, cfg, d / f'{firmware}.out').decode()
                self.assertEqual(grub.count('menuentry '), 8)
            self.assertIn('El Torito', xorriso('-indev', output, '-report_el_torito', 'plain'))


if __name__ == '__main__':
    unittest.main()
