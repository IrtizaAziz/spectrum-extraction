"""Standalone, source-only scan coordinator; no academic application or database."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from time import monotonic, time_ns
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from academic_edge import EdgeTransport
from academic_engine import FIXTURE, LIVE, Halt
from academic_files import MAX_BYTES, _atomic, _directory, _read_at, save_stage, validate_content, close_directory, replace_bytes
from academic_incremental import run_incremental
from academic_moodle import ORIGIN, parse_courses, plugin_url


def validate(args):
    if type(args) is not dict or args.get('action') not in ('discover', 'scan'):
        raise ValueError('Expected discover or scan')
    defaults = dict(max_operations=120, material_limit=8, body_limit=10, download_limit=0)
    action = args['action']
    valid = {'action', 'target_id'} | (set(defaults) | {'course_ids', 'backlog_only', 'course_calendar'} if action == 'scan' else set())
    if set(args) - valid:
        raise ValueError('Unsupported option')
    target = args.get('target_id', '')
    if type(target) is not str or (target and not re.fullmatch(r'[A-Fa-f0-9]{32}', target)):
        raise ValueError('Expected an exact CDP target ID')
    if action == 'discover':
        return dict(action=action,target_id=target)
    ids = args.get('course_ids')
    if type(ids) is not list or not 1 <= len(ids) <= 16 or any(type(i) is not int or not 1 <= i <= 2147483647 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Choose 1–16 unique enrolled course IDs')
    result = dict(action=action, target_id=target, course_ids=sorted(ids))
    for key, cap in dict(max_operations=300, material_limit=30, body_limit=30, download_limit=12).items():
        value = args.get(key, defaults[key])
        if type(value) is not int or not int(key == 'max_operations') <= value <= cap:
            raise ValueError('Invalid cap: ' + key)
        result[key] = value
    for key in ('backlog_only', 'course_calendar'):
        value = args.get(key, False)
        if type(value) is not bool:
            raise ValueError('Expected a boolean: ' + key)
        result[key] = value
    return result


class Baseline:
    def __init__(self, path, fixture):
        self.path = path
        self.namespace = FIXTURE if fixture else LIVE

    def scan_records(self, namespace):
        if namespace != self.namespace:
            raise ValueError('Staging namespace mismatch')
        fd = _directory(self.path.parent, create=False)
        try:
            try:
                data = json.loads(_read_at(fd, self.path.name))
            except FileNotFoundError:
                return []
            if data['namespace'] != namespace:
                raise ValueError('Staging namespace mismatch')
            return data['records']
        finally:
            close_directory(fd)


def replace_json(fd, name, obj):
    data = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('State size limit reached')
    replace_bytes(fd, name, data)

def discover(transport):
    found = {}
    for offset in range(0, 1000, 100):
        data = transport.call('core_course_get_enrolled_courses_by_timeline_classification', classification='all', offset=offset, limit=100, sort='fullname')
        rows = parse_courses(data)
        for row in rows:
            cid = row['native_course_id']
            if cid in found or row['source_url'] != ORIGIN + '/course/view.php?id=' + cid:
                raise ValueError('Course inventory identity mismatch')
            found[cid] = {k: row[k] for k in ('native_course_id', 'code', 'name', 'source_url')}
        if len(rows) < 100:
            break
    return dict(status='complete' if len(rows) < 100 else 'partial', courses=list(found.values()), source_operations=transport.requests)


def archive(transport, stage, scope, fd, limit):
    if not limit:
        return [], 0, False, 0
    seen = {}
    for item in stage['attachments']:
        url = item['source_url']
        try:
            plugin_url(url)
            path = urlsplit(url).path
            if '/assignsubmission_file/' in path:
                continue
            seen.setdefault(url, set()).add((item['native_course_id'], item['native_module_id']))
        except ValueError:
            continue
    try:
        index = json.loads(_read_at(fd, 'downloads.json'))
    except FileNotFoundError:
        index = {}
    eligible = [url for url in seen if len(seen[url]) == 1]
    eligible.sort(key=lambda url: (index.get(url, {}).get('checked_at', 0), url))
    selected = eligible[:limit]
    out, stopped, allocated = [], False, 0
    from time import time_ns
    folder = scope / 'files'
    files = _directory(folder)
    try:
        for url in selected:
            cid, mid = next(iter(seen[url]))
            previous = index.get(url)
            validators = {}
            try:
                if previous:
                    basename = previous['basename']
                    if not isinstance(basename, str) or '/' in basename or basename in ('', '.', '..'):
                        raise ValueError('Invalid cache identity')
                    cached = _read_at(files, basename)
                    if hashlib.sha256(cached).hexdigest() != previous['sha256']:
                        raise ValueError('Cached attachment hash mismatch')
                    validators = previous['validators']
                remaining = 64 * 1024 * 1024 - allocated
                if not remaining:
                    break
                bound = min(MAX_BYTES, remaining)
                data, mime = transport.download(url, max_bytes=bound, validators=validators or None)
                if data is None:
                    if not previous or not validators:
                        raise ValueError('Unexpected conditional response')
                    record = dict(previous, checked_at=time_ns())
                    status = 'unchanged'
                else:
                    allocated += len(data)
                    name = validate_content(data, mime, unquote(urlsplit(url).path.rsplit('/', 1)[-1]), bound)
                    digest = hashlib.sha256(data).hexdigest()
                    basename = digest + '-' + name
                    _atomic(files, basename, data)
                    md = getattr(transport, 'last_download_metadata', {})
                    valid = {k:v for k,v in md.items() if k in ('etag','last_modified') and isinstance(v,str) and 0 < len(v) <= 512 and all(32 <= ord(c) <= 126 for c in v)}
                    record = dict(basename=basename, sha256=digest, byte_size=len(data), validators=valid, checked_at=time_ns())
                    status = 'unchanged' if previous and previous['sha256'] == digest else 'downloaded'
                index[url] = record
                replace_json(fd, 'downloads.json', index)
                out.append(dict(status=status, source_url=url, course_id=cid, module_id=mid,
                                local_path=str(folder/record['basename']), sha256=record['sha256'], byte_size=record['byte_size']))
            except Halt:
                out.append(dict(status='blocked', source_url=url, course_id=cid, module_id=mid))
                stopped = True
                break
            except (OSError, ValueError, KeyError):
                out.append(dict(status='unavailable_or_rejected', source_url=url, course_id=cid, module_id=mid))
    finally:
        close_directory(files)
    return out, allocated, stopped, len(seen)-len(out)


def recognize_image(data):
    if sys.platform == 'win32':
        from io import BytesIO
        try:
            from PIL import Image
            with Image.open(BytesIO(data), formats=['JPEG', 'PNG']) as image:
                if image.width < 1 or image.height < 1 or max(image.size) > 12000 or image.width * image.height > 40000000:
                    raise ValueError('Image dimensions exceed OCR bounds')
        except ImportError as exc:
            raise ValueError('Windows OCR requires Pillow in the Hermes Python runtime') from exc
        roots = (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'))
        exe = next((p for root in roots if root and Path(root).is_absolute()
                    if (p := Path(root)/'Tesseract-OCR'/'tesseract.exe').is_file()), None)
        if exe is None:
            raise ValueError('Windows OCR requires Tesseract in Program Files')
        result = subprocess.run([str(exe), 'stdin', 'stdout', '--tessdata-dir', str(exe.parent/'tessdata'), '-l', 'eng'],
                                input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=20, check=True)
        return result.stdout.decode('utf-8').strip()[:6000]
    helper = Path(__file__).resolve().parents[1] / 'assets' / 'ocr.swift'
    result = subprocess.run(['/usr/bin/swift', str(helper)], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=20, check=True)
    return result.stdout.decode('utf-8').strip()[:6000]


def scan_images(transport, stage, fd):
    refs = {}
    for r in stage['records']:
        if r['verified_at'] != stage['observed_at'] or r['record_type'] not in ('activity', 'post'):
            continue
        payload = r['payload']
        detail = payload.get('detail') or payload
        for url in detail.get('inline_images', []):
            if '/assignsubmission_file/' not in urlsplit(url).path:
                refs.setdefault(url, []).append(dict(course_id=r['course_id'], kind=r['record_type'],
                    native_id=r['native_id'], page=payload.get('source_url')))
    stats = dict(discovered=len(refs), recognized=0, cache_hits=0, review=0, remaining=0, changed=0, source_operations=0)
    evidence = []
    if not refs:
        return stats, evidence, False, None
    try:
        index = json.loads(_read_at(fd, ('inline-ocr-windows-v1.json' if sys.platform == 'win32' else 'inline-ocr-v1.json')))
    except FileNotFoundError:
        index = {'urls': {}, 'text': {}}
    urls, texts = index['urls'], index['text']
    pending = sorted(refs, key=lambda url: (urls.get(url, {}).get('checked', 0), url))
    stopped = False
    start = transport.requests
    until = monotonic() + 30
    for url in pending[:3]:
        if monotonic() >= until:
            break
        row = dict(image_url=url, sources=refs[url], status='review')
        evidence.append(row)
        old = urls.get(url, {})
        digest = old.get('sha256')
        validators = old.get('validators') if digest in texts else None
        try:
            plugin_url(url)
            data, mime = transport.download(url, max_bytes=4 * 1024 * 1024, validators=validators or None)
        except (Halt, OSError):
            row['reason'] = 'image_source_unavailable'
            stopped = True
            break
        except ValueError:
            row['reason'] = 'image_reference_rejected'
            urls[url] = dict(old, checked=time_ns())
            continue
        metadata = validators or {}
        if data is not None:
            if not (mime == 'image/jpeg' and data.startswith(b'\xff\xd8\xff') or
                    mime == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n')):
                row['reason'] = 'unsupported_image_response'
                urls[url] = {'checked': time_ns()}
                continue
            digest = hashlib.sha256(data).hexdigest()
            metadata = {k:v for k,v in getattr(transport, 'last_download_metadata', {}).items()
                        if k in ('etag','last_modified') and isinstance(v,str) and 0 < len(v) <= 512
                        and all(32 <= ord(c) <= 126 for c in v)}
        try:
            if digest in texts:
                text = texts[digest]
                stats['cache_hits'] += 1
            elif data is not None:
                text = recognize_image(data)
                texts[digest] = text
                stats['recognized'] += 1
            else:
                raise ValueError('No cached image evidence')
        except (OSError, ValueError, subprocess.SubprocessError):
            row['reason'] = 'local_ocr_unavailable_or_failed'
            urls[url] = dict(old, checked=time_ns())
            continue
        changed = old.get('sha256') != digest
        stats['changed'] += int(changed)
        row.update(sha256=digest, text=text, changed=changed)
        row['candidate_lines'] = [line[:240] for line in text.splitlines()
                                  if re.search(r'\b(?:due|deadline|submit|submission|hantar|serah|tarikh)\b', line, re.I)][:5]
        row['status'] = 'ocr_text' if text else 'review'
        if not text:
            row['reason'] = 'no_text_detected'
        urls[url] = dict(sha256=digest, validators=metadata, checked=time_ns())
    stats.update(remaining=len(refs)-len(evidence), review=sum(r['status']=='review' for r in evidence),
                 source_operations=transport.requests-start)
    for url in list(urls) if len(urls) > 1024 else ():
        if url not in refs:
            del urls[url]
    kept = sorted(urls, key=lambda u: urls[u]['checked'], reverse=True)[:1024]
    index['urls'] = {u: urls[u] for u in kept}
    index['text'] = {h: texts[h] for h in {urls[u].get('sha256') for u in kept} if h in texts}
    return stats, evidence, stopped, index


def acquire_lease(directory, name):
    if sys.platform == 'win32':
        try:
            return directory.open(name, lease=True)
        except OSError as exc:
            if exc.winerror == 32:
                return None
            raise
    import fcntl
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Invalid scan lease')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise


def scan(transport, root, opts):
    started = monotonic()
    key = hashlib.sha256(json.dumps(opts['course_ids']).encode()).hexdigest()[:20]
    scope = root / ('fixture-' if transport.fixture else 'courses-') / key
    fd = _directory(scope)
    lease = None
    try:
        lease = acquire_lease(fd, 'scan.lock')
        if lease is None:
            return dict(status='busy')
        store = Baseline(scope/'baseline.json', transport.fixture)
        stage = run_incremental(transport, store, checkpoint=scope/'work.json', course_ids=opts['course_ids'],
                                max_operations=opts['max_operations'], verify_limit=opts['body_limit'], body_limit=opts['body_limit'],
                                material_limit=opts['material_limit'], backlog_only=opts['backlog_only'], calendar=opts['course_calendar'])
        image_index = None
        if stage['status'] in ('success', 'partial') and not stage.get('halted'):
            stats, evidence, image_halted, image_index = scan_images(transport, stage, fd)
            stage.update(inline_ocr=stats, inline_image_evidence=evidence)
            if image_halted:
                stage.update(status='blocked', halted=True)
            elif (stats['review'] or stats['remaining']) and stage['status'] == 'success':
                stage['status'] = 'partial'
        else:
            stage['inline_ocr'] = {'status':'not_run', 'source_operations':0}
        # Download from this pass only: cached or historical bodies do not authorize a file GET.
        latest = {(r['course_id'], r['native_id']) for r in stage['records'] if r['record_type']=='activity' and r['complete'] and r['verified_at']==stage['observed_at']}
        latest.update((r['course_id'],r['payload'].get('native_module_id')) for r in stage['records'] if r['record_type']=='post' and r['complete'] and r['verified_at']==stage['observed_at'])
        stage['download_scope'] = 'current-pass attachment references only; no submission originals'
        original_refs = stage['attachments']
        stage['attachments'] = [a for a in original_refs if (a['native_course_id'], a['native_module_id']) in latest]
        start = transport.requests
        if stage['status'] in ('success', 'partial') and not stage.get('halted'):
            downloads, size, stopped, left = archive(transport, stage, scope, fd, opts['download_limit'])
        else:
            downloads, size, stopped, left = [], 0, False, 0
        stage['attachments'] = original_refs
        if stopped:
            stage.update(status='blocked', halted=True)
        elif (left or any(d['status']=='unavailable_or_rejected' for d in downloads)) and stage['status']=='success':
            stage['status']='partial'
        stage.update(downloads=downloads, download_bytes_transferred=size, download_source_operations=transport.requests-start,
                     download_references_remaining=left, control_operations=transport.control_requests,
                     unknown_dispatches=transport.unknown_dispatches, course_ids=opts['course_ids'],
                     calendar_requested=opts['course_calendar'],
                     total_source_operations=stage['source_operation_count'] + (transport.requests-start) + stage['inline_ocr']['source_operations'],
                     crawl_elapsed_seconds=stage['elapsed_seconds'], scan_elapsed_seconds=round(monotonic()-started, 6))
        report = scope / ('report-' + uuid4().hex + '.json')
        commit_baseline = stage['status'] in ('success','partial') and not stage.get('halted')
        # Save evidence before acknowledging changes. The report is evidence, not a commit receipt.
        stage['baseline_commit_planned'] = commit_baseline
        save_stage(report, stage)
        if image_index is not None:
            replace_json(fd, ('inline-ocr-windows-v1.json' if sys.platform == 'win32' else 'inline-ocr-v1.json'), image_index)
        stage['baseline_advanced'] = False
        if commit_baseline:
            records = {(r['record_type'],r['native_id']):r for r in store.scan_records(stage['namespace'])}
            for r in stage['records']:
                key = (r['record_type'],r['native_id'])
                prior = records.get(key)
                if r['complete'] or not prior or not prior['complete']:
                    records[key] = dict(r, payload=json.dumps(r['payload'], ensure_ascii=False))
            replace_json(fd, 'baseline.json', dict(namespace=stage['namespace'], records=list(records.values())))
            stage['baseline_advanced'] = True
        summary = {k:stage[k] for k in ('status','fixture','course_ids','counts','workload','coverage','baseline_advanced','downloads','download_bytes_transferred','download_source_operations','download_references_remaining','control_operations','unknown_dispatches','source_operation_count','observed_at')}
        return dict(summary, report_path=str(report), semantic_changes=len(stage['llm_queue']), verification_backlog=len(stage['verification_queue']), failure_count=len(stage['failures']), total_source_operations=stage['total_source_operations'], crawl_elapsed_seconds=stage['crawl_elapsed_seconds'], scan_elapsed_seconds=stage['scan_elapsed_seconds'], inline_ocr=stage['inline_ocr'])
    finally:
        if lease is not None:
            os.close(lease)
        close_directory(fd)


def main():
    transport = None
    lease = None
    fd = None
    try:
        data = json.load(sys.stdin)
        opts = validate(data['options'])
        if sys.platform not in ('darwin', 'win32'):
            return dict(status='blocked', reason='This live transport requires macOS or native Windows Microsoft Edge')
        home = Path(data['home'])
        if sys.platform == 'win32' and (not home.is_absolute() or not home.is_relative_to(Path.home()) or '..' in home.parts):
            raise ValueError('Windows Hermes profile must be inside the current user profile')
        root = home/'state'/'spectrum-extraction'
        fd = _directory(root)
        try:
            if sys.platform != 'win32':
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('Plugin state directory is not private')
            lease = acquire_lease(fd, 'live.lock')
            if lease is None:
                return dict(status='busy')
        except BaseException:
            close_directory(fd)
            fd = None
            raise
        transport = EdgeTransport(target_id=opts['target_id'], config_path=Path(data['home'])/'config.yaml', native_route=data['route'])
        if opts['action'] == 'discover':
            return discover(transport)
        return scan(transport, root, opts)
    except Halt as exc:
        return dict(status='blocked', reason=getattr(exc, 'reason', 'source_halted'), retry_automatically=False)
    except (KeyError, ValueError, OSError) as exc:
        return dict(status='failed', reason=type(exc).__name__, retry_automatically=False)
    finally:
        try:
            if transport is not None:
                transport.close()
        finally:
            if lease is not None:
                os.close(lease)
            if fd is not None:
                close_directory(fd)


if __name__ == '__main__':
    print(json.dumps(main()))
