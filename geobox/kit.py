"""Modular kit dressing (模組化構件拼裝) — docs/pipeline_v2.md.

A greybox block (oriented box: centre, base_y, size [w, h, d], rot_y) is dressed
with kit pieces instead of one stretched whole-building asset:

          parapet row ───────────────┐   (role parapet, on the roof edge)
          top floor     │ W │ W │ W │   column pattern decided once per column,
          middle floors │ W │ - │ W │   so windows line up floor to floor
          ground floor  │ S │ D │ S │   S = shopfront/arcade, D = door (front face, centre)
          corner pieces at the 4 vertical edges (one stretched column per edge)
          roof slab (role roof) + a few roof props (water tanks, rooftop sheds)

Faces (block local frame, local +Z = front = faces the road):
    0 front (+Z)   1 right (+X)   2 back (-Z)   3 left (-X)
Each face holds n = max(1, round(L / module_w)) slots; pieces are stretched by
sx = L / (n * module_w) so they exactly fill the face. Floors:
n_f = max(1, round(H_usable / floor_h)) with H_usable = H - parapet height;
pieces are stretched by sy = (H_usable / n_f) / module_h.

Instance ids are deterministic so the edit-preserving merge works:
    kit:<block id>:<face>:<floor>:<col>
    kit:<block id>:c<k>:0:0        corner k
    kit:<block id>:<face>:p:<col>  parapet
    kit:<block id>:r:0:0           roof,  kit:<block id>:rp:0:<i>  roof props
"""
from __future__ import annotations

import hashlib
import math
import random
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FACADE_ROLES = ("wall", "window", "door", "shopfront")
BANDS = ("ground", "middle", "top")
DEFAULTS = {
    "window_ratio": 0.8,        # front/back columns that get windows
    "side_window_ratio": 0.5,   # side faces (often party walls in shophouse rows)
    "party_wall_below": 7.0,    # side faces shorter than this are blank walls
    "roof_props_per_100m2": 0.6,
    "max_roof_props": 3,
}


def _rng(*keys) -> random.Random:
    h = hashlib.sha1("|".join(map(str, keys)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


# --- kit sets ----------------------------------------------------------------------
def kit_sets(lib: dict) -> Dict[str, dict]:
    """Group library kit pieces by kit.set:
    {set: {"name", "roles": {role: [asset]}, "module": [w, h], "depth", "tags": set,
           "height_range": [lo, hi] | None, "label"}}"""
    sets: Dict[str, dict] = {}
    for a in lib.get("assets", []):
        k = a.get("kit") or {}
        if a.get("category") != "kit" or not k.get("set") or not k.get("role"):
            continue
        if a.get("weight", 1.0) <= 0 or k.get("weight", 1.0) <= 0:
            continue
        s = sets.setdefault(k["set"], {"name": k["set"], "roles": {}, "module": None, "depth": 0.0,
                                       "tags": set(), "height_range": None})
        s["roles"].setdefault(k["role"], []).append(a)
        if k["role"] in ("wall", "window") and s["module"] is None and k.get("module"):
            s["module"] = [float(v) for v in k["module"]]
        s["depth"] = max(s["depth"], float(k.get("depth", 0.3)))
        s["tags"] |= {t.lower() for t in a.get("tags", []) if t not in (k["role"],)}
        if k.get("height_range"):
            s["height_range"] = list(k["height_range"])
    for s in sets.values():
        if s["module"] is None:
            any_facade = next((a for r in FACADE_ROLES for a in s["roles"].get(r, [])), None)
            s["module"] = [float(v) for v in ((any_facade or {}).get("kit", {}).get("module") or [3.0, 3.2])]
    return sets


def usable(s: dict) -> bool:
    return bool(s["roles"].get("wall") or s["roles"].get("window"))


def choose_set(block: dict, sets: Dict[str, dict], strict: bool = True) -> Optional[dict]:
    """block.kit_set if given; otherwise the set whose tags overlap the block's
    kind/tags and whose height_range contains the block height (strict), or the
    closest one (strict=False, used when dress == "kit" is forced)."""
    if block.get("kit_set") and block["kit_set"] in sets and usable(sets[block["kit_set"]]):
        return sets[block["kit_set"]]
    h = float(block["size"][1])
    tags = {str(t).lower() for t in (block.get("tags") or [])} | {str(block.get("kind") or "").lower()}
    best, best_score = None, -1e9
    for name in sorted(sets):
        s = sets[name]
        if not usable(s):
            continue
        overlap = len(tags & s["tags"])
        lo, hi = s["height_range"] or (0.0, 1e9)
        inside = lo <= h <= hi
        if strict and (overlap == 0 or not inside):
            continue
        dist = 0.0 if inside else abs(math.log(max(h, 0.1) / (lo if h < lo else hi)))
        mid = math.sqrt(max(lo, 1.0) * min(hi, 1000.0))
        score = overlap * 2.0 - dist * 3.0 - 0.2 * abs(math.log(max(h, 0.1) / mid))
        if score > best_score:
            best, best_score = s, score
    return best


# --- helpers -------------------------------------------------------------------------
def _module(a: dict) -> Tuple[float, float]:
    m = (a.get("kit") or {}).get("module")
    if m:
        return float(m[0]), float(m[1])
    b = a["bounds"]
    return b["max"][0] - b["min"][0], b["max"][1] - b["min"][1]


def _bands_ok(a: dict, band: str) -> bool:
    fl = (a.get("kit") or {}).get("floors") or ["any"]
    return "any" in fl or band in fl


def _pick(rng: random.Random, pieces: Sequence[dict], band: str, block: dict) -> Optional[dict]:
    d = float(block["size"][2])
    cands = [a for a in pieces if _bands_ok(a, band) and d >= (a.get("kit") or {}).get("min_depth", 0.0)]
    if not cands:
        return None
    tot = sum((a.get("kit") or {}).get("weight", 1.0) * a.get("weight", 1.0) for a in cands)
    r = rng.uniform(0, tot)
    for a in cands:
        r -= (a.get("kit") or {}).get("weight", 1.0) * a.get("weight", 1.0)
        if r <= 0:
            return a
    return cands[-1]


def faces(block: dict):
    """[(index, phi_deg, start(lx, lz), tangent(tx, tz), length)] in block-local space."""
    w, _h, d = (float(v) for v in block["size"])
    return [
        (0, 0.0, (-w / 2, d / 2), (1.0, 0.0), w),     # front +Z
        (1, 90.0, (w / 2, d / 2), (0.0, -1.0), d),    # right +X
        (2, 180.0, (w / 2, -d / 2), (-1.0, 0.0), w),  # back  -Z
        (3, 270.0, (-w / 2, -d / 2), (0.0, 1.0), d),  # left  -X
    ]


class _Frame:
    def __init__(self, block: dict):
        self.cx, self.cz = (float(v) for v in block["center"])
        self.rot = float(block.get("rot_y", 0.0))
        th = math.radians(self.rot)
        self.ux, self.uz = math.cos(th), -math.sin(th)   # local +X in world
        self.vx, self.vz = math.sin(th), math.cos(th)    # local +Z in world

    def world(self, lx: float, lz: float) -> Tuple[float, float]:
        return (self.cx + lx * self.ux + lz * self.vx, self.cz + lx * self.uz + lz * self.vz)


def _inst(bid: str, key: str, a: dict, x: float, y: float, z: float, rot: float, scale) -> dict:
    return {"id": f"kit:{bid}:{key}", "asset": a["id"], "category": "kit",
            "p": [round(x, 3), round(y, 3), round(z, 3)], "rot_y": round(rot % 360.0, 3),
            "scale": [round(float(s), 4) for s in scale], "rule": "kit",
            "source": "procedural", "locked": False, "block": bid}


# --- dressing ------------------------------------------------------------------------
def dress_block(block: dict, kset: dict, seed: int = 1, options: Optional[dict] = None) -> List[dict]:
    """All kit instances for one block (see module docstring)."""
    opt = dict(DEFAULTS, **(options or {}))
    roles = kset["roles"]
    bid = block["id"]
    w, H, d = (max(float(v), 0.1) for v in block["size"])
    base = float(block.get("base_y", 0.0))
    fr = _Frame(block)
    mw, mh = kset["module"]
    fh_target = float(block.get("floor_height") or mh)

    parapet = roles.get("parapet") or []
    ph = _module(parapet[0])[1] if parapet else 0.0
    if parapet and H - ph < fh_target * 0.8:
        parapet, ph = [], 0.0
    hu = max(H - ph, 0.5)
    nf = max(1, int(round(hu / fh_target)))
    fh = hu / nf
    out: List[dict] = []

    def band_of(f):
        if f == 0:
            return "ground"
        return "top" if f == nf - 1 else "middle"

    walls, windows = roles.get("wall", []), roles.get("window", [])
    for fi, phi, (sx0, sz0), (tx, tz), L in faces(block):
        n = max(1, int(round(L / mw)))
        slot = L / n
        rot = fr.rot + phi
        crng = _rng(seed, bid, fi, "cols")
        side = fi in (1, 3)
        ratio = opt["side_window_ratio"] if side else opt["window_ratio"]
        if side and L < opt["party_wall_below"]:
            ratio = 0.0
        column_window = [crng.random() < ratio for _ in range(n)]
        column_rng_seed = [crng.random() for _ in range(n)]
        door_col = n // 2 if fi == 0 else -1
        for f in range(nf):
            band = band_of(f)
            y = base + f * fh
            for c in range(n):
                prng = _rng(seed, bid, fi, c, band, column_rng_seed[c])
                a = None
                if band == "ground":
                    if c == door_col and roles.get("door") and (n < 3 or not roles.get("shopfront") or d < 8.0):
                        a = _pick(prng, roles["door"], band, block)
                    elif fi == 0 and roles.get("shopfront"):
                        a = _pick(prng, roles["shopfront"], band, block)
                        if a is None and c == door_col and roles.get("door"):
                            a = _pick(prng, roles["door"], band, block)
                    if a is None and fi == 0 and windows and ratio > 0 and c != door_col and not roles.get("shopfront"):
                        a = _pick(prng, windows, band, block)
                if a is None and band != "ground" and column_window[c] and windows:
                    a = _pick(prng, windows, band, block) or _pick(prng, windows, "middle", block)
                if a is None and walls:
                    a = _pick(prng, walls, band, block) or walls[0]
                if a is None:
                    continue
                pw, pmh = _module(a)
                s = (c + 0.5) * slot
                x, z = fr.world(sx0 + tx * s, sz0 + tz * s)
                out.append(_inst(bid, f"{fi}:{f}:{c}", a, x, y, z, rot, (slot / pw, fh / pmh, 1.0)))
        if parapet:
            a = _pick(_rng(seed, bid, fi, "parapet"), parapet, "roof", block) or parapet[0]
            pw, _ = _module(a)
            for c in range(n):
                s = (c + 0.5) * slot
                x, z = fr.world(sx0 + tx * s, sz0 + tz * s)
                out.append(_inst(bid, f"{fi}:p:{c}", a, x, base + hu, z, rot, (slot / pw, 1.0, 1.0)))

    corners = roles.get("corner") or []
    if corners:
        a = _pick(_rng(seed, bid, "corner"), corners, "middle", block) or corners[0]
        _cw, ch = _module(a)
        for fi, phi, (sx0, sz0), (tx, tz), L in faces(block):
            x, z = fr.world(sx0 + tx * L, sz0 + tz * L)  # end of face fi = start of face fi+1
            out.append(_inst(bid, f"c{fi}:0:0", a, x, base, z, fr.rot + phi, (1.0, H / ch, 1.0)))

    roofs = roles.get("roof") or []
    if roofs:
        a = _pick(_rng(seed, bid, "roof"), roofs, "roof", block) or roofs[0]
        rw, rd = _module(a)
        x, z = fr.world(0.0, 0.0)
        out.append(_inst(bid, "r:0:0", a, x, base + hu, z, fr.rot, (w / rw, 1.0, d / rd)))

    props = roles.get("roof_prop") or []
    if props and w > 4.0 and d > 4.0:
        prng = _rng(seed, bid, "roofprops")
        n_props = min(int(opt["max_roof_props"]), int(w * d / 100.0 * opt["roof_props_per_100m2"] + prng.random()))
        placed: List[Tuple[float, float, float]] = []
        for i in range(n_props):
            fit = [p for p in props if w >= (p.get("kit") or {}).get("min_roof", [0, 0])[0]
                   and d >= (p.get("kit") or {}).get("min_roof", [0, 0])[1]]
            a = _pick(prng, fit, "roof", block) if fit else None
            if a is None:
                break
            b = a["bounds"]
            r = 0.5 * math.hypot(b["max"][0] - b["min"][0], b["max"][2] - b["min"][2])
            mx_, mz_ = w / 2 - r - 0.6, d / 2 - r - 0.6
            if mx_ <= 0 or mz_ <= 0:
                continue
            for _try in range(8):
                lx, lz = prng.uniform(-mx_, mx_), prng.uniform(-mz_, mz_)
                if all(math.hypot(lx - qx, lz - qz) > r + qr + 0.4 for qx, qz, qr in placed):
                    placed.append((lx, lz, r))
                    x, z = fr.world(lx, lz)
                    out.append(_inst(bid, f"rp:0:{i}", a, x, base + hu + 0.12, z,
                                     fr.rot + 90.0 * prng.randrange(4), (1.0, 1.0, 1.0)))
                    break
    return out


def strategy_for(block: dict, sets: Dict[str, dict], default: str = "auto") -> Tuple[str, Optional[dict]]:
    """-> ("kit", set) | ("whole", None) | ("none", None)."""
    mode = block.get("dress") or default or "auto"
    if mode == "none":
        return "none", None
    if mode == "whole" or block.get("asset_lock"):
        return "whole", None
    if mode == "kit":
        s = choose_set(block, sets, strict=False)
        return ("kit", s) if s else ("whole", None)
    s = choose_set(block, sets, strict=True)
    return ("kit", s) if s else ("whole", None)


def summary(block: dict, kset: dict) -> dict:
    """Floors / modules a block would get (for UIs)."""
    mw, mh = kset["module"]
    fh = float(block.get("floor_height") or mh)
    parapet = kset["roles"].get("parapet") or []
    ph = _module(parapet[0])[1] if parapet else 0.0
    hu = max(float(block["size"][1]) - ph, 0.5)
    nf = max(1, int(round(hu / fh)))
    mods = [max(1, int(round(L / mw))) for *_x, L in faces(block)]
    return {"set": kset["name"], "floors": nf, "floor_height": round(hu / nf, 3), "modules": mods}
