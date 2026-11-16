// 素材檢索 plugin: search + thumbnail grid (left panel) and "匹配素材 Match" for the
// current selection (right panel). Server side: geobox/retrieval.py (/api/library/*).
import { generateThumbnails } from './thumbs.js';

const STYLE = `
#assetSearch .asrow { display: flex; gap: 4px; align-items: center; margin: 4px 0; }
#assetSearch .asrow input[type=number] { width: 52px; min-width: 0; padding: 3px 4px; }
#assetSearch #asQ { width: 100%; }
#assetSearch #asCat { flex: 1; }
.asgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(68px, 1fr)); gap: 5px; margin-top: 6px; }
.ascard { position: relative; background: #11141b; border: 1px solid var(--line); border-radius: 6px; padding: 3px; cursor: pointer; text-align: center; }
.ascard:hover { border-color: var(--accent); }
.ascard.sel { border-color: var(--accent); background: #1d2c42; box-shadow: 0 0 0 1px var(--accent) inset; }
.ascard .th { width: 100%; aspect-ratio: 1; border-radius: 4px; display: flex; align-items: center; justify-content: center; overflow: hidden;
  background: radial-gradient(circle at 50% 40%, #2b313d, #161a22); }
.ascard .th img { width: 100%; height: 100%; object-fit: contain; }
.ascard .th .sw { width: 38%; height: 38%; border-radius: 4px; opacity: .85; }
.ascard .lb { font-size: 10.5px; line-height: 1.2; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ascard .dm { font-size: 9.5px; color: var(--muted); }
.ascard .bar, .mrow .bar { height: 3px; border-radius: 2px; background: #2a3140; margin-top: 2px; overflow: hidden; }
.ascard .bar i, .mrow .bar i { display: block; height: 100%; background: linear-gradient(90deg, #3f78bb, #5aa9ff); }
#matchBody .mrow { display: grid; grid-template-columns: 46px 1fr; gap: 6px; padding: 4px; border: 1px solid transparent; border-radius: 6px; cursor: pointer; }
#matchBody .mrow:hover { border-color: var(--accent); background: #1b2230; }
#matchBody .mrow.cur { border-color: var(--accent2); }
#matchBody .mrow .th { width: 46px; height: 46px; border-radius: 4px; overflow: hidden; background: radial-gradient(circle at 50% 40%, #2b313d, #161a22); display:flex; align-items:center; justify-content:center; }
#matchBody .mrow .th img { width: 100%; height: 100%; object-fit: contain; }
#matchBody .mrow .th .sw { width: 40%; height: 40%; border-radius: 3px; }
#matchBody .t1 { display: flex; justify-content: space-between; gap: 4px; font-size: 12px; }
#matchBody .t1 b { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#matchBody .rs { font-size: 10.5px; color: var(--muted); }
#matchBody .q { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
`;

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = (v) => (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1));
const sizeOf = (a) => {
  const b = a && a.bounds;
  return b ? [b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]] : [1, 1, 1];
};
const colorOf = (a) => (a?.preview_color) || (a?.materials && (a.materials.wall || a.materials.leaves || Object.values(a.materials)[0])) || '#8a8f99';

export function install(ctx) {
  const { app, editor } = ctx;
  const st = { meta: {}, sel: [], results: null, timer: 0, gen: null, matchSeq: 0 };

  const style = document.createElement('style');
  style.textContent = STYLE;
  document.head.appendChild(style);

  // ---------------------------------------------------------------- left panel
  const sec = document.createElement('section');
  sec.id = 'assetSearch';
  sec.className = 'grow-section';
  sec.innerHTML = `
    <h3>素材檢索 Search <span id="asCount" class="muted small"></span></h3>
    <input id="asQ" placeholder="搜尋 名稱/tag/材質/中文：公寓、棕櫚、路燈、brick…" autocomplete="off">
    <div class="asrow">
      <select id="asCat"><option value="">全部類別</option><option value="building">建築</option><option value="tree">植被</option><option value="prop">道具</option><option value="kit">模組構件 Kit</option></select>
      <button id="asFromSel" title="用目前選取物件的真實尺寸與類別當作檢索條件">⇢ 依選取</button>
    </div>
    <div class="asrow small"><span class="muted">尺寸 m</span>
      <input id="asW" type="number" step="0.5" placeholder="W"><input id="asH" type="number" step="0.5" placeholder="H"><input id="asD" type="number" step="0.5" placeholder="D">
      <button id="asClear" title="清除條件">✕</button>
    </div>
    <div class="asrow small"><button id="asThumbs" title="為素材庫產生縮圖 (缺少的；Shift = 全部重做)">🖼 產生縮圖</button><span id="asThumbStatus" class="muted"></span></div>
    <div id="asGrid" class="asgrid"></div>`;
  const legacy = document.getElementById('palette')?.closest('section');
  ctx.panels.left.insertBefore(sec, legacy || null);
  if (legacy) legacy.style.display = 'none'; // the thumbnail grid replaces the plain list
  const $ = (id) => sec.querySelector('#' + id);

  const thumbUrl = (id) => {
    const m = st.meta[id];
    return m && m.thumbnail ? `/api/library/thumbs/${encodeURIComponent(id)}?v=${m.thumb_mtime || 0}` : null;
  };
  const thumbHTML = (id, a) => {
    const u = thumbUrl(id);
    return u ? `<img src="${u}" alt="" loading="lazy">` : `<span class="sw" style="background:${colorOf(a)}"></span>`;
  };

  function setPalette(ids) {
    st.sel = ids;
    editor.setPalette(ids);
    sec.querySelectorAll('.ascard').forEach((c) => c.classList.toggle('sel', ids.includes(c.dataset.id)));
    if (ids.length && !['place', 'paint'].includes(editor.tool)) editor.setTool('place');
  }

  function renderGrid() {
    const lib = app.lib && app.lib.lib;
    if (!lib) return;
    let items;
    if (st.results) {
      items = st.results.map((r) => ({ a: { ...app.lib.get(r.id), ...r.asset }, score: r.score, reasons: r.reasons, real: r.real_size }));
    } else {
      const order = { building: 0, tree: 1, prop: 2 };
      const cat = $('asCat').value;
      items = lib.assets.filter((a) => !cat || a.category === cat)
        .sort((x, y) => (order[x.category] ?? 9) - (order[y.category] ?? 9) || String(x.label || x.id).localeCompare(String(y.label || y.id)))
        .map((a) => ({ a }));
    }
    $('asCount').textContent = `${items.length}`;
    $('asGrid').innerHTML = items.map(({ a, score, reasons, real }) => {
      const s = real || sizeOf(a);
      const tip = `${a.id}\n${(a.tags || []).join(', ')}${reasons && reasons.length ? '\n' + reasons.join('\n') : ''}`;
      return `<div class="ascard${st.sel.includes(a.id) ? ' sel' : ''}" data-id="${esc(a.id)}" title="${esc(tip)}">
        <div class="th">${thumbHTML(a.id, a)}</div>
        <div class="lb">${esc(a.label || a.id)}</div>
        <div class="dm">${s.map(fmt).join('×')}${real ? '' : ''}</div>
        ${score != null ? `<div class="bar"><i style="width:${Math.round(score * 100)}%"></i></div>` : ''}
      </div>`;
    }).join('') || '<div class="muted small">沒有符合的素材</div>';
  }

  $('asGrid').addEventListener('click', (e) => {
    const card = e.target.closest('.ascard');
    if (!card) return;
    const id = card.dataset.id;
    if (e.shiftKey) setPalette(st.sel.includes(id) ? st.sel.filter((x) => x !== id) : [...st.sel, id]);
    else setPalette([id]);
  });

  async function search() {
    const q = $('asQ').value.trim();
    const cat = $('asCat').value;
    const dims = ['asW', 'asH', 'asD'].map((k) => parseFloat($(k).value));
    const hasSize = dims.every((v) => Number.isFinite(v) && v > 0);
    if (!q && !hasSize) { st.results = null; renderGrid(); return; }
    const p = new URLSearchParams({ limit: '120' });
    if (q) p.set('q', q);
    if (cat) p.set('category', cat);
    if (hasSize) { p.set('w', dims[0]); p.set('h', dims[1]); p.set('d', dims[2]); }
    try {
      const r = await (await fetch('/api/library/search?' + p)).json();
      st.results = r.results || [];
    } catch (err) { ctx.showErr(err); st.results = []; }
    renderGrid();
  }
  const debounced = () => { clearTimeout(st.timer); st.timer = setTimeout(search, 180); };
  ['asQ', 'asW', 'asH', 'asD'].forEach((k) => $(k).addEventListener('input', debounced));
  $('asCat').addEventListener('change', search);
  $('asClear').addEventListener('click', () => { ['asQ', 'asW', 'asH', 'asD'].forEach((k) => { $(k).value = ''; }); $('asCat').value = ''; search(); });
  $('asFromSel').addEventListener('click', () => {
    const q = selectionQuery();
    if (!q) return ctx.toast('先選取一個物件或 greybox block', true);
    $('asCat').value = q.category || '';
    ['asW', 'asH', 'asD'].forEach((k, i) => { $(k).value = (+q.size[i]).toFixed(1); });
    search();
  });

  // ---------------------------------------------------------------- index + thumbnails
  async function loadIndex() {
    try {
      const r = await (await fetch('/api/library/index')).json();
      st.meta = r.assets || {};
    } catch { st.meta = {}; }
  }

  async function makeThumbs(all = false) {
    if (st.gen || !app.lib) return;
    const ids = app.lib.lib.assets.map((a) => a.id).filter((id) => all || !(st.meta[id] && st.meta[id].thumbnail));
    if (!ids.length) { $('asThumbStatus').textContent = '縮圖已齊全'; return; }
    st.gen = new AbortController();
    $('asThumbs').disabled = true;
    const res = await generateThumbnails(app.lib, ids, {
      signal: st.gen.signal,
      onProgress: (n, total) => { $('asThumbStatus').textContent = `${n}/${total}`; },
      onDone: (id, info) => {
        st.meta[id] = { ...(st.meta[id] || {}), thumbnail: `thumbs/${id}.png`, thumb_mtime: Date.now(), colors: info.colors };
        sec.querySelectorAll(`.ascard[data-id="${CSS.escape(id)}"] .th`).forEach((th) => { th.innerHTML = thumbHTML(id, app.lib.get(id)); });
      },
    });
    st.gen = null;
    $('asThumbs').disabled = false;
    $('asThumbStatus').textContent = res.failed.length ? `完成 ${res.done}，失敗 ${res.failed.length}` : `完成 ${res.done} 張`;
    await loadIndex();
    renderGrid();
    renderMatch();
  }
  $('asThumbs').addEventListener('click', (e) => makeThumbs(e.shiftKey));

  ctx.hooks.libraryLoaded.push(async () => {
    await loadIndex();
    renderGrid();
    let auto = true;
    try { auto = localStorage.getItem('geobox.autoThumbs') !== '0'; } catch { /* storage blocked */ }
    const missing = app.lib.lib.assets.some((a) => !(st.meta[a.id] && st.meta[a.id].thumbnail));
    if (auto && missing) setTimeout(() => makeThumbs(false), 1500);
  });

  // ---------------------------------------------------------------- match panel (right)
  const msec = document.createElement('section');
  msec.id = 'matchSec';
  msec.innerHTML = '<h3>匹配素材 Match</h3><div id="matchBody" class="muted small">選取一個建築 / 樹 / 道具或 greybox block，依真實尺寸與用途檢索素材庫。</div>';
  const insp = document.getElementById('inspector')?.closest('section');
  ctx.panels.right.insertBefore(msec, insp ? insp.nextSibling : ctx.panels.right.firstChild);
  const body = msec.querySelector('#matchBody');

  const gb = () => ctx.greybox || null;
  const getBlock = (id) => (gb() && typeof gb().getBlock === 'function' ? gb().getBlock(id) : null)
    || (app.data && (app.data.greybox || []).find((b) => b.id === id)) || null;

  /** Query for the current selection, computed client-side (works with unsaved edits). */
  function selectionQuery() {
    const ids = [...editor.selected];
    if (ids.length !== 1 || !app.data) return null;
    const ref = ids[0];
    let block = ref.startsWith('gb:') ? getBlock(ref) : null;
    const inst = block ? (app.data.instances.find((i) => i.block === ref) || null) : app.data.instances.find((i) => i.id === ref);
    if (!block && inst && inst.block) block = getBlock(inst.block);
    if (block) {
      return { ref, block, inst, category: 'building', size: block.size, kind: block.kind, tags: block.tags || [],
        current: block.asset_lock || inst?.asset || null };
    }
    if (!inst) return null;
    const a = app.lib.get(inst.asset);
    const s = sizeOf(a);
    const size = inst.target ? inst.target.size : s.map((v, k) => v * ((inst.scale || [1, 1, 1])[k]));
    const kind = inst.category === 'building' && inst.feature && app.layout
      ? (app.layout.buildings.find((b) => b.id === inst.feature) || {}).kind : null;
    const fit = inst.category === 'tree' ? 'uniform' : inst.category === 'prop' ? 'none' : null;
    return { ref, inst, category: inst.category, size, kind, fit, current: inst.asset };
  }

  async function renderMatch() {
    const q = selectionQuery();
    if (!q) {
      body.className = 'muted small';
      body.textContent = editor.selected.size > 1 ? '多選時不檢索；請選一個物件。' : '選取一個建築 / 樹 / 道具或 greybox block，依真實尺寸與用途檢索素材庫。';
      return;
    }
    const seq = ++st.matchSeq;
    const p = new URLSearchParams({ block: q.ref, w: q.size[0], h: q.size[1], d: q.size[2], category: q.category, limit: '8' });
    if (app.name) p.set('project', app.name);
    if (q.kind) p.set('kind', q.kind);
    if (q.tags && q.tags.length) p.set('tags', q.tags.join(','));
    if (q.fit) p.set('fit', q.fit);
    let r;
    try { r = await (await fetch('/api/library/match?' + p)).json(); } catch (err) { r = { ok: false, error: err.message }; }
    if (seq !== st.matchSeq) return; // a newer selection is pending
    if (!r.ok) { body.className = 'muted small'; body.textContent = '檢索失敗: ' + (r.error || ''); return; }
    body.className = '';
    const head = `<div class="q">目標 ${q.size.map(fmt).join(' × ')} m · ${esc(q.category)}${q.kind ? ' · ' + esc(q.kind) : ''}${q.block ? ' · greybox' : ''}</div>`;
    body.innerHTML = head + r.results.map((h) => {
      const a = { ...app.lib.get(h.id), ...h.asset };
      return `<div class="mrow${h.id === q.current ? ' cur' : ''}" data-id="${esc(h.id)}" title="${esc((h.reasons || []).join('\n'))}">
        <div class="th">${thumbHTML(h.id, a)}</div>
        <div><div class="t1"><b>${esc(a.label || a.id)}</b><span>${(h.score * 100).toFixed(0)}</span></div>
          <div class="bar"><i style="width:${Math.round(h.score * 100)}%"></i></div>
          <div class="rs">${h.real_size.map(fmt).join('×')} m${h.id === q.current ? ' · 目前' : ''}</div>
          <div class="rs">${esc((h.reasons || []).slice(0, 2).join(' · '))}</div></div></div>`;
    }).join('');
    body.querySelectorAll('.mrow').forEach((row) => row.addEventListener('click', () => {
      const hit = r.results.find((x) => x.id === row.dataset.id);
      if (hit) swap(q, hit);
    }));
  }

  function swap(q, hit) {
    const id = hit.id;
    if (q.block) {
      // greybox block: lock the block to this asset and swap its building instance now
      if (gb() && typeof gb().setAssetLock === 'function') gb().setAssetLock(q.block.id, id);
      else { q.block.asset_lock = id; ctx.setDirty(true); }
      if (q.inst) {
        const inst = JSON.parse(JSON.stringify(q.inst));
        Object.assign(inst, { asset: id, scale: hit.scale, locked: true });
        editor.mutate(`swap ${id}`, (tx) => tx.set(inst));
      }
      ctx.toast(`${q.block.id} → ${id}${q.inst ? '' : '（儲存後 re-scatter 生效）'}`);
    } else {
      editor.editSelected(`swap ${id}`, (i) => {
        i.asset = id;
        i.scale = hit.fit === 'none' ? [1, 1, 1] : hit.scale.slice();
        i.locked = true;
      });
      ctx.toast(`已換成 ${id}（保持真實尺寸 ${hit.real_size.map(fmt).join('×')} m）`);
    }
    renderMatch();
  }

  editor.on('select', () => renderMatch());
  editor.on('change', () => { if (editor.selected.size === 1) renderMatch(); });
  ctx.hooks.projectLoaded.push(() => renderMatch());

  ctx.assetSearch = { search, renderMatch, makeThumbs, selectionQuery };
}
