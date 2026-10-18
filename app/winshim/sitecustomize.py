"""Windows gaps in os that Local SEO Brain's runtime.py relies on (loaded automatically from PYTHONPATH).

- os.O_NOFOLLOW: Windows has no such flag; the brain separately rejects symlinks before opening.
- os.O_DIRECTORY + os.fsync(dir): Windows can't open or fsync a folder (NTFS needs no folder fsync after
  os.replace), so opening a folder with O_DIRECTORY returns a placeholder handle that fsync skips.
"""
import os

if os.name == "nt":
    if not hasattr(os, "O_NOFOLLOW"):
        os.O_NOFOLLOW = 0
    if not hasattr(os, "O_DIRECTORY"):
        os.O_DIRECTORY = 0x40000000  # a bit no real Windows open flag uses
        _open, _fsync, _close = os.open, os.fsync, os.close
        _dirs: set[int] = set()

        def _shim_open(path, flags, mode=0o777, *, dir_fd=None):
            if flags & os.O_DIRECTORY:
                if not os.path.isdir(path):
                    raise NotADirectoryError(path)
                fd = _open(os.devnull, os.O_RDONLY)
                _dirs.add(fd)
                return fd
            return _open(path, flags, mode, dir_fd=dir_fd)

        def _shim_fsync(fd):
            if (fd if isinstance(fd, int) else fd.fileno()) in _dirs:
                return
            _fsync(fd)

        def _shim_close(fd):
            _dirs.discard(fd)
            _close(fd)

        os.open, os.fsync, os.close = _shim_open, _shim_fsync, _shim_close
