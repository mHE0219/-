"""Farm queue / jobs / worker / PDG tests.   python -m unittest tests.test_farm"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "houdini", "python"))
sys.path.insert(0, HERE)

os.environ["GEOBOX_FARM_RETRY_DELAY"] = "0"
os.environ.pop("GEOBOX_HYTHON", None)

from geobox import library, retrieval  # noqa: E402
from geobox.farm import jobs  # noqa: E402
from geobox.farm.queue import Farm, write_json  # noqa: E402
from geobox.farm.worker import Worker  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "sample_osm.json")


def quiet(*_a, **_k):
    pass


class FarmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_farm_")
        self.farm = Farm(os.path.join(self.tmp, "farm"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def worker(self, **kw):
        kw.setdefault("once", True)
        kw.setdefault("poll", 0.01)
        w = Worker(self.farm, name=kw.pop("name", "test-node"), log=quiet, **kw)
        w.tags = [t for t in w.tags if t != "houdini"] + list(kw.get("tags", []))  # never auto-claim hython here
        return w

    def make_library(self):
        lp = os.path.join(self.tmp, "lib", "library.json")
        library.init_default(lp)
        return lp


class QueueTest(FarmCase):
    def test_claim_priority_and_complete(self):
        lo = self.farm.submit("reindex", {}, priority=10)
        hi = self.farm.submit("reindex", {}, priority=90)
        j = self.farm.claim("w1")
        self.assertEqual(j["id"], hi["id"])
        self.assertEqual(self.farm.state_of(hi["id"]), "running")
        self.assertTrue(self.farm.complete(j, {"ok": 1}))
        self.assertEqual(self.farm.state_of(hi["id"]), "done")
        self.assertEqual(self.farm.get(hi["id"])["result"], {"ok": 1})
        self.assertEqual(self.farm.claim("w1")["id"], lo["id"])
        self.assertIsNone(self.farm.claim("w1"))

    def test_dependencies(self):
        a = self.farm.submit("reindex", {}, priority=10)
        b = self.farm.submit("reindex", {}, priority=99, deps=[a["id"]])
        j = self.farm.claim("w")
        self.assertEqual(j["id"], a["id"], "b must wait for a despite higher priority")
        self.assertIsNone(self.farm.claim("w"))
        self.farm.complete(j)
        self.assertEqual(self.farm.claim("w")["id"], b["id"])
        # failed dependency fails the dependant
        c = self.farm.submit("reindex", {}, max_retries=0)
        d = self.farm.submit("reindex", {}, deps=[c["id"]])
        jc = self.farm.claim("w")
        self.assertEqual(self.farm.fail(jc, "boom"), "failed")
        self.farm.claim("w")
        self.assertEqual(self.farm.state_of(d["id"]), "failed")
        self.assertIn("dependency", self.farm.get(d["id"])["error"])

    def test_retry_then_fail_then_manual_retry(self):
        a = self.farm.submit("reindex", {}, max_retries=1)
        j = self.farm.claim("w")
        self.assertEqual(self.farm.fail(j, "first"), "queued")
        j = self.farm.claim("w")
        self.assertEqual(j["retries"], 1)
        self.assertEqual(self.farm.fail(j, "second"), "failed")
        self.assertTrue(self.farm.retry(a["id"]))
        self.assertEqual(self.farm.state_of(a["id"]), "queued")

    def test_requires_tags(self):
        self.farm.submit("hython", {"preset": "prep_library"}, requires=["houdini"])
        self.assertIsNone(self.farm.claim("plain", tags=[]))
        self.assertIsNotNone(self.farm.claim("hou", tags=["houdini"]))

    def test_cancel_and_stale_requeue_and_lost_claim(self):
        q = self.farm.submit("reindex", {})
        self.assertEqual(self.farm.cancel(q["id"]), "cancelled")
        self.assertEqual(self.farm.get(q["id"])["status"], "cancelled")
        r = self.farm.submit("reindex", {})
        j = self.farm.claim("w")
        self.assertEqual(self.farm.cancel(r["id"]), "cancel_requested")
        ctx = jobs.JobContext(self.farm, j)
        self.assertRaises(jobs.Cancelled, ctx.check_cancel)
        # simulate a dead worker: heartbeat far in the past
        j2 = dict(j, heartbeat=time.time() - 3600)
        write_json(self.farm.path("running", j["id"]), j2)
        self.assertEqual(self.farm.requeue_stale(stale_seconds=60), [r["id"]])
        self.assertEqual(self.farm.state_of(r["id"]), "queued")
        self.assertFalse(self.farm.complete(j, {}), "old claim must be rejected")
        self.assertIn("requeued", self.farm.read_log(r["id"]))

    def test_worker_records_failure_with_traceback(self):
        j = self.farm.submit("build_box", {"name": "x"}, max_retries=0)  # missing centre -> error
        w = self.worker()
        w.run()
        rec = self.farm.get(j["id"])
        self.assertEqual(rec["state"], "failed")
        self.assertIn("Traceback", self.farm.read_log(j["id"]))
        self.assertEqual(self.farm.workers()[0]["state"], "stopped")


class IngestTest(FarmCase):
    def test_ingest_folder_feeds_retrieval(self):
        lp = self.make_library()
        models = os.path.join(self.tmp, "incoming")
        os.makedirs(os.path.join(models, "trees"))
        os.makedirs(os.path.join(models, "buildings"))
        with open(os.path.join(models, "trees", "camphor_a.obj"), "w", encoding="utf-8") as fh:
            fh.write("v -200 -200 0\nv 200 200 1100\nv 0 200 0\nf 1 2 3\n")  # cm, Z-up, 11 m
        with open(os.path.join(models, "buildings", "shophouse_a.obj"), "w", encoding="utf-8") as fh:
            fh.write("v -6 0 -4\nv 6 14 4\nv 6 0 -4\nf 1 2 3\n")
        with open(os.path.join(models, "buildings", "tower.fbx"), "wb") as fh:
            fh.write(b"Kaydara FBX Binary  \x00")
        top = self.farm.submit("ingest_folder", {"folder": models, "library": lp})
        self.worker().run()
        res = self.farm.get(top["id"])["result"]
        self.assertEqual(res["files"], 3)
        self.assertEqual(self.farm.state_of(res["reindex"]), "done")
        self.assertEqual(self.farm.state_of(res["houdini_prep"]), "queued", "waits for a Houdini node")
        lib = library.load(lp)
        a = library.by_id(lib)
        self.assertEqual((a["camphor_a"]["category"], a["camphor_a"]["source_units"], a["camphor_a"]["source_up"]),
                         ("tree", "cm", "+Z"))
        self.assertAlmostEqual(library.asset_size(a["camphor_a"])[1], 11.0, places=3)
        self.assertEqual(a["shophouse_a"]["category"], "building")
        idx = retrieval.AssetIndex.load(lib)
        self.assertEqual(idx.search(category="tree", text="camphor", limit=1)[0]["id"], "camphor_a")
        self.assertEqual(idx.search(category="building", size=[12, 14, 8], limit=1)[0]["id"], "shophouse_a")
        with open(retrieval.index_path(lib), encoding="utf-8") as fh:
            self.assertIn("camphor_a", json.load(fh)["assets"])
        # second ingest of the same folder finds nothing new
        again = self.farm.submit("ingest_folder", {"folder": models, "library": lp})
        self.worker().run()
        self.assertEqual(self.farm.get(again["id"])["result"]["files"], 0)


class TilesTest(FarmCase):
    def test_build_tiles(self):
        proot = os.path.join(self.tmp, "projects")
        top = self.farm.submit("build_tiles", {"name": "dist", "center": [25.04, 121.52], "size": [600, 600],
                                               "tile": 300, "osm_file": FIXTURE, "projects_root": proot})
        self.worker().run()
        res = self.farm.get(top["id"])["result"]
        self.assertEqual(res["grid"], [2, 2])
        with open(res["manifest"], encoding="utf-8") as fh:
            plan = json.load(fh)
        self.assertEqual([t["offset"] for t in plan["tiles"]],
                         [[-150.0, -150.0], [150.0, -150.0], [-150.0, 150.0], [150.0, 150.0]])
        total = 0
        for t in plan["tiles"]:
            self.assertEqual(self.farm.state_of(t["job"]), "done")
            with open(os.path.join(proot, t["name"], "scene.json"), encoding="utf-8") as fh:
                total += len(json.load(fh)["instances"])
        self.assertGreater(total, 800)

    def test_tile_plan_true_metres(self):
        from geobox.geo import LocalFrame
        plan = jobs.tile_plan((25.0339, 121.5645), (1000, 600), 500)
        self.assertEqual(plan["grid"], [2, 2])
        f = LocalFrame(25.0339, 121.5645)
        for t in plan["tiles"]:
            x, z = f.to_local(*t["center"])
            self.assertAlmostEqual(x, t["offset"][0], delta=0.01)
            self.assertAlmostEqual(z, t["offset"][1], delta=0.01)


class ParallelWorkersTest(FarmCase):
    def test_two_processes_never_double_claim(self):
        lp = self.make_library()
        ids = [self.farm.submit("reindex", {"library": lp})["id"] for _ in range(16)]
        env = dict(os.environ, PYTHONPATH=ROOT)
        procs = [subprocess.Popen([sys.executable, "-m", "geobox", "farm", "--root", self.farm.root, "worker",
                                   "--name", n, "--once", "--poll", "0.05"], cwd=ROOT, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for n in ("cf-20", "cf-21")]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode()[-2000:])
        workers = {}
        for jid in ids:
            rec = self.farm.get(jid)
            self.assertEqual(rec["state"], "done")
            self.assertEqual(self.farm.read_log(jid).count("claimed by"), 1, "claimed twice: " + jid)
            workers[rec["worker"]] = workers.get(rec["worker"], 0) + 1
        self.assertEqual(sum(workers.values()), 16)
        self.assertEqual(self.farm.counts(), {"queued": 0, "running": 0, "done": 16, "failed": 0})


class ApiTest(FarmCase):
    def test_routes(self):
        from geobox.farm import api

        class Req:
            def __init__(self, body=None, query=None, match=None):
                self._b, self.query, self.match = body, query or {}, match

            def json(self):
                return self._b

        os.environ["GEOBOX_FARM_ROOT"] = self.farm.root
        try:
            r = api._submit(Req({"type": "reindex", "params": {}}))
            self.assertTrue(r["ok"])
            code, body = api._submit(Req({"type": "hython", "params": {"code": "print(1)"}}))
            self.assertEqual(code, 403)
            listed = api._jobs(Req(query={}))
            self.assertEqual(listed["counts"]["queued"], 1)
            self.assertNotIn("claim", listed["jobs"][0])
            import re
            m = re.match(r"^/api/farm/jobs/([A-Za-z0-9_\-]+)/(retry|cancel)$", "/api/farm/jobs/%s/cancel" % r["id"])
            self.assertEqual(api._action(Req(match=m))["state"], "cancelled")
        finally:
            os.environ.pop("GEOBOX_FARM_ROOT", None)


class PdgMockTest(FarmCase):
    def test_networks_and_ingest_item(self):
        import mock_hou
        mock_hou.install()
        from geobox_houdini import pdg
        net = pdg.build_ingest_network(os.path.join(self.tmp, "incoming"), category="tree", scheduler="deadline")
        kids = {c.name(): c for c in net.children()}
        self.assertTrue({"ASSET_FILES", "INGEST_ASSET", "ALL_INGESTED", "REINDEX", "deadline_scheduler"} <= set(kids))
        self.assertIn("pdg_ingest_item", kids["INGEST_ASSET"].parm("script").value)
        self.assertEqual(net.parm("topscheduler").value, kids["deadline_scheduler"].path())
        tiles = pdg.build_tiles_network(center=(25.04, 121.52), size=(600, 600), tile=300, name="pd",
                                        projects_root=os.path.join(self.tmp, "projects"), osm_file=FIXTURE)
        tk = {c.name(): c for c in tiles.children()}
        self.assertEqual(tk["TILES"].parm("itemcount").value, 4)
        manifest = os.path.join(self.tmp, "projects", "_district", "pd", "tiles.json")
        r = pdg.pdg_tile_item(manifest, 3, log=quiet)
        self.assertEqual(r["name"], "pd_r01c01")
        self.assertEqual(pdg.pdg_tiles_report(manifest, log=quiet), ["pd_r01c01"])

        class WI:  # PDG work item stand-in (H18+ API)
            class F:
                path = os.path.join(self.tmp, "t.obj")
            inputFiles = [F]

        with open(WI.F.path, "w", encoding="utf-8") as fh:
            fh.write("v -1 0 -1\nv 1 6 1\nv 1 0 -1\nf 1 2 3\n")
        lp = self.make_library()
        res = pdg.pdg_ingest_item(pdg.item_input_path(WI), category="tree", library=lp, log=quiet)
        self.assertTrue(os.path.exists(res["houdini_normalized"]))
        a = library.by_id(library.load(lp))[res["id"]]
        self.assertTrue(a["files"]["houdini_normalized"].endswith(".bgeo.sc"))


if __name__ == "__main__":
    unittest.main()
