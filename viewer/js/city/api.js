// Thin wrapper over the GeoBox server API (python -m geobox serve).
async function req(method, url, body, isBinary = false) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isBinary) { opt.body = body; opt.headers['Content-Type'] = 'application/octet-stream'; }
    else { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  }
  const r = await fetch(url, opt);
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!r.ok) {
    const e = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    e.detail = data && data.detail; e.status = r.status;
    throw e;
  }
  return data;
}

const enc = encodeURIComponent;
export const api = {
  projects: () => req('GET', '/api/projects'),
  library: () => req('GET', '/api/library'),
  parse: (text) => req('GET', '/api/parse?text=' + enc(text)),
  scene: (name) => req('GET', `/api/projects/${enc(name)}/scene`),
  layout: (name) => req('GET', `/api/projects/${enc(name)}/layout`),
  saveScene: (name, scene) => req('PUT', `/api/projects/${enc(name)}/scene`, scene),
  rescatter: (name, body) => req('POST', `/api/projects/${enc(name)}/rescatter`, body || {}),
  build: (body) => req('POST', '/api/build', body),
  sky: (name) => req('GET', `/api/projects/${enc(name)}/sky`),
  saveSky: (name, params) => req('PUT', `/api/projects/${enc(name)}/sky`, params),
  putFile: (name, rel, data) => req('PUT', `/api/projects/${enc(name)}/files/${rel}`, data, true),
};
