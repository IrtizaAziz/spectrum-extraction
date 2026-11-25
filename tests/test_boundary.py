"""Offline checks of approved browser identity and download admission."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from academic_edge import ENDPOINT, validate_identity
from academic_engine import Halt
from academic_files import _directory, close_directory
from portable_scan import archive
from windows_edge import attest

O = 'https://spectrum.um.edu.my'


class BoundaryTest(unittest.TestCase):
    def test_identity_accepts_only_exact_approved_route(self):
        ws = 'ws://127.0.0.1:9337/devtools/browser/fixture'
        edge = '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'
        good = dict(endpoint=ENDPOINT, configured=ENDPOINT, listener='127.0.0.1:9337', executable=edge,
                    command=f'edge --user-data-dir="{Path.home()}/.hermes/spectrum-edge" --remote-debugging-port=9337',
                    version={'Browser':'Edg/1', 'webSocketDebuggerUrl':ws})
        with patch('academic_edge.sys.platform', 'darwin'):
            validate_identity(**good)
            for key, value in [('listener','0.0.0.0:9337'), ('configured',ws+'-stale'), ('executable','Chromium'),
                               ('command',good['command'].replace('spectrum-edge','other-profile'))]:
                with self.subTest(key=key), self.assertRaises(Halt):
                    validate_identity(**dict(good, **{key:value}))

    def test_windows_edge_requires_installed_executable_and_exact_profile(self):
        root = r'C:\Program Files (x86)'
        exe = root + r'\Microsoft\Edge\Application\msedge.exe'
        home = r'C:\Profiles\Example Person'
        args = [exe, '--user-data-dir='+home+r'\.hermes\spectrum-edge', '--remote-debugging-port=9337']
        attest(exe, args, home, [root])
        for bad in [args+[args[1]], [exe,args[1]+'-cua',args[2]], [exe,args[1],'--remote-debugging-port=9338']]:
            with self.assertRaises(ValueError):
                attest(exe, bad, home, [root])
        with self.assertRaises(ValueError):
            attest(r'C:\Temp\msedge.exe', args, home, [root])

    @unittest.skipUnless(sys.platform == 'win32', 'native Windows handles')
    def test_windows_handles_keep_directory_pinned_and_lease_exclusive(self):
        from portable_scan import acquire_lease
        from academic_files import replace_bytes, _read_at
        from windows_edge import split_command
        self.assertEqual(split_command(r'"C:\Program Files\edge.exe" "--user-data-dir=C:\Example User"')[1],
                         r'--user-data-dir=C:\Example User')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve()/'state'
            directory = _directory(path)
            try:
                with self.assertRaises(OSError):
                    path.rename(path.with_name('moved'))
                replace_bytes(directory, 'test.json', b'one')
                replace_bytes(directory, 'test.json', b'two')
                self.assertEqual(_read_at(directory, 'test.json'), b'two')
                lease = acquire_lease(directory, 'scan.lock')
                try:
                    self.assertIsNotNone(lease)
                    self.assertIsNone(acquire_lease(directory, 'scan.lock'))
                finally:
                    if lease is not None: os.close(lease)
            finally:
                close_directory(directory)

    def test_download_does_not_dispatch_for_submission_or_ambiguous_reference(self):
        common = O+'/pluginfile.php/3/mod_folder/content/0/lecture.txt'
        submission = O+'/pluginfile.php/3/assignsubmission_file/submission_files/report.txt'
        def ref(url, mid):
            return dict(source_url=url,native_course_id='11',native_module_id=mid)
        data = dict(attachments=[ref(common,'1'),ref(common,'2'),ref(submission,'3')])
        with tempfile.TemporaryDirectory() as folder:
            scope = Path(folder).resolve()
            fd = _directory(scope)
            try:
                transport = Mock()
                results, bytes_read, halted, left = archive(transport,data,scope,fd,12)
                transport.download.assert_not_called()
                self.assertEqual((results,bytes_read,halted,left),([],0,False,1))
            finally:
                close_directory(fd)
