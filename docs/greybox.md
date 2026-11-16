# 灰盒 Greybox（Blockout）層

GeoBox 的本質是「**灰盒 blockout → 遊戲素材**」流程：

```
OSM 建築 footprint ──► 灰盒 blocks（scene.json greybox[]，可編輯）──► Art pass（素材檢索 + scatter）──► 建築素材 instances
                         ▲  WebGL 編輯器 / Houdini 都可以改                    （樹、路燈、行道樹另由 layout 規則產生）
```

灰盒是建築的「意圖」：位置、底面尺寸、高度、朝向、用途。素材只是灰盒的一種「美術解」，
每次 **▶ 生成素材 (Art pass)** 都會依灰盒重新檢索、重新擺放（鎖定的除外）。

## 資料格式（scene.json）
```json
"greybox": [
  {"id": "gb:w123", "kind": "apartments", "center": [x, z], "base_y": 0.0,
   "size": [w, h, d], "rot_y": 12.5, "tags": ["apartments", "residential"],
   "source": "osm", "feature": "w123", "locked": false, "asset_lock": null}
],
"suppressed_blocks": ["gb:w456"]
```
| 欄位 | 說明 |
|---|---|
| `center`, `base_y` | 底面中心（pivot = base centre），公尺 |
| `size` | `[W, H, D]`：局部 X 寬（長邊）、高、局部 Z 深 |
| `rot_y` | 度，繞 +Y；局部 **+Z = 正面**（OSM 灰盒自動朝向最近的車道） |
| `tags` | 給素材檢索用（預設由 `default_rules.json → buildings.kind_tags` 依 `kind` 帶入） |
| `source` | `osm`（由 OSM 建築產生）或 `manual`（手動畫的） |
| `locked` | 編輯過的 OSM 灰盒自動為 true，重建時保留 |
| `asset_lock` | 指定素材 id；`null` = 由檢索自動挑 |
| `suppressed_blocks` | 被刪除的 OSM 灰盒，重建時不會再長回來 |

建築 instance 以 `"block": "gb:..."` 指回所屬灰盒；OSM 灰盒的 instance id 維持 `bld:<osm id>`，手動灰盒為 `bld:gb:manual:...`。

## Art pass 規則
1. `blocks_from_layout()` 由 OSM 重建灰盒 → `merge_blocks()` 保留 `locked` / `manual`、排除 `suppressed_blocks`。
2. 每個灰盒：`asset_lock` 有值就用它；否則 `retrieval.AssetIndex.search(category="building", size=灰盒尺寸, tags=灰盒 tags)`，
   在前 `variety_top_n` 名中以 deterministic 權重挑選（同 seed 結果相同）。
3. 依素材 `fit`（stretch / uniform / none）縮放到灰盒尺寸；位置/朝向 = 灰盒。
4. 樹、路燈的避讓用灰盒：未編輯的 OSM 灰盒用精確的 OSM 輪廓，編輯過/手動的用灰盒矩形 → 刪掉一個灰盒就騰出空間，畫一個新灰盒樹就會讓開。
5. 灰盒被編輯或刪除時，對應的**未鎖定**建築素材會立即移除，下一次 Art pass 重新生成；鎖定的素材保留。

## WebGL 編輯器
* 上方 **灰盒 / 素材 / 疊加** 切換檢視（`V` 循環）。灰盒模式隱藏建築素材、保留樹與道具；疊加模式灰盒半透明。
* 灰盒顏色：灰 = OSM 原樣、暖灰 = 已編輯、藍灰 = 手動、綠灰 = 指定素材。正面 (+Z) 較暗。表面有 5 m 格線與 3.2 m 樓層線。
* 點選灰盒 → 同一套 gizmo：移動 (W)、旋轉 (E)、**縮放 (R) 會改尺寸**（不是 scale）。
* 屬性面板：W·H·D、中心、底 Y、旋轉、用途、tags、指定素材（自動 / 某素材）、鎖定。
* **▭ 灰盒 (G)**：在地面拖曳畫底面（Shift = 對齊 1 m），單擊 = 10×10 m；高度與用途在工具選項設定。長邊自動放在局部 X。
* Delete / 複製 (Ctrl+D) / 復原重做都支援灰盒，與素材混選也可以。
* 在素材面板換建築素材時，會寫入所屬灰盒的 `asset_lock`（之後每次 Art pass 都用這個素材）。
* **▶ 生成素材**：存檔 → 重新 scatter → 重新載入。
* Box 資訊下方可下載 `greybox.obj`（`/api/projects/<name>/greybox.obj`），給 Unity / Unreal 做 blockout。

## Houdini
`/obj/geobox_city` 新增：
```
GREYBOX (Python SOP) ─► EDIT_GREYBOX (null：在這裡插 Edit / Transform / PolyExtrude…) ─► OUT_GREYBOX
VIEW_MODE (switch, geo 節點參數 "View" = Greybox / Art / Both) ─► MERGE ─► OUT
```
* 每個灰盒 = 6 個四邊形的封閉盒，prim 屬性 `s@geobox_block_id, s@kind, s@tags, s@source, s@feature, s@asset_lock, f@height, v@size, f@rot_y, i@locked`。
* **Export Edits** 也會回寫灰盒：每個 `geobox_block_id` 的點重新擬合（最小面積旋轉矩形 + 高度）→ 中心 / 尺寸 / 旋轉；
  有變動的設為 locked；不見的 OSM 灰盒進 `suppressed_blocks`；沒有 id 的新盒子（例如 merge 一個 Box SOP）依連通性變成手動灰盒。
  若 OUT_GREYBOX 是空的（cook 失敗等），為了安全**不會**清空灰盒。
* Solaris：`scene.usda` 內有 `/GeoBox/Reference/Greybox`（purpose = guide）。

## CLI
```bash
python -m geobox greybox <project>                 # 統計
python -m geobox greybox <project> --obj out.obj   # 匯出 OBJ
python -m geobox greybox <project> --reset-edits   # 丟掉所有灰盒編輯（還原 OSM），再 rescatter
python -m geobox rescatter <project>               # = Art pass
```

## 限制
* 一個 OSM 建築 = 一個灰盒（最小面積旋轉矩形）；L 形 / 中庭建築會被包成一個矩形。之後可加「拆成多個灰盒」工具。
* 灰盒只有長方體；斜屋頂、退縮等細節由素材負責。
* Houdini 端的節點參數名稱未在真正的 Houdini 驗證（以 mock 測試）。
