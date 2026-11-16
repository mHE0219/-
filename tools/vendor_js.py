"""Copy exactly the front-end library files the viewer needs from
viewer/node_modules into viewer/vendor/, so the Web preview runs with no
Node.js / npm on the target machine (e.g. a Windows workstation).

    cd viewer && npm install          # only on the machine that updates the vendor copy
    python tools/vendor_js.py         # walks the import graph, copies, rewrites nothing else

The import graph starts at every module in viewer/js/** (plus dynamic
imports) and follows static/dynamic imports through the import map below.
The HTML import maps point at ./vendor/... (see IMPORT_MAP).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER = os.path.join(ROOT, "viewer")
NM = os.path.join(VIEWER, "node_modules")
VENDOR = os.path.join(VIEWER, "vendor")

# bare specifier -> path inside node_modules (prefix entries end with "/")
IMPORT_MAP = {
    "three": "three/build/three.module.js",
    "three/addons/": "three/examples/jsm/",
    "lil-gui": "lil-gui/dist/lil-gui.esm.js",
    "leaflet": "leaflet/dist/leaflet-src.esm.js",
    "3d-tiles-renderer": "3d-tiles-renderer/build/index.js",
    "3d-tiles-renderer/plugins": "3d-tiles-renderer/build/index.plugins.js",
}

# non-module assets loaded at runtime by URL
EXTRA_FILES = [
    "three/examples/jsm/libs/draco/gltf/draco_decoder.js",
    "three/examples/jsm/libs/draco/gltf/draco_decoder.wasm",
    "three/examples/jsm/libs/draco/gltf/draco_wasm_wrapper.js",
    "leaflet/dist/leaflet.css",
]
EXTRA_DIRS = ["leaflet/dist/images"]

# bare specifiers that are intentionally NOT vendored:
#  - lazy import() of optional 3d-tiles-renderer plugins we never enable (vector tiles / PMTiles)
#  - regex false positives inside comments / strings
IGNORE_SPECIFIERS = {"pmtiles", "pbf", "@mapbox/vector-tile", "srgb-linear", "Interactive layer"}
LICENSES = ["three/LICENSE", "lil-gui/LICENSE", "leaflet/LICENSE", "3d-tiles-renderer/LICENSE"]

_IMPORT_RES = [
    re.compile(r"""\bfrom\s*["']([^"'\n]+)["']"""),
    re.compile(r"""\bimport\s*\(\s*["']([^"'\n]+)["']\s*\)"""),
    re.compile(r"""(?:^|[;\n}])\s*import\s*["']([^"'\n]+)["']"""),
]


def specifiers(src: str):
    for rx in _IMPORT_RES:
        for m in rx.finditer(src):
            yield m.group(1)


def resolve_bare(spec: str):
    if spec in IMPORT_MAP:
        return IMPORT_MAP[spec]
    for k, v in IMPORT_MAP.items():
        if k.endswith("/") and spec.startswith(k):
            return v + spec[len(k):]
    return None


def walk(entries):
    """entries: absolute paths of app modules. Returns set of node_modules-relative paths."""
    seen_app, seen_lib = set(), set()
    stack = [("app", os.path.abspath(e)) for e in entries]
    missing = []
    while stack:
        kind, path = stack.pop()
        key = path
        if kind == "app":
            if key in seen_app or not os.path.isfile(path):
                continue
            seen_app.add(key)
        else:
            if key in seen_lib:
                continue
            if not os.path.isfile(os.path.join(NM, path)):
                missing.append(path)
                continue
            seen_lib.add(key)
        full = path if kind == "app" else os.path.join(NM, path)
        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
        for spec in set(specifiers(src)):
            if spec.startswith(("http:", "https:", "data:")) or spec in IGNORE_SPECIFIERS:
                continue
            if spec.startswith((".", "/")):
                if kind == "app":
                    tgt = os.path.normpath(os.path.join(os.path.dirname(full), spec))
                    if tgt.startswith(VIEWER + os.sep) and not tgt.startswith(NM):
                        stack.append(("app", tgt))
                else:
                    rel = os.path.normpath(os.path.join(os.path.dirname(path), spec)).replace(os.sep, "/")
                    stack.append(("lib", rel))
            else:
                r = resolve_bare(spec)
                if r is None:
                    missing.append("(unmapped bare import) %s  <- %s" % (spec, os.path.relpath(full, ROOT)))
                else:
                    stack.append(("lib", r))
    return seen_lib, missing


def app_entries():
    out = []
    for d, _dirs, files in os.walk(os.path.join(VIEWER, "js")):
        out += [os.path.join(d, f) for f in files if f.endswith((".js", ".mjs"))]
    for html in ("index.html", "sky.html"):
        p = os.path.join(VIEWER, html)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as fh:
                for m in re.finditer(r"""<script[^>]+type=["']module["'][^>]+src=["']([^"']+)["']""", fh.read()):
                    out.append(os.path.normpath(os.path.join(VIEWER, m.group(1))))
    return out


def main(argv=None):
    if not os.path.isdir(NM):
        print("viewer/node_modules not found: run `cd viewer && npm install` first", file=sys.stderr)
        return 2
    files, missing = walk(app_entries())
    files |= set(EXTRA_FILES) | {p for p in LICENSES if os.path.exists(os.path.join(NM, p))}
    for d in EXTRA_DIRS:
        for f in os.listdir(os.path.join(NM, d)):
            files.add(d + "/" + f)
    if os.path.isdir(VENDOR):
        shutil.rmtree(VENDOR)
    total = 0
    for rel in sorted(files):
        src = os.path.join(NM, rel)
        dst = os.path.join(VENDOR, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        total += os.path.getsize(src)
    versions = {}
    for pkg in ("three", "lil-gui", "leaflet", "3d-tiles-renderer"):
        with open(os.path.join(NM, pkg, "package.json"), "r", encoding="utf-8") as fh:
            versions[pkg] = json.load(fh)["version"]
    manifest = {"generated_by": "tools/vendor_js.py", "versions": versions,
                "import_map": {k: "./vendor/" + v for k, v in IMPORT_MAP.items()},
                "files": sorted(files)}
    with open(os.path.join(VENDOR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print("vendored %d files, %.2f MB -> %s" % (len(files), total / 1e6, os.path.relpath(VENDOR, ROOT)))
    print("versions: " + ", ".join("%s@%s" % kv for kv in versions.items()))
    real_missing = [m for m in missing if not m.startswith("(unmapped")]
    for m in missing:
        print("  ! " + m)
    return 1 if real_missing else 0


if __name__ == "__main__":
    sys.exit(main())
