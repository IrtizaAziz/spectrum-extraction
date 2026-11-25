"""Windows Edge listener/process attestation. Never starts a browser."""
import ctypes as c
from ctypes import wintypes as w
import json
import ntpath
import os
from pathlib import Path
import subprocess


def split_command(command):
    shell = c.WinDLL('shell32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, c.POINTER(c.c_int)]
    shell.CommandLineToArgvW.restype = c.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    count = c.c_int()
    ptr = shell.CommandLineToArgvW(command, c.byref(count))
    if not ptr:
        raise c.WinError(c.get_last_error())
    try:
        return [ptr[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(ptr)


def attest(executable, args, home, install_roots):
    normalize = lambda value: ntpath.normcase(ntpath.normpath(value))
    approved = {normalize(ntpath.join(root, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
                for root in install_roots if root and ntpath.isabs(root)}
    if normalize(executable) not in approved or not args or normalize(args[0]) != normalize(executable):
        raise ValueError('Windows listener is not the installed Microsoft Edge executable')
    flags = {}
    for arg in args[1:]:
        key, _, value = arg.partition('=')
        key = key.lower()
        if key in ('--user-data-dir','--remote-debugging-port'):
            if key in flags or not value:
                raise ValueError('Ambiguous Windows Edge flags')
            flags[key] = value
    directory = flags.get('--user-data-dir','')
    if not ntpath.isabs(directory) or normalize(directory) != normalize(ntpath.join(home,'.hermes','spectrum-edge')) or flags.get('--remote-debugging-port') != '9337':
        raise ValueError('Windows listener profile or port mismatch')


def process_identity():
    # Two listener snapshots reduce the chance of accepting a PID replaced during inspection.
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Listener {
  $rows = @(Get-NetTCPConnection -LocalPort 9337 -State Listen)
  if ($rows.Count -ne 1 -or $rows[0].LocalAddress -ne '127.0.0.1') { throw 'Unexpected listener' }
  return [int]$rows[0].OwningProcess
}
$owner = Listener
$p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $owner)
if ((Listener) -ne $owner -or !$p.ExecutablePath -or !$p.CommandLine) { throw 'Process changed or unavailable' }
@{pid=$owner; executable=$p.ExecutablePath; command=$p.CommandLine} | ConvertTo-Json -Compress
"""
    powershell = Path(os.environ['SystemRoot'])/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, check=True, timeout=8, encoding='utf-8')
    row = json.loads(result.stdout)
    return str(row['pid']), '127.0.0.1:9337', row['executable'], row['command']
