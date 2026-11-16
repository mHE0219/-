"""Small filesystem helpers that behave the same on Windows and POSIX.

* atomic_replace: os.replace retried on PermissionError — on Windows the target
  can be momentarily open in Houdini, an editor, the web server or antivirus.
* relpath: forward-slash relative path; falls back to an absolute path when
  `path` and `start` are on different Windows drives (os.path.relpath raises).
"""
from __future__ import annotations

import os
import time


def atomic_replace(src: str, dst: str, attempts: int = 20, delay: float = 0.05) -> None:
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (1 + i))


def relpath(path: str, start: str) -> str:
    try:
        return os.path.relpath(path, start).replace(os.sep, "/")
    except ValueError:  # different drives on Windows
        return os.path.abspath(path).replace(os.sep, "/")
