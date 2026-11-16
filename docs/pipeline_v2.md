# Pipeline v2：以 Houdini 為中心的灰盒 → 模組化素材流程

> v1 的問題（使用者回饋）：以 Python server + Web 為中心、每棟建築拉伸一個整棟素材、灰盒由地圖自動決定、素材入庫在最後、而且在雲端很難測試。
> v2 調整順序與責任：**素材先入庫 → 地圖只當參考 → 灰盒由設計者主導 → 依灰盒用模組化 kit 拼裝 → Houdini 為主、Web 為預覽**。

```
 0. 素材入庫 Intake（farm / PDG，先做）
    原始素材 → 統一比例尺(公尺/+Y/底部中心) → 分類(整棟/kit 構件/植被/道具) → kit 模組尺寸 → 縮圖 → 索引
 1. 參考 Reference（只當底圖，不直接變成場景）
    Google Maps 範圍 → OSM 輪廓/道路/綠地 + 地形 → 參考層 (地圖貼圖、OSM 量體、道路線)
 2. 灰盒 Greybox（設計者主導；OSM 只是可選的「起始草稿」）
    Houdini：GREYBOX_SEED(可空) → 設計者用任何 SOP 畫/改盒子 → OUT_GREYBOX   ／   Web：灰盒工具
 3. 素材化 Dress（在 Houdini 內即時計算，同一份 Python 也在 farm 上跑）
    每個灰盒依 dress 策略：kit = 模組化構件拼牆面/轉角/屋頂；whole = 檢索整棟素材；none = 留灰盒
    植被/道具依區域與道路規則 scatter（避開灰盒）
 4. 輸出 / 預覽
    Houdini 視窗 / Karma / USD(Solaris)；Web 預覽 = scene.json（由 Houdini 存出或 Web 編輯）
```

## 合約（各模組依此並行開發）

### 灰盒 block（scene.json `greybox[]`；Houdini 灰盒幾何的 prim 屬性同名）
沿用 docs/conventions.md 的欄位，新增：
| 欄位 | 值 | 說明 |
|---|---|---|
| `dress` | `"auto"`(預設) \| `"kit"` \| `"whole"` \| `"none"` | auto = 有對應 kit set 用 kit，否則 whole |
| `kit_set` | 字串或 null | 指定 kit set（例：`demo_shophouse`）；null = 依 kind/tags 自動挑 |
| `floor_height` | 公尺或 null | null = kit set 的模組高度 |

### Kit 構件（library.json 條目）
```json
{"id": "kit_shophouse_window_a", "category": "kit",
 "kit": {"set": "demo_shophouse", "role": "window", "module": [3.0, 3.2], "depth": 0.4,
         "floors": ["middle", "top"], "weight": 1.0},
 "tags": ["shophouse", "residential", "commercial"], "files": {...}, "bounds": {...}}
```
* `role` ∈ `wall` `window` `door` `shopfront` `corner` `parapet` `roof` `roof_edge`（可擴充）
* 構件座標：底部中心為原點，寬沿 X（`[-w/2, w/2]`），正面朝 +Z，背面貼在 z=0（= 灰盒外牆面），高 `[0, h]`
* `corner`：放在外角，原點在角點，兩個面分別沿 +X 與 +Z 方向延伸（L 型）
* 屋頂 `roof`：在 XZ 平面鋪 module×module

### Dress 入口（單一入口，Web/Server、Houdini、farm 共用）
```python
from geobox.scatter import scatter
instances, info = scatter(layout, lib, rules, previous=None, blocks=blocks)
```
* kit 構件 instance：`{"id": "kit:<block id>:<face>:<floor>:<col>", "category": "kit", "block": "<block id>", "asset": ..., "p", "rot_y", "scale", "rule": "kit"}`
  （id 決定論 → 鎖定 / 刪除的編輯在重新 dress 後保留，同既有 merge 規則）
* whole 素材 instance 維持原格式（`bld:...`）

### Houdini v2 網路（`/obj/geobox`）
`REFERENCE`(GROUND/AREAS/ROADS/MASSING → 參考顯示) · `GREYBOX_SEED` → `GREYBOX_EDIT` → `OUT_GREYBOX` ·
`DRESS`(Python SOP，input = OUT_GREYBOX，在 Houdini 內呼叫 `geobox.scatter.scatter`) → `OUT_INSTANCES` ·
`LIBRARY` → `PACK_LIBRARY` → `COPY_TO_POINTS` → `OUT`。按鈕：Seed from OSM / Clear Greybox / Save scene.json / Export USD / Open Web Preview。

### 落地（Windows 工作站 CF-20）
* `setup_cf20.bat`：找 Python（系統 Python 或 Houdini 內建的），寫 Houdini package，前端套件已內附（不需 Node/npm）
* `start_geobox.bat`：啟動 Web 預覽並開瀏覽器
* `run_houdini_selftest.bat`：用 `hython` 在真的 Houdini 裡建網路、cook 每個節點，輸出 `selftest_report.txt`（PASS/FAIL）
