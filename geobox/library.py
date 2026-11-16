"""Asset library: manifest loading, scale unification and validation.

THE STANDARD (統一比例尺) every asset is brought to:
  * 1 unit = 1 metre
  * +Y up
  * pivot = centre of the bounding box footprint, at the lowest point (base centre)
  * buildings: long side along X, main facade facing +Z
  * trees/props: trunk/pole at the origin

Source files don't have to follow it: each entry declares `source_units`
(m/cm/mm/in/ft) and `source_up` (+Y/+Z). `normalize_entry` measures the file
and stores `bounds` (already in standard space) plus a `normalize` transform
(scale, up-axis rotation, offset) that the web viewer and Houdini apply at
load time, so the original files stay untouched.
"""
from __future__ import annotations

import json
import math
import os
import struct
from typing import Dict, List, Optional, Tuple

from . import proxies
from .fsutil import relpath

UNIT_SCALE = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048, "km": 1000.0}

# plausible real-world heights (metres) per category, used for unit guessing / checks
PLAUSIBLE_HEIGHT = {"building": (2.0, 700.0), "tree": (0.3, 90.0), "prop": (0.05, 30.0), "kit": (0.05, 30.0)}
CATEGORIES = ("building", "tree", "prop", "kit")

LIBRARY_VERSION = 1
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LIBRARY = os.path.join(REPO_ROOT, "assets", "library.json")


# --- manifest ----------------------------------------------------------------
def load(path: str = DEFAULT_LIBRARY) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        lib = json.load(fh)
    lib["_path"] = os.path.abspath(path)
    lib["_root"] = os.path.dirname(os.path.abspath(path))
    for a in lib.get("assets", []):
        _fill_defaults(a)
    return lib


def save(lib: dict, path: Optional[str] = None) -> str:
    path = path or lib.get("_path") or DEFAULT_LIBRARY
    clean = {k: v for k, v in lib.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def _fill_defaults(a: dict) -> None:
    a.setdefault("tags", [])
    a.setdefault("files", {})
    a.setdefault("source_units", "m")
    a.setdefault("source_up", "+Y")
    a.setdefault("weight", 1.0)
    a.setdefault("fit", "stretch" if a.get("category") == "building" else "uniform")
    if a.get("category") == "building":
        a.setdefault("scale_range", [0.25, 4.0])
    else:
        a.setdefault("scale_range", [0.6, 1.6])
    a.setdefault("floor_height", 3.2)


def asset_size(a: dict) -> Tuple[float, float, float]:
    """(width X, height Y, depth Z) in metres from bounds."""
    b = a.get("bounds")
    if not b:
        return (10.0, 10.0, 10.0)
    mn, mx = b["min"], b["max"]
    return (max(mx[0] - mn[0], 1e-3), max(mx[1] - mn[1], 1e-3), max(mx[2] - mn[2], 1e-3))


def resolve(lib: dict, rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(lib["_root"], rel))


def by_id(lib: dict) -> Dict[str, dict]:
    return {a["id"]: a for a in lib.get("assets", [])}


# --- bounds measurement ------------------------------------------------------
def _obj_points(path: str):
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("v "):
                parts = line.split()
                yield float(parts[1]), float(parts[2]), float(parts[3])


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _node_matrix(node) -> List[List[float]]:
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    t = node.get("translation", [0, 0, 0])
    q = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    x, y, z, w = q
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[0][0] * s[0], r[0][1] * s[1], r[0][2] * s[2], t[0]],
            [r[1][0] * s[0], r[1][1] * s[1], r[1][2] * s[2], t[1]],
            [r[2][0] * s[0], r[2][1] * s[1], r[2][2] * s[2], t[2]],
            [0, 0, 0, 1]]


def _gltf_json(path: str) -> dict:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] == b"glTF":
        ln = struct.unpack("<I", data[12:16])[0]
        return json.loads(data[20:20 + ln].decode("utf-8"))
    return json.loads(data.decode("utf-8"))


def _gltf_points(path: str):
    """Transformed accessor min/max corners of every mesh primitive (glTF is +Y up, metres)."""
    g = _gltf_json(path)
    nodes, meshes, accs = g.get("nodes", []), g.get("meshes", []), g.get("accessors", [])
    scene = g.get("scenes", [{}])[g.get("scene", 0)] if g.get("scenes") else {"nodes": list(range(len(nodes)))}
    ident = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    stack = [(n, ident) for n in scene.get("nodes", [])]
    while stack:
        ni, parent = stack.pop()
        node = nodes[ni]
        m = _mat_mul(parent, _node_matrix(node))
        if "mesh" in node:
            for prim in meshes[node["mesh"]].get("primitives", []):
                acc = accs[prim["attributes"]["POSITION"]]
                mn, mx = acc.get("min"), acc.get("max")
                if not mn or not mx:
                    continue
                for cx in (mn[0], mx[0]):
                    for cy in (mn[1], mx[1]):
                        for cz in (mn[2], mx[2]):
                            yield tuple(m[r][0] * cx + m[r][1] * cy + m[r][2] * cz + m[r][3] for r in range(3))
        for c in node.get("children", []):
            stack.append((c, m))


def _usda_points(path: str):
    """Very small USDA reader: first extentsHint / extent / points arrays."""
    import re
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        txt = fh.read()
    for m in re.finditer(r"point3f\[\] points = \[(.*?)\]", txt, re.S):
        for t in re.finditer(r"\(([-\d.eE+]+),\s*([-\d.eE+]+),\s*([-\d.eE+]+)\)", m.group(1)):
            yield float(t.group(1)), float(t.group(2)), float(t.group(3))


def measure_raw_bounds(path: str) -> Optional[Tuple[List[float], List[float]]]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".obj":
        pts = _obj_points(path)
    elif ext in (".glb", ".gltf"):
        pts = _gltf_points(path)
    elif ext == ".usda":
        pts = _usda_points(path)
    else:
        return None  # fbx / bgeo / usd(c): measure in Houdini (geobox_houdini.asset_prep)
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    n = 0
    for p in pts:
        n += 1
        for i in range(3):
            mn[i] = min(mn[i], p[i])
            mx[i] = max(mx[i], p[i])
    return (mn, mx) if n else None


def up_rotation(source_up: str) -> List[List[float]]:
    """3x3 rotation taking the source up axis to +Y."""
    if source_up in ("+Z", "Z", "z"):
        return [[1, 0, 0], [0, 0, 1], [0, -1, 0]]  # rotate -90 deg about X: (x,y,z)->(x,z,-y)
    if source_up in ("-Z",):
        return [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    return [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def standardise_bounds(raw_min, raw_max, units: str, up: str):
    s = UNIT_SCALE.get(units, 1.0)
    r = up_rotation(up)
    corners = []
    for x in (raw_min[0], raw_max[0]):
        for y in (raw_min[1], raw_max[1]):
            for z in (raw_min[2], raw_max[2]):
                corners.append([s * sum(r[i][k] * v for k, v in enumerate((x, y, z))) for i in range(3)])
    mn = [min(c[i] for c in corners) for i in range(3)]
    mx = [max(c[i] for c in corners) for i in range(3)]
    return mn, mx


def guess_units(category: str, raw_min, raw_max) -> Tuple[str, str, List[str]]:
    """Heuristic (units, up, notes) from raw extents and the category."""
    notes = []
    lo, hi = PLAUSIBLE_HEIGHT.get(category, (0.05, 1000.0))
    ext = [raw_max[i] - raw_min[i] for i in range(3)]
    up = "+Y"
    # trees/props are taller than wide: if Z is the tall axis and Y isn't, assume Z-up
    if category in ("tree", "prop") and ext[2] > 1.5 * ext[1]:
        up = "+Z"
        notes.append("Z is the tallest axis -> assuming Z-up source")
    h = ext[2] if up == "+Z" else ext[1]
    for units in ("m", "cm", "mm", "in", "ft"):
        if lo <= h * UNIT_SCALE[units] <= hi:
            if units != "m":
                notes.append(f"height {h:.3g} raw units implausible in metres -> guessing '{units}'")
            return units, up, notes
    notes.append(f"height {h:.3g} out of plausible range for {category}; left as metres")
    return "m", up, notes


def normalize_entry(lib: dict, a: dict, guess: bool = False) -> List[str]:
    """Measure the asset file and store standardised bounds + normalize transform."""
    notes: List[str] = []
    src = resolve(lib, a["files"].get("web") or a["files"].get("houdini") or a["files"].get("usd"))
    if not src or not os.path.exists(src):
        return [f"{a['id']}: file not found ({src})"]
    raw = measure_raw_bounds(src)
    if raw is None:
        return [f"{a['id']}: cannot measure {os.path.basename(src)} here; run geobox_houdini.asset_prep in Houdini"]
    if guess and not a.get("units_confirmed"):
        units, up, n = guess_units(a.get("category", "prop"), *raw)
        a["source_units"], a["source_up"] = units, up
        notes += [f"{a['id']}: {x}" for x in n]
    mn, mx = standardise_bounds(raw[0], raw[1], a["source_units"], a["source_up"])
    # pivot correction: base centre -> origin. Kit facade/corner pieces keep their
    # back face on z=0 (docs/pipeline_v2.md); kit roofs are centred like everything else.
    role = (a.get("kit") or {}).get("role")
    if a.get("category") == "kit" and role not in ("roof", "roof_prop"):
        off = [-(mn[0] + mx[0]) / 2 if role != "corner" else -mn[0], -mn[1], -mn[2]]
    else:
        off = [-(mn[0] + mx[0]) / 2, -mn[1], -(mn[2] + mx[2]) / 2]
    a["normalize"] = {"scale": UNIT_SCALE.get(a["source_units"], 1.0), "up": a["source_up"],
                      "offset": [round(v, 5) for v in off]}
    a["bounds"] = {"min": [round(mn[0] + off[0], 4), 0.0, round(mn[2] + off[2], 4)],
                   "max": [round(mx[0] + off[0], 4), round(mx[1] - mn[1], 4), round(mx[2] + off[2], 4)]}
    w, h, d = asset_size(a)
    lo, hi = PLAUSIBLE_HEIGHT.get(a.get("category"), (0, 1e9))
    if not lo <= h <= hi:
        notes.append(f"{a['id']}: WARNING height {h:.2f} m is implausible for a {a.get('category')} "
                     f"(check source_units={a['source_units']}, source_up={a['source_up']})")
    if a.get("category") == "building" and d > w * 1.05:
        notes.append(f"{a['id']}: note: depth ({d:.1f}) > width ({w:.1f}); standard is long side along X")
    return notes


def check(lib: dict) -> List[str]:
    """Validation report for the whole library."""
    notes = []
    ids = set()
    for a in lib.get("assets", []):
        if a["id"] in ids:
            notes.append(f"duplicate id {a['id']}")
        ids.add(a["id"])
        if a.get("category") not in CATEGORIES:
            notes.append(f"{a['id']}: unknown category {a.get('category')!r}")
        if not a.get("bounds"):
            notes.append(f"{a['id']}: no bounds - run `python -m geobox library normalize`")
        for k in ("web", "houdini"):
            p = resolve(lib, a["files"].get(k))
            if p and not os.path.exists(p):
                notes.append(f"{a['id']}: files.{k} missing: {p}")
        w, h, d = asset_size(a)
        lo, hi = PLAUSIBLE_HEIGHT.get(a.get("category"), (0, 1e9))
        if a.get("bounds") and not lo <= h <= hi:
            notes.append(f"{a['id']}: implausible height {h:.2f} m")
    return notes


def scan(lib: dict, folder: str, category: Optional[str] = None) -> List[str]:
    """Add every .glb/.gltf/.obj/.fbx/.usd* under `folder` that isn't in the
    manifest yet. Category from the folder name (building(s)/tree(s)/prop(s))."""
    notes = []
    known = {os.path.normpath(resolve(lib, f)) for a in lib["assets"] for f in a["files"].values() if f}
    ids = {a["id"] for a in lib["assets"]}
    for dirpath, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".glb", ".gltf", ".obj", ".fbx", ".usd", ".usda", ".usdc", ".bgeo", ".sc"):
                continue
            full = os.path.normpath(os.path.join(dirpath, fn))
            if full in known:
                continue
            low = dirpath.lower()
            cat = category or ("building" if "build" in low else "tree" if ("tree" in low or "veg" in low or "plant" in low) else "prop")
            base = os.path.splitext(fn)[0].replace(".bgeo", "")
            aid = base
            i = 2
            while aid in ids:
                aid = f"{base}_{i}"
                i += 1
            rel = relpath(full, lib["_root"])
            files = {"web": rel} if ext in (".glb", ".gltf", ".obj", ".fbx") else {}
            files["houdini"] = rel
            if ext.startswith(".usd"):
                files["usd"] = rel
            a = {"id": aid, "category": cat, "tags": [cat] + ([base.split("_")[0]] if "_" in base else []),
                 "files": files, "label": base}
            _fill_defaults(a)
            notes += normalize_entry(lib, a, guess=True)
            lib["assets"].append(a)
            ids.add(aid)
            notes.append(f"added {aid} ({cat}) from {rel}")
    return notes


def init_default(path: str = DEFAULT_LIBRARY) -> dict:
    """(Re)generate proxy assets and a manifest containing them; keeps
    user-added (non-proxy) entries of an existing manifest."""
    root = os.path.dirname(os.path.abspath(path))
    entries = proxies.generate(os.path.join(root, "proxies"))
    user = []
    if os.path.exists(path):
        try:
            old = load(path)
            user = [a for a in old.get("assets", []) if not a.get("proxy")]
        except Exception:  # noqa: BLE001
            user = []
    lib = {
        "version": LIBRARY_VERSION,
        "type": "geobox.library",
        "standard": {"units": "m", "up": "+Y", "pivot": "base_center",
                     "building_front": "+Z", "building_long_axis": "X"},
        "assets": entries + user,
    }
    for a in lib["assets"]:
        _fill_defaults(a)
    lib["_path"], lib["_root"] = os.path.abspath(path), root
    save(lib, path)
    return lib
