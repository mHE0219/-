"""OSM features -> layout.json (semantic map in local metres).

layout.json schema (version 1):
{
  "version": 1, "type": "geobox.layout",
  "box":   {"center":[lat,lon], "size":[w,d], "rect":[x0,z0,x1,z1], "bbox_latlon":[s,w,n,e]},
  "origin":{"lat":..,"lon":..,"elevation": metres above sea level of y=0},
  "buildings": [{"id","outer":[[x,z]..],"holes":[..],"height","min_height","levels","height_source",
                 "kind","roof","name","obb":{"center","size","angle"},"area","base_y"}],
  "trees":     [{"id","p":[x,z],"kind","height"|null,"source"}],
  "tree_rows": [{"id","line":[[x,z]..]}],
  "roads":     [{"id","line":[[x,z]..],"kind","width","bridge","tunnel","layer","name"}],
  "areas":     [{"id","kind","outer","holes","area"}],      kind in AREA_KINDS
  "waterways": [{"id","line","kind","width"}],
  "terrain":   null | {"res":[nx,nz], "rect":[...], "heights":[...row-major z then x...]},
  "attribution": "© OpenStreetMap contributors"
}
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from . import geom
from .geo import BoxSpec, LocalFrame
from .osm import iter_features

LEVEL_HEIGHT = 3.2

DEFAULT_HEIGHTS = {
    "house": 7.0, "detached": 7.0, "semidetached_house": 7.0, "bungalow": 4.5, "terrace": 8.0,
    "residential": 12.0, "apartments": 18.0, "dormitory": 15.0, "hotel": 30.0,
    "commercial": 14.0, "retail": 8.0, "office": 25.0, "supermarket": 7.0,
    "industrial": 9.0, "warehouse": 10.0, "factory": 10.0,
    "school": 12.0, "university": 16.0, "college": 14.0, "kindergarten": 6.0, "hospital": 20.0,
    "church": 14.0, "temple": 8.0, "mosque": 12.0, "shrine": 5.0, "public": 12.0, "civic": 12.0,
    "train_station": 12.0, "transportation": 8.0, "parking": 10.0, "stadium": 20.0,
    "garage": 3.0, "garages": 3.0, "carport": 3.0, "shed": 3.0, "hut": 3.0, "roof": 4.0,
    "kiosk": 3.0, "toilets": 3.5, "service": 4.0, "construction": 8.0, "ruins": 4.0,
    "yes": 9.0,
}

ROAD_WIDTHS = {
    "motorway": 15.0, "trunk": 13.0, "primary": 12.0, "secondary": 10.0, "tertiary": 8.0,
    "motorway_link": 6.0, "trunk_link": 6.0, "primary_link": 6.0, "secondary_link": 6.0, "tertiary_link": 5.0,
    "residential": 6.0, "unclassified": 6.0, "living_street": 5.0, "service": 4.0, "road": 6.0,
    "pedestrian": 5.0, "footway": 2.0, "path": 1.5, "cycleway": 2.0, "steps": 2.0, "track": 3.0,
    "busway": 6.0, "corridor": 2.0, "bridleway": 2.0,
}
SKIP_HIGHWAY = {"proposed", "construction", "platform", "elevator", "raceway", "abandoned", "razed"}

AREA_KINDS = ("forest", "park", "grass", "scrub", "farmland", "orchard", "water", "wetland", "sand",
              "residential", "commercial", "industrial", "parking", "plaza", "school", "cemetery", "other")


def parse_length(v: Optional[str]) -> Optional[float]:
    """'12', '12 m', '12.5m', "40'", "40 ft", "12;15" -> metres."""
    if not v:
        return None
    v = v.split(";")[0].strip().lower().replace(",", ".")
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(m|meters?|metres?|ft|feet|')?", v)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) in ("ft", "feet", "'"):
        val *= 0.3048
    return val


def parse_float(v: Optional[str]) -> Optional[float]:
    try:
        return float(str(v).split(";")[0].strip()) if v not in (None, "") else None
    except ValueError:
        return None


def building_height(tags: Dict[str, str]) -> Tuple[float, float, Optional[float], str]:
    """-> (height, min_height, levels, source)."""
    kind = tags.get("building") or tags.get("building:part") or "yes"
    levels = parse_float(tags.get("building:levels"))
    roof_levels = parse_float(tags.get("roof:levels")) or 0.0
    h = parse_length(tags.get("height")) or parse_length(tags.get("building:height"))
    min_h = parse_length(tags.get("min_height"))
    if min_h is None:
        min_level = parse_float(tags.get("building:min_level"))
        min_h = min_level * LEVEL_HEIGHT if min_level else 0.0
    if h is not None and h > 0:
        src = "height"
    elif levels is not None and levels > 0:
        h = (levels + roof_levels) * LEVEL_HEIGHT + (0.0 if roof_levels else 0.6)
        src = "levels"
    else:
        h = DEFAULT_HEIGHTS.get(kind, DEFAULT_HEIGHTS["yes"])
        src = "default"
    if levels is None:
        levels = max(1.0, round((h - min_h) / LEVEL_HEIGHT))
    return round(h, 2), round(min_h, 2), levels, src


def area_kind(tags: Dict[str, str]) -> Optional[str]:
    lu, le, na = tags.get("landuse"), tags.get("leisure"), tags.get("natural")
    if na == "wood" or lu == "forest":
        return "forest"
    if na == "water" or lu in ("reservoir", "basin") or tags.get("waterway") == "riverbank" or "water" in tags and na is None and lu is None and le is None:
        return "water"
    if na == "wetland":
        return "wetland"
    if na in ("sand", "beach", "bare_rock"):
        return "sand"
    if na in ("scrub", "heath"):
        return "scrub"
    if le in ("park", "garden", "nature_reserve") or lu in ("recreation_ground", "village_green"):
        return "park"
    if le in ("pitch", "golf_course", "playground", "sports_centre", "dog_park") or lu in ("grass", "meadow") or na == "grassland":
        return "grass"
    if lu == "orchard" or lu == "vineyard":
        return "orchard"
    if lu in ("farmland", "farmyard", "allotments", "greenhouse_horticulture", "plant_nursery"):
        return "farmland"
    if lu == "cemetery" or tags.get("amenity") == "grave_yard":
        return "cemetery"
    if lu == "residential":
        return "residential"
    if lu in ("commercial", "retail"):
        return "commercial"
    if lu in ("industrial", "railway", "construction", "brownfield", "port"):
        return "industrial"
    if tags.get("amenity") == "parking":
        return "parking"
    if tags.get("amenity") in ("school", "university") or lu == "education":
        return "school"
    if tags.get("highway") in ("pedestrian",) or tags.get("place") == "square" or tags.get("man_made") == "pier":
        return "plaza"
    if lu or le or na:
        return "other"
    return None


def road_width(tags: Dict[str, str]) -> float:
    w = parse_length(tags.get("width")) or parse_length(tags.get("est_width"))
    if w:
        return w
    kind = tags.get("highway", "road")
    lanes = parse_float(tags.get("lanes"))
    if lanes and kind not in ("footway", "path", "cycleway", "steps", "pedestrian"):
        return max(lanes * 3.25 + 1.0, ROAD_WIDTHS.get(kind, 6.0) * 0.6)
    return ROAD_WIDTHS.get(kind, 5.0)


def tree_kind(tags: Dict[str, str]) -> str:
    lt = tags.get("leaf_type", "")
    if "needle" in lt:
        return "conifer"
    if "palm" in (tags.get("genus", "") + tags.get("species", "") + tags.get("taxon", "")).lower() or lt == "palm":
        return "palm"
    if "broad" in lt:
        return "broadleaf"
    return "any"


def _ring_ok(r) -> bool:
    return len(r) >= 3 and geom.area(r) > 0.5


def extract_layout(osm: dict, box: BoxSpec, building_mode: str = "centroid", min_building_area: float = 8.0,
                   simplify_tol: float = 0.15, log=print) -> dict:
    """building_mode: "centroid" keeps whole buildings whose centroid is inside
    the box (assets can't be cut); "clip" cuts footprints at the box edge."""
    frame: LocalFrame = box.frame
    rect = box.local_rect()
    big = box.local_rect(margin=max(box.width, box.depth))  # coarse sanity rect

    out = {
        "version": 1, "type": "geobox.layout",
        "box": dict(box.to_dict(), rect=list(rect)),
        "origin": {"lat": box.lat, "lon": box.lon, "elevation": 0.0},
        "buildings": [], "trees": [], "tree_rows": [], "roads": [], "areas": [], "waterways": [],
        "terrain": None,
        "attribution": "© OpenStreetMap contributors (ODbL)",
    }
    stats: Dict[str, int] = {}

    def bump(k):
        stats[k] = stats.get(k, 0) + 1

    for f in iter_features(osm):
        tags = f["tags"]
        fid = f["osm_id"]
        if f["kind"] == "node":
            if tags.get("natural") == "tree":
                x, z = frame.to_local(*f["point"])
                if rect[0] <= x <= rect[2] and rect[1] <= z <= rect[3]:
                    out["trees"].append({"id": fid, "p": [round(x, 3), round(z, 3)], "kind": tree_kind(tags),
                                         "height": parse_length(tags.get("height")), "source": "osm"})
                    bump("trees")
            continue

        if f["kind"] == "line":
            line = frame.project(f["line"])
            if "highway" in tags:
                if tags["highway"] in SKIP_HIGHWAY:
                    continue
                for part in geom.clip_polyline_rect(line, rect):
                    out["roads"].append({
                        "id": fid, "line": part, "kind": tags["highway"], "width": round(road_width(tags), 2),
                        "bridge": tags.get("bridge") not in (None, "no"), "tunnel": tags.get("tunnel") not in (None, "no"),
                        "layer": int(parse_float(tags.get("layer")) or 0), "name": tags.get("name"),
                    })
                    bump("roads")
            elif tags.get("natural") == "tree_row":
                for part in geom.clip_polyline_rect(line, rect):
                    out["tree_rows"].append({"id": fid, "line": part})
                    bump("tree_rows")
            elif "waterway" in tags:
                for part in geom.clip_polyline_rect(line, rect):
                    w = parse_length(tags.get("width")) or {"river": 12.0, "canal": 8.0, "stream": 2.5, "drain": 1.5, "ditch": 1.0}.get(tags["waterway"], 2.0)
                    out["waterways"].append({"id": fid, "line": part, "kind": tags["waterway"], "width": w})
                    bump("waterways")
            continue

        # polygons
        outers = [geom.orient_outer(geom.simplify(frame.project(r), simplify_tol)) for r in f["outers"]]
        inners = [geom.orient_hole(geom.simplify(frame.project(r), simplify_tol)) for r in f["inners"]]
        outers = [r for r in outers if _ring_ok(r)]
        if not outers:
            continue

        if "building" in tags or "building:part" in tags:
            if tags.get("building") in ("no", "demolished", "proposed", "construction_site"):
                continue
            if "building:part" in tags and "building" not in tags:
                # parts duplicate the parent outline; keep them only as extra detail (skipped for scatter)
                bump("building_parts_skipped")
                continue
            h, min_h, levels, src = building_height(tags)
            for oi, outer in enumerate(outers):
                cx, cz = geom.centroid(outer)
                if building_mode == "clip":
                    outer = geom.orient_outer(geom.clip_ring_rect(outer, rect))
                    if not _ring_ok(outer):
                        continue
                elif not (rect[0] <= cx <= rect[2] and rect[1] <= cz <= rect[3]):
                    continue
                a = geom.area(outer)
                if a < min_building_area:
                    bump("buildings_tiny")
                    continue
                holes = [hr for hr in inners if geom.point_in_ring(hr[0][0], hr[0][1], outer)]
                obb = geom.min_area_rect(outer)
                out["buildings"].append({
                    "id": fid if len(outers) == 1 else f"{fid}_{oi}",
                    "outer": outer, "holes": holes,
                    "height": h, "min_height": min_h, "levels": levels, "height_source": src,
                    "kind": tags.get("building") or "yes", "roof": tags.get("roof:shape"),
                    "name": tags.get("name"),
                    "obb": {"center": [round(obb["center"][0], 3), round(obb["center"][1], 3)],
                            "size": [round(obb["size"][0], 3), round(obb["size"][1], 3)],
                            "angle": round(obb["angle"], 5)},
                    "area": round(a, 2), "base_y": 0.0,
                })
                bump("buildings")
            continue

        kind = area_kind(tags)
        if kind is None:
            continue
        for oi, outer in enumerate(outers):
            clipped = geom.orient_outer(geom.clip_ring_rect(outer, rect))
            if not _ring_ok(clipped):
                continue
            holes = []
            for hr in inners:
                if geom.point_in_ring(hr[0][0], hr[0][1], outer):
                    ch = geom.clip_ring_rect(hr, rect)
                    if _ring_ok(ch):
                        holes.append(geom.orient_hole(ch))
            out["areas"].append({"id": fid if len(outers) == 1 else f"{fid}_{oi}", "kind": kind,
                                 "outer": clipped, "holes": holes, "area": round(geom.area(clipped), 2),
                                 "name": tags.get("name")})
            bump("areas_" + kind)

    # Water/landuse first, small things last -> stable draw order in viewers
    order = {k: i for i, k in enumerate(AREA_KINDS[::-1])}
    out["areas"].sort(key=lambda a: (-a["area"], order.get(a["kind"], 0)))
    out["stats"] = stats
    log("[layout] " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    return out
