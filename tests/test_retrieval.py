"""Retrieval (素材檢索) tests.  python -m unittest discover -s tests"""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from geobox import library, project, retrieval  # noqa: E402


def _png(w=4, h=4, rgba=(200, 30, 30, 255)):
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (retrieval.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_ret_")
        self.lib = library.init_default(os.path.join(self.tmp, "library.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def idx(self):
        return retrieval.AssetIndex.load(library.load(os.path.join(self.tmp, "library.json")))

    def test_api_shape_stable(self):
        hits = self.idx().search(category="building", size=[18, 24, 14], tags=["apartments"], limit=3)
        self.assertEqual(hits[0]["id"], "bld_midrise_a")
        for h in hits:
            for k in ("id", "asset", "score", "scale", "reasons", "real_size", "fit", "penalties"):
                self.assertIn(k, h)
            self.assertTrue(0.0 <= h["score"] <= 1.0)
        self.assertGreaterEqual(hits[0]["score"], hits[1]["score"])
        w, h_, d = library.asset_size(hits[0]["asset"])
        self.assertAlmostEqual(w * hits[0]["scale"][0], 18, places=2)
        self.assertAlmostEqual(h_ * hits[0]["scale"][1], 24, places=2)

    def test_dimension_ranking(self):
        idx = self.idx()
        tall = idx.search(category="building", size=[30, 90, 28], limit=1)[0]
        self.assertEqual(tall["id"], "bld_highrise_a")
        house = idx.search(category="building", size=[10, 6.5, 8], limit=1)[0]
        self.assertEqual(house["id"], "bld_house_a")

    def test_synonyms_and_chinese(self):
        idx = self.idx()
        self.assertEqual(idx.search(text="棕櫚", limit=1)[0]["id"], "tree_palm_a")
        self.assertEqual(idx.search(text="路燈", limit=1)[0]["id"], "prop_streetlamp_a")
        apt = {h["id"] for h in idx.search(category="building", tags=["公寓"], limit=2)}
        self.assertIn("bld_midrise_a", apt)
        ids = [h["id"] for h in idx.search(text="tree")]
        self.assertNotIn("prop_streetlamp_a", ids, "'tree' must not match 'street'")
        self.assertEqual(idx.search(text="磚造不存在的東西"), [])

    def test_scale_range_penalty(self):
        idx = self.idx()
        h = [x for x in idx.search(category="tree", size=[30, 60, 30], fit="uniform", limit=20)]
        self.assertTrue(all("scale_range" in x["penalties"] for x in h))
        self.assertTrue(all(x["real_size"][1] < 60 for x in h), "real size is clamped to scale_range")

    def test_filters_and_diversity(self):
        lib = library.load(os.path.join(self.tmp, "library.json"))
        retrieval.rebuild_index(lib)
        idx = retrieval.AssetIndex.load(lib)
        low = idx.search(category="building", max_tris=20, limit=10)
        self.assertEqual([h["id"] for h in low], ["bld_generic_box"])
        for a in lib["assets"]:
            if a["id"] == "bld_house_a":
                a["style"] = "japanese"
        idx = retrieval.AssetIndex(lib)
        self.assertEqual([h["id"] for h in idx.search(category="building", style="日式")], ["bld_house_a"])
        div = idx.search(category="tree", limit=5, diversity=0.9)
        fams = [retrieval._family(h["id"]) for h in div[:4]]
        self.assertEqual(len(set(fams)), 4, fams)

    def test_index_incremental_and_thumbnail(self):
        lib = library.load(os.path.join(self.tmp, "library.json"))
        meta = retrieval.rebuild_index(lib)
        self.assertEqual(len(meta), len(lib["assets"]))
        m = meta["bld_house_a"]
        self.assertEqual(m["tri_count"], 24)
        self.assertEqual(sorted(m["materials"]), ["roof", "trim", "wall"])
        self.assertIn("獨棟住宅", " ".join(m["keywords"]))
        t0 = m["indexed_at"]
        time.sleep(0.15)
        meta2 = retrieval.rebuild_index(lib)
        self.assertEqual(meta2["bld_house_a"]["indexed_at"], t0, "unchanged asset is not re-indexed")
        info = retrieval.save_thumbnail(lib, "bld_house_a", _png())
        self.assertEqual(info["thumbnail"], "thumbs/bld_house_a.png")
        try:
            import PIL  # noqa: F401
            self.assertEqual(info["colors"][0]["hex"], "#c81e1e")
        except ImportError:
            pass
        with self.assertRaises(ValueError):
            retrieval.save_thumbnail(lib, "bld_house_a", b"not a png")
        with self.assertRaises(ValueError):
            retrieval.thumb_path(lib, "../evil")
        with open(retrieval.index_path(lib), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["type"], "geobox.asset_index")

    def test_glb_stats(self):
        pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
        idxb = struct.pack("<3H", 0, 1, 2) + b"\x00\x00"
        binb = pos + idxb
        gltf = {"asset": {"version": "2.0"}, "scenes": [{"nodes": [0, 1]}],
                "nodes": [{"mesh": 0}, {"mesh": 0, "translation": [2, 0, 0]}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
                "materials": [{"name": "brick_wall"}],
                "buffers": [{"byteLength": len(binb)}],
                "bufferViews": [{"buffer": 0, "byteLength": 36}, {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
                              {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}]}
        js = json.dumps(gltf).encode()
        js += b" " * (-len(js) % 4)
        body = struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(binb), b"BIN\x00") + binb
        p = os.path.join(self.tmp, "w.glb")
        with open(p, "wb") as fh:
            fh.write(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)
        st = retrieval._glb_stats(p)
        self.assertEqual((st["tri_count"], st["vertex_count"], st["materials"]), (2, 6, ["brick_wall"]))


class MatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="geobox_match_")
        cls.scene = project.build("m", (25.04, 121.52), (300, 300), osm_file=os.path.join(HERE, "fixtures", "sample_osm.json"),
                                  root=cls.tmp, log=lambda *a: None)
        cls.layout = project.read_json(os.path.join(cls.tmp, "m", "layout.json"))
        cls.lib = library.load(library.DEFAULT_LIBRARY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_instance_match(self):
        inst = next(i for i in self.scene["instances"] if i["category"] == "building" and i.get("target"))
        q = retrieval.match_query(self.scene, self.lib, self.layout, inst["id"])
        self.assertEqual(q["category"], "building")
        blocks = {b["id"]: b for b in self.scene.get("greybox", []) or []}
        expect = blocks[inst["block"]]["size"] if inst.get("block") in blocks else inst["target"]["size"]
        self.assertEqual([round(v, 2) for v in q["size"]], [round(v, 2) for v in expect])
        self.assertEqual(q["current"], inst["asset"])
        tree = next(i for i in self.scene["instances"] if i["category"] == "tree")
        q = retrieval.match_query(self.scene, self.lib, self.layout, tree["id"])
        self.assertEqual((q["category"], q["fit"]), ("tree", "uniform"))

    def test_greybox_block_match(self):
        scene = dict(self.scene)
        scene["greybox"] = [{"id": "gb:test", "kind": "apartments", "center": [0, 0], "base_y": 0,
                             "size": [20, 30, 15], "rot_y": 0, "tags": [], "source": "manual",
                             "locked": False, "asset_lock": None}]
        q = retrieval.match_query(scene, self.lib, self.layout, "gb:test")
        self.assertEqual(q["size"], [20, 30, 15])
        self.assertIn("apartments", q["tags"])
        hits = retrieval.AssetIndex.load(self.lib).search(**{k: q[k] for k in ("category", "size", "tags")})
        self.assertEqual(hits[0]["id"], "bld_midrise_a")
        with self.assertRaises(KeyError):
            retrieval.match_query(scene, self.lib, self.layout, "gb:nope")


if __name__ == "__main__":
    unittest.main()
