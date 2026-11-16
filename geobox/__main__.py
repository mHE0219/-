"""Command line:  python -m geobox <command> ...   (see README.md)"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import library as libmod
from . import project
from .geo import parse_location


CLI_EXTENSIONS = ["geobox.retrieval", "geobox.greybox", "geobox.farm.cli"]


def _center_size(args):
    loc = parse_location(args.at)
    size = args.size or loc.get("suggested_size") or [500, 500]
    if len(size) == 1:
        size = [size[0], size[0]]
    return loc["center"], [float(size[0]), float(size[1])], loc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="geobox", description="Map area -> metric box -> asset scatter -> WebGL/Houdini")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="fetch OSM for an area, extract layout, scatter assets, export")
    b.add_argument("--name", required=True, help="project name (folder under projects/)")
    b.add_argument("--at", required=True, help='Google Maps URL or "lat, lon"')
    b.add_argument("--size", type=float, nargs="+", help="box size in metres: W [D] (default from URL zoom or 500)")
    b.add_argument("--terrain", action="store_true", help="sample terrain heights (AWS Terrain Tiles)")
    b.add_argument("--terrain-res", type=float, default=4.0, help="terrain grid spacing in metres")
    b.add_argument("--terrain-smooth", type=int, default=2, help="3x3 blur passes (SRTM includes some buildings in cities)")
    b.add_argument("--clip-buildings", action="store_true", help="cut building footprints at the box edge")
    b.add_argument("--refetch", action="store_true", help="ignore cached osm.json")
    b.add_argument("--osm-file", help="use a local Overpass JSON instead of downloading")
    b.add_argument("--greybox", choices=["osm", "empty"],
                   help="greybox source: osm = footprints as editable draft (default), empty = map is reference only")
    b.add_argument("--seed", type=int)
    b.add_argument("--library", help="path to library.json")

    r = sub.add_parser("rescatter", help="re-run scatter (keeps manual / locked edits)")
    r.add_argument("name")
    r.add_argument("--seed", type=int)
    r.add_argument("--rules", help="JSON file with rule overrides")
    r.add_argument("--library", help="path to library.json")

    e = sub.add_parser("export", help="re-export scene.usda from scene.json")
    e.add_argument("name")

    lp = sub.add_parser("library", help="asset library tools")
    lsub = lp.add_subparsers(dest="lcmd", required=True)
    li = lsub.add_parser("init", help="(re)generate proxy assets + manifest (keeps your own entries)")
    li.add_argument("--path", default=libmod.DEFAULT_LIBRARY)
    ln = lsub.add_parser("normalize", help="measure files, store standard bounds + normalize transforms")
    ln.add_argument("--path", default=libmod.DEFAULT_LIBRARY)
    ln.add_argument("--guess-units", action="store_true", help="guess source units/up-axis from plausible sizes")
    lc = lsub.add_parser("check", help="validate the library (units, missing files, sizes)")
    lc.add_argument("--path", default=libmod.DEFAULT_LIBRARY)
    ls = lsub.add_parser("scan", help="add new asset files from a folder")
    ls.add_argument("folder")
    ls.add_argument("--category", choices=["building", "tree", "prop"])
    ls.add_argument("--path", default=libmod.DEFAULT_LIBRARY)

    p = sub.add_parser("parse", help="show how a Google Maps URL is interpreted")
    p.add_argument("text")

    hi = sub.add_parser("houdini-install", help="write <houdini prefs>/packages/geobox.json for this repo")
    hi.add_argument("--prefs", help="Houdini user pref dir (default: auto-detect all houdiniXX.Y)")

    s = sub.add_parser("serve", help="start the web viewer / editor")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")

    # feature modules add sub-commands: def add_cli(sub): p = sub.add_parser(...); p.set_defaults(func=handler)
    import importlib
    for name in CLI_EXTENSIONS:
        try:
            importlib.import_module(name).add_cli(sub)
        except ModuleNotFoundError as exc:
            if not (exc.name and name.startswith(exc.name)):
                raise
        except AttributeError:
            pass

    args = ap.parse_args(argv)
    if getattr(args, "func", None):
        return args.func(args)

    if args.cmd == "build":
        center, size, loc = _center_size(args)
        print(f"[geobox] centre {center[0]:.6f}, {center[1]:.6f}  box {size[0]:.0f} x {size[1]:.0f} m ({loc.get('kind')})")
        overrides = {"seed": args.seed} if args.seed is not None else None
        scene = project.build(args.name, center, size, refetch=args.refetch, terrain=args.terrain,
                              terrain_resolution=args.terrain_res, terrain_smooth=args.terrain_smooth,
                              building_mode="clip" if args.clip_buildings else "centroid",
                              rules_overrides=overrides, library_path=args.library, osm_file=args.osm_file,
                              greybox_source=args.greybox)
        d = project.project_dir(args.name)
        print(f"[geobox] {len(scene['instances'])} instances -> {d}/scene.json, scene.usda")
    elif args.cmd == "rescatter":
        ov = {}
        if args.rules:
            with open(args.rules, encoding="utf-8") as fh:
                ov = json.load(fh)
        if args.seed is not None:
            ov["seed"] = args.seed
        scene = project.rescatter(args.name, rules_overrides=ov or None, library_path=args.library)
        print(f"[geobox] {len(scene['instances'])} instances")
    elif args.cmd == "export":
        scene = project.read_json(os.path.join(project.project_dir(args.name), "scene.json"))
        print(project.save_scene(args.name, scene))
    elif args.cmd == "library":
        if args.lcmd == "init":
            lib = libmod.init_default(args.path)
            print(f"[library] {len(lib['assets'])} assets -> {args.path}")
            return
        lib = libmod.load(args.path)
        if args.lcmd == "normalize":
            notes = []
            for a in lib["assets"]:
                notes += libmod.normalize_entry(lib, a, guess=args.guess_units)
            libmod.save(lib)
        elif args.lcmd == "scan":
            notes = libmod.scan(lib, args.folder, args.category)
            libmod.save(lib)
        else:
            notes = libmod.check(lib)
        print("\n".join(notes) if notes else "[library] OK - %d assets" % len(lib["assets"]))
    elif args.cmd == "parse":
        print(json.dumps(parse_location(args.text), indent=2, ensure_ascii=False))
    elif args.cmd == "houdini-install":
        from .houdini_install import install
        for w in install(args.prefs):
            print("[houdini] wrote", w)
    elif args.cmd == "serve":
        from .server import serve
        serve(args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
