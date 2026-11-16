#!/usr/bin/env hython
"""GeoBox Houdini self-test — run the REAL Houdini code paths and write a report.

    hython houdini/scripts/geobox_selftest.py [--project projects/sample_synthetic]
                                              [--report <dir or .txt>] [--hip <path.hip>]
                                              [--skip pdg,sky,solaris]
    (or the "Self-test" shelf tool / "Run Self-test" button: same checks, in-process)

* Works on a COPY of the project (projects/_selftest/<name>), never on your files.
* Every check is isolated: a failure is recorded (with the Houdini error text / the parm
  names this Houdini version actually has) and the next check still runs.
* Writes selftest_report.txt (human readable) + selftest_report.json; exit code 0 = all
  PASS/WARN/SKIP, 1 = at least one FAIL, 2 = could not start (no `hou`).
Send the .txt back when something fails — it contains what is needed to fix it.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import shutil
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
REQUIRED_NODES = ("GREYBOX_SEED", "OUT_GREYBOX", "DRESS", "OUT_INSTANCES", "LIBRARY", "PACK_LIBRARY",
                  "COPY_TO_POINTS", "REFERENCE", "OUT")
GEO_NAME = "geobox_selftest"


def _paths():
    for p in (ROOT, os.path.join(ROOT, "houdini", "python")):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("GEOBOX", ROOT.replace("\\", "/"))


class Ctx:
    def __init__(self, hou, project, out_dir, hip=None, inprocess=False, skip=(), cook=None):
        self.hou = hou
        self.src_project = os.path.abspath(project)
        self.out_dir = os.path.abspath(out_dir)
        self.hip = hip
        self.inprocess = inprocess
        self.skip = set(skip)
        self._cook = cook
        self.work = os.path.join(self.out_dir, os.path.basename(self.src_project.rstrip("/\\")))
        self.scene_path = os.path.join(self.work, "scene.json")
        self.geo = None
        self.results = []
        self.state = {}

    def cook(self, node):
        if self._cook is not None:
            return self._cook(node)
        return node.cook(force=True)

    def node(self, name):
        n = self.geo.node(name) if self.geo is not None else None
        if n is None:
            raise RuntimeError("node %s not found in the GeoBox network" % name)
        return n

    def geometry(self, name):
        n = self.node(name)
        self.cook(n)
        return n.geometry()


CHECKS = []


def check(name, needs=(), tag=None):
    def deco(fn):
        CHECKS.append((name, fn, tuple(needs), tag or name))
        return fn
    return deco


def _scene(ctx):
    with open(ctx.scene_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _node_msgs(node):
    out = {}
    for kind in ("errors", "warnings"):
        try:
            msgs = list(getattr(node, kind)())
        except Exception:  # noqa: BLE001
            msgs = []
        if msgs:
            out[kind] = [str(m) for m in msgs]
    return out


# --- checks ----------------------------------------------------------------------------------
@check("environment")
def c_environment(ctx):
    hou = ctx.hou
    d = {"python": sys.version.split()[0], "platform": platform.platform(), "repo": ROOT}
    for k, fn in (("houdini", "applicationVersionString"), ("license", "licenseCategory")):
        try:
            d[k] = str(getattr(hou, fn)())
        except Exception:  # noqa: BLE001
            d[k] = "?"
    d["GEOBOX_env"] = os.environ.get("GEOBOX")
    import geobox  # noqa: F401
    import geobox_houdini  # noqa: F401
    d["geobox_from"] = os.path.dirname(os.path.dirname(os.path.abspath(sys.modules["geobox"].__file__)))
    found = {}
    for f in ("toolbar/geobox.shelf", "vex/include/geobox_sky.h"):
        try:
            found[f] = hou.findFile(f)
        except Exception:  # noqa: BLE001
            found[f] = None
    d["houdini_path_files"] = found
    if not all(found.values()):
        return WARN, ("geobox imported, but the Houdini package is not active (shelf / VEX include not on "
                      "HOUDINI_PATH) — run setup_cf20.bat or `python -m geobox houdini-install`, restart Houdini"), d
    return PASS, "Houdini %s, Python %s" % (d["houdini"], d["python"]), d


@check("copy_project")
def c_copy_project(ctx):
    if not os.path.exists(os.path.join(ctx.src_project, "scene.json")):
        return FAIL, "no scene.json in %s" % ctx.src_project, {}
    if os.path.exists(ctx.work):
        shutil.rmtree(ctx.work)
    shutil.copytree(ctx.src_project, ctx.work, ignore=shutil.ignore_patterns("sky", "exports", "*.hip*", "backup"))
    sc = _scene(ctx)
    ctx.state["blocks"] = len(sc.get("greybox", []))
    ctx.state["saved_instances"] = len(sc.get("instances", []))
    return PASS, "work copy %s (%d blocks, %d saved instances)" % (ctx.work, ctx.state["blocks"],
                                                                    ctx.state["saved_instances"]), {}


@check("build_network", needs=("copy_project",))
def c_build(ctx):
    from geobox_houdini import build
    obj = ctx.hou.node("/obj")
    old = obj.node(GEO_NAME)
    if old is not None:
        old.destroy()
    t0 = time.time()
    ctx.geo = build.build_city(ctx.scene_path, parent="/obj", name=GEO_NAME)
    kids = sorted(c.name() for c in ctx.geo.children())
    missing = [n for n in REQUIRED_NODES if n not in kids]
    d = {"nodes": kids, "seconds": round(time.time() - t0, 2)}
    if missing:
        return FAIL, "missing nodes: %s" % missing, d
    return PASS, "%s with %d nodes" % (ctx.geo.path(), len(kids)), d


@check("cook_all", needs=("build_network",))
def c_cook_all(ctx):
    msgs, times, bad = {}, {}, []
    for n in ctx.geo.children():
        t0 = time.time()
        try:
            ctx.cook(n)
        except Exception as exc:  # noqa: BLE001
            msgs.setdefault(n.name(), {})["exception"] = str(exc)
        times[n.name()] = round(time.time() - t0, 3)
        m = _node_msgs(n)
        if m:
            msgs.setdefault(n.name(), {}).update(m)
        if n.name() in REQUIRED_NODES and (msgs.get(n.name(), {}).get("errors") or msgs.get(n.name(), {}).get("exception")):
            bad.append(n.name())
    d = {"messages": msgs, "cook_seconds": times}
    if bad:
        return FAIL, "errors on %s (see details)" % bad, d
    if msgs:
        return WARN, "cooked; %d nodes have warnings/errors outside the main chain" % len(msgs), d
    return PASS, "all %d nodes cooked without errors" % len(times), d


@check("greybox", needs=("build_network",))
def c_greybox(ctx):
    g = ctx.geometry("OUT_GREYBOX")
    ids = set()
    if g.findPrimAttrib("geobox_block_id") is not None:
        ids = {pr.attribValue("geobox_block_id") for pr in g.prims()}
    n = ctx.state.get("blocks", 0)
    d = {"prims": len(g.prims()), "blocks_in_geometry": len(ids), "blocks_in_scene": n}
    if len(ids) != n:
        return FAIL, "OUT_GREYBOX has %d blocks, scene has %d" % (len(ids), n), d
    return PASS, "%d blocks (%d prims)" % (n, len(g.prims())), d


@check("dress", needs=("build_network",))
def c_dress(ctx):
    t0 = time.time()
    g = ctx.geometry("DRESS")
    secs = round(time.time() - t0, 2)
    cats = {}
    if g.findPointAttrib("category") is not None:
        for p in g.points():
            c = p.attribValue("category")
            cats[c] = cats.get(c, 0) + 1
    info = {}
    if g.findGlobalAttrib("geobox_dress") is not None:
        try:
            info = json.loads(g.attribValue("geobox_dress"))
        except Exception:  # noqa: BLE001
            info = {}
    d = {"points": len(g.points()), "categories": cats, "dress_info": info, "seconds": secs,
         "point_attribs": sorted(a.name() for a in g.pointAttribs()) if hasattr(g, "pointAttribs") else None}
    ctx.state["dress_points"] = len(g.points())
    if not g.points():
        return FAIL, "DRESS produced no points", d
    return PASS, "%d instances (%s) in %.2fs; kit pieces: %d" % (
        len(g.points()), ", ".join("%s %d" % kv for kv in sorted(cats.items())), secs, cats.get("kit", 0)), d


@check("copy_to_points", needs=("dress",))
def c_copy(ctx):
    node = ctx.node("COPY_TO_POINTS")
    g = ctx.geometry("COPY_TO_POINTS")
    try:
        names = sorted(p.name() for p in node.parms())
    except Exception:  # noqa: BLE001
        names = []
    interesting = {}
    for p in node.parms() if names else []:
        nm = p.name()
        if any(k in nm.lower() for k in ("piece", "attrib", "pack", "pivot", "transform", "id")):
            try:
                interesting[nm] = p.eval()
            except Exception:  # noqa: BLE001
                interesting[nm] = "?"
    d = {"prims": len(g.prims()), "parms_of_interest": interesting, "all_parms": names,
         "node_type": node.type().name()}
    piece_ok = any("piece" in n.lower() for n in names) or not names
    try:
        bb = g.boundingBox()
        d["bbox_min"], d["bbox_max"] = list(bb.minvec()), list(bb.maxvec())
    except Exception:  # noqa: BLE001
        bb = None
    if not g.prims():
        return FAIL, "COPY_TO_POINTS output is empty (piece attribute parm names: see all_parms)", d
    if not piece_ok:
        return FAIL, "this Houdini's Copy to Points has no 'piece' parm — names listed in all_parms", d
    if bb is not None and d["bbox_min"][1] < -3.0:
        return WARN, ("copies extend %.1f m below ground — pack/copy 'pivot' is probably 'centroid' "
                      "(set PACK_LIBRARY and COPY_TO_POINTS pivot to Origin)" % d["bbox_min"][1]), d
    return PASS, "%d copied prims for %d points" % (len(g.prims()), ctx.state.get("dress_points", 0)), d


@check("reference", needs=("build_network",))
def c_reference(ctx):
    g = ctx.geometry("REFERENCE")
    d = {"prims": len(g.prims())}
    return (PASS, "%d reference prims" % len(g.prims()), d) if g.prims() else (FAIL, "REFERENCE is empty", d)


@check("view_modes", needs=("copy_to_points",))
def c_view(ctx):
    counts = {}
    p = ctx.geo.parm("view_mode")
    for i, label in enumerate(("greybox", "art", "both")):
        p.set(i)
        counts[label] = len(ctx.geometry("OUT").prims())
    p.set(1)
    if not all(counts.values()):
        return FAIL, "empty OUT in some view mode: %s" % counts, counts
    return PASS, "OUT prims %s" % counts, counts


@check("designer_box", needs=("dress",))
def c_designer_box(ctx):
    """A designer draws a plain Box SOP (no ids) and wires it into GREYBOX_DRAW."""
    draw = ctx.node("GREYBOX_DRAW")
    box = ctx.geo.createNode("box", "SELFTEST_DESIGNER_BOX")
    for name, val in (("size", (24.0, 33.0, 14.0)), ("t", (0.0, 16.5, 0.0))):
        p = box.parmTuple(name)
        if p is not None:
            p.set(val)
    try:
        draw.setInput(1, box)
        before = ctx.state.get("dress_points", 0)
        g = ctx.geometry("DRESS")
        blocks = set()
        if g.findPointAttrib("block") is not None:
            blocks = {p.attribValue("block") for p in g.points() if str(p.attribValue("block")).startswith("gb:draw:")}
        d = {"points_before": before, "points_after": len(g.points()), "designer_blocks": sorted(blocks)}
        if not blocks:
            return FAIL, "the drawn box did not produce any dressed instance (block gb:draw:*)", d
        return PASS, "drawn box -> block %s dressed (%d -> %d instances)" % (sorted(blocks)[0], before, len(g.points())), d
    finally:
        try:
            draw.setInput(1, None)
            box.destroy()
            ctx.cook(ctx.node("DRESS"))
        except Exception:  # noqa: BLE001
            pass


@check("save_round_trip", needs=("dress",))
def c_round_trip(ctx):
    """Edit one greybox block (+5 m) and one instance (+3 m X) on frozen copies, Save,
    reload, verify both edits persisted and survive the next DRESS cook."""
    from geobox_houdini import build
    grey = ctx.geometry("OUT_GREYBOX").freeze()
    inst = ctx.geometry("OUT_INSTANCES").freeze()
    if not grey.prims() or not inst.points():
        return FAIL, "nothing to edit", {}
    bid = grey.prims()[0].attribValue("geobox_block_id")
    pts = {}
    for pr in grey.prims():
        if pr.attribValue("geobox_block_id") == bid:
            for v in pr.vertices():
                pts[v.point().number()] = v.point()
    ytop = max(p.position()[1] for p in pts.values())
    for p in pts.values():
        x, y, z = p.position()
        if y >= ytop - 1e-4:
            p.setPosition((x, y + 5.0, z))
    # pick a non-building instance (buildings are regenerated from the edited block)
    target = None
    for p in inst.points():
        if p.attribValue("category") in ("tree", "prop"):
            target = p
            break
    target = target or inst.points()[0]
    iid = target.attribValue("geobox_id")
    x, y, z = target.position()
    target.setPosition((x + 3.0, y, z))
    before = {b["id"]: b for b in _scene(ctx).get("greybox", [])}
    rep = build.save(ctx.geo, grey_geo=grey, inst_geo=inst)
    sc = _scene(ctx)
    blk = {b["id"]: b for b in sc.get("greybox", [])}.get(bid)
    ins = {i["id"]: i for i in sc.get("instances", [])}.get(iid)
    d = {"report": {k: v for k, v in rep.items() if k != "written"}, "written": rep.get("written"),
         "block": bid, "instance": iid}
    problems = []
    if not blk or abs(blk["size"][1] - before[bid]["size"][1] - 5.0) > 0.05 or not blk.get("locked"):
        problems.append("greybox edit not saved: %s" % (blk and blk.get("size")))
    if not ins or abs(ins["p"][0] - round(x + 3.0, 3)) > 0.01 or not ins.get("locked"):
        problems.append("instance edit not saved: %s" % (ins and ins.get("p")))
    build.reload(ctx.geo)
    g = ctx.geometry("DRESS")
    after = None
    for p in g.points():
        if p.attribValue("geobox_id") == iid:
            after = tuple(p.position())
            break
    d["after_reload"] = after
    if not after or abs(after[0] - (x + 3.0)) > 0.01:
        problems.append("locked instance not kept by DRESS after reload: %s" % (after,))
    usd_ok = any(str(w).endswith("scene.usda") for w in rep.get("written", []))
    if not usd_ok:
        problems.append("scene.usda not re-exported: %s" % rep.get("written"))
    if problems:
        return FAIL, "; ".join(problems), d
    return PASS, "greybox +5 m and instance +3 m saved, locked, kept after reload; usda re-exported", d


@check("solaris", needs=("copy_project",), tag="solaris")
def c_solaris(ctx):
    from geobox_houdini import build
    lop = build.build_stage(ctx.scene_path, name="geobox_selftest_stage")
    try:
        lop.cook(force=True)
    except Exception:  # noqa: BLE001
        pass
    d = _node_msgs(lop)
    stage = lop.stage()
    if stage is None:
        return FAIL, "LOP stage is None", d
    types = {}
    for prim in stage.Traverse():
        t = str(prim.GetTypeName())
        types[t] = types.get(t, 0) + 1
    d["prim_types"] = types
    n_pi = types.get("PointInstancer", 0)
    if not n_pi:
        return FAIL, "no PointInstancer in the stage", d
    return PASS, "%d prims, %d PointInstancers" % (sum(types.values()), n_pi), d


@check("sky", needs=("copy_project",), tag="sky")
def c_sky(ctx):
    from geobox_houdini import sky
    params = os.path.join(ctx.work, "sky_params.json")
    if not os.path.exists(params):
        with open(params, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "type": "geobox.sky"}, fh)  # everything else from sky.DEFAULTS
    nodes = sky.build_sky(params, parent="/obj", name="geobox_selftest_sky", cloud_extent=4000.0, voxel_size=200.0)
    d = {"nodes": sorted(k for k in nodes)}
    wr = nodes.get("wrangle")
    if wr is None:
        return WARN, "no cloud wrangle built (clouds disabled?)", d
    try:
        wr.cook(force=True)
    except Exception as exc:  # noqa: BLE001
        d["exception"] = str(exc)
    d.update(_node_msgs(wr))
    if d.get("errors") or d.get("exception"):
        return FAIL, "cloud VEX did not compile/cook — Houdini message in details (verbatim)", d
    out = nodes.get("out")
    if out is not None:
        try:
            out.cook(force=True)
            d["volume_prims"] = len(out.geometry().prims())
        except Exception as exc:  # noqa: BLE001
            d["out_exception"] = str(exc)
    for k in ("env", "sun"):
        if k in nodes:
            d[k + "_messages"] = _node_msgs(nodes[k])
    return PASS, "sky built; cloud volume cooked (%s prims)" % d.get("volume_prims", "?"), d


@check("pdg_ingest", needs=("copy_project",), tag="pdg")
def c_pdg(ctx):
    from geobox_houdini import pdg as gp
    base = os.path.join(ctx.out_dir, "pdg")
    if os.path.exists(base):
        shutil.rmtree(base)
    folder = os.path.join(base, "incoming", "trees")
    os.makedirs(folder)
    lib_dir = os.path.join(base, "lib")
    os.makedirs(lib_dir)
    lib_path = os.path.join(lib_dir, "library.json")
    with open(lib_path, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "type": "geobox.library", "assets": []}, fh)
    for i, h in enumerate((600.0, 900.0)):  # cm, Z-up trees -> unit/axis guessing must kick in
        with open(os.path.join(folder, "selftest_tree_%d.obj" % i), "w", encoding="utf-8") as fh:
            fh.write("v -50 -50 0\nv 50 -50 0\nv 50 50 0\nv -50 50 0\nv 0 0 %g\n"
                     "f 1 2 5\nf 2 3 5\nf 3 4 5\nf 4 1 5\nf 4 3 2 1\n" % h)
    net = gp.build_ingest_network(folder, category="tree", library=lib_path, thumbnails=False,
                                  parent="/obj", name="geobox_selftest_farm", scheduler="local")
    top = net.node("REINDEX")
    d = {"network": net.path(), "library": lib_path}
    t0 = time.time()
    try:
        top.cookWorkItems(block=True)
    except Exception as exc:  # noqa: BLE001
        d["cook_exception"] = str(exc)
    d["seconds"] = round(time.time() - t0, 1)
    states = {}
    for n in net.children():
        try:
            pn = n.getPDGNode()
            if pn is None:
                continue
            for wi in pn.workItems:
                st = str(wi.state)
                states.setdefault(n.name(), {}).setdefault(st, 0)
                states[n.name()][st] += 1
        except Exception:  # noqa: BLE001
            continue
        d.setdefault("node_messages", {})[n.name()] = _node_msgs(n)
    d["work_item_states"] = states
    with open(lib_path, "r", encoding="utf-8") as fh:
        assets = json.load(fh).get("assets", [])
    d["ingested"] = [(a["id"], a.get("source_units"), a.get("source_up"), a.get("bounds")) for a in assets]
    if len(assets) < 2:
        return FAIL, "expected 2 ingested assets, got %d (see work_item_states / cook_exception)" % len(assets), d
    return PASS, "2 assets ingested through TOPs (local scheduler) in %.1fs" % d["seconds"], d


@check("save_hip", needs=("build_network",))
def c_save_hip(ctx):
    if ctx.inprocess and not ctx.hip:
        return SKIP, "in-process run: your .hip is not saved", {}
    path = ctx.hip or os.path.join(ctx.out_dir, "geobox_selftest.hip")
    ctx.hou.hipFile.save(file_name=path.replace("\\", "/"))
    return PASS, path, {}


# --- runner -----------------------------------------------------------------------------------
def run_checks(ctx, only=None):
    status = {}
    for name, fn, needs, tag in CHECKS:
        if only and name not in only:
            continue
        t0 = time.time()
        if tag in ctx.skip or name in ctx.skip:
            res = (SKIP, "skipped (--skip)", {})
        elif any(status.get(n) not in (PASS, WARN) for n in needs):
            res = (SKIP, "needs %s" % ", ".join(needs), {})
        else:
            try:
                res = fn(ctx)
            except Exception as exc:  # noqa: BLE001 - isolate every check
                res = (FAIL, "%s: %s" % (type(exc).__name__, exc), {"traceback": traceback.format_exc()})
        status[name] = res[0]
        r = {"name": name, "status": res[0], "message": res[1], "details": res[2], "seconds": round(time.time() - t0, 2)}
        ctx.results.append(r)
        print("[%s] %-16s %s" % (r["status"], name, r["message"]))
        sys.stdout.flush()
    return ctx.results


def summary_line(results):
    c = {}
    for r in results:
        c[r["status"]] = c.get(r["status"], 0) + 1
    return " ".join("%s %d" % (k, c[k]) for k in (PASS, WARN, FAIL, SKIP) if c.get(k))


def write_report(ctx, report_txt):
    os.makedirs(os.path.dirname(report_txt), exist_ok=True)
    env = next((r["details"] for r in ctx.results if r["name"] == "environment"), {})
    lines = ["GeoBox Houdini self-test — %s" % _dt.datetime.now().isoformat(timespec="seconds"),
             "Houdini %s | Python %s | %s" % (env.get("houdini", "?"), env.get("python", "?"), env.get("platform", "?")),
             "repo %s | project %s" % (ROOT, ctx.src_project),
             "RESULT: %s" % summary_line(ctx.results), ""]
    for r in ctx.results:
        lines.append("[%s] %-16s %s  (%.2fs)" % (r["status"], r["name"], r["message"], r["seconds"]))
    lines += ["", "=" * 78, "DETAILS", "=" * 78]
    for r in ctx.results:
        if not r["details"]:
            continue
        lines += ["", "--- %s [%s]" % (r["name"], r["status"]),
                  json.dumps(r["details"], indent=2, ensure_ascii=False, default=str)]
    if any(r["status"] == FAIL for r in ctx.results):
        lines += ["", "Something failed: please send this file back (it lists the exact Houdini messages / parm names)."]
    with open(report_txt, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.splitext(report_txt)[0] + ".json", "w", encoding="utf-8") as fh:
        json.dump({"summary": summary_line(ctx.results), "project": ctx.src_project, "results": ctx.results},
                  fh, indent=1, ensure_ascii=False, default=str)
    return report_txt


def _report_path(report, out_dir):
    if not report:
        return os.path.join(out_dir, "selftest_report.txt")
    return report if report.lower().endswith(".txt") else os.path.join(report, "selftest_report.txt")


def run(project=None, report=None, hip=None, skip=(), inprocess=False, hou_module=None, cook=None, only=None):
    """Entry point for the shelf / tests. Returns (results, report_txt_path)."""
    _paths()
    hou = hou_module
    if hou is None:
        import hou  # noqa: F811
    project = project or os.path.join(ROOT, "projects", "sample_synthetic")
    out_dir = os.path.join(ROOT, "projects", "_selftest")
    if report:
        out_dir = os.path.dirname(os.path.abspath(_report_path(report, out_dir)))
    if inprocess and "pdg" not in skip:
        skip = tuple(skip) + ("pdg",)  # a blocking TOP cook would freeze the UI; run hython for PDG
    ctx = Ctx(hou, project, out_dir, hip=hip, inprocess=inprocess, skip=skip, cook=cook)
    run_checks(ctx, only=only)
    txt = write_report(ctx, _report_path(report, out_dir))
    print("\nGeoBox self-test: %s\nreport: %s" % (summary_line(ctx.results), txt))
    return ctx.results, txt


def main(argv=None):
    ap = argparse.ArgumentParser(description="GeoBox Houdini self-test (run with hython)")
    ap.add_argument("--project", help="project folder with scene.json (default projects/sample_synthetic)")
    ap.add_argument("--report", help="report .txt path or folder (default projects/_selftest/)")
    ap.add_argument("--hip", help="save the test scene here (default next to the report)")
    ap.add_argument("--skip", default="", help="comma list: pdg,sky,solaris")
    args = ap.parse_args(argv)
    _paths()
    try:
        import hou  # noqa: F401
    except ImportError:
        print("This script needs Houdini's Python: run it with hython, e.g.\n"
              '  "C:\\Program Files\\Side Effects Software\\Houdini 20.5.xxx\\bin\\hython.exe" %s' % __file__)
        return 2
    try:
        hou.hipFile.clear(suppress_save_prompt=True)
    except Exception:  # noqa: BLE001
        pass
    results, _txt = run(args.project, args.report, args.hip, tuple(s for s in args.skip.split(",") if s))
    return 1 if any(r["status"] == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
