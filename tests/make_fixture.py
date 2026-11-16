"""Generate a synthetic Overpass JSON (out geom) fixture: a small fictional
neighbourhood with a road grid, buildings with mixed tags, a park built from a
multipolygon relation (two open ways), trees, a tree row and a pond.

    python tests/make_fixture.py  -> tests/fixtures/sample_osm.json
"""
import json
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobox.geo import LocalFrame  # noqa: E402

LAT, LON = 25.04, 121.52
F = LocalFrame(LAT, LON)
_id = [1000]


def nid():
    _id[0] += 1
    return _id[0]


def geomify(pts):
    out = []
    for x, z in pts:
        lat, lon = F.from_local(x, z)
        out.append({"lat": round(lat, 7), "lon": round(lon, 7)})
    return out


def way(pts, tags, closed=False):
    if closed:
        pts = list(pts) + [pts[0]]
    return {"type": "way", "id": nid(), "nodes": [nid() for _ in pts], "geometry": geomify(pts), "tags": tags}


def rect(cx, cz, w, d, ang=0.0):
    c, s = math.cos(ang), math.sin(ang)
    pts = []
    for (u, v) in ((-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)):
        pts.append((cx + u * c + v * s, cz - u * s + v * c))
    return pts


def main():
    rng = random.Random(7)
    els = []
    # road grid: avenues every 120 m, streets every 60 m
    for i, x in enumerate(range(-300, 301, 60)):
        kind = "secondary" if x % 120 == 0 else "residential"
        tags = {"highway": kind, "name": f"Street {i}"}
        if kind == "secondary":
            tags["lanes"] = "4"
        els.append(way([(x, -320), (x, 320)], tags))
    for j, z in enumerate(range(-300, 301, 60)):
        kind = "primary" if z == 0 else "residential"
        els.append(way([(-320, z), (320, z)], {"highway": kind, "name": f"Road {j}"}))
    els.append(way([(-250, -250), (-200, -180), (-130, -170)], {"highway": "footway"}))

    # park block (x 60..120, z 60..120) as a multipolygon relation made of two open ways + pond hole
    park = [(66, 66), (114, 66), (114, 114), (66, 114)]
    w1 = way([park[0], park[1], park[2]], {})
    w2 = way([park[2], park[3], park[0]], {})
    pond = way([(85, 85), (97, 85), (97, 95), (85, 95)], {"natural": "water"}, closed=True)
    els += [w2, pond]
    els.append({"type": "relation", "id": nid(), "tags": {"type": "multipolygon", "leisure": "park", "name": "Fixture Park"},
                "members": [{"type": "way", "ref": w1["id"], "role": "outer", "geometry": w1["geometry"]},
                            {"type": "way", "ref": w2["id"], "role": "outer", "geometry": w2["geometry"]},
                            {"type": "way", "ref": pond["id"], "role": "inner", "geometry": pond["geometry"]}]})
    # forest patch
    els.append(way([(-294, 186), (-186, 186), (-186, 294), (-294, 294)], {"landuse": "forest"}, closed=True))
    els.append(way([(186, -294), (294, -294), (294, -186), (186, -186)], {"landuse": "grass"}, closed=True))

    kinds = [("apartments", {"building:levels": "7"}), ("residential", {"building:levels": "4"}),
             ("house", {}), ("commercial", {"height": "21 m"}), ("yes", {"building:levels": "12"}),
             ("office", {"building:levels": "28"}), ("industrial", {}), ("retail", {"building:levels": "2"})]
    for bx in range(-300, 300, 60):
        for bz in range(-300, 300, 60):
            if (bx, bz) in ((60, 60),) or (bx < -180 and bz > 180) or (bx >= 180 and bz < -180):
                continue
            # 2x2 lots per block
            for (ox, oz) in ((15, 15), (45, 15), (15, 45), (45, 45)):
                if rng.random() < 0.15:
                    continue
                k, extra = kinds[rng.randrange(len(kinds))]
                w, d = rng.uniform(12, 22), rng.uniform(10, 20)
                tags = {"building": k}
                tags.update(extra)
                els.append(way(rect(bx + ox, bz + oz, w, d, rng.uniform(-0.15, 0.15)), tags, closed=True))
    # an L-shaped building
    els.append(way([(-55, -55), (-5, -55), (-5, -35), (-35, -35), (-35, -5), (-55, -5)], {"building": "school", "building:levels": "3"}, closed=True))

    # trees
    for k in range(25):
        x, z = rng.uniform(70, 110), rng.uniform(70, 110)
        if 84 < x < 98 and 84 < z < 96:
            continue
        els.append({"type": "node", "id": nid(), "lat": F.from_local(x, z)[0], "lon": F.from_local(x, z)[1],
                    "tags": {"natural": "tree", "leaf_type": rng.choice(["broadleaved", "needleleaved"])}})
    els.append(way([(-290, 7), (-130, 7)], {"natural": "tree_row"}))
    data = {"version": 0.6, "generator": "geobox synthetic fixture", "elements": els}
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample_osm.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    print(out, len(els), "elements")


if __name__ == "__main__":
    main()
