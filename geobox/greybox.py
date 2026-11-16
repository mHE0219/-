"""Greybox (灰盒 blockout): the editable layer between the map and the art pass.

    OSM layout --blocks_from_layout--> scene["greybox"]  (edit in WebGL / Houdini)
                                            |
                         art pass (scatter.buildings + retrieval)
                                            v
                                  building asset instances

Block schema (docs/conventions.md):
  {"id": "gb:w123", "kind": "apartments", "center": [x, z], "base_y": 0.0,
   "size": [w, h, d], "rot_y": 12.5, "tags": ["apartments"], "source": "osm"|"manual",
   "feature": "w123", "locked": false, "asset_lock": null,
   "dress": "auto"|"kit"|"whole"|"none", "kit_set": null, "floor_height": null}   (optional, pipeline v2)

* pivot = base centre; size = (width along local X, height, depth along local Z)
* rot_y in degrees about +Y; local +Z is the front (OSM blocks face the nearest road)
* editing an OSM block sets locked=true; deleting one adds its id to
  scene["suppressed_blocks"]; locked/manual blocks survive a rebuild.
"""
from __future__ import annotations

import math
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import geom

DEFAULT_MIN_AREA = 8.0


def _tags_for(kind: str, rules: Optional[dict]) -> List[str]:
    R = (rules or {}).get("buildings", {})
    kt = R.get("kind_tags", {})
    return list(kt.get(kind) or kt.get("*") or R.get("fallback_tags") or ["generic"])


def block_corners(b: dict) -> List[List[float]]:
    """Footprint rectangle [x, z] (CCW seen from above)."""
    cx, cz = b["center"]
    w, _h, d = b["size"]
    th = math.radians(b.get("rot_y", 0.0))
    ux, uz = math.cos(th), -math.sin(th)  # local +X in world (x, z)
    vx, vz = math.sin(th), math.cos(th)   # local +Z in world (x, z)
    pts = []
    for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        pts.append([cx + su * w / 2 * ux + sv * d / 2 * vx, cz + su * w / 2 * uz + sv * d / 2 * vz])
    return geom.orient_outer(pts)


def blocks_from_layout(layout: dict, height_at: Optional[Callable[[float, float], float]] = None,
                       rules: Optional[dict] = None, face_road: Optional[bool] = None) -> List[dict]:
    """One block per OSM building footprint (oriented bounding box + height)."""
    from .scatter import Obstacles  # local import: scatter imports this module too
    from .terrain import sampler

    h_at = height_at or sampler(layout.get("terrain"))
    R = (rules or {}).get("buildings", {})
    min_area = R.get("min_area", DEFAULT_MIN_AREA)
    if face_road is None:
        face_road = R.get("face_road", True)
    obs = Obstacles(layout) if face_road else None
    out = []
    for b in layout.get("buildings", []):
        if b.get("area", 0.0) < min_area:
            continue
        w, d = b["obb"]["size"]
        h = max(b["height"] - b.get("min_height", 0.0), 2.0)
        cx, cz = b["obb"]["center"]
        rot = math.degrees(b["obb"]["angle"])
        if obs is not None:
            v = obs.nearest_road_dir(cx, cz)
            if v is not None:
                th = math.radians(rot)
                if math.sin(th) * v[0] + math.cos(th) * v[1] < 0:  # local +Z faces away from the road
                    rot += 180.0
        ys = [h_at(p[0], p[1]) for p in b["outer"]] or [0.0]
        out.append({
            "id": "gb:" + b["id"], "kind": b.get("kind") or "yes",
            "center": [round(cx, 3), round(cz, 3)],
            "base_y": round(min(ys) + b.get("min_height", 0.0), 3),
            "size": [round(w, 3), round(h, 3), round(d, 3)],
            "rot_y": round(rot % 360.0, 3),
            "tags": _tags_for(b.get("kind") or "yes", rules),
            "source": "osm", "feature": b["id"], "locked": False, "asset_lock": None,
        })
    return out


def merge_blocks(new: List[dict], previous: Optional[dict]) -> Tuple[List[dict], dict]:
    """Fresh OSM blocks + user edits of the previous scene.

    * blocks with source=="manual" or locked==True are kept verbatim (and win over the fresh one)
    * ids in previous["suppressed_blocks"] stay deleted
    """
    if not previous or "greybox" not in previous:
        return list(new), {"kept": 0, "suppressed": 0}
    kept = [b for b in previous.get("greybox", []) if b.get("source") == "manual" or b.get("locked")]
    kept_ids = {b["id"] for b in kept}
    sup = set(previous.get("suppressed_blocks", []))
    out, n_sup = list(kept), 0
    for b in new:
        if b["id"] in kept_ids:
            continue
        if b["id"] in sup:
            n_sup += 1
            continue
        out.append(b)
    return out, {"kept": len(kept), "suppressed": n_sup}


def footprints(blocks: Sequence[dict], layout: Optional[dict] = None) -> List[dict]:
    """Obstacle polygons for the vegetation/prop passes: the exact OSM outline
    for untouched OSM blocks, the block rectangle for edited/manual ones."""
    by_feature = {b["id"]: b for b in (layout or {}).get("buildings", [])}
    out = []
    for b in blocks:
        f = by_feature.get(b.get("feature")) if b.get("source") == "osm" and not b.get("locked") else None
        out.append({"id": b["id"], "outer": f["outer"] if f else block_corners(b)})
    return out


# --- export -------------------------------------------------------------------------
def box_vertices(b: dict) -> List[Tuple[float, float, float]]:
    """8 corners: bottom (0-3) then top (4-7), same order as block_corners."""
    y0 = b.get("base_y", 0.0)
    y1 = y0 + b["size"][1]
    ring = block_corners(b)
    return [(p[0], y0, p[1]) for p in ring] + [(p[0], y1, p[1]) for p in ring]


# faces CCW from outside for a CCW (from above) ring
BOX_FACES = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]


def to_obj(blocks: Sequence[dict], path: str) -> str:
    """Greybox as OBJ (one object per block) — for game-engine blockout import."""
    lines = ["# GeoBox greybox: metres, +Y up, +X east, -Z north"]
    n = 0
    for b in blocks:
        lines.append("o " + b["id"].replace(":", "_"))
        for v in box_vertices(b):
            lines.append("v %.3f %.3f %.3f" % v)
        for f in BOX_FACES:
            lines.append("f " + " ".join(str(n + i + 1) for i in f))
        n += 8
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# --- designer actions: seed from OSM / clear -----------------------------------------------
def seed_from_osm(name: str, root: Optional[str] = None, replace: bool = False) -> dict:
    """Add OSM footprint blocks to the project's greybox as an editable draft.
    Existing manual/edited blocks are kept; replace=True also forgets deletions."""
    from . import project
    from .terrain import sampler
    d = project.project_dir(name, root)
    scene = project.read_json(os.path.join(d, "scene.json"))
    layout = project.read_json(os.path.join(d, "layout.json"))
    if replace:
        scene["suppressed_blocks"] = []
    fresh = blocks_from_layout(layout, sampler(layout.get("terrain")), scene.get("rules"))
    have = {b["id"] for b in scene.get("greybox", [])}
    sup = set(scene.get("suppressed_blocks", []))
    added = [b for b in fresh if b["id"] not in have and b["id"] not in sup]
    scene["greybox"] = scene.get("greybox", []) + added
    scene["greybox_source"] = "osm"
    project.save_scene(name, scene, layout=layout, root=root)
    return {"added": len(added), "blocks": len(scene["greybox"])}


def clear(name: str, root: Optional[str] = None, keep_manual: bool = True) -> dict:
    """Designer mode: drop OSM draft blocks (and their procedural art); the map stays as reference."""
    from . import project
    d = project.project_dir(name, root)
    scene = project.read_json(os.path.join(d, "scene.json"))
    keep = [b for b in scene.get("greybox", []) if keep_manual and b.get("source") == "manual"]
    gone = {b["id"] for b in scene.get("greybox", [])} - {b["id"] for b in keep}
    scene["greybox"] = keep
    scene["greybox_source"] = "empty"
    scene["suppressed_blocks"] = []
    scene["instances"] = [i for i in scene.get("instances", [])
                          if not (i.get("block") in gone and i.get("source") != "manual")]
    project.save_scene(name, scene, root=root)
    return {"removed": len(gone), "blocks": len(keep)}


# --- server + CLI extension points -------------------------------------------------------
def _route_obj(req):
    from . import project
    name = project.safe_name(req.match.group(1))
    d = project.project_dir(name)
    scene = project.read_json(os.path.join(d, "scene.json"))
    os.makedirs(os.path.join(d, "exports"), exist_ok=True)
    path = to_obj(scene.get("greybox", []), os.path.join(d, "exports", "greybox.obj"))
    with open(path, "rb") as fh:
        return 200, fh.read(), "text/plain; charset=utf-8"


def _route_seed(req):
    from . import project
    body = req.json() if req.body() else {}
    return {"ok": True, **seed_from_osm(project.safe_name(req.match.group(1)), replace=bool(body.get("replace")))}


def _route_clear(req):
    from . import project
    body = req.json() if req.body() else {}
    return {"ok": True, **clear(project.safe_name(req.match.group(1)), keep_manual=body.get("keep_manual", True))}


ROUTES = [("GET", r"^/api/projects/([^/]+)/greybox\.obj$", _route_obj),
          ("POST", r"^/api/projects/([^/]+)/greybox/seed$", _route_seed),
          ("POST", r"^/api/projects/([^/]+)/greybox/clear$", _route_clear)]


def add_cli(sub):
    p = sub.add_parser("greybox", help="greybox (blockout) tools: export OBJ, reset edits")
    p.add_argument("name", help="project name")
    p.add_argument("--obj", help="write the greybox as OBJ to this path")
    p.add_argument("--reset-edits", action="store_true", help="drop manual/locked blocks and un-delete OSM blocks, then rescatter")
    p.add_argument("--seed-osm", action="store_true", help="add OSM footprints as draft blocks (keeps your blocks)")
    p.add_argument("--clear", action="store_true", help="remove OSM draft blocks (designer mode; keeps manual blocks)")
    p.set_defaults(func=_cli)


def _cli(args):
    from . import project
    d = project.project_dir(args.name)
    if args.clear:
        print("[greybox] cleared:", clear(args.name))
    if args.seed_osm:
        print("[greybox] seeded:", seed_from_osm(args.name))
    scene = project.read_json(os.path.join(d, "scene.json"))
    if args.reset_edits:
        scene["greybox"] = [b for b in scene.get("greybox", []) if b.get("source") == "osm" and not b.get("locked")]
        scene["suppressed_blocks"] = []
        project.save_scene(args.name, scene, export_usd=False)
        scene = project.rescatter(args.name)
        print("[greybox] edits reset, %d blocks" % len(scene.get("greybox", [])))
    if args.obj:
        print("[greybox] wrote", to_obj(scene.get("greybox", []), args.obj))
    if not (args.obj or args.reset_edits or args.seed_osm or args.clear):
        bl = scene.get("greybox", [])
        n_edit = sum(1 for b in bl if b.get("locked") or b.get("source") == "manual")
        print("[greybox] %d blocks (%d edited/manual), %d deleted" % (len(bl), n_edit, len(scene.get("suppressed_blocks", []))))
    return 0
