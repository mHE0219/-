# 素材庫：統一比例尺規範

## 標準 (THE STANDARD)
所有素材在 pipeline 裡都被帶到同一個標準空間：

| 項目 | 規範 |
|---|---|
| 單位 | **公尺**（1 unit = 1 m） |
| 上軸 | **+Y** |
| Pivot | **底面中心**（bounding box 的 XZ 中心、最低點 Y=0） |
| 建築 | 長邊沿 **X**，主立面朝 **+Z**（scatter 會把 +Z 轉向最近的道路） |
| 樹 / 道具 | 樹幹 / 桿子在原點 |
| 尺寸 | 做成**真實尺寸**（一棵 8 m 的樹就是 8 units 高）；scatter 再依 OSM 高度 / `scale_range` 縮放 |

原始檔**不需要**照標準製作：在 manifest 宣告 `source_units` (m/cm/mm/in/ft) 與 `source_up` (+Y/+Z)，
`library normalize` 會量測檔案並寫入 `bounds`（標準空間）與 `normalize`（scale → 上軸旋轉 → pivot 位移）。
WebGL 與 Houdini 都在載入時套用同一個 normalize，原檔不動。

## assets/library.json
```json
{
  "id": "tree_camphor_a",
  "category": "tree",                    // building | tree | prop
  "label": "樟樹 A",
  "tags": ["broadleaf", "street", "any"], // 給 scatter 規則配對
  "files": {
    "web": "library/tree/camphor_a.glb",  // WebGL 預覽 (glb/gltf/obj/fbx)
    "houdini": "library/tree/camphor_a.fbx",
    "usd": "library/tree/camphor_a.usd",   // Solaris PointInstancer prototype（選用）
    "houdini_normalized": "houdini_cache/tree_camphor_a.bgeo.sc"  // asset_prep 產生
  },
  "source_units": "cm", "source_up": "+Y",
  "bounds": {"min": [-3.1, 0, -2.9], "max": [3.0, 9.8, 3.2]},   // 標準空間（自動）
  "normalize": {"scale": 0.01, "up": "+Y", "offset": [0.1, 0, -0.2]},  // 自動
  "fit": "uniform",                // building 預設 stretch；uniform / none
  "scale_range": [0.7, 1.3],       // 隨機/擬合縮放的上下限
  "height_range": [3, 12],         // building：適合的真實高度範圍 (m)
  "weight": 1.0,                   // 隨機挑選權重
  "materials": {"leaves": "#4f7d3a"}  // 選用：OBJ 材質名 → 預覽顏色
}
```

## 指令
```bash
python -m geobox library init                 # 產生/更新內建 proxy 素材（保留你自己的條目）
python -m geobox library scan assets/library  # 自動加入資料夾內新的 .glb/.obj/.fbx/.usd（依資料夾名猜類別）
python -m geobox library normalize --guess-units   # 量測 + 猜單位/上軸（例如 1200 高的樹 → cm）
python -m geobox library check                # 檢查缺檔、不合理尺寸（例如 300 m 高的樹）
```
`scan` / `normalize` 在純 Python 可量測 OBJ / glTF / GLB / USDA。FBX、ABC、BGEO、USDC 請在 Houdini 跑
**GeoBox ▸ Prep Library**（`geobox_houdini.asset_prep.prep_library()`），會量測、輸出標準化的
`houdini_cache/<id>.bgeo.sc` 並回寫 bounds。

在 Houdini 直接把一個 SOP 註冊成素材：選取 SOP → **GeoBox ▸ Register Asset**（輸入 id、類別、原始單位、上軸、tags）。
會輸出標準化 `.bgeo.sc`（Houdini 用）+ `.obj`（WebGL 預覽用）並寫入 library.json。

## 內建 proxy
`assets/proxies/` 的 12 個低模（6 種建築、4 種樹、灌木、路燈）是程式產生的佔位素材（OBJ + USDA），
讓整條 pipeline 在你的素材進來前就能跑。換成真素材時：加入新條目、把 proxy 的 `weight` 設 0 或刪掉條目即可。

## 建議的素材分類 tags
* 建築：`house` `residential` `apartments` `commercial` `retail` `office` `hotel` `industrial` `warehouse` `generic`（`default_rules.json → buildings.kind_tags` 把 OSM `building=*` 對到這些 tags）
* 植被：`broadleaf` `conifer` `palm` `bush` `street`（行道樹）`forest` `any`
* 道具：`streetlamp`（`street_lamps.tags`）
