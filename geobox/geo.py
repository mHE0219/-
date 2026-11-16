"""Geodesy helpers: WGS84 lat/lon <-> local metric frame of the box.

Local frame (see docs/conventions.md):
    +X = east, +Y = up, -Z = north   (1 unit = 1 metre)

We use an exact geodetic -> ECEF -> ENU transform at the box centre, so
distances are true ground metres. (Google Maps / web maps display Web
Mercator, whose scale is inflated by 1/cos(lat); we never use it for geometry.)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_B = WGS84_A * (1.0 - WGS84_F)
WGS84_EP2 = (WGS84_A ** 2 - WGS84_B ** 2) / WGS84_B ** 2


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h: float = 0.0) -> Tuple[float, float, float]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    s, c = math.sin(lat), math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
    x = (n + h) * c * math.cos(lon)
    y = (n + h) * c * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + h) * s
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Bowring's method; sub-millimetre accuracy near the surface."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    st, ct = math.sin(theta), math.cos(theta)
    lat = math.atan2(z + WGS84_EP2 * WGS84_B * st ** 3, p - WGS84_E2 * WGS84_A * ct ** 3)
    s = math.sin(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
    h = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-12 else abs(z) - WGS84_B
    return math.degrees(lat), math.degrees(lon), h


@dataclass
class LocalFrame:
    """Tangent-plane frame anchored at (lat0, lon0, h0)."""

    lat0: float
    lon0: float
    h0: float = 0.0
    _o: Tuple[float, float, float] = field(init=False, repr=False)
    _r: Tuple[Tuple[float, float, float], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._o = geodetic_to_ecef(self.lat0, self.lon0, self.h0)
        lat = math.radians(self.lat0)
        lon = math.radians(self.lon0)
        sl, cl = math.sin(lat), math.cos(lat)
        so, co = math.sin(lon), math.cos(lon)
        east = (-so, co, 0.0)
        north = (-sl * co, -sl * so, cl)
        up = (cl * co, cl * so, sl)
        self._r = (east, north, up)

    # --- geodetic <-> ENU -------------------------------------------------
    def to_enu(self, lat: float, lon: float, h: float = 0.0) -> Tuple[float, float, float]:
        x, y, z = geodetic_to_ecef(lat, lon, h)
        dx, dy, dz = x - self._o[0], y - self._o[1], z - self._o[2]
        e, n, u = (r[0] * dx + r[1] * dy + r[2] * dz for r in self._r)
        return e, n, u

    def from_enu(self, e: float, n: float, u: float = 0.0) -> Tuple[float, float, float]:
        (ex, ey, ez), (nx, ny, nz), (ux, uy, uz) = self._r
        x = self._o[0] + ex * e + nx * n + ux * u
        y = self._o[1] + ey * e + ny * n + uy * u
        z = self._o[2] + ez * e + nz * n + uz * u
        return ecef_to_geodetic(x, y, z)

    # --- geodetic <-> local (x east, z south) -----------------------------
    def to_local(self, lat: float, lon: float) -> Tuple[float, float]:
        """Returns ground-plane (x, z). Height of the tangent plane drop is
        ignored on purpose (it is < 8 cm at 1 km from the centre)."""
        e, n, _u = self.to_enu(lat, lon, self.h0)
        return e, -n

    def from_local(self, x: float, z: float) -> Tuple[float, float]:
        lat, lon, _h = self.from_enu(x, -z, 0.0)
        return lat, lon

    def project(self, pts: Iterable[Tuple[float, float]]) -> List[List[float]]:
        """[(lat, lon), ...] -> [[x, z], ...] rounded to millimetres."""
        out = []
        for lat, lon in pts:
            x, z = self.to_local(lat, lon)
            out.append([round(x, 3), round(z, 3)])
        return out


@dataclass
class BoxSpec:
    """An axis-aligned (north-up) box on the ground, size in metres."""

    lat: float
    lon: float
    width: float  # east-west extent, metres (X)
    depth: float  # north-south extent, metres (Z)

    @property
    def frame(self) -> LocalFrame:
        return LocalFrame(self.lat, self.lon, 0.0)

    @property
    def half(self) -> Tuple[float, float]:
        return self.width / 2.0, self.depth / 2.0

    def local_rect(self, margin: float = 0.0) -> Tuple[float, float, float, float]:
        hx, hz = self.half
        return (-hx - margin, -hz - margin, hx + margin, hz + margin)

    def latlon_bbox(self, margin: float = 0.0) -> Tuple[float, float, float, float]:
        """(south, west, north, east) covering the box (+margin metres)."""
        f = self.frame
        x0, z0, x1, z1 = self.local_rect(margin)
        corners = [f.from_local(x, z) for x in (x0, 0.0, x1) for z in (z0, 0.0, z1)]
        lats = [c[0] for c in corners]
        lons = [c[1] for c in corners]
        return min(lats), min(lons), max(lats), max(lons)

    def to_dict(self) -> dict:
        s, w, n, e = self.latlon_bbox()
        return {
            "center": [self.lat, self.lon],
            "size": [self.width, self.depth],
            "bbox_latlon": [s, w, n, e],
        }


# --- Google Maps URL / text parsing ---------------------------------------
_FLOAT = r"(-?\d+(?:\.\d+)?)"


def _web_mercator_m_per_px(lat: float, zoom: float) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2.0 ** zoom)


def parse_location(text: str, viewport_px: Sequence[int] = (1280, 800)) -> dict:
    """Parse a Google Maps URL (or "lat, lon" text) into a centre and a
    *suggested* box size.

    Supported:
      .../@25.0339,121.5645,17z            (zoom -> suggested size)
      .../@25.0339,121.5645,600m/...       (satellite view height in metres)
      .../@25.0339,121.5645,350a,35y,...   (3D view, camera altitude)
      ...!3d25.0339!4d121.5645             (exact place pin, preferred)
      ?q=25.03,121.56  ?ll=  ?query=  ?center=
      "25.0339, 121.5645"
    """
    text = text.strip()
    result: dict = {"source": text}
    lat = lon = None

    m = re.search(r"!3d" + _FLOAT + r"!4d" + _FLOAT, text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        result["kind"] = "place"
    at = re.search(r"@" + _FLOAT + "," + _FLOAT + r"(?:,([\d.]+)([zmay]))?", text)
    if at:
        if lat is None:
            lat, lon = float(at.group(1)), float(at.group(2))
            result["kind"] = "view"
        if at.group(3):
            val, unit = float(at.group(3)), at.group(4)
            if unit == "z":
                result["zoom"] = val
                mpp = _web_mercator_m_per_px(lat, val)
                result["suggested_size"] = [round(mpp * viewport_px[0]), round(mpp * viewport_px[1])]
            elif unit in ("m", "a"):
                # satellite "m" ~ visible height; 3D "a" ~ camera altitude
                s = val if unit == "m" else val * 1.2
                result["suggested_size"] = [round(s * viewport_px[0] / viewport_px[1]), round(s)]
    if lat is None:
        m = re.search(r"[?&](?:q|ll|query|center|destination)=" + _FLOAT + r"(?:,|%2C)\s*" + _FLOAT, text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            result["kind"] = "query"
    if lat is None:
        m = re.fullmatch(r"\(?\s*" + _FLOAT + r"\s*[, ]\s*" + _FLOAT + r"\s*\)?", text)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            result["kind"] = "latlon"
    if lat is None:
        raise ValueError(
            "Could not find coordinates. Paste a Google Maps URL containing '@lat,lon' "
            "(right-click the map -> copy coordinates also works): %r" % text[:120]
        )
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range: %s, %s" % (lat, lon))
    result["center"] = [lat, lon]
    return result


def sun_direction(azimuth_deg: float, elevation_deg: float) -> Tuple[float, float, float]:
    """Compass azimuth (clockwise from north) + elevation -> unit vector in the
    local frame (+X east, +Y up, -Z north)."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return (math.sin(az) * math.cos(el), math.sin(el), -math.cos(az) * math.cos(el))
