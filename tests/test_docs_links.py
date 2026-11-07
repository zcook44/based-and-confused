"""Validate operator documentation after centralizing the root guides."""
from pathlib import Path
import re
import subprocess
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


if __name__ == '__main__':
    unittest.main()
