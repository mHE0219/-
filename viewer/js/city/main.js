// GeoBox city editor: app shell (loading, rendering, layers, project/build UI).
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { api } from './api.js';
import { AssetLibrary, AssetBatch } from './assets.js';
import { buildGround, buildMassing, buildGrid, buildHuman, makeHeightSampler } from './ground.js';
import { Editor, paletteHTML } from './editor.js';
import { Google3DTiles } from './google3d.js';
import { LocalFrame } from './geo.js';
import { initNewBoxDialog } from './newbox.js';

const $ = (id) => document.getElementById(id);
const canvas = $('c');
const viewport = $('viewport');

// ------------------------------------------------------------------ renderer / scene
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;

const scene = new THREE.Scene();
scene.background = new THREE.Color('#9fb8d3');
const camera = new THREE.PerspectiveCamera(50, 1, 0.5, 60000);
camera.position.set(-300, 350, 450);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.495;
controls.screenSpacePanning = false;

const hemi = new THREE.HemisphereLight('#cfe0ff', '#5b5246', 0.9);
const sun = new THREE.DirectionalLight('#fff4e0', 2.6);
sun.castShadow = true;
sun.shadow.mapSize.set(4096, 4096);
sun.shadow.bias = -0.0004;
sun.shadow.normalBias = 0.4;
scene.add(hemi, sun, sun.target);

const world = new THREE.Group(); world.name = 'world'; scene.add(world);
const instancesGroup = new THREE.Group(); instancesGroup.name = 'instances'; world.add(instancesGroup);

// ------------------------------------------------------------------ app state
const app = {
  name: null, data: null, layout: null, lib: null, frame: null,
  ground: null, massing: null, grid: null, human: null,
  batches: new Map(), where: new Map(),
  heightAt: () => 0, dirty: false, sky: null, google: null, shadows: true,
};

function toast(msg, isErr = false, ms = 2600) {
  const t = $('toast');
  t.textContent = msg; t.className = isErr ? 'err' : ''; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, isErr ? ms * 2 : ms);
}

function setDirty(v) {
  app.dirty = v;
  $('dirtyFlag').hidden = !v;
  updateStats();
}

function updateStats() {
  if (!app.data) { $('stats').textContent = ''; return; }
  const n = app.data.instances.length;
  const by = {};
  for (const i of app.data.instances) by[i.category] = (by[i.category] || 0) + 1;
  $('stats').textContent = `${n} instances · ${Object.entries(by).map(([k, v]) => `${k} ${v}`).join(' · ')}`;
}

// ------------------------------------------------------------------ instances
async function refresh(assetIds = null) {
  if (!app.data) return;
  const groups = new Map();
  for (const inst of app.data.instances) {
    if (assetIds && !assetIds.has(inst.asset)) continue;
    if (!groups.has(inst.asset)) groups.set(inst.asset, []);
    groups.get(inst.asset).push(inst);
  }
  const ids = assetIds ? [...assetIds] : [...new Set([...groups.keys(), ...app.batches.keys()])];
  await Promise.all(ids.map(async (aid) => {
    const list = groups.get(aid) || [];
    let b = app.batches.get(aid);
    if (!b) {
      if (!list.length) return;
      const parts = await app.lib.load(aid);
      b = app.batches.get(aid) || new AssetBatch(aid, parts, instancesGroup);
      app.batches.set(aid, b);
    }
    // re-read in case data changed while loading
    const fresh = app.data.instances.filter((i) => i.asset === aid);
    b.rebuild(fresh, app.shadows);
    fresh.forEach((inst, k) => app.where.set(inst.id, { b, k }));
  }));
  updateStats();
}

function updateInstance(inst) {
  const w = app.where.get(inst.id);
  if (w && w.b.ids[w.k] === inst.id) w.b.updateOne(w.k, inst);
}

function clearWorld() {
  for (const b of app.batches.values()) b.dispose();
  app.batches.clear(); app.where.clear();
  for (const k of ['ground', 'massing', 'grid', 'human']) {
    if (app[k]) { world.remove(app[k].group || app[k]); }
    app[k] = null;
  }
}

// ------------------------------------------------------------------ editor host
const editor = new Editor({
  scene, camera, renderer, domElement: canvas, controls, instancesGroup,
  get lib() { return app.lib; },
  get layout() { return app.layout; },
  heightAt: (x, z) => app.heightAt(x, z),
  groundMeshes: () => (app.ground ? [app.ground.topMesh] : []),
  refresh, updateInstance,
  onDirty: () => setDirty(true),
  toast,
  onTool: (t) => {
    document.querySelectorAll('#tools button').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
    $('brushOpts').hidden = !(t === 'paint' || t === 'erase');
    canvas.style.cursor = { place: 'copy', paint: 'crosshair', erase: 'crosshair', measure: 'crosshair' }[t] || 'default';
  },
  onCursor: (p) => {
    if (!p || !app.frame) { $('hud').textContent = ''; return; }
    const [lat, lon] = app.frame.fromLocal(p.x, p.z);
    $('hud').textContent = `x ${p.x.toFixed(1)}  y ${p.y.toFixed(1)}  z ${p.z.toFixed(1)} m   ·   ${lat.toFixed(6)}, ${lon.toFixed(6)}`;
  },
});

// ------------------------------------------------------------------ loading
async function loadLibrary() {
  const lib = await api.library();
  app.lib = new AssetLibrary(lib);
  $('palette').innerHTML = paletteHTML(lib);
  $('palette').querySelectorAll('.item').forEach((el) => el.addEventListener('click', (e) => {
    if (!e.shiftKey) $('palette').querySelectorAll('.item.sel').forEach((x) => x !== el && x.classList.remove('sel'));
    el.classList.toggle('sel', e.shiftKey ? !el.classList.contains('sel') : true);
    const sel = [...$('palette').querySelectorAll('.item.sel')].map((x) => x.dataset.id);
    editor.setPalette(sel);
    if (!['place', 'paint'].includes(editor.tool)) editor.setTool('place');
  }));
}

async function loadProject(name) {
  if (app.dirty && !confirm('有未儲存的修改，確定切換？ Discard unsaved changes?')) { $('projectSelect').value = app.name || ''; return; }
  clearWorld();
  app.name = name;
  history.replaceState(null, '', `?project=${encodeURIComponent(name)}`);
  $('linkSky').href = `./sky.html?project=${encodeURIComponent(name)}`;
  const [data, layout] = await Promise.all([api.scene(name), api.layout(name)]);
  app.data = data; app.layout = layout;
  app.frame = new LocalFrame(layout.origin.lat, layout.origin.lon);
  app.heightAt = makeHeightSampler(layout.terrain);
  $('seedInput').value = data.rules?.seed ?? 1234;

  app.ground = buildGround(layout, { showMap: $('lyrMap').checked });
  world.add(app.ground.group);
  app.massing = buildMassing(layout, app.heightAt);
  app.massing.visible = $('lyrMassing').checked;
  world.add(app.massing);
  app.grid = buildGrid(layout); app.grid.visible = $('lyrGrid').checked; world.add(app.grid);
  app.human = buildHuman(); app.human.position.set(0, app.heightAt(0, 0), 0); app.human.visible = $('lyrHuman').checked; world.add(app.human);
  if (app.google) app.google.setBox(layout.box.rect);

  editor.setData(data);
  await refresh();
  setDirty(false);
  frameBox();
  fitShadow();
  boxInfo();
  $('emptyState').hidden = true;
  await loadSky(name);
}

function boxInfo() {
  const L = app.layout; const b = L.box;
  const s = L.stats || {};
  $('boxInfo').innerHTML = `中心 ${b.center[0].toFixed(6)}, ${b.center[1].toFixed(6)}<br>
    尺寸 <b>${b.size[0]} × ${b.size[1]} m</b> (W×D, 真實公尺)<br>
    建築 ${L.buildings.length} · 道路 ${L.roads.length} · 綠地/水 ${L.areas.length} · OSM 樹 ${L.trees.length}<br>
    ${L.terrain ? `地形 ${L.terrain.res.join('×')} · 海拔 ${L.terrain.base_elevation} m<br>` : ''}
    <span class="muted">${L.attribution || ''}</span>`;
  $('houdiniPaths').innerHTML = `projects/${app.name}/scene.json<br>projects/${app.name}/scene.usda`;
}

function frameBox() {
  const [x0, z0, x1, z1] = app.layout.box.rect;
  const s = Math.max(x1 - x0, z1 - z0);
  controls.target.set((x0 + x1) / 2, 0, (z0 + z1) / 2);
  camera.position.set(controls.target.x - s * 0.55, s * 0.62, controls.target.z + s * 0.75);
  camera.near = Math.max(0.3, s / 4000); camera.far = s * 60;
  camera.updateProjectionMatrix();
  controls.update();
}

function fitShadow() {
  if (!app.layout) return;
  const [x0, z0, x1, z1] = app.layout.box.rect;
  const R = Math.hypot(x1 - x0, z1 - z0) / 2 + 40;
  const cam = sun.shadow.camera;
  cam.left = -R; cam.right = R; cam.top = R; cam.bottom = -R; cam.near = 1; cam.far = R * 4;
  cam.updateProjectionMatrix();
  sun.userData.R = R;
}

async function refreshProjectList(select) {
  const { projects } = await api.projects();
  const sel = $('projectSelect');
  sel.innerHTML = '<option value="">—</option>' + projects.map((p) => `<option value="${p.name}">${p.name} (${p.size?.join('×')} m)</option>`).join('');
  if (select) sel.value = select;
  return projects;
}

// ------------------------------------------------------------------ sky
let SkyModule = null;
async function loadSky(name) {
  if (!SkyModule) {
    try { SkyModule = await import('../sky/SkySystem.js'); } catch (e) { console.warn('SkySystem not available', e); SkyModule = false; }
  }
  if (!SkyModule) return;
  let params = null;
  if (name) { try { params = await api.sky(name); } catch { params = null; } }
  if (!app.sky) {
    app.sky = new SkyModule.SkySystem(renderer, { params: params || undefined });
    scene.add(app.sky.object);
  } else if (params) {
    app.sky.setParams(params);
  }
  applySkyVisibility();
}

function applySkyVisibility() {
  const on = $('lyrSky').checked && app.sky;
  if (app.sky) app.sky.object.visible = !!on;
  scene.background = on ? null : new THREE.Color('#9fb8d3');
}

// ------------------------------------------------------------------ UI wiring
document.querySelectorAll('#tools button').forEach((b) => b.addEventListener('click', () => editor.setTool(b.dataset.tool)));
$('projectSelect').addEventListener('change', (e) => e.target.value && loadProject(e.target.value).catch(showErr));
$('btnSave').addEventListener('click', () => save().catch(showErr));
$('btnUndo').addEventListener('click', () => editor.undo());
$('btnRedo').addEventListener('click', () => editor.redo());
$('btnRescatter').addEventListener('click', () => rescatter().catch(showErr));
$('brushRadius').addEventListener('input', (e) => { editor.brush.radius = +e.target.value; $('brushRadiusV').textContent = e.target.value; });
$('brushSpacing').addEventListener('input', (e) => { editor.brush.spacing = +e.target.value; $('brushSpacingV').textContent = e.target.value; });
$('eraseCategory').addEventListener('change', (e) => { editor.brush.eraseCategory = e.target.value; });
$('lyrAssets').addEventListener('change', (e) => { instancesGroup.visible = e.target.checked; });
$('lyrMassing').addEventListener('change', (e) => { if (app.massing) app.massing.visible = e.target.checked; });
$('lyrGrid').addEventListener('change', (e) => { if (app.grid) app.grid.visible = e.target.checked; });
$('lyrHuman').addEventListener('change', (e) => { if (app.human) app.human.visible = e.target.checked; });
$('lyrMap').addEventListener('change', (e) => { if (app.ground) app.ground.topMesh.material = e.target.checked ? app.ground.mapMat : app.ground.plainMat; });
$('lyrSky').addEventListener('change', applySkyVisibility);
$('lyrShadows').addEventListener('change', (e) => {
  app.shadows = e.target.checked; sun.castShadow = app.shadows;
  for (const b of app.batches.values()) for (const m of b.meshes) { m.castShadow = app.shadows; }
});

// Google 3D tiles
try { $('gKey').value = localStorage.getItem('geobox.googleKey') || ''; } catch { /* storage unavailable */ }
$('lyrGoogle').addEventListener('change', async (e) => {
  try {
    if (!app.google) { app.google = new Google3DTiles(renderer, camera, world); }
    if (e.target.checked) {
      const key = $('gKey').value.trim();
      if (!key) { e.target.checked = false; return toast('請先輸入 Google Map Tiles API key', true); }
      try { localStorage.setItem('geobox.googleKey', key); } catch { /* ignore */ }
      if (!app.layout) return;
      const elev = app.layout.origin.elevation || 0;
      await app.google.enable({ apiKey: key, lat: app.layout.origin.lat, lon: app.layout.origin.lon, height: elev });
      app.google.setBox(app.layout.box.rect);
      toast('Google 3D Tiles 載入中… 若高度對不上，用 Y offset 調整（大地水準面差約 +15~+30 m）');
    } else app.google.disable();
  } catch (err) { showErr(err); }
});
$('gOffset').addEventListener('input', (e) => { $('gOffsetV').textContent = e.target.value; app.google?.setOffset(+e.target.value); });
$('gOpacity').addEventListener('input', (e) => app.google?.setOpacity(+e.target.value));

function cleanScene() {
  const d = JSON.parse(JSON.stringify(app.data));
  d.rules = d.rules || {};
  return d;
}

async function save() {
  if (!app.name) return;
  const r = await api.saveScene(app.name, cleanScene());
  setDirty(false);
  toast(`已儲存 ${r.instances} instances → scene.json + scene.usda`);
}

async function rescatter() {
  if (!app.name) return;
  if (app.dirty) await save();
  const seed = parseInt($('seedInput').value, 10);
  const r = await api.rescatter(app.name, { seed: Number.isFinite(seed) ? seed : undefined });
  app.dirty = false;
  await loadProject(app.name);
  toast((r.log || []).join('\n') || 'rescattered');
}

function showErr(err) {
  console.error(err);
  toast(`錯誤: ${err.message}${err.detail ? '\n' + String(err.detail).slice(0, 400) : ''}`, true);
}

window.addEventListener('keydown', (e) => {
  if (e.target.matches('input, select, textarea')) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 's') { e.preventDefault(); save().catch(showErr); return; }
  if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); e.shiftKey ? editor.redo() : editor.undo(); return; }
  if ((e.ctrlKey || e.metaKey) && k === 'y') { e.preventDefault(); editor.redo(); return; }
  if ((e.ctrlKey || e.metaKey) && k === 'd') { e.preventDefault(); editor.duplicateSelected(); return; }
  const tools = { q: 'select', w: 'move', e: 'rotate', r: 'scale', p: 'place', b: 'paint', x: 'erase', m: 'measure' };
  if (tools[k] && !e.ctrlKey && !e.metaKey) editor.setTool(tools[k]);
  else if (k === 'delete' || k === 'backspace') editor.deleteSelected();
  else if (k === 'f') editor.focusSelected();
  else if (k === 'l') editor.toggleLock();
  else if (k === 'escape') { editor.select([]); editor.setTool('select'); }
});
window.addEventListener('beforeunload', (e) => { if (app.dirty) { e.preventDefault(); e.returnValue = ''; } });

initNewBoxDialog({
  api, toast, showErr,
  onBuilt: async (name) => { await refreshProjectList(name); app.dirty = false; await loadProject(name); },
});

// ------------------------------------------------------------------ loop
function resize() {
  const w = viewport.clientWidth, h = viewport.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewport);
resize();

const timer = new THREE.Timer();
const tmp = new THREE.Vector3();
function frame(t) {
  timer.update(t);
  const dt = Math.min(timer.getDelta(), 0.1);
  controls.update();
  if (app.sky && app.sky.object.visible) {
    app.sky.update(dt, camera);
    if (app.sky.sunDirection) tmp.copy(app.sky.sunDirection).normalize();
    if (app.sky.sunColor) sun.color.copy(app.sky.sunColor);
    if (app.sky.sunIntensity != null) sun.intensity = app.sky.sunIntensity;
    if (app.sky.ambientColor) hemi.color.copy(app.sky.ambientColor);
  } else {
    tmp.set(-0.45, 0.75, 0.48).normalize();
  }
  const R = sun.userData.R || 500;
  sun.target.position.copy(controls.target).setY(0);
  sun.position.copy(sun.target.position).addScaledVector(tmp, R * 2);
  sun.visible = tmp.y > -0.02;
  if (app.google?.tiles) {
    app.google.update();
    if ((frame.n = (frame.n || 0) + 1) % 60 === 0) $('gAttrib').textContent = app.google.attributions();
  }
  editor.updateOverlays();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    await loadLibrary();
    const projects = await refreshProjectList();
    const want = new URLSearchParams(location.search).get('project');
    const pick = want && projects.find((p) => p.name === want) ? want : projects.find((p) => p.has_scene)?.name;
    if (pick) { $('projectSelect').value = pick; await loadProject(pick); }
    else { $('emptyState').hidden = false; await loadSky(null); }
  } catch (err) {
    showErr(err);
    $('emptyState').hidden = false;
  }
  requestAnimationFrame(frame);
})();

window.geobox = { app, editor, scene, camera, renderer }; // for debugging in the console
