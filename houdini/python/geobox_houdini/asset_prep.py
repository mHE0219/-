"""Bring assets to the library standard *inside Houdini* (統一比例尺):
metres, +Y up, pivot at base centre, buildings' long side on X / front on +Z.

    import geobox_houdini.asset_prep as ap
    # 1) register a model you have in a SOP network:
    ap.register_sop(hou.node("/obj/my_tree/OUT"), "tree_camphor_a", "tree",
                    source_units="cm", source_up="+Y", tags=["broadleaf", "street"])
    # 2) measure/normalise every library entry Houdini can read (fbx, bgeo, abc, usd...):
    ap.prep_library()

Both write `assets/houdini_cache/<id>.bgeo.sc` (normalised, used by the SOP
network and the packed-disk path) and update assets/library.json (bounds,
files). register_sop also writes `<id>.obj` so the web viewer can show it.
"""
from __future__ import annotations

import os
import sys

from . import core


def _rel(path, start):
    """Forward-slash relative path; absolute if on another Windows drive."""
    try:
        return os.path.relpath(path, start).replace(os.sep, "/")
    except ValueError:
        return os.path.abspath(path).replace(os.sep, "/")

UNIT_SCALE = {"m": 1.0, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048}


def _hou():
    import hou
    return hou


def _geobox_library():
    root = core.repo_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    from geobox import library as libmod  # noqa: WPS433
    return libmod


def _std_matrix(units, up):
    hou = _hou()
    a = {"normalize": {"scale": UNIT_SCALE.get(units, 1.0), "up": up, "offset": [0, 0, 0]}}
    return hou.Matrix4(core.normalize_matrix_rows(a))


def normalise_geometry(geo, units="m", up="+Y", rotate_long_side_to_x=False):
    """Return (frozen normalised geometry, bounds dict) — pivot at base centre."""
    hou = _hou()
    g = geo.freeze()
    g.transform(_std_matrix(units, up))
    bb = g.boundingBox()
    if rotate_long_side_to_x and bb.sizevec()[2] > bb.sizevec()[0] * 1.05:
        g.transform(hou.hmath.buildRotate(0, 90, 0))
        bb = g.boundingBox()
    mn, mx = bb.minvec(), bb.maxvec()
    g.transform(hou.hmath.buildTranslate(-(mn[0] + mx[0]) / 2.0, -mn[1], -(mn[2] + mx[2]) / 2.0))
    bb = g.boundingBox()
    mn, mx = bb.minvec(), bb.maxvec()
    return g, {"min": [round(v, 4) for v in mn], "max": [round(v, 4) for v in mx]}


def _cache_dir(lib):
    d = os.path.join(lib["_root"], "houdini_cache")
    os.makedirs(d, exist_ok=True)
    return d


def register_sop(sop_node, asset_id, category, source_units="m", source_up="+Y", tags=None,
                 label=None, write_obj=True, library_path=None):
    """Normalise the geometry of `sop_node` and add/replace it in the library."""
    libmod = _geobox_library()
    lib = libmod.load(library_path or libmod.DEFAULT_LIBRARY)
    g, bounds = normalise_geometry(sop_node.geometry(), source_units, source_up,
                                   rotate_long_side_to_x=(category == "building"))
    sub = os.path.join(lib["_root"], "library", category)
    os.makedirs(sub, exist_ok=True)
    bgeo = os.path.join(_cache_dir(lib), asset_id + ".bgeo.sc")
    g.saveToFile(bgeo)
    files = {"houdini": _rel(bgeo, lib["_root"])}
    files["houdini_normalized"] = files["houdini"]
    if write_obj:
        obj = os.path.join(sub, asset_id + ".obj")
        g.saveToFile(obj)
        files["web"] = _rel(obj, lib["_root"])
    entry = {"id": asset_id, "category": category, "tags": list(tags or [category]), "label": label or asset_id,
             "files": files, "source_units": "m", "source_up": "+Y", "units_confirmed": True,
             "bounds": bounds, "normalize": {"scale": 1.0, "up": "+Y", "offset": [0.0, 0.0, 0.0]}}
    lib["assets"] = [a for a in lib["assets"] if a["id"] != asset_id] + [entry]
    for a in lib["assets"]:
        libmod._fill_defaults(a)  # noqa: SLF001
    notes = libmod.check({**lib, "assets": [entry]})
    libmod.save(lib)
    size = [bounds["max"][i] - bounds["min"][i] for i in range(3)]
    print("GeoBox: registered %s (%s) %.2f x %.2f x %.2f m" % (asset_id, category, *size))
    for n in notes:
        print("  ! " + n)
    return entry


def prep_library(library_path=None, force=False):
    """For each asset with a Houdini-readable file: write a normalised cache and
    update bounds (handles fbx/abc/bgeo/usd that the pure-Python tool can't measure)."""
    hou = _hou()
    libmod = _geobox_library()
    lib = libmod.load(library_path or libmod.DEFAULT_LIBRARY)
    cache = _cache_dir(lib)
    report = []
    for a in lib["assets"]:
        f = a.get("files") or {}
        if f.get("houdini_normalized") and not force:
            continue
        src = libmod.resolve(lib, f.get("houdini") or f.get("usd") or f.get("web"))
        if not src or not os.path.exists(src):
            report.append("%s: missing source file" % a["id"])
            continue
        geo = hou.Geometry()
        try:
            geo.loadFromFile(src)
        except hou.OperationFailed as exc:
            report.append("%s: cannot load (%s)" % (a["id"], exc))
            continue
        g, bounds = normalise_geometry(geo, a.get("source_units", "m"), a.get("source_up", "+Y"))
        out = os.path.join(cache, a["id"] + ".bgeo.sc")
        g.saveToFile(out)
        f["houdini_normalized"] = _rel(out, lib["_root"])
        a["files"] = f
        a["bounds"] = bounds
        size = [bounds["max"][i] - bounds["min"][i] for i in range(3)]
        report.append("%s: %.2f x %.2f x %.2f m" % (a["id"], *size))
    report += libmod.check(lib)
    libmod.save(lib)
    print("GeoBox asset prep:\n  " + "\n  ".join(report))
    return report
