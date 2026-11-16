# Farm：素材 ingest / 檢索索引 / 整區 build 分散到多台機器

「檢索素材」和「farm」在這裡是同一條線：**farm 負責把素材變成可檢索的資料**（量測、統一比例尺、
Houdini 標準化、縮圖、索引），**檢索負責在 scatter / greybox 時挑素材**；整區城市 build 也在 farm 上平行跑，
而每個 tile 的 build 會用同一份檢索索引挑素材。

```
                ┌──────────── 共享資料夾 (NAS / SMB / NFS) ─────────────┐
 web 編輯器 ─┐  │ farm/jobs/{queued,running,done,failed}/<id>.json      │  ┌─ worker cf-20 (python)
 CLI ────────┼─►│ farm/logs/<id>.log   farm/workers/<name>.json         │◄─┼─ worker cf-21 (python + hython → tag houdini)
 Houdini ────┘  │ assets/library.json + assets/index.json + thumbs/     │  └─ worker ...
                └────────────────────────────────────────────────────────┘
 Houdini PDG/TOPs 版本：同樣的工作，用 TOP 的 scheduler（Local / HQueue / Deadline / Tractor）派送
```

## 兩種跑法
| | 檔案式佇列 `geobox.farm` | Houdini PDG / TOPs `geobox_houdini.pdg` |
|---|---|---|
| 需要 | Python 3.9+（標準函式庫），共享資料夾 | Houdini + 你們的 TOP scheduler |
| 派送 | 每台機器跑 `farm worker`，自己去搶工作 | TOP network 交給 scheduler（HQueue / Deadline / Tractor / Local） |
| 適合 | 沒有排程系統、或要混用沒裝 Houdini 的機器 | 已經有 PDG farm 的工作室 |
| 共用 | 兩者呼叫同一組函式：`geobox.farm.jobs.ingest_file`、`tile_plan`、`geobox.project.build`、`geobox.retrieval.rebuild_index` |

## 工作類型（`python -m geobox farm types`）
| type | 說明 | 需要 tag |
|---|---|---|
| `ingest_folder` | 掃資料夾，對每個**新**模型送出 `ingest_asset`，之後送 `reindex`；有 fbx/abc/bgeo/usd 時另送 Houdini prep + 第二次 reindex | |
| `ingest_asset` | 一個檔案 → library.json 條目（依資料夾判斷類別、猜單位/上軸、量測 bounds、標準化）→ 檢索索引；結果含 `needs_thumbnail`、`needs_houdini` | |
| `reindex` | 重建 `assets/index.json`（增量；回報沒有縮圖的素材） | |
| `fetch_osm` | 整區只下載一次 OSM（避免每個 tile 各打一次 Overpass） | |
| `build_box` | 一個 box：OSM → layout → scatter → scene.json / scene.usda | |
| `build_tiles` | 把 W×D 的整區切成 N×M 個 tile，送出 `fetch_osm` + 每個 tile 的 `build_box`，寫 `projects/_district/<name>/tiles.json` | |
| `rescatter` / `export_usd` | 重新 scatter（保留手動編輯）/ 重新輸出 USD | |
| `hython` | 在有 Houdini 的節點跑 preset（`prep_library`、`cook_top`、`build_city`）或指定腳本 | `houdini` |

Ingest 與檢索的接點：每個 `ingest_asset` 完成時就呼叫 `retrieval.rebuild_index(lib, [id])`，所以素材**一進來就搜得到**；
WebGL 編輯器的 Farm 面板看到 reindex/ingest 完成會自動重新載入素材庫與檢索面板。
沒有縮圖的素材（`needs_thumbnail`）可以在編輯器的素材檢索面板「產生縮圖」，或在 Houdini PDG ingest 時用 OpenGL ROP 產生。

## 在 farm 節點（例如 cf-20）跑 worker
```bash
# 每台節點：repo 放在共享位置或各自 clone，農場資料夾放在共享位置
python -m geobox farm worker --root //nas/geobox_farm --name cf-20
python -m geobox farm worker --root /mnt/geobox_farm --name cf-21 --tags gpu      # 有 hython 會自動加 houdini tag
python -m geobox farm worker --root ... --types ingest_asset,reindex               # 只接某些工作
```
* 也可用環境變數 `GEOBOX_FARM_ROOT` 指定農場資料夾（預設 `<repo>/farm`，已在 .gitignore）。
* Windows 與 Linux 看到同一個共享的路徑不同時：`GEOBOX_PATH_MAP="//nas/geo=/mnt/geo;Z:/=/mnt/z/"`，worker 會轉換工作參數裡的路徑。
* hython 位置：`GEOBOX_HYTHON="C:/Program Files/Side Effects Software/Houdini 20.5.xxx/bin/hython.exe"` 或放進 PATH。
* 停止：Ctrl+C / SIGTERM（做完手上的工作才停），或 `python -m geobox farm stop cf-20`（遠端，寫 stop 檔）。

常駐方式：
* **Windows**：工作排程器「開機時」執行 `python -m geobox farm worker --root \\nas\geobox_farm --name %COMPUTERNAME%`；或用 NSSM 包成服務。
* **Linux**：systemd unit
  ```ini
  [Service]
  WorkingDirectory=/opt/geobox
  Environment=GEOBOX_FARM_ROOT=/mnt/geobox_farm
  ExecStart=/usr/bin/python3 -m geobox farm worker --name %H
  Restart=always
  ```
  或 cron：`@reboot cd /opt/geobox && python3 -m geobox farm worker --root /mnt/geobox_farm`
* `--once`：把目前能做的工作做完就離開（適合交給既有排程系統當一個 task）。

## 送工作
```bash
python -m geobox farm ingest //nas/assets/incoming/trees --category tree        # 素材進庫 + 索引
python -m geobox farm tiles --name xinyi --at "<Google Maps 網址>" --size 3000 2000 --tile 500   # 整區 6×4 tiles
python -m geobox farm submit rescatter --params '{"name": "xinyi_r00c00", "seed": 7}'
python -m geobox farm status [--watch 2]      # 節點與工作
python -m geobox farm log <job_id>  |  retry <job_id>  |  cancel <job_id>  |  clean --days 7
```
Web：編輯器左欄 **Farm 工作農場**（節點狀態、工作進度、log、retry/cancel、Ingest…、整區 Tiles…）。
API：`GET /api/farm/jobs`、`GET /api/farm/workers`、`GET /api/farm/types`、`POST /api/farm/submit`、
`POST /api/farm/jobs/<id>/retry|cancel`、`GET /api/farm/jobs/<id>/log`。

## 整區 tiles
* 每個 tile 是獨立的 box 專案 `<name>_rRRcCC`（列由北到南、欄由西到東），各自以 tile 中心為原點。
* `projects/_district/<name>/tiles.json` 記錄每個 tile 的經緯度中心與在整區座標中的 `offset [x, z]`（公尺），
  拼回整區只要平移 offset（幾公里範圍內 tile 切平面之間的旋轉 < 0.01°，誤差 < 1 cm）。
* 先跑一個 `fetch_osm`（整區一次 Overpass 查詢），tile 的 build 都依賴它、共用同一份 OSM。

## 佇列的保證
* **原子搶工作**：`os.rename(queued→running)`，同一個工作不會被兩台拿到（測試：兩個 worker process 搶 16 個工作，每個只被 claim 一次）。
* **心跳**：執行中每 5 秒更新；超過 `GEOBOX_FARM_STALE`（預設 120 秒）沒心跳 → 自動回到 queued（節點當機/斷線）。
  原 worker 若之後才完成，結果會因 claim token 不符被丟棄。
* **依賴**：`deps` 全部 done 才會被拿；任一依賴 failed → 自己也 failed。
* **重試**：`max_retries`（預設 1），間隔 `GEOBOX_FARM_RETRY_DELAY × 2^n` 秒。
* **優先權**：`priority` 大的先跑；同優先權先送先跑。
* **能力 tag**：`requires: ["houdini"]` 只會被有該 tag 的 worker 拿。
* **取消**：queued 直接取消；running 為合作式（工作在步驟之間檢查，hython 子程序會被 kill）。

## Houdini PDG / TOPs
```python
import geobox_houdini.pdg as gp
gp.build_ingest_network("//nas/assets/incoming", category="tree", scheduler="deadline")   # /obj/geobox_farm
gp.build_tiles_network(at="25.0339, 121.5645", size=(3000, 2000), tile=500,
                       osm_file="//nas/osm/xinyi.json", scheduler="hqueue")              # /obj/geobox_tiles
```
* Ingest：`filepattern` → `pythonscript`（每檔：`ingest_file` + Houdini 讀檔 → `asset_prep.normalise_geometry` → `houdini_cache/<id>.bgeo.sc`
  + OpenGL ROP 縮圖）→ `waitforall` → `pythonscript`（reindex）。
* Tiles：`genericgenerator`（N 個 tile）→ `pythonscript`（依 `work_item.index` 讀 tiles.json build）→ `waitforall` → 報告。
* Scheduler：`local`（預設）/ `hqueue` / `deadline` / `tractor`，就是換 TOP network 裡的 scheduler node。
* 也可以反過來：檔案式佇列的 `hython` 工作用 preset `cook_top` 在某台有 Houdini 的節點 cook 一個 hip 裡的 TOP network。
* ⚠ PDG 這部分在沒有 Houdini 的環境撰寫，TOP 參數名稱以防呆方式設定（找不到會印警告），請在你們的 Houdini 版本確認一次。

## 安全
* 有共享資料夾**寫入權**的人就能送工作給所有節點，請把農場資料夾權限限制在 pipeline 帳號。
* `hython` 工作只接受 preset 或既有腳本檔；內嵌程式碼需要 worker 以 `--allow-code` 啟動，且 HTTP API 一律拒絕內嵌程式碼。
* Web server 預設只綁 127.0.0.1；若要給整個工作室用，請放在內網並加上認證代理。
* 不要讓 worker 以管理員/root 身分執行。
