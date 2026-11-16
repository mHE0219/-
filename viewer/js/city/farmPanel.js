// Farm panel (left column): workers, recent jobs, ingest / tiled-district submission.
// Talks to /api/farm/* (geobox/farm/api.py). Polls only while the section is open.

const STATE_COLORS = { queued: '#8b93a5', running: '#5aa9ff', done: '#5fbf6a', failed: '#ff6b6b', cancelled: '#c08a4a' };

const CSS = `
#farmSection summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 6px; }
#farmSection summary::-webkit-details-marker { display: none; }
#farmSection summary h3 { margin: 6px 0; }
#farmSection .badge { font-size: 11px; color: var(--muted); margin-left: auto; }
#farmSection .row { display: flex; gap: 4px; flex-wrap: wrap; margin: 6px 0; }
#farmSection .row button { font-size: 12px; padding: 3px 7px; flex: 1 1 auto; }
.farm-w { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 2px 0; min-width: 0; }
.farm-w b, .farm-w .st { white-space: nowrap; flex: none; }
.farm-w .cur { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }
.farm-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.farm-jobs { display: flex; flex-direction: column; gap: 3px; max-height: 260px; overflow-y: auto; }
.farm-j { font-size: 11.5px; padding: 3px 5px; border-radius: 5px; background: var(--panel2); }
.farm-j .t { display: flex; gap: 5px; align-items: center; }
.farm-j .lbl { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.farm-j .meta { color: var(--muted); font-size: 10.5px; display: flex; gap: 6px; }
.farm-j .err { color: #ff9d9d; font-size: 10.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.farm-j button { font-size: 10.5px; padding: 0 5px; }
.farm-bar { height: 3px; background: #11131a; border-radius: 2px; margin-top: 2px; overflow: hidden; }
.farm-bar > i { display: block; height: 100%; background: #5aa9ff; }
#farmLogDlg pre { max-height: 60vh; overflow: auto; background: #0e1015; padding: 8px; font-size: 11px; white-space: pre-wrap; }
`;

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ago = (t) => { if (!t) return ''; const d = Date.now() / 1000 - t; return d < 90 ? `${d | 0}s` : d < 5400 ? `${(d / 60) | 0}m` : `${(d / 3600) | 0}h`; };

async function jfetch(url, opt) {
  const r = await fetch(url, opt);
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw new Error((data && data.error) || `${r.status}`);
  return data;
}
const post = (url, body) => jfetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

export function install(ctx) {
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  const sec = document.createElement('section');
  sec.id = 'farmSection';
  sec.innerHTML = `
    <details id="farmDetails">
      <summary><h3>Farm 工作農場</h3><span class="badge" id="farmBadge">…</span></summary>
      <div id="farmWorkers" class="small"></div>
      <div class="row">
        <button id="farmIngest" title="把資料夾裡的新模型加入素材庫：量測、統一比例尺、建檢索索引（在 farm 節點上跑）">⇪ Ingest…</button>
        <button id="farmTiles" title="把一整區切成 N×M 個 box，分給 farm 節點平行 build">▦ 整區 Tiles…</button>
      </div>
      <div id="farmJobs" class="farm-jobs"></div>
      <div id="farmRoot" class="muted small mono" style="margin-top:4px"></div>
    </details>`;
  const left = ctx.panels.left;
  const lib = left.querySelector('.grow-section');
  left.insertBefore(sec, lib || null);

  const dlg = document.createElement('dialog');
  dlg.id = 'farmLogDlg';
  dlg.innerHTML = '<h2 id="farmLogTitle">log</h2><pre id="farmLogText"></pre><div style="text-align:right"><button id="farmLogClose">關閉</button></div>';
  document.body.appendChild(dlg);
  dlg.querySelector('#farmLogClose').onclick = () => dlg.close();

  const $ = (id) => document.getElementById(id);
  const details = $('farmDetails');
  try { details.open = localStorage.getItem('geobox.farmOpen') === '1'; } catch { /* storage unavailable */ }
  const seenDone = new Set();
  let first = true;
  let timer = null;

  async function refresh() {
    let jobs, workers;
    try {
      [jobs, workers] = await Promise.all([jfetch('/api/farm/jobs?limit=40'), jfetch('/api/farm/workers')]);
    } catch (e) {
      $('farmBadge').textContent = 'offline';
      return;
    }
    const c = jobs.counts;
    $('farmBadge').textContent = `${workers.workers.filter((w) => w.alive).length} 節點 · ▶${c.running} ⏳${c.queued} ✗${c.failed}`;
    $('farmRoot').textContent = jobs.root;
    $('farmWorkers').innerHTML = workers.workers.length ? workers.workers.map((w) => `
      <div class="farm-w"><span class="farm-dot" style="background:${w.alive ? (w.state === 'busy' ? '#5aa9ff' : '#5fbf6a') : '#555'}"></span>
        <b>${esc(w.name)}</b><span class="muted st" title="${esc((w.tags || []).join(', '))}">${w.alive ? esc(w.state) : 'offline'}${w.tags?.length ? ' · ' + esc(w.tags.join(',')) : ''}</span>
        <span class="muted cur" title="${esc(w.current_label || '')}">${esc(w.current_label || '')}</span></div>`).join('')
      : '<div class="muted">沒有 worker。在 farm 節點執行：<br><code>python -m geobox farm worker --root &lt;共享資料夾&gt;</code></div>';

    $('farmJobs').innerHTML = jobs.jobs.map((j) => {
      const st = j.status === 'cancelled' ? 'cancelled' : j.state;
      const pct = Math.round(100 * (j.progress || (st === 'done' ? 1 : 0)));
      return `<div class="farm-j" data-id="${esc(j.id)}">
        <div class="t"><span class="farm-dot" style="background:${STATE_COLORS[st] || '#888'}"></span>
          <span class="lbl" title="${esc(j.type)} · ${esc(j.id)}">${esc(j.label || j.type)}</span>
          <button data-act="log" title="log">≡</button>
          ${st === 'failed' || st === 'cancelled' ? '<button data-act="retry" title="retry">↻</button>' : ''}
          ${st === 'queued' || st === 'running' ? '<button data-act="cancel" title="cancel">✕</button>' : ''}</div>
        <div class="meta"><span>${esc(st)}</span><span>${esc(j.worker || '')}</span><span>${ago(j.created)}</span>
          ${j.deps?.length && st === 'queued' ? `<span>等待 ${j.deps.length} 個前置</span>` : ''}
          ${j.requires?.length ? `<span>需 ${esc(j.requires.join(','))}</span>` : ''}</div>
        ${st === 'running' ? `<div class="farm-bar"><i style="width:${pct}%"></i></div>` : ''}
        ${j.error && st !== 'done' ? `<div class="err" title="${esc(j.error)}">${esc(j.error)}</div>` : ''}
      </div>`;
    }).join('') || '<div class="muted small">尚無工作</div>';

    // a finished reindex / ingest means the library changed -> reload palette + search
    let libChanged = false;
    for (const j of jobs.jobs) {
      if (j.state !== 'done' || seenDone.has(j.id)) continue;
      seenDone.add(j.id);
      if (!first && (j.type === 'reindex' || j.type === 'ingest_asset')) libChanged = true;
      if (!first && j.type === 'build_box') ctx.toast(`Farm: ${j.result?.name || j.label} 完成 (${j.result?.instances ?? '?'} instances)`);
    }
    first = false;
    if (libChanged) {
      try { await ctx.reloadLibrary(); ctx.toast('Farm: 素材庫 / 檢索索引已更新'); } catch (e) { console.warn(e); }
    }
  }

  function schedule() {
    clearTimeout(timer);
    const visible = details.open && document.visibilityState === 'visible';
    timer = setTimeout(async () => { await refresh(); schedule(); }, visible ? 3000 : 20000);
  }

  details.addEventListener('toggle', () => {
    try { localStorage.setItem('geobox.farmOpen', details.open ? '1' : '0'); } catch { /* ignore */ }
    if (details.open) refresh();
    schedule();
  });
  document.addEventListener('visibilitychange', schedule);

  $('farmJobs').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const id = b.closest('.farm-j').dataset.id;
    try {
      if (b.dataset.act === 'log') {
        const r = await fetch(`/api/farm/jobs/${encodeURIComponent(id)}/log`);
        $('farmLogTitle').textContent = id;
        $('farmLogText').textContent = await r.text() || '(empty)';
        dlg.showModal();
      } else {
        await post(`/api/farm/jobs/${encodeURIComponent(id)}/${b.dataset.act}`);
        refresh();
      }
    } catch (err) { ctx.showErr(err); }
  });

  $('farmIngest').addEventListener('click', async () => {
    const folder = prompt('要 ingest 的資料夾（farm 節點看得到的路徑，例如 //nas/assets/trees）');
    if (!folder) return;
    const category = prompt('類別 building / tree / prop（留空 = 依資料夾名稱自動判斷）', '') || undefined;
    try {
      const r = await post('/api/farm/submit', { type: 'ingest_folder', label: `ingest ${folder.split(/[\\/]/).filter(Boolean).pop()}`, params: { folder, category } });
      ctx.toast(`已送出 ingest: ${r.id}`);
      details.open = true; refresh();
    } catch (err) { ctx.showErr(err); }
  });

  $('farmTiles').addEventListener('click', async () => {
    const L = ctx.app.layout;
    const where = prompt('整區中心：Google Maps 網址或 "lat, lon"', L ? `${L.box.center[0].toFixed(6)}, ${L.box.center[1].toFixed(6)}` : '');
    if (!where) return;
    const size = (prompt('整區尺寸 W D（公尺）', '2000 2000') || '').trim().split(/[\s,x×]+/).map(Number);
    if (size.length < 2 || size.some((v) => !(v > 0))) return ctx.toast('尺寸格式：2000 1500', true);
    const tile = Number(prompt('每個 tile 的邊長（公尺）', '500')) || 500;
    const name = (prompt('區域名稱（英數）', 'district') || 'district').replace(/[^A-Za-z0-9_-]/g, '_');
    const params = { name, size, tile };
    if (/^\s*-?\d/.test(where)) params.center = where.split(',').map(Number); else params.at = where;
    try {
      const r = await post('/api/farm/submit', { type: 'build_tiles', label: `district ${name}`, params });
      const n = Math.ceil(size[0] / tile) * Math.ceil(size[1] / tile);
      ctx.toast(`已送出 ${n} 個 tile 的 build: ${r.id}`);
      details.open = true; refresh();
    } catch (err) { ctx.showErr(err); }
  });

  refresh();
  schedule();
  return { refresh };
}
