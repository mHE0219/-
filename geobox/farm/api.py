"""Web API for the farm (registered through geobox.server EXTENSIONS)."""
from __future__ import annotations

from . import jobs
from .queue import Farm, default_root

_farm = None


def farm() -> Farm:
    global _farm
    if _farm is None or _farm.root != Farm(default_root()).root:
        _farm = Farm(default_root())
    return _farm


def _jobs(req):
    q = req.query
    states = q.get("state", ["queued,running,done,failed"])[0].split(",")
    limit = int(q.get("limit", ["60"])[0])
    f = farm()
    jl = f.list(states=[s for s in states if s], limit=limit)
    for j in jl:
        j.pop("claim", None)
    return {"root": f.root, "counts": f.counts(), "jobs": jl}


def _workers(req):
    return {"workers": farm().workers()}


def _types(req):
    return {"types": jobs.describe()}


def _submit(req):
    body = req.json()
    t = body.get("type")
    if t not in jobs.JOB_TYPES:
        return 400, {"ok": False, "error": "unknown job type %r" % t}
    if t == "hython" and body.get("params", {}).get("code"):
        return 403, {"ok": False, "error": "inline code jobs cannot be submitted over HTTP"}
    j = farm().submit(t, body.get("params") or {}, priority=int(body.get("priority", 50)),
                      deps=body.get("deps") or [], requires=body.get("requires") or jobs.JOB_TYPES[t]["requires"],
                      max_retries=int(body.get("max_retries", 1)), label=body.get("label"))
    return {"ok": True, "id": j["id"], "job": j}


def _action(req):
    jid, action = req.match.group(1), req.match.group(2)
    f = farm()
    if action == "retry":
        return {"ok": f.retry(jid)}
    return {"ok": True, "state": f.cancel(jid)}


def _log(req):
    return 200, farm().read_log(req.match.group(1)).encode("utf-8"), "text/plain; charset=utf-8"


def _job(req):
    j = farm().get(req.match.group(1))
    return (200, j) if j else (404, {"ok": False, "error": "no such job"})


ROUTES = [
    ("GET", r"^/api/farm/jobs$", _jobs),
    ("GET", r"^/api/farm/workers$", _workers),
    ("GET", r"^/api/farm/types$", _types),
    ("POST", r"^/api/farm/submit$", _submit),
    ("POST", r"^/api/farm/jobs/([A-Za-z0-9_\-]+)/(retry|cancel)$", _action),
    ("GET", r"^/api/farm/jobs/([A-Za-z0-9_\-]+)/log$", _log),
    ("GET", r"^/api/farm/jobs/([A-Za-z0-9_\-]+)$", _job),
]
