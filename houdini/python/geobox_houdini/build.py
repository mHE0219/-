"""Create / drive the GeoBox network in Houdini (pipeline v2, docs/pipeline_v2.md).

    import geobox_houdini.build as gb
    geo = gb.build_city("C:/geobox/projects/my_box/scene.json")   # -> /obj/geobox
    gb.save(geo)            # greybox + (edited) instances -> scene.json + scene.usda
    gb.build_stage(...)     # Solaris: sublayer scene.usda

/obj/geobox (v2 — Houdini is the main tool, the web viewer is a preview):

    REFERENCE   GROUND, AREAS, ROADS, MASSING ─► REFERENCE (merge) ─► SHOW_REFERENCE
    GREYBOX     GREYBOX_SEED (saved blocks) ─► GREYBOX_DRAW (merge: wire YOUR boxes into inputs 2+)
                ─► GREYBOX_EDIT (null: insert Edit/Transform SOPs) ─► OUT_GREYBOX
    DRESS       DRESS (Python: OUT_GREYBOX -> geobox.scatter.scatter in-process: kit / whole / veg / props)
                ─► EDIT_INSTANCES (null: tweak/blast instances) ─► OUT_INSTANCES
    ART         LIBRARY (assets used by OUT_INSTANCES) ─► PACK_LIBRARY ─► PIECE_ATTRIB ─┐
                OUT_INSTANCES ─────────────────────────────────────────────────────────┴► COPY_TO_POINTS
    VIEW        VIEW_MODE (greybox / art / both) + SHOW_REFERENCE ─► OUT (display/render)

Buttons on the geo node (GeoBox tab): Seed Greybox from OSM, Clear Greybox, Reload,
Save scene.json, Export USD, Open Web Preview, Run Self-test.

Every parm is set defensively (`_set`): a parm missing in your Houdini version prints a
warning instead of failing; the self-test (houdini/scripts/geobox_selftest.py) reports
the exact parm names it found so they can be fixed.
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import core

PY_CODE = "import geobox_houdini.sop as g\ng.{fn}()\n"
DEFAULT_NAME = "geobox"
WEB_PORT = 8765

NO_BUILDINGS_VEX = r"""// GeoBox greybox view: keep trees/props, hide building art (the greybox stands in for it)
int pt = primpoint(0, @primnum, 0);
string cat = point(0, "category", pt);
if (cat == "building" || cat == "kit") removeprim(0, @primnum, 1);
"""

PIECE_VEX = r"""// GeoBox: Copy to Points matches source pieces to points by this attribute
// (both 'name' and the Houdini default 'variant' are provided)
s@variant = s@name;
"""

DISK_VEX = r"""// GeoBox: one packed-disk primitive per point, loading s@instancefile lazily.
// Requires instancefile to be already normalised (see geobox_houdini.asset_prep).
int prim = addprim(0, "packeddisk", @ptnum);
setprimintrinsic(0, "unexpandedfilename", prim, s@instancefile);
matrix3 m = ident();
scale(m, v@scale);
m *= qconvert(p@orient);
setprimintrinsic(0, "transform", prim, m);
"""


def _hou():
    import hou
    return hou


def _log(msg):
    print("GeoBox: " + msg)


# --- defensive helpers ---------------------------------------------------------------
def _set(node, name, value):
    p = node.parm(name)
    if p is None:
        pt = node.parmTuple(name)
        if pt is None:
            _log("%s has no parm '%s' (Houdini version difference) - set it by hand" % (node.path(), name))
            return False
        try:
            pt.set(value)
            return True
        except Exception as exc:  # noqa: BLE001
            _log("could not set %s/%s = %r (%s)" % (node.path(), name, value, exc))
            return False
    try:
        p.set(value)
    except Exception:  # noqa: BLE001 - menu parms may want a token / index
        try:
            p.set(str(value))
        except Exception as exc:  # noqa: BLE001
            _log("could not set %s/%s = %r (%s)" % (node.path(), name, value, exc))
            return False
    return True


def _set_like(node, needle, toggle_value=None, string_value=None):
    """Fallback when an exact parm name is unknown: set every toggle / string parm whose
    name contains `needle` (e.g. Copy to Points 'piece' attribute parms)."""
    hou = _hou()
    done = []
    try:
        parms = node.parms()
    except Exception:  # noqa: BLE001
        return done
    for p in parms:
        try:
            nm = p.name()
            if needle not in nm.lower():
                continue
            ptype = p.parmTemplate().type()
            if toggle_value is not None and ptype == hou.parmTemplateType.Toggle:
                p.set(toggle_value)
                done.append(nm)
            elif string_value is not None and ptype == hou.parmTemplateType.String:
                p.set(string_value)
                done.append(nm)
        except Exception:  # noqa: BLE001
            continue
    return done


def _create(parent, types, name):
    """Create the first node type that exists (types differ across versions)."""
    last = None
    for t in ([types] if isinstance(types, str) else types):
        try:
            return parent.createNode(t, name)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError("GeoBox: cannot create %s (%s): %s" % (name, types, last))


def _python_sop(parent, name, fn, comment=None):
    n = _create(parent, "python", name)
    n.parm("python").set(PY_CODE.format(fn=fn))
    if comment:
        _comment(n, comment)
    return n


def _comment(node, text):
    try:
        hou = _hou()
        node.setComment(text)
        node.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    except Exception:  # noqa: BLE001
        pass


def _button(hou, name, label, fn):
    return hou.ButtonParmTemplate(name, label,
                                  script_callback="import geobox_houdini.build as b; b.%s(kwargs['node'])" % fn,
                                  script_callback_language=hou.scriptLanguage.Python)


def _add_spare_parms(geo, scene_file, seed=0):
    hou = _hou()
    ptg = geo.parmTemplateGroup()
    if ptg.find("geobox") is not None:
        ptg.remove("geobox")
    f = hou.FolderParmTemplate("geobox", "GeoBox", folder_type=hou.folderType.Tabs)
    f.addParmTemplate(hou.StringParmTemplate(
        "scene_file", "Scene File (scene.json)", 1, default_value=(scene_file,),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.json"}))
    f.addParmTemplate(hou.IntParmTemplate("seed", "Seed (0 = scene)", 1, default_value=(int(seed),), min=0, max=99999))
    f.addParmTemplate(hou.MenuParmTemplate(
        "view_mode", "View", menu_items=("greybox", "art", "both"),
        menu_labels=("Greybox 灰盒", "Art 素材", "Both 疊加"), default_value=1))
    f.addParmTemplate(hou.ToggleParmTemplate("show_reference", "Show Reference (map / roads)", default_value=True))
    f.addParmTemplate(hou.ToggleParmTemplate("show_massing", "Show OSM Massing", default_value=False))
    f.addParmTemplate(hou.ToggleParmTemplate("only_used", "Load Only Used Assets", default_value=True))
    for name, label, fn in (("seed_greybox", "Seed Greybox from OSM", "seed_greybox"),
                            ("clear_greybox", "Clear Greybox (designer start)", "clear_greybox"),
                            ("reload", "Reload From Disk", "reload"),
                            ("save", "Save scene.json", "save"),
                            ("export_usd", "Export USD", "export_usd"),
                            ("web_preview", "Open Web Preview", "web_preview"),
                            ("selftest", "Run Self-test", "run_selftest")):
        f.addParmTemplate(_button(hou, name, label, fn))
    ptg.append(f)
    geo.setParmTemplateGroup(ptg)


# --- the network -----------------------------------------------------------------------
def build_city(scene_file=None, parent="/obj", name=DEFAULT_NAME):
    """Build (or rebuild) the v2 network for projects/<name>/scene.json."""
    hou = _hou()
    if not scene_file:
        scene_file = hou.ui.selectFile(title="GeoBox: choose projects/<name>/scene.json", pattern="*.json",
                                       file_type=hou.fileType.Any)
        if not scene_file:
            return None
    scene_file = hou.expandString(scene_file).replace("\\", "/")
    data = core.SceneData(scene_file)  # fail early with a readable error if the file is wrong
    obj = hou.node(parent)
    geo = obj.node(name)
    if geo is None:
        geo = obj.createNode("geo", name)
    for c in geo.children():
        c.destroy()
    _add_spare_parms(geo, scene_file, seed=int((data.scene.get("rules") or {}).get("seed", 0) or 0))

    # REFERENCE (map is only a reference in v2) ------------------------------------------
    ground = _python_sop(geo, "GROUND", "ground")
    areas = _python_sop(geo, "AREAS", "areas")
    roads = _python_sop(geo, "ROADS", "roads")
    mass = _python_sop(geo, "MASSING", "massing")
    empty = _create(geo, "null", "EMPTY")
    sw_mass = _create(geo, "switch", "SHOW_MASSING")
    sw_mass.setInput(0, empty)
    sw_mass.setInput(1, mass)
    sw_mass.parm("input").setExpression('ch("../show_massing")')
    ref = _create(geo, "merge", "REFERENCE")
    for i, n in enumerate((ground, areas, roads, sw_mass)):
        ref.setInput(i, n)
    sw_ref = _create(geo, "switch", "SHOW_REFERENCE")
    sw_ref.setInput(0, empty)
    sw_ref.setInput(1, ref)
    sw_ref.parm("input").setExpression('ch("../show_reference")')

    # GREYBOX (designer-owned) --------------------------------------------------------------
    seed = _python_sop(geo, "GREYBOX_SEED", "greybox_seed", "saved blocks (Seed from OSM / Clear on the geo node)")
    draw = _create(geo, "merge", "GREYBOX_DRAW")
    draw.setInput(0, seed)
    _comment(draw, "wire your own boxes into inputs 2+ (one closed piece = one block)")
    gedit = _create(geo, "null", "GREYBOX_EDIT")
    gedit.setInput(0, draw)
    _comment(gedit, "insert Edit / Transform SOPs above this node")
    out_grey = _create(geo, "null", "OUT_GREYBOX")
    out_grey.setInput(0, gedit)

    # DRESS (art pass, live) -----------------------------------------------------------------
    dress = _python_sop(geo, "DRESS", "dress", "art pass: kit / whole / vegetation (geobox.scatter)")
    dress.setInput(0, out_grey)
    iedit = _create(geo, "null", "EDIT_INSTANCES")
    iedit.setInput(0, dress)
    _comment(iedit, "insert Edit / Blast / Attribute SOPs above this node")
    out_inst = _create(geo, "null", "OUT_INSTANCES")
    out_inst.setInput(0, iedit)

    # ART ---------------------------------------------------------------------------------------
    lib = _python_sop(geo, "LIBRARY", "library")
    lib.setInput(0, out_inst)
    pack = _create(geo, "pack", "PACK_LIBRARY")
    pack.setInput(0, lib)
    if not (_set(pack, "packbyname", 1) and _set(pack, "nameattribute", "name")):
        _set_like(pack, "byname", toggle_value=1)
    _set(pack, "pivot", "origin")
    piece = _create(geo, "attribwrangle", "PIECE_ATTRIB")
    piece.setInput(0, pack)
    _set(piece, "class", 1)  # run over primitives
    _set(piece, "snippet", PIECE_VEX)
    copy = _create(geo, ["copytopoints::2.0", "copytopoints"], "COPY_TO_POINTS")
    copy.setInput(0, piece)
    copy.setInput(1, out_inst)
    if not (_set(copy, "usepieceattrib", 1) and _set(copy, "pieceattrib", "name")):
        set_ = _set_like(copy, "piece", toggle_value=1, string_value="name")
        _log("COPY_TO_POINTS piece attribute via fuzzy match: %s (s@variant is also set)" % (set_ or "none"))
    _set(copy, "pack", 1)
    _set(copy, "pivot", "origin")
    _set(copy, "transform", 1)

    # VIEW --------------------------------------------------------------------------------------
    no_bld = _create(geo, "attribwrangle", "ART_WITHOUT_BUILDINGS")
    no_bld.setInput(0, copy)
    _set(no_bld, "class", 1)
    _set(no_bld, "snippet", NO_BUILDINGS_VEX)
    grey_view = _create(geo, "merge", "GREYBOX_VIEW")
    grey_view.setInput(0, out_grey)
    grey_view.setInput(1, no_bld)
    both_view = _create(geo, "merge", "BOTH_VIEW")
    both_view.setInput(0, out_grey)
    both_view.setInput(1, copy)
    view = _create(geo, "switch", "VIEW_MODE")
    view.setInput(0, grey_view)
    view.setInput(1, copy)
    view.setInput(2, both_view)
    view.parm("input").setExpression('ch("../view_mode")')
    merge = _create(geo, "merge", "MERGE")
    merge.setInput(0, view)
    merge.setInput(1, sw_ref)
    out = _create(geo, "null", "OUT")
    out.setInput(0, merge)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)

    # optional / starting points (not wired) ------------------------------------------------------
    _python_sop(geo, "SAVED_INSTANCES", "instances", "not wired: instances exactly as saved in scene.json")
    _python_sop(geo, "FOOTPRINTS", "footprints", "not wired: OSM footprints (prim attribs) for procedural HDAs")
    disk = _create(geo, "attribwrangle", "INSTANCE_PACKED_DISK")
    disk.setInput(0, out_inst)
    _set(disk, "snippet", DISK_VEX)
    _comment(disk, "not wired: packed-disk instancing from s@instancefile")
    try:
        geo.layoutChildren()
        geo.setComment("GeoBox v2: %s" % scene_file)
    except Exception:  # noqa: BLE001
        pass
    _log("built %s from %s" % (geo.path(), scene_file))
    return geo


# --- actions (buttons / shelf) -------------------------------------------------------------
def find_geobox(required=True):
    """The GeoBox geo node to act on: a selected node with a scene_file parm (or inside one),
    else /obj/geobox, else the v1 /obj/geobox_city."""
    hou = _hou()
    try:
        sel = list(hou.selectedNodes())
    except Exception:  # noqa: BLE001
        sel = []
    for n in sel:
        while n is not None:
            if n.parm("scene_file") is not None:
                return n
            n = n.parent()
    for path in ("/obj/" + DEFAULT_NAME, "/obj/geobox_city"):
        n = hou.node(path)
        if n is not None and n.parm("scene_file") is not None:
            return n
    if required:
        _message("GeoBox: no GeoBox network yet — use the 'Build GeoBox' shelf tool first.")
    return None

def _scene_path(geo):
    hou = _hou()
    return hou.expandString(geo.parm("scene_file").evalAsString())


def _message(text):
    print(text)
    hou = _hou()
    try:
        if hou.isUIAvailable():
            hou.ui.displayMessage(text, title="GeoBox")
    except Exception:  # noqa: BLE001
        pass


def reload(geo):
    """Re-read scene.json / layout / library (after the web viewer or a farm job saved)."""
    from . import sop
    sop._DRESS_CACHE.clear()  # noqa: SLF001
    for c in geo.children():
        if c.type().name() == "python":
            try:
                c.cook(force=True)
            except Exception as exc:  # noqa: BLE001
                _log("%s failed to cook: %s" % (c.path(), exc))
    _log("reloaded %s" % geo.parm("scene_file").evalAsString())


def seed_greybox(geo):
    data = core.SceneData(_scene_path(geo))
    scene, info = core.seed_greybox(data)
    core.save_scene(data, scene)
    reload(geo)
    _message("GeoBox: greybox seeded from OSM — %(blocks)d blocks (%(kept)d edited/manual kept)" % info)
    return info


def clear_greybox(geo):
    hou = _hou()
    if hou.isUIAvailable():
        if hou.ui.displayMessage("Clear every greybox block? (OSM blocks are hidden, re-seed brings them back)",
                                 buttons=("Clear", "Cancel"), title="GeoBox") != 0:
            return None
    data = core.SceneData(_scene_path(geo))
    scene, info = core.clear_greybox(data)
    core.save_scene(data, scene)
    reload(geo)
    _log("greybox cleared (%(suppressed)d OSM blocks hidden)" % info)
    return info


def _records_from_geo(g):
    names = ("name", "geobox_id", "category", "source", "rule", "block")
    has = {n: g.findPointAttrib(n) is not None for n in names + ("orient", "scale", "locked")}
    recs = []
    for pt in g.points():
        r = {"P": tuple(pt.position())}
        r["orient"] = tuple(pt.attribValue("orient")) if has["orient"] else (0.0, 0.0, 0.0, 1.0)
        r["scale"] = tuple(pt.attribValue("scale")) if has["scale"] else (1.0, 1.0, 1.0)
        r["locked"] = int(pt.attribValue("locked")) if has["locked"] else 0
        for n in names:
            r[n] = pt.attribValue(n) if has[n] else ""
        recs.append(r)
    return recs


def _pieces_from_geo(g):  # kept for older callers / tests
    from . import sop
    return sop.pieces_from_geo(g)


def save(geo, grey_geo=None, inst_geo=None, dress_geo=None):
    """Greybox (OUT_GREYBOX) + instances (OUT_INSTANCES, compared against DRESS) -> scene.json
    + scene.usda. Geometry overrides are for scripts/tests (e.g. frozen, edited copies)."""
    from . import sop
    data = core.SceneData(_scene_path(geo))
    gnode, dnode, onode = geo.node("OUT_GREYBOX"), geo.node("DRESS"), geo.node("OUT_INSTANCES")
    grey_geo = grey_geo if grey_geo is not None else (gnode.geometry() if gnode is not None else None)
    pieces = sop.pieces_from_geo(grey_geo) if grey_geo is not None else None
    if not pieces and data.scene.get("greybox"):
        _log("OUT_GREYBOX is empty - greybox left unchanged (use Clear Greybox to empty it)")
        pieces = None
    if dnode is None:  # v1 network: no live dressing, compare with the saved scene
        return export_edits_v1(geo)
    dress_geo = dress_geo if dress_geo is not None else dnode.geometry()
    inst_geo = inst_geo if inst_geo is not None else onode.geometry()
    rep = core.save_v2(data, pieces, _records_from_geo(dress_geo), _records_from_geo(inst_geo))
    ir = rep["instances"]
    msg = "GeoBox save: instances %(updated)d edited, %(added)d added, %(deleted)d deleted, %(unchanged)d procedural" % ir
    if "greybox" in rep:
        msg += "\ngreybox: %(updated)d edited, %(added)d added, %(deleted)d deleted, %(unchanged)d unchanged" % rep["greybox"]
    _message(msg + "\n\n" + "\n".join(rep["written"]))
    return rep


export_edits = save  # v1 name (shelf tool / button)


def export_edits_v1(geo, source="OUT_INSTANCES", greybox_source="OUT_GREYBOX"):
    """v1 networks (INSTANCES/GREYBOX without DRESS): edits relative to the saved scene."""
    from . import sop
    data = core.SceneData(_scene_path(geo))
    recs = _records_from_geo(geo.node(source).geometry())
    scene, report = core.records_to_scene(recs, data.scene, data.assets)
    gnode = geo.node(greybox_source) if greybox_source else None
    if gnode is not None and "greybox" in scene:
        pieces = sop.pieces_from_geo(gnode.geometry())
        if pieces or not scene["greybox"]:
            scene, report["greybox"] = core.pieces_to_greybox(pieces, scene)
    report["written"] = core.save_scene(data, scene)
    _message("GeoBox export: %(updated)d updated, %(added)d added, %(deleted)d deleted, %(unchanged)d unchanged" % report)
    return report


def export_usd(geo):
    data = core.SceneData(_scene_path(geo))
    path = core.export_usd(data)
    _message("GeoBox: wrote %s (Solaris: City Box (Solaris) shelf tool / Sublayer LOP)" % path)
    return path


# --- web preview ------------------------------------------------------------------------------
def _server_up(port=WEB_PORT):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/projects" % port, timeout=1.0) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def find_python():
    """A plain Python 3 to run the web server (never hython: that would hold a licence).
    Order: $GEOBOX_PYTHON, py/python3/python on PATH, Houdini's bundled python."""
    import glob
    import shutil
    env = os.environ.get("GEOBOX_PYTHON")
    if env and os.path.exists(env):
        return [env]
    if sys.platform.startswith("win") and shutil.which("py"):
        return [shutil.which("py"), "-3"]
    for exe in ("python3", "python"):
        p = shutil.which(exe)
        if p and "WindowsApps" not in p:  # skip the Microsoft Store stub
            return [p]
    hfs = os.environ.get("HFS", "")
    for pat in ("python3*/python.exe", "python*/python.exe", "python/bin/python3", "bin/python3*"):
        hits = sorted(glob.glob(os.path.join(hfs, pat)))
        if hits:
            return [hits[-1]]
    return None


def web_preview(geo=None, port=WEB_PORT):
    """Open the web preview of this project, starting `python -m geobox serve` if needed."""
    import webbrowser
    root = core.repo_root()
    name = os.path.basename(os.path.dirname(_scene_path(geo))) if geo is not None else ""
    if not _server_up(port):
        py = find_python()
        if not py or not root:
            _message("GeoBox: no Python found to start the web server.\nRun start_geobox.bat (or "
                     "`python -m geobox serve`) in %s, or set GEOBOX_PYTHON." % (root or "the repo"))
            return None
        flags = 0x00000010 if sys.platform.startswith("win") else 0  # CREATE_NEW_CONSOLE
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for k in ("PYTHONHOME", "PYTHONPATH"):  # Houdini's python env must not leak into the server
            env.pop(k, None)
        subprocess.Popen(py + ["-m", "geobox", "serve", "--port", str(port)], cwd=root, env=env, creationflags=flags)
        import time
        for _ in range(40):
            if _server_up(port):
                break
            time.sleep(0.25)
    url = "http://127.0.0.1:%d/%s" % (port, ("?project=" + name) if name else "")
    webbrowser.open(url)
    _log("web preview: " + url)
    return url


def run_selftest(geo=None):
    """Run houdini/scripts/geobox_selftest.py inside this Houdini session (non-destructive:
    works on a copy of the project, own node names, does not save your .hip)."""
    import importlib.util
    root = core.repo_root()
    path = os.path.join(root or "", "houdini", "scripts", "geobox_selftest.py")
    spec = importlib.util.spec_from_file_location("geobox_selftest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    project = os.path.dirname(_scene_path(geo)) if geo is not None else None
    results, report_txt = mod.run(project=project, inprocess=True)
    summary = mod.summary_line(results)
    _message("GeoBox self-test: %s\n\nReport: %s" % (summary, report_txt))
    return results


# --- Solaris ----------------------------------------------------------------------------------
def build_stage(scene_file=None, name="geobox_stage"):
    """Solaris: Sublayer LOP loading scene.usda (PointInstancers, ground, guides)."""
    hou = _hou()
    if not scene_file:
        scene_file = hou.ui.selectFile(title="GeoBox: choose scene.json or scene.usda", pattern="*.json *.usda")
        if not scene_file:
            return None
    scene_file = hou.expandString(scene_file)
    usd = scene_file if scene_file.endswith(".usda") else os.path.join(os.path.dirname(scene_file), "scene.usda")
    stage = hou.node("/stage")
    n = stage.node(name) or stage.createNode("sublayer", name)
    _set(n, "filepath1", usd.replace("\\", "/"))
    n.setDisplayFlag(True)
    _log("%s -> %s" % (n.path(), usd))
    return n
