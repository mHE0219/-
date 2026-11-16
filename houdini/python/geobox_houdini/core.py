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
