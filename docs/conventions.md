# GeoBox 共用約定 (Contract)

所有模組（Python pipeline / WebGL viewer / Houdini / Sky）都遵守以下約定。

## 座標與單位
| 項目 | 約定 |
|---|---|
| 單位 | **1 unit = 1 公尺**（素材庫、scene.json、Houdini、three.js 全部一致） |
| 上軸 | **+Y up**（Houdini 與 three.js 預設相同，右手座標） |
| 水平方位 | **+X = 東**, **-Z = 北**（+Z = 南） |
| 原點 | Box 中心（地理中心點的 ENU 切平面），地面高度 = `origin.elevation` |
| 旋轉 | `rotation_y` 以「度」為單位，繞 +Y 逆時針（從上往下看），與 Houdini `ry` 相同 |
| 四元數 | `orient` = `[x, y, z, w]`（Houdini `p@orient` 相同順序） |
| 太陽方向 | 指向太陽的單位向量；`azimuth` = 從北順時針的方位角(度)、`elevation` = 仰角(度) |
| | `dir = (sin(az)·cos(el), sin(el), -cos(az)·cos(el))` |
| 時間 | 秒；Houdini 用 `$T`，frame = `time * fps + 1` |

## 專案資料夾 `projects/<name>/`
| 檔案 | 產生者 | 說明 |
|---|---|---|
| `project.json` | build | 中心經緯度、box 尺寸、建立參數 |
| `osm.json` | fetch | Overpass 原始資料（快取） |
| `terrain.json` | fetch（選用） | 高程網格 |
| `layout.json` | extract | 地圖語意：建築 footprint/高度、樹點、道路、綠地、水域（公尺座標） |
| `scene.json` | scatter / viewer / Houdini | **可編輯場景**：layout + 素材 instances（主交換格式） |
| `scene.usda` | export | Solaris/USD 用（PointInstancer） |
| `sky_params.json` | sky editor | 天空 shader 參數（web 與 Houdini 共用） |
| `sky/sky.####.hdr` | sky editor bake | equirect HDR（Houdini 環境光） |

## Server API（`python -m geobox serve`，預設 http://localhost:8765）
| Method | Path | 說明 |
|---|---|---|
| GET | `/api/projects` | 專案清單 |
| POST | `/api/build` | body `{name, center:[lat,lon], size:[w,d], ...}`，抓資料→layout→scatter |
| GET/PUT | `/api/projects/<name>/scene` | 讀/存 scene.json（存檔時同步輸出 scene.usda） |
| POST | `/api/projects/<name>/rescatter` | 重新 scatter，保留手動/鎖定的 instance |
| GET/PUT | `/api/projects/<name>/sky` | 讀/存 sky_params.json |
| PUT | `/api/projects/<name>/files/<relpath>` | 寫入二進位檔（僅允許 `sky/`、`exports/` 底下） |
| GET | `/api/library` | 素材庫 manifest（含 web 可載入的 URL） |
| GET | `/projects/<name>/<file>` | 靜態讀取專案檔案 |

## 擴充點（多個功能模組並行開發用）
| 層 | 擴充方式 |
|---|---|
| Server API | 模組內 `ROUTES = [(method, regex, handler)]`；`geobox/server.py: EXTENSIONS` 已登記 `geobox.retrieval`、`geobox.greybox`、`geobox.farm.api` |
| CLI | 模組內 `add_cli(subparsers)`，子命令 `set_defaults(func=handler)`；`geobox/__main__.py: CLI_EXTENSIONS` 已登記 `geobox.retrieval`、`geobox.greybox`、`geobox.farm.cli` |
| Viewer | `viewer/js/city/{greybox,assetSearch,farmPanel}.js` 匯出 `install(ctx)`；`ctx.hooks.{projectLoaded,libraryLoaded,frame,beforeSave}`、`ctx.editor.on('select'|'change'|'tool', fn)`、`ctx.panels.{left,right,topbar,viewport}` |

## Greybox（灰盒）
scene.json `greybox[]` 是可編輯的 blockout，scatter 的建築 pass 由它產生（不是直接讀 layout）：
```json
{"id": "gb:w123", "kind": "apartments", "center": [x, z], "base_y": 0.0, "size": [w, h, d], "rot_y": 12.5,
 "tags": ["apartments"], "source": "osm" | "manual", "feature": "w123", "locked": false, "asset_lock": null}
```
建築 instance 以 `"block": "gb:w123"` 指回所屬的 greybox。`asset_lock` = 指定素材 id（不再自動檢索）。

## 素材檢索 (retrieval)
`geobox/retrieval.py`：`AssetIndex.load(lib).search(category, size, tags, text, fit, limit, exclude)` →
`[{id, asset, score(0..1 越高越好), scale, reasons}]`；`index_asset(lib, id)`、`rebuild_index(lib)` → `assets/index.json`；
縮圖 `assets/thumbs/<id>.png`。

## Farm
`geobox/farm/`：共享資料夾上的檔案式工作佇列（`<farm_root>/jobs/{queued,running,done,failed}/<id>.json`），
`python -m geobox farm worker` 在任何機器（例如 farm 節點）上執行；工作類型包含素材 ingest（量測/標準化/縮圖/索引 → 檢索用）、
box build / rescatter、USD export；Houdini PDG/TOPs 版本在 `houdini/python/geobox_houdini/pdg.py`。
