# GeoBox Sky — 天空 / 雲彩 Shader

高可控的程序化天空：大氣（藝術漸層 ＋ 物理散射）、太陽、體積積雲層、卷雲層、動畫、調色；
可以**丟參考照片自動生成對應的雲彩**，並且**進 Houdini**（烘焙 HDR 環境光 ＋ VEX 體積雲）。

```
viewer/sky.html                  天空編輯器（獨立頁面；?project=<name> 時直接讀寫專案）
viewer/js/sky/SkySystem.js       核心類別（city viewer 也用它當背景＋太陽光）、DEFAULT_SKY_PARAMS
viewer/js/sky/skyShader.js       GLSL：大氣、雲 raymarch、卷雲、tonemap
viewer/js/sky/refFit.js          參考圖分析 → 參數；閉環精修 refine()
viewer/js/sky/hdr.js             equirect HDR 烘焙 / Radiance .hdr (RGBE, RLE) 編碼
viewer/js/sky/presets.js         預設：clear-noon / fair-cumulus / overcast-stratus / golden-hour / cirrus-high / stormy
viewer/js/sky/skyEditor.js       sky.html 的 UI 邏輯
houdini/vex/include/geobox_sky.h VEX 版（外觀相近，非逐像素一致）
houdini/python/geobox_houdini/sky.py   build_sky()：環境光 + 太陽 + 體積雲
tests/sky/                       Node 測試（HDR、參考圖統計）＋ Python 測試（Houdini 端，mock hou）
```

---

## 1. 快速開始

```bash
python -m geobox serve                 # 或任何靜態 server：cd viewer && python -m http.server
# 瀏覽器開
http://localhost:8765/sky.html?project=<專案名>   # 讀寫 projects/<name>/sky_params.json 與 sky/*.hdr
http://localhost:8765/sky.html                    # 單機模式：儲存 / 烘焙改為下載檔案
```

操作：拖曳 = 轉視角、滾輪 = FOV、空白鍵 = 播放 / 暫停。右側 lil-gui 調所有參數，左側是參考圖面板。

URL 參數（方便截圖 / 自動化）：`preset=`, `quality=low|medium|high`, `time=`, `paused=1`, `yaw=`, `pitch=`, `fov=`, `ui=0`。
頁面上 `window.__sky` 提供 `{ sky, renderer, camera, applyPreset, analyze, refine, bake }`。

---

## 2. 座標、單位、顏色約定（與 docs/conventions.md 一致）

* 公尺、+Y 上、+X 東、-Z 北；地面 y = 0；行星半徑 6360 km（雲層與大氣有地球曲率）。
* `sun.azimuth`：從北順時針（度）；`sun.elevation`：仰角（度）。方向向量 `(sin az·cos el, sin el, -cos az·cos el)`。
* `animation.windDirection` / `cirrus.direction`：雲**往哪裡飄**的方位角（不是氣象學的「風從哪來」）。
* 顏色一律是 **sRGB hex 字串**（`'#rrggbb'`），shader 內轉成 linear。強度另外用數值參數（`*Intensity`、`ambient`）。
* 時間：`sky.time` 是「天空秒」；`update(dt)` 會加上 `dt × animation.timeScale`。雲的位移 = 風向 × 風速 × time（純函數，
  所以任何一幀都可重現 —— 烘焙與 Houdini 都依此對時間）。

---

## 3. 在自己的 three.js 場景用（city viewer 整合）

```js
import { SkySystem } from './js/sky/SkySystem.js';
const sky = new SkySystem( renderer, { params } );      // params = sky_params.json 的內容（可部分）
scene.add( sky.object );                                // 全螢幕背景，renderOrder -1，先畫
sky.configureRenderer( renderer );                      // 讓場景 mesh 用同一個 tonemap / exposure（建議）
// 每幀
sky.update( dt, camera );
sunLight.position.copy( sky.sunDirection ).multiplyScalar( 1000 );
sunLight.color.copy( sky.sunColor ); sunLight.intensity = sky.sunIntensity;
hemi.color.copy( sky.ambientColor ); hemi.groundColor.copy( sky.groundColor ); hemi.intensity = sky.ambientIntensity;
```

| API | 說明 |
|---|---|
| `new SkySystem(renderer, {params})` | 建立；params 與預設值深度合併 |
| `sky.object` | 背景 mesh（`frustumCulled=false`、不寫深度、`toneMapped=false`）；重的 pass 在它的 `onBeforeRender` 內以低解析度渲染（相機、時間、參數不變時跳過） |
| `sky.update(dt, camera)` | 推進動畫時間 |
| `sky.setParams(partial)` / `sky.getParams()` / `sky.resetParams(full)` | 參數讀寫；`sky.params` 物件身分不變（可直接綁 UI，改完呼叫 `sky.syncUniforms()`） |
| `sky.time` / `sky.setTime(t)` | 天空時間（秒） |
| `sky.sunDirection` `sky.sunColor` `sky.sunIntensity` | DirectionalLight 用（顏色已含低角度紅化） |
| `sky.ambientColor` `sky.ambientIntensity` `sky.groundColor` | HemisphereLight 用（近似值） |
| `sky.configureRenderer(renderer)` | `renderer.toneMapping/toneMappingExposure` 對齊天空 |
| `sky.renderView({...})` | 低解析度渲染 + 回讀（參考圖精修用） |
| `sky.renderEquirect({...})` | 線性 HDR 全景回讀（烘焙用，分條渲染避免 GPU timeout） |
| `sky.dispose()` | 釋放 |

**Tone mapping**：天空自己做 exposure → tonemap → 飽和/對比/色調，並寫出 renderer 的輸出色彩空間；它忽略
`renderer.toneMapping`。tonemap 函式直接取自 three.js（ACES / AgX / Neutral / Reinhard），所以呼叫
`configureRenderer()` 後場景與天空曲線完全一致；飽和度 / 對比 / grade 只作用在天空。

---

## 4. 參數（sky_params.json）

```jsonc
{ "version": 1, "type": "geobox.sky", "name": "...",
  "sun": {...}, "atmosphere": {...}, "clouds": {...}, "cirrus": {...},
  "animation": {...}, "post": {...}, "quality": {...}, "view": {...} }
```

### sun 太陽
| 參數 | 預設 | 說明 |
|---|---|---|
| `azimuth` | 150 | 方位（度，北=0 順時針） |
| `elevation` | 38 | 仰角（度）；< 0 時太陽光淡出 |
| `color` | `#fff3e2` | 太陽顏色（`sunReddening=1` 時建議接近白色，紅化由大氣計算） |
| `intensity` | 3.0 | 太陽強度 = city viewer 的 DirectionalLight 強度 |
| `discSize` | 1 | 日盤大小（× 真實 0.53°） |
| `discIntensity` | 40 | 日盤亮度倍率（HDR 烘焙中的太陽） |
| `glow` / `glowSharpness` | 1 / 12 | 太陽周圍光暈強度 / 集中度 |

### atmosphere 大氣
| 參數 | 預設 | 說明 |
|---|---|---|
| `physicalMix` | 0.6 | 0 = 藝術漸層、1 = 物理單次散射（Rayleigh + Mie + 臭氧） |
| `physicalIntensity` | 1 | 物理天空亮度 |
| `zenithColor` / `horizonColor` / `groundColor` | | 漸層天頂 / 地平 / 地面顏色 |
| `horizonFalloff` | 0.45 | 漸層指數 `t = sin(el)^k`（小 = 天頂色往下延伸） |
| `skyIntensity` | 0.6 | 漸層亮度 |
| `tint` | `#ffffff` | 整體天空色調 |
| `rayleigh` | 1 | 空氣分子散射（藍色程度） |
| `mie` / `mieG` | 1 / 0.76 | 氣溶膠 / 霾的量與前向性 |
| `haze` | 0.2 | 地平線霧帶寬度（天空與地面交界） |
| `sunReddening` | 1 | 低角度太陽變紅的程度（0 = 只用 `sun.color`） |

### clouds 體積積雲層
| 參數 | 預設 | 說明 |
|---|---|---|
| `enabled` | true | |
| `coverage` | 0.42 | 覆蓋率 ≈ 從上往下看被雲蓋住的比例（由 2D 覆蓋場決定，與雲型大致無關） |
| `density` | 1 | 密度倍率 |
| `type` | 0.75 | 0 層雲（薄片）→ 0.5 層積雲 → 1 積雲（平底、圓頂） |
| `bottom` / `top` | 1500 / 4200 | 雲層底 / 頂高度（公尺） |
| `scale` | 1 | 雲的尺寸（1 ≈ 單朵 1–2 km） |
| `detailScale` / `detailStrength` | 1 / 0.35 | Worley 細節侵蝕的尺度 / 強度（底部絲狀、頂部花椰菜狀） |
| `coverageVariation` | 0.3 | 大範圍覆蓋變化（天空某些區域更多雲） |
| `seed` | 1 | 雲的隨機種子 |
| `litColor` / `litIntensity` | `#ffffff` / 1 | 受光面色調 / 強度 |
| `shadowColor` / `ambient` | `#76859e` / 1 | 雲底環境光顏色 / 環境光強度（雲頂環境光取天頂色） |
| `silverIntensity` / `silverSpread` | 0.6 / 0.25 | 逆光銀邊強度 / 擴散 |
| `forwardScatter` / `backScatter` | 0.6 / -0.3 | 雙瓣 Henyey–Greenstein 相位 g 值 |
| `powder` | 0.5 | Powder 效果（背光時薄邊變暗） |
| `multiScatter` | 0.5 | 多重散射近似（厚雲不會變黑；也控制陰天雲底亮度） |
| `extinction` | 0.04 | 消光係數（1/m，density 1 時） |
| `fadeDistance` / `maxDistance` | 35 km / 80 km | 遠景淡出距離 / raymarch 上限 |

### cirrus 卷雲（高空 2D 層）
`enabled`, `coverage` 0.25, `opacity` 0.5, `altitude` 8500 m, `scale` 1, `stretch` 3（沿方向拉長）,
`direction` 60°（條紋與飄移方位）, `speed` 25 m/s, `color`。

### animation 動畫
`timeScale` 8（天空秒 / 真實秒，縮時）、`paused`、`windDirection` 70°（雲往哪飄）、`windSpeed` 12 m/s、
`evolveSpeed` 1（雲形變速度；1 = 每 10 天空分鐘一個噪聲週期）。

### post 後製（只影響顯示，不進 HDR 烘焙）
`exposure` 1、`tonemap` `aces|agx|neutral|reinhard|none`、`saturation` 1、`contrast` 1、`grade` 色調。
（烘焙時可選擇套用 `grade` 與 `saturation`，因為它們在線性空間有意義；tonemap / 對比不套用。）

### quality 品質
`steps` 48（主 raymarch 步數）、`lightSteps` 6、`atmosphereSteps` 10、`renderScale` 0.5（重的 pass 的解析度比例）、
`jitter` true。sky.html 的「品質」選單：low（24/3/6/0.35）、medium（48/6/10/0.5）、high（96/8/16/1.0）。

### view
sky.html 編輯器相機（`fov`, `yaw`, `pitch`, `height`）；參考圖擬合時會設成照片的相機。SkySystem 本身不使用。

---

## 5. 渲染模型

* **大氣**：藝術漸層 `mix(horizon, zenith, sin(el)^k)` 與物理單次散射（Rayleigh 8 km、Mie 1.2 km 尺度高度、臭氧吸收；
  光路用 Kasten–Young air mass 解析近似；視線取樣靠近相機較密）以 `physicalMix` 混合，再乘 `tint`。
  地平線以下為地面（`groundColor` × 太陽 / 天空光），以 `haze` 寬度混合。
* **積雲**：在地球曲率的球殼 [bottom, top] 內 raymarch（相機在雲下、雲中、雲上皆可）。
  密度 = 2D 覆蓋場（Perlin+Worley，決定雲在哪裡）× 依雲型的高度輪廓（覆蓋門檻隨高度升高 → 平底圓頂）→
  3D Perlin-Worley 侵蝕（花椰菜邊緣）→ Worley 細節侵蝕。光照：朝太陽的光線步進求光學深度、Beer–Lambert、
  3 個 octave 的多重散射近似、雙瓣 HG ＋ 銀邊、powder、雲頂取天頂天空光、雲底取 `shadowColor`、穿透的漫射太陽光；
  能量守恆積分；依距離淡出到天空。取樣距離 ∝ u^1.6（近處細、遠處粗）＋ 每像素 jitter，由合成 pass 的 tent filter 平滑。
* **卷雲**：高度 `altitude` 的球面上 2D：大尺度 patch 決定位置、沿方向拉長的扭曲纖維、斷裂成羽狀。
* **太陽日盤**：在全解析度合成 pass 加上（被雲的透射率遮擋），所以低 `renderScale` 也不會糊。
* **噪聲**：全部是 hash（pcg3d）生成的 gradient / Worley noise，沒有貼圖，方便移植（VEX 版見 §8）。

---

## 6. 參考圖 → 自動生成雲彩

左側面板：拖放照片 →（自動分析並顯示紅色雲遮罩、藍色地平線）→ **① 分析並套用 Fit** → **② 精修 Refine**。

**分析（refFit.computeSkyStats / statsToParams）**
1. **地平線**：下半部最強的「上亮下暗、上藍下不藍」列轉換；可手動。依地平線與 FOV 推出相機 pitch 與每列仰角。
2. **雲遮罩**：特徵 `F = (B−R)/(B+R) + 0.5·飽和度`（越藍越飽和越像晴空）。晴空越接近地平線越淡，所以用
   「**同一列的晴空參考值**」（每列第 85 百分位，限制往下的下降速度）求相對值 `G = F − ref(row)`，Otsu 自動門檻
   （可改手動）；整片灰 → 陰天、整片藍 → 晴天。地平線上 3° 以內的霧帶不參與統計。
3. **覆蓋率** = 雲像素比例。
4. **天空色**：低仰角帶與高仰角帶的晴空像素**中位數**（排除太陽附近），反推漸層的 horizon / zenith 顏色。
5. **雲色**：雲像素最亮 15% → `litColor/litIntensity`，最暗 15% → `shadowColor`。
6. **太陽**：小而緊密的飽和光斑 → 方位 / 仰角；找不到時依左右亮度梯度放在畫面外，均勻則假設在相機後方；仰角由地平線色溫估計。
7. **雲型 / 尺寸 / 細節**：水平連續長度 → `scale`；雲內對比、邊緣柔和度、異向性 → `type`、`detailStrength`、卷雲。
8. 顏色都先經過近似的**反 tonemap**（ACES），所以參數是線性 HDR 值。

**精修（refFit.refine，閉環）**：以照片的相機（FOV、pitch、yaw）低解析度渲染 shader，用同一套統計比較：
* 第一階段只修天空色（量測**無雲**渲染，雲不會干擾）。
* 第二階段：覆蓋率用 bracketed secant（量測渲染的**實際雲不透明度**，不受顏色門檻影響）、`scale` 依連續長度比例、
  雲亮部 / 暗部顏色依比例（含阻尼與上下限）。保留分數最好的一次。
面板會顯示每次迭代的分數；右側縮圖是同相機的渲染對照。

> 自動擬合是「好的起點」：雲型、太陽在畫面外的方位、夕陽天空的遮罩都屬啟發式；結果可再用 GUI 手動調整。

---

## 7. HDR 烘焙（→ Houdini 環境光）

GUI「HDR 烘焙 Bake」：寬度（512–4096，高 = 寬/2）、是否含日盤、是否套用 grade、幀範圍、FPS、起始時間。
* **Bake frame**：輸出 `sky/sky.<start>.hdr`。**Bake sequence**：`sky/sky.$F4.hdr`。
* 有 `?project=` 時直接寫到 `projects/<name>/sky/`（`PUT /api/projects/<name>/files/sky/...`），並同時存
  `sky_params.json`；否則逐檔下載。
* 幀 f 的天空時間 = `startTime + (f − start) / fps × timeScale`（與 Houdini 的 `$T × timeScale` 對應）。
* 輸出是**線性 HDR**（Radiance RGBE，RLE），不含 tonemap / 對比；烘焙會用 2× raymarch 步數 + jitter。
* 全景方向（與 three.js equirect / HDRLoader 相同）：第 0 列 = 天頂；`u = atan2(z, x)/2π + 0.5`
  → u=0.5 為 **+X 東**、u=0.75 為 **+Z 南**、u=0.25 為 **−Z 北**、u=0/1 為 **−X 西**。

---

## 8. Houdini

前提：已安裝 GeoBox Houdini package（`houdini/` 在 `HOUDINI_PATH`，`houdini/python` 在 `PYTHONPATH`，見 docs/houdini.md）。

```python
import geobox_houdini.sky as sky
nodes = sky.build_sky("/path/projects/taipei/sky_params.json")          # 或 shelf：GeoBox ▸ Sky
# 改了 sky_params.json 之後只更新參數（保留你在 Houdini 裡的其他修改）
sky.update_sky("/path/projects/taipei/sky_params.json")
```

`build_sky(params_path, hdr_pattern=None, parent='/obj', with_volume=True, name='geobox_sky', cloud_extent=20000, voxel_size=60, env_rotate_y=-90)` 建立 `/obj/geobox_sky`：

| 節點 | 內容 |
|---|---|
| `env_sky` (envlight) | `env_map` = 烘焙的 HDR（序列時自動用 `sky.$F4.hdr`）。**與網頁完全一致的天空** |
| `sun` (hlight 2.0, distant) | 方向由 azimuth/elevation（旋轉順序 xyz：`rx = −asin(d.y)`、`ry = atan2(d.x, d.z)`，已驗證 +Z → 太陽方向）；顏色 / 強度同網頁太陽（含紅化） |
| `clouds` (geo) | `volume`（覆蓋 `cloud_extent` × [bottom, top]）→ `volumewrangle`（`#include "geobox_sky.h"`）→ `OUT_CLOUDS`。所有雲參數都是 wrangle 上的 spare parms（GeoBox Sky 分頁），動畫用 `@Time × time_scale + time_offset` |

* Volume 的 `density` 已乘上 `extinction`（1/m），場景單位是公尺，所以 Karma / Mantra 的體積材質 density 倍率保持 1 即可。
* `geobox_sky.h` 也提供 `gbx_sky_background()`、`gbx_physical_sky()`、`gbx_latlong_dir(u, v)`，可在 wrangle / COP 中原生烘焙全景。
* VEX 版雲用的是 VEX 內建 `noise()` / `wnoise()`：**形狀相似但不是逐像素一致**。要與網頁完全一致請用烘焙的 HDR；
  VEX 體積雲適合需要真實 3D 體積（陰影投到城市、飛進雲裡）的鏡頭。

---

## 9. 已知限制

* **環境光方向（Houdini）**：網頁烘焙的全景方向已在 three.js 驗證（太陽像素位置與方向一致）；Houdini envlight 的
  lat-long 對應方式在此環境無法實測，`env_rotate_y` 預設 −90°。第一次使用請用鉻球確認 HDR 中的太陽亮點與 `sun`
  燈方向一致，不一致就調 `env_sky` 的 `ry`（或 `build_sky(..., env_rotate_y=...)`）。
* VEX 檔沒有在 Houdini 內編譯測試過（此環境沒有 Houdini）；Python 端以 mock `hou` 測試流程。
* 參考圖擬合是啟發式：夕陽 / 強逆光照片的雲遮罩可能需要手動調「雲閾值」；雲型 `type` 不參與閉環精修。
* 雲層是單一層（加一層卷雲）；沒有積雨雲砧狀頂、雨幕、閃電。
* 太陽在地平線下時只有天空漸層亮度（沒有星空 / 月亮）。
* 效能：積雲 raymarch 很重。整合到城市預覽時建議 `quality.renderScale` 0.35–0.5；SwiftShader（無 GPU）下 2048 全景烘焙約 20 秒。

---

## 10. 測試

```bash
node tests/sky/test_sky.mjs                                      # RGBE 編解碼、參考圖統計（合成圖）
python -m unittest discover -s tests/sky -p "test_*.py"          # Houdini 端：數學、build_sky（mock hou）
```
瀏覽器端（shader 編譯、預設截圖、合成參考圖擬合 / 精修、HDR 方向）以 Playwright + SwiftShader 驗證過。
