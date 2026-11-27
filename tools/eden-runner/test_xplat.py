import json
import sys
import tempfile
import types
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
        self.assertEqual(self.listener([{'pid': 20, 'line': line}]), 20)

    def test_owned_listener_windows_skips_non_dict_entries(self):
        line = 'TCP 0.0.0.0:7041 0.0.0.0:0 LISTENING 20'
        self.assertEqual(self.listener([{'pid': 20, 'line': line}],
                                       [None, {'pid': 20, 'image': r'C:\Eden\x\eden.exe'}]), 20)

    def test_owned_listener_windows_refuses_a_foreign_listener(self):
        with self.assertRaises(RuntimeError) as error:
            self.listener([{'pid': 20, 'line': 'OWNED'}, {'pid': 30, 'line': 'FOREIGN'}])
        self.assertIn('port 7041 is not bound by the owned Eden (pid 20): ', str(error.exception))
        self.assertIn('OWNED', str(error.exception))
        self.assertIn('FOREIGN', str(error.exception))

    def test_owned_listener_windows_refuses_no_listener(self):
        with self.assertRaisesRegex(RuntimeError, r'port 7041.*pid 20.*no listener'):
            self.listener([])

    def test_owned_listener_windows_refuses_without_an_eden_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'invalid owned pid'):
            self.listener([], self.owned[:1])

    def test_capture_spawn_keywords_per_os(self):
        class Spawned(Exception):
            pass

        class StopAfterSpawn(dict):
            def setdefault(self, *args):
                raise Spawned()

        def call(method, args):
            return {'value': args['key'] == 'core.use_gdbstub'}

        ctx = types.SimpleNamespace(instance={}, call=call, check_stop=lambda: None, row_dir=self.root / 'row',
                                    on='offline', runner=types.SimpleNamespace(instances=StopAfterSpawn()))
        token = 'a' * 32
        with mock.patch.object(routes.subprocess, 'Popen', return_value=types.SimpleNamespace(pid=123)) as popen, \
                mock.patch.object(routes, 'instance_path', side_effect=KeyError), \
                mock.patch.object(routes, '_bridge_on_stub_enable', return_value={}), \
                mock.patch.object(routes, '_bridge_on_events', return_value=['[Bridge] listening port=7041']), \
                mock.patch.object(routes, '_bridge_on_owned_listener', return_value=20), \
                mock.patch.object(routes, '_bridge_on_run_token', return_value=token), \
                mock.patch.object(routes.portable_lock, 'lock_exclusive'):
            for windows, flags, detached in ((False, [], {'start_new_session': True}),
                                             (True, ['-X', 'utf8'], {'creationflags': 0x1000208})):
                with mock.patch.object(host_os, 'WINDOWS', windows), self.assertRaises(Spawned):
                    routes.bridge_on(ctx, port=7041)
                run = self.root / 'bridge-run'
                self.assertEqual(popen.call_args.args[0],
                                 [sys.executable, *flags, '-I', str(HERE / 'switch_hw.py'), 'capture-bridge',
                                  str(run), '--bridge-host', '127.0.0.1:7041', '--owner-pid', '20',
                                  '--run-token', token, '--console', str(run / 'bridge-console.log')])
                stderr = popen.call_args.kwargs['stderr']
                self.assertEqual(Path(stderr.name), run / 'capture.stderr.log')
                self.assertEqual(popen.call_args.kwargs,
                                 {'stdin': routes.subprocess.DEVNULL, 'stdout': routes.subprocess.DEVNULL,
                                  'stderr': stderr, **detached})


if __name__ == '__main__':
    unittest.main()
