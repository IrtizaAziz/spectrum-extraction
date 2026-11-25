import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
try:
    from portable_scan import scan, validate
except ModuleNotFoundError:
    scan = validate = None

O = 'https://spectrum.um.edu.my'
F = O + '/pluginfile.php/3/mod_folder/content/0/lecture.txt'


class Fixture:
    fixture = True
    observed_at = '2030-01-01T00:00:00+00:00'
    timezone = None
    timezone_evidence = None
    control_requests = 0
    unknown_dispatches = 0
    last_download_metadata = {'etag': '"fixture-1"'}

    def __init__(self, changed=False):
        self.requests = 0
        self.started = []
        self.changed = changed

    def call(self, method, **args):
        self.requests += 1
        if method == 'core_course_get_enrolled_courses_by_timeline_classification':
            return {'courses': [dict(id=i, shortname='DEMO', fullname=f'Course {i}', viewurl=f'{O}/course/view.php?id={i}') for i in (11, 13)]}
        assert args == {'courseid': 11}, args
        self.started.append(11)
        return {'course': {'id': 11}, 'section': [{'number': 1, 'title': 'Week 1', 'cmlist': [21, 22]}],
                'cm': [dict(id=i, name=k, module=k, url=f'{O}/mod/{k}/view.php?id={i}') for i, k in ((21, 'assign'), (22, 'folder'))]}

    def page(self, url):
        self.requests += 1
        if '/assign/' in url:
            return '<div id="intro">Submit one report' + (' with a cover sheet' if self.changed else '') + '.</div>'
        return f'<a href="{F}">Lecture</a>'

    def download(self, url, *, max_bytes, validators=None):
        self.requests += 1
        assert url == F
        return (None if validators else b'Lecture notes\n'), 'text/plain'


class PortableTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(scan, 'portable scan is not implemented')
        folder = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_scoped_scan_cache_and_download(self):
        f = Fixture()
        opts = validate({'action': 'scan', 'course_ids': [11], 'download_limit': 1})
        first = scan(f, self.root, opts)
        self.assertEqual(f.started, [11])
        self.assertTrue(first['fixture'])
        self.assertEqual(first['downloads'][0]['status'], 'downloaded')
        self.assertEqual(first['download_source_operations'], 1)
        self.assertEqual(first['total_source_operations'], first['source_operation_count'] + 1)
        self.assertGreaterEqual(first['scan_elapsed_seconds'], first['crawl_elapsed_seconds'])
        self.assertIn('Submit one report.', Path(first['report_path']).read_text())
        self.assertEqual(Path(first['downloads'][0]['local_path']).read_bytes(), b'Lecture notes\n')
        second = scan(Fixture(), self.root, opts)
        self.assertEqual(second['downloads'][0]['status'], 'unchanged')
        self.assertEqual(second['semantic_changes'], 0)
        changed = scan(Fixture(changed=True), self.root, opts)
        self.assertEqual(changed['semantic_changes'], 1)

    def test_rejects_unbounded_or_ambiguous_input(self):
        for opts in ({'action':'scan','course_ids':[]}, {'action':'scan','course_ids':[True]}, {'action':'scan','course_ids':[11,11]}, {'action':'scan','course_ids':[11],'download_limit':13}, {'action':'scan','course_ids':[11],'url':F}):
            with self.assertRaises(ValueError): validate(opts)

    def test_forum_history_is_not_current_freshness(self):
        class Forum(Fixture):
            def call(self, method, **args):
                if method == 'mod_forum_get_discussion_posts':
                    self.requests += 1
                    self.assert_discussion = args['discussionid']
                    return {'courseid':11, 'forumid':31, 'posts':[{'id':51,'discussionid':41,'subject':'Notice','message':'Read this.'}]}
                data = super().call(method, **args)
                if 'cm' in data:
                    data['section'][0]['cmlist'].append(23)
                    data['cm'].append(dict(id=23,instance=31,name='Forum',module='forum',url=O+'/mod/forum/view.php?id=23'))
                return data
            def page(self,url):
                if '/forum/' in url:
                    self.requests += 1
                    return f'<body data-hermes-course-id="11" data-hermes-module-id="23" data-hermes-forum-id="31"><a href="{O}/mod/forum/discuss.php?d=41">Notice</a></body>'
                return super().page(url)
        f = Forum()
        first = scan(f, self.root, validate({'action':'scan','course_ids':[11]}))
        self.assertEqual(f.assert_discussion, 41)
        self.assertEqual(first['counts']['discussion_bodies_resolved'], 1)
        second = scan(Forum(), self.root, validate({'action':'scan','course_ids':[11],'body_limit':0}))
        self.assertEqual(second['counts']['discussion_historically_verified'], 1)
        self.assertEqual(second['counts']['discussion_bodies_resolved'], 0)
        self.assertFalse(second['coverage']['current_bodies_complete'])

    def test_halted_download_does_not_advance_baseline_or_retry(self):
        from academic_engine import Halt

        class Stopped(Fixture):
            downloads = 0

            def download(self, *args, **kwargs):
                self.downloads += 1
                self.requests += 1
                raise Halt('source halted')

        f = Stopped()
        result = scan(f, self.root, validate({'action':'scan','course_ids':[11],'download_limit':3}))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(f.downloads, 1)
        self.assertFalse(result['baseline_advanced'])
        self.assertEqual(list(self.root.rglob('baseline.json')), [])

    def test_report_failure_keeps_baseline_and_change_is_rediscovered(self):
        from unittest.mock import patch

        opts = validate({'action':'scan','course_ids':[11]})
        scan(Fixture(), self.root, opts)
        baseline = next(self.root.rglob('baseline.json'))
        before = baseline.read_bytes()
        with patch('portable_scan.save_stage', side_effect=OSError('report unavailable')):
            with self.assertRaises(OSError):
                scan(Fixture(changed=True), self.root, opts)
        self.assertEqual(baseline.read_bytes(), before)
        result = scan(Fixture(changed=True), self.root, opts)
        self.assertEqual(result['semantic_changes'], 1)

    def test_inline_image_ocr_is_default_and_revalidates_same_url(self):
        from unittest.mock import patch
        url = O + '/pluginfile.php/3/mod_assign/intro/deadline.jpg'

        class Inline(Fixture):
            def page(self, address):
                super().page(address)
                return f'<main id="region-main"><div id="intro"><img data-src="{url}"></div></main>' if '/assign/' in address else ''

            def download(self, address, *, max_bytes, validators=None):
                self.requests += 1
                assert address == url
                return (b'\xff\xd8\xff' + bytes([self.changed])), 'image/jpeg'

        opts = validate({'action':'scan','course_ids':[11]})
        with patch('portable_scan.recognize_image', create=True, return_value='Due: 15 March 2030') as ocr:
            first = scan(Inline(), self.root, opts)
            second = scan(Inline(), self.root, opts)
            third = scan(Inline(changed=True), self.root, opts)
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(first['downloads'], [])
        self.assertEqual(first['inline_ocr']['recognized'], 1)
        self.assertEqual(second['inline_ocr']['cache_hits'], 1)
        self.assertEqual(third['inline_ocr']['recognized'], 1)
        self.assertIn('Due: 15 March 2030', Path(first['report_path']).read_text())

    def test_budget_and_explicit_no_download(self):
        f = Fixture()
        result = scan(f, self.root, validate({'action':'scan','course_ids':[11],'max_operations':2}))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['workload']['observed_source_operations'], 2)
        self.assertEqual(result['downloads'], [])
