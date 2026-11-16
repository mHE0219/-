# 落地到 CF-20（Windows 工作站）

目標：拿到 repo → **雙擊一個檔案**就能用 Web 預覽與 Houdini shelf；再雙擊一個檔案就能在**真的 Houdini** 裡自動測試，產生一份可以直接傳回來的報告。

需要：Windows 10/11、**Houdini**（建議 20.5 以上；selftest 用的 hython 需要可用的授權，Apprentice 版可能無法執行 hython）。
不需要：Node.js / npm（前端套件已內附在 `viewer/vendor/`）、另外安裝 Python（會自動使用 Houdini 內建的 Python）。

## 1. 取得檔案
擇一：
* **git**：`git clone -b claude/relaxed-hopper-fmu3xi https://github.com/mHE0219/-.git D:\geobox`
* **ZIP**：GitHub 切到 branch `claude/relaxed-hopper-fmu3xi` → **Code ▸ Download ZIP**。
  解壓縮**前**先在 ZIP 檔按右鍵 → 內容 → 勾選「**解除封鎖**」，否則 Windows 會擋 .bat（SmartScreen「Windows 已保護您的電腦」→ 其他資訊 → 仍要執行）。

建議放在路徑短、沒有中文的位置，例如 `D:\geobox`。

## 2. 雙擊 `setup_cf20.bat`
會依序：
1. 找 Python：`py -3` → Houdini 內建（`C:\Program Files\Side Effects Software\Houdini X.Y.Z\python3xx\python.exe`）→ PATH 上的 `python`
2. 找到所有 Houdini 版本，寫入 `文件\houdiniX.Y\packages\geobox.json`（OneDrive 重新導向的「文件」也會正確找到；有設 `HOUDINI_USER_PREF_DIR` 會優先使用）
3. 確認範例專案 `projects\sample_synthetic`
4. 跑單元測試（約 10–30 秒）
5. 寫 `tools\geobox_env.bat`（之後其他 .bat 共用）與 **`setup_report.txt`**

最後一行顯示「完成 ✔」即可。有 `[FAIL]` 的話把 `setup_report.txt` 傳給我。

## 3. Web 預覽：雙擊 `start_geobox.bat`
自動開瀏覽器到 http://127.0.0.1:8765/?project=sample_synthetic。關掉黑色視窗 = 停止。
* 只綁定本機 127.0.0.1，不需要開防火牆。
* 顯示「cannot listen on 127.0.0.1:8765」= 已經有一個在跑（直接用瀏覽器開即可），或改 port：`start_geobox.bat --port 8780`。
* 建新 Box 時會連 OpenStreetMap Overpass（`overpass-api.de`）與 OSM 圖磚；公司網路若有代理伺服器，請確認這兩個網域可連。

## 4. Houdini
重開 Houdini → 上方 shelf 區出現 **GeoBox**（沒看到：shelf 區最右邊的 ▾ → Shelves → 勾 GeoBox）。
* **City Box (SOP)**：選 `projects\sample_synthetic\scene.json`
* **City Box (Solaris)**、**Sky**、**Register Asset** 等見 [houdini.md](houdini.md)

## 5. 在真的 Houdini 自動測試：雙擊 `run_houdini_selftest.bat`
用最新版的 `hython.exe`（或 `HFS` 環境變數指定的版本）執行 `houdini\scripts\geobox_selftest.py`：
建立整個 GeoBox 網路、cook 每個節點、檢查輸出數量、建 Solaris / Sky / PDG，存一個 `.hip`。
完成後自動用記事本打開 **`selftest\selftest_report.txt`**（同資料夾還有 `.json`、`hython_output.log`、`geobox_selftest.hip`）。

**請把 `selftest\selftest_report.txt` 傳給我**——這是目前最需要的回饋：Houdini 端的程式只在模擬環境測過，報告會直接指出哪個節點/參數在你的 Houdini 版本不對。

## 6. Farm worker（選用）
雙擊 `run_farm_worker.bat` → 輸入共享資料夾（例如 `\\nas\geobox_farm`，留空 = 本機 `farm\`）。
固定使用時可先設好環境變數 `GEOBOX_FARM_ROOT`。開機自動執行：工作排程器 → 建立基本工作 → 「電腦啟動時」→ 程式 = `run_farm_worker.bat` 完整路徑。
詳見 [farm.md](farm.md)。

## 疑難排解
| 症狀 | 處理 |
|---|---|
| `Python 3.9+ was not found` | 安裝 Houdini 或 python.org 的 Python（勾 Add to PATH）後重跑 setup |
| setup 找不到 Houdini | 設環境變數 `HFS=C:\Program Files\Side Effects Software\Houdini 20.5.xxx` 後重跑 |
| Houdini 沒有 GeoBox shelf | 確認 `文件\houdiniX.Y\packages\geobox.json` 存在、版本號對應；重開 Houdini |
| 瀏覽器一片空白 | 按 F12 看 Console；確認 `viewer\vendor\` 完整（重新下載 repo） |
| 中文亂碼 | 請用 .bat 啟動（已設定 UTF-8）；自己開 cmd 時先 `chcp 65001` 並 `set PYTHONUTF8=1` |
| selftest 報告寫「No licenses」 | hython 需要授權（Houdini FX/Core/Indie）；改用 Houdini 介面裡的 shelf 手動測試 |
| 路徑太長 / 含中文 | 把 repo 放到 `D:\geobox` 這種短路徑 |

## 讓 Claude 直接在 CF-20 上測試
雲端的 Claude session 看不到你的電腦，所以 Houdini 部分只能靠你跑 selftest 回傳報告。若要讓 Claude 自己在 CF-20 上跑 hython、看錯誤、修正：
* **在 CF-20 上直接跑 Claude Code**：安裝 Claude Desktop（或 `claude` CLI），在 repo 資料夾開 session。這時 Claude 就在 CF-20 上，可以直接執行 `run_houdini_selftest.bat` / hython。
* **或分享資料夾給雲端 session（Remote Control）**：Claude Desktop → Settings → Claude Code → **Remote Control**，把 repo 資料夾加入。之後雲端 session 可以要求使用這個資料夾（每次都會先徵求你同意）。這項功能可能尚未對所有帳號開放。
