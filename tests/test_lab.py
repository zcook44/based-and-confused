import pathlib
import tempfile
import unittest

from lab import render_kickstart, patch_boot_config, inspect_treeinfo, make_iso, extract
import subprocess

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== lab@test'


class LabTests(unittest.TestCase):
    def test_rhel9_kickstart_is_stig_aligned_and_disk_scoped(self):
        text = render_kickstart('9', 'vda', KEY)
        self.assertIn('profile = xccdf_org.ssgproject.content_profile_stig', text)
        self.assertIn('clearpart --all --initlabel --drives=vda', text)
        self.assertIn('ignoredisk --only-use=vda', text)
        self.assertIn('bootloader --append="fips=1"', text)
        self.assertIn('sshkey --username=labadmin "' + KEY + '"', text)
        self.assertIn('logvol /var/log/audit ', text)
        self.assertIn('ssg-rhel9-ds.xml', text)
        self.assertNotIn('rootpw --plaintext', text)

    def test_rhel10_uses_matching_datastream_without_removed_addon(self):
        text = render_kickstart('10', 'sda', KEY)
        self.assertIn('ssg-rhel10-ds.xml', text)
        self.assertIn('oscap xccdf eval --remediate', text)
        self.assertNotIn('%addon com_redhat_oscap', text)

    def test_rejects_bad_disk_key_and_version(self):
        for args in [('11', 'vda', KEY), ('9', '/dev/vda', KEY), ('9', 'vda', 'evil\nclearpart --all')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                render_kickstart(*args)

    def test_patch_boot_config_adds_inst_ks_and_fips_to_install_only(self):
        bios = ('default vesamenu.c32\ntimeout 600\nlabel linux\nmenu label Install\n'
                'append initrd=initrd.img inst.stage2=hd:LABEL=RHEL-9 quiet\n'
                'label check\nmenu label Test media\nmenu default\n'
                'append initrd=initrd.img inst.stage2=hd:LABEL=RHEL-9 rd.live.check quiet\n'
                'label rescue\nappend initrd=initrd.img inst.stage2=hd:LABEL=RHEL-9 inst.rescue\n')
        efi = ('set default="1"\nset timeout=60\nmenuentry "Install" {\n'
               ' linuxefi /vmlinuz inst.stage2=hd:LABEL=RHEL-9 fips=1\n}\n'
               'menuentry "Rescue" {\n linuxefi /vmlinuz inst.stage2=hd:LABEL=RHEL-9 inst.rescue\n}\n')
        b = patch_boot_config(bios, 'bios')
        e = patch_boot_config(efi, 'efi')
        self.assertIn('inst.ks=cdrom:/ks.cfg fips=1', b)
        self.assertIn('inst.ks=cdrom:/ks.cfg', e)
        self.assertNotIn('fips=1 fips=1', e)
        self.assertIn('set default="0"', e)
        self.assertIn('menu default', b.split('label check')[0])
        self.assertNotIn('menu default', b.split('label check')[1])
        self.assertNotIn('inst.rescue inst.ks=', b + e)
        with self.assertRaises(ValueError):
            patch_boot_config('menuentry unrelated', 'efi')

    def test_iso_remaster_preserves_boot_and_embeds_kickstart(self):
        for version in ('9', '10'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as root:
                root = pathlib.Path(root)
                tree = root / 'tree'
                (tree / 'isolinux').mkdir(parents=True)
                (tree / 'EFI/BOOT').mkdir(parents=True)
                (tree / '.treeinfo').write_text(f'[general]\nfamily = Red Hat Enterprise Linux\nversion = {version}.6\n[repository-BaseOS]\n[repository-AppStream]\n')
                if version == '9':
                    (tree / 'isolinux/isolinux.bin').write_bytes(b'B' * 2048)
                    (tree / 'isolinux/isolinux.cfg').write_text('append initrd=initrd.img inst.stage2=hd:LABEL=RHEL quiet\n')
                    bios_boot = 'isolinux/isolinux.bin'
                else:
                    (tree / 'images').mkdir()
                    (tree / 'boot/grub2').mkdir(parents=True)
                    (tree / 'images/eltorito.img').write_bytes(b'B' * 2048)
                    (tree / 'boot/grub2/grub.cfg').write_text('set default="1"\n linux /vmlinuz inst.stage2=hd:LABEL=RHEL quiet\n')
                    bios_boot = 'images/eltorito.img'
                (tree / 'EFI/BOOT/grub.cfg').write_text(' linuxefi /vmlinuz inst.stage2=hd:LABEL=RHEL quiet\n')
                source, output, ks = root / 'vendor.iso', root / 'lab.iso', root / f'{version}.ks'
                ks.write_text(render_kickstart(version, 'vda', KEY))
                subprocess.run(['xorriso', '-as', 'mkisofs', '-o', str(source), '-b', bios_boot, '-no-emul-boot', '-boot-load-size', '4', str(tree)], check=True, capture_output=True)
                make_iso(version, source, ks, output)
                self.assertEqual(extract(output, '/ks.cfg', root / 'ks-extracted'), ks.read_bytes())
                self.assertIn(b'inst.ks=cdrom:/ks.cfg fips=1', extract(output, '/EFI/BOOT/grub.cfg', root / 'efi-extracted'))
                if version == '10':
                    bios_cfg = extract(output, '/boot/grub2/grub.cfg', root / 'bios-extracted')
                    self.assertIn(b'inst.ks=cdrom:/ks.cfg fips=1', bios_cfg)
                    self.assertIn(b'set default="0"', bios_cfg)
                self.assertTrue(output.exists())

    def test_real_productmd_variant_treeinfo_is_full_dvd(self):
        inspect_treeinfo('[general]\nfamily = Red Hat Enterprise Linux\nversion = 10.1\nvariants = AppStream,BaseOS\n[variant-AppStream]\nrepository = AppStream\n[variant-BaseOS]\nrepository = BaseOS\n', '10')

    def test_grub2_bios_uses_same_grub_config_as_uefi(self):
        grub = "menuentry 'Install RHEL 10' {\n  linuxefi /vmlinuz inst.stage2=hd:LABEL=RHEL quiet\n}\n"
        self.assertIn('inst.ks=cdrom:/ks.cfg fips=1', patch_boot_config(grub, 'efi'))

    def test_treeinfo_rejects_wrong_major_and_boot_only_media(self):
        with self.assertRaises(ValueError):
            inspect_treeinfo('[general]\nfamily = Red Hat Enterprise Linux\nversion = 9.6\n', '10')
        with self.assertRaises(ValueError):
            inspect_treeinfo('[general]\nfamily = Red Hat Enterprise Linux\nversion = 9.6\n', '9')
        inspect_treeinfo('[general]\nfamily = Red Hat Enterprise Linux\nversion = 9.6\n[repository-BaseOS]\n[repository-AppStream]\n', '9')


if __name__ == '__main__':
    unittest.main()
