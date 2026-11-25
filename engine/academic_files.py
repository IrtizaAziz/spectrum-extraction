"""Bounded original-only attachment validation/cache. No automatic execution/extraction."""
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata
from urllib.parse import unquote, urlsplit
import uuid
import zipfile
from xml.etree import ElementTree

from academic_moodle import plugin_url

MAX_BYTES = 32 * 1024 * 1024
MIMES = {
    '.pdf': {'application/pdf'}, '.zip': {'application/zip', 'application/x-zip-compressed'},
    '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
    '.pptx': {'application/vnd.openxmlformats-officedocument.presentationml.presentation'},
    '.xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},
    '.txt': {'text/plain'}, '.csv': {'text/csv', 'text/plain'},
    '.png': {'image/png'}, '.jpg': {'image/jpeg'}, '.jpeg': {'image/jpeg'},
}
ACTIVE = {'.exe', '.dll', '.com', '.bat', '.cmd', '.ps1', '.sh', '.js', '.vbs', '.hta', '.html', '.htm', '.app', '.dmg', '.msi', '.scr', '.jar'}


def byte_bound(value):
    if type(value) is not int or not 1 <= value <= MAX_BYTES:
        raise ValueError('Attachment byte bound must be 1..32MiB')
    return value


def safe_name(value):
    value = unicodedata.normalize('NFC', value)
    suffix = Path(value).suffix.lower()
    if suffix not in MIMES:
        raise ValueError('Unsupported or active attachment extension')
    stem = ''.join(c if c.isalnum() or c in ' ._-' else '_' for c in value[:-len(suffix)]).strip(' .') or 'attachment'
    while len(stem.encode('utf-8')) > 120:
        stem = stem[:-1]
    return stem + suffix


def validate_content(data, mime, name, max_bytes=MAX_BYTES):
    byte_bound(max_bytes)
    if not isinstance(data, bytes) or not 0 < len(data) <= max_bytes:
        raise ValueError('Empty or oversized attachment')
    name = safe_name(name)
    ext = Path(name).suffix
    mime = mime.split(';')[0].strip().lower()
    if mime not in MIMES[ext] | {'application/octet-stream', 'binary/octet-stream'}:
        raise ValueError('Attachment MIME does not match original extension')
    head = data[:4096].lstrip().lower()
    if data.startswith((b'MZ', b'\x7fELF', b'#!', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf')) or re.search(rb'<\s*(?:!doctype\s+html|html|script|svg|iframe)', head):
        raise ValueError('Active/executable/login content rejected')
    if ext == '.pdf':
        if not re.match(rb'%PDF-(?:1\.[0-7]|2\.0)[\r\n]', data) or not data.rstrip().endswith(b'%%EOF'):
            raise ValueError('Invalid PDF signature/trailer')
        offsets = list(re.finditer(rb'startxref\s+(\d{1,12})\s+%%EOF', data))
        if not offsets or not re.search(rb'\d+\s+\d+\s+obj\b', data) or not re.search(rb'/Root\s+\d+\s+\d+\s+R\b', data):
            raise ValueError('Incomplete PDF object/cross-reference structure')
        offset = int(offsets[-1][1])
        xref = data[offset:offset+1024]
        if not xref.startswith(b'xref') and not (re.match(rb'\d+\s+\d+\s+obj\b', xref) and re.search(rb'/Type\s*/XRef\b', xref)):
            raise ValueError('Invalid PDF cross-reference offset')
        if re.search(rb'/(?:JavaScript|JS|Launch|EmbeddedFile|OpenAction)\b', data):
            raise ValueError('Active PDF content rejected')
    elif ext in ('.zip', '.docx', '.pptx', '.xlsx'):
        if not data.startswith(b'PK\x03\x04'):
            raise ValueError('Invalid ZIP signature')
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 10000 or sum(e.file_size for e in entries) > 128*1024*1024:
                    raise ValueError('ZIP expansion/entry bound exceeded')
                names = set()
                for e in entries:
                    path = PurePosixPath(e.filename)
                    if (e.filename in names or e.filename.startswith('/') or '\\' in e.filename or ':' in e.filename
                        or any(c in ('.', '..') for c in e.filename.split('/')) or any(ord(c) < 32 for c in e.filename)
                        or stat.S_ISLNK(e.external_attr >> 16) or e.flag_bits & 1
                        or Path(e.filename).suffix.lower() in ACTIVE or e.file_size > max(1, e.compress_size)*200):
                        raise ValueError('Unsafe ZIP member or compression ratio')
                    names.add(e.filename)
                if archive.testzip() is not None:
                    raise ValueError('ZIP integrity failure')
                if ext != '.zip':
                    required = {'.docx': 'word/document.xml', '.pptx': 'ppt/presentation.xml', '.xlsx': 'xl/workbook.xml'}[ext]
                    if not {'[Content_Types].xml', required}.issubset(names) or any('vbaproject' in n.lower() for n in names):
                        raise ValueError('Invalid or macro-enabled OOXML structure')
                    for member in ('[Content_Types].xml', required):
                        xml = archive.read(member)
                        if len(xml) > 4*1024*1024 or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                            raise ValueError('Unsafe or oversized OOXML metadata')
                        ElementTree.fromstring(xml)
        except (zipfile.BadZipFile, RuntimeError, ElementTree.ParseError, NotImplementedError) as exc:
            raise ValueError('Invalid ZIP/OOXML content') from exc
    elif ext == '.png':
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid PNG signature')
    elif ext in ('.jpg', '.jpeg'):
        if not data.startswith(b'\xff\xd8\xff') or not data.endswith(b'\xff\xd9'):
            raise ValueError('Invalid JPEG signature')
    else:
        text = data.decode('utf-8-sig')
        if '\x00' in text or re.search(r'<\s*(html|script|iframe)', text, re.I):
            raise ValueError('Invalid plain-text content')
    return name


def _directory(path, create=True):
    """Walk from / using directory descriptors; never follow substituted symlinks."""
    if sys.platform == "win32":
        from windows_io import Directory
        return Directory(path, create)
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = nxt
        return fd
    except BaseException:
        os.close(fd); raise


def _read_at(fd, name):
    handle = fd.open(name) if sys.platform == "win32" else os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(handle, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size > MAX_BYTES:
            raise ValueError('Cache entry is not a bounded regular file')
        return stream.read(MAX_BYTES+1)


def close_directory(fd):
    fd.close() if sys.platform == 'win32' else os.close(fd)


def replace_bytes(fd, name, data, *, overwrite=True):
    if sys.platform == 'win32':
        dst = fd.leaf(name)
        temp = fd.leaf('.partial-' + uuid.uuid4().hex)
        with temp.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            try:
                prior = _read_at(fd, name)
            except FileNotFoundError:
                prior = None
            if prior is not None and not overwrite:
                if prior != data:
                    raise ValueError('Existing cache target differs; never overwritten')
                return
            (os.replace if overwrite else os.rename)(temp, dst)
        finally:
            temp.unlink(missing_ok=True)
        return
    if not overwrite:
        return _atomic(fd, name, data)
    try:
        if not stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
            raise ValueError('Unsafe state destination')
    except FileNotFoundError:
        pass
    temp = '.stage-' + uuid.uuid4().hex
    _atomic(fd, temp, data)
    try:
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try: os.unlink(temp, dir_fd=fd)
        except FileNotFoundError: pass


def _atomic(fd, name, data):
    if sys.platform == 'win32':
        return replace_bytes(fd, name, data, overwrite=False)
    temp = '.partial-' + uuid.uuid4().hex
    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            if _read_at(fd, name) != data:
                raise ValueError('Existing cache target differs; never overwritten')
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass
























def save_stage(path, stage):
    """Explicit local opt-in; atomic and no overwrite, including symlink targets."""
    path = Path(os.path.abspath(path))
    data = json.dumps(stage, ensure_ascii=False, indent=2).encode()
    if len(data) > MAX_BYTES:
        raise ValueError('Stage output exceeds 32MiB')
    fd = _directory(path.parent)
    try:
        _atomic(fd, path.name, data)
    finally:
        close_directory(fd)
