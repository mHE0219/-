"""Greybox (blockout) layer tests.  python -m unittest discover -s tests"""
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from geobox import geom, greybox, project  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "sample_osm.json")
KEYS = {"id", "kind", "center", "base_y", "size", "rot_y", "tags", "source", "feature", "locked", "asset_lock"}


class GreyboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="geobox_gb_")
        cls.scene = project.build("g", (25.04, 121.52), (600, 600), osm_file=FIXTURE, root=cls.tmp, log=lambda *a: None)
        cls.layout = project.read_json(os.path.join(cls.tmp, "g", "layout.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _rescatter(self, scene, **kw):
        project.save_scene("g", scene, root=self.tmp, export_usd=False)
        return project.rescatter("g", root=self.tmp, log=lambda *a: None, **kw)

    def test_schema_and_footprint(self):
        blocks = self.scene["greybox"]
        self.assertEqual(len(blocks), len([b for b in self.layout["buildings"] if b["area"] >= 8]))
        by_feature = {b["id"]: b for b in self.layout["buildings"]}
        for blk in blocks[:50]:
            self.assertEqual(set(blk), KEYS)
            self.assertTrue(blk["id"].startswith("gb:"))
            src = by_feature[blk["feature"]]
            w, h, d = blk["size"]
            self.assertAlmostEqual(w * d, src["obb"]["size"][0] * src["obb"]["size"][1], delta=0.05)
            self.assertAlmostEqual(geom.area(greybox.block_corners(blk)), w * d, delta=0.05)
            self.assertAlmostEqual(h, src["height"] - src.get("min_height", 0), delta=0.01)
        # every building instance points back at its block and matches its size
        bi = [i for i in self.scene["instances"] if i["category"] == "building"]
        self.assertEqual(len(bi), len(blocks))
        bb = {b["id"]: b for b in blocks}
        for i in bi[:50]:
            self.assertEqual(i["target"]["size"], [round(v, 2) for v in bb[i["block"]]["size"]])
            self.assertAlmostEqual(i["rot_y"], bb[i["block"]]["rot_y"] % 360, places=2)

    def test_front_faces_road(self):
        from geobox.scatter import Obstacles
        obs = Obstacles(self.layout)
        n = ok = 0
        for blk in self.scene["greybox"][:80]:
            v = obs.nearest_road_dir(*blk["center"])
            if v is None:
                continue
            th = math.radians(blk["rot_y"])
            n += 1
            ok += (math.sin(th) * v[0] + math.cos(th) * v[1]) >= 0
        self.assertGreater(n, 10)
        self.assertEqual(ok, n)

    def test_edits_survive_art_pass(self):
        scene = project.read_json(os.path.join(self.tmp, "g", "scene.json"))
        blocks = {b["id"]: b for b in scene["greybox"]}
        ids = list(blocks)
        edited, deleted = blocks[ids[0]], blocks[ids[1]]
        edited["size"] = [edited["size"][0], 60.0, edited["size"][2]]
        edited["locked"] = True
        scene["greybox"] = [b for b in scene["greybox"] if b["id"] != deleted["id"]]
        scene["suppressed_blocks"] = [deleted["id"]]
        manual = {"id": "gb:manual:t1", "kind": "office", "center": [5.0, 5.0], "base_y": 0.0, "size": [24.0, 40.0, 18.0],
                  "rot_y": 0.0, "tags": ["office"], "source": "manual", "feature": None, "locked": True,
                  "asset_lock": "bld_highrise_a"}
        scene["greybox"].append(manual)
        # the viewer drops the stale art of the edited block before saving
        scene["instances"] = [i for i in scene["instances"] if i.get("block") != edited["id"]]
        out = self._rescatter(scene)
        nb = {b["id"]: b for b in out["greybox"]}
        self.assertNotIn(deleted["id"], nb)
        self.assertEqual(nb[edited["id"]]["size"][1], 60.0)
        self.assertIn("gb:manual:t1", nb)
        bi = {i["block"]: i for i in out["instances"] if i["category"] == "building"}
        self.assertNotIn(deleted["id"], bi)
        self.assertEqual(bi[edited["id"]]["target"]["size"][1], 60.0)
        m = bi["gb:manual:t1"]
        self.assertEqual(m["id"], "bld:gb:manual:t1")
        self.assertEqual(m["asset"], "bld_highrise_a")  # asset_lock honoured
        # vegetation keeps clear of the new manual block
        rect = greybox.block_corners(manual)
        for i in out["instances"]:
            if i["category"] == "tree":
                self.assertFalse(geom.point_in_ring(i["p"][0], i["p"][2], rect), i["id"])
        # a second art pass with another seed keeps all of it
        again = project.rescatter("g", rules_overrides={"seed": 77}, root=self.tmp, log=lambda *a: None)
        nb2 = {b["id"]: b for b in again["greybox"]}
        self.assertEqual(nb2[edited["id"]]["size"][1], 60.0)
        self.assertNotIn(deleted["id"], nb2)

    def test_merge_blocks(self):
        new = [{"id": "gb:a", "source": "osm"}, {"id": "gb:b", "source": "osm"}, {"id": "gb:c", "source": "osm"}]
        prev = {"greybox": [{"id": "gb:a", "source": "osm", "locked": True, "x": 1}, {"id": "gb:m", "source": "manual"},
                            {"id": "gb:b", "source": "osm", "locked": False, "x": 2}],
                "suppressed_blocks": ["gb:c"]}
        out, info = greybox.merge_blocks(new, prev)
        ids = [b["id"] for b in out]
        self.assertEqual(sorted(ids), ["gb:a", "gb:b", "gb:m"])
        self.assertEqual([b for b in out if b["id"] == "gb:a"][0]["x"], 1)  # locked kept verbatim
        self.assertNotIn("x", [b for b in out if b["id"] == "gb:b"][0])     # unlocked refreshed
        self.assertEqual(info, {"kept": 2, "suppressed": 1})
        out, info = greybox.merge_blocks(new, {"instances": []})  # pre-greybox scene
        self.assertEqual(len(out), 3)

    def test_obj_export(self):
        path = os.path.join(self.tmp, "gb.obj")
        greybox.to_obj(self.scene["greybox"][:3], path)
        txt = open(path, encoding="utf-8").read()
        self.assertEqual(txt.count("\no "), 3)
        self.assertEqual(txt.count("\nv "), 24)
        self.assertEqual(txt.count("\nf "), 18)

    def test_usd_greybox(self):
        try:
            from pxr import Usd, UsdGeom
        except ImportError:
            self.skipTest("usd-core not installed")
        project.save_scene("g", project.read_json(os.path.join(self.tmp, "g", "scene.json")), root=self.tmp)
        st = Usd.Stage.Open(os.path.join(self.tmp, "g", "scene.usda"))
        m = UsdGeom.Mesh(st.GetPrimAtPath("/GeoBox/Reference/Greybox"))
        self.assertTrue(m)
        self.assertEqual(UsdGeom.Imageable(m).GetPurposeAttr().Get(), "guide")
        n = len(project.read_json(os.path.join(self.tmp, "g", "scene.json"))["greybox"])
        self.assertEqual(len(m.GetFaceVertexCountsAttr().Get()), 6 * n)


if __name__ == "__main__":
    unittest.main()
