"""Unit tests for the stdlib pipeline.  python -m unittest discover -s tests"""
import math
import os
import random
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from geobox import geom, library, osm, project, scatter  # noqa: E402
from geobox.geo import BoxSpec, LocalFrame, parse_location, sun_direction  # noqa: E402
from geobox.layout import building_height, parse_length  # noqa: E402
from geobox.terrain import decode_png_rgb  # noqa: E402


class GeoTest(unittest.TestCase):
    def test_true_metres(self):
        # 0.001 deg of latitude ~ 110.8 m at 25N; Web Mercator would say ~122 m. We must be true.
        f = LocalFrame(25.0, 121.5)
        x, z = f.to_local(25.001, 121.5)
        self.assertAlmostEqual(abs(z), 110.77, delta=0.05)
        self.assertLess(z, 0)  # north is -Z
        x, z = f.to_local(25.0, 121.501)
        self.assertAlmostEqual(x, 100.92, delta=0.05)  # 111.32 * cos(25deg)
        self.assertGreater(x, 0)  # east is +X

    def test_roundtrip(self):
        f = LocalFrame(35.6586, 139.7454)
        for x, z in ((0, 0), (500, -250), (-1200, 900)):
            lat, lon = f.from_local(x, z)
            x2, z2 = f.to_local(lat, lon)
            self.assertAlmostEqual(x, x2, places=3)
            self.assertAlmostEqual(z, z2, places=3)

    def test_box_bbox(self):
        b = BoxSpec(25.04, 121.52, 600, 400)
        s, w, n, e = b.latlon_bbox()
        f = b.frame
        self.assertAlmostEqual(f.to_local(n, b.lon)[1], -200, delta=0.5)
        self.assertAlmostEqual(f.to_local(b.lat, e)[0], 300, delta=0.5)

    def test_parse_google_urls(self):
        r = parse_location("https://www.google.com/maps/@25.0339639,121.5644722,17z")
        self.assertEqual(r["center"], [25.0339639, 121.5644722])
        self.assertTrue(1200 < r["suggested_size"][0] < 1500)
        r = parse_location("https://www.google.com/maps/place/X/@25.03,121.56,15z/data=!3m1!4b1!8m2!3d25.0339!4d121.5645")
        self.assertEqual(r["center"], [25.0339, 121.5645])
        r = parse_location("https://www.google.com/maps/@25.0339,121.5644,612m/data=!3m1!1e3")
        self.assertEqual(r["suggested_size"][1], 612)
        self.assertEqual(parse_location(" 25.03, 121.56 ")["center"], [25.03, 121.56])
        self.assertEqual(parse_location("https://maps.google.com/?q=25.03,121.56")["center"], [25.03, 121.56])
        with self.assertRaises(ValueError):
            parse_location("https://www.google.com/maps/search/coffee")

    def test_sun_direction(self):
        x, y, z = sun_direction(90, 0)  # east, horizon
        self.assertAlmostEqual(x, 1)
        x, y, z = sun_direction(0, 0)  # north -> -Z
        self.assertAlmostEqual(z, -1)


class GeomTest(unittest.TestCase):
    def test_obb(self):
        # 20x8 rectangle rotated by 30 deg (CCW seen from above)
        a = math.radians(30)
        pts = []
        for u, v in ((-10, -4), (10, -4), (10, 4), (-10, 4)):
            # local X axis -> world (cos a, -sin a) in (x, z)
            pts.append((u * math.cos(a) + v * math.sin(a), -u * math.sin(a) + v * math.cos(a)))
        r = geom.min_area_rect(pts)
        self.assertAlmostEqual(r["size"][0], 20, places=3)
        self.assertAlmostEqual(r["size"][1], 8, places=3)
        self.assertAlmostEqual(r["angle"], a, places=4)

    def test_orientation_and_clip(self):
        sq = [[0, 0], [10, 0], [10, 10], [0, 10]]  # raw (x,z) positive area => CW from above
        o = geom.orient_outer(sq)
        self.assertLess(geom.signed_area_xz(o), 0)
        c = geom.clip_ring_rect(o, (5, -5, 20, 5))
        self.assertAlmostEqual(geom.area(c), 25)
        parts = geom.clip_polyline_rect([[-10, 0], [10, 0], [10, 20], [-10, 20]], (-5, -5, 5, 25))
        self.assertEqual(len(parts), 2)

    def test_poisson(self):
        pts = geom.poisson_disk((0, 0, 100, 100), 5.0, random.Random(1))
        self.assertGreater(len(pts), 150)
        for i in range(0, len(pts), 7):
            for j in range(i + 1, len(pts)):
                d = math.dist(pts[i], pts[j])
                self.assertGreaterEqual(d, 5.0 - 1e-9)


class OsmLayoutTest(unittest.TestCase):
    def test_ring_assembly(self):
        a, b, c, d = (0, 0), (0, 1), (1, 1), (1, 0)
        rings = osm.assemble_rings([[a, b], [c, b], [c, d, a]])
        self.assertEqual(len(rings), 1)
        self.assertEqual(len(rings[0]), 5)

    def test_heights(self):
        self.assertAlmostEqual(parse_length("40'"), 12.192)
        self.assertEqual(parse_length("12.5 m"), 12.5)
        h, mh, lv, src = building_height({"building": "apartments", "building:levels": "10"})
        self.assertEqual(src, "levels")
        self.assertAlmostEqual(h, 32.6)
        h, *_rest, src = building_height({"building": "house"})
        self.assertEqual((h, src), (7.0, "default"))


class PngTest(unittest.TestCase):
    def test_decode_filters(self):
        w, h = 3, 2
        rows = [bytes([1]) + bytes([10, 20, 30, 1, 1, 1, 2, 2, 2]),  # Sub filter
                bytes([2]) + bytes([1, 1, 1, 1, 1, 1, 1, 1, 1])]  # Up filter
        raw = zlib.compress(b"".join(rows))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
        W, H, bpp, out = decode_png_rgb(png)
        self.assertEqual(list(out[0][:6]), [10, 20, 30, 11, 21, 31])
        self.assertEqual(list(out[1][:3]), [11, 21, 31])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_and_merge(self):
        sc = project.build("t", (25.04, 121.52), (600, 600), osm_file=os.path.join(HERE, "fixtures", "sample_osm.json"),
                           root=self.tmp, log=lambda *a: None)
        cats = {i["category"] for i in sc["instances"]}
        self.assertEqual(cats, {"building", "tree", "prop"})
        for i in sc["instances"]:
            x0, z0, x1, z1 = sc["box"]["rect"]
            self.assertTrue(x0 - 30 <= i["p"][0] <= x1 + 30 and z0 - 30 <= i["p"][2] <= z1 + 30)
        # building scale reproduces the footprint size in metres
        lib = library.load(library.DEFAULT_LIBRARY)
        assets = library.by_id(lib)
        b = next(i for i in sc["instances"] if i["category"] == "building" and "fit_warning" not in i)
        w, h, d = library.asset_size(assets[b["asset"]])
        self.assertAlmostEqual(w * b["scale"][0], b["target"]["size"][0], places=1)
        self.assertAlmostEqual(h * b["scale"][1], b["target"]["size"][1], places=1)
        # deterministic
        sc2 = project.rescatter("t", root=self.tmp, log=lambda *a: None)
        self.assertEqual([i["id"] for i in sc["instances"]], [i["id"] for i in sc2["instances"]])
        # edits survive a rescatter with a different seed
        sc2["instances"][0]["locked"] = True
        sc2["instances"][0]["p"][0] += 3
        gone = sc2["instances"].pop(5)
        sc2["suppressed"].append(gone["id"])
        sc2["instances"].append({"id": "manual:x", "asset": "bush_a", "category": "tree", "p": [1, 0, 1],
                                 "rot_y": 0, "scale": [1, 1, 1], "source": "manual", "locked": True})
        project.save_scene("t", sc2, root=self.tmp)
        sc3 = project.rescatter("t", rules_overrides={"seed": 99}, root=self.tmp, log=lambda *a: None)
        ids = {i["id"]: i for i in sc3["instances"]}
        self.assertIn("manual:x", ids)
        self.assertNotIn(gone["id"], ids)
        self.assertEqual(ids[sc2["instances"][0]["id"]]["p"], sc2["instances"][0]["p"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "t", "scene.usda")))

    def test_usd_valid(self):
        try:
            from pxr import Usd, UsdGeom
        except ImportError:
            self.skipTest("usd-core not installed")
        project.build("u", (25.04, 121.52), (300, 300), osm_file=os.path.join(HERE, "fixtures", "sample_osm.json"),
                      root=self.tmp, log=lambda *a: None)
        stage = Usd.Stage.Open(os.path.join(self.tmp, "u", "scene.usda"))
        pi = UsdGeom.PointInstancer(stage.GetPrimAtPath("/GeoBox/Instances/Buildings"))
        self.assertTrue(pi)
        n = len(pi.GetPositionsAttr().Get())
        self.assertEqual(len(pi.GetProtoIndicesAttr().Get()), n)
        self.assertEqual(len(pi.ComputeInstanceTransformsAtTime(Usd.TimeCode.Default(), Usd.TimeCode.Default())), n)


class LibraryTest(unittest.TestCase):
    def test_unit_guess_and_normalize(self):
        units, up, _ = library.guess_units("tree", [-200, 0, -200], [200, 1200, 200])
        self.assertEqual((units, up), ("cm", "+Y"))
        units, up, _ = library.guess_units("tree", [-2, -2, 0], [2, 2, 9])
        self.assertEqual(up, "+Z")
        tmp = tempfile.mkdtemp()
        try:
            obj = os.path.join(tmp, "t.obj")
            with open(obj, "w", encoding="utf-8") as fh:  # a 1000cm-tall, Z-up "tree" with its pivot off-centre
                fh.write("v 100 100 0\nv 300 300 1000\nv 100 300 0\nf 1 2 3\n")
            lib = {"_root": tmp, "assets": []}
            a = {"id": "t", "category": "tree", "files": {"web": "t.obj"}}
            library._fill_defaults(a)
            library.normalize_entry(lib, a, guess=True)
            self.assertEqual((a["source_units"], a["source_up"]), ("cm", "+Z"))
            self.assertAlmostEqual(a["bounds"]["max"][1], 10.0, places=4)
            self.assertAlmostEqual(a["bounds"]["min"][0], -1.0, places=4)
        finally:
            shutil.rmtree(tmp)


class GlbTest(unittest.TestCase):
    def test_glb_bounds_with_node_transform(self):
        import json as _json
        pos = struct.pack("<9f", 0, 0, 0, 200, 0, 0, 0, 0, 600)  # cm, Z-up triangle, 6 m "tall"
        gltf = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
                "nodes": [{"children": [1], "scale": [1, 1, 1]}, {"mesh": 0, "translation": [50, 0, 0]}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
                "buffers": [{"byteLength": len(pos)}], "bufferViews": [{"buffer": 0, "byteLength": len(pos)}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                               "min": [0, 0, 0], "max": [200, 0, 600]}]}
        js = _json.dumps(gltf).encode()
        js += b" " * (-len(js) % 4)
        body = struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(pos), b"BIN\x00") + pos
        glb = struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "a.glb"), "wb") as fh:
                fh.write(glb)
            mn, mx = library.measure_raw_bounds(os.path.join(tmp, "a.glb"))
            self.assertEqual((mn[0], mx[0], mx[2]), (50, 250, 600))
            lib = {"_root": tmp, "assets": []}
            notes = library.scan(lib, tmp, category="tree")
            a = lib["assets"][0]
            self.assertEqual((a["source_units"], a["source_up"]), ("cm", "+Z"), notes)
            self.assertAlmostEqual(a["bounds"]["max"][1], 6.0, places=4)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
