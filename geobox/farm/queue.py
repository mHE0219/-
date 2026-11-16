"""File-based job queue on a shared folder (NAS / SMB / NFS / local disk).

Layout of a farm root:

    <root>/jobs/queued/<id>.json     waiting (priority, deps, not_before)
    <root>/jobs/running/<id>.json    claimed by a worker (heartbeat every few s)
    <root>/jobs/done/<id>.json       finished, `result` filled
    <root>/jobs/failed/<id>.json     failed after retries, or cancelled
    <root>/cancel/<id>               cancel request for a running job (cooperative)
    <root>/logs/<id>.log             per-job log
    <root>/workers/<name>.json       worker heartbeat / status
    <root>/workers/<name>.stop       touch this to stop a worker after its current job

A worker claims a job by os.rename(queued/x.json -> running/x.json): the rename
is atomic on one filesystem (including SMB/NFS shares), so two workers can never
own the same job. Every state change is a whole-file atomic replace.
Pure standard library; works on Windows, Linux and macOS.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import socket
import time
import uuid
from typing import Dict, Iterable, List, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATES = ("queued", "running", "done", "failed")
STALE_SECONDS = float(os.environ.get("GEOBOX_FARM_STALE", "120"))
WORKER_ALIVE_SECONDS = 60.0


def default_root() -> str:
    return os.environ.get("GEOBOX_FARM_ROOT") or os.path.join(REPO_ROOT, "farm")


def new_id() -> str:
    # time-sortable, unique across machines
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


# --- robust file helpers (Windows can briefly refuse replace/read while another
# process holds the file; network shares add latency) -------------------------------
def _retry(fn, attempts=8, delay=0.05):
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def read_json(path: str) -> Optional[dict]:
    def rd():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    try:
        return _retry(rd)
    except FileNotFoundError:
        return None
    except ValueError:
        # caught mid-write on a share without atomic replace: try once more
        time.sleep(0.1)
        try:
            return _retry(rd)
        except (FileNotFoundError, ValueError):
            return None


def write_json(path: str, data: dict) -> None:
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:6])
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    _retry(lambda: os.replace(tmp, path))


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 900.0, stale: float = 3600.0, poll: float = 0.2):
    """Cross-process / cross-machine exclusive lock (O_CREAT|O_EXCL lock file).
    A lock older than `stale` seconds is considered abandoned and broken."""
    start = time.time()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("%s pid=%d %s\n" % (socket.gethostname(), os.getpid(), time.ctime()))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.remove(path)
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                raise TimeoutError("could not acquire lock %s" % path)
            time.sleep(poll * (0.5 + random.random()))
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def map_path(value):
    """Translate paths between OS views of the same share.
    GEOBOX_PATH_MAP="//nas/geo=/mnt/geo;Z:/=/mnt/z/"  (applied left-to-right, first match wins)."""
    spec = os.environ.get("GEOBOX_PATH_MAP", "")
    if not spec or not isinstance(value, str):
        return value
    v = value.replace("\\", "/")
    for pair in spec.split(";"):
        if "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        src = src.replace("\\", "/")
        if src and v.lower().startswith(src.lower()):
            return dst + v[len(src):]
    return value


class Farm:
    """Handle on one farm root. Cheap to create; holds no open files."""

    def __init__(self, root: Optional[str] = None):
        self.root = os.path.abspath(root or default_root())
        for st in STATES:
            os.makedirs(os.path.join(self.root, "jobs", st), exist_ok=True)
        for d in ("logs", "workers", "cancel"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        self._last_stale_scan = 0.0

    # --- paths -----------------------------------------------------------------
    def path(self, state: str, job_id: str) -> str:
        return os.path.join(self.root, "jobs", state, job_id + ".json")

    def log_path(self, job_id: str) -> str:
        return os.path.join(self.root, "logs", job_id + ".log")

    def _ids(self, state: str) -> List[str]:
        d = os.path.join(self.root, "jobs", state)
        try:
            return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
        except FileNotFoundError:
            return []

    # --- submit / query -----------------------------------------------------------
    def submit(self, job_type: str, params: Optional[dict] = None, *, priority: int = 50,
               deps: Sequence[str] = (), requires: Sequence[str] = (), max_retries: int = 1,
               label: Optional[str] = None, parent: Optional[str] = None) -> dict:
        jid = new_id()
        job = {
            "id": jid, "type": job_type, "params": params or {}, "label": label or job_type,
            "priority": int(priority), "deps": list(deps), "requires": list(requires),
            "retries": 0, "max_retries": int(max_retries), "requeues": 0, "not_before": 0.0,
            "status": "queued", "created": time.time(), "submitted_from": socket.gethostname(),
            "parent": parent, "worker": None, "claim": None, "started": None, "heartbeat": None,
            "finished": None, "progress": 0.0, "result": None, "error": None,
        }
        write_json(self.path("queued", jid), job)
        self.log(jid, "submitted %s from %s%s" % (job_type, socket.gethostname(),
                                                   (" (deps: %s)" % ", ".join(deps)) if deps else ""))
        return job

    def state_of(self, job_id: str) -> Optional[str]:
        for st in ("done", "failed", "running", "queued"):
            if os.path.exists(self.path(st, job_id)):
                return st
        return None

    def get(self, job_id: str) -> Optional[dict]:
        for st in ("done", "failed", "running", "queued"):
            j = read_json(self.path(st, job_id))
            if j is not None:
                j["state"] = st
                return j
        return None

    def list(self, states: Iterable[str] = STATES, limit: Optional[int] = None) -> List[dict]:
        out = []
        for st in states:
            for jid in self._ids(st):
                j = read_json(self.path(st, jid))
                if j is not None:
                    j["state"] = st
                    out.append(j)
        out.sort(key=lambda j: (j.get("created") or 0, j["id"]), reverse=True)
        return out[:limit] if limit else out

    def counts(self) -> Dict[str, int]:
        return {st: len(self._ids(st)) for st in STATES}

    # --- logging ----------------------------------------------------------------------
    def log(self, job_id: str, msg: str) -> None:
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.log_path(job_id), "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    def read_log(self, job_id: str, tail: int = 200000) -> str:
        try:
            with open(self.log_path(job_id), "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
            return data[-tail:]
        except FileNotFoundError:
            return ""

    # --- claiming ------------------------------------------------------------------------
    def claim(self, worker: str, tags: Sequence[str] = (), types: Optional[Sequence[str]] = None) -> Optional[dict]:
        if time.time() - self._last_stale_scan > 15:
            self.requeue_stale()
        now = time.time()
        tags = set(tags)
        ready = []
        for jid in self._ids("queued"):
            j = read_json(self.path("queued", jid))
            if j is None:
                continue
            if types and j["type"] not in types:
                continue
            if not set(j.get("requires") or []) <= tags:
                continue
            if (j.get("not_before") or 0) > now:
                continue
            blocked = False
            for d in j.get("deps") or []:
                st = self.state_of(d)
                if st == "failed":
                    self._fail_queued(j, "dependency %s failed" % d)
                    blocked = True
                    break
                if st != "done":
                    blocked = True
                    break
            if not blocked:
                ready.append(j)
        ready.sort(key=lambda j: (-int(j.get("priority", 50)), j.get("created") or 0, j["id"]))
        for j in ready:
            src, dst = self.path("queued", j["id"]), self.path("running", j["id"])
            try:
                os.rename(src, dst)  # the atomic claim
            except OSError:
                continue  # someone else got it (or it was cancelled)
            j.update(status="running", worker=worker, claim=uuid.uuid4().hex, started=time.time(),
                     heartbeat=time.time(), progress=0.0)
            write_json(dst, j)
            self.log(j["id"], "claimed by %s" % worker)
            return j
        return None

    def _fail_queued(self, j: dict, error: str) -> None:
        src = self.path("queued", j["id"])
        dst = self.path("failed", j["id"])
        try:
            os.rename(src, dst)
        except OSError:
            return
        j.update(status="failed", error=error, finished=time.time())
        write_json(dst, j)
        self.log(j["id"], "failed: " + error)

    def _owns(self, job: dict) -> bool:
        cur = read_json(self.path("running", job["id"]))
        return cur is not None and cur.get("claim") == job.get("claim")

    def heartbeat(self, job: dict) -> bool:
        """Refresh the running record. False = the claim was lost (requeued/cancelled)."""
        if not self._owns(job):
            return False
        job["heartbeat"] = time.time()
        write_json(self.path("running", job["id"]), job)
        return True

    def complete(self, job: dict, result=None) -> bool:
        if not self._owns(job):
            self.log(job["id"], "result of %s discarded: claim lost" % job.get("worker"))
            return False
        job.update(status="done", result=result, finished=time.time(), progress=1.0, error=None)
        write_json(self.path("done", job["id"]), job)
        self._remove(self.path("running", job["id"]))
        self._remove(os.path.join(self.root, "cancel", job["id"]))
        self.log(job["id"], "done in %.1fs" % (job["finished"] - (job.get("started") or job["finished"])))
        return True

    def fail(self, job: dict, error: str, retry: bool = True) -> str:
        """-> 'queued' (will retry) or 'failed'."""
        if not self._owns(job):
            return "lost"
        if retry and job.get("retries", 0) < job.get("max_retries", 0):
            job["retries"] = job.get("retries", 0) + 1
            delay = float(os.environ.get("GEOBOX_FARM_RETRY_DELAY", "10")) * (2 ** (job["retries"] - 1))
            job.update(status="queued", error=error, worker=None, claim=None, not_before=time.time() + delay)
            write_json(self.path("queued", job["id"]), job)
            self._remove(self.path("running", job["id"]))
            self.log(job["id"], "error (retry %d/%d in %.0fs): %s" % (job["retries"], job["max_retries"], delay, error))
            return "queued"
        job.update(status="failed", error=error, finished=time.time())
        write_json(self.path("failed", job["id"]), job)
        self._remove(self.path("running", job["id"]))
        self._remove(os.path.join(self.root, "cancel", job["id"]))
        self.log(job["id"], "failed: %s" % error)
        return "failed"

    @staticmethod
    def _remove(p: str) -> None:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    # --- maintenance ---------------------------------------------------------------------
    def requeue_stale(self, stale_seconds: Optional[float] = None) -> List[str]:
        """Running jobs whose worker stopped heart-beating go back to the queue."""
        stale_seconds = STALE_SECONDS if stale_seconds is None else stale_seconds
        self._last_stale_scan = time.time()
        moved = []
        for jid in self._ids("running"):
            j = read_json(self.path("running", jid))
            if j is None or time.time() - (j.get("heartbeat") or j.get("started") or 0) < stale_seconds:
                continue
            try:
                os.rename(self.path("running", jid), self.path("queued", jid))
            except OSError:
                continue
            j.update(status="queued", worker=None, claim=None, requeues=j.get("requeues", 0) + 1)
            write_json(self.path("queued", jid), j)
            self.log(jid, "requeued: worker heartbeat lost (> %.0fs)" % stale_seconds)
            moved.append(jid)
        return moved

    def cancel(self, job_id: str) -> Optional[str]:
        st = self.state_of(job_id)
        if st == "queued":
            src, dst = self.path("queued", job_id), self.path("failed", job_id)
            try:
                os.rename(src, dst)
            except OSError:
                return self.cancel(job_id) if self.state_of(job_id) == "running" else None
            j = read_json(dst) or {"id": job_id}
            j.update(status="cancelled", error="cancelled", finished=time.time())
            write_json(dst, j)
            self.log(job_id, "cancelled while queued")
            return "cancelled"
        if st == "running":
            with open(os.path.join(self.root, "cancel", job_id), "w", encoding="utf-8") as fh:
                fh.write(time.ctime())
            self.log(job_id, "cancel requested")
            return "cancel_requested"
        return st

    def cancel_requested(self, job_id: str) -> bool:
        return os.path.exists(os.path.join(self.root, "cancel", job_id))

    def retry(self, job_id: str) -> bool:
        src = self.path("failed", job_id)
        j = read_json(src)
        if j is None:
            return False
        dst = self.path("queued", job_id)
        try:
            os.rename(src, dst)
        except OSError:
            return False
        j.update(status="queued", error=None, retries=0, not_before=0.0, worker=None, claim=None,
                 finished=None, progress=0.0)
        write_json(dst, j)
        self.log(job_id, "manually re-queued")
        return True

    def clean(self, older_than_days: float = 7.0, states: Sequence[str] = ("done",)) -> int:
        cut = time.time() - older_than_days * 86400
        n = 0
        for st in states:
            for jid in self._ids(st):
                j = read_json(self.path(st, jid)) or {}
                if (j.get("finished") or j.get("created") or 0) < cut:
                    self._remove(self.path(st, jid))
                    self._remove(self.log_path(jid))
                    n += 1
        return n

    # --- workers --------------------------------------------------------------------------
    def worker_path(self, name: str, ext: str = ".json") -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return os.path.join(self.root, "workers", safe + ext)

    def update_worker(self, info: dict) -> None:
        info = dict(info, heartbeat=time.time())
        write_json(self.worker_path(info["name"]), info)

    def workers(self) -> List[dict]:
        out = []
        d = os.path.join(self.root, "workers")
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            w = read_json(os.path.join(d, f))
            if not w:
                continue
            age = time.time() - (w.get("heartbeat") or 0)
            w["alive"] = age < WORKER_ALIVE_SECONDS and w.get("state") != "stopped"
            w["age"] = round(age, 1)
            out.append(w)
        return out

    def stop_requested(self, name: str) -> bool:
        return os.path.exists(self.worker_path(name, ".stop"))

    def request_stop(self, name: str) -> None:
        with open(self.worker_path(name, ".stop"), "w", encoding="utf-8") as fh:
            fh.write(time.ctime())
