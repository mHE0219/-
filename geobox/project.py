"""Project orchestration: fetch -> layout -> scatter -> exports.

projects/<name>/
    project.json  osm.json  terrain.json  layout.json  scene.json  scene.usda
    sky_params.json  sky/  exports/
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Optional

from . import library as libmod
from . import osm as osmmod
from .export_usd import export_usda
from .geo import BoxSpec
from .layout import extract_layout
from .scatter import load_rules, scatter
from .terrain import sample_terrain  # noqa: F401  (smooth imported lazily)
from .fsutil import atomic_replace, relpath

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.environ.get("GEOBOX_PROJECTS", os.path.join(REPO_ROOT, "projects"))

SCENE_VERSION = 1


def safe_name(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9_\-]+", "_", name.strip())[:64].strip("_")
    if not n:
        raise ValueError("invalid project name")
    return n


def project_dir(name: str, root: Optional[str] = None) -> str:
    return os.path.join(root or PROJECTS_DIR, safe_name(name))


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, data, indent: Optional[int] = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    atomic_replace(tmp, path)
    return path


def write_scene_json(path: str, scene: dict) -> str:
    """scene.json with one compact line per instance / greybox block: ~3x smaller
    than indent=1 for kit-dressed scenes, still diff- and grep-friendly, and
    plain JSON for every reader (Houdini, viewer, USD export)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    big = ("instances", "greybox")
    head = {k: v for k, v in scene.items() if k not in big}
    parts = []
    for k, v in head.items():
        parts.append(" %s: %s" % (json.dumps(k), json.dumps(v, ensure_ascii=False, separators=(",", ":"))))
    for k in big:
        if k in scene:
            rows = ",\n  ".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in scene[k])
            parts.append(" %s: [\n  %s\n ]" % (json.dumps(k), rows) if rows else " %s: []" % json.dumps(k))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("{\n" + ",\n".join(parts) + "\n}\n")
    atomic_replace(tmp, path)
    return path


def list_projects(root: Optional[str] = None):
    root = root or PROJECTS_DIR
    out = []
    if not os.path.isdir(root):
        return out
    for n in sorted(os.listdir(root)):
        pj = os.path.join(root, n, "project.json")
        if os.path.exists(pj):
            try:
                meta = read_json(pj)
            except Exception:  # noqa: BLE001
                continue
            meta["name"] = n
            meta["has_scene"] = os.path.exists(os.path.join(root, n, "scene.json"))
            out.append(meta)
    return out


def build(name: str, center, size, *, refetch: bool = False, terrain: bool = False,
          terrain_resolution: float = 4.0, terrain_smooth: int = 2, building_mode: str = "centroid",
          rules_overrides: Optional[dict] = None, library_path: Optional[str] = None,
          osm_file: Optional[str] = None, root: Optional[str] = None, greybox_source: Optional[str] = None,
          log=print) -> dict:
    """Full pipeline for one box. Returns the scene dict.

    greybox_source: "osm" = OSM footprints become a draft greybox (editable);
                    "empty" = map is reference only, designers draw the greybox.
                    None keeps the existing project's choice (default "osm")."""
    d = project_dir(name, root)
    os.makedirs(d, exist_ok=True)
    box = BoxSpec(float(center[0]), float(center[1]), float(size[0]), float(size[1]))
    meta = {"name": safe_name(name), "center": [box.lat, box.lon], "size": [box.width, box.depth],
            "terrain": terrain, "building_mode": building_mode,
            "updated": _dt.datetime.now().isoformat(timespec="seconds")}
    write_json(os.path.join(d, "project.json"), meta, indent=2)

    # 1. fetch (cached) -------------------------------------------------------
    osm_path = os.path.join(d, "osm.json")
    if osm_file:
        osm = read_json(osm_file)
        write_json(osm_path, osm)
    elif os.path.exists(osm_path) and not refetch and _cache_covers(d, box):
        log("[osm] using cached osm.json")
        osm = read_json(osm_path)
    else:
        bbox = box.latlon_bbox(margin=30.0)
        osm = osmmod.fetch_overpass(bbox, log=log)
        write_json(osm_path, osm)
        write_json(os.path.join(d, "osm_bbox.json"), {"bbox": bbox, "center": meta["center"], "size": meta["size"]})

    # 2. layout -----------------------------------------------------------------
    layout = extract_layout(osm, box, building_mode=building_mode, log=log)
    if terrain:
        tpath = os.path.join(d, "terrain.json")
        t = None
        if os.path.exists(tpath) and not refetch:
            t = read_json(tpath)
            if t.get("rect") != list(box.local_rect()):
                t = None
        if t is None:
            try:
                t = sample_terrain(box, resolution=terrain_resolution, log=log)
                write_json(tpath, t)
            except Exception as exc:  # noqa: BLE001
                log(f"[terrain] failed ({exc}); continuing flat")
        if t:
            if terrain_smooth:
                from .terrain import smooth
                t = smooth(t, terrain_smooth)
            layout["terrain"] = t
            layout["origin"]["elevation"] = t.get("base_elevation", 0.0)
    write_json(os.path.join(d, "layout.json"), layout)

    # 3. scatter (keeps manual edits of an existing scene) ------------------------
    return rescatter(name, rules_overrides=rules_overrides, library_path=library_path, root=root, log=log,
                     greybox_source=greybox_source)


def _cache_covers(d: str, box: BoxSpec) -> bool:
    p = os.path.join(d, "osm_bbox.json")
    if not os.path.exists(p):
        return True  # hand-provided osm.json
    s0, w0, n0, e0 = read_json(p)["bbox"]
    s, w, n, e = box.latlon_bbox()
    return s >= s0 and w >= w0 and n <= n0 and e <= e0


GREYBOX_SOURCES = ("osm", "empty")


def rescatter(name: str, rules_overrides: Optional[dict] = None, library_path: Optional[str] = None,
              root: Optional[str] = None, log=print, greybox_source: Optional[str] = None) -> dict:
    d = project_dir(name, root)
    layout = read_json(os.path.join(d, "layout.json"))
    scene_path = os.path.join(d, "scene.json")
    previous = read_json(scene_path) if os.path.exists(scene_path) else None
    rules = (previous or {}).get("rules") or load_rules()
    if rules_overrides:
        from .scatter import _deep_update
        _deep_update(rules, rules_overrides)
    lib_path = library_path or _scene_library_path(previous, d)
    if not os.path.exists(lib_path):
        lib_path = libmod.DEFAULT_LIBRARY
    if not os.path.exists(lib_path):
        libmod.init_default(lib_path)
    lib = libmod.load(lib_path)
    from .greybox import blocks_from_layout, merge_blocks
    from .terrain import sampler
    source = greybox_source or (previous or {}).get("greybox_source") or "osm"
    if source not in GREYBOX_SOURCES:
        raise ValueError("greybox_source must be one of %s" % (GREYBOX_SOURCES,))
    fresh = blocks_from_layout(layout, sampler(layout.get("terrain")), rules) if source == "osm" else []
    blocks, binfo = merge_blocks(fresh, previous)
    if binfo.get("kept") or binfo.get("suppressed"):
        log(f"[greybox] kept {binfo['kept']} edited/manual blocks, {binfo['suppressed']} still deleted")
    instances, info = scatter(layout, lib, rules, previous=previous, log=log, blocks=blocks)
    if info.get("kept"):
        log(f"[scatter] kept {info['kept']} manual/locked instances, {info['suppressed']} still deleted")
    scene = {
        "version": SCENE_VERSION, "type": "geobox.scene", "name": safe_name(name),
        "layout_file": "layout.json",
        "library": relpath(lib_path, d),
        "box": layout["box"], "origin": layout["origin"],
        "rules": rules,
        "greybox_source": source,
        "greybox": blocks,
        "suppressed_blocks": (previous or {}).get("suppressed_blocks", []),
        "instances": instances,
        "suppressed": (previous or {}).get("suppressed", []),
        "stats": info.get("stats", {}),
        "updated": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    save_scene(name, scene, layout=layout, lib=lib, root=root)
    return scene


def _scene_library_path(scene: Optional[dict], d: str) -> str:
    """Relative `library` first (portable), then the absolute hint, then the default."""
    scene = scene or {}
    for cand in (os.path.join(d, scene["library"]) if scene.get("library") else None, scene.get("library_abs")):
        if cand and os.path.exists(cand):
            return os.path.normpath(cand)
    return libmod.DEFAULT_LIBRARY


def save_scene(name: str, scene: dict, layout: Optional[dict] = None, lib: Optional[dict] = None,
               root: Optional[str] = None, export_usd: bool = True) -> str:
    d = project_dir(name, root)
    scene["updated"] = _dt.datetime.now().isoformat(timespec="seconds")
    path = write_scene_json(os.path.join(d, "scene.json"), scene)
    if export_usd:
        layout = layout or read_json(os.path.join(d, "layout.json"))
        if lib is None:
            lib = libmod.load(_scene_library_path(scene, d))
        export_usda(scene, layout, lib, os.path.join(d, "scene.usda"))
    return path
