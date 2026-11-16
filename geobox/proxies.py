"""Procedural placeholder assets (OBJ + USDA), already normalised to the
library standard: metres, +Y up, pivot at base centre, building front = +Z,
building long side along X.

They let the whole pipeline run before real assets exist; replace them by
editing assets/library.json (see docs/asset_library.md).
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Tuple

V3 = Tuple[float, float, float]


class Mesh:
    def __init__(self):
        self.parts: Dict[str, Tuple[List[V3], List[List[int]]]] = {}

    def _part(self, mat):
        return self.parts.setdefault(mat, ([], []))

    def add_face_list(self, mat: str, verts: List[V3], faces: List[List[int]]):
        pv, pf = self._part(mat)
        base = len(pv)
        pv.extend(verts)
        pf.extend([[base + i for i in f] for f in faces])

    # primitives (faces are CCW seen from outside)
    def box(self, mat, x0, y0, z0, x1, y1, z1, top=True, bottom=False):
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1),
             (x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)]
        f = [[1, 0, 4, 5], [2, 1, 5, 6], [3, 2, 6, 7], [0, 3, 7, 4]]
        if top:
            f.append([4, 7, 6, 5])
        if bottom:
            f.append([0, 1, 2, 3])
        self.add_face_list(mat, v, f)

    def gable(self, mat_roof, mat_wall, x0, z0, x1, z1, y0, y1):
        """Gable roof with ridge along X."""
        zm = (z0 + z1) / 2
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1), (x0, y1, zm), (x1, y1, zm)]
        self.add_face_list(mat_roof, v, [[0, 4, 5, 1], [2, 5, 4, 3]])
        self.add_face_list(mat_wall, v, [[3, 4, 0], [1, 5, 2]])

    def cylinder(self, mat, cx, cz, r0, r1, y0, y1, seg=8, cap=True):
        v, f = [], []
        for i in range(seg):
            a = 2 * math.pi * i / seg
            v.append((cx + r0 * math.cos(a), y0, cz + r0 * math.sin(a)))
            v.append((cx + r1 * math.cos(a), y1, cz + r1 * math.sin(a)))
        for i in range(seg):
            j = (i + 1) % seg
            f.append([2 * i, 2 * i + 1, 2 * j + 1, 2 * j])
        if cap and r1 > 1e-6:
            f.append([2 * i + 1 for i in range(seg)][::-1])
        self.add_face_list(mat, v, f)

    def cone(self, mat, cx, cz, r, y0, y1, seg=10):
        v = [(cx + r * math.cos(2 * math.pi * i / seg), y0, cz + r * math.sin(2 * math.pi * i / seg)) for i in range(seg)]
        v.append((cx, y1, cz))
        f = [[i, seg, (i + 1) % seg] for i in range(seg)]
        f.append(list(range(seg)))
        self.add_face_list(mat, v, f)

    def blob(self, mat, cx, cy, cz, rx, ry, rz, rings=5, seg=10, seed=0.0):
        """Lumpy ellipsoid (tree crown)."""
        v = [(cx, cy - ry, cz)]
        for r in range(1, rings):
            phi = math.pi * r / rings
            for s in range(seg):
                th = 2 * math.pi * s / seg
                n = 1.0 + 0.12 * math.sin(3 * th + seed) * math.sin(2 * phi + seed * 0.7)
                v.append((cx + rx * n * math.sin(phi) * math.cos(th), cy - ry * n * math.cos(phi),
                          cz + rz * n * math.sin(phi) * math.sin(th)))
        v.append((cx, cy + ry, cz))
        top = len(v) - 1
        f = []
        for s in range(seg):
            f.append([0, 1 + s, 1 + (s + 1) % seg])
        for r in range(rings - 2):
            a, b = 1 + r * seg, 1 + (r + 1) * seg
            for s in range(seg):
                t = (s + 1) % seg
                f.append([a + s, b + s, b + t, a + t])
        last = 1 + (rings - 2) * seg
        for s in range(seg):
            f.append([last + s, top, last + (s + 1) % seg])
        self.add_face_list(mat, v, f)

    # writers -----------------------------------------------------------------
    def bounds(self):
        pts = [p for verts, _ in self.parts.values() for p in verts]
        mn = [min(p[i] for p in pts) for i in range(3)]
        mx = [max(p[i] for p in pts) for i in range(3)]
        return mn, mx

    def write_obj(self, path: str, name: str):
        lines = [f"# GeoBox proxy asset '{name}' - units: metres, +Y up, pivot base-centre", f"o {name}"]
        offset = 0
        body = []
        for mat, (verts, faces) in self.parts.items():
            for p in verts:
                lines.append("v %.4f %.4f %.4f" % p)
            body.append(f"g {mat}")
            body.append(f"usemtl {mat}")
            for fc in faces:
                body.append("f " + " ".join(str(offset + i + 1) for i in fc))
            offset += len(verts)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines + body) + "\n")

    def write_usda(self, path: str, name: str, colors: Dict[str, str]):
        def hex2rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

        mn, mx = self.bounds()
        out = ["#usda 1.0", "(", f'    defaultPrim = "{name}"', "    metersPerUnit = 1", '    upAxis = "Y"', ")", "",
               f'def Xform "{name}" (', '    kind = "component"', ")", "{",
               "    float3[] extentsHint = [(%g, %g, %g), (%g, %g, %g)]" % (*mn, *mx)]
        for mat, (verts, faces) in self.parts.items():
            # USD default orientation is rightHanded == CCW front faces, same as ours
            c = hex2rgb(colors.get(mat, "#bbbbbb"))
            pts = ", ".join("(%.4f, %.4f, %.4f)" % p for p in verts)
            counts = ", ".join(str(len(fc)) for fc in faces)
            idx = ", ".join(str(i) for fc in faces for i in fc)
            pmn = [min(p[i] for p in verts) for i in range(3)]
            pmx = [max(p[i] for p in verts) for i in range(3)]
            out += [f'    def Mesh "{mat}"', "    {",
                    "        float3[] extent = [(%g, %g, %g), (%g, %g, %g)]" % (*pmn, *pmx),
                    f"        int[] faceVertexCounts = [{counts}]",
                    f"        int[] faceVertexIndices = [{idx}]",
                    f"        point3f[] points = [{pts}]",
                    "        color3f[] primvars:displayColor = [(%.4f, %.4f, %.4f)]" % c,
                    '        uniform token subdivisionScheme = "none"',
                    "    }"]
        out.append("}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")


# --- the default proxy set ----------------------------------------------------
def _house():
    m = Mesh()
    m.box("wall", -5, 0, -3.5, 5, 4.2, 3.5, top=False)
    m.gable("roof", "wall", -5.4, -4.0, 5.4, 4.0, 4.2, 7.0)
    m.box("trim", -1.0, 0, 3.5, 1.0, 2.2, 3.62)  # door on the front (+Z)
    return m


def _lowrise():
    m = Mesh()
    m.box("wall", -8, 0, -6, 8, 11.0, 6)
    m.box("trim", -8.1, 0, -6.1, 8.1, 3.6, 6.1)  # ground-floor shop band
    m.box("roof", -2.5, 11.0, -2.0, 2.5, 12.6, 2.0)  # stair/roof box
    return m


def _midrise():
    m = Mesh()
    m.box("wall", -10, 0, -8, 10, 24.0, 8)
    m.box("wall", -8, 24.0, -6, 8, 30.0, 6)  # setback
    m.box("trim", -10.1, 0, -8.1, 10.1, 4.5, 8.1)
    m.box("roof", -3, 30.0, -2, 3, 32.0, 2)
    return m


def _highrise():
    m = Mesh()
    m.box("trim", -15, 0, -15, 15, 12.0, 15)  # podium
    m.box("wall", -11, 12.0, -11, 11, 82.0, 11)
    m.box("glass", -9, 82.0, -9, 9, 90.0, 9)
    m.cylinder("roof", 0, 0, 0.4, 0.15, 90.0, 98.0, seg=6)
    return m


def _industrial():
    m = Mesh()
    m.box("wall", -15, 0, -10, 15, 7.0, 10, top=False)
    for i in range(5):
        x0 = -15 + i * 6
        m.add_face_list("roof", [(x0, 7.0, -10), (x0 + 6, 7.0, -10), (x0 + 6, 7.0, 10), (x0, 7.0, 10),
                                 (x0, 9.0, -10), (x0, 9.0, 10)],
                        [[3, 5, 4, 0], [5, 2, 1, 4], [0, 4, 1], [3, 2, 5]])
    return m


def _generic():
    m = Mesh()
    m.box("wall", -5, 0, -5, 5, 10, 5)
    return m


def _broadleaf(h=8.0, seed=0.0):
    m = Mesh()
    m.cylinder("trunk", 0, 0, 0.25 * h / 8, 0.16 * h / 8, 0, h * 0.45, seg=6)
    m.blob("leaves", 0, h * 0.62, 0, h * 0.34, h * 0.36, h * 0.34, seed=seed)
    return m


def _conifer(h=10.0):
    m = Mesh()
    m.cylinder("trunk", 0, 0, 0.22, 0.12, 0, h * 0.25, seg=6)
    m.cone("leaves", 0, 0, h * 0.24, h * 0.18, h * 0.7, seg=10)
    m.cone("leaves", 0, 0, h * 0.18, h * 0.5, h, seg=10)
    return m


def _palm(h=9.0):
    m = Mesh()
    m.cylinder("trunk", 0, 0, 0.22, 0.16, 0, h * 0.9, seg=6)
    m.blob("leaves", 0, h * 0.9, 0, 2.6, 0.7, 2.6, rings=4, seg=8, seed=2.0)
    return m


def _bush():
    m = Mesh()
    m.blob("leaves", 0, 0.7, 0, 1.0, 0.7, 1.0, rings=4, seg=8, seed=1.0)
    return m


def _lamp():
    m = Mesh()
    m.cylinder("metal", 0, 0, 0.09, 0.06, 0, 6.0, seg=6)
    m.box("metal", -0.05, 5.85, 0, 0.05, 5.95, 1.2)
    m.box("light", -0.18, 5.75, 0.95, 0.18, 5.85, 1.35)
    return m


PROXY_COLORS = {
    "wall": "#d8d0c4", "roof": "#7c5b4a", "trim": "#9aa3ab", "glass": "#7fa7c4",
    "trunk": "#6b4a32", "leaves": "#4f7d3a", "metal": "#4a4f55", "light": "#fff2b0",
}

PROXIES = {
    # id: (builder, category, tags, extra manifest fields)
    "bld_house_a": (_house, "building", ["house", "detached", "residential", "small"],
                    {"label": "獨棟住宅 House", "height_range": [3, 10], "fit": "stretch"}),
    "bld_lowrise_a": (_lowrise, "building", ["residential", "commercial", "retail", "generic", "small"],
                      {"label": "低樓層 Low-rise", "height_range": [6, 18], "fit": "stretch"}),
    "bld_midrise_a": (_midrise, "building", ["apartments", "residential", "office", "commercial", "generic"],
                      {"label": "中高樓 Mid-rise", "height_range": [15, 45], "fit": "stretch"}),
    "bld_highrise_a": (_highrise, "building", ["office", "hotel", "apartments", "commercial", "tower"],
                       {"label": "高樓 High-rise", "height_range": [40, 600], "fit": "stretch"}),
    "bld_industrial_a": (_industrial, "building", ["industrial", "warehouse", "factory", "retail", "supermarket"],
                         {"label": "廠房 Industrial", "height_range": [4, 15], "fit": "stretch"}),
    "bld_generic_box": (_generic, "building", ["generic", "fallback"],
                        {"label": "通用量體 Generic box", "height_range": [1, 1000], "fit": "stretch", "weight": 0.2}),
    "tree_broadleaf_a": (lambda: _broadleaf(8.0, 0.0), "tree", ["broadleaf", "any", "street"],
                         {"label": "闊葉樹 A", "scale_range": [0.7, 1.4]}),
    "tree_broadleaf_b": (lambda: _broadleaf(12.0, 1.7), "tree", ["broadleaf", "any", "forest"],
                         {"label": "闊葉樹 B (大)", "scale_range": [0.7, 1.3]}),
    "tree_conifer_a": (_conifer, "tree", ["conifer", "any", "forest"], {"label": "針葉樹", "scale_range": [0.7, 1.4]}),
    "tree_palm_a": (_palm, "tree", ["palm", "street"], {"label": "棕櫚 Palm", "scale_range": [0.8, 1.2], "weight": 0.5}),
    "bush_a": (_bush, "tree", ["bush", "scrub"], {"label": "灌木 Bush", "scale_range": [0.6, 1.5]}),
    "prop_streetlamp_a": (_lamp, "prop", ["streetlamp"], {"label": "路燈 Street lamp", "fit": "none"}),
}


def generate(out_dir: str) -> List[dict]:
    """Write all proxies into out_dir; return manifest entries."""
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for aid, (builder, cat, tags, extra) in PROXIES.items():
        mesh = builder()
        obj = os.path.join(out_dir, aid + ".obj")
        usda = os.path.join(out_dir, aid + ".usda")
        mesh.write_obj(obj, aid)
        mesh.write_usda(usda, aid, PROXY_COLORS)
        mn, mx = mesh.bounds()
        mats = {k: PROXY_COLORS.get(k, "#bbbbbb") for k in mesh.parts}
        e = {"id": aid, "category": cat, "tags": tags,
             "files": {"web": f"proxies/{aid}.obj", "houdini": f"proxies/{aid}.obj", "usd": f"proxies/{aid}.usda"},
             "source_units": "m", "source_up": "+Y",
             "bounds": {"min": [round(v, 4) for v in mn], "max": [round(v, 4) for v in mx]},
             "materials": mats, "proxy": True}
        e.update(extra)
        entries.append(e)
    return entries
