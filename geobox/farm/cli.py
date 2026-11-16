"""`python -m geobox farm ...` sub-commands (registered through geobox.__main__ CLI_EXTENSIONS)."""
from __future__ import annotations

import json
import os
import sys
import time

from . import jobs
from .queue import Farm, default_root


def add_cli(sub):
    fp = sub.add_parser("farm", help="farm job queue: workers, ingest, tiled builds")
    fp.add_argument("--root", default=None, help="farm root folder (default $GEOBOX_FARM_ROOT or <repo>/farm)")
    fs = fp.add_subparsers(dest="farm_cmd", required=True)

    w = fs.add_parser("worker", help="run a worker on this machine")
    w.add_argument("--name", help="worker name (default: hostname, e.g. cf-20)")
    w.add_argument("--tags", default="", help="capability tags, comma separated (houdini is auto-detected)")
    w.add_argument("--types", default="", help="only run these job types (comma separated)")
    w.add_argument("--poll", type=float, default=2.0)
    w.add_argument("--once", action="store_true", help="exit when the queue has nothing runnable")
    w.add_argument("--max-jobs", type=int)
    w.add_argument("--allow-code", action="store_true", help="allow inline-code hython jobs (trusted farms only)")
    w.set_defaults(func=_worker)

    s = fs.add_parser("submit", help="submit any job type")
    s.add_argument("type", choices=sorted(jobs.JOB_TYPES))
    s.add_argument("--params", default="{}", help="JSON object, or @file.json")
    s.add_argument("--priority", type=int, default=50)
    s.add_argument("--deps", default="", help="job ids, comma separated")
    s.add_argument("--requires", default="", help="worker tags required, comma separated")
    s.add_argument("--retries", type=int, default=1)
    s.set_defaults(func=_submit)

    i = fs.add_parser("ingest", help="ingest a folder of models into the library + retrieval index")
    i.add_argument("folder")
    i.add_argument("--category", choices=["building", "tree", "prop"])
    i.add_argument("--tags", default="")
    i.add_argument("--units", choices=["m", "cm", "mm", "in", "ft"])
    i.add_argument("--up", choices=["+Y", "+Z"])
    i.add_argument("--library", help="library.json (default assets/library.json)")
    i.add_argument("--force", action="store_true", help="re-ingest files already in the library")
    i.set_defaults(func=_ingest)

    t = fs.add_parser("tiles", help="build a whole district as N x M boxes on the farm")
    t.add_argument("--name", required=True)
    t.add_argument("--at", required=True, help='Google Maps URL or "lat, lon" (district centre)')
    t.add_argument("--size", type=float, nargs=2, required=True, metavar=("W", "D"))
    t.add_argument("--tile", type=float, default=500.0, help="tile size in metres")
    t.add_argument("--terrain", action="store_true")
    t.add_argument("--osm-file", help="district-wide Overpass JSON (skips the fetch job)")
    t.add_argument("--projects-root")
    t.add_argument("--seed", type=int)
    t.set_defaults(func=_tiles)

    st = fs.add_parser("status", help="workers and recent jobs")
    st.add_argument("--limit", type=int, default=25)
    st.add_argument("--json", action="store_true")
    st.add_argument("--watch", type=float, help="refresh every N seconds")
    st.set_defaults(func=_status)

    for name, fn, hlp in (("retry", _retry, "re-queue a failed/cancelled job"), ("cancel", _cancel, "cancel a job"),
                          ("log", _log, "print a job log"), ("show", _show, "print a job record")):
        x = fs.add_parser(name, help=hlp)
        x.add_argument("job_id")
        x.set_defaults(func=fn)
    sp = fs.add_parser("stop", help="ask a worker to stop after its current job")
    sp.add_argument("worker")
    sp.set_defaults(func=lambda a: (_farm(a).request_stop(a.worker), print("stop requested for", a.worker)) and 0)
    c = fs.add_parser("clean", help="delete finished jobs older than N days")
    c.add_argument("--days", type=float, default=7.0)
    c.add_argument("--failed", action="store_true", help="also delete failed jobs")
    c.set_defaults(func=lambda a: print("removed", _farm(a).clean(a.days, ("done", "failed") if a.failed else ("done",))) or 0)
    ty = fs.add_parser("types", help="list job types")
    ty.set_defaults(func=lambda a: [print("%-14s %-10s %s" % (d["type"], ",".join(d["requires"]) or "-", d["doc"])) for d in jobs.describe()] and 0)


def _farm(args) -> Farm:
    return Farm(args.root or default_root())


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _worker(args):
    from .worker import Worker
    return Worker(_farm(args), name=args.name, tags=_csv(args.tags), types=_csv(args.types) or None,
                  poll=args.poll, once=args.once, max_jobs=args.max_jobs, allow_code=args.allow_code).run()


def _print_job(j, prefix="submitted"):
    print("%s %s  %s  %s" % (prefix, j["id"], j["type"], j.get("label", "")))


def _submit(args):
    params = args.params
    if params.startswith("@"):
        with open(params[1:], encoding="utf-8") as fh:
            params = fh.read()
    req = _csv(args.requires) or jobs.JOB_TYPES[args.type]["requires"]
    j = _farm(args).submit(args.type, json.loads(params), priority=args.priority, deps=_csv(args.deps),
                           requires=req, max_retries=args.retries)
    _print_job(j)
    return 0


def _ingest(args):
    p = {"folder": os.path.abspath(args.folder)}
    for k in ("category", "units", "up", "library"):
        if getattr(args, k):
            p[k] = os.path.abspath(getattr(args, k)) if k == "library" else getattr(args, k)
    if args.tags:
        p["tags"] = _csv(args.tags)
    if args.force:
        p["force"] = True
    _print_job(_farm(args).submit("ingest_folder", p, label="ingest " + os.path.basename(p["folder"].rstrip("/\\"))))
    return 0


def _tiles(args):
    p = {"name": args.name, "at": args.at, "size": args.size, "tile": args.tile, "terrain": args.terrain}
    if args.osm_file:
        p["osm_file"] = os.path.abspath(args.osm_file)
    if args.projects_root:
        p["projects_root"] = os.path.abspath(args.projects_root)
    if args.seed is not None:
        p["seed"] = args.seed
    plan = jobs.tile_plan(jobs._center(p), args.size, args.tile)  # noqa: SLF001
    print("[farm] %d x %d tiles of %.0f x %.0f m" % (plan["grid"][0], plan["grid"][1], *plan["tile_size"]))
    _print_job(_farm(args).submit("build_tiles", p, label="district " + args.name))
    return 0


def _fmt_age(t):
    if not t:
        return "-"
    d = time.time() - t
    return "%ds" % d if d < 120 else "%dm" % (d // 60) if d < 7200 else "%dh" % (d // 3600)


def _status(args):
    farm = _farm(args)
    while True:
        jl = farm.list(limit=args.limit)
        ws = farm.workers()
        if args.json:
            print(json.dumps({"root": farm.root, "counts": farm.counts(), "workers": ws, "jobs": jl}, indent=1))
            return 0
        if args.watch:
            sys.stdout.write("\x1b[2J\x1b[H")
        c = farm.counts()
        print("farm %s   queued %d  running %d  done %d  failed %d" % (farm.root, c["queued"], c["running"], c["done"], c["failed"]))
        for w in ws:
            print("  worker %-14s %-8s %-7s tags=%s  done=%s failed=%s  %s" % (
                w["name"], "alive" if w["alive"] else "offline", w.get("state"), ",".join(w.get("tags") or []),
                w.get("done"), w.get("failed"), w.get("current_label") or ""))
        for j in jl:
            print("  %-8s %-24s %-13s %4.0f%%  %-10s %5s  %s%s" % (
                j["state"], j["id"], j["type"], 100 * (j.get("progress") or 0), j.get("worker") or "",
                _fmt_age(j.get("created")), j.get("label", ""), ("  ! " + j["error"]) if j.get("error") else ""))
        if not args.watch:
            return 0
        time.sleep(args.watch)


def _retry(args):
    print("re-queued" if _farm(args).retry(args.job_id) else "not in failed/")
    return 0


def _cancel(args):
    print(_farm(args).cancel(args.job_id))
    return 0


def _log(args):
    sys.stdout.write(_farm(args).read_log(args.job_id))
    return 0


def _show(args):
    print(json.dumps(_farm(args).get(args.job_id), indent=1, ensure_ascii=False))
    return 0
