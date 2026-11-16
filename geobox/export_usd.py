"""scene.json -> scene.usda (pure text, no pxr dependency).

Layout of the stage (metres, Y-up):
  /GeoBox                       Xform  kind=assembly
    Ground                      Mesh   the box slab (top at y=0)
    Terrain                     Mesh   (if terrain was sampled)
    Reference/Massing           Mesh   OSM footprints extruded, purpose=guide
    Reference/Greybox           Mesh   editable greybox blocks (scene greybox[]), purpose=guide
    Reference/Roads             BasisCurves (widths = road width), purpose=guide
    Instances/<Category>        PointInstancer per category (Buildings/Trees/Props)
        Prototypes/<asset_id>   Xform referencing the asset's USD file (normalize
                                transform applied), or a coloured Cube proxy.

In Houdini Solaris: Sublayer or Reference LOP -> this file.
"""
from __future__ import annotations

import math
import os
from typing import Dict, List

from .library import asset_size, by_id, resolve
from .fsutil import relpath

CAT_PRIM = {"building": "Buildings", "tree": "Trees", "prop": "Props"}


def _f(v: float) -> str:
    return ("%.4f" % v).rstrip("0").rstrip(".") if v != 0 else "0"


def _v3(p) -> str:
    return "(%s, %s, %s)" % (_f(p[0]), _f(p[1]), _f(p[2]))


def _arr(items: List[str], per_line: int = 8, indent: str = "            ") -> str:
    if not items:
        return "[]"
    lines = [", ".join(items[i:i + per_line]) for i in range(0, len(items), per_line)]
    return "[\n" + indent + (",\n" + indent).join(lines) + "\n" + indent[:-4] + "]"


def _hex(c: str):
    c = (c or "#bbbbbb").lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _ident(s: str) -> str:
    out = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)
    return out if out and not out[0].isdigit() else "_" + out


def _massing(layout: dict, height_at) -> Dict[str, List[str]]:
    pts: List[str] = []
    counts: List[str] = []
    idx: List[str] = []
    for b in layout.get("buildings", []):
        ring = b["outer"]
        n = len(ring)
        if n < 3:
            continue
        y0 = min(height_at(p[0], p[1]) for p in ring) + b.get("min_height", 0.0)
        y1 = y0 + max(b["height"] - b.get("min_height", 0.0), 1.0)
        base = len(pts)
        for p in ring:
            pts.append(_v3((p[0], y0, p[1])))
        for p in ring:
            pts.append(_v3((p[0], y1, p[1])))
        for i in range(n):
            j = (i + 1) % n
            counts.append("4")
            idx += [str(base + i), str(base + j), str(base + n + j), str(base + n + i)]
        counts.append(str(n))
        idx += [str(base + n + i) for i in range(n)]
    return {"points": pts, "counts": counts, "indices": idx}


def _prototype(lib: dict, a: dict, layer_dir: str, ind: str) -> List[str]:
    name = _ident(a["id"])
    usd = resolve(lib, (a.get("files") or {}).get("usd"))
    if usd and os.path.exists(usd):
        rel = relpath(usd, layer_dir)
        nz = a.get("normalize") or {}
        s = nz.get("scale", 1.0)
        off = nz.get("offset", [0.0, 0.0, 0.0])
        lines = [f'{ind}def Xform "{name}" (', f"{ind}    prepend references = @{rel}@", f"{ind})", f"{ind}{{"]
        ops = []
        if any(abs(v) > 1e-9 for v in off):
            lines.append(f"{ind}    double3 xformOp:translate = {_v3(off)}")
            ops.append('"xformOp:translate"')
        if nz.get("up") in ("+Z", "Z", "z"):
            lines.append(f"{ind}    float xformOp:rotateX = -90")
            ops.append('"xformOp:rotateX"')
        if abs(s - 1.0) > 1e-9:
            lines.append(f"{ind}    float3 xformOp:scale = {_v3((s, s, s))}")
            ops.append('"xformOp:scale"')
        if ops:
            lines.append(f"{ind}    uniform token[] xformOpOrder = [{', '.join(ops)}]")
        lines.append(f"{ind}}}")
        return lines
    # fallback: coloured box with the asset's measured bounds
    w, h, d = asset_size(a)
    col = _hex(a.get("preview_color") or next(iter((a.get("materials") or {}).values()), "#bbbbbb"))
    return [f'{ind}def Cube "{name}"', f"{ind}{{",
            f"{ind}    double size = 2",
            f"{ind}    color3f[] primvars:displayColor = [{_v3(col)}]",
            f"{ind}    double3 xformOp:translate = {_v3((0, h / 2, 0))}",
            f"{ind}    float3 xformOp:scale = {_v3((w / 2, h / 2, d / 2))}",
            f'{ind}    uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]',
            f"{ind}}}"]


def export_usda(scene: dict, layout: dict, lib: dict, path: str) -> str:
    from .terrain import sampler

    height_at = sampler(layout.get("terrain"))
    layer_dir = os.path.dirname(os.path.abspath(path))
    assets = by_id(lib)
    x0, z0, x1, z1 = layout["box"]["rect"]
    o = layout.get("origin", {})
    L: List[str] = [
        "#usda 1.0", "(",
        '    defaultPrim = "GeoBox"',
        "    metersPerUnit = 1",
        '    upAxis = "Y"',
        "    customLayerData = {",
        f'        string geobox_name = "{scene.get("name", "")}"',
        f'        string geobox_origin = "{o.get("lat")}, {o.get("lon")}, {o.get("elevation", 0)}"',
        '        string geobox_attribution = "Map data (c) OpenStreetMap contributors, ODbL"',
        "    }", ")", "",
        'def Xform "GeoBox" (', '    kind = "assembly"', ")", "{",
    ]
    # ground slab
    T = 4.0
    g = [(x0, -T, z0), (x1, -T, z0), (x1, -T, z1), (x0, -T, z1), (x0, 0, z0), (x1, 0, z0), (x1, 0, z1), (x0, 0, z1)]
    L += ['    def Mesh "Ground"', "    {",
          "        float3[] extent = [%s, %s]" % (_v3(g[0]), _v3(g[6])),
          "        int[] faceVertexCounts = [4, 4, 4, 4, 4, 4]",
          "        int[] faceVertexIndices = [4, 7, 6, 5, 0, 1, 2, 3, 1, 0, 4, 5, 2, 1, 5, 6, 3, 2, 6, 7, 0, 3, 7, 4]",
          "        point3f[] points = [%s]" % ", ".join(_v3(p) for p in g),
          "        color3f[] primvars:displayColor = [(0.42, 0.45, 0.4)]",
          '        uniform token subdivisionScheme = "none"', "    }"]

    terr = layout.get("terrain")
    if terr:
        nx, nz = terr["res"]
        tx0, tz0, tx1, tz1 = terr["rect"]
        H = terr["heights"]
        pts = [_v3((tx0 + (tx1 - tx0) * i / (nx - 1), H[j * nx + i], tz0 + (tz1 - tz0) * j / (nz - 1)))
               for j in range(nz) for i in range(nx)]
        idx = []
        for j in range(nz - 1):
            for i in range(nx - 1):
                a = j * nx + i
                idx += [str(a), str(a + nx), str(a + nx + 1), str(a + 1)]
        L += ['    def Mesh "Terrain"', "    {",
              f"        int[] faceVertexCounts = {_arr(['4'] * ((nx - 1) * (nz - 1)), 32)}",
              f"        int[] faceVertexIndices = {_arr(idx, 32)}",
              f"        point3f[] points = {_arr(pts, 6)}",
              "        color3f[] primvars:displayColor = [(0.36, 0.42, 0.3)]",
              '        uniform token subdivisionScheme = "none"', "    }"]

    # reference layers
    m = _massing(layout, height_at)
    L += ['    def Scope "Reference"', "    {"]
    if m["counts"]:
        L += ['        def Mesh "Massing"', "        {",
              '            uniform token purpose = "guide"',
              f"            int[] faceVertexCounts = {_arr(m['counts'], 32, '                ')}",
              f"            int[] faceVertexIndices = {_arr(m['indices'], 32, '                ')}",
              f"            point3f[] points = {_arr(m['points'], 6, '                ')}",
              "            color3f[] primvars:displayColor = [(0.8, 0.8, 0.85)]",
              '            uniform token subdivisionScheme = "none"', "        }"]
    blocks = scene.get("greybox") or []
    if blocks:
        from .greybox import BOX_FACES, box_vertices
        gp, gc, gi = [], [], []
        for b in blocks:
            base = len(gp)
            gp += [_v3(v) for v in box_vertices(b)]
            for f in BOX_FACES:
                gc.append("4")
                gi += [str(base + i) for i in f]
        L += ['        def Mesh "Greybox" (', '            doc = "GeoBox greybox blockout (scene.json greybox[])"', "        )", "        {",
              '            uniform token purpose = "guide"',
              f"            int[] faceVertexCounts = {_arr(gc, 32, '                ')}",
              f"            int[] faceVertexIndices = {_arr(gi, 32, '                ')}",
              f"            point3f[] points = {_arr(gp, 6, '                ')}",
              "            color3f[] primvars:displayColor = [(0.55, 0.55, 0.55)]",
              '            uniform token subdivisionScheme = "none"', "        }"]
    roads = [r for r in layout.get("roads", []) if len(r["line"]) >= 2]
    if roads:
        pts, cnt, wid = [], [], []
        for r in roads:
            cnt.append(str(len(r["line"])))
            for p in r["line"]:
                pts.append(_v3((p[0], height_at(p[0], p[1]) + 0.05, p[1])))
                wid.append(_f(r["width"]))
        L += ['        def BasisCurves "Roads"', "        {",
              '            uniform token purpose = "guide"',
              '            uniform token type = "linear"',
              f"            int[] curveVertexCounts = {_arr(cnt, 32, '                ')}",
              f"            point3f[] points = {_arr(pts, 6, '                ')}",
              f"            float[] widths = {_arr(wid, 32, '                ')} (",
              '                interpolation = "vertex"', "            )",
              "            color3f[] primvars:displayColor = [(0.25, 0.25, 0.27)]", "        }"]
    L += ["    }"]

    # instancers
    L += ['    def Scope "Instances"', "    {"]
    by_cat: Dict[str, List[dict]] = {}
    for inst in scene.get("instances", []):
        if inst.get("asset") in assets:
            by_cat.setdefault(assets[inst["asset"]].get("category", "prop"), []).append(inst)
    for cat, insts in by_cat.items():
        protos = sorted({i["asset"] for i in insts})
        pidx = {a: k for k, a in enumerate(protos)}
        prim = CAT_PRIM.get(cat, _ident(cat.title()))
        base = f"/GeoBox/Instances/{prim}/Prototypes"
        pos, ori, scl, pi, ids, names = [], [], [], [], [], []
        for i in insts:
            th = math.radians(i.get("rot_y", 0.0)) / 2.0
            pos.append(_v3(i["p"]))
            ori.append("(%s, 0, %s, 0)" % (_f(math.cos(th)), _f(math.sin(th))))  # (w, x, y, z)
            scl.append(_v3(i.get("scale", [1, 1, 1])))
            pi.append(str(pidx[i["asset"]]))
            ids.append('"%s"' % i["id"].replace('"', "'"))
            names.append('"%s"' % i["asset"])
        L += [f'        def PointInstancer "{prim}"', "        {",
              "            rel prototypes = [%s]" % ", ".join(f"<{base}/{_ident(a)}>" for a in protos),
              f"            int[] protoIndices = {_arr(pi, 32, '                ')}",
              f"            point3f[] positions = {_arr(pos, 6, '                ')}",
              f"            quath[] orientations = {_arr(ori, 6, '                ')}",
              f"            quatf[] orientationsf = {_arr(ori, 6, '                ')}",
              f"            float3[] scales = {_arr(scl, 6, '                ')}",
              f"            string[] primvars:geobox_id = {_arr(ids, 6, '                ')} (",
              '                interpolation = "vertex"', "            )",
              f"            string[] primvars:geobox_asset = {_arr(names, 6, '                ')} (",
              '                interpolation = "vertex"', "            )",
              '            def Scope "Prototypes"', "            {"]
        for a in protos:
            L += _prototype(lib, assets[a], layer_dir, "                ")
        L += ["            }", "        }"]
    L += ["    }", "}", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return path
