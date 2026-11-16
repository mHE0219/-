"""Tests for houdini/python/geobox_houdini/sky.py (maths + build against tests/mock_hou.py).

Run:  python -m unittest discover -s tests/sky -p "test_*.py"
"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(ROOT, "houdini", "python"))
sys.path.insert(0, TESTS)

import mock_hou  # noqa: E402

hou = mock_hou.install()
# the shared mock has no numeric parm templates yet — the generic template is enough here
for _n in ("FloatParmTemplate", "IntParmTemplate"):
    if not hasattr(hou, _n):
        setattr(hou, _n, mock_hou._Template)

from geobox_houdini import sky  # noqa: E402


class SkyMathTest(unittest.TestCase):
    def test_sun_direction_convention(self):
        # north (-Z), east (+X), south (+Z), zenith
        for az, el, want in [(0, 0, (0, 0, -1)), (90, 0, (1, 0, 0)), (180, 0, (0, 0, 1)), (33, 90, (0, 1, 0))]:
            d = sky.sun_direction(az, el)
            for a, b in zip(d, want):
                self.assertAlmostEqual(a, b, places=6)

    def test_sun_rotation_points_local_z_at_sun(self):
        for az in (0, 37, 90, 145, 180, 260, 359):
            for el in (-3, 2, 30, 60, 89):
                rx, ry, rz = sky.sun_rotation(az, el)
                z_world = sky.rotate_xyz((0, 0, 1), rx, ry, rz)
                d = sky.sun_direction(az, el)
                for a, b in zip(z_world, d):
                    self.assertAlmostEqual(a, b, places=6, msg=(az, el))
                # the light shines along its -Z = away from the sun
                shine = sky.rotate_xyz((0, 0, -1), rx, ry, rz)
                self.assertLess(sum(a * b for a, b in zip(shine, d)), -0.999)

    def test_hex_to_linear(self):
        self.assertEqual(sky.hex_to_linear("#ffffff"), (1.0, 1.0, 1.0))
        self.assertEqual(sky.hex_to_linear("#000"), (0.0, 0.0, 0.0))
        r, g, b = sky.hex_to_linear("#808080")
        self.assertAlmostEqual(r, 0.2158605, places=5)

    def test_sun_light_reddens_at_low_sun(self):
        hi = sky._merge(sky.DEFAULTS, {"sun": {"elevation": 60, "color": "#ffffff"}})
        lo = sky._merge(sky.DEFAULTS, {"sun": {"elevation": 3, "color": "#ffffff"}})
        (c_hi, i_hi), (c_lo, i_lo) = sky.sun_light(hi), sky.sun_light(lo)
        self.assertGreater(c_hi[2], 0.8)          # near white at high sun
        self.assertLess(c_lo[2], c_lo[0] * 0.5)   # strongly red/orange at low sun
        self.assertGreater(i_hi, i_lo)
        night = sky._merge(sky.DEFAULTS, {"sun": {"elevation": -10}})
        self.assertLess(sky.sun_light(night)[1], 1e-3)

    def test_seed_vector_deterministic(self):
        self.assertEqual(sky.seed_vector(1), sky.seed_vector(1))
        self.assertNotEqual(sky.seed_vector(1), sky.seed_vector(2))
        self.assertTrue(all(0 <= v < 97 for v in sky.seed_vector(5)))

    def test_heading(self):
        x, z = sky.heading_xz(90)
        self.assertAlmostEqual(x, 1.0)
        self.assertAlmostEqual(z, 0.0, places=9)


class SkyBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_sky_")
        self.params_path = os.path.join(self.tmp, "sky_params.json")
        with open(self.params_path, "w") as fh:
            json.dump({"version": 1, "type": "geobox.sky", "sun": {"azimuth": 200, "elevation": 25},
                       "clouds": {"coverage": 0.6, "seed": 3}, "animation": {"timeScale": 4}}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_params_merges_defaults(self):
        p = sky.load_params(self.params_path)
        self.assertEqual(p["clouds"]["coverage"], 0.6)
        self.assertEqual(p["clouds"]["bottom"], sky.DEFAULTS["clouds"]["bottom"])
        self.assertEqual(p["sun"]["color"], sky.DEFAULTS["sun"]["color"])

    def test_hdr_pattern(self):
        self.assertIsNone(sky.hdr_pattern_for(self.params_path))
        os.makedirs(os.path.join(self.tmp, "sky"))
        open(os.path.join(self.tmp, "sky", "sky.0001.hdr"), "wb").close()
        self.assertTrue(sky.hdr_pattern_for(self.params_path).endswith("sky/sky.0001.hdr"))
        open(os.path.join(self.tmp, "sky", "sky.0002.hdr"), "wb").close()
        self.assertTrue(sky.hdr_pattern_for(self.params_path).endswith("sky/sky.$F4.hdr"))

    def test_build_sky_creates_network(self):
        os.makedirs(os.path.join(self.tmp, "sky"))
        for f in (1, 2, 3):
            open(os.path.join(self.tmp, "sky", "sky.%04d.hdr" % f), "wb").close()
        nodes = sky.build_sky(self.params_path, parent="/obj", name="gb_sky_test")
        self.assertEqual(nodes["subnet"].path(), "/obj/gb_sky_test")
        self.assertTrue(nodes["env"].parm("env_map").eval().endswith("sky.$F4.hdr"))
        self.assertEqual(nodes["sun"].parm("light_type").eval(), "distant")
        rx, ry, _ = nodes["sun"].parm("r").eval()
        exp = sky.sun_rotation(200, 25)
        self.assertAlmostEqual(rx, exp[0])
        self.assertAlmostEqual(ry, exp[1])
        wr = nodes["wrangle"]
        self.assertIn('#include "geobox_sky.h"', wr.parm("snippet").eval())
        self.assertAlmostEqual(wr.parm("coverage").eval(), 0.6)
        self.assertAlmostEqual(wr.parm("time_scale").eval(), 4)
        self.assertEqual(tuple(wr.parm("seed_offset").eval()), sky.seed_vector(3))
        self.assertIs(nodes["out"].inputs[0], wr)
        # every chf/chv/chi in the snippet has a spare parm
        import re
        names = set(re.findall(r'ch[fvi]\("([a-z_]+)"\)', sky.WRANGLE_SNIPPET))
        spare = {n for n, _l, _k, _v in sky._spare_values(sky.load_params(self.params_path))}
        self.assertEqual(names - spare, set())

    def test_vex_header_declares_used_functions(self):
        with open(os.path.join(ROOT, "houdini", "vex", "include", "geobox_sky.h"), encoding="utf-8") as fh:
            hdr = fh.read()
        for fn in ("gbx_cloud_layer_density", "gbx_cloud_density", "gbx_sky_background", "gbx_latlong_dir"):
            self.assertRegex(hdr, r"\b%s\s*\(" % fn)
        self.assertEqual(hdr.count("{"), hdr.count("}"))
        self.assertEqual(hdr.count("("), hdr.count(")"))


if __name__ == "__main__":
    unittest.main()
