"""Write the Houdini package file (packages/geobox.json) pointing at this repo."""
from __future__ import annotations

import glob
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def package_json(root: str = REPO_ROOT) -> dict:
    root = root.replace("\\", "/")
    return {
        "env": [
            {"GEOBOX": root},
            {"PYTHONPATH": {"value": "$GEOBOX/houdini/python", "method": "prepend"}},
        ],
        "path": "$GEOBOX/houdini",
    }


def candidate_pref_dirs():
    home = os.path.expanduser("~")
    pats = [os.path.join(home, "houdini[0-9]*.[0-9]*"),
            os.path.join(home, "Documents", "houdini[0-9]*.[0-9]*"),
            os.path.join(home, "Library", "Preferences", "houdini", "[0-9]*.[0-9]*")]
    if os.environ.get("HOUDINI_USER_PREF_DIR"):
        pats.insert(0, os.environ["HOUDINI_USER_PREF_DIR"])
    out = []
    for p in pats:
        out += [d for d in glob.glob(p) if os.path.isdir(d)]
    return sorted(set(out))


def install(prefs=None) -> list:
    dirs = [prefs] if prefs else candidate_pref_dirs()
    data = json.dumps(package_json(), indent=2)
    written = []
    for d in dirs:
        pk = os.path.join(d, "packages")
        os.makedirs(pk, exist_ok=True)
        f = os.path.join(pk, "geobox.json")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(data + "\n")
        written.append(f)
    if not written:
        print("No Houdini preference folder found. Copy this into <houdini prefs>/packages/geobox.json:\n")
        print(data)
    return written


if __name__ == "__main__":
    for w in install(sys.argv[1] if len(sys.argv) > 1 else None):
        print("wrote", w)
