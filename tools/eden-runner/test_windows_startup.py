import contextlib
import errno
import io
import os
import subprocess
import tempfile
import runpy
import sys
import types
from pathlib import Path
from unittest import TestCase, main, mock

HERE = Path(__file__).resolve().parent


class WindowsStartupTest(TestCase):
    def test_runner_starts_without_fcntl(self):
        # The Linux test host has no msvcrt. Substitute the module without
        # altering os.name or exercising unrelated Windows platform branches.
        with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": types.ModuleType("msvcrt")}):
            for name in ("judge_queue", "switch_hw", "portable_lock", "routes", "runner"):
                sys.modules.pop(name, None)
            with mock.patch.object(sys, "argv", ["runner.py", "--help"]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(Path(__file__).with_name("runner.py")), run_name="__main__")
            self.assertEqual(result.exception.code, 0)
            import switch_hw  # Imports the bridge capture locking path.
            import routes
            import runner
            self.assertTrue(callable(switch_hw.capture_bridge))
            self.assertTrue(callable(routes.bridge_on))
            self.assertTrue(callable(runner.main))



class PortableLockTest(TestCase):
    def test_contention_and_release(self):
        sys.path.insert(0, str(HERE))
        self.addCleanup(sys.path.remove, str(HERE))
        from portable_lock import lock_exclusive

        child = """
import sys
sys.path.insert(0, sys.argv[1])
from portable_lock import lock_exclusive
with open(sys.argv[2], "a+b") as lock:
    try:
        lock_exclusive(lock, blocking=False)
    except BlockingIOError:
        sys.exit(7)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            path.write_bytes(b"existing")
            argv = [sys.executable, "-B", "-I", "-c", child, str(HERE), str(path)]
            with path.open("a+b") as lock:
                lock.seek(3)
                lock_exclusive(lock)
                self.assertEqual(lock.tell(), 3)
                self.assertEqual(subprocess.run(argv, timeout=10).returncode, 7)
            self.assertEqual(subprocess.run(argv, timeout=10).returncode, 0)
            self.assertEqual(path.read_bytes(), b"existing")

    def test_windows_blocking_retry(self):
        runtime = types.ModuleType("msvcrt")
        runtime.LK_NBLCK = 2
        with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": runtime}):
            acquire = runpy.run_path(str(HERE / "portable_lock.py"))["lock_exclusive"]
        with tempfile.TemporaryFile("w+b") as stream:
            stream.seek(3)

            def locking(fd, mode, length):
                self.assertEqual((fd, mode, length), (stream.fileno(), runtime.LK_NBLCK, 1))
                self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
                if runtime.locking.call_count == 1:
                    raise OSError(errno.EACCES, "lock is held")

            runtime.locking = mock.Mock(side_effect=locking)
            with mock.patch("time.sleep") as sleep:
                acquire(stream)
            self.assertEqual(runtime.locking.call_count, 2)
            sleep.assert_called_once()
            self.assertEqual(stream.tell(), 3)


if __name__ == "__main__":
    main()
