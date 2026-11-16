// Asset library loading (normalised to the GeoBox standard) + instanced batches.
import * as THREE from 'three';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';

const CAT_COLORS = { building: '#d8d0c4', tree: '#4f7d3a', prop: '#5a6068' };

export function assetColor(a) {
  if (a.preview_color) return a.preview_color;
  const m = a.materials && Object.values(a.materials);
  if (m && m.length) return a.category === 'tree' ? (a.materials.leaves || m[0]) : (a.materials.wall || m[0]);
  return CAT_COLORS[a.category] || '#bbbbbb';
}

export function assetSize(a) {
  const b = a.bounds;
  if (!b) return [10, 10, 10];
  return [b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]].map((v) => Math.max(v, 1e-3));
}

/** Matrix taking raw file coordinates to standard space (metres, +Y up, base-centre pivot). */
function normalizeMatrix(a) {
  const nz = a.normalize || {};
  const s = nz.scale ?? 1;
  const m = new THREE.Matrix4();
  if (nz.offset) m.makeTranslation(nz.offset[0], nz.offset[1], nz.offset[2]);
  if (nz.up === '+Z' || nz.up === 'Z' || nz.up === 'z') m.multiply(new THREE.Matrix4().makeRotationX(-Math.PI / 2));
  if (nz.up === '-Z') m.multiply(new THREE.Matrix4().makeRotationX(Math.PI / 2));
  m.multiply(new THREE.Matrix4().makeScale(s, s, s));
  return m;
}

/** Split a geometry with groups into one geometry per material index. */
function splitByGroups(geometry, materials) {
  if (!Array.isArray(materials) || !geometry.groups.length) return [{ geometry, material: Array.isArray(materials) ? materials[0] : materials }];
  const out = [];
  const g = geometry.index ? geometry.toNonIndexed() : geometry;
  for (const grp of g.groups) {
    const sub = new THREE.BufferGeometry();
    for (const [name, attr] of Object.entries(g.attributes)) {
      const n = attr.itemSize;
      sub.setAttribute(name, new THREE.BufferAttribute(attr.array.slice(grp.start * n, (grp.start + grp.count) * n), n, attr.normalized));
    }
    out.push({ geometry: sub, material: materials[grp.materialIndex ?? 0] });
  }
  return out;
}

export class AssetLibrary {
  constructor(lib) {
    this.lib = lib;
    this.byId = new Map(lib.assets.map((a) => [a.id, a]));
    this.cache = new Map();
    this.gltf = new GLTFLoader();
    const draco = new DRACOLoader();
    draco.setDecoderPath('./vendor/three/examples/jsm/libs/draco/gltf/');
    this.gltf.setDRACOLoader(draco);
    this.obj = new OBJLoader();
    this.fbx = new FBXLoader();
  }

  get(id) { return this.byId.get(id); }

  /** -> Promise<[{geometry, material}]> in standard space */
  load(id) {
    if (!this.cache.has(id)) this.cache.set(id, this._load(id));
    return this.cache.get(id);
  }

  async _load(id) {
    const a = this.byId.get(id);
    if (!a) return this._fallback({ id, category: 'prop' });
    if (!a.web_url) return this._fallback(a);
    try {
      const ext = a.web_url.split('.').pop().toLowerCase();
      let root;
      if (ext === 'obj') root = await this.obj.loadAsync(a.web_url);
      else if (ext === 'glb' || ext === 'gltf') root = (await this.gltf.loadAsync(a.web_url)).scene;
      else if (ext === 'fbx') root = await this.fbx.loadAsync(a.web_url);
      else return this._fallback(a);
      root.updateMatrixWorld(true);
      const norm = normalizeMatrix(a);
      const parts = [];
      root.traverse((o) => {
        if (!o.isMesh) return;
        const geo = o.geometry.clone();
        geo.applyMatrix4(new THREE.Matrix4().multiplyMatrices(norm, o.matrixWorld));
        for (const p of splitByGroups(geo, o.material)) {
          p.material = this._material(a, p.material);
          if (!p.geometry.attributes.normal) p.geometry.computeVertexNormals();
          p.geometry.computeBoundingSphere();
          parts.push(p);
        }
      });
      if (!parts.length) return this._fallback(a);
      return parts;
    } catch (err) {
      console.warn('asset load failed', id, err);
      return this._fallback(a);
    }
  }

  _material(a, src) {
    const name = src && src.name;
    const hex = a.materials && name && a.materials[name];
    if (hex || !src || src.type === 'MeshPhongMaterial' && a.proxy) {
      return new THREE.MeshStandardMaterial({ color: hex || assetColor(a), roughness: 0.85, metalness: 0.0, name: name || '' });
    }
    // keep authored PBR materials (glTF); upgrade Phong/Lambert from OBJ/FBX
    if (src.isMeshStandardMaterial) return src;
    return new THREE.MeshStandardMaterial({ color: src.color || assetColor(a), map: src.map || null, roughness: 0.85, name: name || '' });
  }

  _fallback(a) {
    const [w, h, d] = assetSize(a);
    const geo = a.category === 'tree' ? new THREE.ConeGeometry(Math.max(w, d) / 2, h, 8) : new THREE.BoxGeometry(w, h, d);
    geo.translate(0, h / 2, 0);
    geo.computeBoundingSphere();
    return [{ geometry: geo, material: new THREE.MeshStandardMaterial({ color: assetColor(a), roughness: 0.9 }) }];
  }
}

const _q = new THREE.Quaternion();
const _p = new THREE.Vector3();
const _s = new THREE.Vector3();
const _y = new THREE.Vector3(0, 1, 0);

export function instanceMatrix(inst, target = new THREE.Matrix4()) {
  _p.fromArray(inst.p);
  _q.setFromAxisAngle(_y, THREE.MathUtils.degToRad(inst.rot_y || 0));
  _s.fromArray(inst.scale || [1, 1, 1]);
  return target.compose(_p, _q, _s);
}

/** One InstancedMesh per asset part; `ids[i]` = scene instance id of instance i. */
export class AssetBatch {
  constructor(assetId, parts, group) {
    this.assetId = assetId;
    this.parts = parts;
    this.group = group;
    this.meshes = [];
    this.ids = [];
    this.capacity = 0;
  }

  rebuild(instances, shadows) {
    this.ids = instances.map((i) => i.id);
    const need = Math.max(instances.length, 1);
    if (need > this.capacity) {
      for (const m of this.meshes) { this.group.remove(m); m.dispose(); }
      this.capacity = Math.ceil(need * 1.25) + 8;
      this.meshes = this.parts.map((p) => {
        const m = new THREE.InstancedMesh(p.geometry, p.material, this.capacity);
        m.userData.batch = this;
        m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        this.group.add(m);
        return m;
      });
    }
    const mat = new THREE.Matrix4();
    for (const m of this.meshes) {
      m.count = instances.length;
      m.castShadow = shadows; m.receiveShadow = shadows;
    }
    instances.forEach((inst, i) => {
      instanceMatrix(inst, mat);
      for (const m of this.meshes) m.setMatrixAt(i, mat);
    });
    this._commit();
  }

  updateOne(index, inst) {
    const mat = instanceMatrix(inst);
    for (const m of this.meshes) m.setMatrixAt(index, mat);
    this._commit();
  }

  _commit() {
    for (const m of this.meshes) {
      m.instanceMatrix.needsUpdate = true;
      m.boundingSphere = null; m.boundingBox = null;
      if (m.count > 0) m.computeBoundingSphere();
    }
  }

  dispose() {
    for (const m of this.meshes) { this.group.remove(m); m.dispose(); }
    this.meshes = [];
  }
}
