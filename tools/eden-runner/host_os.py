"""Host-specific process, listener and interactive-task operations."""

import csv
import io
import os
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

WINDOWS = os.name == 'nt'
_TASK_NAME = re.compile(r'Eden[A-Za-z]+-[0-9a-f]{12}')
_POWERSHELL = r'C:\Program Files\PowerShell\7\pwsh.exe'


def _run(argv, run):
    try:
        result = run(argv, capture_output=True, text=True, timeout=20)
    except FileNotFoundError as error:
        raise RuntimeError(f'{argv[0]} is not available') from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f'{argv[0]} failed: timed out') from error
    except OSError as error:
        raise RuntimeError(f'{argv[0]} failed: {error}') from error
    if result.returncode:
        raise RuntimeError(f'{argv[0]} failed: {result.stderr or result.stdout}')
    return result.stdout


def tcp_listeners(port, run=subprocess.run):
    if WINDOWS:
        text = _run(['netstat', '-ano', '-p', 'TCP'], run)
        rows = []
        for line in text.splitlines():
            fields = line.split()
            if (len(fields) >= 5 and fields[0].upper() == 'TCP'
                    and fields[1].endswith(f':{port}')
                    and fields[2] in ('0.0.0.0:0', '[::]:0')):
                try:
                    pid = int(fields[-1])
                except ValueError:
                    continue
                rows.append({'pid': pid, 'line': line})
        return rows
    text = _run(['ss', '-H', '-ltnp', f'sport = :{port}'], run)
    rows = []
    for line in text.splitlines():
        pids = re.findall(r'pid=(\d+)', line)
        rows.extend({'pid': int(pid), 'line': line} for pid in pids)
        if not pids:
            rows.append({'pid': None, 'line': line})
    return rows


def detached():
    return {'creationflags': 0x8 | 0x200 | 0x01000000} if WINDOWS else {'start_new_session': True}


def python_flags():
    return ['-X', 'utf8'] if WINDOWS else []


def argv_has(pid, *needles, run=subprocess.run):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if WINDOWS:
        command = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
        text = _run([_POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command], run).strip()
        return all(needle in text for needle in needles) if text else None
    try:
        parts = Path(f'/proc/{int(pid)}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return None
    return all(os.fsencode(needle) in parts for needle in needles)


def _task_name(name):
    if not _TASK_NAME.fullmatch(name):
        raise ValueError(f'invalid task name: {name}')


def interactive_task_start(name, argv, cwd, limit_s, run=subprocess.run):
    if not WINDOWS:
        raise RuntimeError('interactive tasks require Windows')
    _task_name(name)
    text = _run(['whoami', '/user', '/fo', 'csv', '/nh'], run)
    try:
        sid = next(csv.reader(io.StringIO(text)))[1]
    except (StopIteration, IndexError, csv.Error) as error:
        raise RuntimeError('cannot read current user SID') from error
    if not re.fullmatch(r'S-\d-(?:\d+-)*\d+', sid):
        raise RuntimeError('invalid current user SID')
    xml = ('<?xml version="1.0" encoding="UTF-16"?><Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
           '<Triggers><TimeTrigger><StartBoundary>2099-01-01T00:00:00</StartBoundary><Enabled>false</Enabled></TimeTrigger></Triggers>'
           '<Principals><Principal id="Author"><UserId>' + escape(sid) +
           '</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
           '<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>'
           '<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT' + str(int(limit_s)) +
           'S</ExecutionTimeLimit></Settings><Actions Context="Author"><Exec><Command>' + escape(str(argv[0])) +
           '</Command><Arguments>' + escape(subprocess.list2cmdline([str(a) for a in argv[1:]])) +
           '</Arguments><WorkingDirectory>' + escape(str(cwd)) + '</WorkingDirectory></Exec></Actions></Task>')
    path = Path(cwd) / f'{name}.task.xml'
    path.write_text(xml, encoding='utf-16')
    _run(['schtasks.exe', '/create', '/f', '/tn', name, '/xml', str(path)], run)
    try:
        _run(['schtasks.exe', '/run', '/tn', name], run)
    except Exception:
        interactive_task_end(name, run=run)
        raise
    return path


def interactive_task_running(name, run=subprocess.run):
    _task_name(name)
    return 'Running' in _run(['schtasks.exe', '/query', '/tn', name, '/fo', 'LIST'], run)


def interactive_task_end(name, run=subprocess.run):
    try:
        _task_name(name)
        for args in (['/end', '/tn', name], ['/delete', '/f', '/tn', name]):
            try:
                _run(['schtasks.exe', *args], run)
            except Exception:
                pass
    except Exception:
        pass
