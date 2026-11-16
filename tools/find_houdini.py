"""Locate Houdini installs (hython, bundled Python) and Houdini user-pref folders.

    python tools/find_houdini.py --hython      # newest hython path (exit 1 if none)
    python tools/find_houdini.py --python      # newest Houdini-bundled python
    python tools/find_houdini.py --json        # everything that was found

Works on Windows (Program Files\\Side Effects Software\\Houdini X.Y.Z), Linux
(/opt/hfsX.Y.Z) and macOS (/Applications/Houdini/HoudiniX.Y.Z). $HFS wins if set.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from typing import Dict, List, Optional

IS_WIN = os.name == "nt"


def _version(path: str):
    """Last X.Y.Z in the path (macOS keeps it in a parent folder)."""
    m = re.findall(r"(\d+)\.(\d+)\.(\d+)", path)
    return tuple(int(x) for x in m[-1]) if m else None


def _hfs_dirs(program_files: Optional[List[str]] = None) -> List[str]:
    dirs = []
    if IS_WIN or program_files:
        roots = program_files or [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        for r in filter(None, roots):
            dirs += glob.glob(os.path.join(r, "Side Effects Software", "Houdini *"))
    if not IS_WIN and program_files is None:
        dirs += glob.glob("/opt/hfs*")
        dirs += [os.path.join(d, "Frameworks", "Houdini.framework", "Versions", "Current", "Resources")
                 for d in glob.glob("/Applications/Houdini/Houdini*")]
    return [d for d in dirs if os.path.isdir(d)]


def _exe(name: str) -> str:
    return name + ".exe" if IS_WIN else name


def describe(hfs: str) -> Dict:
    ver = _version(hfs) or (0, 0, 0)
    hython = os.path.join(hfs, "bin", _exe("hython"))
    pythons = sorted(glob.glob(os.path.join(hfs, "python3*", _exe("python"))) +   # Windows: python311\python.exe
                     glob.glob(os.path.join(hfs, "python", "bin", "python3")))     # Linux
    return {"hfs": hfs, "version": ".".join(map(str, ver)), "major_minor": "%d.%d" % ver[:2], "_v": ver,
            "hython": hython if os.path.isfile(hython) else None,
            "python": pythons[-1] if pythons else None}


def installs(program_files: Optional[List[str]] = None, use_env: bool = True) -> List[Dict]:
    """All Houdini installs, newest first."""
    found = {}
    if use_env and os.environ.get("HFS") and os.path.isdir(os.environ["HFS"]):
        d = describe(os.environ["HFS"])
        d["from_env"] = True
        found[os.path.normcase(d["hfs"])] = d
    for h in _hfs_dirs(program_files):
        found.setdefault(os.path.normcase(h), describe(h))
    out = sorted(found.values(), key=lambda d: (d.get("from_env", False), d["_v"]), reverse=True)
    for d in out:
        d.pop("_v", None)
    return out


def documents_dir() -> str:
    """Real 'My Documents' (may be redirected to OneDrive) - where Houdini keeps houdiniX.Y on Windows."""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # CSIDL_PERSONAL
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def pref_dir_for(major_minor: str, home: Optional[str] = None, documents: Optional[str] = None,
                 platform: Optional[str] = None) -> str:
    env = os.environ.get("HOUDINI_USER_PREF_DIR")
    if env:
        return env.replace("__HVER__", major_minor)
    platform = platform or ("win" if IS_WIN else "mac" if sys.platform == "darwin" else "linux")
    home = home or os.path.expanduser("~")
    if platform == "win":
        return os.path.join(documents or documents_dir(), "houdini" + major_minor)
    if platform == "mac":
        return os.path.join(home, "Library", "Preferences", "houdini", major_minor)
    return os.path.join(home, "houdini" + major_minor)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hython", action="store_true")
    g.add_argument("--python", action="store_true")
    g.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    found = installs()
    if a.hython or a.python:
        key = "hython" if a.hython else "python"
        for d in found:
            if d.get(key):
                print(d[key])
                return 0
        return 1
    print(json.dumps({"installs": found, "pref_dirs": [pref_dir_for(d["major_minor"]) for d in found]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
