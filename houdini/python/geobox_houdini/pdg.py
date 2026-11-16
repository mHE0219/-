"""GeoBox on Houdini PDG / TOPs — the same farm work as geobox.farm, but scheduled by
TOPs, so it runs on whatever scheduler your studio uses (Local, HQueue, Deadline,
Tractor...: swap the scheduler node, the network stays the same).

    import geobox_houdini.pdg as gp
    gp.build_ingest_network("//nas/assets/incoming", category="tree")      # /obj/geobox_farm
    gp.build_tiles_network(at="25.0339, 121.5645", size=(2000, 1500), tile=500)
    gp.build_ingest_network(..., scheduler="deadline")                   # or "hqueue" / "tractor"

Ingest network:   filepattern ─► pythonscript (per file: ingest + Houdini normalise + thumbnail)
                  ─► waitforall ─► pythonscript (reindex retrieval)
Tiles network:    genericgenerator (N tiles) ─► pythonscript (build_box per tile) ─► waitforall
                  ─► pythonscript (report)

Work items only call the functions at the bottom of this module (pdg_ingest_item,
pdg_tile_item, pdg_reindex), so the logic is shared with the standalone queue
(geobox.farm.jobs) and is testable without Houdini. TOP parameter names are set
defensively (`_set`), like build.py: a missing parm prints a warning.
NOTE: written without access to Houdini — verify on your Houdini version.
"""
from __future__ import annotations

import json
import os
import sys

from . import core

SCHEDULERS = {"local": "localscheduler", "hqueue": "hqueuescheduler", "deadline": "deadlinescheduler",
              "tractor": "tractorscheduler"}
DEFAULT_PATTERN = "*.{fbx,obj,glb,gltf,abc,usd,usda,usdc,bgeo.sc}"


def _hou():
    import hou
    return hou


def _geobox():
    root = core.repo_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    import geobox  # noqa: F401
    return root


def _set(node, name, value):
    p = node.parm(name)
    if p is None:
        print("GeoBox PDG: %s has no parm '%s' - set it by hand" % (node.path(), name))
        return False
    try:
        p.set(value)
        return True
    except Exception as exc:  # noqa: BLE001
        print("GeoBox PDG: could not set %s/%s = %r (%s)" % (node.path(), name, value, exc))
        return False


def _topnet(parent: str, name: str, scheduler: str):
    hou = _hou()
    par = hou.node(parent)
    net = par.node(name)
    if net is None:
        net = par.createNode("topnet", name)
    for c in net.children():
        c.destroy()
    stype = SCHEDULERS.get(scheduler, scheduler)
    sched = net.createNode(stype, scheduler + "_scheduler")
    _set(net, "topscheduler", sched.path())
    return net, sched


def _python_script(net, name, code):
    n = net.createNode("pythonscript", name)
    _set(n, "script", code)
    # run in hython so work items can use hou (menu token differs between versions; defensive)
    _set(n, "pythonbin", "hython")
    return n


# --- network builders --------------------------------------------------------------------
def build_ingest_network(folder, pattern=DEFAULT_PATTERN, category="", library=None, units="", up="",
                         thumbnails=True, parent="/obj", name="geobox_farm", scheduler="local"):
    """filepattern over `folder` -> per-file ingest (+ Houdini normalise/thumbnail) -> reindex."""
    library = library or os.path.join(core.repo_root() or "", "assets", "library.json")
    net, sched = _topnet(parent, name, scheduler)
    files = net.createNode("filepattern", "ASSET_FILES")
    _set(files, "pattern", os.path.join(folder, pattern).replace("\\", "/"))
    ingest = _python_script(net, "INGEST_ASSET", (
        "# GeoBox: one model file -> library + retrieval index (+ normalised bgeo + thumbnail)\n"
        "import geobox_houdini.pdg as gp\n"
        "gp.pdg_ingest_item(gp.item_input_path(work_item), category=%r, library=%r, units=%r, up=%r, thumbnail=%r)\n"
        % (category, library, units, up, bool(thumbnails))))
    ingest.setInput(0, files)
    wait = net.createNode("waitforall", "ALL_INGESTED")
    wait.setInput(0, ingest)
    reindex = _python_script(net, "REINDEX", "import geobox_houdini.pdg as gp\ngp.pdg_reindex(%r)\n" % library)
    reindex.setInput(0, wait)
    reindex.setDisplayFlag(True)
    net.layoutChildren()
    net.setComment("GeoBox ingest: %s (%s scheduler)" % (folder, scheduler))
    print("GeoBox PDG: %s ready - cook REINDEX (Shift+V) to ingest %s" % (net.path(), folder))
    return net


def build_tiles_network(at=None, center=None, size=(1000, 1000), tile=500.0, name="district",
                        projects_root=None, osm_file=None, terrain=False, seed=None, parent="/obj",
                        node_name="geobox_tiles", scheduler="local"):
    """Plan the tiles now (written to <projects>/_district/<name>/tiles.json), one work item per tile."""
    _geobox()
    from geobox import project
    from geobox.farm.jobs import _center, tile_plan

    lat, lon = _center({"at": at, "center": center})
    plan = tile_plan((lat, lon), size, float(tile))
    proot = projects_root or project.PROJECTS_DIR
    safe = project.safe_name(name)
    ddir = os.path.join(proot, "_district", safe)
    os.makedirs(ddir, exist_ok=True)
    for t in plan["tiles"]:
        t["name"] = "%s_r%02dc%02d" % (safe, t["row"], t["col"])
    plan.update(name=safe, osm_file=osm_file, terrain=bool(terrain), seed=seed, projects_root=proot,
                version=1, type="geobox.district")
    manifest = os.path.join(ddir, "tiles.json").replace("\\", "/")
    project.write_json(manifest, plan, indent=1)

    net, sched = _topnet(parent, node_name, scheduler)
    gen = net.createNode("genericgenerator", "TILES")
    _set(gen, "itemcount", len(plan["tiles"]))
    build = _python_script(net, "BUILD_TILE",
                           "import geobox_houdini.pdg as gp\ngp.pdg_tile_item(%r, work_item.index)\n" % manifest)
    build.setInput(0, gen)
    wait = net.createNode("waitforall", "ALL_TILES")
    wait.setInput(0, build)
    report = _python_script(net, "REPORT", "import geobox_houdini.pdg as gp\ngp.pdg_tiles_report(%r)\n" % manifest)
    report.setInput(0, wait)
    report.setDisplayFlag(True)
    net.layoutChildren()
    print("GeoBox PDG: %s - %d x %d tiles, manifest %s" % (net.path(), plan["grid"][0], plan["grid"][1], manifest))
    if not osm_file:
        print("  tip: pass osm_file= (one Overpass download for the district) to avoid one query per tile")
    return net


# --- work item functions (also callable from plain Python) ----------------------------------
def item_input_path(work_item) -> str:
    """Input file of a work item across PDG API versions."""
    for getter in (lambda w: w.inputFiles[0].path,
                   lambda w: w.inputResultData[0][0],
                   lambda w: w.stringAttribValue("filename"),
                   lambda w: w.data.stringData("filename", 0)):
        try:
            v = getter(work_item)
            if v:
                return v
        except Exception:  # noqa: BLE001 - try the next API flavour
            continue
    raise RuntimeError("GeoBox PDG: could not read the input file of work item %s" % getattr(work_item, "name", "?"))


def pdg_ingest_item(path, category="", library=None, units="", up="", thumbnail=True, log=print):
    """Pure-Python ingest (library entry + index) and, when `hou` is available, the
    Houdini-side normalise (any format Houdini reads) + an OpenGL thumbnail."""
    _geobox()
    from geobox import library as libmod
    from geobox.farm.jobs import _lib_lock, ingest_file
    from geobox.farm.queue import file_lock

    lp = library or libmod.DEFAULT_LIBRARY
    res = ingest_file(path, category=category or None, lib_path=lp, units=units or None, up=up or None, log=log)
    try:
        hou = _hou()
    except ImportError:
        return res
    from . import asset_prep

    lib = libmod.load(lp)
    a = libmod.by_id(lib)[res["id"]]
    geo = hou.Geometry()
    try:
        geo.loadFromFile(res["path"])
    except Exception as exc:  # noqa: BLE001
        log("  houdini could not load %s: %s" % (res["path"], exc))
        return res
    g, bounds = asset_prep.normalise_geometry(geo, a.get("source_units", "m"), a.get("source_up", "+Y"),
                                             rotate_long_side_to_x=(a.get("category") == "building"))
    cache = os.path.join(lib["_root"], "houdini_cache")
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, res["id"] + ".bgeo.sc")
    g.saveToFile(out)
    with file_lock(_lib_lock(lp)):
        lib = libmod.load(lp)
        a = libmod.by_id(lib)[res["id"]]
        a.setdefault("files", {})["houdini_normalized"] = os.path.relpath(out, lib["_root"]).replace(os.sep, "/")
        if not a.get("bounds"):
            a["bounds"] = bounds
        libmod.save(lib)
    res["houdini_normalized"] = out
    if thumbnail:
        try:
            from geobox import retrieval
            png = os.path.join(retrieval.thumbs_dir(lib), res["id"] + ".png")
            if render_thumbnail(out, png, bounds):
                retrieval.rebuild_index(libmod.load(lp), [res["id"]], force=True)
                res["thumbnail"] = png
        except Exception as exc:  # noqa: BLE001 - thumbnails are optional
            log("  thumbnail skipped: %s" % exc)
    return res


def render_thumbnail(geo_file, png, bounds, res=256):
    """Best effort OpenGL ROP render of a normalised asset (needs a GPU-capable hython)."""
    hou = _hou()
    import math
    obj = hou.node("/obj")
    tmp = obj.createNode("geo", "__geobox_thumb")
    cam = obj.createNode("cam", "__geobox_thumb_cam")
    rop = hou.node("/out").createNode("opengl", "__geobox_thumb_rop")
    try:
        f = tmp.createNode("file")
        _set(f, "file", geo_file)
        mn, mx = bounds["min"], bounds["max"]
        c = [(mn[i] + mx[i]) / 2 for i in range(3)]
        r = max(math.dist(mn, mx) / 2, 0.1)
        dist = r / math.tan(math.radians(20)) * 1.15
        az, el = math.radians(35), math.radians(25)
        pos = (c[0] + dist * math.sin(az) * math.cos(el), c[1] + dist * math.sin(el), c[2] + dist * math.cos(az) * math.cos(el))
        cam.parmTuple("t").set(pos)
        cam.parmTuple("r").set((-math.degrees(el), math.degrees(az), 0))
        _set(cam, "resx", res)
        _set(cam, "resy", res)
        _set(rop, "camera", cam.path())
        _set(rop, "picture", png.replace("\\", "/"))
        _set(rop, "tres", 1)
        if rop.parmTuple("res") is not None:
            rop.parmTuple("res").set((res, res))
        os.makedirs(os.path.dirname(png), exist_ok=True)
        rop.render()
        return os.path.exists(png)
    finally:
        for n in (rop, cam, tmp):
            try:
                n.destroy()
            except Exception:  # noqa: BLE001
                pass


def pdg_reindex(library=None, log=print):
    _geobox()
    from geobox import library as libmod
    from geobox import retrieval
    lib = libmod.load(library or libmod.DEFAULT_LIBRARY)
    meta = retrieval.rebuild_index(lib)
    log("GeoBox: retrieval index has %d assets" % len(meta))
    return len(meta)


def pdg_tile_item(manifest, index, log=print):
    _geobox()
    from geobox import project
    with open(manifest, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    t = plan["tiles"][int(index)]
    scene = project.build(t["name"], t["center"], t["size"], terrain=bool(plan.get("terrain")),
                          osm_file=plan.get("osm_file"),
                          rules_overrides={"seed": int(plan["seed"])} if plan.get("seed") is not None else None,
                          root=plan.get("projects_root"), log=log)
    log("GeoBox: tile %s -> %d instances" % (t["name"], len(scene["instances"])))
    return {"name": t["name"], "instances": len(scene["instances"])}


def pdg_tiles_report(manifest, log=print):
    with open(manifest, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    root = plan.get("projects_root") or ""
    ok = [t["name"] for t in plan["tiles"] if os.path.exists(os.path.join(root, t["name"], "scene.json"))]
    log("GeoBox: %d / %d tiles built (%s)" % (len(ok), len(plan["tiles"]), manifest))
    return ok
