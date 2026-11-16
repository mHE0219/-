"""Local web server: static viewer + JSON API (stdlib only).

    python -m geobox serve            -> http://127.0.0.1:8765

Binds to localhost by default; every write is confined to projects/<name>/.
See docs/conventions.md for the API table.
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import threading
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import library as libmod
from . import project
from .geo import parse_location

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER_DIR = os.path.join(REPO_ROOT, "viewer")
ASSETS_DIR = os.path.join(REPO_ROOT, "assets")
WRITABLE_PREFIXES = ("sky/", "exports/")
MAX_BODY = 512 * 1024 * 1024

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")
mimetypes.add_type("text/plain", ".obj")
mimetypes.add_type("text/plain", ".mtl")
mimetypes.add_type("text/plain", ".usda")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("image/vnd.radiance", ".hdr")

_build_lock = threading.Lock()

# --- extension routes ----------------------------------------------------------
# Feature modules register API routes without editing this file:
#     ROUTES = [("GET", r"^/api/library/search$", handler), ...]
#     def handler(req): return {...}                      -> 200 JSON
#                       return 404, {...}                  -> JSON with status
#                       return 200, b"bytes", "image/png"  -> raw body
# `req` has .method .path .match (re.Match) .query (dict of lists) .body() .json()
EXTENSIONS = ["geobox.retrieval", "geobox.greybox", "geobox.farm.api"]
_routes = None


def extension_routes():
    global _routes
    if _routes is None:
        import importlib
        _routes = []
        for name in EXTENSIONS:
            try:
                mod = importlib.import_module(name)
            except ModuleNotFoundError as exc:
                if exc.name and name.startswith(exc.name):
                    continue  # feature not installed yet
                raise
            for method, pattern, fn in getattr(mod, "ROUTES", []):
                _routes.append((method, re.compile(pattern), fn))
    return _routes


class _Req:
    def __init__(self, handler, method, path, match, query):
        self._h, self.method, self.path, self.match, self.query = handler, method, path, match, query
        self._b = None

    def body(self) -> bytes:
        if self._b is None:
            self._b = self._h._body()
        return self._b

    def json(self):
        b = self.body()
        return json.loads(b.decode("utf-8")) if b else {}


def _inside(base: str, path: str) -> bool:
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base + os.sep)


def _library_for_web(lib: dict) -> dict:
    out = {k: v for k, v in lib.items() if not k.startswith("_")}
    assets = []
    for a in lib.get("assets", []):
        a = dict(a)
        web = (a.get("files") or {}).get("web")
        if web:
            a["web_url"] = f"/library/{urllib.parse.quote(a['id'])}/{urllib.parse.quote(os.path.basename(web))}"
        assets.append(a)
    out["assets"] = assets
    out["path"] = lib.get("_path")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoBox/0.1"

    # --- helpers -----------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter: only errors and API calls
        if self.path.startswith("/api") or (args and str(args[1]).startswith(("4", "5"))):
            super().log_message(fmt, *args)

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _err(self, code, msg, detail=None):
        self._json({"ok": False, "error": msg, "detail": detail}, code)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _body_json(self):
        b = self._body()
        return json.loads(b.decode("utf-8")) if b else {}

    def _file(self, path):
        if not os.path.isfile(path):
            return self._err(404, "not found", self.path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)

    # --- routing -------------------------------------------------------------
    def _try_extension(self, method) -> bool:
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        for m, rx, fn in extension_routes():
            if m != method and not (m == "GET" and method == "HEAD"):
                continue
            mt = rx.match(p)
            if not mt:
                continue
            res = fn(_Req(self, method, p, mt, urllib.parse.parse_qs(u.query)))
            if isinstance(res, tuple) and len(res) == 3:
                self._send(res[0], res[1], res[2])
            elif isinstance(res, tuple):
                self._json(res[1], res[0])
            else:
                self._json(res)
            return True
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            p = urllib.parse.unquote(u.path)
            q = urllib.parse.parse_qs(u.query)
            if p.startswith("/api/") and self._try_extension(self.command):
                return
            if p.startswith("/api/"):
                return self._api_get(p, q)
            if p.startswith("/library/"):
                return self._library_file(p)
            if p.startswith("/projects/"):
                return self._static(project.PROJECTS_DIR, p[len("/projects/"):])
            if p.startswith("/assets/"):
                return self._static(ASSETS_DIR, p[len("/assets/"):])
            if p in ("", "/"):
                p = "/index.html"
            return self._static(VIEWER_DIR, p.lstrip("/"))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, str(exc))

    def do_POST(self):
        self._write_dispatch("POST")

    def do_PUT(self):
        self._write_dispatch("PUT")

    def do_DELETE(self):
        self._write_dispatch("DELETE")

    def _write_dispatch(self, method):
        try:
            p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
            if self._try_extension(method):
                return
            if p == "/api/build" and method == "POST":
                return self._build()
            m = re.match(r"^/api/projects/([^/]+)/(scene|sky|rescatter|files/(.+))$", p)
            if not m:
                return self._err(404, "unknown endpoint", p)
            name = project.safe_name(m.group(1))
            what = m.group(2)
            d = project.project_dir(name)
            if what == "scene" and method == "PUT":
                scene = self._body_json()
                if scene.get("type") != "geobox.scene":
                    return self._err(400, "body is not a geobox.scene")
                if not os.path.exists(os.path.join(d, "layout.json")):
                    return self._err(404, "project has no layout.json")
                project.save_scene(name, scene)
                return self._json({"ok": True, "updated": scene.get("updated"), "instances": len(scene.get("instances", []))})
            if what == "sky" and method == "PUT":
                params = self._body_json()
                project.write_json(os.path.join(d, "sky_params.json"), params, indent=2)
                return self._json({"ok": True})
            if what == "rescatter" and method == "POST":
                body = self._body_json()
                ov = body.get("rules") or {}
                if body.get("seed") is not None:
                    ov["seed"] = int(body["seed"])
                logs = []
                with _build_lock:
                    scene = project.rescatter(name, rules_overrides=ov or None, log=logs.append)
                return self._json({"ok": True, "log": logs, "instances": len(scene["instances"])})
            if what.startswith("files/") and method == "PUT":
                rel = posixpath.normpath(m.group(3)).lstrip("/")
                if not rel.startswith(WRITABLE_PREFIXES) or ".." in rel.split("/"):
                    return self._err(403, "writes allowed only under %s" % ", ".join(WRITABLE_PREFIXES))
                full = os.path.join(d, rel)
                if not _inside(d, full):
                    return self._err(403, "bad path")
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(self._body())
                return self._json({"ok": True, "path": full})
            return self._err(405, "method not allowed")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._err(500, str(exc), traceback.format_exc(limit=3))

    # --- GET API -------------------------------------------------------------
    def _api_get(self, p, q):
        if p == "/api/projects":
            return self._json({"projects": project.list_projects()})
        if p == "/api/library":
            path = (q.get("path") or [libmod.DEFAULT_LIBRARY])[0]
            return self._json(_library_for_web(libmod.load(path)))
        if p == "/api/parse":
            try:
                return self._json({"ok": True, **parse_location((q.get("text") or [""])[0])})
            except ValueError as exc:
                return self._err(400, str(exc))
        m = re.match(r"^/api/projects/([^/]+)/(scene|sky|layout)$", p)
        if m:
            d = project.project_dir(m.group(1))
            fn = {"scene": "scene.json", "sky": "sky_params.json", "layout": "layout.json"}[m.group(2)]
            f = os.path.join(d, fn)
            if not os.path.exists(f):
                return self._err(404, f"{fn} not found")
            return self._file(f)
        return self._err(404, "unknown endpoint", p)

    def _library_file(self, p):
        parts = p.split("/")
        if len(parts) < 4:
            return self._err(404, "bad library path")
        aid = parts[2]
        lib = libmod.load(libmod.DEFAULT_LIBRARY)
        a = libmod.by_id(lib).get(aid)
        if not a:
            return self._err(404, "unknown asset", aid)
        web = libmod.resolve(lib, a["files"].get("web"))
        rest = "/".join(parts[3:])
        if rest != os.path.basename(web):
            # sibling resources (textures/.bin next to the model)
            web = os.path.join(os.path.dirname(web), rest)
            if not _inside(os.path.dirname(libmod.resolve(lib, a["files"]["web"])), web):
                return self._err(403, "bad path")
        return self._file(web)

    def _static(self, base, rel):
        full = os.path.normpath(os.path.join(base, rel))
        if not _inside(base, full):
            return self._err(403, "forbidden")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return self._file(full)

    def _build(self):
        body = self._body_json()
        name = project.safe_name(body.get("name") or "untitled")
        if body.get("at"):
            loc = parse_location(body["at"])
            center = loc["center"]
            size = body.get("size") or loc.get("suggested_size") or [500, 500]
        else:
            center, size = body["center"], body.get("size") or [500, 500]
        size = [min(max(float(size[0]), 20.0), 5000.0), min(max(float(size[1]), 20.0), 5000.0)]
        logs = []
        ov = {"seed": int(body["seed"])} if body.get("seed") is not None else None
        with _build_lock:
            scene = project.build(name, center, size, refetch=bool(body.get("refetch")), terrain=bool(body.get("terrain")),
                                  building_mode="clip" if body.get("clip_buildings") else "centroid",
                                  rules_overrides=ov, log=logs.append,
                                  greybox_source=body.get("greybox") or None)
        return self._json({"ok": True, "name": name, "log": logs, "instances": len(scene["instances"])})


def startup_check() -> list:
    """Problems that would make the viewer fail to load (printed at start-up)."""
    issues = []
    vendor = os.path.join(VIEWER_DIR, "vendor", "manifest.json")
    if os.path.exists(vendor):
        try:
            with open(vendor, "r", encoding="utf-8") as fh:
                v = json.load(fh).get("versions", {})
            print("front-end libs: viewer/vendor (" + ", ".join("%s@%s" % kv for kv in v.items()) + ")")
        except (OSError, ValueError) as exc:
            issues.append("viewer/vendor/manifest.json unreadable: %s" % exc)
    else:
        issues.append("viewer/vendor missing - re-download the repo, or run `cd viewer && npm install && python tools/vendor_js.py`")
    return issues


def serve(host="127.0.0.1", port=8765, open_url=None):
    """open_url: path such as "/?project=sample_synthetic" to open in the browser once listening."""
    if not os.path.exists(libmod.DEFAULT_LIBRARY):
        libmod.init_default()
    for msg in startup_check():
        print("!! " + msg)
    url = f"http://{host}:{port}"
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"!! cannot listen on {host}:{port} ({exc}).")
        print(f"   GeoBox may already be running - try {url}/ - or start with another --port.")
        if open_url:
            import webbrowser
            webbrowser.open(url + open_url)
        return 1
    print(f"GeoBox viewer:  {url}/        (sky editor: /sky.html)")
    print(f"projects dir:   {project.PROJECTS_DIR}")
    print("stop with Ctrl+C")
    if open_url:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url + open_url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
