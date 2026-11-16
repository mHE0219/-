# Pipeline 細節：地圖 → Box → Scatter → WebGL / Houdini

```
 Google Maps 網址 / 經緯度 + box 尺寸(公尺)
            │  geobox/geo.py   (WGS84 → ECEF → ENU，真實公尺；不是 Web Mercator)
            ▼
 ① fetch    geobox/osm.py      Overpass API → osm.json（快取）
            geobox/terrain.py  AWS Terrain Tiles → terrain.json（選用）
            ▼
 ② extract  geobox/layout.py   → layout.json  建築 footprint/高度、樹、道路、綠地、水域（公尺座標）
            ▼
 ③ scatter  geobox/scatter.py  layout + 素材庫 + 規則 → instances
            （deterministic id；保留手動/鎖定的編輯）
            ▼
 ④ scene    projects/<name>/scene.json   ← 唯一的「可編輯」交換格式
            projects/<name>/scene.usda   ← Solaris (PointInstancer)
            ▼
 ⑤ edit     WebGL 編輯器 (viewer/)  ⇄  Houdini (geobox_houdini)
```

## ① 為什麼用 OSM 而不是直接抓 Google 的資料？
* Google Maps Platform 條款**不允許**把 Google 地圖內容（建築、道路、3D mesh）抽取出來做成其他資料或在別的工具裡使用。
* OpenStreetMap 有我們 scatter 真正需要的**語意資料**：每棟建築的輪廓、樓層數/高度、用途；路樹、行道樹、公園、森林、水域、道路寬度。授權 ODbL，只需標示「© OpenStreetMap contributors」。
* Google 的部分保留為**視覺參考**：在 WebGL 編輯器開啟「Google 3D Tiles 參考層」（自己的 Map Tiles API key），會被裁切在 box 內，可以和你的素材版本疊圖比對，但不匯出。
* Google Maps 網址只用來取「中心點與縮放」：`@lat,lon,17z`、`!3d..!4d..`（地標釘選點）、`?q=lat,lon` 都支援，縮放會換算成建議的 box 尺寸。

## 比例尺（「比例還是對的」）
* Box 的局部座標是 box 中心點的切平面 (ENU)：**+X 東、+Y 上、-Z 北，1 unit = 1 m**。
* 用精確的 WGS84 → ECEF → ENU 轉換，1 km 範圍內誤差 < 1 cm。網頁地圖的 Web Mercator 在台北會把距離放大約 10%（1/cos 25°），這裡不會。
* 驗證工具：編輯器的 📏 量測工具、10 m / 100 m 格線、1.8 m 人形比例尺；`tests/test_pipeline.py::GeoTest` 用已知經緯度距離驗證。

## ② layout.json
| 欄位 | 內容 |
|---|---|
| `box` | `center`、`size [W,D]`、`rect [x0,z0,x1,z1]`、`bbox_latlon` |
| `origin` | `lat, lon, elevation`（有地形時為中心點海拔） |
| `buildings[]` | `outer` (CCW 俯視) / `holes`、`height`、`min_height`、`levels`、`height_source`(height/levels/default)、`kind`(OSM building=*)、`obb`(最小面積旋轉矩形 center/size/angle)、`area` |
| `trees[]` | OSM `natural=tree`：`p [x,z]`、`kind` (conifer/broadleaf/palm/any)、`height` |
| `tree_rows[]` | 行道樹線 |
| `roads[]` | 中心線 `line`、`kind`(highway=*)、`width`(width 標籤 → lanes×3.25 → 類型預設值)、`bridge/tunnel/layer` |
| `areas[]` | `kind` ∈ forest/park/grass/scrub/farmland/orchard/water/wetland/sand/residential/commercial/industrial/parking/plaza/school/cemetery/other |
| `waterways[]` | 河川/水道中心線 + 寬度 |
| `terrain` | `res [nx,nz]`、`rect`、`heights`(row-major，相對中心點) |

建築高度規則：`height` 標籤 → `building:levels × 3.2 m (+roof:levels)` → 依用途的預設高度（`DEFAULT_HEIGHTS`）。
邊界處理：建築預設「重心在 box 內才保留、不裁切」（素材不能被切半）；`--clip-buildings` 改成裁切。綠地/道路一律裁切到 box。

## ③ Scatter 規則（`geobox/default_rules.json`，可在 scene.json 的 `rules` 覆寫）
| 規則 | 說明 |
|---|---|
| `buildings` | 每個 footprint → 1 個建築素材。用 `kind_tags` 把 OSM 用途對到素材 tags，以「高度/寬/深的對數誤差 + height_range」評分，取前 `variety_top_n` 名加權隨機。`fit: stretch` 會非等比縮放到 footprint OBB 與高度；`face_road` 會把正面 (+Z) 轉向最近的車道。 |
| `trees.osm_trees` | OSM 單棵樹的精確位置（有高度就用高度縮放） |
| `trees.tree_row_spacing` | 行道樹線每 N 公尺一棵 |
| `trees.areas.<kind>` | 在綠地多邊形內 Poisson-disk 撒點：`spacing`、`fill`(保留比例)、`tags`、`scale`；`grid` = 果園格狀 |
| `trees.clearance` | 與建築/道路的淨距 |
| `street_trees` / `street_lamps` | 沿指定道路類型兩側 / 單側，`spacing`、`offset`(離路緣)，路燈自動朝向道路 |
| `seed` | 全域隨機種子；同 seed 結果完全一樣（deterministic） |

### Instance（scene.json `instances[]`）
```json
{"id": "bld:w123", "asset": "bld_lowrise_a", "category": "building",
 "p": [x, y, z], "rot_y": 32.5, "scale": [1.1, 0.9, 1.3],
 "rule": "bld", "feature": "w123", "source": "procedural", "locked": false,
 "target": {"size": [w, h, d]}}
```
* `id` = 規則 + OSM id (+ index)，重跑 scatter 會得到同樣的 id。
* **保留編輯**：`source: "manual"` 或 `locked: true` 的 instance 在 re-scatter 時原樣保留；被刪除的程序性 instance 記在 `suppressed[]`，不會再長回來。WebGL 編輯器與 Houdini 匯出都會自動把「動過的」程序性物件設為 locked。

## ④ scene.usda
`/GeoBox/Ground`、`/GeoBox/Terrain`、`/GeoBox/Reference/{Massing,Roads}`（purpose = guide）、
`/GeoBox/Instances/{Buildings,Trees,Props}` 各一個 PointInstancer（`orientations` quath + `orientationsf` quatf、`scales`、`primvars:geobox_id`、`primvars:geobox_asset`），prototype 直接 reference 素材的 `files.usd`（套上 normalize 轉換），沒有 USD 檔時用依 bounds 的彩色 Cube 代替。已用 `usd-core` 驗證可開啟與計算 instance transforms。

## CLI
```bash
python -m geobox build --name taipei101 --at "https://www.google.com/maps/@25.0339,121.5645,17z" --size 800 600 [--terrain] [--clip-buildings] [--seed 7]
python -m geobox rescatter taipei101 --seed 42            # 保留手動編輯
python -m geobox rescatter taipei101 --rules my_rules.json # 規則覆寫 (JSON, 深層合併)
python -m geobox export taipei101                         # scene.json → scene.usda
python -m geobox parse "<google maps url>"                # 看網址被解析成什麼
python -m geobox serve [--port 8765]
```

## 已知限制 / 下一步
* 一個 footprint 對一個素材（OBB 擬合）。L 形、超大街廓會被拉伸；下一步可在 Houdini 用 `FOOTPRINTS` 做程序化建築，或把大 footprint 切成多個 lot。
* OSM 資料完整度依地區而定（台灣多數城市的建築輪廓完整，樓層數約一半有標）。
* 地形只做高度場；道路/綠地以貼圖方式貼在地形上（WebGL），Houdini 端以頂點高度貼合。
