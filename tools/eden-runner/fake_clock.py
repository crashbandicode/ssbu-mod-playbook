"""A clock for host tests: time moves only when the code under test sleeps or a fixture spends it."""
import time as _time
from unittest.mock import patch


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def monotonic_ns(self):
        return int(self.now * 1_000_000_000)

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.spend(seconds)

    def spend(self, seconds):
        if seconds > 0:
            self.now += seconds

    def __getattr__(self, name):
        return getattr(_time, name)


def install(test, *modules, start=1000.0):
    """Give each module this one clock as its `time` for the length of `test`."""
    clock = FakeClock(start)
    for module in modules:
        patcher = patch.object(module, 'time', clock)
        patcher.start()
        test.addCleanup(patcher.stop)
    return clock
