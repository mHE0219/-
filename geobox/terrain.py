"""Optional terrain: samples AWS Terrain Tiles (Terrarium PNG, free, no key)
into a regular height grid over the box. Pure stdlib (tiny PNG decoder).

Data: https://registry.opendata.aws/terrain-tiles/  (SRTM/GMTED/NED/ETOPO etc.)
Height = (R * 256 + G + B / 256) - 32768  metres above sea level.
"""
from __future__ import annotations

import math
import struct
import urllib.request
import zlib
from typing import Dict, List, Tuple

from .geo import BoxSpec

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
USER_AGENT = "geobox-pipeline/0.1"


def decode_png_rgb(data: bytes) -> Tuple[int, int, int, List[bytearray]]:
    """Minimal PNG decoder for 8-bit RGB/RGBA non-interlaced images."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = bytearray()
    width = height = ctype = depth = interlace = 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            width, height, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
        elif typ == b"IDAT":
            idat += chunk
        elif typ == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError(f"unsupported PNG (depth={depth}, color type={ctype}, interlace={interlace})")
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    rows: List[bytearray] = []
    prev = bytearray(stride)
    p = 0
    for _y in range(height):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        rows.append(line)
        prev = line
    return width, height, bpp, rows


def _tile_xy(lat: float, lon: float, z: int) -> Tuple[float, float]:
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lr)) / math.pi) / 2.0 * n
    return x, y


def sample_terrain(box: BoxSpec, resolution: float = 4.0, zoom: int = 15, log=print) -> dict:
    """Return {"res":[nx,nz], "rect":[x0,z0,x1,z1], "heights":[...], "base_elevation": m}
    heights are relative to base_elevation (the box centre ground)."""
    frame = box.frame
    x0, z0, x1, z1 = box.local_rect()
    nx = max(2, int(round((x1 - x0) / resolution)) + 1)
    nz = max(2, int(round((z1 - z0) / resolution)) + 1)
    cache: Dict[Tuple[int, int], Tuple[int, int, int, List[bytearray]]] = {}

    def tile(tx, ty):
        k = (tx, ty)
        if k not in cache:
            url = TILE_URL.format(z=zoom, x=tx, y=ty)
            log(f"[terrain] GET {url}")
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                cache[k] = decode_png_rgb(r.read())
        return cache[k]

    def height_at_pixel(px, py):
        tx, ty = int(px // 256), int(py // 256)
        w, h, bpp, rows = tile(tx, ty)
        ix, iy = int(px - tx * 256), int(py - ty * 256)
        ix, iy = min(max(ix, 0), w - 1), min(max(iy, 0), h - 1)
        r, g, b = rows[iy][ix * bpp], rows[iy][ix * bpp + 1], rows[iy][ix * bpp + 2]
        return r * 256.0 + g + b / 256.0 - 32768.0

    def height_at(lat, lon):
        fx, fy = _tile_xy(lat, lon, zoom)
        px, py = fx * 256 - 0.5, fy * 256 - 0.5
        ix, iy = math.floor(px), math.floor(py)
        tx, ty = px - ix, py - iy
        h00 = height_at_pixel(ix, iy)
        h10 = height_at_pixel(ix + 1, iy)
        h01 = height_at_pixel(ix, iy + 1)
        h11 = height_at_pixel(ix + 1, iy + 1)
        return (h00 * (1 - tx) + h10 * tx) * (1 - ty) + (h01 * (1 - tx) + h11 * tx) * ty

    heights: List[float] = []
    for j in range(nz):
        z = z0 + (z1 - z0) * j / (nz - 1)
        for i in range(nx):
            x = x0 + (x1 - x0) * i / (nx - 1)
            lat, lon = frame.from_local(x, z)
            heights.append(height_at(lat, lon))
    base = height_at(box.lat, box.lon)
    rel = [round(h - base, 2) for h in heights]
    log(f"[terrain] {nx}x{nz} samples, base elevation {base:.1f} m, range {min(rel):.1f}..{max(rel):.1f} m")
    return {"res": [nx, nz], "rect": [x0, z0, x1, z1], "heights": rel, "base_elevation": round(base, 2),
            "source": "AWS Terrain Tiles (terrarium), zoom %d" % zoom}


def smooth(terrain: dict, passes: int = 1) -> dict:
    """3x3 box blur (edge-clamped). SRTM-based tiles are surface models: in dense
    cities they partly contain buildings/trees, which shows up as bumps."""
    nx, nz = terrain["res"]
    H = list(terrain["heights"])
    for _ in range(max(0, passes)):
        out = H[:]
        for j in range(nz):
            for i in range(nx):
                acc, n = 0.0, 0
                for dj in (-1, 0, 1):
                    jj = min(max(j + dj, 0), nz - 1)
                    for di in (-1, 0, 1):
                        ii = min(max(i + di, 0), nx - 1)
                        acc += H[jj * nx + ii]
                        n += 1
                out[j * nx + i] = acc / n
        H = out
    t = dict(terrain)
    t["heights"] = [round(h, 2) for h in H]
    t["smooth_passes"] = terrain.get("smooth_passes", 0) + passes
    return t


def sampler(terrain: dict):
    """Bilinear height lookup h(x, z) for a terrain dict (or 0 if None)."""
    if not terrain:
        return lambda x, z: 0.0
    nx, nz = terrain["res"]
    x0, z0, x1, z1 = terrain["rect"]
    H = terrain["heights"]

    def f(x: float, z: float) -> float:
        u = (x - x0) / (x1 - x0) * (nx - 1)
        v = (z - z0) / (z1 - z0) * (nz - 1)
        u = min(max(u, 0.0), nx - 1.000001)
        v = min(max(v, 0.0), nz - 1.000001)
        i, j = int(u), int(v)
        fu, fv = u - i, v - j
        a = H[j * nx + i]
        b = H[j * nx + i + 1]
        c = H[(j + 1) * nx + i]
        d = H[(j + 1) * nx + i + 1]
        return (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv

    return f
