# 素材檢索 (Asset Retrieval)

灰盒 (greybox) → 遊戲素材的關鍵一步：**每個灰盒 / 物件要換成素材庫裡「尺寸與用途最合適」的素材**。
同一套檢索同時被 scatter（自動 art pass）、WebGL 編輯器（搜尋 / 匹配面板）、CLI、farm 的 ingest 工作使用。

```
 素材檔 ──ingest（本機 / web 縮圖上傳 / farm job）──► assets/index.json + assets/thumbs/<id>.png
                                                            │
 greybox block / instance（真實尺寸 W×H×D、用途 kind、tags）──► AssetIndex.search() ──► 排序結果 + 原因
                                                            │
                        scatter 建築 pass、編輯器「匹配素材」、`python -m geobox search`
```

## 索引 `assets/index.json`
`{"version": 2, "type": "geobox.asset_index", "assets": {<id>: {...}}}`，每個素材：

| 欄位 | 說明 |
|---|---|
| `size` `aspect` `footprint` `volume` | 標準空間（公尺）的尺寸、W/D 比、底面積、體積 |
| `tri_count` `vertex_count` | 面數 / 頂點數（OBJ、glTF/GLB；GLB 以 accessor count × 節點引用數計） |
| `materials` | 材質名稱（OBJ `usemtl`、glTF materials）— 也進關鍵字，可搜「brick」「玻璃」 |
| `keywords` `label` | id/label/tags/類別/材質/style/era 拆詞（小寫），中文 label 保留整串做子字串比對 |
| `style` `era` `lods` | 素材條目上的選用欄位（manifest 可寫 `"style": "japanese"`, `"era": "1970s"`, `"lods": [0,1,2]`） |
| `thumbnail` `thumb_mtime` | `thumbs/<id>.png`（256², 透明背景） |
| `colors` | 縮圖主色 `[{hex, frac}]`（需要 Pillow；沒有就略過） |
| `source` `source_mtime` `source_size` `sha1` | 來源檔資訊 |
| `fingerprint` | manifest 條目雜湊 + 來源檔 + 縮圖的 mtime/size → **增量重建**：沒變的素材不重算 |

寫入時有跨程序鎖（`index.json.lock`，共享磁碟也適用），多台 farm worker 同時 ingest 不會互相覆蓋。

## 評分（score 0..1，越高越好）
`score = exp(-Σpenalty / 2) × 權重係數(weight)`，每一項都會出現在 `penalties` 與 `reasons`（中文說明）：

| penalty | 規則 |
|---|---|
| `size` | 尺寸對數誤差：`1.5·|ln(h/ah)| + |ln(w/aw)| + |ln(d/ad)|`（fit=uniform 時改以等比縮放後的高度誤差 + 長寬比） |
| `aspect` | footprint 長寬比差異 ×0.7（避免長條建築被拉成方塔） |
| `height_range` | 目標高度不在素材的 `height_range` → 3 + 超出量 |
| `scale_range` | 需要的縮放超出素材 `scale_range` → 2×超出量（`real_size` 為夾住後的實際尺寸） |
| `tags` | 有指定 tags/用途但完全對不上 → 2.0（同義詞展開後比對） |
| `text` | 文字查詢每個沒命中的詞 1.5；一個都沒命中則排除 |

**同義詞**（`SYNONYMS`，可自行擴充）：`住宅/residential`、`公寓/大樓/apartments`、`透天/獨棟/house`、`辦公/office`、`廠房/倉庫/industrial`、
`高樓/摩天/tower`、`棕櫚/椰子/palm`、`闊葉`、`針葉/松`、`灌木/bush`、`路燈/lamp`、`磚/brick`、`玻璃/glass`、`日式/japanese`…
英文詞以「整字或 ≥3 字元前綴」比對（`tree` 不會命中 `street`），中文以子字串比對。

**硬性過濾**（選用）：`category`、`style`、`era`、`lod`、`max_tris`（面數預算）、`exclude`。
**多樣性** `diversity` 0..1：MMR 式重排，同系列（`tree_broadleaf_a/_b`）第 n 個分數乘 `(1-diversity)^n`。

## Python API（穩定介面）
```python
from geobox import library, retrieval
lib = library.load()
idx = retrieval.AssetIndex.load(lib)
hits = idx.search(category="building", size=[18, 24, 14], tags=["apartments"], text="磚", fit="stretch",
                  limit=10, exclude=(), style=None, era=None, lod=None, max_tris=None, diversity=0.0)
# [{id, asset, score, scale, reasons, real_size, fit, penalties}]
retrieval.index_asset(lib, "tree_palm_a")           # 單一素材的 metadata（不寫檔）
retrieval.rebuild_index(lib, ["tree_palm_a"])       # 增量更新並寫入 index.json（force=True 全部重算）
retrieval.save_thumbnail(lib, "tree_palm_a", png)   # 存縮圖 + 重新索引
retrieval.match_query(scene, lib, layout, "gb:w123")  # greybox block / instance id → 檢索條件
```

## HTTP API（`python -m geobox serve`）
| Method | Path | 說明 |
|---|---|---|
| GET | `/api/library/search?category=&w=&h=&d=&tags=a,b&q=&fit=&style=&era=&lod=&max_tris=&diversity=&limit=` | 檢索；結果含 `asset.thumbnail_url`、`web_url` |
| GET | `/api/library/match?project=<name>&block=<gb:… 或 instance id>[&w&h&d&category&kind&tags&fit&q]` | 為 greybox block / 物件找素材；可帶未儲存的尺寸覆寫 |
| GET | `/api/library/index` | 整份索引 |
| POST | `/api/library/reindex` | body `{"ids": [...]?, "force": bool?}` |
| PUT | `/api/library/thumbs/<id>` | PNG 位元組 → `assets/thumbs/<id>.png` 並重新索引 |
| GET | `/api/library/thumbs/<id>` | 取縮圖 |

## CLI
```bash
python -m geobox search --category building --size 18 24 14 --tags apartments -q 磚
python -m geobox search -q 棕櫚 --limit 3 --json
python -m geobox search --category tree --diversity 0.7 --limit 6
python -m geobox reindex            # 增量
python -m geobox reindex --force    # 全部重算
```

## WebGL 編輯器
* 左側 **素材檢索**：搜尋框（中英文、同義詞）、類別、尺寸 W/H/D、**⇢ 依選取**（帶入選取物件的真實尺寸與類別）、縮圖格線。
  點縮圖 = 選為放置 / 筆刷素材（Shift 多選），取代原本的純文字清單。
* **🖼 產生縮圖**：在瀏覽器內以中性棚燈渲染每個素材（正面 +Z 朝鏡頭、3/4 視角），上傳到 server；載入素材庫時會自動補齊缺少的縮圖
  （`localStorage.geobox.autoThumbs = '0'` 可關閉）。Shift+點擊 = 全部重做。
* 右側 **匹配素材 Match**：選取一個建築 / 樹 / 道具（或 greybox block `gb:*`）時，列出排序後的候選素材、分數、換上後的真實尺寸與原因；
  點一下即替換（保持真實尺寸、自動 locked、可復原）。對 greybox block 會寫入 `asset_lock`，re-scatter 時固定使用該素材。

## 與 Farm 的結合
Farm 的素材 ingest 工作（`geobox/farm`、Houdini PDG）對每個新 / 變更的素材：量測 + 標準化（`library.normalize_entry` 或 Houdini `asset_prep`）
→ 可選渲染縮圖（Houdini OpenGL ROP 寫到 `assets/thumbs/<id>.png`）→ `retrieval.rebuild_index(lib, [id])`。
索引寫入有鎖，所以多個 worker 可並行；之後所有 box build / re-scatter（不論在本機或 farm 上）都用同一份索引檢索。
沒有 farm 時，本機 `python -m geobox reindex` + 編輯器自動縮圖就能得到相同結果。

## 限制
* 尚無影像 / 形狀 embedding（視覺相似度）；`index.json` 預留欄位，可由 farm 加算（例如 CLIP 向量）後在 `search()` 加一項 penalty。
* 縮圖由瀏覽器渲染：FBX/BGEO 等 web 讀不到的格式，請在 Houdini（farm）產生。
* 面數只計 OBJ / glTF；其他格式由 Houdini ingest 補上。
