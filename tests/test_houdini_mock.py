"""Exercise the Houdini bridge against tests/mock_hou.py (no Houdini needed).

Run:  python -m unittest discover -s tests
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "houdini", "python"))
sys.path.insert(0, HERE)

import mock_hou  # noqa: E402

hou = mock_hou.install()

from geobox import project  # noqa: E402
from geobox_houdini import build, core, sop  # noqa: E402


class HoudiniBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="geobox_hou_")
        project.build("fx", (25.04, 121.52), (260, 260), osm_file=os.path.join(HERE, "fixtures", "sample_osm.json"),
                      root=cls.tmp, log=lambda *a: None)
        cls.scene_path = os.path.join(cls.tmp, "fx", "scene.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _sop(self, fn):
        geo = hou.Node("/obj/geobox_city", "geo")
        geo.parm("scene_file").set(self.scene_path)
        n = hou.Node("/obj/geobox_city/X", "python", geo)
        hou.set_pwd(n)
        getattr(sop, fn)()
        return n.geometry()

    def test_instances(self):
        g = self._sop("instances")
        scene = json.load(open(self.scene_path, encoding="utf-8"))
        self.assertEqual(len(g.points()), len(scene["instances"]))
        p0 = g.points()[0]
        self.assertEqual(p0.attribValue("geobox_id"), scene["instances"][0]["id"])
        q = p0.attribValue("orient")
        self.assertAlmostEqual(core.yaw_from_quat(q), scene["instances"][0]["rot_y"] % 360, places=2)
        self.assertIn("building", g.groups)
        self.assertTrue(p0.attribValue("instancefile").endswith(".obj"))

    def test_library_and_layers(self):
        g = self._sop("library")
        names = {p.attrs.get("name") for p in g.prims()}
        self.assertTrue(any(n and n.startswith("bld_") for n in names))
        for fn in ("footprints", "massing", "roads", "areas", "ground"):
            g = self._sop(fn)
            self.assertGreater(len(g.prims()), 0, fn)

    def test_ground_winding_clockwise_from_above(self):
        g = self._sop("ground")
        quad = g.prims()[0].verts
        pts = [(v.pos[0], -v.pos[2]) for v in quad]  # (east, north)
        area = sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4)) / 2
        self.assertLess(area, 0, "Houdini front faces are clockwise -> negative CCW area")

    def test_greybox_round_trip(self):
        g = self._sop("greybox")
        data = core.SceneData(self.scene_path)
        blocks = data.scene["greybox"]
        self.assertEqual(len(g.prims()), 6 * len(blocks))
        pieces = build._pieces_from_geo(g)
        self.assertEqual(len(pieces), len(blocks))
        by = {p["id"]: p for p in pieces}
        b0, b1 = blocks[0], blocks[1]
        # move block 0 by +4 m east and make it 5 m taller; delete block 1; add a new id-less box
        top = max(p[1] for p in by[b0["id"]]["points"])
        by[b0["id"]]["points"] = [(x + 4, y + (5 if y >= top - 1e-6 else 0), z) for x, y, z in by[b0["id"]]["points"]]
        del by[b1["id"]]
        newbox = core.block_box_vertices({"center": [0, 0], "size": [10, 20, 6], "rot_y": 30, "base_y": 0})
        pcs = list(by.values()) + [{"id": "", "points": newbox, "attrs": {}}]
        scene, rep = core.pieces_to_greybox(pcs, data.scene)
        self.assertEqual((rep["updated"], rep["deleted"], rep["added"]), (1, 1, 1))
        nb = {b["id"]: b for b in scene["greybox"]}
        self.assertAlmostEqual(nb[b0["id"]]["center"][0], b0["center"][0] + 4, places=2)
        self.assertAlmostEqual(nb[b0["id"]]["size"][1], b0["size"][1] + 5, places=2)
        self.assertAlmostEqual(nb[b0["id"]]["size"][0], b0["size"][0], places=2)  # W/D not swapped
        self.assertTrue(nb[b0["id"]]["locked"])
        self.assertIn(b1["id"], scene["suppressed_blocks"])
        man = [b for b in scene["greybox"] if b["source"] == "manual"][0]
        self.assertAlmostEqual(man["size"][1], 20, places=2)
        self.assertEqual(sorted(round(v, 2) for v in (man["size"][0], man["size"][2])), [6, 10])
        # stale non-locked art of edited/deleted blocks is dropped
        self.assertFalse([i for i in scene["instances"] if i.get("block") in (b0["id"], b1["id"])])

    def test_round_trip_edits(self):
        g = self._sop("instances")
        data = core.SceneData(self.scene_path)
        recs = build._records_from_geo(g)
        n0 = len(recs)
        moved = recs[0]
        moved["P"] = (moved["P"][0] + 5.0, moved["P"][1], moved["P"][2])
        deleted = recs.pop(1)
        dup = copy.deepcopy(recs[2])  # duplicate keeps id -> becomes new manual instance
        recs.append(dup)
        scene, rep = core.records_to_scene(recs, data.scene, data.assets)
        self.assertEqual(rep["updated"], 1)
        self.assertEqual(rep["deleted"], 1)
        self.assertEqual(rep["added"], 1)
        self.assertEqual(len(scene["instances"]), n0)
        self.assertIn(deleted["geobox_id"], scene["suppressed"])
        first = [i for i in scene["instances"] if i["id"] == moved["geobox_id"]][0]
        self.assertTrue(first["locked"])
        # rescatter keeps the edit and the deletion
        written = core.save_scene(data, scene)
        self.assertTrue(any(w.endswith("scene.usda") for w in written), written)
        again = project.rescatter("fx", root=self.tmp, log=lambda *a: None)
        ids = {i["id"]: i for i in again["instances"]}
        self.assertNotIn(deleted["geobox_id"], ids)
        self.assertAlmostEqual(ids[moved["geobox_id"]]["p"][0], round(moved["P"][0], 3), places=2)

    # --- v2 network ---------------------------------------------------------------------
    def _v2(self, name="geobox"):
        geo = build.build_city(self.scene_path, parent="/obj", name=name)
        return geo

    def test_build_v2_network_cooks(self):
        geo = self._v2()
        kids = {c.name() for c in geo.children()}
        for n in ("GROUND", "AREAS", "ROADS", "MASSING", "REFERENCE", "SHOW_REFERENCE", "GREYBOX_SEED", "GREYBOX_DRAW",
                  "GREYBOX_EDIT", "OUT_GREYBOX", "DRESS", "EDIT_INSTANCES", "OUT_INSTANCES", "LIBRARY", "PACK_LIBRARY",
                  "PIECE_ATTRIB", "COPY_TO_POINTS", "VIEW_MODE", "OUT"):
            self.assertIn(n, kids)
        self.assertIn("g.dress()", geo.node("DRESS").parm("python").value)
        self.assertIs(geo.node("DRESS").inputs()[0], geo.node("OUT_GREYBOX"))
        self.assertIs(geo.node("LIBRARY").inputs()[0], geo.node("OUT_INSTANCES"))
        self.assertEqual(geo.node("VIEW_MODE").parm("input").expr, 'ch("../view_mode")')
        for p in ("seed_greybox", "clear_greybox", "save", "export_usd", "web_preview", "selftest", "seed"):
            self.assertIsNotNone(geo.parm(p))
        geo.node("OUT").cook(force=True)  # mock evaluates the whole network
        dressed = geo.node("DRESS").geometry()
        n_blocks = len(core.SceneData(self.scene_path).scene["greybox"])
        self.assertGreater(len(dressed.points()), n_blocks)
        info = json.loads(dressed.attribValue("geobox_dress"))
        self.assertEqual(info["blocks"], n_blocks)
        self.assertGreater(len(geo.node("COPY_TO_POINTS").geometry().prims()), 0)
        lib_names = {pr.attrs.get("variant") for pr in geo.node("LIBRARY").geometry().prims()}
        used = {pt.attrs.get("name") for pt in dressed.points()}
        self.assertTrue(used <= lib_names, used - lib_names)  # every dressed asset is loaded
        for mode in (0, 1, 2):
            geo.parm("view_mode").set(mode)
            geo.node("OUT").cook()
            self.assertGreater(len(geo.node("OUT").geometry().prims()), 0)
        # second cook hits the cache
        geo.node("DRESS").cook()
        self.assertTrue(json.loads(geo.node("DRESS").geometry().attribValue("geobox_dress"))["cached"])

    def test_dress_designer_boxes(self):
        """Two hand-made boxes without any ids -> two manual blocks -> dressed buildings."""
        g = hou.Geometry()
        v = mock_hou._Verb("box")
        for size, t in (((20, 30, 12), (40, 15, 40)), ((10, 9, 8), (-60, 4.5, 20))):
            v.parms = {"size": size, "t": t}
            v.execute(g, [])
        net = hou.Node("/obj/dt", "geo")
        net.parm("scene_file").set(self.scene_path)
        src = net.createNode("null", "SRC")
        src._geo = g
        src.cook = lambda force=False: None  # keep the hand-made geometry
        dress = net.createNode("python", "DRESS")
        dress.parm("python").set(build.PY_CODE.format(fn="dress"))
        dress.setInput(0, src)
        dress.cook()
        out = dress.geometry()
        blocks = {pt.attrs.get("block") for pt in out.points() if str(pt.attrs.get("block", "")).startswith("gb:draw:")}
        self.assertEqual(len(blocks), 2, blocks)
        cats = {pt.attrs.get("category") for pt in out.points() if pt.attrs.get("block") in blocks}
        self.assertTrue(cats & {"building", "kit"}, cats)
        first = sorted(blocks)
        sop._DRESS_CACHE.clear()
        dress.cook()
        again = {pt.attrs.get("block") for pt in dress.geometry().points() if str(pt.attrs.get("block", "")).startswith("gb:draw:")}
        self.assertEqual(sorted(again), first, "designer block ids must be stable across cooks")
        pieces = sop.pieces_from_geo(g)
        self.assertEqual(len(pieces), 2)
        b = core.block_from_points(pieces[0]["points"], {}, None)
        self.assertEqual(sorted(round(x, 2) for x in (b["size"][0], b["size"][2])), [12, 20])
        self.assertAlmostEqual(b["size"][1], 30, places=2)

    def test_save_v2_round_trip(self):
        tmp = tempfile.mkdtemp(prefix="geobox_v2_")
        try:
            shutil.copytree(os.path.dirname(self.scene_path), os.path.join(tmp, "fx"))
            sp = os.path.join(tmp, "fx", "scene.json")
            geo = build.build_city(sp, parent="/obj", name="geobox_rt")
            geo.node("OUT_INSTANCES").cook()
            geo.node("OUT_GREYBOX").cook()
            grey = geo.node("OUT_GREYBOX").geometry().freeze()
            inst = geo.node("OUT_INSTANCES").geometry().freeze()
            bid = grey.prims()[0].attribValue("geobox_block_id")
            pts = {v.point().number(): v.point() for pr in grey.prims() if pr.attribValue("geobox_block_id") == bid
                   for v in pr.vertices()}
            top = max(p.position()[1] for p in pts.values())
            for p in pts.values():
                x, y, z = p.position()
                if y >= top - 1e-6:
                    p.setPosition((x, y + 5, z))
            tree = next(p for p in inst.points() if p.attribValue("category") == "tree")
            iid = tree.attribValue("geobox_id")
            x0 = tree.position()[0]
            tree.setPosition((x0 + 3, tree.position()[1], tree.position()[2]))
            gone = next(p for p in inst.points() if p.attribValue("category") == "prop")
            gone_id = gone.attribValue("geobox_id")
            inst._pts.remove(gone)  # "blast" one lamp
            before = {b["id"]: b for b in core.SceneData(sp).scene["greybox"]}
            manual_before = {i["id"] for i in core.SceneData(sp).scene["instances"] if i.get("source") == "manual"}
            rep = build.save(geo, grey_geo=grey, inst_geo=inst)
            self.assertEqual(rep["greybox"]["updated"], 1)
            self.assertEqual((rep["instances"]["updated"], rep["instances"]["deleted"], rep["instances"]["added"]), (1, 1, 0))
            sc = core.SceneData(sp).scene
            blk = {b["id"]: b for b in sc["greybox"]}[bid]
            self.assertAlmostEqual(blk["size"][1], before[bid]["size"][1] + 5, places=2)
            ins = {i["id"]: i for i in sc["instances"]}
            self.assertTrue(ins[iid]["locked"])
            self.assertIn(gone_id, sc["suppressed"])
            self.assertNotIn(gone_id, ins)
            # procedural instances are saved as procedural (not as manual copies)
            self.assertEqual({i["id"] for i in sc["instances"] if i.get("source") == "manual"}, manual_before)
            build.reload(geo)
            geo.node("DRESS").cook()
            after = {pt.attrs.get("geobox_id"): pt.pos for pt in geo.node("DRESS").geometry().points()}
            self.assertAlmostEqual(after[iid][0], round(x0 + 3, 3), places=2)
            self.assertNotIn(gone_id, after)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_seed_and_clear(self):
        tmp = tempfile.mkdtemp(prefix="geobox_sc_")
        try:
            shutil.copytree(os.path.dirname(self.scene_path), os.path.join(tmp, "fx"))
            sp = os.path.join(tmp, "fx", "scene.json")
            n0 = len(core.SceneData(sp).scene["greybox"])
            geo = build.build_city(sp, parent="/obj", name="geobox_sc")
            info = build.clear_greybox(geo)
            sc = core.SceneData(sp).scene
            self.assertEqual(sc["greybox"], [])
            self.assertEqual(len(sc["suppressed_blocks"]), n0)
            # a web/CLI rescatter must not bring the OSM blocks back
            again = project.rescatter("fx", root=tmp, log=lambda *a: None)
            self.assertEqual(again["greybox"], [])
            geo.node("DRESS").cook()
            # only locked / manual art (edited by the user) survives a Clear; procedural building/kit art is gone
            left = [p for p in geo.node("DRESS").geometry().points() if p.attrs.get("category") in ("building", "kit")]
            self.assertFalse([p for p in left if p.attrs.get("source") != "manual" and not p.attrs.get("locked")])
            info = build.seed_greybox(geo)
            self.assertEqual(info["blocks"], n0)
            self.assertEqual(len(core.SceneData(sp).scene["greybox"]), n0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_v1_export_and_stage(self):
        geo = self._v2("geobox_v1compat")
        geo.node("OUT_INSTANCES").cook()
        geo.node("OUT_GREYBOX").cook()
        stage = build.build_stage(self.scene_path)
        self.assertTrue(stage.parm("filepath1").value.endswith("scene.usda"))
        self.assertIs(build.export_edits, build.save)

    def test_selftest_checks_on_mock(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("geobox_selftest", os.path.join(ROOT, "houdini", "scripts", "geobox_selftest.py"))
        st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(st)
        tmp = tempfile.mkdtemp(prefix="geobox_st_")
        try:
            results, txt = st.run(project=os.path.dirname(self.scene_path), report=os.path.join(tmp, "rep"),
                                  skip=("solaris", "sky", "pdg"), hou_module=hou)
            status = {r["name"]: r["status"] for r in results}
            fails = {r["name"]: r["message"] for r in results if r["status"] == "FAIL"}
            self.assertFalse(fails, fails)
            for name in ("copy_project", "build_network", "cook_all", "greybox", "dress", "copy_to_points", "reference",
                         "view_modes", "designer_box", "save_round_trip", "save_hip"):
                self.assertIn(status[name], ("PASS", "WARN"), (name, status[name]))
            for name in ("solaris", "sky", "pdg_ingest"):
                self.assertEqual(status[name], "SKIP")
            self.assertTrue(os.path.exists(txt))
            self.assertTrue(os.path.exists(os.path.splitext(txt)[0] + ".json"))
            with open(txt, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("RESULT:", body)
            # the source project is untouched
            self.assertEqual(core.SceneData(self.scene_path).scene["greybox"][0]["size"],
                             json.load(open(self.scene_path, encoding="utf-8"))["greybox"][0]["size"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_package_puts_repo_and_bridge_on_pythonpath(self):
        from geobox.houdini_install import package_json
        pj = package_json("C:\\geobox")
        vals = [e["PYTHONPATH"]["value"] for e in pj["env"] if "PYTHONPATH" in e]
        self.assertIn("$GEOBOX", vals)
        self.assertIn("$GEOBOX/houdini/python", vals)
        self.assertEqual(pj["env"][0]["GEOBOX"], "C:/geobox")


if __name__ == "__main__":
    unittest.main()
