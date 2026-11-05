import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from menu_lab import load_gui_credentials


class BuildKickstartIsoTests(unittest.TestCase):
    def test_creates_private_distinct_gui_credentials_and_rejects_reuse(self):
        from build_kickstart_iso import create_gui_credentials

        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / 'gui.json'
            create_gui_credentials(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            data = json.loads(path.read_text())
            self.assertEqual(set(data), {'rhel8-node2', 'rhel9-node2', 'rhel10-node2'})
            self.assertEqual(len({entry['password'] for entry in data.values()}), 3)
            self.assertEqual(load_gui_credentials(path), {name: entry['hash'] for name, entry in data.items()})
            with self.assertRaises(FileExistsError):
                create_gui_credentials(path)

    def test_rejects_mismatched_vendor_minor_release(self):
        from build_kickstart_iso import check_dvd_versions
        from lab import xorriso
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            sources = []
            for version in ('8.10', '9.7', '10.2'):
                tree = root / ('src-' + version)
                tree.mkdir()
                (tree / '.treeinfo').write_text('[general]\nfamily = Red Hat Enterprise Linux\nversion = ' + version + '\n[variant-BaseOS]\nrepository = BaseOS\n[variant-AppStream]\nrepository = AppStream\n')
                dvd = root / (version + '.iso')
                xorriso('-as', 'mkisofs', '-o', dvd, tree)
                sources.append(dvd)
            with self.assertRaisesRegex(ValueError, '9.8'):
                check_dvd_versions(*sources)

    def test_existing_credentials_inside_git_worktree_are_rejected(self):
        from argparse import Namespace
        from build_kickstart_iso import ROOT, create_gui_credentials, validate_inputs
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory(dir=ROOT) as inside:
            root = pathlib.Path(d)
            private = root / 'gui.json'
            create_gui_credentials(private)
            misplaced = pathlib.Path(inside) / 'gui.json'
            misplaced.write_bytes(private.read_bytes())
            misplaced.chmod(0o600)
            inputs = [root / f'{v}.iso' for v in ('8', '9', '10')]
            for path in inputs:
                path.write_bytes(b'placeholder')
            key = root / 'key.pub'
            key.write_text('ecdsa-sha2-nistp256 AAAAtest test@lab\n')
            args = Namespace(rhel8=inputs[0], rhel9=inputs[1], rhel10=inputs[2],
                             ssh_key=key, gui_credentials=misplaced, out=root / 'new.iso')
            with self.assertRaisesRegex(ValueError, 'outside the Git repository'):
                validate_inputs(args)

    def test_check_only_validates_inputs_without_creating_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            from build_kickstart_iso import create_gui_credentials
            gui = root / 'gui.json'
            create_gui_credentials(gui)
            inputs = [root / f'{v}.iso' for v in ('8', '9', '10')]
            for path in inputs:
                path.write_bytes(b'placeholder')
            key = root / 'id_ecdsa.pub'
            key.write_text('ecdsa-sha2-nistp256 AAAAtest test@lab\n')
            output = root / 'new.iso'
            args = ['python', 'build_kickstart_iso.py', '--rhel8', str(inputs[0]), '--rhel9', str(inputs[1]), '--rhel10', str(inputs[2]), '--out', str(output), '--ssh-key', str(key), '--gui-credentials', str(gui), '--check-only']
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())
            output.write_bytes(b'already exists')
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), b'already exists')


if __name__ == '__main__':
    unittest.main()
