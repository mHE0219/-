# GeoBox — 真實地圖 → 等比例 Box → 素材庫 Scatter → WebGL / Houdini

把 Google Maps 上的一塊範圍，變成**真實公尺比例**的 3D box，再用**你自己的素材庫**（建築 / 樹木 / 道具）
依地圖語意 scatter 出可編輯的場景；用 WebGL 預覽與編輯，並可匯入 Houdini（SOP 與 Solaris/USD）。
另含一套可由參考圖自動生成雲彩、可動畫、高可控的**天空 / 雲彩 shader**（同樣可進 Houdini）。

```mermaid
flowchart LR
  A[Google Maps 網址<br/>或 lat,lon + W×D 公尺] --> B[geo.py<br/>WGS84→ENU 真實公尺]
  B --> C[OSM Overpass<br/>+ 地形(選用)]
  C --> D[layout.json]
  D --> GB[灰盒 Greybox<br/>可編輯 blockout]
  GB -->|Art pass| E[scatter<br/>依灰盒檢索素材]
  IDX[(素材索引<br/>index.json + 縮圖)] --> E
  FARM[Farm 工作佇列 / PDG<br/>ingest·縮圖·整區 tiles] --> IDX
  L[(素材庫<br/>統一比例尺)] --> FARM
  E --> F[scene.json]
  F <--> G[WebGL 編輯器]
  F <--> H[Houdini SOP]
  F --> U[scene.usda / Solaris]
  S[sky_params.json] --> G
  S --> H
```

**本質：灰盒 (greybox) + 遊戲素材流程。** 地圖 → 真實比例的灰盒 blockout（可編輯）→ 依每個灰盒的尺寸/用途**檢索**素材庫 → 生成素材版本（Art pass）。
素材進庫（量測、統一比例尺、縮圖、索引）與整區建置都可以丟到 **farm** 上跑，跑完立刻可被檢索。

## 快速開始
需要：Python 3.9+（只用標準函式庫）、Node.js（只為了 `npm install` 一次 three.js 等前端套件）。

```bash
cd viewer && npm install && cd ..        # 安裝 three.js / lil-gui / leaflet / 3d-tiles-renderer（固定版本）
python -m geobox serve                    # http://127.0.0.1:8765
```
1. 瀏覽器開 http://127.0.0.1:8765 → **＋ 新 Box**
2. 貼上 Google Maps 網址（例如 `https://www.google.com/maps/@25.0339,121.5645,17z`）或 `25.0339, 121.5645`，
   設定 W×D（公尺）。地圖上的紅框就是 box 的實際範圍。
3. **建立** → 下載 OSM、產生 layout、用素材庫 scatter，幾秒到一分鐘。
4. 在編輯器裡選取 / 移動 / 旋轉 / 縮放 / 換素材 / 筆刷種樹 / 擦除 / 量測 → **💾 儲存**（寫 `scene.json` + `scene.usda`）。
5. Houdini：`python -m geobox houdini-install` 後重開 Houdini → **GeoBox** shelf → *City Box (SOP)* 或 *City Box (Solaris)*。

沒有網路時可以用內附的合成測試資料：
```bash
python -m geobox build --name demo --at "25.04, 121.52" --size 600 --osm-file tests/fixtures/sample_osm.json
```

命令列完整流程：
```bash
python -m geobox build --name taipei101 --at "<Google Maps 網址>" --size 800 600 --terrain
python -m geobox rescatter taipei101 --seed 42     # 換一組隨機結果，保留你手動改過的物件
```

## 功能對照（需求 → 實作）
| 需求 | 實作 |
|---|---|
| Google Map 範圍變成 box，比例正確 | `geobox/geo.py`：Google Maps 網址解析（`@lat,lon,zoom`、地標 `!3d!4d`、`?q=`），WGS84→ECEF→ENU，1 unit = 1 m；box 為以中心點為原點的 W×D 公尺矩形。量測工具 / 10 m 格線 / 1.8 m 人形驗證比例。 |
| 素材庫統一比例尺 | `assets/library.json` + `geobox/library.py`：標準 = 公尺 / +Y / 底面中心 pivot；宣告原始單位與上軸，自動量測 bounds、猜單位、檢查不合理尺寸。Houdini 端 `asset_prep` 可標準化任何格式。→ [docs/asset_library.md](docs/asset_library.md) |
| Map box → 自己素材庫的版本、可編輯（本質 scatter） | `geobox/scatter.py`：建築 footprint → 最適素材（OBB 擬合、正面朝路）、OSM 樹、行道樹、綠地 Poisson 撒點、路燈；規則在 `default_rules.json`。deterministic id，手動/鎖定編輯在 re-scatter 時保留。→ [docs/pipeline.md](docs/pipeline.md) |
| WebGL 預覽 | `viewer/`（three.js r186）：instanced 渲染、地面地圖貼圖、OSM 量體參考、Google Photorealistic 3D Tiles 參考層（裁切在 box 內，需自己的 key）、天空 shader 背景與陽光。 |
| 可編輯 | 選取 / 移動 / 旋轉 / 縮放 gizmo、屬性面板（換素材時保持真實尺寸）、放置、筆刷 scatter、擦除、量測、復原/重做、儲存。 |
| 匯入 Houdini | `houdini/`：package + shelf；Python SOP 讀 scene.json（instance 點、素材庫、地面、道路、綠地、footprints、量體）、Copy to Points；Solaris 讀 scene.usda；**Export Edits** 把 Houdini 的修改寫回。→ [docs/houdini.md](docs/houdini.md) |
| 灰盒 Greybox | `geobox/greybox.py` + `viewer/js/city/greybox.js`：OSM 建築 → 可編輯灰盒（移動/旋轉/改 W·H·D/畫新灰盒/刪除/指定素材），頂列 灰盒/素材/疊加 切換，**▶ 生成素材 (Art pass)**；Houdini `GREYBOX → EDIT_GREYBOX` 可編輯並回寫。→ [docs/greybox.md](docs/greybox.md) |
| 素材檢索 | `geobox/retrieval.py`：尺寸擬合、用途/tag 中英同義詞、中文/英文文字、面數預算、風格篩選；縮圖自動產生；左側搜尋格、右側「匹配素材」排名一鍵替換；`python -m geobox search`。→ [docs/retrieval.md](docs/retrieval.md) |
| Farm（與檢索結合） | `geobox/farm/`：共享資料夾工作佇列 + `python -m geobox farm worker --name cf-20`；`farm ingest <資料夾>` 量測/標準化/索引新素材（完成即可被檢索），`farm tiles` 把整區切成多個 box 分散建置；Houdini PDG/TOPs 版本（local/HQueue/Deadline/Tractor 排程器）。→ [docs/farm.md](docs/farm.md) |
| 天空 / 雲彩 shader | `viewer/sky.html` + `viewer/js/sky/`：大氣散射 + 體積雲 + 卷雲，風/演化動畫、全部顏色可調；丟參考圖自動擬合參數；烘焙 equirect HDR 序列；VEX 版本與 Houdini 建置腳本。→ [docs/sky.md](docs/sky.md) |

## 資料夾
```
geobox/            Python pipeline（純標準函式庫）: geo, osm, terrain, layout, greybox, retrieval, scatter, library, proxies, export_usd, project, server, farm/
viewer/            WebGL 編輯器 index.html（城市）與 sky.html（天空）；js/city/*, js/sky/*
assets/            library.json + proxies/（內建佔位素材 OBJ/USDA）
houdini/           python/geobox_houdini/*, toolbar/geobox.shelf, vex/include/*.h
projects/<name>/   每個 box 的資料：osm.json, layout.json, scene.json, scene.usda, sky_params.json, sky/*.hdr
docs/              conventions.md（座標/單位/API/擴充點約定）, pipeline.md, greybox.md, retrieval.md, farm.md, asset_library.md, houdini.md, sky.md
tests/             python -m unittest discover -s tests   （含 mock hou 的 Houdini 測試）
```

## 座標約定（全部模組一致）
**公尺、+Y 上、+X 東、-Z 北**，旋轉 `rot_y` 為度（同 Houdini `ry`），四元數 `(x,y,z,w)` 同 `p@orient`。詳見 [docs/conventions.md](docs/conventions.md)。

## 資料來源與授權注意
* 地圖語意資料：OpenStreetMap（ODbL）— 成品需標示「© OpenStreetMap contributors」。
* 地形：AWS Terrain Tiles（Terrarium，公開資料）。
* Google：只用網址取座標；Photorealistic 3D Tiles 僅作**串流視覺參考**（自己的 API key、顯示 attribution），依 Google 條款不匯出、不快取其幾何。
* 2D 選區地圖使用 OSM 標準圖磚（低流量互動使用；量大時請改用自己的圖磚服務）。

## Farm 快速上手
```bash
# 每台 farm 節點（共享資料夾路徑在各機器上要指向同一處；Windows/Linux 對應用 GEOBOX_PATH_MAP）
python -m geobox farm worker --root //nas/geobox_farm --name cf-20
# 送件
python -m geobox farm ingest //nas/assets/incoming --root //nas/geobox_farm      # 新素材 → 量測/標準化/索引
python -m geobox farm tiles --at "<Google Maps 網址>" --size 3000 2000 --tile 500 --root //nas/geobox_farm
python -m geobox farm status --watch --root //nas/geobox_farm
```

## 狀態
「先把流程架起來」的第一版：整條 pipeline 可從網址一路跑到 WebGL 編輯與 USD 輸出，並有自動測試。
尚未完成 / 需要你那邊驗證的部分：
* Houdini 端程式尚未在真正的 Houdini 中執行過（以 mock 與 USD 驗證）；
* 一棟建築對一個素材（OBB 拉伸），複雜形狀建築的程序化生成是下一步（`FOOTPRINTS` 已備好）；
* 素材庫目前是程式產生的 proxy，等你的素材進來後用 `farm ingest` / `library scan` / Houdini *Register Asset* 加入；
* Houdini 端（SOP 網路、PDG、天空 VEX/環境光方向）都只用 mock 測過，第一次在 Houdini 跑請看 console 的防呆警告；
* 檢索目前依尺寸/tag/文字/面數，尚未做影像或形狀相似度（索引已預留 embedding 欄位）；
* OSM Overpass 在這個雲端沙盒被網路政策擋住，所以真實地圖資料還沒在這裡跑過（合成資料 + 真實地形已測）；在你的機器上會直接連 Overpass。
