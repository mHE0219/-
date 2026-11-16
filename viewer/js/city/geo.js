// WGS84 <-> local box frame (+X east, +Y up, -Z north), same math as geobox/geo.py
const A = 6378137.0;
const F = 1 / 298.257223563;
const E2 = F * (2 - F);
const B = A * (1 - F);
const EP2 = (A * A - B * B) / (B * B);
const D2R = Math.PI / 180;

export function geodeticToEcef(lat, lon, h = 0) {
  const la = lat * D2R, lo = lon * D2R;
  const s = Math.sin(la), c = Math.cos(la);
  const n = A / Math.sqrt(1 - E2 * s * s);
  return [(n + h) * c * Math.cos(lo), (n + h) * c * Math.sin(lo), (n * (1 - E2) + h) * s];
}

export function ecefToGeodetic(x, y, z) {
  const lon = Math.atan2(y, x);
  const p = Math.hypot(x, y);
  const th = Math.atan2(z * A, p * B);
  const st = Math.sin(th), ct = Math.cos(th);
  const lat = Math.atan2(z + EP2 * B * st ** 3, p - E2 * A * ct ** 3);
  const s = Math.sin(lat);
  const n = A / Math.sqrt(1 - E2 * s * s);
  return [lat / D2R, lon / D2R, p / Math.cos(lat) - n];
}

export class LocalFrame {
  constructor(lat0, lon0, h0 = 0) {
    this.lat0 = lat0; this.lon0 = lon0; this.h0 = h0;
    this.o = geodeticToEcef(lat0, lon0, h0);
    const la = lat0 * D2R, lo = lon0 * D2R;
    const sl = Math.sin(la), cl = Math.cos(la), so = Math.sin(lo), co = Math.cos(lo);
    this.e = [-so, co, 0];
    this.n = [-sl * co, -sl * so, cl];
    this.u = [cl * co, cl * so, sl];
  }
  toLocal(lat, lon) {
    const p = geodeticToEcef(lat, lon, this.h0);
    const d = [p[0] - this.o[0], p[1] - this.o[1], p[2] - this.o[2]];
    const dot = (r) => r[0] * d[0] + r[1] * d[1] + r[2] * d[2];
    return [dot(this.e), -dot(this.n)];
  }
  fromLocal(x, z) {
    const e = x, n = -z;
    const p = [0, 1, 2].map((i) => this.o[i] + this.e[i] * e + this.n[i] * n);
    const g = ecefToGeodetic(p[0], p[1], p[2]);
    return [g[0], g[1]];
  }
  /** lat/lon bounds of a W x D box centred on the frame origin */
  boxBounds(w, d) {
    const pts = [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]].map(([x, z]) => this.fromLocal(x, z));
    return pts;
  }
}
