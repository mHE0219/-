"""Farm job types. Each handler gets a JobContext and returns a JSON-able result.

    ingest_asset   one model file -> library.json entry (units/up-axis guessed, measured,
                   normalised) -> retrieval index (+ thumbnail request / Houdini follow-up)
    ingest_folder  fan-out: ingest_asset per new file, then [hython prep] -> reindex
    reindex        rebuild the retrieval index (assets/index.json)
    fetch_osm      one Overpass download for a whole district (shared by its tiles)
    build_box      geobox.project.build for one box
    build_tiles    split a district into N x M boxes -> fetch_osm + build_box per tile + tiles.json
    rescatter      re-run scatter of a project (keeps manual edits)
    export_usd     re-export scene.usda
    hython         run a Houdini script on a node that has Houdini (requires tag "houdini")

Add a type with:   @job("my_type", doc="...")   def my_type(ctx): ...
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional

from .. import library as libmod
from .. import project
from ..geo import BoxSpec, LocalFrame, parse_location
from .queue import REPO_ROOT, file_lock, map_path
from ..fsutil import relpath

JOB_TYPES: Dict[str, dict] = {}

MODEL_EXTS = (".glb", ".gltf", ".obj", ".fbx", ".usd", ".usda", ".usdc", ".usdz", ".abc", ".bgeo", ".sc", ".bgeo.sc")
PURE_PY_MEASURABLE = (".glb", ".gltf", ".obj", ".usda")
PATH_KEYS = ("path", "folder", "library", "osm_file", "projects_root", "script", "out", "hip")


class Cancelled(Exception):
    pass


def job(name: str, requires=(), doc: str = ""):
    def deco(fn: Callable):
        JOB_TYPES[name] = {"fn": fn, "requires": list(requires), "doc": doc or (fn.__doc__ or "").strip().split("\n")[0]}
        return fn
    return deco


def describe() -> List[dict]:
    return [{"type": k, "requires": v["requires"], "doc": v["doc"]} for k, v in sorted(JOB_TYPES.items())]


class JobContext:
    """What a handler sees. Also usable standalone (farm=None) e.g. from PDG work items."""

    def __init__(self, farm, job: dict, worker=None, log_fn=None):
        self.farm, self.job, self.worker = farm, job, worker
        self.params = {k: (map_path(v) if k in PATH_KEYS else v) for k, v in (job.get("params") or {}).items()}
        self.children: List[str] = []
        self._log_fn = log_fn

    @property
    def id(self) -> str:
        return self.job.get("id", "local")

    def log(self, *msg) -> None:
        text = " ".join(str(m) for m in msg)
        if self.farm is not None:
            self.farm.log(self.id, text)
        if self._log_fn:
            self._log_fn(text)

    def progress(self, f: float) -> None:
        self.job["progress"] = max(0.0, min(1.0, float(f)))

    def check_cancel(self) -> None:
        if self.farm is not None and self.farm.cancel_requested(self.id):
            raise Cancelled("cancelled by user")

    def submit(self, job_type: str, params: dict, **kw) -> str:
        if self.farm is None:
            # standalone (no queue): run inline, depth-first
            sub = JobContext(None, {"id": "inline", "type": job_type, "params": params}, log_fn=self._log_fn)
            run(job_type, sub)
            self.children.append("inline:" + job_type)
            return "inline"
        kw.setdefault("priority", self.job.get("priority", 50))
        j = self.farm.submit(job_type, params, parent=self.id, **kw)
        self.children.append(j["id"])
        return j["id"]


def run(job_type: str, ctx: JobContext):
    if job_type not in JOB_TYPES:
        raise ValueError("unknown job type %r (known: %s)" % (job_type, ", ".join(sorted(JOB_TYPES))))
    return JOB_TYPES[job_type]["fn"](ctx)


# --- helpers ------------------------------------------------------------------------------
def library_path(params: dict) -> str:
    return params.get("library") or os.environ.get("GEOBOX_LIBRARY") or libmod.DEFAULT_LIBRARY


def projects_root(params: dict) -> Optional[str]:
    return params.get("projects_root") or None  # None -> project.PROJECTS_DIR


def _lib_lock(lib_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(lib_path)), ".geobox_library.lock")


def _ext(path: str) -> str:
    low = path.lower()
    return ".bgeo.sc" if low.endswith(".bgeo.sc") else os.path.splitext(low)[1]


def _rel(path: str, root: str) -> str:
    try:
        return relpath(path, root)
    except ValueError:  # different Windows drive -> keep absolute
        return os.path.abspath(path).replace(os.sep, "/")


def _guess_category(path: str) -> str:
    low = path.replace("\\", "/").lower()
    if re.search(r"build|bldg|house|tower|建築", low):
        return "building"
    if re.search(r"tree|veg|plant|bush|shrub|palm|樹|植", low):
        return "tree"
    return "prop"


def _safe_id(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "_", s).strip("_.")
    return s or "asset"


def ingest_file(path: str, *, category: Optional[str] = None, lib_path: Optional[str] = None,
                asset_id: Optional[str] = None, tags=None, units: Optional[str] = None, up: Optional[str] = None,
                label: Optional[str] = None, force: bool = False, log=print) -> dict:
    """Add/refresh one model in the library + retrieval index (locks library.json).
    Shared by the queue job, the PDG work item and tests."""
    from .. import retrieval

    lib_path = lib_path or libmod.DEFAULT_LIBRARY
    full = os.path.normpath(os.path.abspath(path))
    if not os.path.isfile(full):
        raise FileNotFoundError(full)
    ext = _ext(full)
    out = {"path": full.replace(os.sep, "/")}
    with file_lock(_lib_lock(lib_path)):
        lib = libmod.load(lib_path)
        existing = None
        for a in lib["assets"]:
            for f in (a.get("files") or {}).values():
                if f and os.path.normcase(os.path.normpath(libmod.resolve(lib, f))) == os.path.normcase(full):
                    existing = a
                    break
            if existing:
                break
        if existing and not force and existing.get("bounds"):
            aid = existing["id"]
            out.update(id=aid, action="exists")
        else:
            cat = category or (existing or {}).get("category") or _guess_category(full)
            if existing:
                a = existing
                action = "updated"
            else:
                base = os.path.basename(full)
                base = base[: -len(ext)] if base.lower().endswith(ext) else os.path.splitext(base)[0]
                aid = _safe_id(asset_id or base)
                ids = {x["id"] for x in lib["assets"]}
                k, cand = 2, aid
                while cand in ids:
                    cand, k = "%s_%d" % (aid, k), k + 1
                aid = cand
                rel = _rel(full, lib["_root"])
                files = {"houdini": rel}
                if ext in (".glb", ".gltf", ".obj", ".fbx"):
                    files["web"] = rel
                if ext.startswith(".usd"):
                    files["usd"] = rel
                tag_list = [cat] + ([base.split("_")[0].lower()] if "_" in base else [])
                a = {"id": aid, "category": cat, "label": label or base, "files": files,
                     "tags": sorted(set(tag_list + list(tags or []))), "ingested_by": "farm"}
                lib["assets"].append(a)
                action = "added"
            if units or up:
                a["source_units"] = units or a.get("source_units", "m")
                a["source_up"] = up or a.get("source_up", "+Y")
                a["units_confirmed"] = True
            libmod._fill_defaults(a)  # noqa: SLF001 - shared defaults
            notes = libmod.normalize_entry(lib, a, guess=not (units or up))
            for n in notes:
                log("  " + n)
            libmod.save(lib)
            aid = a["id"]
            out.update(id=aid, action=action, notes=notes, units=a.get("source_units"), up=a.get("source_up"))
    lib = libmod.load(lib_path)
    a = libmod.by_id(lib)[aid]
    out["has_bounds"] = bool(a.get("bounds"))
    out["needs_houdini"] = not out["has_bounds"] and ext not in PURE_PY_MEASURABLE
    if out["has_bounds"]:
        w, h, d = libmod.asset_size(a)
        out["size"] = [round(w, 3), round(h, 3), round(d, 3)]
    try:
        retrieval.rebuild_index(lib, [aid])
        out["indexed"] = True
    except Exception as exc:  # noqa: BLE001 - index is best-effort; reindex job will retry
        out["indexed"] = False
        log("  index update failed: %s" % exc)
    tp = getattr(retrieval, "thumb_path", None)
    has_thumb = bool(tp and os.path.exists(tp(lib, aid)))
    out["needs_thumbnail"] = not has_thumb
    log("ingest %s -> %s (%s)%s" % (os.path.basename(full), aid, out["action"],
                                     "" if out["has_bounds"] else "  [no bounds yet: needs Houdini prep]"))
    return out


# --- job types ------------------------------------------------------------------------------
@job("ingest_asset", doc="One model file -> library entry (units guessed, measured) -> retrieval index")
def ingest_asset(ctx: JobContext):
    p = ctx.params
    res = ingest_file(p["path"], category=p.get("category"), lib_path=library_path(p), asset_id=p.get("id"),
                      tags=p.get("tags"), units=p.get("units"), up=p.get("up"), label=p.get("label"),
                      force=bool(p.get("force")), log=ctx.log)
    if res.get("needs_houdini") and p.get("followups", True):
        prep = ctx.submit("hython", {"preset": "prep_library", "library": library_path(p)},
                          requires=["houdini"], label="houdini prep " + res["id"])
        ctx.submit("reindex", {"library": library_path(p), "ids": [res["id"]]}, deps=[prep], label="reindex")
        res["followups"] = ctx.children[-2:]
    return res


@job("ingest_folder", doc="Fan-out ingest_asset for every new model in a folder, then reindex")
def ingest_folder(ctx: JobContext):
    p = ctx.params
    folder = p["folder"]
    if not os.path.isdir(folder):
        raise FileNotFoundError(folder)
    exts = tuple(e.lower() for e in (p.get("exts") or MODEL_EXTS))
    lp = library_path(p)
    lib = libmod.load(lp)
    known = set()
    for a in lib["assets"]:
        for f in (a.get("files") or {}).values():
            if f:
                known.add(os.path.normcase(os.path.normpath(libmod.resolve(lib, f))))
    files = []
    for dirpath, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith((".", "houdini_cache", "thumbs"))]
        if not p.get("recursive", True) and dirpath != folder:
            continue
        for n in sorted(names):
            full = os.path.normpath(os.path.join(dirpath, n))
            if _ext(full) in exts and (p.get("force") or os.path.normcase(full) not in known):
                files.append(full)
    ctx.log("found %d new model files under %s" % (len(files), folder))
    children = []
    needs_houdini = False
    for i, f in enumerate(files):
        ctx.check_cancel()
        params = {k: p[k] for k in ("category", "tags", "units", "up", "force") if k in p}
        params.update(path=f, library=lp, followups=False)
        children.append(ctx.submit("ingest_asset", params, label="ingest " + os.path.basename(f)))
        needs_houdini |= _ext(f) not in PURE_PY_MEASURABLE
        ctx.progress((i + 1) / max(len(files), 1) * 0.9)
    reindex = ctx.submit("reindex", {"library": lp}, deps=children, label="reindex library") if files else None
    prep = reindex2 = None
    if needs_houdini and p.get("houdini_prep", True):
        # only a worker with Houdini takes this; the pure-Python assets are already searchable meanwhile
        prep = ctx.submit("hython", {"preset": "prep_library", "library": lp}, deps=children, requires=["houdini"],
                          label="houdini prep (fbx/abc/bgeo/usd)")
        reindex2 = ctx.submit("reindex", {"library": lp}, deps=[prep], label="reindex after houdini prep")
    return {"files": len(files), "children": children, "reindex": reindex, "houdini_prep": prep,
            "reindex_after_prep": reindex2}


@job("reindex", doc="Rebuild the retrieval index (assets/index.json)")
def reindex(ctx: JobContext):
    from .. import retrieval
    lib = libmod.load(library_path(ctx.params))
    meta = retrieval.rebuild_index(lib, ctx.params.get("ids"), force=bool(ctx.params.get("force")))
    missing_thumbs = [k for k, v in meta.items() if not v.get("thumbnail")]
    ctx.log("index: %d assets, %d without thumbnail" % (len(meta), len(missing_thumbs)))
    return {"assets": len(meta), "missing_thumbnails": missing_thumbs[:200]}


@job("fetch_osm", doc="Download OSM (Overpass) once for a district; tiles reuse the file")
def fetch_osm(ctx: JobContext):
    from .. import osm as osmmod
    p = ctx.params
    if p.get("bbox"):
        bbox = p["bbox"]
    else:
        lat, lon = _center(p)
        bbox = BoxSpec(lat, lon, *p["size"]).latlon_bbox(margin=float(p.get("margin", 40.0)))
    data = osmmod.fetch_overpass(bbox, log=ctx.log)
    os.makedirs(os.path.dirname(os.path.abspath(p["out"])), exist_ok=True)
    project.write_json(p["out"], data)
    return {"out": p["out"], "elements": len(data.get("elements", [])), "bbox": list(bbox)}


def _center(p: dict):
    if p.get("center"):
        return float(p["center"][0]), float(p["center"][1])
    if not p.get("at"):
        raise ValueError("needs 'center' [lat, lon] or 'at' (Google Maps URL or 'lat, lon')")
    loc = parse_location(p["at"])
    return loc["center"][0], loc["center"][1]


@job("build_box", doc="Build one box: OSM -> layout -> scatter -> scene.json/usda")
def build_box(ctx: JobContext):
    p = ctx.params
    lat, lon = _center(p)
    size = p.get("size") or [500, 500]
    scene = project.build(p["name"], (lat, lon), (float(size[0]), float(size[1])),
                          refetch=bool(p.get("refetch")), terrain=bool(p.get("terrain")),
                          terrain_resolution=float(p.get("terrain_resolution", 4.0)),
                          building_mode="clip" if p.get("clip_buildings") else "centroid",
                          rules_overrides={"seed": int(p["seed"])} if p.get("seed") is not None else None,
                          library_path=p.get("library"), osm_file=p.get("osm_file"),
                          root=projects_root(p), log=ctx.log)
    return {"name": project.safe_name(p["name"]), "instances": len(scene["instances"]),
            "stats": scene.get("stats", {})}


def tile_plan(center, size, tile: Optional[float] = 500.0, grid=None) -> dict:
    """Split a W x D district (metres) into tiles. Rows go north -> south, cols west -> east.
    Each tile is its own box (own ENU frame at its centre); `offset` is the tile centre in the
    district frame, i.e. the translation to assemble tiles (tangent-plane rotation between
    tiles is < 0.01 deg for districts of a few km, i.e. sub-centimetre)."""
    W, D = float(size[0]), float(size[1])
    if grid:
        nx, nz = int(grid[0]), int(grid[1])
    else:
        nx, nz = max(1, math.ceil(W / tile - 1e-9)), max(1, math.ceil(D / tile - 1e-9))
    tw, td = W / nx, D / nz
    frame = LocalFrame(float(center[0]), float(center[1]))
    tiles = []
    for j in range(nz):
        for i in range(nx):
            x = -W / 2 + (i + 0.5) * tw
            z = -D / 2 + (j + 0.5) * td
            lat, lon = frame.from_local(x, z)
            tiles.append({"row": j, "col": i, "center": [round(lat, 8), round(lon, 8)],
                          "offset": [round(x, 3), round(z, 3)], "size": [round(tw, 3), round(td, 3)]})
    return {"center": [float(center[0]), float(center[1])], "size": [W, D], "grid": [nx, nz],
            "tile_size": [round(tw, 3), round(td, 3)], "tiles": tiles}


@job("build_tiles", doc="Split a district into N x M boxes and build them in parallel on the farm")
def build_tiles(ctx: JobContext):
    p = ctx.params
    name = project.safe_name(p["name"])
    lat, lon = _center(p)
    plan = tile_plan((lat, lon), p["size"], float(p.get("tile", 500.0)), p.get("grid"))
    proot = projects_root(p) or project.PROJECTS_DIR
    ddir = os.path.join(proot, "_district", name)
    os.makedirs(ddir, exist_ok=True)
    deps, osm_file = [], p.get("osm_file")
    if not osm_file:
        osm_file = os.path.join(ddir, "osm.json")
        deps = [ctx.submit("fetch_osm", {"center": [lat, lon], "size": plan["size"], "out": osm_file},
                           label="fetch OSM " + name, max_retries=3)]
    ctx.log("district %s: %d x %d tiles of %.0f x %.0f m" % (name, plan["grid"][0], plan["grid"][1], *plan["tile_size"]))
    for t in plan["tiles"]:
        ctx.check_cancel()
        t["name"] = "%s_r%02dc%02d" % (name, t["row"], t["col"])
        params = {"name": t["name"], "center": t["center"], "size": t["size"], "osm_file": osm_file}
        for k in ("terrain", "terrain_resolution", "seed", "clip_buildings", "library", "projects_root"):
            if k in p:
                params[k] = p[k]
        t["job"] = ctx.submit("build_box", params, deps=deps, label="tile " + t["name"])
    plan.update(name=name, osm_file=osm_file, version=1, type="geobox.district")
    manifest = os.path.join(ddir, "tiles.json")
    project.write_json(manifest, plan, indent=1)
    return {"manifest": manifest, "tiles": len(plan["tiles"]), "grid": plan["grid"], "fetch": deps[0] if deps else None}


@job("rescatter", doc="Re-run scatter for a project (keeps manual / locked edits)")
def rescatter(ctx: JobContext):
    p = ctx.params
    ov = dict(p.get("rules") or {})
    if p.get("seed") is not None:
        ov["seed"] = int(p["seed"])
    scene = project.rescatter(p["name"], rules_overrides=ov or None, library_path=p.get("library"),
                              root=projects_root(p), log=ctx.log)
    return {"name": p["name"], "instances": len(scene["instances"])}


@job("export_usd", doc="Re-export scene.usda from scene.json")
def export_usd(ctx: JobContext):
    p = ctx.params
    d = project.project_dir(p["name"], projects_root(p))
    scene = project.read_json(os.path.join(d, "scene.json"))
    project.save_scene(p["name"], scene, root=projects_root(p))
    return {"usd": os.path.join(d, "scene.usda")}


HYTHON_PRESETS = {
    # preset: (code template, holds the library lock)
    "prep_library": ("import geobox_houdini.asset_prep as ap\nap.prep_library({library!r}, force={force!r})\n", True),
    "cook_top": ("import hou\nhou.hipFile.load({hip!r}, suppress_save_prompt=True, ignore_load_warnings=True)\n"
                 "n = hou.node({node!r})\nn.cookWorkItems(block=True)\nprint('cooked', n.path())\n", False),
    "build_city": ("import hou\nimport geobox_houdini.build as b\nb.build_city({scene!r})\n"
                   "hou.hipFile.save({hip!r})\n", False),
}


def hython_bin() -> Optional[str]:
    return os.environ.get("GEOBOX_HYTHON") or shutil.which("hython")


@job("hython", requires=["houdini"], doc="Run a Houdini (hython) preset or script on a node with Houdini")
def hython(ctx: JobContext):
    p = ctx.params
    exe = hython_bin()
    if not exe:
        raise RuntimeError("no hython on this worker (set GEOBOX_HYTHON or put hython on PATH)")
    lock = None
    if p.get("preset"):
        tpl, needs_lock = HYTHON_PRESETS[p["preset"]]
        fmt = {"library": p.get("library") or libmod.DEFAULT_LIBRARY, "force": bool(p.get("force")),
               "hip": p.get("hip", ""), "node": p.get("node", ""), "scene": p.get("scene", "")}
        code = tpl.format(**fmt)
        if needs_lock:
            lock = _lib_lock(fmt["library"])
        script, cleanup = _write_tmp(code), True
    elif p.get("script"):
        script, cleanup = p["script"], False
        if not os.path.isfile(script):
            raise FileNotFoundError(script)
    elif p.get("code"):
        if not getattr(ctx.worker, "allow_code", False):
            raise PermissionError("inline code jobs need a worker started with --allow-code")
        script, cleanup = _write_tmp(p["code"]), True
    else:
        raise ValueError("hython job needs 'preset', 'script' or 'code'")
    env = dict(os.environ)
    env["GEOBOX"] = REPO_ROOT
    pp = [os.path.join(REPO_ROOT, "houdini", "python"), REPO_ROOT]
    env["PYTHONPATH"] = os.pathsep.join(pp + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    cmd = [exe, script] + [str(a) for a in p.get("args", [])]
    ctx.log("$ " + " ".join(cmd))
    try:
        if lock:
            with file_lock(lock):
                rc = _run_logged(cmd, env, ctx, float(p.get("timeout", 3600)))
        else:
            rc = _run_logged(cmd, env, ctx, float(p.get("timeout", 3600)))
    finally:
        if cleanup:
            try:
                os.remove(script)
            except OSError:
                pass
    if rc != 0:
        raise RuntimeError("hython exited with %d" % rc)
    return {"returncode": rc, "preset": p.get("preset")}


def _write_tmp(code: str) -> str:
    fd, path = tempfile.mkstemp(prefix="geobox_job_", suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code)
    return path


def _run_logged(cmd, env, ctx: JobContext, timeout: float) -> int:
    import time
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    t0 = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        ctx.log(line.rstrip())
        if ctx.farm is not None and ctx.farm.cancel_requested(ctx.id):
            proc.kill()
            raise Cancelled("cancelled by user")
        if time.time() - t0 > timeout:
            proc.kill()
            raise TimeoutError("hython timed out after %.0fs" % timeout)
    return proc.wait()
