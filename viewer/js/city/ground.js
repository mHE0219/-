// Ground slab with a rasterised map texture, optional terrain, OSM massing, grid, scale figure.
import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

export const AREA_COLORS = {
  forest: '#3f6b35', park: '#6d9a4f', grass: '#8bb36a', scrub: '#7c8f55', farmland: '#b9b77a', orchard: '#97ad5e',
  water: '#4b86b4', wetland: '#6b9a8f', sand: '#d9c99a', residential: '#c9c2b6', commercial: '#cdbfb6',
  industrial: '#b9b3b8', parking: '#9b9b9b', plaza: '#d3cfc6', school: '#d6c9a8', cemetery: '#a3b38f', other: '#bdb8ab',
};
const BASE = '#b8b2a5';
const ROAD_COLORS = { footway: '#d8d3c8', path: '#cfc7b3', cycleway: '#c7c9d6', steps: '#d0c8bc', pedestrian: '#dcd6cc', track: '#a8957a' };

export function makeHeightSampler(terrain) {
  if (!terrain) return () => 0;
  const [nx, nz] = terrain.res;
  const [x0, z0, x1, z1] = terrain.rect;
  const H = terrain.heights;
  return (x, z) => {
    let u = (x - x0) / (x1 - x0) * (nx - 1);
    let v = (z - z0) / (z1 - z0) * (nz - 1);
    u = Math.min(Math.max(u, 0), nx - 1.000001); v = Math.min(Math.max(v, 0), nz - 1.000001);
    const i = Math.floor(u), j = Math.floor(v), fu = u - i, fv = v - j;
    const a = H[j * nx + i], b = H[j * nx + i + 1], c = H[(j + 1) * nx + i], d = H[(j + 1) * nx + i + 1];
    return (a * (1 - fu) + b * fu) * (1 - fv) + (c * (1 - fu) + d * fu) * fv;
  };
}

/** Rasterise areas/roads/water/footprints into a canvas (north up). */
export function drawMapCanvas(layout, maxPx = 4096) {
  const [x0, z0, x1, z1] = layout.box.rect;
  const w = x1 - x0, d = z1 - z0;
  const k = Math.min(maxPx / Math.max(w, d), 4); // px per metre
  const cv = document.createElement('canvas');
  cv.width = Math.max(2, Math.round(w * k)); cv.height = Math.max(2, Math.round(d * k));
  const g = cv.getContext('2d');
  const X = (x) => (x - x0) * k, Z = (z) => (z - z0) * k;
  g.fillStyle = BASE; g.fillRect(0, 0, cv.width, cv.height);

  const poly = (outer, holes) => {
    g.beginPath();
    for (const ring of [outer, ...(holes || [])]) {
      ring.forEach((p, i) => (i ? g.lineTo(X(p[0]), Z(p[1])) : g.moveTo(X(p[0]), Z(p[1]))));
      g.closePath();
    }
  };
  const areas = [...layout.areas].sort((a, b) => b.area - a.area);
  for (const a of areas) {
    poly(a.outer, a.holes);
    g.fillStyle = AREA_COLORS[a.kind] || AREA_COLORS.other;
    g.fill('evenodd');
  }
  g.lineCap = 'round'; g.lineJoin = 'round';
  for (const w of layout.waterways || []) {
    g.strokeStyle = AREA_COLORS.water; g.lineWidth = Math.max(w.width * k, 1);
    g.beginPath(); w.line.forEach((p, i) => (i ? g.lineTo(X(p[0]), Z(p[1])) : g.moveTo(X(p[0]), Z(p[1])))); g.stroke();
  }
  const roads = [...layout.roads].filter((r) => !r.tunnel).sort((a, b) => a.width - b.width);
  // casing then fill for a map-like look
  for (const pass of [0, 1]) {
    for (const r of roads) {
      const foot = r.kind in ROAD_COLORS;
      if (pass === 0 && foot) continue;
      g.strokeStyle = pass === 0 ? '#5d5a55' : (ROAD_COLORS[r.kind] || '#3f4145');
      g.lineWidth = Math.max(r.width * k + (pass === 0 ? 1.2 * k : 0), 1);
      g.beginPath(); r.line.forEach((p, i) => (i ? g.lineTo(X(p[0]), Z(p[1])) : g.moveTo(X(p[0]), Z(p[1])))); g.stroke();
    }
  }
  // lane markings on wider roads
  g.setLineDash([3 * k, 4 * k]); g.strokeStyle = 'rgba(240,240,230,.55)'; g.lineWidth = Math.max(0.15 * k, 1);
  for (const r of roads) {
    if (r.width < 7 || r.kind in ROAD_COLORS) continue;
    g.beginPath(); r.line.forEach((p, i) => (i ? g.lineTo(X(p[0]), Z(p[1])) : g.moveTo(X(p[0]), Z(p[1])))); g.stroke();
  }
  g.setLineDash([]);
  g.fillStyle = 'rgba(70,66,62,.35)';
  for (const b of layout.buildings) { poly(b.outer, b.holes); g.fill('evenodd'); }
  return cv;
}

export function buildGround(layout, { showMap = true } = {}) {
  const group = new THREE.Group();
  group.name = 'ground';
  const [x0, z0, x1, z1] = layout.box.rect;
  const w = x1 - x0, d = z1 - z0;
  const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
  const terrain = layout.terrain;
  const hAt = makeHeightSampler(terrain);

  const canvas = drawMapCanvas(layout);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  const mapMat = new THREE.MeshStandardMaterial({ map: tex, roughness: 1 });
  const plainMat = new THREE.MeshStandardMaterial({ color: BASE, roughness: 1 });

  let top;
  if (terrain) {
    const [nx, nz] = terrain.res;
    top = new THREE.PlaneGeometry(w, d, nx - 1, nz - 1);
    top.rotateX(-Math.PI / 2);
    const pos = top.attributes.position;
    for (let i = 0; i < pos.count; i++) pos.setY(i, terrain.heights[i]);
    top.computeVertexNormals();
  } else {
    top = new THREE.PlaneGeometry(w, d);
    top.rotateX(-Math.PI / 2);
  }
  top.translate(cx, 0, cz);
  const topMesh = new THREE.Mesh(top, showMap ? mapMat : plainMat);
  topMesh.receiveShadow = true;
  topMesh.name = 'groundTop';
  group.add(topMesh);

  // slab sides (diorama look)
  const minH = terrain ? Math.min(...terrain.heights) : 0;
  const T = Math.max(6, Math.min(w, d) * 0.03);
  const slab = new THREE.Mesh(new THREE.BoxGeometry(w, T, d), new THREE.MeshStandardMaterial({ color: '#4a3f35', roughness: 1 }));
  slab.position.set(cx, minH - T / 2 - 0.05, cz);
  group.add(slab);
  if (terrain) group.add(terrainSkirt(layout, minH - 0.05));

  return { group, topMesh, mapMat, plainMat, heightAt: hAt, canvas };
}

function terrainSkirt(layout, yBottom) {
  const t = layout.terrain;
  const [nx, nz] = t.res;
  const [x0, z0, x1, z1] = t.rect;
  const pos = [];
  const edge = (pts) => {
    for (let i = 0; i < pts.length - 1; i++) {
      const [ax, az, ah] = pts[i], [bx, bz, bh] = pts[i + 1];
      pos.push(ax, yBottom, az, bx, yBottom, bz, bx, bh, bz, ax, yBottom, az, bx, bh, bz, ax, ah, az);
    }
  };
  const P = (i, j) => [x0 + (x1 - x0) * i / (nx - 1), z0 + (z1 - z0) * j / (nz - 1), t.heights[j * nx + i]];
  const south = [], north = [], east = [], west = [];
  for (let i = 0; i < nx; i++) { north.push(P(nx - 1 - i, 0)); south.push(P(i, nz - 1)); }
  for (let j = 0; j < nz; j++) { west.push(P(0, j)); east.push(P(nx - 1, nz - 1 - j)); }
  [north, south, east, west].forEach(edge);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  g.computeVertexNormals();
  return new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: '#5a4b3d', roughness: 1, side: THREE.DoubleSide }));
}

/** OSM footprints extruded to their heights (the "map box" reference). */
export function buildMassing(layout, heightAt) {
  const geos = [];
  for (const b of layout.buildings) {
    if (b.outer.length < 3) continue;
    const shape = new THREE.Shape(b.outer.map((p) => new THREE.Vector2(p[0], -p[1])));
    for (const h of b.holes || []) shape.holes.push(new THREE.Path(h.map((p) => new THREE.Vector2(p[0], -p[1]))));
    const height = Math.max(b.height - (b.min_height || 0), 1);
    const g = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });
    g.rotateX(-Math.PI / 2);
    const base = Math.min(...b.outer.map((p) => heightAt(p[0], p[1]))) + (b.min_height || 0);
    g.translate(0, base, 0);
    g.clearGroups();
    geos.push(g);
  }
  if (!geos.length) return new THREE.Group();
  const merged = mergeGeometries(geos, false);
  geos.forEach((g) => g.dispose());
  const mat = new THREE.MeshStandardMaterial({ color: '#e8ecf4', roughness: 0.7, transparent: true, opacity: 0.55, depthWrite: false });
  const mesh = new THREE.Mesh(merged, mat);
  mesh.name = 'massing';
  return mesh;
}

export function buildGrid(layout) {
  const [x0, z0, x1, z1] = layout.box.rect;
  const size = Math.max(x1 - x0, z1 - z0);
  const div = Math.round(size / 10);
  const g = new THREE.GridHelper(div * 10, div, 0xffffff, 0xffffff);
  g.material.transparent = true; g.material.opacity = 0.18; g.material.depthWrite = false;
  g.position.set((x0 + x1) / 2, 0.15, (z0 + z1) / 2);
  const g100 = new THREE.GridHelper(Math.round(size / 100) * 100 || 100, Math.max(1, Math.round(size / 100)), 0xffcc66, 0xffcc66);
  g100.material.transparent = true; g100.material.opacity = 0.5; g100.material.depthWrite = false;
  g100.position.copy(g.position).y += 0.02;
  const grp = new THREE.Group(); grp.add(g, g100); grp.name = 'grid';
  return grp;
}

/** 1.8 m tall person silhouette at the given position — the scale sanity check. */
export function buildHuman() {
  const grp = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: '#ff8a3d', roughness: 0.6 });
  const body = new THREE.Mesh(new THREE.CapsuleGeometry(0.2, 1.0, 4, 8), mat);
  body.position.y = 0.2 + 0.5 + 0.12;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.12, 12, 8), mat);
  head.position.y = 1.68;
  grp.add(body, head);
  grp.traverse((o) => { o.castShadow = true; });
  grp.name = 'human_1p8m';
  return grp;
}
