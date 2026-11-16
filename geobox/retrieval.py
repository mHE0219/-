"""Asset retrieval (素材檢索): rank library assets for a query.

CONTRACT (used by scatter / greybox / farm / web UI — keep signatures stable):

    idx = AssetIndex.load(lib)                 # lib = geobox.library.load(...)
    hits = idx.search(category="building", size=[w, h, d], tags=["apartments"],
                      text="brick", fit="stretch", limit=10, exclude=())
    # -> [{"id", "asset": entry, "score": 0..1 (higher = better),
    #      "scale": [sx, sy, sz] to reach `size`, "reasons": [str, ...]}]

    meta = index_asset(lib, asset_id)          # (re)compute index metadata of one asset
    index = rebuild_index(lib)                 # all assets -> assets/index.json

Index metadata (assets/index.json, keyed by asset id) is produced by the
ingest step (locally or on the farm) and may contain: size, aspect, volume,
tri_count, thumbnail (relative path), colors, keywords, mtime, embedding.

This file is the baseline implementation; the retrieval feature extends it.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence

from . import library as libmod

INDEX_VERSION = 1


def index_path(lib: dict) -> str:
    return os.path.join(lib["_root"], "index.json")


def _load_index(lib: dict) -> dict:
    p = index_path(lib)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("assets", {})
        except (OSError, ValueError):
            return {}
    return {}


def _save_index(lib: dict, assets_meta: dict) -> str:
    p = index_path(lib)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": INDEX_VERSION, "type": "geobox.asset_index", "assets": assets_meta}, fh,
                  ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def _keywords(a: dict) -> List[str]:
    words = [a.get("id", ""), a.get("label", ""), a.get("category", "")] + list(a.get("tags", []))
    out = set()
    for w in words:
        for t in re.split(r"[\s_\-/,()]+", str(w).lower()):
            if t:
                out.add(t)
    return sorted(out)


def _tri_count(path: Optional[str]) -> Optional[int]:
    if not path or not os.path.exists(path) or not path.lower().endswith(".obj"):
        return None
    n = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("f "):
                n += max(1, len(line.split()) - 3)
    return n


def index_asset(lib: dict, asset_id: str) -> dict:
    a = libmod.by_id(lib)[asset_id]
    w, h, d = libmod.asset_size(a)
    src = libmod.resolve(lib, (a.get("files") or {}).get("web") or (a.get("files") or {}).get("houdini"))
    meta = {
        "size": [round(w, 3), round(h, 3), round(d, 3)],
        "aspect": round(w / max(d, 1e-3), 3),
        "volume": round(w * h * d, 3),
        "keywords": _keywords(a),
        "tri_count": _tri_count(src),
        "mtime": os.path.getmtime(src) if src and os.path.exists(src) else None,
    }
    thumb = os.path.join(lib["_root"], "thumbs", asset_id + ".png")
    if os.path.exists(thumb):
        meta["thumbnail"] = "thumbs/%s.png" % asset_id
    return meta


def rebuild_index(lib: dict, asset_ids: Optional[Iterable[str]] = None) -> dict:
    meta = _load_index(lib)
    ids = list(asset_ids) if asset_ids is not None else [a["id"] for a in lib.get("assets", [])]
    for aid in ids:
        meta[aid] = {**meta.get(aid, {}), **index_asset(lib, aid)}
    known = {a["id"] for a in lib.get("assets", [])}
    meta = {k: v for k, v in meta.items() if k in known}
    _save_index(lib, meta)
    return meta


class AssetIndex:
    def __init__(self, lib: dict, meta: Optional[dict] = None):
        self.lib = lib
        self.meta = meta if meta is not None else _load_index(lib)
        self.assets = [a for a in lib.get("assets", []) if a.get("bounds")]

    @classmethod
    def load(cls, lib: dict) -> "AssetIndex":
        return cls(lib)

    def search(self, category: Optional[str] = None, size: Optional[Sequence[float]] = None,
               tags: Optional[Sequence[str]] = None, text: Optional[str] = None, fit: Optional[str] = None,
               limit: int = 10, exclude: Sequence[str] = ()) -> List[Dict]:
        tags = [t.lower() for t in (tags or [])]
        words = [t for t in re.split(r"\s+", (text or "").lower()) if t]
        hits = []
        for a in self.assets:
            if category and a.get("category") != category:
                continue
            if a["id"] in exclude or a.get("weight", 1.0) <= 0:
                continue
            reasons, penalty = [], 0.0
            aw, ah, ad = libmod.asset_size(a)
            scale = [1.0, 1.0, 1.0]
            if size:
                w, h, d = [max(float(v), 1e-3) for v in size]
                penalty += abs(math.log(h / ah)) * 1.5 + abs(math.log(w / aw)) + abs(math.log(d / ad))
                lo, hi = a.get("height_range", [0, 1e9])
                if not lo <= h <= hi:
                    penalty += 3.0 + abs(math.log(h / (lo if h < lo else hi)))
                    reasons.append("height %.1f outside %s" % (h, [lo, hi]))
                mode = fit or a.get("fit", "stretch")
                if mode == "uniform":
                    s = min(w / aw, d / ad)
                    scale = [s, s, s]
                elif mode != "none":
                    scale = [w / aw, h / ah, d / ad]
            atags = {t.lower() for t in a.get("tags", [])}
            if tags:
                common = atags & set(tags)
                if common:
                    reasons.append("tags " + ",".join(sorted(common)))
                else:
                    penalty += 2.0
            if words:
                kw = set((self.meta.get(a["id"]) or {}).get("keywords") or _keywords(a))
                found = [w for w in words if any(w in k for k in kw)]
                penalty += 1.5 * (len(words) - len(found))
                if found:
                    reasons.append("text " + ",".join(found))
            score = math.exp(-penalty / 2.0) * min(1.0, 0.5 + 0.5 * a.get("weight", 1.0))
            hits.append({"id": a["id"], "asset": a, "score": round(score, 4),
                         "scale": [round(v, 4) for v in scale], "reasons": reasons})
        hits.sort(key=lambda r: -r["score"])
        return hits[:limit]
