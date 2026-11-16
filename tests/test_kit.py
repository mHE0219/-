"""Modular kit dressing + designer-led greybox (docs/pipeline_v2.md).
python -m unittest discover -s tests"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from geobox import greybox, kit, library, project  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "sample_osm.json")


def _block(**kw):
    b = {"id": "gb:t", "kind": "residential", "center": [10.0, -5.0], "base_y": 2.0, "size": [12.0, 16.0, 10.0],
         "rot_y": 30.0, "tags": ["residential"], "source": "manual", "feature": None, "locked": True, "asset_lock": None}
    b.update(kw)
    return b


class KitUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = library.load(library.DEFAULT_LIBRARY)
        cls.assets = library.by_id(cls.lib)
        cls.sets = kit.kit_sets(cls.lib)

    def test_sets(self):
        self.assertIn("demo_shophouse", self.sets)
        self.assertIn("demo_tower", self.sets)
        sh = self.sets["demo_shophouse"]
        for role in ("wall", "window", "shopfront", "door", "corner", "parapet", "roof"):
            self.assertIn(role, sh["roles"], role)
        self.assertEqual(sh["module"], [3.0, 3.2])

    def test_piece_conventions(self):
        for a in self.lib["assets"]:
            if a.get("category") != "kit":
                continue
            k, b = a["kit"], a["bounds"]
            if k["role"] in ("wall", "window", "door", "parapet") or (k["role"] == "shopfront" and not k.get("min_depth")):
                self.assertGreaterEqual(b["min"][2], -1e-6, a["id"])      # back face on z=0
                self.assertAlmostEqual(b["min"][1], 0.0, places=4, msg=a["id"])
                self.assertLessEqual(b["max"][0], k["module"][0] / 2 + 1e-6, a["id"])
                self.assertGreaterEqual(b["min"][0], -k["module"][0] / 2 - 1e-6, a["id"])
            if k["role"] == "corner":
                self.assertAlmostEqual(b["min"][0], 0.0, places=4)
                self.assertAlmostEqual(b["min"][2], 0.0, places=4)

    def test_strategy(self):
        self.assertEqual(kit.strategy_for(_block(), self.sets)[1]["name"], "demo_shophouse")
        tall = _block(kind="office", tags=["office"], size=[30, 80, 24])
        self.assertEqual(kit.strategy_for(tall, self.sets)[1]["name"], "demo_tower")
        self.assertEqual(kit.strategy_for(_block(kind="industrial", tags=["industrial"]), self.sets)[0], "whole")
        self.assertEqual(kit.strategy_for(_block(dress="none"), self.sets)[0], "none")
        self.assertEqual(kit.strategy_for(_block(dress="whole"), self.sets)[0], "whole")
        self.assertEqual(kit.strategy_for(_block(asset_lock="bld_house_a"), self.sets)[0], "whole")
        forced = _block(kind="industrial", tags=["industrial"], dress="kit")
        self.assertEqual(kit.strategy_for(forced, self.sets)[0], "kit")
        self.assertEqual(kit.strategy_for(_block(kit_set="demo_tower"), self.sets)[1]["name"], "demo_tower")

    def _dress(self, b):
        mode, s = kit.strategy_for(b, self.sets)
        return kit.dress_block(b, s, seed=3), s

    def test_faces_filled_and_floors_aligned(self):
        b = _block()
        out, s = self._dress(b)
        ids = [i["id"] for i in out]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [i["id"] for i in self._dress(b)[0]])  # deterministic
        summ = kit.summary(b, s)
        floors = summ["floors"]
        rows = defaultdict(list)  # (face, floor) -> pieces
        for i in out:
            face, floor, _col = i["id"].rsplit(":", 3)[1:]
            if face.isdigit() and floor.isdigit():
                rows[(int(face), int(floor))].append(i)
        lengths = {0: 12.0, 1: 10.0, 2: 12.0, 3: 10.0}
        for (face, floor), pieces in rows.items():
            width = sum(self.assets[i["asset"]]["kit"]["module"][0] * i["scale"][0] for i in pieces)
            self.assertAlmostEqual(width, lengths[face], places=2)              # face exactly filled
            for i in pieces:
                self.assertAlmostEqual(i["p"][1], 2.0 + floor * summ["floor_height"], places=2)
                self.assertAlmostEqual((i["rot_y"] - (30 + 90 * face)) % 360, 0, places=2)
        self.assertEqual({f for (_fa, f) in rows}, set(range(floors)))
        # windows line up: a column is window (or not) on every middle floor
        for face in range(4):
            cols = defaultdict(set)
            for (fa, fl), pieces in rows.items():
                if fa == face and 0 < fl < floors - 1:
                    for i in pieces:
                        cols[i["id"].rsplit(":", 1)[1]].add(self.assets[i["asset"]]["kit"]["role"])
            for roles in cols.values():
                self.assertEqual(len(roles), 1, roles)
        # front ground floor = arcade/door; parapet + roof + 4 corners exist
        ground = [self.assets[i["asset"]]["kit"]["role"] for i in rows[(0, 0)]]
        self.assertTrue(set(ground) <= {"shopfront", "door"}, ground)
        corners = [i for i in out if i["id"].rsplit(":", 3)[1].startswith("c")]
        self.assertEqual(len(corners), 4)
        self.assertTrue(any(":p:" in i["id"] for i in out))
        self.assertTrue(any(i["id"].endswith(":r:0:0") for i in out))

    def test_corners_on_block_corners(self):
        b = _block(rot_y=0.0, center=[0.0, 0.0])
        out, _s = self._dress(b)
        pts = sorted((round(i["p"][0], 2), round(i["p"][2], 2)) for i in out if i["id"].rsplit(":", 3)[1].startswith("c"))
        self.assertEqual(pts, sorted([(6.0, 5.0), (6.0, -5.0), (-6.0, -5.0), (-6.0, 5.0)]))

    def test_shallow_block_has_no_arcade(self):
        out, _s = self._dress(_block(size=[9.0, 10.0, 6.0]))
        roles = {self.assets[i["asset"]]["kit"]["role"] for i in out}
        self.assertNotIn("shopfront", roles)  # arcade needs >= 8 m depth
        self.assertIn("door", roles)


class KitProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_kit_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scene_kit_and_edits(self):
        sc = project.build("k", (25.04, 121.52), (300, 300), osm_file=FIXTURE, root=self.tmp, log=lambda *a: None)
        kits = [i for i in sc["instances"] if i["category"] == "kit"]
        self.assertGreater(len(kits), 1000)
        blocks = {b["id"]: b for b in sc["greybox"]}
        self.assertTrue(all(i["block"] in blocks for i in kits))
        # scene.json is compact but plain JSON
        path = os.path.join(self.tmp, "k", "scene.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["instances"]), len(sc["instances"]))
        # lock one kit piece, delete another, re-dress with a new seed
        keep, gone = kits[0], kits[1]
        keep["p"][1] += 1.0
        keep["locked"] = True
        sc["instances"] = [i for i in sc["instances"] if i["id"] != gone["id"]]
        sc["suppressed"].append(gone["id"])
        project.save_scene("k", sc, root=self.tmp, export_usd=False)
        again = project.rescatter("k", rules_overrides={"seed": 5}, root=self.tmp, log=lambda *a: None)
        ids = {i["id"]: i for i in again["instances"]}
        self.assertNotIn(gone["id"], ids)
        self.assertEqual(ids[keep["id"]]["p"], keep["p"])
        # a block switched to whole gets one building asset instead of kit pieces
        bid = kits[5]["block"]
        for b in again["greybox"]:
            if b["id"] == bid:
                b["dress"], b["locked"] = "whole", True
        again["instances"] = [i for i in again["instances"] if i.get("block") != bid or i.get("locked")]
        project.save_scene("k", again, root=self.tmp, export_usd=False)
        third = project.rescatter("k", root=self.tmp, log=lambda *a: None)
        mine = [i for i in third["instances"] if i.get("block") == bid and not i.get("locked")]
        self.assertTrue(mine and all(i["category"] == "building" for i in mine), [i["category"] for i in mine])

    def test_designer_greybox_empty_then_seed_and_clear(self):
        sc = project.build("e", (25.04, 121.52), (300, 300), osm_file=FIXTURE, root=self.tmp, log=lambda *a: None,
                           greybox_source="empty")
        self.assertEqual(sc["greybox"], [])
        self.assertEqual(sc["greybox_source"], "empty")
        self.assertFalse([i for i in sc["instances"] if i["category"] in ("kit", "building")])
        self.assertTrue([i for i in sc["instances"] if i["category"] == "tree"])  # map still drives vegetation
        # designer draws a block -> art pass dresses it with a kit
        sc["greybox"].append(_block(id="gb:manual:one", center=[0.0, 0.0], base_y=0.0, rot_y=0.0))
        project.save_scene("e", sc, root=self.tmp, export_usd=False)
        sc2 = project.rescatter("e", root=self.tmp, log=lambda *a: None)
        self.assertEqual(len(sc2["greybox"]), 1)
        self.assertTrue([i for i in sc2["instances"] if i.get("block") == "gb:manual:one" and i["category"] == "kit"])
        # seed OSM draft blocks, then clear them again (manual block survives)
        r = greybox.seed_from_osm("e", root=self.tmp)
        self.assertGreater(r["added"], 10)
        r = greybox.clear("e", root=self.tmp)
        self.assertEqual(r["blocks"], 1)
        with open(os.path.join(self.tmp, "e", "scene.json"), encoding="utf-8") as fh:
            sc3 = json.load(fh)
        self.assertEqual([b["id"] for b in sc3["greybox"]], ["gb:manual:one"])
        self.assertEqual(sc3["greybox_source"], "empty")

    def test_usd_kit_instancer(self):
        try:
            from pxr import Usd, UsdGeom
        except ImportError:
            self.skipTest("usd-core not installed")
        project.build("u", (25.04, 121.52), (200, 200), osm_file=FIXTURE, root=self.tmp, log=lambda *a: None)
        stage = Usd.Stage.Open(os.path.join(self.tmp, "u", "scene.usda"))
        pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/GeoBox/Instances/Kit"))
        self.assertTrue(pi)
        n = len(pi.GetPositionsAttr().Get())
        self.assertGreater(n, 100)
        self.assertEqual(len(pi.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default())), n)


if __name__ == "__main__":
    unittest.main()
