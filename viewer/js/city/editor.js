// Editing: selection, gizmo transforms, place / paint / erase brushes, measure, undo/redo, inspector.
import * as THREE from 'three';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { assetSize, assetColor, instanceMatrix } from './assets.js';

const clone = (o) => (o ? JSON.parse(JSON.stringify(o)) : null);
const r3 = (v) => Math.round(v * 1000) / 1000;
let manualCounter = 0;
const newId = () => `manual:${Date.now().toString(36)}${(manualCounter++).toString(36)}`;

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

    this._bindPointer();
  }

  // ---------------------------------------------------------------- data
  setData(data) {
    this.data = data;
    this.byId = new Map(data.instances.map((i) => [i.id, i]));
    data.suppressed = data.suppressed || [];
    this.selected.clear();
    this.undoStack = []; this.redoStack = [];
    this._syncSelection();
  }

  /** Run `fn(tx)` as one undoable step. tx.set(inst) / tx.remove(id) / tx.add(inst). */
  mutate(label, fn) {
    const before = new Map();
    const touched = new Set();
    const supBefore = [...this.data.suppressed];
    const touch = (id) => { if (!before.has(id)) before.set(id, clone(this.byId.get(id) || null)); touched.add(id); };
    const tx = {
      set: (inst) => { touch(inst.id); this._put(inst); },
      add: (inst) => { touch(inst.id); this._put(inst); },
      remove: (id) => {
        const cur = this.byId.get(id);
        if (!cur) return;
        touch(id);
        if (cur.source !== 'manual' && !this.data.suppressed.includes(id)) this.data.suppressed.push(id);
        this._del(id);
      },
    };
    fn(tx);
    if (!touched.size) return;
    const after = new Map([...touched].map((id) => [id, clone(this.byId.get(id) || null)]));
    this.undoStack.push({ label, before, after, supBefore, supAfter: [...this.data.suppressed] });
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
    this.data.suppressed = [...e[supKey]];
    to.push(e);
    this._afterChange(e.before, e.after);
    this.h.toast(`${key === 'before' ? '復原' : '重做'}: ${e.label}`);
  }

  _put(inst) {
    const old = this.byId.get(inst.id);
    if (old) Object.keys(old).forEach((k) => delete old[k]), Object.assign(old, inst);
    else { this.data.instances.push(inst); this.byId.set(inst.id, inst); }
  }

  _del(id) {
    const i = this.data.instances.findIndex((x) => x.id === id);
    if (i >= 0) this.data.instances.splice(i, 1);
    this.byId.delete(id);
    this.selected.delete(id);
  }

  _afterChange(before, after) {
    const assets = new Set();
    for (const m of [before, after]) for (const st of m.values()) if (st) assets.add(st.asset);
    this.h.refresh(assets);
    for (const id of [...this.selected]) if (!this.byId.has(id)) this.selected.delete(id);
    this._syncSelection();
    this.h.onDirty();
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
      const inst = this.byId.get(id);
      if (!inst) continue;
      const a = this.h.lib.get(inst.asset) || {};
      const b = a.bounds || { min: [-1, 0, -1], max: [1, 2, 1] };
      const box = new THREE.Box3(new THREE.Vector3(...b.min), new THREE.Vector3(...b.max));
      const helper = new THREE.Box3Helper(box, 0xffcc33);
      helper.matrixAutoUpdate = false;
      helper.matrix.copy(instanceMatrix(inst));
      helper.material.depthTest = false; helper.renderOrder = 998;
      this.selGroup.add(helper);
    }
    const first = this.byId.get([...this.selected][0]);
    const gizmoTool = { move: 'translate', rotate: 'rotate', scale: 'scale' }[this.tool];
    if (first && gizmoTool) {
      this.pivot.position.fromArray(first.p);
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
  }

  // ---------------------------------------------------------------- gizmo
  _gizmoStart() {
    this._g0 = {
      pos: this.pivot.position.clone(), yaw: this.pivot.rotation.y, scale: this.pivot.scale.clone(),
      insts: new Map([...this.selected].map((id) => [id, clone(this.byId.get(id))])),
    };
  }

  _gizmoChange() {
    if (!this._g0) return;
    const g = this._g0;
    const dPos = this.pivot.position.clone().sub(g.pos);
    const dYaw = this.pivot.rotation.y - g.yaw;
    const f = this.pivot.scale.clone().divide(g.scale);
    const mode = this.gizmo.getMode();
    for (const [id, st] of g.insts) {
      const inst = this.byId.get(id);
      if (!inst) continue;
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
    const finals = [...g.insts.keys()].map((id) => clone(this.byId.get(id))).filter(Boolean);
    // restore originals then re-apply as one undoable mutation
    for (const st of g.insts.values()) this._put(clone(st));
    this.mutate(this.gizmo.getMode(), (tx) => {
      for (const inst of finals) { inst.locked = inst.source === 'manual' ? inst.locked : true; tx.set(inst); }
    });
  }

  _syncHelpersOnly() {
    let i = 0;
    for (const id of this.selected) {
      const inst = this.byId.get(id); const h = this.selGroup.children[i++];
      if (inst && h) h.matrix.copy(instanceMatrix(inst));
    }
  }

  // ---------------------------------------------------------------- tools
  setTool(t) {
    this.tool = t;
    this.brushRing.visible = false;
    if (t !== 'measure') this._clearMeasure();
    this._syncSelection();
    this.h.onTool(t);
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

  _pickInstance(e) {
    this._ndc(e);
    const hits = this.raycaster.intersectObject(this.h.instancesGroup, true);
    for (const h of hits) {
      const b = h.object.userData.batch;
      if (b && h.instanceId != null && h.instanceId < b.ids.length) return b.ids[h.instanceId];
    }
    return null;
  }

  _click(e) {
    if (this.tool === 'place') return this._place(e);
    if (this.tool === 'measure') return this._measureClick(e);
    if (this.tool === 'paint' || this.tool === 'erase') return;
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
    const src = [...this.selected].map((id) => this.byId.get(id)).filter(Boolean);
    if (!src.length) return;
    const copies = src.map((s) => ({ ...clone(s), id: newId(), source: 'manual', rule: 'manual', locked: true, p: [s.p[0] + 3, s.p[1], s.p[2] + 3] }));
    this.mutate(`duplicate ${copies.length}`, (tx) => copies.forEach((c) => tx.add(c)));
    this.select(copies.map((c) => c.id));
  }

  editSelected(label, fn) {
    const ids = [...this.selected];
    this.mutate(label, (tx) => {
      for (const id of ids) {
        const inst = clone(this.byId.get(id));
        if (!inst) continue;
        fn(inst);
        tx.set(inst);
      }
    });
  }

  toggleLock() {
    const any = [...this.selected].some((id) => !this.byId.get(id)?.locked);
    this.editSelected(any ? 'lock' : 'unlock', (i) => { i.locked = any; });
  }

  focusSelected() {
    const first = this.byId.get([...this.selected][0]);
    if (!first) return;
    const a = this.h.lib.get(first.asset);
    const s = Math.max(...assetSize(a || {}).map((v, k) => v * (first.scale?.[k] || 1)));
    const t = new THREE.Vector3(first.p[0], first.p[1] + s * 0.4, first.p[2]);
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
      this.editSelected('swap asset', (i) => {
        const oa = lib.get(i.asset);
        if (oa && na && oa.category === 'building' && i.target) {
          // keep the real-world target size when swapping buildings
          const [w, hh, d] = assetSize(na); const t = i.target.size;
          i.scale = [r3(t[0] / w), r3(t[1] / hh), r3(t[2] / d)];
        }
        i.asset = nid; i.locked = true;
      });
    });
    q('insDel')?.addEventListener('click', () => this.deleteSelected());
    q('insDup')?.addEventListener('click', () => this.duplicateSelected());
    q('insLock')?.addEventListener('click', () => this.toggleLock());
    q('insDrop')?.addEventListener('click', () => this.editSelected('drop to ground', (i) => { i.p[1] = r3(this.h.heightAt(i.p[0], i.p[2])); }));
  }
}

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
