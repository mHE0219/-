// Client-side asset thumbnails: render each library asset in a neutral "studio",
// 256x256 PNG with transparent background, uploaded to PUT /api/library/thumbs/<id>
// (the server stores assets/thumbs/<id>.png and re-indexes the asset: dominant colours etc.).
import * as THREE from 'three';

let R = null;

function renderer(size) {
  if (!R) {
    const canvas = document.createElement('canvas');
    R = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
    R.setPixelRatio(1);
    R.outputColorSpace = THREE.SRGBColorSpace;
    R.toneMapping = THREE.ACESFilmicToneMapping;
    R.toneMappingExposure = 1.05;
    R.setClearColor(0x000000, 0);
  }
  R.setSize(size, size, false);
  return R;
}

export function disposeThumbRenderer() {
  if (R) { R.dispose(); R.forceContextLoss?.(); R = null; }
}

/** Render one asset (AssetLibrary id) -> PNG Blob. Front of the asset (+Z) faces the camera, 3/4 view. */
export async function renderThumbnail(lib, id, size = 256) {
  const parts = await lib.load(id);
  const scene = new THREE.Scene();
  const group = new THREE.Group();
  for (const p of parts) group.add(new THREE.Mesh(p.geometry, p.material));
  scene.add(group);

  const box = new THREE.Box3().setFromObject(group);
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  const r = Math.max(sphere.radius, 0.05);
  const c = sphere.center;

  scene.add(new THREE.HemisphereLight(0xf4f6ff, 0x5a5650, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(c.x - r * 1.5, c.y + r * 2.5, c.z + r * 2.0);
  key.target.position.copy(c);
  const fill = new THREE.DirectionalLight(0xdfe8ff, 0.7);
  fill.position.set(c.x + r * 2.0, c.y + r * 0.6, c.z - r * 1.0);
  fill.target.position.copy(c);
  scene.add(key, key.target, fill, fill.target);

  // soft contact "shadow" disc so silhouettes read on the dark UI
  const disc = new THREE.Mesh(new THREE.CircleGeometry(r * 0.9, 40),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.18, depthWrite: false }));
  disc.rotation.x = -Math.PI / 2; disc.position.set(c.x, box.min.y + 0.001, c.z);
  scene.add(disc);

  const fov = 26;
  const cam = new THREE.PerspectiveCamera(fov, 1, r / 50, r * 20);
  const dir = new THREE.Vector3(0.62, 0.48, 1.0).normalize();
  const dist = (r / Math.sin(THREE.MathUtils.degToRad(fov / 2))) * 1.04;
  cam.position.copy(c).addScaledVector(dir, dist);
  cam.lookAt(c);

  const rr = renderer(size);
  rr.render(scene, cam);
  const blob = await new Promise((res) => rr.domElement.toBlob(res, 'image/png'));
  disc.geometry.dispose(); disc.material.dispose();
  return blob;
}

/**
 * Render + upload thumbnails for `ids` one by one (throttled so the editor stays responsive).
 * onDone(id, info) receives the server response {thumbnail_url, colors}.
 */
export async function generateThumbnails(lib, ids, { onDone, onProgress, pause = 40, signal } = {}) {
  let n = 0;
  const failed = [];
  for (const id of ids) {
    if (signal?.aborted) break;
    try {
      const blob = await renderThumbnail(lib, id);
      const r = await fetch(`/api/library/thumbs/${encodeURIComponent(id)}`, { method: 'PUT', body: blob, headers: { 'Content-Type': 'image/png' } });
      const info = await r.json();
      if (!r.ok) throw new Error(info.error || r.statusText);
      onDone?.(id, info);
    } catch (e) {
      console.warn('thumbnail failed', id, e);
      failed.push(id);
    }
    n++;
    onProgress?.(n, ids.length);
    await new Promise((res) => setTimeout(res, pause));
  }
  disposeThumbRenderer();
  return { done: n - failed.length, failed };
}
