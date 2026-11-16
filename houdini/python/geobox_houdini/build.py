"""Create / refresh the GeoBox networks in Houdini.

    import geobox_houdini.build as gb
    gb.build_city("/path/projects/my_box/scene.json")     # SOP network in /obj
    gb.build_stage("/path/projects/my_box/scene.json")    # Solaris: sublayer scene.usda
    gb.export_edits(hou.node("/obj/geobox_city"))         # Houdini edits -> scene.json (+usda)

SOP network (/obj/geobox_city):

    INSTANCES (Python) ─► EDIT_INSTANCES (null: insert Edit/Transform/Attribute SOPs here) ─► OUT_INSTANCES
    LIBRARY (Python) ─► PACK_LIBRARY (pack by name) ─┐
                                    OUT_INSTANCES ───┴► COPY_TO_POINTS (piece attribute = name)
    GROUND, AREAS, ROADS, FOOTPRINTS (Python) ; MASSING (Python, toggle)
    MERGE ─► OUT (display/render)

All parameter names are set defensively (a missing parm prints a warning
instead of failing), because they vary slightly between Houdini versions.
"""
from __future__ import annotations

import os

from . import core

PY_CODE = "import geobox_houdini.sop as g\ng.{fn}()\n"


def _hou():
    import hou
    return hou


def _set(node, name, value):
    p = node.parm(name)
    if p is None:
        pt = node.parmTuple(name)
        if pt is None:
            print("GeoBox: %s has no parm '%s' (Houdini version difference) - set it by hand" % (node.path(), name))
            return False
        pt.set(value)
        return True
    try:
        p.set(value)
    except Exception:  # noqa: BLE001 - menu parms may want a token / index
        try:
            p.set(str(value))
        except Exception as exc:  # noqa: BLE001
            print("GeoBox: could not set %s/%s = %r (%s)" % (node.path(), name, value, exc))
            return False
    return True


def _python_sop(parent, name, fn):
    n = parent.createNode("python", name)
    n.parm("python").set(PY_CODE.format(fn=fn))
    return n


def _add_spare_parms(geo, scene_file):
    hou = _hou()
    ptg = geo.parmTemplateGroup()
    if ptg.find("geobox") is not None:
        ptg.remove("geobox")
    f = hou.FolderParmTemplate("geobox", "GeoBox", folder_type=hou.folderType.Tabs)
    f.addParmTemplate(hou.StringParmTemplate(
        "scene_file", "Scene File (scene.json)", 1, default_value=(scene_file,),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
        tags={"filechooser_pattern": "*.json"}))
    f.addParmTemplate(hou.ButtonParmTemplate(
        "reload", "Reload From Disk",
        script_callback="import geobox_houdini.build as b; b.reload(kwargs['node'])",
        script_callback_language=hou.scriptLanguage.Python))
    f.addParmTemplate(hou.ButtonParmTemplate(
        "export_edits", "Export Edits -> scene.json",
        script_callback="import geobox_houdini.build as b; b.export_edits(kwargs['node'])",
        script_callback_language=hou.scriptLanguage.Python))
    f.addParmTemplate(hou.ToggleParmTemplate("only_used", "Load Only Used Assets", default_value=True))
    f.addParmTemplate(hou.ToggleParmTemplate("show_massing", "Show OSM Massing (reference)", default_value=False))
    f.addParmTemplate(hou.ToggleParmTemplate("show_layers", "Show Ground / Roads / Areas", default_value=True))
    ptg.append(f)
    geo.setParmTemplateGroup(ptg)


def build_city(scene_file=None, parent="/obj", name="geobox_city"):
    hou = _hou()
    if not scene_file:
        scene_file = hou.ui.selectFile(title="GeoBox: choose projects/<name>/scene.json", pattern="*.json",
                                       file_type=hou.fileType.Any)
        if not scene_file:
            return None
    scene_file = hou.expandString(scene_file)
    core.SceneData(scene_file)  # fail early with a readable error if the file is wrong
    obj = hou.node(parent)
    geo = obj.node(name)
    if geo is None:
        geo = obj.createNode("geo", name)
    for c in geo.children():
        c.destroy()
    _add_spare_parms(geo, scene_file)

    inst = _python_sop(geo, "INSTANCES", "instances")
    edit = geo.createNode("null", "EDIT_INSTANCES")
    edit.setInput(0, inst)
    out_inst = geo.createNode("null", "OUT_INSTANCES")
    out_inst.setInput(0, edit)

    lib = _python_sop(geo, "LIBRARY", "library")
    pack = geo.createNode("pack", "PACK_LIBRARY")
    pack.setInput(0, lib)
    _set(pack, "packbyname", 1)
    _set(pack, "nameattribute", "name")
    _set(pack, "pivot", "origin")

    copy = geo.createNode("copytopoints", "COPY_TO_POINTS")
    copy.setInput(0, pack)
    copy.setInput(1, out_inst)
    _set(copy, "usepieceattrib", 1)
    _set(copy, "pieceattrib", "name")
    _set(copy, "pack", 1)
    _set(copy, "pivot", "origin")
    _set(copy, "transform", 1)

    # Alternative fast path: packed disk prims straight from s@instancefile (renders lazily).
    disk = geo.createNode("attribwrangle", "INSTANCE_PACKED_DISK")
    disk.setInput(0, out_inst)
    _set(disk, "snippet", DISK_VEX)  # default "Run Over" is Points

    ground = _python_sop(geo, "GROUND", "ground")
    areas = _python_sop(geo, "AREAS", "areas")
    roads = _python_sop(geo, "ROADS", "roads")
    feet = _python_sop(geo, "FOOTPRINTS", "footprints")
    mass = _python_sop(geo, "MASSING", "massing")

    layers = geo.createNode("merge", "LAYERS")
    for i, n in enumerate((ground, areas, roads)):
        layers.setInput(i, n)
    sw_layers = geo.createNode("switch", "SHOW_LAYERS")
    empty = geo.createNode("null", "EMPTY")
    sw_layers.setInput(0, empty)
    sw_layers.setInput(1, layers)
    sw_layers.parm("input").setExpression('ch("../show_layers")')
    sw_mass = geo.createNode("switch", "SHOW_MASSING")
    sw_mass.setInput(0, empty)
    sw_mass.setInput(1, mass)
    sw_mass.parm("input").setExpression('ch("../show_massing")')

    merge = geo.createNode("merge", "MERGE")
    merge.setInput(0, copy)
    merge.setInput(1, sw_layers)
    merge.setInput(2, sw_mass)
    out = geo.createNode("null", "OUT")
    out.setInput(0, merge)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    for n in (feet, disk):
        n.setComment("not wired: use as a starting point")
        n.setGenericFlag(hou.nodeFlag.DisplayComment, True)
    geo.layoutChildren()
    geo.setComment("GeoBox: %s" % scene_file)
    print("GeoBox: built %s from %s" % (geo.path(), scene_file))
    return geo


DISK_VEX = r"""// GeoBox: one packed-disk primitive per point, loading s@instancefile lazily.
// Requires instancefile to be already normalised (see geobox_houdini.asset_prep).
int prim = addprim(0, "packeddisk", @ptnum);
setprimintrinsic(0, "unexpandedfilename", prim, s@instancefile);
matrix3 m = ident();
scale(m, v@scale);
m *= qconvert(p@orient);
setprimintrinsic(0, "transform", prim, m);
"""


def reload(geo):
    for c in geo.children():
        if c.type().name() == "python":
            c.cook(force=True)
    print("GeoBox: reloaded %s" % geo.parm("scene_file").evalAsString())


def _records_from_geo(g):
    names = ("name", "geobox_id", "category")
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


def export_edits(geo, source="OUT_INSTANCES"):
    """Write the (edited) instance points of `geo/<source>` back to scene.json."""
    hou = _hou()
    path = hou.expandString(geo.parm("scene_file").evalAsString())
    data = core.SceneData(path)
    node = geo.node(source)
    recs = _records_from_geo(node.geometry())
    scene, report = core.records_to_scene(recs, data.scene, data.assets)
    written = core.save_scene(data, scene)
    msg = ("GeoBox export: %(updated)d updated, %(added)d added, %(deleted)d deleted, %(unchanged)d unchanged" % report)
    print(msg, "\n", "\n ".join(written))
    if hou.isUIAvailable():
        hou.ui.displayMessage(msg + "\n\n" + "\n".join(written), title="GeoBox")
    return report


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
    print("GeoBox: %s -> %s" % (n.path(), usd))
    return n
