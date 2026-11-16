"""Asset retrieval (素材檢索): index the library and rank assets for a query.

PUBLIC API (used by scatter / greybox / farm / web UI — keep stable):

    idx = AssetIndex.load(lib)                 # lib = geobox.library.load(...)
    hits = idx.search(category="building", size=[w, h, d], tags=["apartments"],
                      text="磚 brick", fit="stretch", limit=10, exclude=(),
                      # optional extras:
                      style=None, era=None, lod=None, max_tris=None, diversity=0.0)
    # -> [{"id", "asset": entry, "score": 0..1 (higher = better),
    #      "scale": [sx, sy, sz] that reaches `size` (raw, not clamped),
    #      "reasons": [str, ...],
    #      "real_size": [w, h, d] after clamping to scale_range,
    #      "fit": mode, "penalties": {name: value}}]

    meta = index_asset(lib, asset_id)          # compute index metadata of one asset (no write)
    index = rebuild_index(lib, asset_ids=None, force=False)   # incremental -> assets/index.json

The ingest step (locally, via the web UI thumbnail uploader, or as farm jobs)
fills assets/index.json; search() works without it (falls back to manifest data).

Index entry (assets/index.json -> "assets" -> <id>):
    size [w,h,d] m, aspect (w/d), volume, footprint (w*d), tri_count, vertex_count,
    materials [names], keywords [lowercase tokens incl. Chinese label], label,
    style, era, lods, thumbnail "thumbs/<id>.png", thumb_mtime, colors [{hex, frac}],
    source, source_mtime, source_size, sha1, fingerprint, indexed_at
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import struct
import time
import urllib.parse
from typing import Dict, Iterable, List, Optional, Sequence

from . import library as libmod

INDEX_VERSION = 2
MAX_THUMB_BYTES = 4 * 1024 * 1024
PNG_SIG = b"\x89PNG\r\n\x1a\n"

# --- synonyms: query words <-> tags (EN / 繁中 / 簡中) ---------------------------
SYNONYMS: List[set] = [
    {"residential", "住宅", "住家", "民宅", "居住"},
    {"apartments", "apartment", "公寓", "大樓", "集合住宅", "華廈"},
    {"house", "detached", "透天", "獨棟", "別墅", "平房", "房子", "住宅"},
    {"commercial", "商業", "商辦", "店面", "商店"},
    {"retail", "shop", "store", "零售", "店", "商場", "賣場", "supermarket", "超市"},
    {"office", "辦公", "辦公室", "辦公大樓", "寫字樓"},
    {"tower", "highrise", "high-rise", "skyscraper", "高樓", "摩天", "塔"},
    {"industrial", "工業", "工廠", "廠房", "factory", "warehouse", "倉庫"},
    {"hotel", "飯店", "旅館", "酒店"},
    {"school", "學校", "校舍", "university", "大學"},
    {"hospital", "醫院"},
    {"temple", "廟", "寺", "宮", "宮廟", "shrine", "church", "教堂"},
    {"generic", "通用", "量體", "box", "方塊", "灰盒", "greybox"},
    {"tree", "樹", "樹木", "vegetation", "植被", "植物"},
    {"broadleaf", "broadleaved", "闊葉", "闊葉樹", "deciduous"},
    {"conifer", "needleleaved", "針葉", "針葉樹", "松", "杉", "pine"},
    {"palm", "棕櫚", "椰子", "椰子樹"},
    {"bush", "shrub", "scrub", "灌木", "樹叢"},
    {"street", "行道樹", "路樹", "街道"},
    {"forest", "森林", "樹林"},
    {"streetlamp", "lamp", "light", "路燈", "燈"},
    {"prop", "道具", "街道傢俱", "furniture"},
    {"brick", "磚", "紅磚"},
    {"concrete", "水泥", "混凝土", "清水模"},
    {"glass", "玻璃", "帷幕"},
    {"wood", "wooden", "木", "木造"},
    {"metal", "金屬", "鐵", "steel", "鋼"},
    {"roof", "屋頂"},
    {"modern", "現代"},
    {"old", "老", "舊", "老街", "historic", "古蹟", "傳統"},
    {"japanese", "日式"},
    {"small", "小"},
    {"large", "big", "大"},
]
_SYN: Dict[str, set] = {}
for _g in SYNONYMS:
    for _w in _g:
        _SYN.setdefault(_w.lower(), set()).update(x.lower() for x in _g)

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_TOKEN = re.compile(r"[\s_\-/,，、()（）:;|]+")


def expand(term: str) -> set:
    t = term.lower().strip()
    return {t} | _SYN.get(t, set())


def tokens(text: Optional[str]) -> List[str]:
    return [t for t in _TOKEN.split((text or "").lower()) if t]


# --- index file ----------------------------------------------------------------------
def index_path(lib: dict) -> str:
    return os.path.join(lib["_root"], "index.json")


def thumbs_dir(lib: dict) -> str:
    return os.path.join(lib["_root"], "thumbs")


def thumb_path(lib: dict, asset_id: str) -> str:
    return os.path.join(thumbs_dir(lib), _safe_id(asset_id) + ".png")


def _safe_id(asset_id: str) -> str:
    if not re.match(r"^[A-Za-z0-9_.\-]+$", asset_id or "") or asset_id.startswith("."):
        raise ValueError("invalid asset id %r" % asset_id)
    return asset_id


def _load_index(lib: dict) -> dict:
    p = index_path(lib)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh).get("assets", {})
        except (OSError, ValueError):
            return {}
    return {}


def _save_index(lib: dict, assets_meta: dict) -> str:
    p = index_path(lib)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": INDEX_VERSION, "type": "geobox.asset_index", "assets": assets_meta}, fh,
                  ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


@contextlib.contextmanager
def _index_lock(lib: dict, timeout: float = 15.0, stale: float = 120.0):
    """Cross-process (and shared-drive) lock so farm workers don't lose index updates."""
    lock = index_path(lib) + ".lock"
    t0 = time.time()
    fd = None
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, ("%d@%s" % (os.getpid(), time.time())).encode())
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > stale:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            if time.time() - t0 > timeout:
                raise TimeoutError("asset index locked: %s" % lock)
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(lock)


# --- metadata extraction --------------------------------------------------------------
def _obj_stats(path: str) -> dict:
    v = tris = 0
    mats = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("v "):
                v += 1
            elif line.startswith("f "):
                tris += max(1, len(line.split()) - 3)
            elif line.startswith("usemtl "):
                m = line.split(None, 1)[1].strip()
                if m and m not in mats:
                    mats.append(m)
    return {"vertex_count": v, "tri_count": tris, "materials": mats}


def _glb_stats(path: str) -> dict:
    g = libmod._gltf_json(path)  # noqa: SLF001 - shared parser
    accs, meshes, nodes = g.get("accessors", []), g.get("meshes", []), g.get("nodes", [])
    uses: Dict[int, int] = {}
    for n in nodes:
        if "mesh" in n:
            uses[n["mesh"]] = uses.get(n["mesh"], 0) + 1
    v = tris = 0
    for mi, m in enumerate(meshes):
        k = uses.get(mi, 1)
        for p in m.get("primitives", []):
            nv = accs[p["attributes"]["POSITION"]].get("count", 0) if "POSITION" in p.get("attributes", {}) else 0
            mode = p.get("mode", 4)
            ni = accs[p["indices"]].get("count", nv) if "indices" in p else nv
            v += nv * k
            if mode == 4:
                tris += (ni // 3) * k
            elif mode in (5, 6):
                tris += max(0, ni - 2) * k
    mats = [m.get("name") or "material_%d" % i for i, m in enumerate(g.get("materials", []))]
    return {"vertex_count": v, "tri_count": tris, "materials": mats}


def _mesh_stats(path: Optional[str]) -> dict:
    if not path or not os.path.exists(path):
        return {}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".obj":
            return _obj_stats(path)
        if ext in (".glb", ".gltf"):
            return _glb_stats(path)
    except (OSError, ValueError, KeyError, IndexError, struct.error):
        return {}
    return {}


def _sha1(path: str, limit: int = 64 * 1024 * 1024) -> Optional[str]:
    try:
        if os.path.getsize(path) > limit:
            return None
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def dominant_colors(png_path: str, k: int = 4) -> Optional[List[dict]]:
    """Dominant colours of a thumbnail (transparent background ignored). Needs Pillow."""
    try:
        from PIL import Image  # optional dependency
    except ImportError:
        return None
    try:
        im = Image.open(png_path).convert("RGBA")
    except OSError:
        return None
    im.thumbnail((64, 64))
    px = [p[:3] for p in im.getdata() if p[3] > 128]
    if not px:
        return []
    solid = Image.new("RGB", (len(px), 1))
    solid.putdata(px)
    q = solid.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    counts = sorted(q.getcolors(), reverse=True)
    out = []
    for n, idx in counts:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        out.append({"hex": "#%02x%02x%02x" % (r, g, b), "frac": round(n / len(px), 3)})
    return out


def _source_file(lib: dict, a: dict) -> Optional[str]:
    f = a.get("files") or {}
    for k in ("web", "houdini", "usd", "houdini_normalized"):
        p = libmod.resolve(lib, f.get(k))
        if p and os.path.exists(p):
            return p
    return None


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def _keywords(a: dict, extra: Iterable[str] = ()) -> List[str]:
    words = [a.get("id", ""), a.get("label", ""), a.get("category", "")] + list(a.get("tags", []))
    words += _as_list(a.get("style")) + _as_list(a.get("era")) + list(extra)
    out = set()
    for w in words:
        for t in tokens(str(w)):
            if len(t) > 1 or _CJK.search(t):
                out.add(t)
        if _CJK.search(str(w)):
            out.add(str(w).lower())  # keep whole Chinese label for substring search
    return sorted(out)


def _word_hit(q: str, k: str) -> bool:
    """CJK: substring. Latin: whole word or prefix (>=3 chars) — so 'tree' doesn't hit 'street'."""
    if _CJK.search(q):
        return q in k
    return k == q or (len(q) >= 3 and k.startswith(q))


def _fingerprint(a: dict, src: Optional[str], thumb: Optional[str]) -> str:
    entry = json.dumps({k: v for k, v in a.items() if not k.startswith("_")}, sort_keys=True, ensure_ascii=False)
    parts = [hashlib.sha1(entry.encode("utf-8")).hexdigest()[:16]]
    for p in (src, thumb):
        if p and os.path.exists(p):
            st = os.stat(p)
            parts.append("%d:%d" % (int(st.st_mtime), st.st_size))
        else:
            parts.append("-")
    return "|".join(parts)


def index_asset(lib: dict, asset_id: str) -> dict:
    """Compute (not save) the index metadata of one asset."""
    a = libmod.by_id(lib)[asset_id]
    w, h, d = libmod.asset_size(a)
    src = _source_file(lib, a)
    stats = _mesh_stats(src)
    meta = {
        "label": a.get("label") or a["id"],
        "category": a.get("category"),
        "size": [round(w, 3), round(h, 3), round(d, 3)],
        "aspect": round(w / max(d, 1e-3), 3),
        "footprint": round(w * d, 3),
        "volume": round(w * h * d, 3),
        "tri_count": stats.get("tri_count"),
        "vertex_count": stats.get("vertex_count"),
        "materials": stats.get("materials") or sorted((a.get("materials") or {}).keys()),
        "style": _as_list(a.get("style")),
        "era": _as_list(a.get("era")),
        "lods": a.get("lods") or ([a["lod"]] if "lod" in a else []),
        "source": os.path.relpath(src, lib["_root"]).replace(os.sep, "/") if src else None,
        "source_mtime": os.path.getmtime(src) if src else None,
        "source_size": os.path.getsize(src) if src else None,
        "sha1": _sha1(src) if src else None,
        "indexed_at": round(time.time(), 1),
    }
    meta["keywords"] = _keywords(a, meta["materials"])
    tp = thumb_path(lib, asset_id) if re.match(r"^[A-Za-z0-9_.\-]+$", asset_id) else None
    if tp and os.path.exists(tp):
        meta["thumbnail"] = "thumbs/%s.png" % asset_id
        meta["thumb_mtime"] = int(os.path.getmtime(tp))
        cols = dominant_colors(tp)
        if cols is not None:
            meta["colors"] = cols
    meta["fingerprint"] = _fingerprint(a, src, tp if tp and os.path.exists(tp) else None)
    return meta


def rebuild_index(lib: dict, asset_ids: Optional[Iterable[str]] = None, force: bool = False) -> dict:
    """Incremental: unchanged assets (same manifest entry, source and thumbnail
    mtime/size) keep their metadata unless force=True. Returns the full index."""
    assets = libmod.by_id(lib)
    ids = list(asset_ids) if asset_ids is not None else list(assets)
    with _index_lock(lib):
        meta = _load_index(lib)
        for aid in ids:
            a = assets.get(aid)
            if a is None:
                meta.pop(aid, None)
                continue
            old = meta.get(aid) or {}
            if not force and old.get("fingerprint"):
                src = _source_file(lib, a)
                tp = thumb_path(lib, aid) if re.match(r"^[A-Za-z0-9_.\-]+$", aid) else None
                if old["fingerprint"] == _fingerprint(a, src, tp if tp and os.path.exists(tp) else None):
                    continue
            meta[aid] = index_asset(lib, aid)
        meta = {k: v for k, v in meta.items() if k in assets}
        _save_index(lib, meta)
    return meta


def save_thumbnail(lib: dict, asset_id: str, png: bytes) -> dict:
    """Store a PNG thumbnail and re-index that asset. Returns its metadata."""
    if asset_id not in libmod.by_id(lib):
        raise KeyError(asset_id)
    if not png.startswith(PNG_SIG) or len(png) > MAX_THUMB_BYTES:
        raise ValueError("expected a PNG under %d bytes" % MAX_THUMB_BYTES)
    os.makedirs(thumbs_dir(lib), exist_ok=True)
    p = thumb_path(lib, asset_id)
    tmp = p + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(png)
    os.replace(tmp, p)
    return rebuild_index(lib, [asset_id], force=True)[asset_id]


# --- search ---------------------------------------------------------------------------
def _family(asset_id: str) -> str:
    return re.sub(r"(?:[_\-](?:[a-z]|v?\d+|lod\d+)|\d+)$", "", asset_id.lower())


class AssetIndex:
    def __init__(self, lib: dict, meta: Optional[dict] = None):
        self.lib = lib
        self.meta = meta if meta is not None else _load_index(lib)
        self.assets = [a for a in lib.get("assets", []) if a.get("bounds")]

    @classmethod
    def load(cls, lib: dict) -> "AssetIndex":
        return cls(lib)

    # -- helpers ------------------------------------------------------------------
    def _kw(self, a: dict) -> List[str]:
        m = self.meta.get(a["id"]) or {}
        return m.get("keywords") or _keywords(a)

    def _attr_match(self, a: dict, field: str, wanted: Optional[str]) -> bool:
        if not wanted:
            return True
        vals = {v.lower() for v in _as_list(a.get(field))} | {t.lower() for t in a.get("tags", [])}
        want = expand(wanted)
        return any(w in v or v in w for w in want for v in vals if v)

    @staticmethod
    def _lod_ok(a: dict, meta: dict, lod) -> bool:
        if lod is None or lod == "":
            return True
        lods = [str(x) for x in (meta.get("lods") or a.get("lods") or ([a["lod"]] if "lod" in a else []))]
        tags = {t.lower() for t in a.get("tags", [])}
        return str(lod) in lods or ("lod%s" % lod) in tags

    # -- main entry -----------------------------------------------------------------
    def search(self, category: Optional[str] = None, size: Optional[Sequence[float]] = None,
               tags: Optional[Sequence[str]] = None, text: Optional[str] = None, fit: Optional[str] = None,
               limit: int = 10, exclude: Sequence[str] = (), *, style: Optional[str] = None,
               era: Optional[str] = None, lod=None, max_tris: Optional[int] = None,
               diversity: float = 0.0, require_text: bool = True) -> List[Dict]:
        want_tags = set()
        for t in tags or []:
            want_tags |= expand(str(t))
        words = tokens(text)
        hits = []
        for a in self.assets:
            if category and a.get("category") != category:
                continue
            if a["id"] in exclude or a.get("weight", 1.0) <= 0:
                continue
            meta = self.meta.get(a["id"]) or {}
            if max_tris and meta.get("tri_count") and meta["tri_count"] > int(max_tris):
                continue
            if not (self._attr_match(a, "style", style) and self._attr_match(a, "era", era) and self._lod_ok(a, meta, lod)):
                continue

            reasons: List[str] = []
            pen: Dict[str, float] = {}
            aw, ah, ad = libmod.asset_size(a)
            mode = fit or a.get("fit", "stretch")
            scale = [1.0, 1.0, 1.0]
            real = [aw, ah, ad]
            if size:
                w, h, d = [max(float(v), 1e-3) for v in size]
                if mode == "uniform":
                    s = min(w / aw, d / ad)
                    scale = [s, s, s]
                    pen["size"] = abs(math.log(h / (ah * s))) * 1.0 + abs(math.log(w / aw) - math.log(d / ad)) * 0.5
                elif mode == "none":
                    pen["size"] = abs(math.log(h / ah)) * 1.5 + abs(math.log(w / aw)) + abs(math.log(d / ad))
                else:  # stretch
                    scale = [w / aw, h / ah, d / ad]
                    pen["size"] = abs(math.log(h / ah)) * 1.5 + abs(math.log(w / aw)) + abs(math.log(d / ad))
                    # footprint proportion: a long slab shouldn't become a square tower
                    pen["aspect"] = 0.7 * abs(math.log(w / d) - math.log(aw / ad))
                lo, hi = a.get("height_range", [0, 1e9])
                if not lo <= h <= hi:
                    pen["height_range"] = 3.0 + abs(math.log(h / (lo if h < lo else hi)))
                    reasons.append("高度 %.1f m 超出適用範圍 %s" % (h, [lo, hi]))
                slo, shi = a.get("scale_range", [0.0, 1e9])
                clamped = [min(max(v, slo), shi) for v in scale]
                over = sum(abs(math.log(max(v, 1e-6) / max(c, 1e-6))) for v, c in zip(scale, clamped))
                if over > 1e-6:
                    pen["scale_range"] = 2.0 * over
                    reasons.append("需縮放 %s 超出 scale_range %s" % ("×".join("%.2f" % v for v in scale), [slo, shi]))
                if mode != "none":
                    real = [aw * clamped[0], ah * clamped[1], ad * clamped[2]]
                if pen.get("size", 0) < 0.25:
                    reasons.append("尺寸吻合")
            atags = set()
            for t in a.get("tags", []):
                atags |= expand(t)
            if want_tags:
                common = want_tags & atags
                if common:
                    own = sorted(t for t in common if t in {x.lower() for x in a.get("tags", [])})
                    reasons.append("tags: " + ", ".join((own or sorted(common))[:4]))
                else:
                    pen["tags"] = 2.0
            if words:
                kw = self._kw(a)
                blob = " ".join(kw)
                found = []
                for wd in words:
                    if any(_word_hit(alt, k) for alt in expand(wd) for k in kw) or (_CJK.search(wd) and wd in blob):
                        found.append(wd)
                if not found and require_text:
                    continue
                miss = len(words) - len(found)
                if miss:
                    pen["text"] = 1.5 * miss
                if found:
                    reasons.append("文字: " + ", ".join(found))
            penalty = sum(pen.values())
            weight = a.get("weight", 1.0)
            score = math.exp(-penalty / 2.0) * min(1.0, 0.5 + 0.5 * weight)
            hits.append({"id": a["id"], "asset": a, "score": round(score, 4),
                         "scale": [round(v, 4) for v in scale], "reasons": reasons,
                         "real_size": [round(v, 3) for v in real], "fit": mode,
                         "penalties": {k: round(v, 3) for k, v in pen.items()}})
        hits.sort(key=lambda r: (-r["score"], r["id"]))
        if diversity > 0 and len(hits) > 1:
            hits = self._diversify(hits, float(diversity))
        return hits[:limit]

    @staticmethod
    def _diversify(hits: List[Dict], diversity: float) -> List[Dict]:
        """Greedy MMR-style re-rank: repeat families lose `diversity` of their score per repeat."""
        pool = list(hits)
        out: List[Dict] = []
        seen: Dict[str, int] = {}
        while pool:
            best = max(pool, key=lambda r: r["score"] * (1 - diversity) ** seen.get(_family(r["id"]), 0))
            pool.remove(best)
            fam = _family(best["id"])
            if seen.get(fam):
                best = dict(best, reasons=best["reasons"] + ["多樣性: 同系列第 %d 個" % (seen[fam] + 1)])
            seen[fam] = seen.get(fam, 0) + 1
            out.append(best)
        return out


# --- web helpers ----------------------------------------------------------------------
def thumb_url(asset_id: str, meta: Optional[dict]) -> Optional[str]:
    if not meta or not meta.get("thumbnail"):
        return None
    return "/api/library/thumbs/%s?v=%s" % (urllib.parse.quote(asset_id), meta.get("thumb_mtime", 0))


def asset_for_web(a: dict, meta: Optional[dict]) -> dict:
    out = {k: a.get(k) for k in ("id", "label", "category", "tags", "bounds", "fit", "scale_range",
                                  "height_range", "weight", "style", "era", "materials") if k in a}
    web = (a.get("files") or {}).get("web")
    if web:
        out["web_url"] = "/library/%s/%s" % (urllib.parse.quote(a["id"]), urllib.parse.quote(os.path.basename(web)))
    out["size"] = [round(v, 3) for v in libmod.asset_size(a)]
    out["thumbnail_url"] = thumb_url(a["id"], meta)
    if meta:
        out["tri_count"] = meta.get("tri_count")
        out["colors"] = meta.get("colors")
    return out


def hits_for_web(idx: AssetIndex, hits: List[Dict]) -> List[Dict]:
    return [{**{k: v for k, v in h.items() if k != "asset"}, "asset": asset_for_web(h["asset"], idx.meta.get(h["id"]))}
            for h in hits]


def query_from_params(q: Dict[str, List[str]]) -> dict:
    """URL query (parse_qs) -> search kwargs."""
    g = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731
    kw: dict = {}
    if g("category"):
        kw["category"] = g("category")
    if g("size"):
        kw["size"] = [float(v) for v in g("size").split(",")][:3]
    elif g("w") and g("h") and g("d"):
        kw["size"] = [float(g("w")), float(g("h")), float(g("d"))]
    if g("tags"):
        kw["tags"] = [t for t in re.split(r"[,\s]+", g("tags")) if t]
    if g("q"):
        kw["text"] = g("q")
    for k in ("fit", "style", "era", "lod"):
        if g(k):
            kw[k] = g(k)
    if g("max_tris"):
        kw["max_tris"] = int(float(g("max_tris")))
    if g("diversity"):
        kw["diversity"] = float(g("diversity"))
    if g("exclude"):
        kw["exclude"] = [t for t in g("exclude").split(",") if t]
    kw["limit"] = max(1, min(int(g("limit", "20")), 500))
    return kw


# --- match: greybox block / instance -> query -----------------------------------------
def _project_bits(name: str):
    from . import project
    d = project.project_dir(name)
    scene_p = os.path.join(d, "scene.json")
    if not os.path.exists(scene_p):
        raise FileNotFoundError("project %s has no scene.json" % name)
    scene = project.read_json(scene_p)
    lp = os.path.join(d, scene["library"]) if scene.get("library") else None
    lib = libmod.load(lp if lp and os.path.exists(lp) else libmod.DEFAULT_LIBRARY)
    layout_p = os.path.join(d, scene.get("layout_file", "layout.json"))
    layout = project.read_json(layout_p) if os.path.exists(layout_p) else {}
    return scene, lib, layout


def _kind_tags(scene: dict, kind: Optional[str]) -> List[str]:
    if not kind:
        return []
    rules = (scene.get("rules") or {}).get("buildings") or {}
    kt = rules.get("kind_tags")
    if kt is None:
        from .scatter import load_rules
        kt = load_rules().get("buildings", {}).get("kind_tags", {})
    return list(kt.get(kind) or kt.get("*") or [kind])


def match_query(scene: dict, lib: dict, layout: dict, ref: str) -> dict:
    """Build search kwargs for a greybox block id ("gb:...") or an instance id."""
    assets = libmod.by_id(lib)
    blocks = {b["id"]: b for b in scene.get("greybox", []) or []}
    insts = {i["id"]: i for i in scene.get("instances", [])}
    q: dict = {"ref": ref, "current": None}
    block = blocks.get(ref)
    inst = insts.get(ref)
    if inst is None and block is not None:
        inst = next((i for i in scene.get("instances", []) if i.get("block") == ref), None)
    if block is None and inst is not None and inst.get("block"):
        block = blocks.get(inst["block"])
    if block is not None:
        q.update(category="building", size=list(block["size"]), kind=block.get("kind"),
                 tags=list(block.get("tags") or []) or _kind_tags(scene, block.get("kind")))
        if block.get("asset_lock"):
            q["current"] = block["asset_lock"]
    if inst is not None:
        q["current"] = q.get("current") or inst.get("asset")
        if block is None:
            a = assets.get(inst.get("asset"), {})
            q["category"] = inst.get("category") or a.get("category")
            if inst.get("target"):
                q["size"] = list(inst["target"]["size"])
            else:
                w, h, d = libmod.asset_size(a) if a else (1, 1, 1)
                s = inst.get("scale", [1, 1, 1])
                q["size"] = [w * s[0], h * s[1], d * s[2]]
            kind = None
            if q["category"] == "building" and inst.get("feature"):
                kind = next((b.get("kind") for b in layout.get("buildings", []) if b["id"] == inst["feature"]), None)
            q["kind"] = kind
            q["tags"] = _kind_tags(scene, kind) if kind else [t for t in a.get("tags", []) if t not in ("any",)][:3]
            if q["category"] in ("tree", "prop"):
                q["fit"] = "uniform" if q["category"] == "tree" else "none"
    if "size" not in q:
        raise KeyError("no greybox block or instance %r" % ref)
    return q


# --- HTTP routes (registered by geobox/server.py EXTENSIONS) ------------------------------
def _default_lib(req=None):
    return libmod.load(libmod.DEFAULT_LIBRARY)


def _r_search(req):
    lib = _default_lib()
    idx = AssetIndex.load(lib)
    try:
        kw = query_from_params(req.query)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    hits = idx.search(**kw)
    return {"ok": True, "query": {k: v for k, v in kw.items()}, "results": hits_for_web(idx, hits)}


def _r_index(req):
    lib = _default_lib()
    meta = _load_index(lib)
    return {"ok": True, "assets": meta, "count": len(meta)}


def _r_reindex(req):
    lib = _default_lib()
    body = req.json() if req.body() else {}
    meta = rebuild_index(lib, body.get("ids"), force=bool(body.get("force")))
    return {"ok": True, "count": len(meta)}


def _r_put_thumb(req):
    lib = _default_lib()
    aid = urllib.parse.unquote(req.match.group(1))
    try:
        meta = save_thumbnail(lib, aid, req.body())
    except KeyError:
        return 404, {"ok": False, "error": "unknown asset %s" % aid}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return {"ok": True, "id": aid, "thumbnail_url": thumb_url(aid, meta), "colors": meta.get("colors")}


def _r_get_thumb(req):
    lib = _default_lib()
    aid = urllib.parse.unquote(req.match.group(1))
    try:
        p = thumb_path(lib, aid)
    except ValueError:
        return 400, {"ok": False, "error": "bad id"}
    if not os.path.exists(p):
        return 404, {"ok": False, "error": "no thumbnail"}
    with open(p, "rb") as fh:
        return 200, fh.read(), "image/png"


def _r_match(req):
    g = lambda k, d=None: (req.query.get(k) or [d])[0]  # noqa: E731
    name, ref = g("project"), g("block") or g("instance")
    lib = None
    scene: dict = {}
    q: dict = {}
    if name:
        try:
            scene, lib, _layout = _project_bits(name)
            if ref:
                q = match_query(scene, lib, _layout, ref)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            if not (g("w") and g("h") and g("d")):
                return 404, {"ok": False, "error": str(exc)}
    lib = lib or _default_lib()
    # client-side overrides (unsaved edits): w/h/d, category, kind, tags, q, fit
    if g("w") and g("h") and g("d"):
        q["size"] = [float(g("w")), float(g("h")), float(g("d"))]
    if g("category"):
        q["category"] = g("category")
    if g("kind"):
        q["kind"] = g("kind")
        if not g("tags"):
            q["tags"] = _kind_tags(scene, g("kind"))
    if g("tags"):
        q["tags"] = [t for t in re.split(r"[,\s]+", g("tags")) if t]
    if g("fit"):
        q["fit"] = g("fit")
    if "size" not in q:
        return 400, {"ok": False, "error": "need project+block or w,h,d"}
    idx = AssetIndex.load(lib)
    kw = {k: q[k] for k in ("category", "size", "tags", "fit") if q.get(k)}
    if g("q"):
        kw["text"] = g("q")
    kw["limit"] = max(1, min(int(g("limit", "12")), 100))
    hits = idx.search(**kw)
    return {"ok": True, "query": {k: v for k, v in q.items()}, "results": hits_for_web(idx, hits)}


ROUTES = [
    ("GET", r"^/api/library/search$", _r_search),
    ("GET", r"^/api/library/index$", _r_index),
    ("POST", r"^/api/library/reindex$", _r_reindex),
    ("PUT", r"^/api/library/thumbs/([^/]+?)(?:\.png)?$", _r_put_thumb),
    ("GET", r"^/api/library/thumbs/([^/]+?)(?:\.png)?$", _r_get_thumb),
    ("GET", r"^/api/library/match$", _r_match),
]


# --- CLI (registered by geobox/__main__.py CLI_EXTENSIONS) ---------------------------------
def _cli_search(args):
    lib = libmod.load(args.library)
    idx = AssetIndex.load(lib)
    kw = {"category": args.category, "size": args.size, "tags": args.tags, "text": args.q, "fit": args.fit,
          "limit": args.limit, "style": args.style, "era": args.era, "lod": args.lod, "max_tris": args.max_tris,
          "diversity": args.diversity}
    hits = idx.search(**{k: v for k, v in kw.items() if v not in (None, [], "")})
    if args.json:
        print(json.dumps(hits_for_web(idx, hits), ensure_ascii=False, indent=1))
        return 0
    if not hits:
        print("(no match)")
    for i, h in enumerate(hits, 1):
        rs = "×".join("%.1f" % v for v in h["real_size"])
        print("%2d. %-22s %.3f  %-9s %s m  %s" % (i, h["id"], h["score"], h["asset"].get("category"), rs,
                                                "; ".join(h["reasons"])))
    return 0


def _cli_reindex(args):
    lib = libmod.load(args.library)
    meta = rebuild_index(lib, args.ids or None, force=args.force)
    thumbs = sum(1 for m in meta.values() if m.get("thumbnail"))
    print("[index] %d assets (%d with thumbnails) -> %s" % (len(meta), thumbs, index_path(lib)))
    return 0


def add_cli(sub):
    s = sub.add_parser("search", help="素材檢索: rank library assets for a size / tags / text query")
    s.add_argument("--category", choices=["building", "tree", "prop"])
    s.add_argument("--size", type=float, nargs=3, metavar=("W", "H", "D"), help="target real size in metres")
    s.add_argument("--tags", nargs="*", help="tags / OSM kinds (synonyms expanded, 中文 ok)")
    s.add_argument("-q", help="text query (id / label / tags / materials, 中文 ok)")
    s.add_argument("--fit", choices=["stretch", "uniform", "none"])
    s.add_argument("--style")
    s.add_argument("--era")
    s.add_argument("--lod")
    s.add_argument("--max-tris", type=int)
    s.add_argument("--diversity", type=float, default=0.0)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.add_argument("--library", default=libmod.DEFAULT_LIBRARY)
    s.set_defaults(func=_cli_search)
    r = sub.add_parser("reindex", help="rebuild assets/index.json (incremental; --force for all)")
    r.add_argument("--ids", nargs="*")
    r.add_argument("--force", action="store_true")
    r.add_argument("--library", default=libmod.DEFAULT_LIBRARY)
    r.set_defaults(func=_cli_reindex)
