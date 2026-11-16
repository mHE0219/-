"""2D geometry on the ground plane. Points are [x, z] (x east, z south).

Ring orientation convention for outputs: outer rings are counter-clockwise
*as seen from above* (east right, north up), i.e. negative shoelace area in
raw (x, z). Use :func:`orient_outer` to enforce it.
"""
from __future__ import annotations

import math
import random
from typing import Iterable, List, Optional, Sequence, Tuple

Pt = Sequence[float]
Ring = List[List[float]]


def signed_area_xz(ring: Sequence[Pt]) -> float:
    """Shoelace in raw (x, z) coordinates."""
    a = 0.0
    n = len(ring)
    for i in range(n):
        x0, z0 = ring[i][0], ring[i][1]
        x1, z1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        a += x0 * z1 - x1 * z0
    return 0.5 * a


def area(ring: Sequence[Pt]) -> float:
    return abs(signed_area_xz(ring))


def strip_closing(ring: Sequence[Pt]) -> Ring:
    pts = [list(p) for p in ring]
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts.pop()
    return pts


def orient_outer(ring: Sequence[Pt]) -> Ring:
    """CCW seen from above  <=>  signed_area_xz < 0."""
    r = strip_closing(ring)
    return r[::-1] if signed_area_xz(r) > 0 else r


def orient_hole(ring: Sequence[Pt]) -> Ring:
    r = strip_closing(ring)
    return r[::-1] if signed_area_xz(r) < 0 else r


def centroid(ring: Sequence[Pt]) -> Tuple[float, float]:
    a = signed_area_xz(ring)
    if abs(a) < 1e-9:
        xs = [p[0] for p in ring]
        zs = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(zs) / len(zs)
    cx = cz = 0.0
    n = len(ring)
    for i in range(n):
        x0, z0 = ring[i][0], ring[i][1]
        x1, z1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        cr = x0 * z1 - x1 * z0
        cx += (x0 + x1) * cr
        cz += (z0 + z1) * cr
    return cx / (6.0 * a), cz / (6.0 * a)


def bbox(points: Iterable[Pt]) -> Tuple[float, float, float, float]:
    xs, zs = [], []
    for p in points:
        xs.append(p[0])
        zs.append(p[1])
    return min(xs), min(zs), max(xs), max(zs)


def point_in_ring(x: float, z: float, ring: Sequence[Pt]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, zi = ring[i][0], ring[i][1]
        xj, zj = ring[j][0], ring[j][1]
        if (zi > z) != (zj > z):
            xc = (xj - xi) * (z - zi) / (zj - zi) + xi
            if x < xc:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x: float, z: float, outer: Sequence[Pt], holes: Sequence[Sequence[Pt]] = ()) -> bool:
    if not point_in_ring(x, z, outer):
        return False
    return not any(point_in_ring(x, z, h) for h in holes)


def dist_point_segment(px: float, pz: float, a: Pt, b: Pt) -> float:
    ax, az, bx, bz = a[0], a[1], b[0], b[1]
    dx, dz = bx - ax, bz - az
    L2 = dx * dx + dz * dz
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (pz - az) * dz) / L2))
    qx, qz = ax + t * dx, az + t * dz
    return math.hypot(px - qx, pz - qz)


def dist_point_polyline(px: float, pz: float, line: Sequence[Pt]) -> float:
    return min(dist_point_segment(px, pz, line[i], line[i + 1]) for i in range(len(line) - 1))


def polyline_length(line: Sequence[Pt]) -> float:
    return sum(math.hypot(line[i + 1][0] - line[i][0], line[i + 1][1] - line[i][1]) for i in range(len(line) - 1))


def resample_polyline(line: Sequence[Pt], spacing: float, offset: float = 0.0) -> List[Tuple[float, float, float]]:
    """Points every `spacing` metres along a polyline -> (x, z, heading_rad)."""
    out = []
    d_next = offset if offset > 0 else spacing * 0.5
    acc = 0.0
    for i in range(len(line) - 1):
        ax, az = line[i][0], line[i][1]
        bx, bz = line[i + 1][0], line[i + 1][1]
        seg = math.hypot(bx - ax, bz - az)
        if seg <= 1e-9:
            continue
        heading = math.atan2(bx - ax, bz - az)
        while d_next <= acc + seg:
            t = (d_next - acc) / seg
            out.append((ax + t * (bx - ax), az + t * (bz - az), heading))
            d_next += spacing
        acc += seg
    return out


# --- convex hull & minimum-area oriented rectangle --------------------------
def convex_hull(points: Iterable[Pt]) -> List[Tuple[float, float]]:
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def min_area_rect(points: Iterable[Pt]) -> dict:
    """Oriented bounding box via rotating calipers over hull edges.

    Returns {center:[x,z], size:[w,d], angle: radians} where `angle` is the
    rotation about +Y (counter-clockwise seen from above) that maps the local
    box X axis (width) onto the world. Width is the longer side.
    """
    hull = convex_hull(points)
    if len(hull) < 3:
        x0, z0, x1, z1 = bbox(hull) if hull else (0, 0, 0, 0)
        return {"center": [(x0 + x1) / 2, (z0 + z1) / 2], "size": [max(x1 - x0, 0.01), max(z1 - z0, 0.01)], "angle": 0.0}
    best = None
    n = len(hull)
    for i in range(n):
        ax, az = hull[i]
        bx, bz = hull[(i + 1) % n]
        ex, ez = bx - ax, bz - az
        L = math.hypot(ex, ez)
        if L < 1e-9:
            continue
        ux, uz = ex / L, ez / L  # edge direction
        vx, vz = -uz, ux  # perpendicular
        us = [p[0] * ux + p[1] * uz for p in hull]
        vs = [p[0] * vx + p[1] * vz for p in hull]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        a = (u1 - u0) * (v1 - v0)
        if best is None or a < best[0] - 1e-9:
            best = (a, ux, uz, vx, vz, u0, u1, v0, v1)
    _a, ux, uz, vx, vz, u0, u1, v0, v1 = best
    cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
    cx, cz = cu * ux + cv * vx, cu * uz + cv * vz
    w, d = u1 - u0, v1 - v0
    axis = (ux, uz)
    if d > w:  # make width the long side
        w, d = d, w
        axis = (vx, vz)
    # world direction of local +X is (cos a, -sin a) in (x, z) for a rotation a about +Y
    angle = math.atan2(-axis[1], axis[0])
    # canonicalise to (-pi/2, pi/2]
    while angle <= -math.pi / 2:
        angle += math.pi
    while angle > math.pi / 2:
        angle -= math.pi
    return {"center": [cx, cz], "size": [w, d], "angle": angle}


# --- clipping to the box rectangle ----------------------------------------
def clip_ring_rect(ring: Sequence[Pt], rect: Tuple[float, float, float, float]) -> Ring:
    """Sutherland–Hodgman against an axis-aligned rectangle."""
    x0, z0, x1, z1 = rect
    pts = [list(p) for p in ring]

    def clip(pts, inside, inter):
        if not pts:
            return pts
        out = []
        prev = pts[-1]
        for cur in pts:
            if inside(cur):
                if not inside(prev):
                    out.append(inter(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(inter(prev, cur))
            prev = cur
        return out

    def ix(xc):
        def f(a, b):
            t = (xc - a[0]) / (b[0] - a[0])
            return [xc, a[1] + t * (b[1] - a[1])]
        return f

    def iz(zc):
        def f(a, b):
            t = (zc - a[1]) / (b[1] - a[1])
            return [a[0] + t * (b[0] - a[0]), zc]
        return f

    pts = clip(pts, lambda p: p[0] >= x0, ix(x0))
    pts = clip(pts, lambda p: p[0] <= x1, ix(x1))
    pts = clip(pts, lambda p: p[1] >= z0, iz(z0))
    pts = clip(pts, lambda p: p[1] <= z1, iz(z1))
    return [[round(p[0], 3), round(p[1], 3)] for p in pts]


def clip_polyline_rect(line: Sequence[Pt], rect: Tuple[float, float, float, float]) -> List[Ring]:
    """Liang–Barsky per segment; returns list of polylines inside the rect."""
    x0, z0, x1, z1 = rect
    parts: List[Ring] = []
    cur: Ring = []
    for i in range(len(line) - 1):
        ax, az = line[i][0], line[i][1]
        bx, bz = line[i + 1][0], line[i + 1][1]
        dx, dz = bx - ax, bz - az
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dz, az - z0), (dz, z1 - az)):
            if abs(p) < 1e-12:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if not ok or t0 > t1:
            if len(cur) >= 2:
                parts.append(cur)
            cur = []
            continue
        pa = [round(ax + t0 * dx, 3), round(az + t0 * dz, 3)]
        pb = [round(ax + t1 * dx, 3), round(az + t1 * dz, 3)]
        if cur and (abs(cur[-1][0] - pa[0]) > 1e-3 or abs(cur[-1][1] - pa[1]) > 1e-3):
            if len(cur) >= 2:
                parts.append(cur)
            cur = []
        if not cur:
            cur = [pa]
        cur.append(pb)
        if t1 < 1.0:
            parts.append(cur)
            cur = []
    if len(cur) >= 2:
        parts.append(cur)
    return parts


def simplify(line: Sequence[Pt], tol: float) -> Ring:
    """Douglas–Peucker."""
    pts = [list(p) for p in line]
    if len(pts) < 3 or tol <= 0:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        dmax, idx = 0.0, -1
        for k in range(i + 1, j):
            d = dist_point_segment(pts[k][0], pts[k][1], pts[i], pts[j])
            if d > dmax:
                dmax, idx = d, k
        if dmax > tol:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(pts, keep) if k]


# --- sampling ----------------------------------------------------------------
def poisson_disk(rect: Tuple[float, float, float, float], radius: float, rng: random.Random,
                 accept=None, k: int = 20, max_points: int = 200000) -> List[Tuple[float, float]]:
    """Bridson Poisson-disk sampling inside `rect`, filtered by `accept(x, z)`."""
    x0, z0, x1, z1 = rect
    w, h = x1 - x0, z1 - z0
    if w <= 0 or h <= 0 or radius <= 0:
        return []
    cell = radius / math.sqrt(2)
    gw, gh = int(math.ceil(w / cell)), int(math.ceil(h / cell))
    grid: dict = {}
    samples: List[Tuple[float, float]] = []
    active: List[Tuple[float, float]] = []

    def fits(x, z):
        gx, gz = int((x - x0) / cell), int((z - z0) / cell)
        for ix in range(max(gx - 2, 0), min(gx + 3, gw)):
            for iz in range(max(gz - 2, 0), min(gz + 3, gh)):
                s = grid.get((ix, iz))
                if s is not None and (s[0] - x) ** 2 + (s[1] - z) ** 2 < radius * radius:
                    return False
        return True

    def add(x, z):
        samples.append((x, z))
        active.append((x, z))
        grid[(int((x - x0) / cell), int((z - z0) / cell))] = (x, z)

    # seed several points so disconnected regions get covered
    seeds = max(1, int(w * h / (radius * radius * 50)))
    for _ in range(seeds * 4):
        x, z = rng.uniform(x0, x1), rng.uniform(z0, z1)
        if (accept is None or accept(x, z)) and fits(x, z):
            add(x, z)
    while active and len(samples) < max_points:
        i = rng.randrange(len(active))
        ax, az = active[i]
        found = False
        for _ in range(k):
            a = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(radius, 2 * radius)
            x, z = ax + r * math.cos(a), az + r * math.sin(a)
            if x0 <= x < x1 and z0 <= z < z1 and fits(x, z):
                if accept is not None and not accept(x, z):
                    continue
                add(x, z)
                found = True
                break
        if not found:
            active[i] = active[-1]
            active.pop()
    return samples


class SpatialHash:
    """Bucket line segments / polygons for fast proximity tests."""

    def __init__(self, cell: float = 25.0):
        self.cell = cell
        self.buckets: dict = {}

    def _keys(self, x0, z0, x1, z1):
        c = self.cell
        for ix in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for iz in range(int(math.floor(z0 / c)), int(math.floor(z1 / c)) + 1):
                yield (ix, iz)

    def insert(self, item, box):
        for k in self._keys(*box):
            self.buckets.setdefault(k, []).append(item)

    def query(self, x: float, z: float, r: float = 0.0):
        seen = set()
        for k in self._keys(x - r, z - r, x + r, z + r):
            for it in self.buckets.get(k, ()):
                if id(it) not in seen:
                    seen.add(id(it))
                    yield it
