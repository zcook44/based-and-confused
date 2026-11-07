import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from lab import xorriso

ROOT = Path(__file__).resolve().parents[1]


def fixture(root: Path):
    media = root / 'media'
    media.mkdir()
    hashes = []
    for major, version in (('8', '8.10'), ('9', '9.8'), ('10', '10.2')):
        tree = root / ('src' + major)
        (tree / 'BaseOS/repodata').mkdir(parents=True)
        (tree / 'AppStream/repodata').mkdir(parents=True)
        (tree / 'images/pxeboot').mkdir(parents=True)
        (tree / '.treeinfo').write_text('[general]\nfamily = Red Hat Enterprise Linux\nversion = ' + version + '\n[variant-BaseOS]\nrepository = BaseOS\n[variant-AppStream]\nrepository = AppStream\n')
        for path in ('BaseOS/repodata/repomd.xml', 'AppStream/repodata/repomd.xml'):
            (tree / path).write_text('metadata')
        (tree / 'images/pxeboot/vmlinuz').write_bytes(b'kernel')
        (tree / 'images/pxeboot/initrd.img').write_bytes(b'initrd')
        (tree / 'images/install.img').write_bytes(b'stage2')
        iso = media / f'rhel-{version}-x86_64-dvd.iso'
        if major == '10':
            (tree / 'images/eltorito.img').write_bytes(b'B' * 2048)
            (tree / 'images/efiboot.img').write_bytes(b'E' * 2048)
            (tree / 'EFI/BOOT').mkdir(parents=True)
            (tree / 'boot/grub2').mkdir(parents=True)
            (tree / 'EFI/BOOT/grub.cfg').write_text('set default="1"\n')
            (tree / 'boot/grub2/grub.cfg').write_text('set default="1"\n')
            xorriso('-as', 'mkisofs', '-o', iso, '-V', 'RHEL-10-2-BaseOS-x86_64',
                    '-b', 'images/eltorito.img', '-no-emul-boot', '-boot-load-size', '4',
                    '-eltorito-alt-boot', '-e', 'images/efiboot.img', '-no-emul-boot', tree)
        else:
            xorriso('-as', 'mkisofs', '-o', iso, '-V', f'TEST-{version}', tree)
        hashes.append(f'{hashlib.sha256(iso.read_bytes()).hexdigest()}  {iso.name}')
    checksums = root / 'vendor.sha256'
    checksums.write_text('\n'.join(hashes) + '\n')
    return media, checksums


class SetupLabTests(unittest.TestCase):
    def test_compose_uses_operator_repository_dir(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, LAB_REPO_DIR=str(Path(d) / 'repo'))
            result = subprocess.run(['docker', 'compose', '-f', 'compose.repo.yaml', 'config', '--format', 'json'],
                                    cwd=ROOT, env=env, capture_output=True, text=True, check=True)
            config = json.loads(result.stdout)
            mounts = config['services']['repo']['volumes']
            self.assertIn(str(Path(d) / 'repo'), [m['source'] for m in mounts])

    def run_setup(self, root, *args):
        return subprocess.run(['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                               '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256'),
                               *args], cwd=ROOT, capture_output=True, text=True)

    def test_check_validates_three_dvds_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            result = self.run_setup(root, 'check')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'repo').exists())
            (root / 'media/rhel-9.8-x86_64-dvd.iso').write_bytes(b'corrupted')
            result = self.run_setup(root, 'check')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'repo').exists())

    def test_check_refuses_insufficient_space_before_any_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fake = root / 'bin'
            fake.mkdir()
            df = fake / 'df'
            df.write_text('#!/bin/sh\ncase "$*" in *-PB1*) printf "Filesystem 1B-blocks Used Available Use%% Mounted on\\nmock 100 99 1 99%% /\\n" ;; *) /usr/bin/df "$@" ;; esac\n')
            df.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'])
            args = ['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                    '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256'), 'check']
            result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Insufficient free space', result.stderr)
            self.assertFalse((root / 'repo').exists())

    def test_check_refuses_an_existing_route_before_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fake = root / 'bin'
            fake.mkdir()
            (fake / 'virsh').write_text('#!/bin/sh\ncase "$*" in *net-info*) exit 1 ;; esac\nexit 0\n')
            (fake / 'docker').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'ip').write_text('#!/bin/sh\nprintf "192.168.150.0/24 via 10.1.1.1 dev wg0\\n"\n')
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'])
            command = ['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                       '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256'), 'check']
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('already routed', result.stderr)
            self.assertFalse((root / 'repo').exists())

    def test_prepare_stages_both_trees_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fake = root / 'bin'
            fake.mkdir()
            (fake / 'virsh').write_text('#!/bin/sh\ncase "$*" in *net-dumpxml*) /usr/bin/cat "$FAKE_XML" ;; *net-info*) printf "Name: stig-lab\\nActive: yes\\nAutostart: yes\\n" ;; esac\n')
            (fake / 'docker').write_text('#!/bin/sh\ncase "$*" in *up*) printf "%s\\n" "$LAB_REPO_DIR" >> "$FAKE_LOG" ;; esac\n')
            (fake / 'curl').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'ip').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'sudo').write_text('#!/bin/sh\necho unexpected-sudo >&2; exit 1\n')
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'],
                       FAKE_XML=str(ROOT / 'stig-lab-network.xml'), FAKE_LOG=str(root / 'docker.log'))
            command = ['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                       '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256'), 'prepare']
            for _ in range(2):
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                for major in ('8', '9'):
                    self.assertTrue((root / 'repo' / ('rhel' + major) / 'images/install.img').is_file())
                    self.assertFalse((root / 'repo' / ('rhel' + major + '.partial')).exists())
            self.assertEqual((root / 'docker.log').read_text().splitlines(), [str(root / 'repo')] * 2)

    def test_auto_falls_back_to_podman_compose_when_docker_is_unusable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fake = root / 'bin'
            fake.mkdir()
            (fake / 'virsh').write_text('#!/bin/sh\ncase "$*" in *net-dumpxml*) /usr/bin/cat "$FAKE_XML" ;; *net-info*) printf "Name: stig-lab\\nActive: yes\\nAutostart: yes\\n" ;; esac\n')
            (fake / 'docker').write_text('#!/bin/sh\ncase "$*" in *info*) exit 1 ;; esac\nexit 0\n')
            (fake / 'podman').write_text('#!/bin/sh\ncase "$*" in *up*) printf "podman %s\\n" "$LAB_REPO_DIR" >> "$FAKE_LOG" ;; esac\n')
            (fake / 'curl').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'ip').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'sudo').write_text('#!/bin/sh\necho unexpected-sudo >&2; exit 1\n')
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'],
                       FAKE_XML=str(ROOT / 'stig-lab-network.xml'), FAKE_LOG=str(root / 'engine.log'))
            base = ['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                    '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256')]
            result = subprocess.run([*base, 'prepare'], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('podman', result.stdout.lower())
            self.assertEqual((root / 'engine.log').read_text().strip(), f'podman {root / "repo"}')
            forced = subprocess.run([*base, '--engine', 'docker', 'check'], cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(forced.returncode, 0)
            self.assertIn('Docker', forced.stderr)
            (fake / 'docker').write_text('#!/bin/sh\nexit 0\n')
            preferred = subprocess.run([*base, 'check'], cwd=ROOT, env=env,
                                       capture_output=True, text=True)
            self.assertEqual(preferred.returncode, 0, preferred.stderr)
            self.assertIn('Container engine: docker', preferred.stdout)
            chosen = subprocess.run([*base, '--engine', 'podman', 'prepare'], cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(chosen.returncode, 0, chosen.stderr)
            self.assertEqual((root / 'engine.log').read_text().splitlines(),
                             [f'podman {root / "repo"}'] * 2)

    def test_auto_rejects_podman_without_compose_provider(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            fake = root / 'bin'
            fake.mkdir()
            (fake / 'docker').write_text('#!/bin/sh\ncase "$*" in *info*) exit 1 ;; esac\nexit 0\n')
            (fake / 'podman').write_text('#!/bin/sh\ncase "$*" in *"compose version"*) exit 1 ;; esac\nexit 0\n')
            (fake / 'virsh').write_text('#!/bin/sh\nexit 0\n')
            (fake / 'ip').write_text('#!/bin/sh\nexit 0\n')
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH'])
            command = ['bash', str(ROOT / 'setup-lab.sh'), '--media-dir', str(root / 'media'),
                       '--repo-dir', str(root / 'repo'), '--checksums', str(root / 'vendor.sha256'), 'check']
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Compose provider', result.stderr)
            self.assertFalse((root / 'repo').exists())

    def test_build_and_verify_synthetic_iso_without_vms(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            key = root / 'team.pub'
            key.write_text('ecdsa-sha2-nistp256 AAAAC3NzaC1lZDI1NTE5AAAAIGZha2Vub3RyZWFsa2V5Zm9ydGVzdGluZw== team@test\n')
            args = ('--out', str(root / 'menu.iso'), '--ssh-key', str(key),
                    '--credentials', str(root / 'private-gui.json'))
            result = self.run_setup(root, *args, '--generate-credentials', 'build-iso')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'menu.iso').is_file())
            self.assertTrue((root / 'menu.iso.sha256').is_file())
            self.assertEqual((root / 'private-gui.json').stat().st_mode & 0o777, 0o600)
            second = self.run_setup(root, *args, '--generate-credentials', 'build-iso')
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('existing ISO', second.stderr)
            fake = root / 'bin'
            fake.mkdir()
            curl = fake / 'curl'
            curl.write_text('#!/bin/sh\nexit 0\n')
            curl.chmod(0o755)
            command = ['bash', str(ROOT / 'setup-lab.sh'), '--out', str(root / 'menu.iso'), 'verify']
            result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH']),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            (root / 'menu.iso.sha256').write_text('0' * 64 + '  menu.iso\n')
            result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PATH=str(fake) + ':' + os.environ['PATH']),
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('checksum mismatch', result.stderr)

    def test_prepare_refuses_partial_tree_without_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fixture(root)
            partial = root / 'repo/rhel8.partial'
            partial.mkdir(parents=True)
            marker = partial / 'do-not-delete'
            marker.write_text('keep')
            result = self.run_setup(root, 'prepare')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('partial', result.stderr.lower())
            self.assertEqual(marker.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
