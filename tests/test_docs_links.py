"""Validate operator documentation after centralizing the root guides."""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ('README.md', 'QUICKSTART.md', 'QUARTERLY-LAB.md', 'QUARTERLY-WORKFLOW.md')


class DocumentationLinks(unittest.TestCase):
    def test_root_guides_are_centralized(self):
        for name in GUIDES:
            with self.subTest(name=name):
                self.assertFalse((ROOT / name).exists())
                self.assertTrue((ROOT / 'docs' / name).is_file())

    def test_all_relative_markdown_links_and_anchors_resolve(self):
        for doc in (ROOT / 'docs').rglob('*.md'):
            text = doc.read_text()
            for destination in re.findall(r'(?<!!)\[[^]]*\]\(([^)]+)\)', text):
                target = destination.split(' ', 1)[0].strip('<>')
                if '://' in target or target.startswith(('mailto:', '#')):
                    continue
                path, _, anchor = target.partition('#')
                path = (doc.parent / unquote(path)).resolve()
                with self.subTest(doc=doc.name, link=destination):
                    self.assertTrue(path.is_file(), f'missing link target: {path}')
                    if anchor:
                        headings = re.findall(r'^#{1,6}\s+(.+)$', path.read_text(), re.M)
                        slugs = {re.sub(r'[^a-z0-9 -]', '', heading.lower()).replace(' ', '-') for heading in headings}
                        self.assertIn(unquote(anchor), slugs)

    def test_shell_blocks_parse(self):
        for doc in (ROOT / 'docs').rglob('*.md'):
            blocks = re.findall(r'```bash\n(.*?)\n```', doc.read_text(), re.S)
            for index, block in enumerate(blocks):
                with self.subTest(doc=doc.name, block=index):
                    result = subprocess.run(['bash', '-n'], input=block, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_seal_image_gate_real_qcow2(self):
        workflow = (ROOT / 'docs' / 'QUARTERLY-WORKFLOW.md').read_text()
        match = re.search(r'(?ms)^check_image\(\) \{\n(.*?)^\}', workflow)
        assert match is not None, 'documented seal image gate missing'
        check = match.group(0) + '\ncheck_image "$1"\n'
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            plain, external = (Path(tmp) / n for n in ('plain.qcow2', 'external.qcow2'))
            subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(plain), '1M'],
                           check=True, capture_output=True)
            subprocess.run(['qemu-img', 'create', '-f', 'qcow2',
                            '-o', 'data_file=' + str(Path(tmp) / 'payload.raw'),
                            str(external), '1M'], check=True, capture_output=True)
            for path, accepted in ((plain, True), (external, False)):
                with self.subTest(path=path.name):
                    result = subprocess.run(['bash', '-c', check, 'check_image', str(path)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, accepted, result.stderr)


if __name__ == '__main__':
    unittest.main()
