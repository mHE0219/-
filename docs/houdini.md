# Houdini（v2：以 Houdini 為主的工作流程）

> v2 起 **Houdini 是主要工具**：灰盒由設計者在 Houdini 裡畫/改，素材化（DRESS）在 Houdini 內即時計算，
> Web 只是預覽。流程與合約見 [pipeline_v2.md](pipeline_v2.md)。
>
> ⚠ 這些 `hou` 程式是在沒有 Houdini 的環境寫的（用 `tests/mock_hou.py` 測過整個網路的 cook、存檔、自我測試流程）。
> **第一次在 CF-20 上請先跑自我測試**（下面），把報告傳回來即可修正任何版本差異。

## 1. 安裝
```bat
setup_cf20.bat                      :: 一鍵（含 Houdini package）
:: 或手動
python -m geobox houdini-install    :: 自動找 Documents\houdiniXX.Y\packages\
```
`packages/geobox.json` 會設定：
| 變數 | 值 | 用途 |
|---|---|---|
| `GEOBOX` | repo 路徑（正斜線） | 所有腳本找 repo |
| `PYTHONPATH` += `$GEOBOX` | repo 根目錄 | DRESS 在 Houdini 內直接 `import geobox`（純標準函式庫，與 Web/farm 同一份程式） |
| `PYTHONPATH` += `$GEOBOX/houdini/python` | `geobox_houdini` | SOP / 建網路 / 存檔 |
| `HOUDINI_PATH` += `$GEOBOX/houdini` | shelf、`vex/include/geobox_sky.h` | GeoBox shelf、天空 VEX |

重開 Houdini → 上方 shelf 列加入 **GeoBox**。

## 2. 自我測試（先做這個）
```bat
run_houdini_selftest.bat
:: 等同： "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" houdini\scripts\geobox_selftest.py
::        [--project projects\sample_synthetic] [--report <資料夾或.txt>] [--hip <檔案.hip>] [--skip pdg,sky,solaris]
```
或在 Houdini 裡按 shelf **Self-test**（同一套檢查、在目前 session 執行、不存你的 .hip、不跑會卡住 UI 的 PDG）。
* 一律在**專案副本**上測（`projects/_selftest/<name>`），不會改你的檔案。
* 每項檢查獨立；失敗會記下 Houdini 原文錯誤、以及這個版本實際的參數名稱。
* 輸出 `projects/_selftest/selftest_report.txt` + `.json` + `geobox_selftest.hip`；結束碼 0 = 沒有 FAIL。

| 檢查 | 內容 |
|---|---|
| environment | Houdini / Python 版本、`import geobox`、package 是否生效（shelf 與 VEX include 找得到） |
| copy_project | 複製專案到 `_selftest` |
| build_network | 建 `/obj/geobox_selftest`，必要節點都在 |
| cook_all | 每個節點 force cook，收集 errors / warnings 與 cook 時間 |
| greybox | OUT_GREYBOX 的 block 數 = scene.json |
| dress | DRESS 產生的 instance 數、各類別數量（含 kit 構件）、耗時 |
| copy_to_points | COPY_TO_POINTS 有輸出；列出所有參數名（piece attribute 名稱各版本不同）；檢查 pivot（素材沒有沉到地下） |
| reference | 參考層有幾何 |
| view_modes | 灰盒 / 素材 / 疊加 三種顯示都有輸出 |
| designer_box | 接一個沒有任何屬性的 Box SOP 進 GREYBOX_DRAW → 被當成新 block 並素材化 |
| save_round_trip | 灰盒 +5 m、instance +3 m → Save → scene.json 有鎖定、重新載入後 DRESS 仍保留、scene.usda 重新輸出 |
| solaris | Sublayer scene.usda，stage 有 PointInstancer |
| sky | build_sky + 雲體積 VEX 編譯/cook（錯誤原文） |
| pdg_ingest | 兩個 OBJ 經 TOPs（local scheduler）入庫到暫存素材庫（單位/上軸自動判斷） |
| save_hip | 存測試用 .hip |

> Houdini Apprentice 沒有 hython → 用 shelf 的 Self-test。

## 3. 網路 `/obj/geobox`
```
REFERENCE   GROUND, AREAS, ROADS, MASSING(開關) ─► REFERENCE ─► SHOW_REFERENCE(開關)
GREYBOX     GREYBOX_SEED (存檔中的 blocks) ─► GREYBOX_DRAW (merge：第 2 個以後的輸入接你自己畫的盒子)
            ─► GREYBOX_EDIT (null：上面插 Edit / Transform) ─► OUT_GREYBOX
DRESS       DRESS (Python：OUT_GREYBOX → geobox.scatter.scatter：kit / 整棟 / 植被 / 道具)
            ─► EDIT_INSTANCES (null：上面插 Edit / Blast / Attribute) ─► OUT_INSTANCES
ART         LIBRARY (只載入 OUT_INSTANCES 用到的素材) ─► PACK_LIBRARY ─► PIECE_ATTRIB ─┐
            OUT_INSTANCES ──────────────────────────────────────────────────────────┴► COPY_TO_POINTS
VIEW        VIEW_MODE (灰盒 / 素材 / 疊加) + SHOW_REFERENCE ─► MERGE ─► OUT (display / render)
未接線       SAVED_INSTANCES（存檔時的 instances）、FOOTPRINTS（OSM 輪廓，給程序化建築 HDA）、INSTANCE_PACKED_DISK
```
Geo node 的 **GeoBox** 分頁：`scene_file`、`seed`（0 = 用 scene 的）、View、Show Reference、Show OSM Massing，
按鈕 **Seed Greybox from OSM / Clear Greybox / Reload / Save scene.json / Export USD / Open Web Preview / Run Self-test**。

## 4. 工作流程
1. **Build GeoBox**（shelf）→ 選 `projects/<name>/scene.json`。
2. **灰盒**：
   * 要從地圖起步：**Seed Greybox from OSM**（每棟 OSM 建築一個盒子；已編輯/手動的保留）。
   * 要完全自己設計：**Clear Greybox**，然後畫盒子（Box、PolyExtrude、任何封閉幾何皆可），接到 **GREYBOX_DRAW 的第 2 個以後的輸入**。
     每個相連的封閉塊 = 一個 block（自動取最小面積旋轉矩形 + 高度）。可選的 prim 屬性：
     `s@kind`（用途）`s@tags`（逗號）`s@dress`（auto / kit / whole / none）`s@kit_set` `f@floor_height` `s@asset_lock`。
     Packed 幾何請先 Unpack。
   * 微調：在 GREYBOX_EDIT 上方插 Edit / Transform SOP。
3. **DRESS 自動更新**（有快取：只切換顯示不會重算）。View 切換灰盒 / 素材 / 疊加。
4. **調整素材**：在 EDIT_INSTANCES 上方插 Edit / Blast；改 `s@name` = 換素材。
5. **Save scene.json**：
   | 你做的事 | 存檔結果 |
   |---|---|
   | 改了灰盒 | block 更新並鎖定，該 block 的程序素材下次重算 |
   | 新畫的盒子 | 新的 manual block（`gb:draw:<hash>`，id 穩定） |
   | 刪掉灰盒 | OSM block 進 `suppressed_blocks` |
   | 移動/旋轉/縮放/換素材 instance | 更新並 `locked`（之後重算保留） |
   | Blast 掉 instance | 進 `suppressed`（不會再長回來） |
   | 新增的點（無 id） | manual instance |
   | 沒動的 | 仍是 procedural |
   存檔同時重新輸出 `scene.usda`；Web 預覽 / farm 讀同一份 scene.json。
6. **Solaris / Karma**：shelf **City Box (Solaris)**（Sublayer `scene.usda`，PointInstancer）。
7. **Web 預覽**：**Open Web Preview**（自動用系統 Python 或 Houdini 內建 Python 啟動 `python -m geobox serve`，不佔 hython 授權）。

## 5. DRESS 輸出點屬性
| 屬性 | 說明 |
|---|---|
| `P`, `p@orient`, `v@scale`, `f@pscale` | 位置（公尺）、四元數 (x,y,z,w)、縮放 |
| `s@name` = `s@variant` | 素材 id（Copy to Points piece attribute；兩個名字都給，適應不同版本預設） |
| `s@geobox_id` | instance id（存檔比對用，請保留） |
| `s@category` | building / kit / tree / prop（也是點群組） |
| `s@block` | 所屬灰盒 block id |
| `s@source` `s@rule` `i@locked` `s@instancefile` | procedural / manual、規則、鎖定、素材檔路徑 |
| detail `s@geobox_dress` | JSON：block 數、instance 數、各規則統計、是否命中快取、耗時 |

## 6. Python API
```python
import geobox_houdini.build as gb
geo = gb.build_city("C:/geobox/projects/taipei101/scene.json")    # /obj/geobox
gb.seed_greybox(geo); gb.clear_greybox(geo); gb.reload(geo)
gb.save(geo)            # = export_edits
gb.export_usd(geo); gb.web_preview(geo); gb.build_stage(".../scene.json")
gb.run_selftest(geo)

import geobox_houdini.core as core          # 純 Python（不需要 hou）
data = core.SceneData(".../scene.json")
scene, instances, info = core.dress(data)   # 與 DRESS SOP 相同的計算
```
其他：`geobox_houdini.asset_prep`（Register Asset / Prep Library）、`geobox_houdini.pdg`（TOPs 入庫 / 整區 tiles）、
`geobox_houdini.sky`（環境光 / 太陽 / 雲體積）。

## 7. 版本差異與排錯
| 現象 | 原因 / 處理 |
|---|---|
| console：`has no parm 'usepieceattrib'` | Copy to Points 的 piece attribute 參數名不同。Builder 會自動模糊比對含 `piece` 的參數，且點與素材都有 `s@variant`（Houdini 預設的 piece 屬性名）。自我測試報告列出實際參數名，傳回來即可修正 |
| 素材一半沉到地下 | Pack / Copy to Points 的 pivot 是 Centroid：把 PACK_LIBRARY 與 COPY_TO_POINTS 的 Pivot 改 Origin（自我測試會偵測） |
| DRESS 錯誤 `No module named geobox` | package 沒生效：重跑 `setup_cf20.bat` / `houdini-install`，重開 Houdini |
| Web Preview 沒開 | 找不到 Python：設環境變數 `GEOBOX_PYTHON` 指向 python.exe，或先執行 `start_geobox.bat` |
| PDG 自我測試卡住 | 用 `--skip pdg` 先跑其他項目，把 TOP 節點的 log 傳回來 |
