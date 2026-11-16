"""Farm worker: claims jobs from a shared farm root and runs them.

    python -m geobox farm worker --root //nas/geobox_farm --name cf-20 --tags houdini,gpu

Capability tags: a job with requires=["houdini"] is only taken by workers whose
tags include "houdini" (added automatically when hython is found).
Stop: Ctrl+C / SIGTERM (finishes the current job), or `farm stop <name>` which
touches <root>/workers/<name>.stop.
"""
from __future__ import annotations

import os
import platform
import signal
import socket
import threading
import time
import traceback
from typing import Optional, Sequence

from . import jobs
from .queue import Farm


class Worker:
    def __init__(self, farm: Farm, name: Optional[str] = None, tags: Sequence[str] = (),
                 types: Optional[Sequence[str]] = None, poll: float = 2.0, once: bool = False,
                 max_jobs: Optional[int] = None, heartbeat: float = 5.0, allow_code: bool = False, log=print):
        self.farm = farm
        self.name = name or socket.gethostname()
        self.tags = sorted(set(tags) | ({"houdini"} if jobs.hython_bin() else set()))
        self.types = list(types) if types else None
        self.poll, self.once, self.max_jobs, self.hb = poll, once, max_jobs, heartbeat
        self.allow_code = allow_code
        self.stop = threading.Event()
        self.current = None
        self.done = self.failed = 0
        self._lock = threading.Lock()
        self._print = log
        self.started = time.time()

    # --- status ------------------------------------------------------------------
    def info(self, state: str) -> dict:
        return {"name": self.name, "host": socket.gethostname(), "pid": os.getpid(), "os": platform.system(),
                "tags": self.tags, "types": self.types, "state": state, "started": self.started,
                "current_job": self.current["id"] if self.current else None,
                "current_label": self.current.get("label") if self.current else None,
                "done": self.done, "failed": self.failed}

    def _beat(self, job_holder: dict, stop_evt: threading.Event):
        while not stop_evt.wait(self.hb):
            with self._lock:
                j = job_holder.get("job")
                if j is not None and not job_holder.get("finished"):
                    if not self.farm.heartbeat(j):
                        job_holder["lost"] = True
                self.farm.update_worker(self.info("busy" if j else "idle"))

    # --- main loop ------------------------------------------------------------------
    def run(self) -> int:
        self._install_signals()
        self.farm.update_worker(self.info("idle"))
        self._print("[farm] worker %s on %s  tags=%s  types=%s" % (self.name, self.farm.root, self.tags, self.types or "all"))
        idle_since = time.time()
        last_beat = 0.0
        try:
            while not self.stop.is_set():
                if self.farm.stop_requested(self.name):
                    self._print("[farm] stop file found")
                    break
                if self.max_jobs is not None and self.done + self.failed >= self.max_jobs:
                    break
                job = self.farm.claim(self.name, self.tags, self.types)
                if job is None:
                    if self.once:
                        break
                    if time.time() - last_beat > self.hb:
                        self.farm.update_worker(self.info("idle"))
                        last_beat = time.time()
                    self.stop.wait(self.poll)
                    continue
                self.execute(job)
                idle_since = time.time()
        finally:
            self.farm.update_worker(self.info("stopped"))
            try:
                os.remove(self.farm.worker_path(self.name, ".stop"))
            except OSError:
                pass
        self._print("[farm] worker %s stopped (done %d, failed %d)" % (self.name, self.done, self.failed))
        return 0

    def execute(self, job: dict) -> str:
        self.current = job
        holder = {"job": job}
        stop_evt = threading.Event()
        t = threading.Thread(target=self._beat, args=(holder, stop_evt), daemon=True)
        t.start()
        ctx = jobs.JobContext(self.farm, job, worker=self, log_fn=lambda m: self._print("  [%s] %s" % (job["id"], m)))
        self._print("[farm] %s  %s  %s" % (job["id"], job["type"], job.get("label", "")))
        outcome = "failed"
        try:
            result = jobs.run(job["type"], ctx)
            if ctx.children and isinstance(result, dict):
                result.setdefault("children", ctx.children)
            with self._lock:
                holder["finished"] = True
                ok = self.farm.complete(job, result)
            outcome = "done" if ok else "lost"
            self.done += 1 if ok else 0
        except jobs.Cancelled as exc:
            with self._lock:
                holder["finished"] = True
                self.farm.fail(job, "cancelled: %s" % exc, retry=False)
            self.failed += 1
        except Exception as exc:  # noqa: BLE001 - every failure is recorded in the job
            ctx.log(traceback.format_exc())
            with self._lock:
                holder["finished"] = True
                outcome = self.farm.fail(job, "%s: %s" % (type(exc).__name__, exc), retry=True)
            if outcome == "failed":
                self.failed += 1
        finally:
            stop_evt.set()
            t.join(timeout=self.hb + 1)
            self.current = None
            self.farm.update_worker(self.info("idle"))
        self._print("[farm] %s -> %s" % (job["id"], outcome))
        return outcome

    def _install_signals(self):
        def handler(signum, _frame):
            self._print("[farm] signal %s: finishing current job then stopping" % signum)
            self.stop.set()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # not in main thread (e.g. tests)
