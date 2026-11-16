// "New box" dialog: Google Maps URL / lat,lon -> centre + true-metre box preview on a 2D map -> build.
import * as L from 'leaflet';
import { LocalFrame } from './geo.js';

const $ = (id) => document.getElementById(id);

export function initNewBoxDialog({ api, toast, showErr, onBuilt }) {
  const dlg = $('newDialog');
  let map = null, rect = null, marker = null;
  let center = [25.0339, 121.5645];

  const ensureMap = () => {
    if (map) { setTimeout(() => map.invalidateSize(), 50); return; }
    map = L.map('nbMap', { zoomControl: true }).setView(center, 16);
    // OSM standard tiles: fine for light interactive use (tile usage policy); attribution required.
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
    }).addTo(map);
    map.on('click', (e) => { center = [e.latlng.lat, e.latlng.lng]; $('nbAt').value = `${center[0].toFixed(6)}, ${center[1].toFixed(6)}`; draw(true); });
    setTimeout(() => map.invalidateSize(), 50);
  };

  const draw = (keepView = false) => {
    if (!map) return;
    const w = +$('nbW').value || 500, d = +$('nbD').value || 500;
    const f = new LocalFrame(center[0], center[1]);
    const corners = f.boxBounds(w, d); // exact metres -> lat/lon polygon
    if (rect) rect.remove();
    if (marker) marker.remove();
    rect = L.polygon(corners, { color: '#ff3b30', weight: 2, fillOpacity: 0.08 }).addTo(map);
    marker = L.circleMarker(center, { radius: 4, color: '#ff3b30' }).addTo(map);
    if (!keepView) map.fitBounds(rect.getBounds(), { padding: [20, 20] });
  };

  const parse = async () => {
    const text = $('nbAt').value.trim();
    if (!text) return;
    try {
      const r = await api.parse(text);
      center = r.center;
      $('nbParsed').textContent = `→ ${r.center[0].toFixed(6)}, ${r.center[1].toFixed(6)} (${r.kind})` +
        (r.suggested_size ? `  · 建議尺寸 ${r.suggested_size[0]}×${r.suggested_size[1]} m (依縮放)` : '');
      if (r.suggested_size && !$('nbW').dataset.touched) { $('nbW').value = Math.min(r.suggested_size[0], 2000); $('nbD').value = Math.min(r.suggested_size[1], 2000); }
      draw();
    } catch (err) { $('nbParsed').textContent = '✗ ' + err.message; }
  };

  const open = () => { dlg.showModal(); $('nbLog').hidden = true; ensureMap(); draw(); };
  $('btnNew').addEventListener('click', open);
  $('btnNew2').addEventListener('click', open);
  $('nbAt').addEventListener('change', parse);
  $('nbAt').addEventListener('paste', () => setTimeout(parse, 0));
  for (const id of ['nbW', 'nbD']) $(id).addEventListener('input', (e) => { e.target.dataset.touched = '1'; draw(true); });

  $('newForm').addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') return;
    e.preventDefault();
    const name = $('nbName').value.trim();
    if (!/^[A-Za-z0-9_-]+$/.test(name)) return toast('名稱只能用英數、底線、減號', true);
    const body = {
      name, center, size: [+$('nbW').value, +$('nbD').value],
      terrain: $('nbTerrain').checked, clip_buildings: $('nbClip').checked, refetch: $('nbRefetch').checked,
    };
    $('nbGo').disabled = true;
    $('nbLog').hidden = false;
    $('nbLog').textContent = '下載 OSM / 建立 layout / scatter 中… (Overpass 可能需要 10–60 秒)';
    try {
      const r = await api.build(body);
      $('nbLog').textContent = (r.log || []).join('\n');
      dlg.close();
      toast(`Box 建立完成: ${r.instances} instances`);
      await onBuilt(r.name);
    } catch (err) {
      $('nbLog').textContent = '✗ ' + err.message + (err.detail ? '\n' + err.detail : '');
      showErr(err);
    } finally {
      $('nbGo').disabled = false;
    }
  });
}
