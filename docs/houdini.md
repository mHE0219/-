# Houdini 整合

> ⚠ 這部分的 `hou` 程式碼是在沒有 Houdini 的環境寫的：邏輯以 `tests/mock_hou.py` 模擬測過、USD 以 `usd-core` 驗證過，
> 但**尚未在真正的 Houdini 裡跑過**。節點參數名稱都用防呆方式設定（找不到參數會印警告而不是失敗）。第一次使用時請留意 console 訊息。

## 安裝（Houdini package）
```bash
python -m geobox houdini-install            # 自動找 ~/houdiniXX.Y、Documents/houdiniXX.Y、~/Library/Preferences/houdini/XX.Y
python -m geobox houdini-install --prefs "C:/Users/me/Documents/houdini20.5"
```
會寫入 `<prefs>/packages/geobox.json`：設定 `$GEOBOX`、把 `houdini/python` 加進 `PYTHONPATH`、把 `houdini/` 加進 `HOUDINI_PATH`
（因此 `houdini/toolbar/geobox.shelf` 的 **GeoBox** shelf 與 `houdini/vex/include/*.h` 會自動載入）。重開 Houdini 生效。

## Shelf 工具
| 工具 | 作用 |
|---|---|
| City Box (SOP) | 選 `projects/<name>/scene.json` → 建立 `/obj/geobox_city` |
| City Box (Solaris) | `/stage` 建 Sublayer LOP 讀 `scene.usda`（PointInstancer，Karma 直接算） |
| Reload | 在 WebGL 編輯器存檔後重新讀取 |
| Export Edits | 把 Houdini 裡改過的 instance 點寫回 scene.json（+ 重新輸出 scene.usda） |
| Register Asset | 把選取 SOP 的模型標準化並加入素材庫 |
| Prep Library | 量測 / 標準化所有 Houdini 能讀的素材（fbx/abc/bgeo/usd） |
| Sky | 由 `sky_params.json` 建環境光（烘焙的 HDR 序列）、太陽光、體積雲（見 docs/sky.md） |

## /obj/geobox_city 網路
```
INSTANCES (Python SOP) ─► EDIT_INSTANCES (null) ─► OUT_INSTANCES ─┐
LIBRARY   (Python SOP) ─► PACK_LIBRARY (pack by name) ────────────┴► COPY_TO_POINTS ─┐
GROUND / AREAS / ROADS (Python SOP) ─► LAYERS ─► SHOW_LAYERS (switch) ───────────────┼► MERGE ─► OUT
MASSING (Python SOP, OSM 量體參考) ─► SHOW_MASSING (switch) ──────────────────────────┘
FOOTPRINTS (Python SOP, 未接線：程序化建築的起點)
INSTANCE_PACKED_DISK (Wrangle, 未接線：用 s@instancefile 做 packed disk 的快速路徑)
```
Geo node 上的 **GeoBox** 分頁：`scene_file`、Reload、Export Edits、Only Used Assets、Show Massing、Show Layers。

### INSTANCES 點屬性
| 屬性 | 說明 |
|---|---|
| `P` | 位置（公尺） |
| `p@orient` | 四元數 (x,y,z,w)，由 rot_y 轉換 |
| `v@scale`, `f@pscale` | 非等比縮放 / 平均縮放 |
| `s@name` | 素材 id（Copy to Points 的 piece attribute） |
| `s@geobox_id` | instance id（回寫用，請保留） |
| `s@category`, `s@rule`, `s@source`, `i@locked` | 類別 / 規則 / procedural|manual / 鎖定 |
| `s@instancefile` | 素材檔路徑（有 houdini_cache 時為標準化 bgeo） |
| 點群組 | `building`, `tree`, `prop` |
| detail | `s@geobox_scene`, `v@geobox_origin` (lat, lon, elevation) |

### FOOTPRINTS / ROADS / AREAS
* `FOOTPRINTS`：建築底面多邊形（在最低地面高度），prim 屬性 `height, min_height, levels, area, obb_angle, obb_size, kind, osm_id, building_name, height_source, hole`。用 PolyExtrude 依 `@height` 擠出、或接你的程序化建築 HDA。
* `ROADS`：道路/水道中心線 (open polyline)，`width, kind, bridge, tunnel, layer, road_name`。Sweep / PolyExpand2D 依 `@width` 做路面。
* `AREAS`：綠地/水域/土地使用多邊形，`kind`、`hole`。

## 在 Houdini 編輯 → 回寫
1. 在 `EDIT_INSTANCES` 之前插入 Edit / Transform / Attribute Wrangle / Blast 等 SOP（保持 `s@geobox_id`）。
2. 按 **Export Edits**：
   * 動過的 instance → 更新並 `locked = true`
   * 被刪掉的 → 程序性的記入 `suppressed`
   * 新增的點（沒有 id 或複製出來重複 id）→ 新的 manual instance（`s@name` 指定素材）
3. scene.json 與 scene.usda 被更新，WebGL 編輯器重新整理即可看到；之後 re-scatter 也會保留這些修改。

## Python API
```python
import geobox_houdini.build as gb
geo = gb.build_city("$HIP/../projects/taipei101/scene.json")
gb.reload(geo)
gb.export_edits(geo)
gb.build_stage("$HIP/../projects/taipei101/scene.json")

import geobox_houdini.asset_prep as ap
ap.register_sop(hou.node("/obj/tree/OUT"), "tree_camphor_a", "tree", source_units="cm", tags=["broadleaf", "street"])
ap.prep_library()
```
純資料部分在 `geobox_houdini.core`（不 import hou），也可以在 hython 或其他 DCC 使用。

## 若 Copy to Points 的 piece attribute 參數名不同
Builder 會設定 `usepieceattrib = 1`、`pieceattrib = name`。若 console 顯示找不到參數，請在 COPY_TO_POINTS 手動勾選
**Piece Attribute** 並填 `name`；或改用 Solaris 路徑（scene.usda PointInstancer）/ `INSTANCE_PACKED_DISK`。
