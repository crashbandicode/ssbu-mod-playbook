import os
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import host_os


def result(text='', rc=0, error=''):
    return subprocess.CompletedProcess([], rc, stdout=text, stderr=error)


class HostOsTests(unittest.TestCase):
    def test_runs_root_per_os(self):
        self.assertEqual(host_os._runs_root(False), Path('/home/intpa/eden-runs'))
        self.assertEqual(host_os._runs_root(True), Path('C:/home/intpa/eden-runs'))

    def test_windows_processes_parsing(self):
        one = {'ProcessId': 12, 'ParentProcessId': 3, 'Name': 'eden.exe',
               'CommandLine': None, 'ExecutablePath': r'C:\Eden\eden.exe'}
        two = {'ProcessId': 13, 'ParentProcessId': 12, 'Name': 'other.exe',
               'CommandLine': 'other', 'ExecutablePath': None}
        run = mock.Mock(return_value=result(json.dumps(one)))
        self.assertEqual(host_os.windows_processes(run), [{'pid': 12, 'ppid': 3, 'name': 'eden.exe',
                                                            'command': None, 'exe': r'C:\Eden\eden.exe'}])
        run.return_value = result(json.dumps([one, two]))
        self.assertEqual([p['pid'] for p in host_os.windows_processes(run)], [12, 13])
        self.assertEqual(run.call_args.args[0][0], host_os._POWERSHELL)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        run.return_value = result(rc=1, error='access failed')
        with self.assertRaisesRegex(OSError, 'access failed'):
            host_os.windows_processes(run)

    def test_windows_interfaces_parsing_and_failures(self):
        interfaces = [
            {'name': 'Wi-Fi', 'up': True, 'loopback': False, 'ipv4': True},
            {'name': 'Loopback Pseudo-Interface 1', 'up': True, 'loopback': True, 'ipv4': True},
            {'name': 'Ethernet 2', 'up': False, 'loopback': False, 'ipv4': False},
        ]
        run = mock.Mock(return_value=result(json.dumps(interfaces)))
        self.assertEqual(host_os.windows_interfaces(run), interfaces)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], [host_os._POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command'])
        self.assertIn('Get-NetAdapter -IncludeHidden', argv[-1])
        self.assertIn('Get-NetIPAddress -AddressFamily IPv4', argv[-1])
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        run.return_value = result('[]')
        self.assertEqual(host_os.windows_interfaces(run), [])
        for malformed in ('{}', '[{"name":"Wi-Fi","up":1,"loopback":false,"ipv4":true}]', 'not json'):
            run.return_value = result(malformed)
            with self.subTest(malformed=malformed), self.assertRaises(OSError):
                host_os.windows_interfaces(run)
        run.return_value = result(rc=1, error='access failed')
        with self.assertRaisesRegex(OSError, 'access failed'):
            host_os.windows_interfaces(run)

    def test_linux_listeners_parse_ss(self):
        run = mock.Mock(return_value=result(
            'LISTEN 0 4096 0.0.0.0:7041 0.0.0.0:* users:(("eden",pid=4242,fd=99),("x",pid=42,fd=1))\n'
            'LISTEN 0 4096 [::]:7041 [::]:*\n'))
        with mock.patch.object(host_os, 'WINDOWS', False):
            rows = host_os.tcp_listeners(7041, run)
        self.assertEqual([r['pid'] for r in rows], [4242, 42, None])
        self.assertEqual(run.call_args.args[0], ['ss', '-H', '-ltnp', 'sport = :7041'])

    def test_windows_listeners_parse_netstat(self):
        text = '''Active Connections
  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:22             0.0.0.0:0              LISTENING       5512
  TCP    0.0.0.0:7041           0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:7041         127.0.0.1:51123        ESTABLISHED     4242
  TCP    127.0.0.1:51123        127.0.0.1:7041         ESTABLISHED     777
  TCP    [::]:7041              [::]:0                 LISTENING       4242
  TCP    0.0.0.0:17041          0.0.0.0:0              LISTENING       9
'''
        run = mock.Mock(return_value=result(text))
        with mock.patch.object(host_os, 'WINDOWS', True):
            rows = host_os.tcp_listeners(7041, run)
        self.assertEqual([r['pid'] for r in rows], [4242, 4242])
        self.assertEqual(run.call_args.args[0], ['netstat', '-ano', '-p', 'TCP'])

    def test_listener_errors(self):
        with mock.patch.object(host_os, 'WINDOWS', False):
            with self.assertRaisesRegex(RuntimeError, 'ss is not available'):
                host_os.tcp_listeners(1, mock.Mock(side_effect=FileNotFoundError()))
            with self.assertRaisesRegex(RuntimeError, 'ss failed: bad'):
                host_os.tcp_listeners(1, mock.Mock(return_value=result(rc=1, error='bad')))

    def test_command_timeout(self):
        run = mock.Mock(side_effect=subprocess.TimeoutExpired('ss', 20))
        with mock.patch.object(host_os, 'WINDOWS', False):
            with self.assertRaisesRegex(RuntimeError, '^ss failed: timed out$'):
                host_os.tcp_listeners(7041, run)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)

    def test_detached_per_os(self):
        with mock.patch.object(host_os, 'WINDOWS', False):
            self.assertEqual(host_os.detached(), {'start_new_session': True})
        with mock.patch.object(host_os, 'WINDOWS', True):
            self.assertEqual(host_os.detached(), {'creationflags': 0x1000208})

    def test_python_flags_per_os(self):
        with mock.patch.object(host_os, 'WINDOWS', False):
            self.assertEqual(host_os.python_flags(), [])
        with mock.patch.object(host_os, 'WINDOWS', True):
            self.assertEqual(host_os.python_flags(), ['-X', 'utf8'])

    @unittest.skipIf(os.name == 'nt', 'Linux /proc only')
    def test_argv_has_linux_own_process(self):
        self.assertTrue(host_os.argv_has(os.getpid(), sys.executable))
        self.assertFalse(host_os.argv_has(os.getpid(), 'not-a-command-argument'))

    def test_argv_has_windows(self):
        run = mock.Mock(return_value=result('"C:\\Python 312\\pythonw.exe" -I capture-bridge C:\\runs\\one'))
        with mock.patch.object(host_os, 'WINDOWS', True):
            self.assertTrue(host_os.argv_has(42, 'capture-bridge', 'C:\\runs\\one', run=run))
            self.assertFalse(host_os.argv_has(42, 'wrong', run=run))
            run.return_value = result()
            self.assertIsNone(host_os.argv_has(42, 'anything', run=run))
        self.assertIn("ProcessId=42", run.call_args.args[0][-1])
        self.assertEqual(run.call_args.args[0][0], host_os._POWERSHELL)

    def test_interactive_task_xml(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return result('"DOMAIN\\user","S-1-5-21-123"\n' if argv[0] == 'whoami' else '')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(host_os, 'WINDOWS', True):
            name = 'EdenFilm-012345abcdef'
            argv = ['C:\\Program Files\\Python\\pythonw.exe', 'space here', 'a&b']
            path = host_os.interactive_task_start(name, argv, tmp, 63, run)
            root = ET.parse(path).getroot()
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            self.assertEqual(root.findtext('.//t:UserId', namespaces=ns), 'S-1-5-21-123')
            self.assertEqual(root.findtext('.//t:LogonType', namespaces=ns), 'InteractiveToken')
            self.assertEqual(root.findtext('.//t:Command', namespaces=ns), argv[0])
            self.assertEqual(root.findtext('.//t:Arguments', namespaces=ns), subprocess.list2cmdline(argv[1:]))
            self.assertEqual(root.findtext('.//t:WorkingDirectory', namespaces=ns), tmp)
            self.assertEqual(root.findtext('.//t:ExecutionTimeLimit', namespaces=ns), 'PT63S')
            self.assertEqual([c[1] for c in calls[1:]], ['/create', '/run'])
            with self.assertRaises(ValueError):
                host_os.interactive_task_start('bad', argv, tmp, 63, run)
            bad = mock.Mock(return_value=result('"user","bad"\n'))
            with self.assertRaisesRegex(RuntimeError, 'SID'):
                host_os.interactive_task_start(name, argv, tmp, 63, bad)
            self.assertEqual(bad.call_count, 1)

    def test_interactive_task_failure_cleans_up(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'whoami':
                return result('"user","S-1-5-21-123"')
            return result(rc=1, error='run failed') if argv[1] == '/run' else result()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(host_os, 'WINDOWS', True):
            with self.assertRaisesRegex(RuntimeError, 'run failed'):
                host_os.interactive_task_start('EdenFilm-012345abcdef', ['pythonw'], tmp, 63, run)
        self.assertEqual([c[1] for c in calls[1:]], ['/create', '/run', '/end', '/delete'])


if __name__ == '__main__':
    unittest.main()
