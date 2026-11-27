"""Exclusive advisory locking for runner sidecar files."""

import errno
import os
import time

try:
    import fcntl
except ModuleNotFoundError:
    import msvcrt

    fcntl = None


def lock_exclusive(stream, *, blocking=True):
    """Lock a writable binary sidecar until the stream is closed."""
    if fcntl is not None:
        mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(stream, mode)
        return

    # All Windows contenders lock the same byte, even on an empty file.
    fd = stream.fileno()
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno != errno.EACCES:
                    raise
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, "lock is held") from error
                time.sleep(0.05)
    finally:
        os.lseek(fd, offset, os.SEEK_SET)

