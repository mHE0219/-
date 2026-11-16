# 模組化構件 Kit：灰盒 → 牆 / 窗 / 騎樓 / 轉角 / 女兒牆 / 屋頂

v2 的建築素材化不再是「一棟灰盒拉伸一個整棟素材」，而是用**模組化構件 (kit)** 依灰盒尺寸拼裝（docs/pipeline_v2.md）。
整棟素材 (whole) 仍保留，給地標建築或沒有對應 kit 的用途（例如廠房）。

## 拼裝規則（geobox/kit.py）
```
          女兒牆 parapet ─────────────┐   沿屋頂邊一圈
          頂樓 top      │ 窗 │ 窗 │ 窗 │   「每一欄」決定一次是窗或牆 → 各樓層窗戶上下對齊
          中間樓層 middle│ 窗 │ 牆 │ 窗 │
          一樓 ground    │騎樓│ 門 │騎樓│   正面 (+Z，朝道路) 一樓：騎樓 / 店面；中間放門（淺的建築）
          轉角 corner：四個外角各一根（整棟高）
          屋頂 roof：一片屋頂板；roof_prop：頂樓水塔、頂樓加蓋鐵皮屋、機房
```
| 項目 | 規則 |
|---|---|
| 面 | 灰盒 4 面：0 正面(+Z)、1 右(+X)、2 背(-Z)、3 左(-X)；正面就是灰盒「面向道路」的那面 |
| 欄數 | `n = max(1, round(面長 / 模組寬))`，構件 X 方向縮放 `L/(n·模組寬)` → **剛好鋪滿**，不留縫 |
| 樓層 | `n_f = max(1, round((H − 女兒牆高) / 樓高))`，構件 Y 縮放到整數樓層剛好填滿灰盒高度 |
| 窗牆比 | `rules.kit.window_ratio`（正/背面）、`side_window_ratio`（側面）；側面短於 `party_wall_below` (7 m) 視為**共同壁**全牆 |
| 騎樓 | `shopfront` 構件可設 `min_depth`（demo 騎樓內縮 2.5 m，需灰盒深 ≥ 8 m），太淺的建築改用門 + 牆 |
| 決定論 | 同 seed、同灰盒 → 同結果；id = `kit:<灰盒 id>:<面>:<樓>:<欄>`（轉角 `c<k>`、女兒牆 `<面>:p:<欄>`、屋頂 `r:0:0`、屋頂物件 `rp:0:<i>`） |

## 灰盒的素材化策略（每個灰盒可個別設定）
| `dress` | 行為 |
|---|---|
| `auto`（預設，`rules.buildings.strategy`） | 有符合的 kit set（tags 與用途重疊、高度在 `height_range` 內）→ kit；否則 → whole |
| `kit` | 強制 kit；`kit_set` 指定 set，否則挑最接近的 |
| `whole` | 依灰盒尺寸**檢索**整棟素材（或 `asset_lock` 指定） |
| `none` | 只留灰盒，不生成素材 |

`floor_height`：覆寫樓高（例如 3.6 m 的辦公樓層）。Web 編輯器選灰盒 → 右側「素材化 Dress」可改策略 / kit set / 樓高，並顯示預估「N 層 × 模組數」；按 **▶ 生成素材** 生效。

## Kit 構件規範（library.json）
```json
{"id": "kit_sh_window_b", "category": "kit", "label": "透天 鐵窗",
 "kit": {"set": "demo_shophouse", "role": "window", "module": [3.0, 3.2], "depth": 0.3,
         "floors": ["middle", "top"], "height_range": [3, 30], "weight": 0.8},
 "tags": ["shophouse", "residential", "commercial", "window"], "files": {...}, "bounds": {...}}
```
* `role`：`wall` `window` `door` `shopfront` `corner` `parapet` `roof` `roof_prop`
* `floors`：可用的樓層帶 `ground` / `middle` / `top` / `roof` / `any`
* 選用：`min_depth`（灰盒深度下限）、`min_roof`（屋頂物件需要的屋頂尺寸 [w, d]）、`weight`
* **座標**：牆面構件 = 底部中心為原點、寬沿 X `[-w/2, w/2]`、**背面貼 z=0（灰盒外牆面）**、細節往 +Z 凸出（約 0.3 m）；
  轉角 = 原點在外角、佔 `x,z ∈ [0, c]`；屋頂 / 屋頂物件 = XZ 置中。`library normalize` 會依 role 保持這個 pivot。
* 一個 set 的 `tags` = 所有構件 tags 的聯集（去掉 role），`height_range` 取構件上的設定。

## 內建 demo kit（`assets/proxies/kit_*`，程式產生）
| set | 構件 | 模組 |
|---|---|---|
| `demo_shophouse` 透天/騎樓 | 牆、窗、鐵窗、窗+冷氣、騎樓(紅/藍/黃招牌)、門、轉角柱、女兒牆、屋頂、頂樓水塔、頂樓加蓋鐵皮屋 | 3.0 × 3.2 m，高度 3–30 m |
| `demo_tower` 大樓 | 帷幕玻璃、實牆板、大廳、轉角、女兒牆、屋頂、機房 | 3.0 × 3.6 m，高度 24–600 m |

換成自己的 kit：做一組符合上述座標規範的構件（Houdini **Register Asset** 或 farm ingest 入庫），在 library.json 填 `category: "kit"` 與 `kit` 欄位即可；
把 demo 構件 `weight` 設 0 就不會再被選到。

## 規模（600 × 600 m 測試場景 sample_synthetic）
310 個灰盒 → 273 個 kit 拼裝（51,596 個構件）+ 37 個整棟素材；scene.json ≈ 12 MB（每個 instance 一行的精簡 JSON），scene.usda ≈ 8 MB（`/GeoBox/Instances/Kit` PointInstancer）。

## 設計者主導的灰盒
* 新 Box 對話框「灰盒來源」：**OSM 自動草稿**（預設）或 **空白（設計者手動；地圖只當參考）**；CLI `--greybox empty`
* 空白模式仍會建立參考層（地圖貼圖、OSM 量體、道路）與植被，但不產生任何灰盒/建築
* 隨時可「＋ OSM 灰盒草稿」把 OSM 輪廓加進來（保留你畫的），或「清空 OSM 灰盒」回到設計者模式
  （API：`POST /api/projects/<name>/greybox/seed`、`/greybox/clear`；CLI：`python -m geobox greybox <name> --seed-osm | --clear`）

## 已知限制
* 灰盒是矩形盒；L 形 / 中庭建築目前是一個盒子（可在 Houdini 用多個盒子拼）
* 樓層與欄位以整數模組拉伸，極窄的面（< 半個模組）構件會被壓扁
* 騎樓內縮的走廊兩端由側面牆封住；連續騎樓街屋之間沒有打通
* 修改灰盒後，鎖定過的 kit 構件不會跟著移動（其他未鎖定構件會重新生成）
