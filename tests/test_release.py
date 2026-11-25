"""Exercise the public artifact from a different working directory."""
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTest(unittest.TestCase):
    def test_archive_is_reproducible_and_contains_only_public_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            archives = []
            for directory in ('a', 'b'):
                result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/build_release.py'), directory],
                                        cwd=root, capture_output=True, text=True, check=True)
                archives.append(Path(result.stdout.strip()))
            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())
            with ZipFile(archives[0]) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertEqual(len(set(names)), len(names))
                for name in names:
                    self.assertTrue(name.startswith('spectrum-extraction/'))
                    self.assertNotRegex(name, r'(^|/)(\.env[^/]*|__pycache__|state|reports|downloads)(/|$)|\.(db|sqlite3|pyc)$')
                self.assertIn(b'Copyright (c) 2026 Gabriel Werneck', archive.read('spectrum-extraction/THIRD_PARTY_NOTICES.md'))
                self.assertIn('spectrum-extraction/plugin.yaml', names)
                self.assertIn('spectrum-extraction/.github/workflows/ci.yml', names)
                version = re.search(rb'^version: (\S+)', archive.read('spectrum-extraction/plugin.yaml'), re.M)[1].decode()
                self.assertEqual(archives[0].name, f'spectrum-extraction-{version}.zip')
