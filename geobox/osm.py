"""OpenStreetMap (Overpass API) fetch + parsing.

Why OSM and not Google? Google Maps Platform terms do not allow extracting
building/road data out of Google Maps for use in other tools. OSM data is
ODbL-licensed (attribution: "© OpenStreetMap contributors") and contains the
*semantic* layout we need for scattering: footprints, levels/heights, trees,
roads, parks, water. Google Photorealistic 3D Tiles can still be shown as a
visual reference layer in the web viewer (streamed with your own API key).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "geobox-pipeline/0.1 (+https://wiki.openstreetmap.org/wiki/Overpass_API)"

LatLon = Tuple[float, float]


def build_query(bbox: Sequence[float], timeout: int = 90) -> str:
    s, w, n, e = bbox
    b = "%.7f,%.7f,%.7f,%.7f" % (s, w, n, e)
    return f"""[out:json][timeout:{timeout}];
(
  way["building"]({b});
  relation["building"]["type"="multipolygon"]({b});
  way["building:part"]({b});
  way["highway"]({b});
  node["natural"="tree"]({b});
  way["natural"="tree_row"]({b});
  way["landuse"]({b});
  relation["landuse"]["type"="multipolygon"]({b});
  way["leisure"]({b});
  relation["leisure"]["type"="multipolygon"]({b});
  way["natural"~"^(wood|water|scrub|grassland|heath|wetland|beach|sand|bare_rock)$"]({b});
  relation["natural"~"^(wood|water|scrub|grassland|wetland)$"]({b});
  way["waterway"]({b});
  way["amenity"~"^(parking|school|university)$"]({b});
  way["man_made"~"^(bridge|pier)$"]({b});
);
out geom;"""


def fetch_overpass(bbox: Sequence[float], endpoints: Optional[List[str]] = None, timeout: int = 120,
                   retries: int = 2, log=print) -> dict:
    query = build_query(bbox)
    data = urllib.parse.urlencode({"data": query}).encode()
    last_err: Optional[Exception] = None
    for ep in endpoints or OVERPASS_ENDPOINTS:
        for attempt in range(retries + 1):
            try:
                log(f"[osm] POST {ep} (attempt {attempt + 1})")
                req = urllib.request.Request(ep, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                n = len(payload.get("elements", []))
                log(f"[osm] {n} elements")
                return payload
            except Exception as exc:  # noqa: BLE001 - network errors vary
                last_err = exc
                log(f"[osm] failed: {exc}")
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


# --- multipolygon ring assembly ---------------------------------------------
def _key(p: LatLon) -> Tuple[int, int]:
    return (round(p[0] * 1e7), round(p[1] * 1e7))


def assemble_rings(segments: Iterable[List[LatLon]]) -> List[List[LatLon]]:
    """Join open way segments end-to-end into closed rings."""
    rings: List[List[LatLon]] = []
    open_segs: List[List[LatLon]] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        if _key(seg[0]) == _key(seg[-1]) and len(seg) >= 4:
            rings.append(seg)
        else:
            open_segs.append(list(seg))
    while open_segs:
        cur = open_segs.pop()
        changed = True
        while changed and _key(cur[0]) != _key(cur[-1]):
            changed = False
            for i, s in enumerate(open_segs):
                if _key(s[0]) == _key(cur[-1]):
                    cur = cur + s[1:]
                elif _key(s[-1]) == _key(cur[-1]):
                    cur = cur + s[::-1][1:]
                elif _key(s[-1]) == _key(cur[0]):
                    cur = s + cur[1:]
                elif _key(s[0]) == _key(cur[0]):
                    cur = s[::-1] + cur[1:]
                else:
                    continue
                open_segs.pop(i)
                changed = True
                break
        if _key(cur[0]) == _key(cur[-1]) and len(cur) >= 4:
            rings.append(cur)
        # else: ring is incomplete (member outside the query bbox) -> dropped
    return rings


def _geom(el: dict) -> List[LatLon]:
    return [(g["lat"], g["lon"]) for g in el.get("geometry") or [] if g]


def iter_features(osm: dict):
    """Yield normalised features:
        {"osm_id": "w123", "kind": "node"|"line"|"polygon", "tags": {...},
         "point": (lat, lon) | "line": [...] | "outers": [[...]], "inners": [[...]]}
    """
    for el in osm.get("elements", []):
        t = el.get("type")
        tags = el.get("tags") or {}
        if t == "node":
            yield {"osm_id": f"n{el['id']}", "kind": "node", "tags": tags, "point": (el["lat"], el["lon"])}
        elif t == "way":
            g = _geom(el)
            if len(g) < 2:
                continue
            closed = len(g) >= 4 and _key(g[0]) == _key(g[-1])
            if closed and _is_area(tags):
                yield {"osm_id": f"w{el['id']}", "kind": "polygon", "tags": tags, "outers": [g], "inners": []}
            else:
                yield {"osm_id": f"w{el['id']}", "kind": "line", "tags": tags, "line": g}
        elif t == "relation":
            outer_segs, inner_segs = [], []
            for m in el.get("members", []):
                if m.get("type") != "way":
                    continue
                g = _geom(m)
                (inner_segs if m.get("role") == "inner" else outer_segs).append(g)
            outers = assemble_rings(outer_segs)
            if not outers:
                continue
            yield {"osm_id": f"r{el['id']}", "kind": "polygon", "tags": tags, "outers": outers,
                   "inners": assemble_rings(inner_segs)}


def _is_area(tags: Dict[str, str]) -> bool:
    if tags.get("area") == "no":
        return False
    if tags.get("area") == "yes":
        return True
    if "highway" in tags or "barrier" in tags or tags.get("natural") == "tree_row" or "waterway" in tags:
        return tags.get("waterway") == "riverbank"
    return True
