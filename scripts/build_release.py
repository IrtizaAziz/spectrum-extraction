"""Build a reproducible source ZIP using only explicitly listed public files."""
import argparse
from pathlib import Path
import re
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    '.github/workflows/ci.yml', '.gitignore', 'CHANGELOG.md', 'LICENSE', 'README.md',
    'SECURITY.md', 'THIRD_PARTY_NOTICES.md', '__init__.py', 'assets/capture.js', 'assets/ocr.swift', 'engine/academic_edge.py',
    'engine/academic_engine.py', 'engine/academic_execution.py', 'engine/academic_files.py',
    'engine/academic_incremental.py', 'engine/academic_moodle.py', 'engine/portable_scan.py',
    'engine/windows_edge.py', 'engine/windows_io.py',
    'plugin.yaml', 'scripts/build_release.py', 'skills/spectrum-extraction/SKILL.md',
    'tests/test_boundary.py', 'tests/test_capture.cjs', 'tests/test_plugin.py', 'tests/test_release.py', 'tests/test_scan.py',
)
PRIVATE = re.compile(rb'/Users/[\w.-]+/|/home/[\w.-]+/|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output_dir', type=Path)
    args = parser.parse_args()
    files = []
    for name in sorted(FILES):
        file = ROOT / name
        if file.resolve() != file or not file.is_file():
            raise SystemExit(f'Unsafe or missing source: {name}')
        data = file.read_bytes()
        if name != 'scripts/build_release.py' and PRIVATE.search(data):
            raise SystemExit(f'Potential private information in {name}')
        files.append((name, data))
    match = re.search(rb'^version: (\d+\.\d+\.\d+)\s*$', (ROOT / 'plugin.yaml').read_bytes(), re.M)
    if not match:
        raise SystemExit('Invalid plugin version')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out = args.output_dir / f'spectrum-extraction-{match[1].decode()}.zip'
    with ZipFile(out, 'x', compression=ZIP_DEFLATED) as archive:
        for name, data in files:
            info = ZipInfo('spectrum-extraction/' + name)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=ZIP_DEFLATED)
    with ZipFile(out) as archive:
        if archive.testzip() is not None:
            raise SystemExit('ZIP integrity check failed')
    print(out.resolve())


if __name__ == '__main__':
    main()
