// Greybox (灰盒 blockout) layer: render + edit scene.greybox[] blocks, view modes, art pass.
// Plugin contract: install(ctx) — see main.js (pluginCtx) and docs/conventions.md.
import { isBlockId, newBlockId } from './editor.js';

const MODES = { greybox: '灰盒', art: '素材', both: '疊加' };
const MODE_TIPS = { greybox: 'Greybox：只看灰盒 blockout（樹/道具保留）', art: 'Art：只看素材', both: 'Both：素材 + 半透明灰盒' };
const COLORS = { osm: 0xc4c4c4, edited: 0xd8c39a, manual: 0x9fbcd6, assetLock: 0xa8d0a0 };
const ART_CATS = new Set(['building', 'kit']); // art that replaces a greybox block (hidden in greybox mode)
const DRESS = { auto: '自動 auto', kit: '模組 kit', whole: '整棟 whole', none: '不生成 none' };

/** Kit sets from the library (mirrors geobox/kit.py kit_sets / choose_set). */
export function kitSets(lib) {
  const sets = {};
  for (const a of (lib && lib.assets) || []) {
    const k = a.kit || {};
    if (a.category !== 'kit' || !k.set || !k.role || (a.weight ?? 1) <= 0) continue;
    const s = sets[k.set] || (sets[k.set] = { name: k.set, roles: {}, module: null, tags: new Set(), range: null, parapet: 0 });
    (s.roles[k.role] = s.roles[k.role] || []).push(a);
    if ((k.role === 'wall' || k.role === 'window') && !s.module && k.module) s.module = k.module.map(Number);
    if (k.role === 'parapet' && k.module) s.parapet = Math.max(s.parapet, +k.module[1]);
    for (const t of a.tags || []) if (t !== k.role) s.tags.add(String(t).toLowerCase());
    if (k.height_range) s.range = k.height_range;
  }
  for (const s of Object.values(sets)) s.module = s.module || [3, 3.2];
  return sets;
}

export function predictDress(b, sets, defaultStrategy = 'auto') {
  const mode = b.dress || defaultStrategy || 'auto';
  if (mode === 'none') return { mode: 'none' };
  if (mode === 'whole' || b.asset_lock) return { mode: 'whole' };
  const h = +b.size[1];
  const tags = new Set([...(b.tags || []), b.kind || ''].map((t) => String(t).toLowerCase()));
  let set = b.kit_set && sets[b.kit_set] ? sets[b.kit_set] : null;
  if (!set) {
    let best = -1e9;
    for (const s of Object.values(sets)) {
      if (!s.roles.wall && !s.roles.window) continue;
      const overlap = [...tags].filter((t) => s.tags.has(t)).length;
      const [lo, hi] = s.range || [0, 1e9];
      const inside = lo <= h && h <= hi;
      if (mode === 'auto' && (!overlap || !inside)) continue;
      const score = overlap * 2 - (inside ? 0 : Math.abs(Math.log(Math.max(h, 0.1) / (h < lo ? lo : hi))) * 3);
      if (score > best) { best = score; set = s; }
    }
  }
  if (!set) return { mode: 'whole' };
  const fh = +b.floor_height || set.module[1];
  const hu = Math.max(h - (h - set.parapet >= fh * 0.8 ? set.parapet : 0), 0.5);
  const floors = Math.max(1, Math.round(hu / fh));
  const mods = [b.size[0], b.size[2]].map((L) => Math.max(1, Math.round(L / set.module[0])));
  return { mode: 'kit', set: set.name, floors, floorH: hu / floors, mods };
}

export function install(ctx) {
  const { THREE, app, editor, world, hooks, panels, camera } = ctx;
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ rendering
  const group = new THREE.Group();
  group.name = 'greybox';
  world.add(group);

  const geo = new THREE.BoxGeometry(1, 1, 1);
  geo.translate(0, 0.5, 0); // pivot = base centre
  const col = new Float32Array(geo.attributes.position.count * 3).fill(1);
  for (let v = 16; v < 20; v++) col.set([0.74, 0.74, 0.76], v * 3); // BoxGeometry face 4 = +Z = front (faces the road)
  for (let v = 8; v < 12; v++) col.set([1.06, 1.06, 1.06], v * 3); // roof slightly brighter
  geo.setAttribute('color', new THREE.BufferAttribute(col, 3));

  const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, vertexColors: true, roughness: 0.92, metalness: 0 });
  // world-space blockout grid: 5 m horizontal lines, 3.2 m storey lines on walls
  mat.onBeforeCompile = (sh) => {
    sh.vertexShader = 'varying vec3 vGbWorld;\nvarying vec3 vGbN;\n' + sh.vertexShader.replace('#include <project_vertex>', `#include <project_vertex>
      vec4 gbw = vec4(transformed, 1.0);
      vec3 gbn = objectNormal;
      #ifdef USE_INSTANCING
        gbw = instanceMatrix * gbw;
        gbn = mat3(instanceMatrix) * gbn;
      #endif
      vGbWorld = (modelMatrix * gbw).xyz;
      vGbN = mat3(modelMatrix) * gbn;`);
    sh.fragmentShader = 'varying vec3 vGbWorld;\nvarying vec3 vGbN;\n' + sh.fragmentShader.replace('#include <color_fragment>', `#include <color_fragment>
      {
        vec3 n = normalize(vGbN);
        vec2 gc = abs(n.y) > 0.5 ? vGbWorld.xz / 5.0
                : (abs(n.x) > 0.5 ? vec2(vGbWorld.z / 5.0, vGbWorld.y / 3.2) : vec2(vGbWorld.x / 5.0, vGbWorld.y / 3.2));
        vec2 gw = max(fwidth(gc), vec2(1e-4));
        vec2 gl = abs(fract(gc - 0.5) - 0.5) / gw;
        float line = 1.0 - min(min(gl.x, gl.y), 1.0);
        diffuseColor.rgb *= 1.0 - 0.24 * line;
      }`);
  };

  let mesh = null;
  let capacity = 0;
  let ids = [];
  const m4 = new THREE.Matrix4();
  const q = new THREE.Quaternion();
  const up = new THREE.Vector3(0, 1, 0);
  const tmpP = new THREE.Vector3();
  const tmpS = new THREE.Vector3();
  const tmpC = new THREE.Color();

  const blockMatrix = (b) => {
    q.setFromAxisAngle(up, THREE.MathUtils.degToRad(b.rot_y || 0));
    return m4.compose(tmpP.set(b.center[0], b.base_y || 0, b.center[1]), q,
      tmpS.set(Math.max(b.size[0], 0.01), Math.max(b.size[1], 0.01), Math.max(b.size[2], 0.01)));
  };
  const blockColor = (b) => tmpC.setHex(b.source === 'manual' ? COLORS.manual : b.asset_lock ? COLORS.assetLock : b.locked ? COLORS.edited : COLORS.osm);

  function rebuild() {
    const blocks = (app.data && app.data.greybox) || [];
    if (!mesh || blocks.length > capacity) {
      if (mesh) { group.remove(mesh); mesh.dispose(); }
      capacity = Math.ceil(Math.max(blocks.length, 16) * 1.3);
      mesh = new THREE.InstancedMesh(geo, mat, capacity);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      mesh.castShadow = true; mesh.receiveShadow = true;
      mesh.name = 'greyboxBlocks';
      group.add(mesh);
    }
    ids = blocks.map((b) => b.id);
    mesh.count = blocks.length;
    blocks.forEach((b, i) => { mesh.setMatrixAt(i, blockMatrix(b)); mesh.setColorAt(i, blockColor(b)); });
    commit();
    updateCount();
  }
  function commit() {
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.boundingSphere = null; mesh.boundingBox = null;
    if (mesh.count) mesh.computeBoundingSphere();
  }
  function updateOne(b) {
    const i = ids.indexOf(b.id);
    if (i < 0 || !mesh) return rebuild();
    mesh.setMatrixAt(i, blockMatrix(b));
    commit();
  }

  editor.addPicker((ray) => {
    if (!mesh || !group.visible || !mesh.count) return null;
    const hit = ray.intersectObject(mesh, false)[0];
    return hit && hit.instanceId != null && hit.instanceId < ids.length ? { id: ids[hit.instanceId], distance: hit.distance } : null;
  });
  editor.on('change', ({ before, after }) => {
    for (const k of [...before.keys(), ...after.keys()]) if (isBlockId(k)) { rebuild(); return; }
  });
  editor.on('live', (obj) => { if (isBlockId(obj.id)) updateOne(obj); });

  // ------------------------------------------------------------------ view modes
  let mode = 'art';
  try { mode = localStorage.getItem('geobox.viewMode') || 'art'; } catch { /* storage unavailable */ }
  if (!MODES[mode]) mode = 'art';

  function setMode(m) {
    mode = m;
    try { localStorage.setItem('geobox.viewMode', m); } catch { /* ignore */ }
    document.querySelectorAll('#gbModes button').forEach((b) => b.classList.toggle('active', b.dataset.mode === m));
    group.visible = m !== 'art';
    const both = m === 'both';
    if (mat.transparent !== both) {
      mat.transparent = both; mat.opacity = both ? 0.38 : 1; mat.depthWrite = !both; mat.needsUpdate = true;
    }
    if (mesh) mesh.castShadow = !both;
    // dropping art selection that becomes invisible keeps the gizmo honest
    if (m === 'greybox') {
      const hidden = [...editor.selected].filter((id) => !isBlockId(id) && ART_CATS.has(editor.get(id)?.category));
      if (hidden.length) editor.select([...editor.selected].filter((id) => !hidden.includes(id)));
    }
  }
  // building art is hidden in greybox mode (trees/props stay: they have no blockout)
  hooks.frame.push(() => {
    const hideBld = mode === 'greybox';
    for (const [aid, b] of app.batches) {
      const a = app.lib && app.lib.get(aid);
      const v = !(hideBld && a && ART_CATS.has(a.category));
      for (const m of b.meshes) if (m.visible !== v) m.visible = v;
    }
  });

  // ------------------------------------------------------------------ UI
  const bar = document.createElement('span');
  bar.id = 'gbModes';
  bar.className = 'seg';
  bar.innerHTML = Object.entries(MODES).map(([k, v]) => `<button data-mode="${k}" title="${MODE_TIPS[k]} (V 循環)">${v}</button>`).join('')
    + '<button id="gbArtPass" class="primary" title="Art pass：儲存 → 依灰盒檢索素材重新生成（保留鎖定/手動）">▶ 生成素材</button>';
  const anchor = $('linkSky');
  panels.topbar.insertBefore(bar, anchor);
  panels.topbar.insertBefore(Object.assign(document.createElement('span'), { className: 'sep' }), anchor);
  bar.querySelectorAll('button[data-mode]').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('gbArtPass').addEventListener('click', async () => {
    try {
      $('gbArtPass').disabled = true;
      ctx.toast('Art pass：儲存灰盒 → 檢索素材 → 重新生成…');
      await ctx.rescatter();
    } catch (err) { ctx.showErr(err); } finally { $('gbArtPass').disabled = false; }
  });
  const style = document.createElement('style');
  style.textContent = `#gbModes{display:inline-flex;gap:0}#gbModes button{border-radius:0;margin-left:-1px}
    #gbModes button:first-child{border-radius:6px 0 0 6px}#gbModes button:last-child{border-radius:0 6px 6px 0;margin-left:6px}
    #gbOpts input{width:70px}`;
  document.head.appendChild(style);

  // tool button + options
  const tools = $('tools');
  const btn = document.createElement('button');
  btn.dataset.tool = 'block';
  btn.title = '畫灰盒：在地面拖曳出底面 (G)；單擊 = 10×10 m';
  btn.textContent = '▭ 灰盒 G';
  tools.appendChild(btn);
  btn.addEventListener('click', () => editor.setTool('block'));
  const opts = document.createElement('div');
  opts.id = 'gbOpts';
  opts.className = 'opts';
  opts.hidden = true;
  opts.innerHTML = `<label>新灰盒高度 <input id="gbNewH" type="number" value="12" min="1" step="0.5"> m</label>
    <label>用途 kind <input id="gbNewKind" value="residential"></label>
    <div class="muted small">拖曳 = 自訂底面；單擊 = 10×10 m。Shift 拖曳對齊 1 m。</div>`;
  tools.parentElement.appendChild(opts);
  editor.on('tool', (t) => { opts.hidden = t !== 'block'; ctx.renderer.domElement.style.cursor = t === 'block' ? 'crosshair' : ctx.renderer.domElement.style.cursor; });

  const count = document.createElement('div');
  count.className = 'muted small';
  count.id = 'gbCount';
  const boxInfo = $('boxInfo');
  boxInfo.parentElement.appendChild(count);
  function updateCount() {
    const bl = (app.data && app.data.greybox) || [];
    const edited = bl.filter((b) => b.locked || b.source === 'manual').length;
    const src = app.data?.greybox_source === 'empty' ? '設計者模式' : 'OSM 草稿';
    count.innerHTML = `灰盒 ${bl.length} 個 · ${src}（已編輯/手動 ${edited}，刪除 ${(app.data?.suppressed_blocks || []).length}）
      · <a href="/api/projects/${encodeURIComponent(app.name || '')}/greybox.obj" download="greybox.obj">下載 greybox.obj</a>`;
  }

  // ------------------------------------------------------------------ draw-a-block tool
  const preview = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: 0x33ddff, transparent: true, opacity: 0.35, depthWrite: false }));
  preview.visible = false;
  world.add(preview);
  let drag = null;
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const el = ctx.renderer.domElement;
  const groundHit = (e) => {
    const r = el.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const g = app.ground ? ray.intersectObject(app.ground.topMesh, false)[0] : null;
    return g ? g.point : null;
  };
  const rectFrom = (a, b, snap) => {
    let x0 = Math.min(a.x, b.x), x1 = Math.max(a.x, b.x), z0 = Math.min(a.z, b.z), z1 = Math.max(a.z, b.z);
    if (snap) { x0 = Math.floor(x0); z0 = Math.floor(z0); x1 = Math.ceil(x1); z1 = Math.ceil(z1); }
    return { cx: (x0 + x1) / 2, cz: (z0 + z1) / 2, w: x1 - x0, d: z1 - z0 };
  };
  el.addEventListener('pointerdown', (e) => {
    if (editor.tool !== 'block' || e.button !== 0) return;
    const p = groundHit(e);
    if (!p) return;
    drag = { a: p.clone(), b: p.clone() };
    ctx.controls.enabled = false;
  });
  el.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const p = groundHit(e);
    if (!p) return;
    drag.b.copy(p);
    const r = rectFrom(drag.a, drag.b, e.shiftKey);
    const h = parseFloat($('gbNewH').value) || 12;
    preview.position.set(r.cx, app.heightAt(r.cx, r.cz), r.cz);
    preview.scale.set(Math.max(r.w, 0.2), h, Math.max(r.d, 0.2));
    preview.visible = true;
  });
  window.addEventListener('pointerup', (e) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    preview.visible = false;
    ctx.controls.enabled = true;
    let r = rectFrom(d.a, d.b, e.shiftKey);
    if (r.w < 1 || r.d < 1) r = { cx: d.a.x, cz: d.a.z, w: 10, d: 10 };
    const h = parseFloat($('gbNewH').value) || 12;
    const kind = ($('gbNewKind').value || 'residential').trim();
    const rules = (app.data && app.data.rules && app.data.rules.buildings) || {};
    const kt = rules.kind_tags || {};
    // long side on local X (library standard)
    const rot = r.d > r.w ? 90 : 0;
    const [w, dd] = r.d > r.w ? [r.d, r.w] : [r.w, r.d];
    const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sz]) => app.heightAt(r.cx + sx * r.w / 2, r.cz + sz * r.d / 2));
    const block = {
      id: newBlockId(), kind, center: [round(r.cx), round(r.cz)], base_y: round(Math.min(...corners)),
      size: [round(w), round(h), round(dd)], rot_y: rot, tags: kt[kind] || kt['*'] || ['generic'],
      source: 'manual', feature: null, locked: true, asset_lock: null,
    };
    editor.mutate('add block', (tx) => tx.add(block));
    editor.select([block.id]);
    ctx.toast(`灰盒 ${w.toFixed(1)}×${h}×${dd.toFixed(1)} m — 按 ▶ 生成素材`);
  });
  const round = (v) => Math.round(v * 1000) / 1000;

  window.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea') || e.ctrlKey || e.metaKey) return;
    if (e.key === 'g' || e.key === 'G') editor.setTool('block');
    if (e.key === 'v' || e.key === 'V') {
      const keys = Object.keys(MODES);
      setMode(keys[(keys.indexOf(mode) + 1) % keys.length]);
    }
  });

  // ------------------------------------------------------------------ dress section (block inspector)
  editor.on('select', (sel) => {
    const ids = [...sel];
    if (!ids.length || !ids.every(isBlockId)) return;
    const el = $('inspector');
    if (!el || $('gbDressBox')) return;
    const first = editor.get(ids[0]);
    if (!first) return;
    const sets = kitSets(app.lib && app.lib.lib);
    const strategy = app.data?.rules?.buildings?.strategy || 'auto';
    const pred = predictDress(first, sets, strategy);
    const info = pred.mode === 'kit'
      ? `${pred.set} · ${pred.floors} 層 × ${pred.floorH.toFixed(2)} m · 模組 ${pred.mods[0]}×${pred.mods[1]}`
      : pred.mode === 'whole' ? '整棟素材（檢索 / 指定）' : '只留灰盒';
    const box = document.createElement('div');
    box.id = 'gbDressBox';
    box.innerHTML = `<h3 style="margin-top:10px">素材化 Dress</h3><div class="kv">
      <span>策略</span><select id="gbDress">${Object.entries(DRESS).map(([k, v]) => `<option value="${k}" ${(first.dress || 'auto') === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
      <span>Kit set</span><select id="gbKitSet"><option value="">自動（依用途/高度）</option>${Object.keys(sets).map((n) => `<option value="${n}" ${first.kit_set === n ? 'selected' : ''}>${n}</option>`).join('')}</select>
      <span>樓高 m</span><input id="gbFloorH" type="number" min="2" max="8" step="0.1" placeholder="自動" value="${first.floor_height ?? ''}">
      <span>預估</span><span class="small">${info}</span></div>
      <div class="muted small">改完按 ▶ 生成素材。kit = 牆/窗/騎樓/轉角/女兒牆/屋頂 模組拼裝；whole = 檢索整棟素材。</div>`;
    el.appendChild(box);
    const edit = (label, fn) => editor.editSelected(label, (b) => { fn(b); b.locked = true; });
    $('gbDress').addEventListener('change', (e) => edit('dress', (b) => { b.dress = e.target.value === 'auto' ? undefined : e.target.value; if (b.dress === undefined) delete b.dress; }));
    $('gbKitSet').addEventListener('change', (e) => edit('kit set', (b) => { b.kit_set = e.target.value || null; }));
    $('gbFloorH').addEventListener('change', (e) => edit('floor height', (b) => { const v = parseFloat(e.target.value); b.floor_height = Number.isFinite(v) && v > 0 ? v : null; }));
  });

  // ------------------------------------------------------------------ seed / clear (designer-led greybox)
  const actions = document.createElement('div');
  actions.className = 'small';
  actions.style.margin = '4px 0';
  actions.innerHTML = `<button id="gbSeed" title="把 OSM 建築輪廓加入灰盒當草稿（保留你畫的灰盒）">＋ OSM 灰盒草稿</button>
    <button id="gbClear" title="移除 OSM 草稿灰盒（設計者模式，地圖保留當參考；保留手動灰盒）">清空 OSM 灰盒</button>`;
  count.after(actions);
  const serverAction = async (path, label) => {
    if (!app.name) return;
    try {
      if (app.dirty) await ctx.save();
      const r = await fetch(`/api/projects/${encodeURIComponent(app.name)}/greybox/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText);
      await ctx.loadProject(app.name);
      ctx.toast(`${label}：${path === 'seed' ? `新增 ${j.added}` : `移除 ${j.removed}`}，共 ${j.blocks} 個灰盒`);
      setMode(path === 'seed' ? 'greybox' : mode);
    } catch (err) { ctx.showErr(err); }
  };
  actions.querySelector('#gbSeed').addEventListener('click', () => serverAction('seed', 'OSM 灰盒草稿'));
  actions.querySelector('#gbClear').addEventListener('click', () => {
    if (confirm('移除所有 OSM 草稿灰盒與其素材？（手動畫的灰盒保留）')) serverAction('clear', '清空 OSM 灰盒');
  });

  hooks.projectLoaded.push(() => { rebuild(); setMode(mode); });
  setMode(mode);
  return { rebuild, setMode, get mode() { return mode; } };
}
