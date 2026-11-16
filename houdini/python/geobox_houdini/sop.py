"""Python SOP entry points. Each function fills `hou.pwd().geometry()`.

Network created by geobox_houdini.build.build_city(); every Python SOP there
contains a one-liner such as:

    import geobox_houdini.sop as g; g.instances()

The scene file comes from the `scene_file` parm on the parent geo node
(or on the Python SOP itself, if you add one).

Winding: Houdini front faces are clockwise seen from the front, so ground
polygons are written clockwise as seen from above (our rings are CCW).
"""
from __future__ import annotations

from . import core


def _hou():
    import hou  # noqa: WPS433 - only available inside Houdini
    return hou


def scene_path(node=None) -> str:
    hou = _hou()
    node = node or hou.pwd()
    for n in (node, node.parent()):
        p = n.parm("scene_file") if n is not None else None
        if p is not None:
            path = p.evalAsString()
            if path:
                return path
    raise hou.NodeError("GeoBox: set the 'scene_file' parameter (projects/<name>/scene.json)")


def _data(node=None) -> core.SceneData:
    return core.SceneData(scene_path(node))


def _add(geo, cls, name, default):
    hou = _hou()
    kind = {"point": hou.attribType.Point, "prim": hou.attribType.Prim, "detail": hou.attribType.Global}[cls]
    a = geo.findPointAttrib(name) if cls == "point" else geo.findPrimAttrib(name) if cls == "prim" else geo.findGlobalAttrib(name)
    return a or geo.addAttrib(kind, name, default)


def _detail_info(geo, data: core.SceneData):
    o = data.layout.get("origin", {})
    _add(geo, "detail", "geobox_scene", "")
    _add(geo, "detail", "geobox_origin", (0.0, 0.0, 0.0))
    geo.setGlobalAttribValue("geobox_scene", data.scene_path.replace("\\", "/"))
    geo.setGlobalAttribValue("geobox_origin", (float(o.get("lat", 0)), float(o.get("lon", 0)), float(o.get("elevation", 0))))


# --- instances ---------------------------------------------------------------
def instances(node=None):
    """Points: P, orient, scale, pscale, s@name (asset id), s@geobox_id, s@category,
    s@instancefile, s@source, s@rule, i@locked."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    recs = core.instance_records(data)
    _add(geo, "point", "orient", (0.0, 0.0, 0.0, 1.0))
    _add(geo, "point", "scale", (1.0, 1.0, 1.0))
    _add(geo, "point", "pscale", 1.0)
    for s in ("name", "geobox_id", "category", "instancefile", "source", "rule"):
        _add(geo, "point", s, "")
    _add(geo, "point", "locked", 0)
    _detail_info(geo, data)
    if not recs:
        return
    geo.createPoints([r["P"] for r in recs])
    geo.setPointFloatAttribValues("orient", [c for r in recs for c in r["orient"]])
    geo.setPointFloatAttribValues("scale", [c for r in recs for c in r["scale"]])
    geo.setPointFloatAttribValues("pscale", [r["pscale"] for r in recs])
    for s in ("name", "geobox_id", "category", "instancefile", "source", "rule"):
        geo.setPointStringAttribValues(s, [r[s] for r in recs])
    geo.setPointIntAttribValues("locked", [r["locked"] for r in recs])
    grp = {}
    for r in recs:
        grp.setdefault(r["category"] or "other", []).append(r)
    # point groups per category: @group_building etc.
    pts = geo.points()
    groups = {c: geo.createPointGroup(c) for c in grp}
    for i, r in enumerate(recs):
        groups[r["category"] or "other"].add(pts[i])


# --- asset library ---------------------------------------------------------------
def library(node=None):
    """All library assets, normalised to metres / +Y / base-centre, each tagged
    with prim s@name = asset id (feed into Pack by name -> Copy to Points)."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    used = {i.get("asset") for i in data.scene.get("instances", [])}
    only_used = node.parm("only_used") is None or node.evalParm("only_used")
    _add(geo, "prim", "name", "")
    for aid, a in data.assets.items():
        if only_used and aid not in used:
            continue
        g = hou.Geometry()
        path, normalized = data.asset_file(a)
        loaded = False
        if path:
            try:
                g.loadFromFile(path)
                loaded = len(g.prims()) > 0
            except hou.OperationFailed as exc:
                print("GeoBox: could not load %s (%s); using a proxy box" % (path, exc))
        if loaded and not normalized:
            g.transform(hou.Matrix4(core.normalize_matrix_rows(a)))
        if not loaded:
            b = a.get("bounds") or {"min": [-1, 0, -1], "max": [1, 2, 1]}
            size = [b["max"][i] - b["min"][i] for i in range(3)]
            verb = hou.sopNodeTypeCategory().nodeVerb("box")
            verb.setParms({"size": tuple(size), "t": (0.0, size[1] / 2.0, 0.0)})
            verb.execute(g, [])
        if g.findPrimAttrib("name") is None:
            g.addAttrib(hou.attribType.Prim, "name", "")
        g.setPrimStringAttribValues("name", [aid] * len(g.prims()))
        geo.merge(g)


# --- layout layers -------------------------------------------------------------------
def _poly(geo, ring, y_of, reverse=True):
    pts = list(ring)[::-1] if reverse else list(ring)
    poly = geo.createPolygon()
    for p in pts:
        pt = geo.createPoint()
        pt.setPosition((p[0], y_of(p[0], p[1]), p[1]))
        poly.addVertex(pt)
    return poly


def footprints(node=None):
    """Building footprints (flat, at the lowest ground point) with prim attribs
    height, min_height, levels, f@area, s@kind, s@osm_id, s@name, s@height_source.
    Extrude by @height for massing, or feed into your procedural building HDA."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    h = data.height_at()
    for n, d in (("height", 0.0), ("min_height", 0.0), ("levels", 0.0), ("area", 0.0), ("obb_angle", 0.0)):
        _add(geo, "prim", n, d)
    _add(geo, "prim", "obb_size", (0.0, 0.0))
    for n in ("kind", "osm_id", "building_name", "height_source"):
        _add(geo, "prim", n, "")
    _add(geo, "prim", "hole", 0)
    for b in data.layout.get("buildings", []):
        base = min(h(p[0], p[1]) for p in b["outer"])
        y = lambda x, z, base=base: base  # noqa: E731 - flat footprint
        for ring, is_hole in [(b["outer"], 0)] + [(r, 1) for r in b.get("holes", [])]:
            poly = _poly(geo, ring, y, reverse=not is_hole)
            poly.setAttribValue("height", float(b["height"]))
            poly.setAttribValue("min_height", float(b.get("min_height", 0.0)))
            poly.setAttribValue("levels", float(b.get("levels") or 0))
            poly.setAttribValue("area", float(b.get("area", 0.0)))
            poly.setAttribValue("obb_angle", float(b["obb"]["angle"]))
            poly.setAttribValue("obb_size", tuple(b["obb"]["size"]))
            poly.setAttribValue("kind", b.get("kind") or "")
            poly.setAttribValue("osm_id", b["id"])
            poly.setAttribValue("building_name", b.get("name") or "")
            poly.setAttribValue("height_source", b.get("height_source") or "")
            poly.setAttribValue("hole", is_hole)


def massing(node=None):
    """OSM 'map box' reference: footprints extruded to their heights."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    h = data.height_at()
    _add(geo, "prim", "osm_id", "")
    for b in data.layout.get("buildings", []):
        ring = b["outer"]
        y0 = min(h(p[0], p[1]) for p in ring) + b.get("min_height", 0.0)
        y1 = y0 + max(b["height"] - b.get("min_height", 0.0), 1.0)
        bot = [geo.createPoint() for _ in ring]
        top = [geo.createPoint() for _ in ring]
        for i, p in enumerate(ring):
            bot[i].setPosition((p[0], y0, p[1]))
            top[i].setPosition((p[0], y1, p[1]))
        n = len(ring)
        for i in range(n):  # walls: clockwise from outside
            j = (i + 1) % n
            poly = geo.createPolygon()
            for pt in (bot[i], top[i], top[j], bot[j]):
                poly.addVertex(pt)
            poly.setAttribValue("osm_id", b["id"])
        roof = geo.createPolygon()
        for pt in reversed(top):
            roof.addVertex(pt)
        roof.setAttribValue("osm_id", b["id"])


def roads(node=None):
    """Road centre lines (open polylines) with f@width, s@kind, i@bridge, i@tunnel, i@layer, s@road_name."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    h = data.height_at()
    _add(geo, "prim", "width", 0.0)
    for n in ("kind", "osm_id", "road_name"):
        _add(geo, "prim", n, "")
    for n in ("bridge", "tunnel", "layer"):
        _add(geo, "prim", n, 0)
    for layer, key in (("road", "roads"), ("waterway", "waterways")):
        for r in data.layout.get(key, []):
            poly = geo.createPolygon(is_closed=False)
            for p in r["line"]:
                pt = geo.createPoint()
                pt.setPosition((p[0], h(p[0], p[1]) + 0.02, p[1]))
                poly.addVertex(pt)
            poly.setAttribValue("width", float(r["width"]))
            poly.setAttribValue("kind", ("water:" if layer == "waterway" else "") + r["kind"])
            poly.setAttribValue("osm_id", r["id"])
            poly.setAttribValue("road_name", r.get("name") or "")
            poly.setAttribValue("bridge", 1 if r.get("bridge") else 0)
            poly.setAttribValue("tunnel", 1 if r.get("tunnel") else 0)
            poly.setAttribValue("layer", int(r.get("layer", 0)))


def areas(node=None):
    """Land-use / park / water polygons with s@kind (holes as separate prims, i@hole=1)."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    h = data.height_at()
    _add(geo, "prim", "kind", "")
    _add(geo, "prim", "osm_id", "")
    _add(geo, "prim", "hole", 0)
    for a in data.layout.get("areas", []):
        for ring, is_hole in [(a["outer"], 0)] + [(r, 1) for r in a.get("holes", [])]:
            poly = _poly(geo, ring, lambda x, z: h(x, z) + 0.01, reverse=not is_hole)
            poly.setAttribValue("kind", a["kind"])
            poly.setAttribValue("osm_id", a["id"])
            poly.setAttribValue("hole", is_hole)


def ground(node=None):
    """Terrain grid (if sampled) or a flat quad covering the box; f@height on points."""
    hou = _hou()
    node = node or hou.pwd()
    geo = node.geometry()
    data = _data(node)
    _detail_info(geo, data)
    x0, z0, x1, z1 = data.layout["box"]["rect"]
    t = data.layout.get("terrain")
    if not t:
        # NW, NE, SE, SW = clockwise seen from above -> Houdini front face points up
        quad = [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]
        poly = geo.createPolygon()
        for x, z in quad:
            pt = geo.createPoint()
            pt.setPosition((x, 0.0, z))
            poly.addVertex(pt)
        return
    nx, nz = t["res"]
    tx0, tz0, tx1, tz1 = t["rect"]
    H = t["heights"]
    pos = [(tx0 + (tx1 - tx0) * i / (nx - 1), H[j * nx + i], tz0 + (tz1 - tz0) * j / (nz - 1))
           for j in range(nz) for i in range(nx)]
    pts = geo.createPoints(pos)
    for j in range(nz - 1):
        for i in range(nx - 1):
            a = j * nx + i
            poly = geo.createPolygon()
            for k in (a, a + 1, a + nx + 1, a + nx):  # NW, NE, SE, SW = clockwise from above
                poly.addVertex(pts[k])
