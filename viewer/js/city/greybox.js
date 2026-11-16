// Greybox (灰盒 blockout) layer: render + edit scene.greybox[] blocks, view modes, art pass.
// Plugin contract: install(ctx) — see main.js (pluginCtx) and docs/conventions.md.
import { isBlockId, newBlockId } from './editor.js';

const MODES = { greybox: '灰盒 Greybox', art: '素材 Art', both: '疊加 Both' };
const COLORS = { osm: 0xc4c4c4, edited: 0xd8c39a, manual: 0x9fbcd6, assetLock: 0xa8d0a0 };

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
      const hidden = [...editor.selected].filter((id) => !isBlockId(id) && editor.get(id)?.category === 'building');
      if (hidden.length) editor.select([...editor.selected].filter((id) => !hidden.includes(id)));
    }
  }
  // building art is hidden in greybox mode (trees/props stay: they have no blockout)
  hooks.frame.push(() => {
    const hideBld = mode === 'greybox';
    for (const [aid, b] of app.batches) {
      const a = app.lib && app.lib.get(aid);
      const v = !(hideBld && a && a.category === 'building');
      for (const m of b.meshes) if (m.visible !== v) m.visible = v;
    }
  });

  // ------------------------------------------------------------------ UI
  const bar = document.createElement('span');
  bar.id = 'gbModes';
  bar.className = 'seg';
  bar.innerHTML = Object.entries(MODES).map(([k, v]) => `<button data-mode="${k}" title="檢視模式 (V 循環)">${v}</button>`).join('')
    + '<button id="gbArtPass" class="primary" title="儲存 → 依灰盒檢索素材重新生成（保留鎖定/手動）">▶ Art pass</button>';
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
    count.innerHTML = `灰盒 ${bl.length} 個（已編輯/手動 ${edited}，刪除 ${(app.data?.suppressed_blocks || []).length}）
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
    ctx.toast(`灰盒 ${w.toFixed(1)}×${h}×${dd.toFixed(1)} m — 按 ▶ Art pass 生成素材`);
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

  hooks.projectLoaded.push(() => { rebuild(); setMode(mode); });
  setMode(mode);
  return { rebuild, setMode, get mode() { return mode; } };
}
