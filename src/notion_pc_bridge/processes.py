"""Владение процессами: Job Object в Windows, группы процессов в macOS/Linux.

Процесс опознаётся по PID + времени запуска + исполняемому файлу, поэтому
повторно выданный системой PID никогда не будет принят за свой.
"""
from __future__ import annotations
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import time

if os.name == 'nt':
    from ctypes import wintypes


def _api():
    dll = ctypes.WinDLL('kernel32', use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)]*4
    dll.GetProcessTimes.restype = wintypes.BOOL
    dll.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.QueryFullProcessImageNameW.restype = wintypes.BOOL
    dll.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateProcess.restype = wintypes.BOOL
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    return dll


def _identity(dll, handle, pid):
    if dll.WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT means still running
        return None
    times = [wintypes.FILETIME() for _ in range(4)]
    if not dll.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
        return None
    size = wintypes.DWORD(32768)
    name = ctypes.create_unicode_buffer(size.value)
    if not dll.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
        return None
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return {'pid': int(pid), 'created': str(created), 'image': os.path.normcase(name.value)}


def _posix_identity(pid):
    """Linux: время запуска из /proc; macOS: из ps. Зомби считаются завершёнными."""
    proc = Path('/proc')/str(pid)
    if Path('/proc/self/stat').exists():
        try:
            stat = (proc/'stat').read_text(encoding='utf-8', errors='replace')
        except OSError:
            return None
        fields = stat[stat.rindex(')')+2:].split()
        if len(fields) < 20 or fields[0] in ('Z', 'X'):
            return None
        try:
            image = os.readlink(proc/'exe')
        except OSError:
            image = ''
        return {'pid': int(pid), 'created': fields[19], 'image': image}
    try:
        result = subprocess.run(['ps', '-o', 'stat=', '-o', 'lstart=', '-o', 'comm=', '-p', str(pid)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                env={**os.environ, 'LC_ALL': 'C'}, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.strip().split(None, 6)
    if result.returncode or len(parts) < 7 or parts[0].startswith('Z'):
        return None
    return {'pid': int(pid), 'created': ' '.join(parts[1:6]), 'image': parts[6]}


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    if os.name != 'nt':
        return _posix_identity(pid)
    dll = _api()
    handle = dll.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        return None
    try:
        return _identity(dll, handle, pid)
    finally:
        dll.CloseHandle(handle)


def same_process(record):
    if not isinstance(record, dict):
        return False
    current = identity(record.get('pid'))
    return current is not None and current == record


def _posix_terminate(record):
    """SIGTERM всей группе (прокси + Desktop Commander), через 5 с — SIGKILL."""
    pid = record['pid']
    if _posix_identity(pid) != record:
        return False
    try:
        group = os.getpgid(pid) == pid
    except ProcessLookupError:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig) if group else os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        for _ in range(50):
            if _posix_identity(pid) != record:
                return True
            time.sleep(0.1)
    return _posix_identity(pid) != record


def terminate_exact(record):
    """Verify birth time and executable on the SAME handle used to terminate."""
    if not isinstance(record, dict) or type(record.get('pid')) is not int:
        return False
    if os.name != 'nt':
        return _posix_terminate(record)
    dll = _api()
    handle = dll.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, record['pid'])
    if not handle:
        return False
    try:
        if _identity(dll, handle, record['pid']) != record:
            return False
        if not dll.TerminateProcess(handle, 0):
            return False
        dll.WaitForSingleObject(handle, 5000)
        return True
    finally:
        dll.CloseHandle(handle)


def find_owned(root):
    """Find legacy orphan roots by exact project paths, never by port or process name alone.

    PowerShell emits no command lines or keys; birth time is compared with the
    native handle before a process can be terminated.
    """
    if os.name != 'nt':
        return _posix_find_owned(root)
    root = str(Path(root).resolve()).replace('/', '\\').lower().rstrip('\\')
    proxy = (root+'\\node_modules\\mcp-proxy\\dist\\bin\\mcp-proxy.mjs').replace("'", "''")
    commander = (root+'\\node_modules\\@wonderwhy-er\\desktop-commander\\dist\\index.js').replace("'", "''")
    agent = (root+'\\runtime\\ngrok-agent.yml').replace("'", "''")
    legacy_agent = (root+'\\ngrok-agent.yml').replace("'", "''")
    script = f"""$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $out=@();
Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='ngrok.exe'\" | ForEach-Object {{
 $c=([string]$_.CommandLine).Replace('/','\\').ToLowerInvariant(); $role='';
 if ($_.Name -ieq 'node.exe' -and $c.Contains('{proxy}') -and $c.Contains('{commander}')) {{ $role='proxy' }};
 if ($_.Name -ieq 'ngrok.exe' -and ($c.Contains('{agent}') -or $c.Contains('{legacy_agent}'))) {{ $role='ngrok' }};
 if ($role) {{ $out += [pscustomobject]@{{pid=[int]$_.ProcessId;created=$_.CreationDate.ToFileTimeUtc().ToString();image=([string]$_.ExecutablePath).ToLowerInvariant();role=$role}} }}
}}; ConvertTo-Json -InputObject $out -Compress"""
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Не удалось безопасно проверить оставшиеся процессы этой папки.')
        data = json.loads(result.stdout or '[]')
        return data if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Не удалось безопасно проверить оставшиеся процессы этой папки.') from exc


def _posix_find_owned(root):
    """Сироты прошлого запуска (например, после kill -9) по точным путям этой папки."""
    root = Path(root).resolve()
    proxy = str(root/'node_modules'/'mcp-proxy'/'dist'/'bin'/'mcp-proxy.mjs')
    commander = str(root/'node_modules'/'@wonderwhy-er'/'desktop-commander'/'dist'/'index.js')
    agent = str(root/'runtime'/'ngrok-agent.yml')
    try:
        result = subprocess.run(['ps', '-A', '-ww', '-o', 'pid=', '-o', 'args='], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    records = []
    for line in result.stdout.splitlines():
        pid_text, _, args = line.strip().partition(' ')
        if not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        role = ('proxy' if proxy in args and commander in args else 'ngrok' if agent in args
                else 'commander' if commander in args else '')
        current = _posix_identity(int(pid_text)) if role else None
        name = Path(current['image']).name.lower() if current else ''
        if current and (('ngrok' in name) if role == 'ngrok' else ('node' in name)):
            records.append({**current, 'role': role})
    return records


def recover_owned(root):
    count = 0
    records = find_owned(root)
    # Removing the tunnel first prevents new remote requests during recovery.
    order = {'ngrok': 0, 'proxy': 1, 'commander': 2}
    for item in sorted(records, key=lambda record: order.get(record['role'], 3)):
        observed = {key: item[key] for key in ('pid', 'created', 'image')}
        current = identity(observed['pid'])
        # CIM truncates FILETIME to microseconds. Re-check exact native birth time
        # on the termination handle after matching this discovery snapshot.
        if os.name != 'nt':
            matches = current == observed
        else:
            matches = bool(current) and (current['image'] == observed['image']
                                         and int(current['created'])//10 == int(observed['created'])//10)
        if matches:
            if terminate_exact(current):
                count += 1
            elif same_process(current):
                raise RuntimeError('Не удалось завершить подтверждённый процесс этой папки.')
    return count


class ProcessJob:
    """Non-inheritable handle; closing the supervisor kills all assigned descendants."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        class BasicLimits(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
                        ('MaxWorkingSet', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]
        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('Basic', BasicLimits), ('Io', IoCounters), ('ProcessMemory', ctypes.c_size_t),
                        ('JobMemory', ctypes.c_size_t), ('PeakProcessMemory', ctypes.c_size_t), ('PeakJobMemory', ctypes.c_size_t)]
        self.dll = _api()
        self.dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.dll.CreateJobObjectW.restype = wintypes.HANDLE
        self.dll.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.dll.SetInformationJobObject.restype = wintypes.BOOL
        self.dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.dll.AssignProcessToJobObject.restype = wintypes.BOOL
        self.handle = self.dll.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.dll.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def add(self, process):
        if self.handle and not self.dll.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            error = ctypes.get_last_error()
            if process.poll() is not None:
                raise OSError(f'Дочерний процесс завершился до привязки к группе, код: {process.returncode}.')
            raise ctypes.WinError(error)

    def close(self):
        if self.handle:
            self.dll.CloseHandle(self.handle)
            self.handle = None
