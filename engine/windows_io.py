"""Native Windows file handles for a private, local Hermes profile."""
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
from pathlib import Path
import re
import stat

k = c.WinDLL('kernel32', use_last_error=True)

def api(name, args, result):
    fn = getattr(k, name)
    fn.argtypes, fn.restype = args, result
    return fn

open_handle = api('CreateFileW', [w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE], w.HANDLE)
close_handle = api('CloseHandle', [w.HANDLE], w.BOOL)
info = api('GetFileInformationByHandleEx', [w.HANDLE,c.c_int,w.LPVOID,w.DWORD], w.BOOL)
drive_type = api('GetDriveTypeW', [w.LPCWSTR], w.UINT)


class StandardInfo(c.Structure):
    _fields_ = [('allocation_size', c.c_int64), ('end_of_file', c.c_int64),
                ('links', w.DWORD), ('delete_pending', c.c_ubyte), ('directory', c.c_ubyte)]


def checked_handle(path, *, directory=False, access=0x80000000, create=3):
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = open_handle(str(path), access, 3 if directory else 1, None, create, flags, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        attrs = (w.DWORD * 2)()
        if not info(handle, 9, c.byref(attrs), c.sizeof(attrs)):
            raise c.WinError(c.get_last_error())
        if attrs[0] & 0x400 or bool(attrs[0] & 0x10) != directory:
            raise ValueError('Unexpected Windows file type or reparse point')
        if not directory:
            standard = StandardInfo()
            if not info(handle, 1, c.byref(standard), c.sizeof(standard)):
                raise c.WinError(c.get_last_error())
            if standard.links != 1 or standard.delete_pending:
                raise ValueError('Unexpected Windows hardlink or pending deletion')
        return handle
    except BaseException:
        close_handle(handle)
        raise


class Directory:
    def __init__(self, path, create):
        self.path = Path(os.path.abspath(path))
        self.handles = []
        if not re.fullmatch(r'[A-Za-z]:\\', self.path.anchor) or drive_type(self.path.anchor) != 3:
            raise ValueError('Windows state requires a local fixed drive')
        current = Path(self.path.anchor)
        try:
            self.handles.append(checked_handle(current, directory=True, access=0x80))
            for part in self.path.parts[1:]:
                current /= part
                if create:
                    current.mkdir(exist_ok=True)
                self.handles.append(checked_handle(current, directory=True, access=0x80))
        except BaseException:
            self.close()
            raise

    def close(self):
        for handle in reversed(self.handles):
            close_handle(handle)
        self.handles.clear()

    def leaf(self, name):
        if not name or name in ('.','..') or name.endswith(('.', ' ')) or re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
            raise ValueError('Invalid Windows state file name')
        reserved = name.split('.')[0].rstrip().upper()
        if reserved in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$'} or re.fullmatch(r'(?:COM|LPT)[0-9¹²³]', reserved):
            raise ValueError('Reserved Windows state file name')
        return self.path / name

    def open(self, name, *, lease=False):
        h = checked_handle(self.leaf(name), access=0xc0000000 if lease else 0x80000000, create=4 if lease else 3)
        try:
            fd = msvcrt.open_osfhandle(h, os.O_BINARY | (os.O_RDWR if lease else os.O_RDONLY))
        except BaseException:
            close_handle(h)
            raise
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode):
            os.close(fd)
            raise ValueError('Unexpected Windows state file type or hardlink')
        return fd
