"""Windows stand-in for the one fcntl call Local SEO Brain makes: flock() on its vault lock file.

Only put on PYTHONPATH for brain scripts on Windows (see app/brain.py); vendor/ stays unmodified.
"""
import msvcrt
import os

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8


def flock(fd: int, operation: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if operation & LOCK_UN:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK if operation & LOCK_NB else msvcrt.LK_LOCK, 1)
    except OSError as e:
        if operation & LOCK_NB:
            raise BlockingIOError(*e.args) from e
        raise
