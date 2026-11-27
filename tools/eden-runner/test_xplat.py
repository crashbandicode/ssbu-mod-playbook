import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import host_os
import routes


class CrossPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = object()
        self.owned = [
            {'pid': 10, 'image': r'C:\Program Files\Python312\pythonw.exe'},
            {'pid': 20, 'image': r'C:\Eden\x\work\profiles\profile-a\eden.exe'},
        ]

    def listener(self, rows, owned=None):
        (self.root / 'run.json').write_text(json.dumps({'owned_pids': self.owned if owned is None else owned}))
        with mock.patch.object(host_os, 'WINDOWS', True), \
                mock.patch.object(routes, 'instance_path', return_value=self.root / 'control.log'), \
                mock.patch.object(host_os, 'tcp_listeners', return_value=rows):
            return routes._bridge_on_owned_listener(self.ctx, 7041)

    def test_owned_listener_windows_accepts_the_owned_eden(self):
        line = 'TCP 0.0.0.0:7041 0.0.0.0:0 LISTENING 20'
        self.assertEqual(self.listener([{'pid': 20, 'line': line}]), line)

    def test_owned_listener_windows_refuses_a_foreign_listener(self):
        with self.assertRaisesRegex(RuntimeError, r'port 7041.*pid \[20\].*FOREIGN'):
            self.listener([{'pid': 20, 'line': 'OWNED'}, {'pid': 30, 'line': 'FOREIGN'}])

    def test_owned_listener_windows_refuses_no_listener(self):
        with self.assertRaisesRegex(RuntimeError, r'port 7041.*pid \[20\].*no listener'):
            self.listener([])

    def test_owned_listener_windows_refuses_without_an_eden_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'no eden.exe among owned_pids'):
            self.listener([], self.owned[:1])

    def test_capture_spawn_keywords_per_os(self):
        source = ['--bridge-host', '127.0.0.1:7041']
        with mock.patch.object(routes.subprocess, 'Popen') as popen:
            for windows, flags, detached in ((False, [], {'start_new_session': True}),
                                              (True, ['-X', 'utf8'], {'creationflags': 0x1000208})):
                with mock.patch.object(host_os, 'WINDOWS', windows):
                    routes._bridge_on_spawn_capture(self.root, source, 'stderr')
                self.assertEqual(popen.call_args.args[0],
                                 [sys.executable, '-I', *flags, str(HERE / 'switch_hw.py'), 'capture-bridge',
                                  str(self.root), *source, '--console', str(self.root / 'bridge-console.log')])
                self.assertEqual(popen.call_args.kwargs,
                                 {'stdin': routes.subprocess.DEVNULL, 'stdout': routes.subprocess.DEVNULL,
                                  'stderr': 'stderr', **detached})


if __name__ == '__main__':
    unittest.main()
