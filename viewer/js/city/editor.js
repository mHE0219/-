// Editing: selection, gizmo transforms, place / paint / erase brushes, measure, undo/redo, inspector.
import * as THREE from 'three';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { assetSize, assetColor, instanceMatrix } from './assets.js';

const clone = (o) => (o ? JSON.parse(JSON.stringify(o)) : null);
const r3 = (v) => Math.round(v * 1000) / 1000;
let manualCounter = 0;
const newId = () => `manual:${Date.now().toString(36)}${(manualCounter++).toString(36)}`;
export const newBlockId = () => `gb:manual:${Date.now().toString(36)}${(manualCounter++).toString(36)}`;
/** Greybox block ids start with "gb:" (docs/conventions.md); everything else is an asset instance. */
export const isBlockId = (id) => typeof id === 'string' && id.startsWith('gb:');

/** Block -> world matrix without size (pivot = base centre). */
export function blockFrame(b, target = new THREE.Matrix4()) {
  const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), THREE.MathUtils.degToRad(b.rot_y || 0));
  return target.compose(new THREE.Vector3(b.center[0], b.base_y || 0, b.center[1]), q, new THREE.Vector3(1, 1, 1));
}

/** Is (x, z) inside the block footprint (+ margin)? */
export function inBlock(b, x, z, margin = 0) {
  const th = THREE.MathUtils.degToRad(b.rot_y || 0);
  const dx = x - b.center[0], dz = z - b.center[1];
  const u = dx * Math.cos(th) - dz * Math.sin(th); // local X  (local +X -> world (cos, -sin))
  const v = dx * Math.sin(th) + dz * Math.cos(th); // local Z  (local +Z -> world (sin, cos))
  return Math.abs(u) <= b.size[0] / 2 + margin && Math.abs(v) <= b.size[2] / 2 + margin;
}

export class Editor {
  /**
   * host must provide: scene, camera, renderer, domElement, controls (OrbitControls), lib (AssetLibrary),
   *   data (scene.json object), layout, heightAt(x,z), groundMeshes(), instancesGroup,
   *   refresh(assetIds:Set|null), onDirty(), toast(msg, isErr)
   */
  constructor(host) {
    this.h = host;
    this.tool = 'select';
    this.selected = new Set();
    this.paletteSel = [];
    this.undoStack = [];
    this.redoStack = [];
    this.byId = new Map();
    this.blockById = new Map();
    this.pickers = []; // plugin pick providers: (raycaster) => {id, distance} | null
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.brush = { radius: 15, spacing: 6, eraseCategory: 'tree' };

    this.selGroup = new THREE.Group(); this.selGroup.name = 'selection';
    host.scene.add(this.selGroup);

    this.pivot = new THREE.Object3D();
    host.scene.add(this.pivot);
    this.gizmo = new TransformControls(host.camera, host.domElement);
    this.gizmo.setSize(0.9);
    this.gizmoHelper = this.gizmo.getHelper ? this.gizmo.getHelper() : this.gizmo;
    host.scene.add(this.gizmoHelper);
    this.gizmo.addEventListener('dragging-changed', (e) => {
      host.controls.enabled = !e.value;
      if (e.value) this._gizmoStart(); else this._gizmoEnd();
    });
    this.gizmo.addEventListener('objectChange', () => this._gizmoChange());

    // brush ring + measure overlay
    const ring = new THREE.RingGeometry(0.97, 1, 64); ring.rotateX(-Math.PI / 2);
    this.brushRing = new THREE.Mesh(ring, new THREE.MeshBasicMaterial({ color: 0xffcc33, transparent: true, opacity: 0.9, depthTest: false }));
    this.brushRing.renderOrder = 999; this.brushRing.visible = false;
    host.scene.add(this.brushRing);
    this.measure = { pts: [], line: null, label: null };

    this.listeners = { select: [], change: [], tool: [] };
    this._bindPointer();
  }

  /** Subscribe to 'select' (ids:Set), 'change' ({before, after} Maps), 'tool' (name),
   *  'live' (obj being dragged by the gizmo, not yet committed). */
  on(evt, fn) { (this.listeners[evt] = this.listeners[evt] || []).push(fn); return () => { this.listeners[evt] = this.listeners[evt].filter((f) => f !== fn); }; }

  emit(evt, ...args) { for (const fn of this.listeners[evt] || []) { try { fn(...args); } catch (e) { console.error('editor listener', evt, e); } } }

  // ---------------------------------------------------------------- data
  setData(data) {
    this.data = data;
    data.suppressed = data.suppressed || [];
    data.greybox = data.greybox || [];
    data.suppressed_blocks = data.suppressed_blocks || [];
    this.byId = new Map(data.instances.map((i) => [i.id, i]));
    this.blockById = new Map(data.greybox.map((b) => [b.id, b]));
    this.selected.clear();
    this.undoStack = []; this.redoStack = [];
    this._syncSelection();
  }

  /** Instance or greybox block by id. */
  get(id) { return isBlockId(id) ? this.blockById.get(id) : this.byId.get(id); }

  _sup() { return { inst: [...this.data.suppressed], block: [...this.data.suppressed_blocks] }; }

  /** Run `fn(tx)` as one undoable step; works for instances and greybox blocks (ids "gb:...").
   *  tx.set(obj [, {dropArt}]) / tx.add(obj) / tx.remove(id) / tx.drop(id: instance, no suppression).
   *  Setting or removing a block drops its non-locked building instance (rebuilt by the next art pass). */
  mutate(label, fn) {
    const before = new Map();
    const touched = new Set();
    const supBefore = this._sup();
    const touch = (id) => { if (!before.has(id)) before.set(id, clone(this.get(id) || null)); touched.add(id); };
    const dropArt = (blockId) => {
      for (const i of this.data.instances.filter((x) => x.block === blockId && !x.locked)) { touch(i.id); this._del(i.id); }
    };
    const tx = {
      set: (obj, opt = {}) => { touch(obj.id); this._put(obj); if (isBlockId(obj.id) && opt.dropArt !== false) dropArt(obj.id); },
      add: (obj) => { touch(obj.id); this._put(obj); },
      drop: (id) => { if (this.get(id)) { touch(id); this._del(id); } },
      remove: (id) => {
        const cur = this.get(id);
        if (!cur) return;
        touch(id);
        const list = isBlockId(id) ? this.data.suppressed_blocks : this.data.suppressed;
        if (cur.source !== 'manual' && !list.includes(id)) list.push(id);
        this._del(id);
        if (isBlockId(id)) dropArt(id);
      },
    };
    fn(tx);
    if (!touched.size) return;
    const after = new Map([...touched].map((id) => [id, clone(this.get(id) || null)]));
    this.undoStack.push({ label, before, after, supBefore, supAfter: this._sup() });
    if (this.undoStack.length > 200) this.undoStack.shift();
    this.redoStack = [];
    this._afterChange(before, after);
  }

  undo() { this._replay(this.undoStack, this.redoStack, 'before', 'supBefore'); }
  redo() { this._replay(this.redoStack, this.undoStack, 'after', 'supAfter'); }

  _replay(from, to, key, supKey) {
    const e = from.pop();
    if (!e) return;
    for (const [id, st] of e[key]) { if (st) this._put(clone(st)); else this._del(id); }
    const sup = e[supKey];
    this.data.suppressed = [...(Array.isArray(sup) ? sup : sup.inst)];
    if (!Array.isArray(sup)) this.data.suppressed_blocks = [...sup.block];
    to.push(e);
    this._afterChange(e.before, e.after);
    this.h.toast(`${key === 'before' ? '復原' : '重做'}: ${e.label}`);
  }

  _put(obj) {
    const block = isBlockId(obj.id);
    const map = block ? this.blockById : this.byId;
    const list = block ? this.data.greybox : this.data.instances;
    const old = map.get(obj.id);
    if (old) Object.keys(old).forEach((k) => delete old[k]), Object.assign(old, obj);
    else { list.push(obj); map.set(obj.id, obj); }
  }

  _del(id) {
    const block = isBlockId(id);
    const list = block ? this.data.greybox : this.data.instances;
    const i = list.findIndex((x) => x.id === id);
    if (i >= 0) list.splice(i, 1);
    (block ? this.blockById : this.byId).delete(id);
    this.selected.delete(id);
  }

  _afterChange(before, after) {
    const assets = new Set();
    for (const m of [before, after]) for (const st of m.values()) if (st && st.asset) assets.add(st.asset);
    this.h.refresh(assets);
    for (const id of [...this.selected]) if (!this.get(id)) this.selected.delete(id);
    this._syncSelection();
    this.h.onDirty();
    this.emit('change', { before, after });
  }

  // ---------------------------------------------------------------- selection
  select(ids, additive = false) {
    if (!additive) this.selected.clear();
    for (const id of ids) {
      if (additive && this.selected.has(id)) this.selected.delete(id); else this.selected.add(id);
    }
    this._syncSelection();
  }

  _syncSelection() {
    this.selGroup.clear();
    for (const id of this.selected) {
      const obj = this.get(id);
      if (!obj) continue;
      this.selGroup.add(this._helperFor(obj));
    }
    const first = this.get([...this.selected][0]);
    const gizmoTool = { move: 'translate', rotate: 'rotate', scale: 'scale' }[this.tool];
    if (first && gizmoTool) {
      if (isBlockId(first.id)) this.pivot.position.set(first.center[0], first.base_y || 0, first.center[1]);
      else this.pivot.position.fromArray(first.p);
      this.pivot.rotation.set(0, THREE.MathUtils.degToRad(first.rot_y || 0), 0);
      this.pivot.scale.set(1, 1, 1);
      this.gizmo.attach(this.pivot);
      this.gizmo.setMode(gizmoTool);
      this.gizmo.setSpace(gizmoTool === 'translate' ? 'world' : 'local');
      this.gizmo.showX = gizmoTool !== 'rotate';
      this.gizmo.showZ = gizmoTool !== 'rotate';
    } else {
      this.gizmo.detach();
    }
    this.renderInspector();
    this.emit('select', this.selected);
  }

  _helperFor(obj) {
    let box, m;
    if (isBlockId(obj.id)) {
      const [w, h, d] = obj.size;
      box = new THREE.Box3(new THREE.Vector3(-w / 2, 0, -d / 2), new THREE.Vector3(w / 2, h, d / 2));
      m = blockFrame(obj);
    } else {
      const a = this.h.lib.get(obj.asset) || {};
      const b = a.bounds || { min: [-1, 0, -1], max: [1, 2, 1] };
      box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
      m = instanceMatrix(obj);
    }
    const helper = new THREE.Box3Helper(box, isBlockId(obj.id) ? 0x33ddff : 0xffcc33);
    helper.matrixAutoUpdate = false;
    helper.matrix.copy(m);
    helper.material.depthTest = false; helper.renderOrder = 998;
    return helper;
  }

  // ---------------------------------------------------------------- gizmo
  _gizmoStart() {
    this._g0 = {
      pos: this.pivot.position.clone(), yaw: this.pivot.rotation.y, scale: this.pivot.scale.clone(),
      insts: new Map([...this.selected].map((id) => [id, clone(this.get(id))])),
    };
  }

  _gizmoBlock(b, st, mode, g, dPos, dYaw, f) {
    if (mode === 'translate') {
      const x = st.center[0] + dPos.x, z = st.center[1] + dPos.z;
      const yDrag = Math.abs(dPos.y) > 1e-6;
      b.center = [r3(x), r3(z)];
      b.base_y = r3(yDrag ? st.base_y + dPos.y : st.base_y + (this.h.heightAt(x, z) - this.h.heightAt(st.center[0], st.center[1])));
    } else if (mode === 'rotate') {
      const rx = st.center[0] - g.pos.x, rz = st.center[1] - g.pos.z;
      const c = Math.cos(dYaw), s = Math.sin(dYaw);
      b.center = [r3(g.pos.x + c * rx + s * rz), r3(g.pos.z - s * rx + c * rz)];
      b.rot_y = r3((((st.rot_y || 0) + THREE.MathUtils.radToDeg(dYaw)) % 360 + 360) % 360);
    } else if (mode === 'scale') {
      b.size = st.size.map((v, i) => r3(Math.max(v * [f.x, f.y, f.z][i], 0.5))); // greybox: scale edits the size
    }
  }

  _gizmoChange() {
    if (!this._g0) return;
    const g = this._g0;
    const dPos = this.pivot.position.clone().sub(g.pos);
    const dYaw = this.pivot.rotation.y - g.yaw;
    const f = this.pivot.scale.clone().divide(g.scale);
    const mode = this.gizmo.getMode();
    for (const [id, st] of g.insts) {
      const inst = this.get(id);
      if (!inst) continue;
      if (isBlockId(id)) { this._gizmoBlock(inst, st, mode, g, dPos, dYaw, f); this.emit('live', inst); continue; }
      if (mode === 'translate') {
        const x = st.p[0] + dPos.x, z = st.p[2] + dPos.z;
        const yDrag = Math.abs(dPos.y) > 1e-6;
        const y = yDrag ? st.p[1] + dPos.y : st.p[1] + (this.h.heightAt(x, z) - this.h.heightAt(st.p[0], st.p[2]));
        inst.p = [r3(x), r3(y), r3(z)];
      } else if (mode === 'rotate') {
        const rx = st.p[0] - g.pos.x, rz = st.p[2] - g.pos.z;
        const c = Math.cos(dYaw), s = Math.sin(dYaw);
        inst.p = [r3(g.pos.x + c * rx + s * rz), st.p[1], r3(g.pos.z - s * rx + c * rz)];
        inst.rot_y = r3((((st.rot_y || 0) + THREE.MathUtils.radToDeg(dYaw)) % 360 + 360) % 360);
      } else if (mode === 'scale') {
        inst.scale = st.scale.map((v, i) => r3(Math.max(v * [f.x, f.y, f.z][i], 0.01)));
      }
      this.h.updateInstance(inst);
    }
    this._syncHelpersOnly();
  }

  _gizmoEnd() {
    const g = this._g0;
    this._g0 = null;
    if (!g) return;
    const finals = [...g.insts.keys()].map((id) => clone(this.get(id))).filter(Boolean);
    // restore originals then re-apply as one undoable mutation
    for (const st of g.insts.values()) this._put(clone(st));
    this.mutate(this.gizmo.getMode(), (tx) => {
      for (const inst of finals) { inst.locked = inst.source === 'manual' ? inst.locked : true; tx.set(inst); }
    });
  }

  _syncHelpersOnly() {
    let i = 0;
    for (const id of this.selected) {
      const obj = this.get(id); const h = this.selGroup.children[i++];
      if (!obj || !h) continue;
      if (isBlockId(id)) {
        const [w, hh, d] = obj.size;
        h.box.min.set(-w / 2, 0, -d / 2); h.box.max.set(w / 2, hh, d / 2);
        h.matrix.copy(blockFrame(obj));
      } else h.matrix.copy(instanceMatrix(obj));
    }
  }

  // ---------------------------------------------------------------- tools
  setTool(t) {
    this.tool = t;
    this.brushRing.visible = false;
    if (t !== 'measure') this._clearMeasure();
    this._syncSelection();
    this.h.onTool(t);
    this.emit('tool', t);
  }

  setPalette(ids) { this.paletteSel = ids; }

  _bindPointer() {
    const el = this.h.domElement;
    let down = null; let painting = false; let paintTx = null; let lastPaint = 0;
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      down = { x: e.clientX, y: e.clientY };
      if ((this.tool === 'paint' || this.tool === 'erase') && !e.altKey) {
        painting = true; this.h.controls.enabled = false;
        paintTx = { added: [], removed: [] };
        this._brushStep(e, paintTx); lastPaint = performance.now();
      }
    });
    el.addEventListener('pointermove', (e) => {
      const hit = this._groundHit(e);
      this.h.onCursor(hit);
      if (this.tool === 'paint' || this.tool === 'erase') {
        this.brushRing.visible = !!hit;
        if (hit) {
          this.brushRing.position.set(hit.x, hit.y + 0.3, hit.z);
          this.brushRing.scale.setScalar(this.brush.radius);
          this.brushRing.material.color.set(this.tool === 'erase' ? 0xff5555 : 0xffcc33);
        }
        if (painting && performance.now() - lastPaint > 60) { this._brushStep(e, paintTx); lastPaint = performance.now(); }
      }
    });
    el.addEventListener('pointerup', (e) => {
      if (painting) {
        painting = false; this.h.controls.enabled = true;
        this._brushCommit(paintTx); paintTx = null;
      }
      if (!down || e.button !== 0) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4;
      down = null;
      if (moved || this.gizmo.dragging || this.gizmo.axis) return;
      this._click(e);
    });
  }

  _ndc(e) {
    const r = this.h.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.h.camera);
  }

  _groundHit(e) {
    this._ndc(e);
    const hits = this.raycaster.intersectObjects(this.h.groundMeshes(), false);
    return hits.length ? hits[0].point : null;
  }

  /** Register a pick provider (e.g. the greybox layer): fn(raycaster) -> {id, distance} | null */
  addPicker(fn) { this.pickers.push(fn); }

  _pickInstance(e) {
    this._ndc(e);
    const shown = (o) => { for (; o; o = o.parent) if (!o.visible) return false; return true; };
    let best = null;
    for (const h of this.raycaster.intersectObject(this.h.instancesGroup, true)) {
      const b = h.object.userData.batch;
      if (b && h.instanceId != null && h.instanceId < b.ids.length && shown(h.object)) { best = { id: b.ids[h.instanceId], distance: h.distance }; break; }
    }
    for (const p of this.pickers) {
      const r = p(this.raycaster);
      if (r && (!best || r.distance < best.distance)) best = r;
    }
    return best ? best.id : null;
  }

  _click(e) {
    if (this.tool === 'place') return this._place(e);
    if (this.tool === 'measure') return this._measureClick(e);
    if (this.tool === 'paint' || this.tool === 'erase' || this.tool === 'block') return;
    const id = this._pickInstance(e);
    if (id) this.select([id], e.shiftKey);
    else if (!e.shiftKey) this.select([]);
  }

  _randomAsset(category) {
    const pool = this.paletteSel.map((id) => this.h.lib.get(id)).filter((a) => a && (!category || a.category === category));
    if (!pool.length) return null;
    return pool[Math.floor(Math.random() * pool.length)];
  }

  _newInstance(a, x, z, rotY) {
    const isVeg = a.category === 'tree';
    const [lo, hi] = a.scale_range || [0.9, 1.1];
    const s = isVeg ? lo + Math.random() * (hi - lo) : 1;
    return {
      id: newId(), asset: a.id, category: a.category,
      p: [r3(x), r3(this.h.heightAt(x, z)), r3(z)],
      rot_y: r3(rotY ?? (isVeg ? Math.random() * 360 : 0)),
      scale: [r3(s), r3(s), r3(s)], rule: 'manual', feature: null, source: 'manual', locked: true,
    };
  }

  _place(e) {
    const a = this._randomAsset();
    if (!a) return this.h.toast('先在左側素材庫選一個素材 (Pick an asset in the library first)', true);
    const hit = this._groundHit(e);
    if (!hit) return;
    const inst = this._newInstance(a, hit.x, hit.z);
    this.mutate('place ' + a.id, (tx) => tx.add(inst));
  }

  _insideBuilding(x, z) {
    if (this.data && this.data.greybox && this.data.greybox.length) {
      return this.data.greybox.some((b) => inBlock(b, x, z, 0.5));
    }
    for (const b of this.h.layout.buildings) {
      const o = b.obb; const R = Math.hypot(o.size[0], o.size[1]) / 2;
      if (Math.abs(x - o.center[0]) > R || Math.abs(z - o.center[1]) > R) continue;
      let inside = false; const ring = b.outer;
      for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const [xi, zi] = ring[i], [xj, zj] = ring[j];
        if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
      }
      if (inside) return true;
    }
    return false;
  }

  _brushStep(e, tx) {
    const hit = this._groundHit(e);
    if (!hit) return;
    const R = this.brush.radius;
    if (this.tool === 'erase') {
      const cat = this.brush.eraseCategory;
      for (const inst of [...this.data.instances]) {
        if (cat !== '*' && inst.category !== cat) continue;
        if (Math.hypot(inst.p[0] - hit.x, inst.p[2] - hit.z) <= R && !tx.removed.find((x) => x.id === inst.id)) {
          tx.removed.push(clone(inst));
          this._del(inst.id);
        }
      }
      this.h.refresh(new Set(tx.removed.map((i) => i.asset)));
      return;
    }
    const a0 = this._randomAsset();
    if (!a0) { this.h.toast('筆刷：先在素材庫選擇素材 (Shift 可多選)', true); return; }
    const cat = a0.category;
    const sp = this.brush.spacing;
    const near = this.data.instances.filter((i) => i.category === cat && Math.hypot(i.p[0] - hit.x, i.p[2] - hit.z) < R + sp);
    const tries = Math.ceil((Math.PI * R * R) / (sp * sp)) * 2;
    const touched = new Set();
    for (let k = 0; k < tries; k++) {
      const ang = Math.random() * Math.PI * 2, rr = Math.sqrt(Math.random()) * R;
      const x = hit.x + Math.cos(ang) * rr, z = hit.z + Math.sin(ang) * rr;
      const [x0, z0, x1, z1] = this.h.layout.box.rect;
      if (x < x0 || x > x1 || z < z0 || z > z1) continue;
      if (near.some((i) => Math.hypot(i.p[0] - x, i.p[2] - z) < sp)) continue;
      if (cat !== 'building' && this._insideBuilding(x, z)) continue;
      const a = this._randomAsset(cat) || a0;
      const inst = this._newInstance(a, x, z);
      this._put(inst); near.push(inst); tx.added.push(inst.id); touched.add(a.id);
    }
    if (touched.size) this.h.refresh(touched);
  }

  _brushCommit(tx) {
    if (!tx) return;
    // undo entry from the accumulated stroke
    const before = new Map(), after = new Map();
    for (const id of tx.added) { before.set(id, null); after.set(id, clone(this.byId.get(id))); }
    const supBefore = [...this.data.suppressed];
    for (const inst of tx.removed) {
      if (before.has(inst.id)) { before.delete(inst.id); after.delete(inst.id); continue; } // painted then erased
      before.set(inst.id, inst); after.set(inst.id, null);
      if (inst.source !== 'manual' && !this.data.suppressed.includes(inst.id)) this.data.suppressed.push(inst.id);
    }
    if (!before.size) return;
    this.undoStack.push({ label: this.tool === 'erase' ? `erase ${tx.removed.length}` : `paint ${tx.added.length}`, before, after, supBefore, supAfter: [...this.data.suppressed] });
    this.redoStack = [];
    this.h.onDirty();
    this.renderInspector();
  }

  // ---------------------------------------------------------------- measure
  _clearMeasure() {
    const m = this.measure;
    if (m.line) { this.h.scene.remove(m.line); m.line.geometry.dispose(); }
    m.line = null; m.pts = [];
    if (m.label) { m.label.remove(); m.label = null; }
  }

  _measureClick(e) {
    const hit = this._groundHit(e) || null;
    const m = this.measure;
    if (!hit) return;
    if (m.pts.length >= 2) this._clearMeasure();
    m.pts.push(hit.clone().add(new THREE.Vector3(0, 0.4, 0)));
    if (m.pts.length === 2) {
      const g = new THREE.BufferGeometry().setFromPoints(m.pts);
      m.line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0x00e5ff, depthTest: false }));
      m.line.renderOrder = 999;
      this.h.scene.add(m.line);
      const d = m.pts[0].distanceTo(m.pts[1]);
      const dh = Math.hypot(m.pts[0].x - m.pts[1].x, m.pts[0].z - m.pts[1].z);
      m.label = document.createElement('div');
      m.label.className = 'label3d';
      m.label.textContent = `${dh.toFixed(2)} m (水平)  ·  ${d.toFixed(2)} m (3D)`;
      this.h.domElement.parentElement.appendChild(m.label);
      this.h.toast(`距離 ${dh.toFixed(2)} m`);
    }
  }

  updateOverlays() {
    const m = this.measure;
    if (m.label && m.pts.length === 2) {
      const mid = m.pts[0].clone().lerp(m.pts[1], 0.5).project(this.h.camera);
      const r = this.h.domElement.getBoundingClientRect();
      m.label.style.left = `${(mid.x * 0.5 + 0.5) * r.width}px`;
      m.label.style.top = `${(-mid.y * 0.5 + 0.5) * r.height}px`;
      m.label.style.display = mid.z < 1 ? '' : 'none';
    }
  }

  // ---------------------------------------------------------------- commands
  deleteSelected() {
    const ids = [...this.selected];
    if (!ids.length) return;
    this.mutate(`delete ${ids.length}`, (tx) => ids.forEach((id) => tx.remove(id)));
  }

  duplicateSelected() {
    const src = [...this.selected].map((id) => this.get(id)).filter(Boolean);
    if (!src.length) return;
    const copies = src.map((s) => (isBlockId(s.id)
      ? { ...clone(s), id: newBlockId(), source: 'manual', feature: null, locked: true, center: [r3(s.center[0] + s.size[0] + 4), s.center[1]] }
      : { ...clone(s), id: newId(), source: 'manual', rule: 'manual', locked: true, block: undefined, p: [s.p[0] + 3, s.p[1], s.p[2] + 3] }));
    this.mutate(`duplicate ${copies.length}`, (tx) => copies.forEach((c) => tx.add(c)));
    this.select(copies.map((c) => c.id));
  }

  editSelected(label, fn) {
    const ids = [...this.selected];
    this.mutate(label, (tx) => {
      for (const id of ids) {
        const obj = clone(this.get(id));
        if (!obj) continue;
        if (fn(obj, tx) === false) continue;
        tx.set(obj);
      }
    });
  }

  toggleLock() {
    const any = [...this.selected].some((id) => !this.get(id)?.locked);
    this.editSelected(any ? 'lock' : 'unlock', (i) => { i.locked = any; });
  }

  focusSelected() {
    const first = this.get([...this.selected][0]);
    if (!first) return;
    let s, t;
    if (isBlockId(first.id)) {
      s = Math.max(...first.size);
      t = new THREE.Vector3(first.center[0], (first.base_y || 0) + first.size[1] * 0.4, first.center[1]);
    } else {
      const a = this.h.lib.get(first.asset);
      s = Math.max(...assetSize(a || {}).map((v, k) => v * (first.scale?.[k] || 1)));
      t = new THREE.Vector3(first.p[0], first.p[1] + s * 0.4, first.p[2]);
    }
    const dir = this.h.camera.position.clone().sub(this.h.controls.target).normalize();
    this.h.controls.target.copy(t);
    this.h.camera.position.copy(t).addScaledVector(dir, Math.max(s * 2.5, 12));
  }

  // ---------------------------------------------------------------- inspector
  renderInspector() {
    const el = document.getElementById('inspector');
    if (!el) return;
    const ids = [...this.selected];
    if (!ids.length) { el.className = 'muted'; el.textContent = '未選取 — 點擊場景中的物件 (Shift 多選)'; return; }
    el.className = '';
    const lib = this.h.lib;
    const nBlocks = ids.filter(isBlockId).length;
    if (nBlocks === ids.length) return this._blockInspector(el, ids);
    if (nBlocks) {
      el.innerHTML = `<div><b>${ids.length}</b> 個物件（${nBlocks} 灰盒 + ${ids.length - nBlocks} 素材）</div>
        <div class="actions"><button id="insLock">🔒 鎖定/解鎖</button><button id="insDel" class="danger">🗑 刪除</button></div>`;
      document.getElementById('insLock').addEventListener('click', () => this.toggleLock());
      document.getElementById('insDel').addEventListener('click', () => this.deleteSelected());
      return;
    }
    const first = this.byId.get(ids[0]);
    const sameCat = lib.lib.assets.filter((a) => a.category === first.category);
    const opts = sameCat.map((a) => `<option value="${a.id}" ${a.id === first.asset ? 'selected' : ''}>${a.label || a.id}</option>`).join('');
    if (ids.length > 1) {
      el.innerHTML = `<div><b>${ids.length}</b> 個物件</div>
        <div class="kv"><span>換素材</span><select id="insAsset"><option value="">—</option>${opts}</select></div>
        <div class="actions"><button id="insLock">🔒 鎖定/解鎖</button><button id="insDrop">⤓ 貼地</button><button id="insDup">⧉ 複製</button><button id="insDel" class="danger">🗑 刪除</button></div>`;
    } else {
      const a = lib.get(first.asset) || {};
      const sz = assetSize(a).map((v, i) => (v * (first.scale?.[i] || 1)).toFixed(1));
      const num = (id, v, step = 0.1) => `<input id="${id}" type="number" step="${step}" value="${v}">`;
      el.innerHTML = `
        <div class="kv">
          <span>id</span><span class="mono small">${first.id}</span>
          <span>素材</span><select id="insAsset">${opts}</select>
          <span>位置 P</span><div class="vec">${num('insPx', first.p[0])}${num('insPy', first.p[1])}${num('insPz', first.p[2])}</div>
          <span>旋轉 Y°</span>${num('insRot', first.rot_y || 0, 1)}
          <span>縮放 S</span><div class="vec">${num('insSx', first.scale[0], 0.05)}${num('insSy', first.scale[1], 0.05)}${num('insSz', first.scale[2], 0.05)}</div>
          <span>實際尺寸</span><span class="small">${sz.join(' × ')} m (W×H×D)</span>
          ${first.target ? `<span>目標尺寸</span><span class="small">${first.target.size.map((v) => v.toFixed(1)).join(' × ')} m</span>` : ''}
          <span>來源</span><span class="small">${first.source}${first.rule ? ' · ' + first.rule : ''}${first.feature ? ' · OSM ' + first.feature : ''}</span>
          <span>鎖定</span><label><input id="insLocked" type="checkbox" ${first.locked ? 'checked' : ''}> re-scatter 時保留</label>
          ${first.fit_warning ? `<span>⚠</span><span class="small">${first.fit_warning}</span>` : ''}
        </div>
        <div class="actions"><button id="insDrop">⤓ 貼地</button><button id="insFocus">◎ 對焦 F</button><button id="insDup">⧉ 複製</button><button id="insDel" class="danger">🗑 刪除</button></div>`;
      const bindNum = (id, fn) => document.getElementById(id).addEventListener('change', (e) => this.editSelected('edit', (i) => { fn(i, parseFloat(e.target.value) || 0); i.locked = true; }));
      bindNum('insPx', (i, v) => { i.p[0] = v; });
      bindNum('insPy', (i, v) => { i.p[1] = v; });
      bindNum('insPz', (i, v) => { i.p[2] = v; });
      bindNum('insRot', (i, v) => { i.rot_y = ((v % 360) + 360) % 360; });
      bindNum('insSx', (i, v) => { i.scale[0] = Math.max(v, 0.01); });
      bindNum('insSy', (i, v) => { i.scale[1] = Math.max(v, 0.01); });
      bindNum('insSz', (i, v) => { i.scale[2] = Math.max(v, 0.01); });
      document.getElementById('insLocked').addEventListener('change', (e) => this.editSelected('lock', (i) => { i.locked = e.target.checked; }));
      document.getElementById('insFocus').addEventListener('click', () => this.focusSelected());
    }
    const q = (id) => document.getElementById(id);
    q('insAsset')?.addEventListener('change', (e) => {
      const nid = e.target.value; if (!nid) return;
      const na = lib.get(nid);
      this.editSelected('swap asset', (i, tx) => {
        const oa = lib.get(i.asset);
        if (oa && na && oa.category === 'building' && i.target) {
          // keep the real-world target size when swapping buildings
          const [w, hh, d] = assetSize(na); const t = i.target.size;
          i.scale = [r3(t[0] / w), r3(t[1] / hh), r3(t[2] / d)];
        }
        i.asset = nid;
        const blk = i.block && this.blockById.get(i.block);
        if (blk) {
          // the greybox block remembers the choice -> every later art pass uses it
          const nb = clone(blk); nb.asset_lock = nid; nb.locked = true;
          tx.set(nb, { dropArt: false });
        } else i.locked = true;
      });
    });
    q('insDel')?.addEventListener('click', () => this.deleteSelected());
    q('insDup')?.addEventListener('click', () => this.duplicateSelected());
    q('insLock')?.addEventListener('click', () => this.toggleLock());
    q('insDrop')?.addEventListener('click', () => this.editSelected('drop to ground', (i) => { i.p[1] = r3(this.h.heightAt(i.p[0], i.p[2])); }));
  }
}

// ---------------------------------------------------------------- greybox inspector
Editor.prototype._blockInspector = function _blockInspector(el, ids) {
  const lib = this.h.lib;
  const first = this.blockById.get(ids[0]);
  const bOpts = lib.lib.assets.filter((a) => a.category === 'building')
    .map((a) => `<option value="${a.id}" ${a.id === first.asset_lock ? 'selected' : ''}>${a.label || a.id}</option>`).join('');
  const q = (id) => document.getElementById(id);
  if (ids.length > 1) {
    el.innerHTML = `<div><b>${ids.length}</b> 個灰盒 blocks</div>
      <div class="kv"><span>指定素材</span><select id="gbAsset"><option value="">—</option><option value="__auto">自動 (檢索)</option>${bOpts}</select>
      <span>高度 H</span><input id="gbH" type="number" step="0.5" placeholder="不變"></div>
      <div class="actions"><button id="insLock">🔒 鎖定/解鎖</button><button id="insDrop">⤓ 貼地</button><button id="insDup">⧉ 複製</button><button id="insDel" class="danger">🗑 刪除</button></div>`;
  } else {
    const num = (id, v, step = 0.1) => `<input id="${id}" type="number" step="${step}" value="${v}">`;
    const art = this.data.instances.find((i) => i.block === first.id);
    el.innerHTML = `
      <div class="kv">
        <span>灰盒 id</span><span class="mono small">${first.id}</span>
        <span>尺寸 W·H·D</span><div class="vec">${num('gbW', first.size[0], 0.5)}${num('gbH', first.size[1], 0.5)}${num('gbD', first.size[2], 0.5)}</div>
        <span>中心 X·Z</span><div class="vec">${num('gbX', first.center[0])}${num('gbZ', first.center[1])}</div>
        <span>底 Y</span>${num('gbY', first.base_y || 0)}
        <span>旋轉 Y°</span>${num('gbRot', first.rot_y || 0, 1)}
        <span>用途 kind</span><input id="gbKind" value="${first.kind || ''}">
        <span>tags</span><input id="gbTags" value="${(first.tags || []).join(', ')}" title="逗號分隔，給素材檢索用">
        <span>指定素材</span><select id="gbAsset"><option value="__auto">自動 (檢索)</option>${bOpts}</select>
        <span>樓層</span><span class="small">≈ ${Math.max(1, Math.round(first.size[1] / 3.2))} 層 (3.2 m)</span>
        <span>來源</span><span class="small">${first.source}${first.feature ? ' · OSM ' + first.feature : ''}</span>
        <span>鎖定</span><label><input id="insLocked" type="checkbox" ${first.locked ? 'checked' : ''}> 重建時保留</label>
        <span>素材</span><span class="small">${art ? `${art.asset}${art.locked ? ' 🔒' : ''}` : '— (按 ▶ Art pass 生成)'}</span>
      </div>
      <div class="actions"><button id="insDrop">⤓ 貼地</button><button id="insFocus">◎ 對焦 F</button><button id="insDup">⧉ 複製</button><button id="insDel" class="danger">🗑 刪除</button></div>`;
    const bind = (id, fn) => q(id).addEventListener('change', (e) => this.editSelected('edit block', (b) => { fn(b, e.target.value); b.locked = true; }));
    const f = (v) => parseFloat(v) || 0;
    bind('gbW', (b, v) => { b.size[0] = Math.max(f(v), 0.5); });
    bind('gbD', (b, v) => { b.size[2] = Math.max(f(v), 0.5); });
    bind('gbX', (b, v) => { b.center[0] = f(v); });
    bind('gbZ', (b, v) => { b.center[1] = f(v); });
    bind('gbY', (b, v) => { b.base_y = f(v); });
    bind('gbRot', (b, v) => { b.rot_y = ((f(v) % 360) + 360) % 360; });
    bind('gbKind', (b, v) => { b.kind = v.trim() || 'yes'; });
    bind('gbTags', (b, v) => { b.tags = v.split(',').map((t) => t.trim()).filter(Boolean); });
    q('insLocked').addEventListener('change', (e) => this.editSelected('lock', (b) => { b.locked = e.target.checked; }));
    q('insFocus').addEventListener('click', () => this.focusSelected());
  }
  q('gbH').addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v) && v > 0) this.editSelected('block height', (b) => { b.size[1] = v; b.locked = true; });
  });
  q('gbAsset').addEventListener('change', (e) => {
    const v = e.target.value; if (!v) return;
    this.editSelected('lock asset', (b) => { b.asset_lock = v === '__auto' ? null : v; b.locked = true; });
  });
  q('insDel').addEventListener('click', () => this.deleteSelected());
  q('insDup').addEventListener('click', () => this.duplicateSelected());
  q('insLock')?.addEventListener('click', () => this.toggleLock());
  q('insDrop').addEventListener('click', () => this.editSelected('drop to ground', (b) => {
    const th = THREE.MathUtils.degToRad(b.rot_y || 0);
    const pts = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([su, sv]) => [
      b.center[0] + su * b.size[0] / 2 * Math.cos(th) + sv * b.size[2] / 2 * Math.sin(th),
      b.center[1] - su * b.size[0] / 2 * Math.sin(th) + sv * b.size[2] / 2 * Math.cos(th)]);
    b.base_y = r3(Math.min(...pts.map(([x, z]) => this.h.heightAt(x, z))));
  }));
};

export function paletteHTML(lib) {
  const cats = { building: '建築 Buildings', tree: '植被 Vegetation', prop: '道具 Props' };
  let html = '';
  for (const [cat, title] of Object.entries(cats)) {
    const items = lib.assets.filter((a) => a.category === cat);
    if (!items.length) continue;
    html += `<div class="cat">${title}</div>`;
    for (const a of items) {
      const [w, h, d] = assetSize(a);
      html += `<div class="item" data-id="${a.id}" title="${a.id}\n${(a.tags || []).join(', ')}">
        <span class="sw" style="background:${assetColor(a)}"></span>${a.label || a.id}
        <span class="dim">${w.toFixed(0)}×${h.toFixed(0)}×${d.toFixed(0)}</span></div>`;
    }
  }
  return html;
}
