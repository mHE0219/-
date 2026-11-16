"""Layout + asset library + rules -> placed instances (the scatter step).

Instance record (scene.json "instances"):
  {"id": "bld:w123", "asset": "bld_lowrise_a", "category": "building",
   "p": [x, y, z], "rot_y": degrees, "scale": [sx, sy, sz],
   "rule": "building", "feature": "w123", "source": "procedural"|"manual", "locked": false}

Ids are deterministic (rule + OSM id + index), so re-running the scatter
reproduces the same ids; manual edits are preserved by `merge`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import geom
from .library import asset_size
from .terrain import sampler

DEFAULT_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_rules.json")


def load_rules(path: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    with open(path or DEFAULT_RULES_PATH, "r", encoding="utf-8") as fh:
        rules = json.load(fh)
    if overrides:
        _deep_update(rules, overrides)
    return rules


def _deep_update(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)
        else:
            dst[k] = v


def _rng(seed: int, *keys) -> random.Random:
    h = hashlib.sha1(("%d|" % seed + "|".join(map(str, keys))).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _weighted_choice(rng: random.Random, items: List[Tuple[float, dict]]) -> Optional[dict]:
    tot = sum(w for w, _ in items)
    if tot <= 0:
        return None
    r = rng.uniform(0, tot)
    for w, it in items:
        r -= w
        if r <= 0:
            return it
    return items[-1][1]


class Obstacles:
    """Fast 'is this spot free?' queries against buildings, roads and water."""

    def __init__(self, layout: dict, cell: float = 30.0):
        self.bld = geom.SpatialHash(cell)
        for b in layout["buildings"]:
            self.bld.insert(b, geom.bbox(b["outer"]))
        self.roads = geom.SpatialHash(cell)
        for r in layout["roads"]:
            if r.get("tunnel"):
                continue
            hw = r["width"] / 2.0
            for i in range(len(r["line"]) - 1):
                a, c = r["line"][i], r["line"][i + 1]
                seg = (a, c, hw, r)
                self.roads.insert(seg, (min(a[0], c[0]) - hw, min(a[1], c[1]) - hw, max(a[0], c[0]) + hw, max(a[1], c[1]) + hw))
        for w in layout.get("waterways", []):
            hw = w["width"] / 2.0
            for i in range(len(w["line"]) - 1):
                a, c = w["line"][i], w["line"][i + 1]
                self.roads.insert((a, c, hw, w), (min(a[0], c[0]) - hw, min(a[1], c[1]) - hw, max(a[0], c[0]) + hw, max(a[1], c[1]) + hw))
        self.water = geom.SpatialHash(cell * 4)
        for ar in layout["areas"]:
            if ar["kind"] in ("water", "wetland"):
                self.water.insert(ar, geom.bbox(ar["outer"]))

    def in_building(self, x, z, clearance=0.0) -> bool:
        for b in self.bld.query(x, z, clearance):
            if geom.point_in_ring(x, z, b["outer"]):
                return True
            if clearance > 0 and geom.dist_point_polyline(x, z, b["outer"] + [b["outer"][0]]) < clearance:
                return True
        return False

    def on_road(self, x, z, clearance=0.0, exclude=None) -> bool:
        for a, c, hw, r in self.roads.query(x, z, clearance + 8.0):
            if r is exclude:
                continue
            if geom.dist_point_segment(x, z, a, c) < hw + clearance:
                return True
        return False

    def in_water(self, x, z) -> bool:
        for ar in self.water.query(x, z):
            if geom.point_in_polygon(x, z, ar["outer"], ar.get("holes", ())):
                return True
        return False

    def nearest_road_dir(self, x, z, radius=60.0) -> Optional[Tuple[float, float]]:
        best = None
        for a, c, hw, r in self.roads.query(x, z, radius):
            if "bridge" not in r or r.get("kind") in ("footway", "path", "steps", "cycleway", "corridor", "bridleway"):
                continue  # waterways and foot paths don't define a facade direction
            ax, az, cx_, cz_ = a[0], a[1], c[0], c[1]
            dx, dz = cx_ - ax, cz_ - az
            L2 = dx * dx + dz * dz or 1e-9
            t = max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / L2))
            qx, qz = ax + t * dx, az + t * dz
            d = math.hypot(qx - x, qz - z)
            if d <= radius and (best is None or d < best[0]):
                best = (d, qx - x, qz - z)
        return None if best is None else (best[1], best[2])


class Scatterer:
    def __init__(self, layout: dict, library: dict, rules: dict, log=print):
        self.layout = layout
        self.lib = library
        self.rules = rules
        self.seed = int(rules.get("seed", 1))
        self.log = log
        self.height = sampler(layout.get("terrain"))
        self.obs = Obstacles(layout)
        self.assets = [a for a in library.get("assets", []) if a.get("bounds")]
        self.instances: List[dict] = []
        self.stats: Dict[str, int] = {}

    # -- asset selection -----------------------------------------------------
    def candidates(self, category: str, tags: Sequence[str]) -> List[dict]:
        tags = set(tags)
        return [a for a in self.assets if a.get("category") == category and (not tags or tags & set(a.get("tags", [])))]

    def _emit(self, rule, feature, idx, asset, x, z, rot_y, scale, y_offset=0.0):
        y = self.height(x, z) + y_offset
        inst = {"id": f"{rule}:{feature}:{idx}" if idx is not None else f"{rule}:{feature}",
                "asset": asset["id"], "category": asset.get("category"),
                "p": [round(x, 3), round(y, 3), round(z, 3)], "rot_y": round(rot_y % 360.0, 3),
                "scale": [round(s, 4) for s in scale], "rule": rule, "feature": feature,
                "source": "procedural", "locked": False}
        self.instances.append(inst)
        self.stats[rule] = self.stats.get(rule, 0) + 1
        return inst

    # -- buildings -----------------------------------------------------------
    def buildings(self):
        R = self.rules.get("buildings", {})
        if not R.get("enabled", True):
            return
        kind_tags = R.get("kind_tags", {})
        fallback = R.get("fallback_tags", ["generic"])
        top_n = int(R.get("variety_top_n", 3))
        face_road = R.get("face_road", True)
        for b in self.layout["buildings"]:
            if b["area"] < R.get("min_area", 8.0):
                continue
            rng = _rng(self.seed, "bld", b["id"])
            w, d = b["obb"]["size"]
            h = max(b["height"] - b.get("min_height", 0.0), 2.0)
            tags = kind_tags.get(b["kind"]) or kind_tags.get("*") or fallback
            cands = self.candidates("building", tags) or self.candidates("building", fallback) or self.candidates("building", [])
            if not cands:
                continue
            scored = []
            for a in cands:
                aw, ah, ad = asset_size(a)
                lo, hi = a.get("height_range", [0, 1e9])
                s = abs(math.log(h / ah)) * 1.5 + abs(math.log(w / aw)) + abs(math.log(d / ad))
                if not lo <= h <= hi:
                    s += 3.0 + abs(math.log(h / (lo if h < lo else hi)))
                scored.append((s, a))
            scored.sort(key=lambda t: t[0])
            pool = scored[:top_n]
            best_s = pool[0][0]
            a = _weighted_choice(rng, [(a.get("weight", 1.0) * math.exp(-(s - best_s) * 2.0), a) for s, a in pool])
            aw, ah, ad = asset_size(a)
            fit = a.get("fit", "stretch")
            if fit == "uniform":
                s = min(w / aw, d / ad)
                sc = [s, s, s]
            elif fit == "none":
                sc = [1.0, 1.0, 1.0]
            else:
                sc = [w / aw, h / ah, d / ad]
                if R.get("snap_floors", False) and a.get("modular_floors"):
                    # modular facades: keep whole storeys of the asset's floor height
                    fh = a["floor_height"]
                    floors = max(1, round(h / fh))
                    sc[1] = floors * fh / ah if abs(floors * fh - h) < fh * 0.5 else h / ah
            lo_s, hi_s = a.get("scale_range", [0.25, 4.0])
            clamped = [min(max(v, lo_s), hi_s) for v in sc]
            angle = math.degrees(b["obb"]["angle"])
            cx, cz = b["obb"]["center"]
            if face_road:
                v = self.obs.nearest_road_dir(cx, cz)
                if v is not None:
                    th = math.radians(angle)
                    front = (math.sin(th), math.cos(th))  # local +Z in world (x, z)
                    if front[0] * v[0] + front[1] * v[1] < 0:
                        angle += 180.0
            # sit on the lowest terrain point under the footprint
            ys = [self.height(p[0], p[1]) for p in b["outer"]]
            y_base = (min(ys) if ys else 0.0) - self.height(cx, cz)
            inst = self._emit("bld", b["id"], None, a, cx, cz, angle, clamped, y_offset=y_base + b.get("min_height", 0.0))
            if clamped != sc:
                inst["fit_warning"] = "scale clamped to asset scale_range"
            inst["target"] = {"size": [round(w, 2), round(h, 2), round(d, 2)]}

    # -- trees ----------------------------------------------------------------
    def _tree_asset(self, rng, tags):
        c = self.candidates("tree", tags) or self.candidates("tree", ["any"]) or self.candidates("tree", [])
        return _weighted_choice(rng, [(a.get("weight", 1.0), a) for a in c]) if c else None

    def _tree_scale(self, rng, a, height=None, scale_range=None):
        if height:
            s = height / asset_size(a)[1]
        else:
            lo, hi = scale_range or a.get("scale_range", [0.8, 1.2])
            s = rng.uniform(lo, hi)
        return [s, s * rng.uniform(0.92, 1.08), s]

    def _free(self, x, z, clear_b, clear_r):
        return not (self.obs.in_building(x, z, clear_b) or self.obs.on_road(x, z, clear_r) or self.obs.in_water(x, z))

    def trees(self):
        R = self.rules.get("trees", {})
        if not R.get("enabled", True):
            return
        clr_b = R.get("clearance", {}).get("building", 2.0)
        clr_r = R.get("clearance", {}).get("road", 0.5)
        kind_tags = R.get("kind_tags", {"conifer": ["conifer"], "broadleaf": ["broadleaf"], "palm": ["palm"], "any": ["any"]})

        if R.get("osm_trees", True):
            for t in self.layout["trees"]:
                rng = _rng(self.seed, "tree", t["id"])
                a = self._tree_asset(rng, kind_tags.get(t["kind"], ["any"]))
                if a:
                    self._emit("tree", t["id"], None, a, t["p"][0], t["p"][1], rng.uniform(0, 360),
                               self._tree_scale(rng, a, t.get("height")))

        spacing = R.get("tree_row_spacing", 8.0)
        for row in self.layout["tree_rows"]:
            rng = _rng(self.seed, "row", row["id"])
            a0 = self._tree_asset(rng, ["street", "broadleaf"])
            for i, (x, z, _h) in enumerate(geom.resample_polyline(row["line"], spacing)):
                if a0 and not self.obs.in_building(x, z, 1.0):
                    self._emit("row", row["id"], i, a0, x, z, rng.uniform(0, 360), self._tree_scale(rng, a0))

        areas_r = R.get("areas", {})
        for ar in self.layout["areas"]:
            cfg = areas_r.get(ar["kind"])
            if not cfg or not cfg.get("enabled", True):
                continue
            rng = _rng(self.seed, "area", ar["id"])
            outer, holes = ar["outer"], ar.get("holes", [])
            rect = geom.bbox(outer)
            tags = cfg.get("tags", ["any"])

            def ok(x, z, outer=outer, holes=holes):
                return geom.point_in_polygon(x, z, outer, holes) and self._free(x, z, clr_b, clr_r)

            if cfg.get("grid"):
                g = cfg["grid"]
                pts = []
                xx = rect[0] + g / 2
                while xx < rect[2]:
                    zz = rect[1] + g / 2
                    while zz < rect[3]:
                        if ok(xx, zz):
                            pts.append((xx + rng.uniform(-0.3, 0.3), zz + rng.uniform(-0.3, 0.3)))
                        zz += g
                    xx += g
            else:
                pts = geom.poisson_disk(rect, cfg.get("spacing", 10.0), rng, accept=ok)
            keep = cfg.get("fill", 1.0)
            for i, (x, z) in enumerate(pts):
                if rng.random() > keep:
                    continue
                a = self._tree_asset(rng, tags)
                if a:
                    self._emit("area_" + ar["kind"], ar["id"], i, a, x, z, rng.uniform(0, 360),
                               self._tree_scale(rng, a, scale_range=cfg.get("scale")))

    # -- along roads ------------------------------------------------------------
    def along_roads(self, rule_name: str, category: str, cfg: dict, both_sides: bool):
        if not cfg.get("enabled", False):
            return
        kinds = set(cfg.get("road_kinds", []))
        spacing = cfg.get("spacing", 12.0)
        offset = cfg.get("offset", 1.0)
        min_len = cfg.get("min_length", spacing)
        clr_b = cfg.get("building_clearance", 1.5)
        for r in self.layout["roads"]:
            if r["kind"] not in kinds or r.get("bridge") or r.get("tunnel"):
                continue
            if geom.polyline_length(r["line"]) < min_len:
                continue
            rng = _rng(self.seed, rule_name, r["id"], r["line"][0][0], r["line"][0][1])
            a = _weighted_choice(rng, [(x.get("weight", 1.0), x) for x in self.candidates(category, cfg.get("tags", []))])
            if not a:
                continue
            d = r["width"] / 2.0 + offset
            sides = (1, -1) if both_sides else (1,)
            for side in sides:
                for i, (x, z, hd) in enumerate(geom.resample_polyline(r["line"], spacing, offset=spacing * (0.5 if side > 0 else 1.0))):
                    nx, nz = math.cos(hd), -math.sin(hd)  # right-hand normal of heading (sin h, cos h)
                    px, pz = x + side * d * nx, z + side * d * nz
                    if self.obs.in_building(px, pz, clr_b) or self.obs.on_road(px, pz, 0.2) or self.obs.in_water(px, pz):
                        continue
                    if category == "tree":
                        rot, sc = rng.uniform(0, 360), self._tree_scale(rng, a)
                    else:
                        # face the road: local +Z points from the lamp towards the road centre
                        rot, sc = math.degrees(math.atan2(-side * nx, -side * nz)), [1.0, 1.0, 1.0]
                    self._emit(rule_name, r["id"], f"{side:+d}{i}@{r['line'][0][0]:.0f},{r['line'][0][1]:.0f}", a, px, pz, rot, sc)

    def run(self) -> List[dict]:
        self.buildings()
        self.trees()
        self.along_roads("street_tree", "tree", self.rules.get("street_trees", {}), True)
        self.along_roads("lamp", "prop", self.rules.get("street_lamps", {}), False)
        self.log("[scatter] " + ", ".join(f"{k}={v}" for k, v in sorted(self.stats.items())))
        return self.instances


def merge(procedural: List[dict], previous: Optional[dict]) -> Tuple[List[dict], dict]:
    """Combine fresh procedural instances with a previous scene's edits.

    * instances with source=="manual" or locked==True are kept verbatim
    * procedural ids listed in previous["suppressed"] (user deleted) stay deleted
    """
    if not previous:
        return procedural, {"kept": 0, "suppressed": 0}
    kept = [i for i in previous.get("instances", []) if i.get("source") == "manual" or i.get("locked")]
    kept_ids = {i["id"] for i in kept}
    suppressed = set(previous.get("suppressed", []))
    out = list(kept)
    n_sup = 0
    for i in procedural:
        if i["id"] in kept_ids:
            continue
        if i["id"] in suppressed:
            n_sup += 1
            continue
        out.append(i)
    return out, {"kept": len(kept), "suppressed": n_sup}


def scatter(layout: dict, library: dict, rules: dict, previous: Optional[dict] = None, log=print) -> Tuple[List[dict], dict]:
    s = Scatterer(layout, library, rules, log=log)
    inst = s.run()
    merged, info = merge(inst, previous)
    info["stats"] = s.stats
    return merged, info
