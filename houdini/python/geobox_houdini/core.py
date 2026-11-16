"""Pure-Python part of the Houdini bridge (no `hou` import) so it can be unit
tested outside Houdini and reused by any DCC.

Conventions match docs/conventions.md (metres, +Y up, +X east, -Z north,
orient = (x, y, z, w) quaternion like Houdini's p@orient).
"""
from __future__ import annotations

import copy
import json
import math
import os
import time
from typing import Dict, List, Optional, Tuple


def repo_root() -> Optional[str]:
    """Repo root from $GEOBOX (set by the Houdini package) or this file's location."""
    env = os.environ.get("GEOBOX")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(here, "..", "..", ".."))
    return root if os.path.isdir(os.path.join(root, "geobox")) else None


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, data, indent=1):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


# --- loading -------------------------------------------------------------------
class SceneData:
    def __init__(self, scene_path: str):
        self.scene_path = os.path.abspath(scene_path)
        self.dir = os.path.dirname(self.scene_path)
        self.scene = read_json(self.scene_path)
        lp = os.path.join(self.dir, self.scene.get("layout_file", "layout.json"))
        self.layout = read_json(lp) if os.path.exists(lp) else {}
        self.library_path = self._find_library()
        self.library = read_json(self.library_path) if self.library_path else {"assets": []}
        self.library_root = os.path.dirname(self.library_path) if self.library_path else self.dir
        self.assets = {a["id"]: a for a in self.library.get("assets", [])}

    def _find_library(self) -> Optional[str]:
        cands = []
        if self.scene.get("library"):
            cands.append(os.path.join(self.dir, self.scene["library"]))
        if self.scene.get("library_abs"):
            cands.append(self.scene["library_abs"])
        root = repo_root()
        if root:
            cands.append(os.path.join(root, "assets", "library.json"))
        for c in cands:
            if c and os.path.exists(c):
                return os.path.normpath(c)
        return None

    def resolve(self, rel: Optional[str]) -> Optional[str]:
        if not rel:
            return None
        p = rel if os.path.isabs(rel) else os.path.join(self.library_root, rel)
        return os.path.normpath(p).replace("\\", "/")

    def asset_file(self, a: dict) -> Tuple[Optional[str], bool]:
        """(path, already_normalized) — prefer a Houdini-normalized cache."""
        f = a.get("files") or {}
        if f.get("houdini_normalized"):
            p = self.resolve(f["houdini_normalized"])
            if p and os.path.exists(p):
                return p, True
        for k in ("houdini", "usd", "web"):
            p = self.resolve(f.get(k))
            if p and os.path.exists(p):
                return p, False
        return None, False

    def height_at(self):
        t = self.layout.get("terrain")
        if not t:
            return lambda x, z: 0.0
        nx, nz = t["res"]
        x0, z0, x1, z1 = t["rect"]
        H = t["heights"]

        def f(x, z):
            u = min(max((x - x0) / (x1 - x0) * (nx - 1), 0.0), nx - 1.000001)
            v = min(max((z - z0) / (z1 - z0) * (nz - 1), 0.0), nz - 1.000001)
            i, j = int(u), int(v)
            fu, fv = u - i, v - j
            a, b = H[j * nx + i], H[j * nx + i + 1]
            c, d = H[(j + 1) * nx + i], H[(j + 1) * nx + i + 1]
            return (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv
        return f


# --- math ----------------------------------------------------------------------
def yaw_quat(deg: float) -> Tuple[float, float, float, float]:
    h = math.radians(deg) / 2.0
    return (0.0, math.sin(h), 0.0, math.cos(h))


def quat_rotate(q, v):
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 * cross(q.xyz, v); v' = v + w t + cross(q.xyz, t)
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def yaw_from_quat(q) -> float:
    """Heading (deg) of local +X after rotation, about +Y (tilt is ignored)."""
    vx, _vy, vz = quat_rotate(q, (1.0, 0.0, 0.0))
    return math.degrees(math.atan2(-vz, vx)) % 360.0


def normalize_matrix_rows(a: dict) -> List[List[float]]:
    """4x4 row-vector matrix (Houdini convention p' = p * M): scale -> up-axis -> offset."""
    nz = a.get("normalize") or {}
    s = float(nz.get("scale", 1.0))
    up = nz.get("up", "+Y")
    off = nz.get("offset", [0.0, 0.0, 0.0])
    if up in ("+Z", "Z", "z"):
        r = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]  # row-vector form of (x,y,z)->(x,z,-y)
    elif up == "-Z":
        r = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    else:
        r = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    m = [[r[i][j] * s for j in range(3)] + [0.0] for i in range(3)]
    m.append([off[0], off[1], off[2], 1.0])
    return m


# --- instances <-> point records ----------------------------------------------------
def instance_records(data: SceneData) -> List[dict]:
    recs = []
    for inst in data.scene.get("instances", []):
        a = data.assets.get(inst.get("asset"), {})
        path, normalized = data.asset_file(a) if a else (None, False)
        sc = inst.get("scale", [1.0, 1.0, 1.0])
        recs.append({
            "P": tuple(inst["p"]),
            "orient": yaw_quat(inst.get("rot_y", 0.0)),
            "scale": tuple(sc),
            "pscale": float(sum(sc) / 3.0),
            "name": inst.get("asset", ""),
            "geobox_id": inst["id"],
            "category": inst.get("category") or a.get("category", ""),
            "instancefile": path or "",
            "source": inst.get("source", "procedural"),
            "rule": inst.get("rule", "") or "",
            "locked": 1 if inst.get("locked") else 0,
        })
    return recs


def records_to_scene(records: List[dict], scene: dict, assets: Dict[str, dict]) -> Tuple[dict, dict]:
    """Merge (possibly edited) point records back into scene.json semantics.

    * matching id, transform/asset changed     -> updated + locked
    * id missing from records                   -> deleted (procedural ids go to `suppressed`)
    * duplicate id or empty id                  -> new manual instance
    """
    new = copy.deepcopy(scene)
    old = {i["id"]: i for i in scene.get("instances", [])}
    seen = set()
    out, report = [], {"updated": 0, "added": 0, "deleted": 0, "unchanged": 0}
    for k, r in enumerate(records):
        rid = r.get("geobox_id") or ""
        rot = round(yaw_from_quat(r.get("orient", (0, 0, 0, 1))), 3)
        p = [round(float(v), 3) for v in r["P"]]
        sc = [round(float(v), 4) for v in r.get("scale", (1, 1, 1))]
        asset = r.get("name") or (old.get(rid, {}).get("asset"))
        if rid and rid in old and rid not in seen:
            seen.add(rid)
            inst = copy.deepcopy(old[rid])
            changed = (any(abs(a - b) > 1e-3 for a, b in zip(inst["p"], p)) or
                       abs(((inst.get("rot_y", 0.0) - rot + 180) % 360) - 180) > 0.05 or
                       any(abs(a - b) > 1e-3 for a, b in zip(inst.get("scale", [1, 1, 1]), sc)) or
                       inst.get("asset") != asset or bool(inst.get("locked")) != bool(r.get("locked")))
            if changed:
                inst.update({"p": p, "rot_y": rot, "scale": sc, "asset": asset,
                             "locked": True if inst.get("source") != "manual" or r.get("locked") else bool(r.get("locked"))})
                if asset in assets:
                    inst["category"] = assets[asset].get("category", inst.get("category"))
                report["updated"] += 1
            else:
                report["unchanged"] += 1
            out.append(inst)
        else:
            nid = "manual:hou%x%d" % (int(time.time()), k)
            out.append({"id": nid, "asset": asset, "category": assets.get(asset, {}).get("category", r.get("category", "prop")),
                        "p": p, "rot_y": rot, "scale": sc, "rule": "manual", "feature": None,
                        "source": "manual", "locked": True})
            report["added"] += 1
    sup = set(new.get("suppressed", []))
    for oid, inst in old.items():
        if oid not in seen:
            report["deleted"] += 1
            if inst.get("source") != "manual":
                sup.add(oid)
    new["instances"] = out
    new["suppressed"] = sorted(sup)
    return new, report


def save_scene(data: SceneData, scene: dict) -> List[str]:
    """Write scene.json; re-export scene.usda through the geobox package when importable."""
    write_json(data.scene_path, scene)
    written = [data.scene_path]
    root = repo_root()
    if root:
        import sys
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from geobox.export_usd import export_usda
            from geobox import library as libmod
            lib = libmod.load(data.library_path)
            usd = os.path.join(data.dir, "scene.usda")
            export_usda(scene, data.layout, lib, usd)
            written.append(usd)
        except Exception as exc:  # noqa: BLE001
            written.append("(usd export skipped: %s)" % exc)
    return written


# --- greybox ------------------------------------------------------------------------
# faces wound clockwise seen from outside (Houdini front faces) for the vertex order
# of block_box_vertices (bottom ring 0-3 CCW from above, top ring 4-7)
BOX_FACES_HOUDINI = [[0, 1, 2, 3], [7, 6, 5, 4], [4, 5, 1, 0], [5, 6, 2, 1], [6, 7, 3, 2], [7, 4, 0, 3]]


def block_box_vertices(b: dict) -> List[Tuple[float, float, float]]:
    cx, cz = b["center"]
    w, h, d = b["size"]
    th = math.radians(b.get("rot_y", 0.0))
    ux, uz = math.cos(th), -math.sin(th)
    vx, vz = math.sin(th), math.cos(th)
    ring = []
    # CCW seen from above (east right, north up): (-,+) (+,+) (+,-) (-,-) in local (u, v=+Z south)
    for su, sv in ((-1, 1), (1, 1), (1, -1), (-1, -1)):
        ring.append((cx + su * w / 2 * ux + sv * d / 2 * vx, cz + su * w / 2 * uz + sv * d / 2 * vz))
    y0 = b.get("base_y", 0.0)
    return [(x, y0, z) for x, z in ring] + [(x, y0 + h, z) for x, z in ring]


def _min_area_rect(points_xz):
    """Oriented rectangle (centre, (w, d), angle rad) — rotating calipers on the hull."""
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points_xz))
    if len(pts) < 3:
        xs = [p[0] for p in pts] or [0.0]
        zs = [p[1] for p in pts] or [0.0]
        return ((min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2), (max(max(xs) - min(xs), 0.01), max(max(zs) - min(zs), 0.01)), 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    best = None
    for i in range(len(hull)):
        ax, az = hull[i]
        bx, bz = hull[(i + 1) % len(hull)]
        L = math.hypot(bx - ax, bz - az)
        if L < 1e-9:
            continue
        ux, uz = (bx - ax) / L, (bz - az) / L
        vx, vz = -uz, ux
        us = [p[0] * ux + p[1] * uz for p in hull]
        vs = [p[0] * vx + p[1] * vz for p in hull]
        area = (max(us) - min(us)) * (max(vs) - min(vs))
        if best is None or area < best[0] - 1e-9:
            best = (area, ux, uz, vx, vz, min(us), max(us), min(vs), max(vs))
    _a, ux, uz, vx, vz, u0, u1, v0, v1 = best
    cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
    return (cu * ux + cv * vx, cu * uz + cv * vz), (u1 - u0, v1 - v0), (ux, uz)


def block_from_points(points, attrs: dict, previous: Optional[dict] = None) -> dict:
    """Recover a block from its (possibly edited) box points. Keeps the previous
    rotation's axis assignment when the box was only moved/resized, so W/D don't swap."""
    xs = [p[1] for p in points]
    y0, y1 = min(xs), max(xs)
    (cx, cz), (a, c), axis = _min_area_rect([(p[0], p[2]) for p in points])
    if axis == 0.0:
        axis = (1.0, 0.0)
    rot_axis = math.degrees(math.atan2(-axis[1], axis[0]))  # rotation mapping local +X onto `axis`
    cand = []
    for k in range(4):  # the rectangle is symmetric: 4 possible (rot, w, d) labelings
        r = (rot_axis + 90.0 * k) % 360.0
        w, d = (a, c) if k % 2 == 0 else (c, a)
        cand.append((r, w, d))
    prev_rot = (previous or {}).get("rot_y", attrs.get("rot_y", 0.0)) or 0.0
    r, w, d = min(cand, key=lambda t: abs(((t[0] - prev_rot + 180.0) % 360.0) - 180.0))
    b = dict(previous or {})
    b.update({"center": [round(cx, 3), round(cz, 3)], "base_y": round(y0, 3),
              "size": [round(w, 3), round(y1 - y0, 3), round(d, 3)], "rot_y": round(r % 360.0, 3)})
    for k in ("kind", "source", "feature"):
        if attrs.get(k):
            b[k] = attrs[k]
    if "tags" in attrs:
        b["tags"] = [t.strip() for t in str(attrs["tags"]).split(",") if t.strip()] or b.get("tags", [])
    if "asset_lock" in attrs:
        b["asset_lock"] = attrs["asset_lock"] or None
    return b


def _block_changed(a: dict, b: dict) -> bool:
    if any(abs(x - y) > 1e-2 for x, y in zip(a["center"], b["center"])):
        return True
    if any(abs(x - y) > 1e-2 for x, y in zip(a["size"], b["size"])) or abs(a.get("base_y", 0) - b.get("base_y", 0)) > 1e-2:
        return True
    if abs(((a.get("rot_y", 0) - b.get("rot_y", 0) + 180.0) % 360.0) - 180.0) > 0.05:
        return True
    return any((a.get(k) or None) != (b.get(k) or None) for k in ("kind", "asset_lock")) or a.get("tags") != b.get("tags")


def pieces_to_greybox(pieces: List[dict], scene: dict) -> Tuple[dict, dict]:
    """Merge edited greybox boxes back into scene["greybox"].

    pieces: [{"id": block id or "", "points": [(x,y,z)...], "attrs": {...}}]
    * matching id, geometry/attrs changed -> updated + locked (its non-locked building instance
      is dropped so the next art pass regenerates it)
    * id missing                           -> deleted (OSM ids go to suppressed_blocks)
    * no id / duplicate id                 -> new manual block
    """
    new = copy.deepcopy(scene)
    old = {b["id"]: b for b in scene.get("greybox", [])}
    seen, out = set(), []
    rep = {"updated": 0, "added": 0, "deleted": 0, "unchanged": 0}
    changed_ids = set()
    for k, pc in enumerate(pieces):
        bid = pc.get("id") or ""
        if bid and bid in old and bid not in seen:
            seen.add(bid)
            b = block_from_points(pc["points"], pc.get("attrs", {}), old[bid])
            if _block_changed(old[bid], b):
                b["locked"] = True
                rep["updated"] += 1
                changed_ids.add(bid)
            else:
                b = copy.deepcopy(old[bid])
                rep["unchanged"] += 1
            out.append(b)
        else:
            b = block_from_points(pc["points"], pc.get("attrs", {}), None)
            b.update({"id": "gb:manual:hou%x%d" % (int(time.time()), k), "source": "manual", "feature": None,
                      "locked": True})
            b.setdefault("kind", "yes")
            b.setdefault("tags", ["generic"])
            b.setdefault("asset_lock", None)
            out.append(b)
            rep["added"] += 1
    sup = set(new.get("suppressed_blocks", []))
    for oid, b in old.items():
        if oid not in seen:
            rep["deleted"] += 1
            changed_ids.add(oid)
            if b.get("source") != "manual":
                sup.add(oid)
    new["greybox"] = out
    new["suppressed_blocks"] = sorted(sup)
    # drop stale (non-locked) art so the next art pass rebuilds it from the edited blocks
    new["instances"] = [i for i in new.get("instances", []) if not (i.get("block") in changed_ids and not i.get("locked"))]
    return new, rep
