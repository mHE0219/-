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
        project.build("fx", (25.04, 121.52), (400, 300), osm_file=os.path.join(HERE, "fixtures", "sample_osm.json"),
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
        scene = json.load(open(self.scene_path))
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

    def test_build_network_and_export(self):
        geo = build.build_city(self.scene_path, parent="/obj", name="geobox_city")
        kids = {c.name() for c in geo.children()}
        for n in ("INSTANCES", "LIBRARY", "PACK_LIBRARY", "COPY_TO_POINTS", "OUT", "OUT_INSTANCES", "GROUND"):
            self.assertIn(n, kids)
        self.assertIn("g.instances()", geo.node("INSTANCES").parm("python").value)
        hou.set_pwd(geo.node("INSTANCES"))
        sop.instances()
        geo.node("OUT_INSTANCES")._geo = geo.node("INSTANCES").geometry()
        rep = build.export_edits(geo)
        self.assertEqual(rep["updated"] + rep["added"] + rep["deleted"], 0)
        stage = build.build_stage(self.scene_path)
        self.assertTrue(stage.parm("filepath1").value.endswith("scene.usda"))


if __name__ == "__main__":
    unittest.main()
