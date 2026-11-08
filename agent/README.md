# SongForPrism 自动登录（agent/）

基于 MaaFramework 5.15.0（Python 绑定 `maafw`，wheel 自带预编译 DLL，零编译）的 Win32 自动登录。
用户已拍板三项：**终点 = 点击 `TOUCH TO START` 进入游戏**、**启动只用现有 `.lnk`**、**允许管理员运行（提权由用户手动完成）**。

## 目录与运行

```
agent/
├── main.py                    入口：权限自检 / 启动 / 窗口发现 / 连接 / pipeline / 成功判定 / 兜底 / --check
├── config/settings.json       全部配置（两个开关、控制器、识别、流程、权限）
├── resource/                  Resource Bundle，传给 Resource().post_bundle("resource")
│   ├── default_pipeline.json  全局默认（Default + TemplateMatch/OCR 算法默认）
│   ├── pipeline/login.json    登录流水线
│   ├── image/touch_to_start.png  TOUCH TO START 模板
│   └── model/ocr/             OCR 模型占位（det.onnx/rec.onnx/keys.txt，本阶段未下载）
├── tools/make_template.py     纯 Python PNG 裁剪/缩放工具（重裁模板）
└── debug/                     运行期产物（日志/留图，已被 .gitignore 排除）
```

- 安装：`python -m pip install maafw -i https://pypi.org/simple`（本机 pip 全局是清华镜像、无 maafw，**必须显式带官方源**）。
- 静态自测（不启动游戏、不点击、不受权限限制）：`cd agent && python main.py --check`
- 实跑（**必须管理员**，由任务 #5 承担）：
  1. 右键「Windows 终端 / PowerShell / CMD」→ 以管理员身份运行；
  2. `cd /d D:\DMM\scsp_MAA` → `python agent\main.py`（或 `--skip-launch` 仅附加已开窗口）。

## 1. 为什么必须管理员权限运行（★硬性前置★）

目标游戏进程 `imasscprism` 是 **ELEVATED（管理员）**，宿主 WorkBuddy 非管理员。Windows **UIPI（用户界面特权隔离）** 会：
- 静默丢弃 `PostMessage` / `SendMessage` 窗口消息 → 点击/输入失效；
- **连 `SendInput` / `Seize` 真实鼠标输入都无效**（surveyor #7 实证：点击坐标正确落在 `TOUCH TO START` 文字中央、
  `WindowFromPoint` 命中游戏 hwnd、`SendInput` 返回 1/1，但点击后 25 秒 10 帧仍是标题画面、按钮始终存在）；
- 使 `PrintWindow` 等后台截图返回**全黑** → 界面识别失效；
- `SetForegroundWindow` 也可能失效。

> **★「宿主必须管理员运行」是硬性前置，不是建议★**：未提权时**任何输入方式（含真实鼠标 `Seize`/`SendInput`）都不会生效**，
> 不只是消息式输入被丢。任务 #5 实跑**必须由用户手动在管理员终端执行**，脚本本身不能自行提权。

因此 `main.py` 开头 `require_elevation()` 做权限自检：宿主非 ELEVATED 时输出中文提示（`ELEVATION_HINT`）、返回
`FAIL_NOT_ELEVATED` 并以非零退出码结束。**不静默降级、不自行弹 UAC、不自行调用 `runas` / `ShellExecute("runas")`**，
提权动作由用户手动完成。`GameProcessMonitor` 另用 `OpenProcessToken`/`GetTokenInformation` 检测目标进程 elevated 状态并记录日志。

## 2. 输入 / 截图方案的切换方法（都在 `config/settings.json` 的 `controller` 段，一行切换）

| 项 | 默认（实测推荐） | 备选（提权后按 #5 实跑效果切换） |
| --- | --- | --- |
| `screencap` | `Foreground`（`DXGI_DesktopDup_Window \| ScreenDC`，避开实测全黑不可用的 `PrintWindow`） | `ScreenDC` / `FramePool` / `GDI` / `Background` |
| `mouse` / `keyboard` | `Seize`（真实鼠标，**仅在已提权前提下**对 Unity 最可靠） | `PostMessage` / `SendMessage` / `PostMessageWithCursorPos` 等消息式输入 |

> **前提仍是 §1 硬性前置：宿主未提权时连 `Seize`/`SendInput` 真实鼠标都无效**，表中所有输入方式都不生效。
> 提权后 UIPI 不再阻断，可优先选不抢鼠标的消息式输入；但 **Unity 窗口对消息式点击的响应未经验证**，若点击无效请改回 `Seize`。
> 截图若某方式黑屏或失败，改这一行即可。实跑由 #5 按效果决定，**不要写死**。

## 3. 模板素材与真实帧校准（★任务 #18 已用 1280×720 真实帧重裁并重定阈值★）

### 3.1 这一版推翻两个旧结论（务必先读）

1. **「帧宽在 1260~1510 波动」是调试图假象，不是真实几何。**
   `TemplateMatcher::draw_result()` 会把模板缩略图拼在调试帧右侧
   （`source/MaaFramework/Vision/TemplateMatcher.cpp:143-146` 的
   `copyMakeBorder(image_draw, image_draw, 0,0,0, templ.cols, ...)`），所以
   `debug/vision/*_TitleScreenDetectedClick_*.jpg` 的宽度 = **image_target 宽 + 模板宽**
   （旧模板宽 230 → 1280+230=1510、1260+230=1490；22:12 那次会话模板宽也是 230，故 1267→1497、1397→1627）。
   `debug/maafw.log` 的 `calc_target_image_size` 全程只重算过两次：
   - `02:10:11` raw **2532×1447** → target **1260×720**（窗口刚被 prealign 改尺寸的过渡，约 5s）；
   - `02:10:16` raw **2240×1260** → target **1280×720**（= 1280×720 @175% DPI），**此后整场不再变化**。
   `debug/screencap/*.png`（`cached_image` 直接落盘）与 `on_error/*.png` 也都是 **1280×720**，可交叉印证。
   → 结论：**不需要多尺度模板数组**。`Custom Recognition`（`HandleLoadingScreen`）帧没有模板缩略图，
   所以它一直干净地是 1280×720——这也解释了「同一秒内两个节点的帧宽不同」。
2. **「模板不可分」的旧结论作废，因为当时的『加载态负样本』其实是亮标题帧。**
   旧的 `source_frames_loading` 里 `02.11.05.315`（mean 239.7 / std 23.1）目视是**白到发光的标题画面、
   `TOUCH TO START` 清晰可见**（NCC 0.653 命中），`02.10.45.664` 同理。拿「有按钮的亮帧」当负样本，
   自然算出「白屏 0.757 > 标题最低 0.375、不可分」。重标后模板**完全可分**（见 3.3）。

### 3.2 当前模板与几何

- `resource/image/` 下 **8 张模板**，均 **325×23**，裁自 9 张已目视确认干净的真实 1280×720 帧中的 8 张：
  `crop=(x0,y0,x1,y1)=(468,576,793,599)`（纯文字区；避开左侧菱形装饰 404~460 与底部版权 608）。
  v1 原先裁自 `02.10.20.197`——该帧按钮区被**管理员 PowerShell 控制台**遮挡（目视可见灰黑终端条），
  属污染模板（对全部真实标题帧只给出 0.10~0.26），**已换成 `02.10.22.145` 重裁**。
- `roi=[380,540,520,90]` 即 `[380,900]×[540,630]`，完整包住 crop；`--check [5]` 断言 crop ⊂ roi。
- `threshold=0.5`、`method=5`（TM_CCOEFF_NORMED）。三个 TemplateMatch 节点 + `make_button_visible_fn`
  共用 `build_detection_params()` 单一来源（`--check [7]`）。

### 3.3 实测数据（`--check [16]/[17]`，完整矩阵见 `artifacts/18-click-fix.md`）

| 类别 | 帧 | 8 模板取最大的 NCC |
| --- | --- | --- |
| 正样本（1280×720 标题帧，按钮可见） | 9 张，**leave-one-out**（剔除该帧自己裁出的那张模板） | **min 0.676 / median 0.905 / max 0.966** |
| 负样本：黑屏过渡 | `02.10.16.166`（mean 2.8/std 16.0） | 0.282 |
| 负样本：白色警告页（有文字） | `02.10.18.652`（mean 251.5/std 22.1） | 0.000 |
| 负样本：白色公告页（有文字） | `02.10.17.163`（mean 246.9/std 32.0） | 0.158 |
| 合成纯白 / 纯黑 / 纯灰 | — | 0.000 |

→ 取 **0.5**：正样本侧余量 **+0.176**、负样本侧余量 **−0.218**；`--check [20]` 断言
`negative_ncc_max(0.282) < threshold(0.5) < visible_ncc_min(0.676)`。

### 3.4 模板职责被收窄（重要）

模板现在**只用于「排除加载态之后定位按钮 / 决定点击中心」**，**不再承担「按钮消失 = 进入游戏」的判定**
（后者由 §5 的「点击后观察到加载态」负责；`run()` 已不再调用以「按钮不可见」为成功条件的旧复检）。
原因：`TOUCH TO START` 是半透明横条、透出持续轮播的立绘，同一几何下分数天然在 0.65~1.0 摆动，
把它当「存在性判据」必然临界。

### 3.5 几何固定方式（替代「多尺度模板」）

`controller.screenshot_target_long_side = 1280`（替代旧的 `screenshot_target_short_side = 720`）：
短边 720 只在 16:9 时给出 1280×720，窗口比例一变宽度就跟着变；**长边固定 1280 后，横屏窗口的模板坐标系宽度恒为 1280**，
16:9 时仍是 1280×720（与旧值逐像素一致），按钮 x 坐标不再漂移。
另在点击前加**几何守卫** `wait_target_geometry()`（`geometry_wait_sec=15`）：截图目标不收敛到 1280×720±4 就
**本轮禁用点击**（`action` 覆盖为 `DoNothing`），绝不把 1260 宽帧的识别框按 1280 坐标系点出去。

> 旧 1230 / 860 几何素材（`03-after-click-720p.png` 等）仅作历史素材保留，不再参与模板/roi/点击坐标。
> `--check [10]` 是**自匹配管道自检**（喂 `debug/screencap/02.11.27.757_InGameSuccess.png` = v8 母图，
> NCC=1.0 必然命中，**不证明识别能力**）；真实帧能力由 `[16]` 覆盖。任务 #5 实跑仍应用
> `post_screencap()` 抓首帧复核几何，几何再变就用 `tools/make_template.py` 重裁并重测 roi/阈值。

`03-after-click*.png` / `03-main-frame-*.png` 经核对**仍是标题画面**（`TOUCH TO START` 可见），
**严禁裁作主界面正向模板**。★#22 已用 `in_game_probe_*_unverified.png` 的真实主界面帧裁好 3 个模板
并填入 `in_game_marker_template`（不再是 `null`），见 §12.7。

## 4. DbgController 缺口与离线验证方式（#4 reviewer 关注）

team-lead 原要求用 `DbgController` 喂图做离线验证，但 **maafw 5.15.0 wheel 的 `maa/bin/` 下没有
`MaaDbgControlUnit.dll`**（实测 `DbgController(...)` 直接抛 `RuntimeError: Failed to create dbg controller`，
日志 `Failed to load library [MaaDbgControlUnit]`）。本实现改用 wheel **自带的 `MaaCustomControlUnit.dll` / `CustomController`**：
`_make_static_tasker()` 构造一个 `StaticController`（输入回调仅内存记录、不作用于真实窗口）+
**真实 `resource/` bundle**（与实跑完全相同的 4 模板/roi/threshold）+ 注册自定义识别，供两处离线验证复用：
- `[10]` `offline_template_match_check()`：喂主模板母图跑最小 pipeline → **自匹配管道自检**（只证明链路跑通）；
- `[16]` `real_frame_button_visibility_check()`：用**真实的 `make_button_visible_fn`** 在 12 张可见帧 + 4 张加载态
  真实帧 + 3 张纯色帧上断言，输出真实帧 NCC / 命中 box / button_visible 判定（任务 #9 要求的真实帧证据）。

> **★自检性质（如实理解）★** `[10]` 喂的是模板裁剪母图，`NCC=1.0`、`box=(500,578,230,30)` 是必然自匹配值，
> **只证明真实 bundle 能加载 + 识别/点击链路可用**，不得当作命中验证通过或暗示真实帧可用。
> 真实帧识别能力由 `[16]` 覆盖。两者都完全离线、不启动游戏、不受权限限制、不点击真实窗口。

另已读框架源码
`source/MaaFramework/Task/Context.cpp:419-428` 确认 `override_pipeline_once` 以现有节点 `default_result` 为基底，
故 `post_task` 的 `pipeline_override` 是 **MERGE** 语义，部分字段覆盖不会丢失 `next`/`action`/`on_error`。

## 5. 成功判定链与「过渡态会误触发裸 inverse」的风险（#4 阻塞级检查项）

### 风险（实证缺陷）
`click_to_start=true` 分支里 `InGameSuccess` 用 `inverse:true` 判定「`TOUCH TO START` 消失」。点击之后游戏必然进入
加载态：surveyor 实测启动期就有 **白屏 → 黑屏 → 白屏 → 标题画面**（约 10s），点击后的加载可能更久。加载态时按钮
自然不存在 → **裸 inverse 会立刻命中，把「还在加载」误报成「登录成功」**。**静止的亮色/白色加载画面、淡出帧**同样会误触发；
且 3.1 协议明确**纯色 / 低方差区域上「画面静止」判定不可靠**，只挡黑屏的 `[JumpBack]` 挡不住白屏。
另外**标题画面背景角色立绘持续轮播**（surveyor 实测 stat 91~162 波动、全图 diff 37~76）——**全图差分的
`pre/post_wait_freezes` 或全图稳定判定会被轮播干扰而永远不稳定或误判**，故本实现已把全图 `wait_freezes` 全部移除。

### 规避（★#21 三态判定 + 加载页模板★）
1. **前置状态**：必须先有 `TitleScreenDetectedClick`（TemplateMatch 命中 `TOUCH TO START`）→ 才点击 → 才判定，
   不是一上来就 inverse。
2. **加载态排除：统计判据 + 真实加载页模板**（`LoadingScreenReco`）：
   **黑屏(mean<45) / 纯白屏(mean>220 且 std<12) / 低信息量纯色(std<8)** **或**
   **命中 `Now Loading` 模板（半分辨率灰度 NCC ≥ 0.6）** 任一即加载态；
   `[JumpBack]HandleLoadingScreen` 放在 `WaitInGame.next` **第一位**（优先于 `InGameSuccess`），命中即回跳等待。
   - ★#18★ 白屏必须带 `std<12`：否则亮标题帧（mean 239.7/std 23.1，按钮可见）会被判加载 → 亮场永不点按钮。
     真白屏纯色 std≈0~5，白色警告页/公告页 std 22~32，可分。黑屏**不加**方差约束（真黑屏帧 std=16.0）。
   - ★#21★ 用户实测的加载页是**淡蓝渐变 + 右下 `Now Loading...`**（归一化后 mean≈237/std≈25），统计判据**全部 miss**
     （实测 18 次 `ret=false`）。故新增模板判据：`resource/image/loading_now.png`（255×39，裁自用户素材
     归一化到 1280×720 后的 `(895,657,1150,696)`），roi 内滑窗取最大 NCC，**实测 0.872**；
     标题帧 ≤0.270、真黑屏 0.167、白色警告页 0.000 → 阈值 0.6（余量 +0.27/−0.33）。见 §12。
3. **点击后 20~30s 持续采样，三态判定（互斥）**：`post_click_verify()` 每 1s 取一帧，逐帧记录
   亮度/方差/加载态/按钮可见性，并**同时**用主界面模板做正向匹配：
   - `OK_MARKER`（**确认成功**，退出码 0）：正向主界面模板命中；
   - `OK_DEGRADED_UNVERIFIED`（**疑似成功，退出码 3**）：按钮连续 20s 不可见 + 当前非加载态 + 进程存活，
     但**没有**正向标志 → 日志 WARN 明确「未经正向标志确认」，**不作为成功返回**；
     （若 pipeline 已观察到加载页 `loading_seen_after_click=True`，说明确实离开过标题，缺席门槛降到 5s）
   - `NOT_CONFIRMED`（**失败**，退出码 2）：点击未成功 / 仍停在标题 / 窗口结束仍无法确认。
   窗口到期时若**仍在加载页**，自动每次延长 30s，最多 90s（加载页可持续数十秒）；点击后进程/窗口消失
   （实测点击后客户端会重启）按 `SESSION_LOST` 重连，不计判定失败。
4. **单帧「按钮消失」永不作为成功依据**：实测点击后 2~6s `InGameSuccess` 的裸 inverse 必然命中，但
   `10:26:11` / `10:28:18` 的 screencap **仍是标题 LOGO**；`10:29:04` 按钮消失后 `10:29:06` 又出现。
   所以必须有「连续缺席 + 非加载 + 正向证据」三重条件。
5. `post_delay 1500ms` 作辅助；**已移除全图 `pre/post_wait_freezes`**（立绘轮播会让全图差分永远不稳定）。
6. **取证留图**：判定时刻（marker 命中 / 疑似 / 仍标题 / 超时 / pipeline 未达标志）一律把**原始 1280×720 帧**
   存到 `debug/screencap/in_game_probe_<时间戳>_<tag>.png`（只新增文件，不覆盖已有帧），
   供未来主界面布局变化时补裁/重新校准模板（#22 的首批模板即取自这些留图）。

### ★三态判定与结果码（#21 定稿，互斥）★

| 结果码 | 终端文案 | 含义 | `run()` 返回 | 退出码 |
| --- | --- | --- | --- | --- |
| `OK_MARKER` | `结果: 成功（已确认进入主界面）` | 正向主界面模板命中 | `True` | **0** |
| `OK_DEGRADED_UNVERIFIED` | `结果: 疑似成功（已离开标题并进入加载/游戏，但未确认主界面）` | 点击成功 + 按钮连续 20s 不见 + 非加载 + 进程存活，但无正向标志 | `False` | **3** |
| `NOT_CONFIRMED` | `结果: 失败（未离开标题画面）` | 点击未成功 / 仍停在标题 / 窗口结束仍无法确认 | `False` | 2 |

> **★退出码 3 不是失败，是「待确认」★**
> - `0` = 成功（有主界面正向证据）；`2` = 失败（确实没离开标题）；`3` = **疑似成功，未确认**。
> - 退出码 3 时终端会紧接着打印 `debug/screencap/in_game_probe_*.png` 的路径，并提示「挑一张主界面帧裁模板填入
>   `config.recognition.in_game_marker_template`」，填好后下次即可输出 0。
> - `OK_DEGRADED_UNVERIFIED` **绝不**当成功返回（`run()` 返回 False），但也**不是**「没进去」：
>   它的前提是「点击已生效、按钮连续 20s 不见、画面非加载、进程存活」。
> - ★#22 更新★ `in_game_marker_template` **已填入** 3 个真实主界面模板（见 §12.7）：主界面出现即
>   `OK_MARKER`/退出码 0；退出码 3 现在只表示「按钮消失但三类主界面常驻 UI 一个都没匹配到」
>   （例如换了主界面皮肤/布局），此时可按留图补裁模板。
> - `debug/screencap/in_game_probe_*.png` 是本次 #22 裁模板的素材来源，也是未来补裁的可靠来源。

> `artifacts/03-after-click-720p.png`、`10:26~10:29` 的 6 张 `debug/screencap/*_InGameSuccess.png`
> 经逐张目视核对**全是标题画面**（LOGO 期 / 点击后放大淡出期），**严禁裁作主界面模板**。

### 可插拔接口（主界面模板）
模板（文件名或数组）填到 `config.recognition.in_game_marker_template`，每模板各自的阈值/roi 填
`in_game_marker_threshold` / `in_game_marker_roi`（单值广播或与模板等长的数组一一对应）。
**★#22 起 `build_pipeline_override()` 不再切换 `InGameSuccess` 的 `inverse`★**：该节点恒为
`inverse=true` + 按钮模板，只作「离开标题」前置信号（否则 `OK_DEGRADED_UNVERIFIED`/exit 3 不可达）。
权威正向判定在 Python 侧：`build_marker_params()` 规范化数组并做文件存在性硬守护，
`make_marker_hit_fn()` 逐模板用各自 roi/threshold 调 MaaFW `TemplateMatch`，
`post_click_verify()` 任一命中**且当前非加载态** → `OK_MARKER`。
★#23/#25★ `run()` 在每轮 pipeline 前/门控前用 `direct_marker_confirm()` 直判一次，绕开
`sink.is_success` 门槛（修复 SESSION_LOST 重连后 `OK_MARKER` 不可达）；★#25★ 已删除
「点击前不直判」守护，改为**每轮都执行**（覆盖二次运行 / `--skip-launch` / 残留窗口
已在主界面），详见 §12.7。

## 6. 点击目标（★#18：改用 ClickCenter 点框中心，不再用 `target:true` 随机点★）

`TitleScreenDetectedClick` 现在是 `"action": "Custom"` + `"custom_action": "ClickCenter"`：
`ClickCenterAction` 取 TemplateMatch 实际命中框的**中心**
（`cx = box.x + box.w//2`、`cy = box.y + box.h//2`，当前框 `(468,576,325,23)` → 点 `(630,587)`），
再 `controller.post_click()`。`build_pipeline_override` **不注入任何绝对坐标、也不写 `target`**。

> **★为什么撤掉 `target:true`★** 它的背后是 `Actuator::rand_point()`（`source/MaaFramework/Task/Component/Actuator.cpp:145-176`）：
> 框内**正态分布随机点**（σ=尺寸/3、8 次拒绝采样），实测点击落在 `(673,580)`——距框上沿仅 2px。
> 按钮半透明横条只有 23px 高，随机点贴边有踩空风险。`ClickCenter` 精确点中心，且 `--check [20]` 会**真实调用**
> 该动作断言「点击坐标落在框内且等于框中心」。
>
> **仍绝不用母图绝对坐标**：运行时帧 1280×720、模板母图 860×720，写死坐标必然错位。

> **提醒 #5**：本阶段未真实点击。实跑**必须用户手动在管理员终端执行**（见 §1 硬性前置），并验证点击确实生效
> （观察是否真的进入游戏 / 主界面是否变化）；若 Unity 忽略消息式输入，把 `controller.mouse` 改回 `Seize`。

## 7. 进程存活 / 重启兜底参数（`artifacts/06-diagnosis.md` §5 第 4 条）

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `window_poll_interval_sec` / `process_check_interval_sec` | 2.5s | 轮询 2~3s |
| `window_poll_timeout_sec` | 20s | 启动确认超时，覆盖约 10s 的白/黑屏加载 |
| `stable_alive_sec` | 60s | 窗口连续存活 ≥60s 判「本次启动可用」——**仅作重启决策/确定性失败标注的观测口径，非登录前置等待、不是长期保活** |
| `restart_wait_sec` | 12s | 进程消失后等 10~15s 再重启（等 conhost / UnityCrashHandler64 子进程退出、句柄释放） |
| `max_restarts` | 2 | 最多重启 2 次（合计最多 **3 次启动**） |
| `start_rate_window_sec` / `max_starts_per_window` | 600s / 3 | 5~10 分钟内不超过 3 次启动（避免触发 GameGuard 风控） |
| `in_game_stable_sec` | 7s | 连续稳定 5~10s 成功复检 |

第 3 次仍在相同时间窗退出 → 判定 **确定性失败** `FAIL_DETERMINISTIC`，**停止重试并上报**，不无限循环。
每次重启都记录退出时间点、连续存活时长、原因，并保留日志（`debug/maafw.log`）与截图（`debug/on_error/`、`debug/screencap/`）。
进程退出经 `monitor_job` 独立分支处理，**不会误报成「模板识别失败」**。
**P1-8 受控点击**：`run()` 维护 `click_count`，单次 `run()` 内最多 `flow.max_clicks=2` 次（首次 + 未观察到加载态时允许再点 1 次）；
达上限、或**几何未收敛**时把 `TitleScreenDetectedClick.action` 覆盖为 `DoNothing`，**不无限重试**
（`--check [18]` 源码级确认，且断言上限 ≤2）。

> **★重要语义★** 游戏自退时长不固定、实测约 **3~6 分钟**，形态是**静默主动退出、非崩溃**。
> 进程存活检测必要，但**有限重启只保证「至少一次成功启动并完成短动作」，对长期驻留无意义**——不要写成长期保活逻辑，
> 不要以「游戏长期驻留」为登录成功前提（9~12s 即到标题画面，尽快完成识别与点击即可）。
> 若根因是静态令牌过期，每次 `os.startfile(<lnk>)` 携带**同一份令牌、不刷新**，必然再次自退——
> **绝对不要写任何令牌刷新逻辑**；用户选择仍是只用现有 `.lnk`。启动命令集中在 `launch_game()` 单一函数，
> `launch.mode` 锁定 `lnk`，无 DMMGamePlayer / 协议 / 令牌转发分支。

## 8. 安全合规

- 只保存 `.lnk` 的**路径**，绝不解析或复述其内嵌的 `pf_access_token` 等令牌值；无账号密码。
- 不用 `subprocess.Popen` 直接拉起 `imasscprism.exe`（会丢 `.lnk` 的认证参数）。
- 不读取凭据/Cookie/密码管理器；除 `maafw` 外不下载安装任何东西；不修改游戏文件与配置；不 commit/push。
- **OCR 目录约束（P0 守护）**：MaaFramework 的 `post_bundle` 遇到「存在但缺三件套的 `resource/model/ocr/`」
  会整体失败（`OCRResMgr::lazy_load` 报 `model file not found`）。默认 template 方式下**不要创建该目录**，
  仅保留 `resource/model/.gitkeep`；启用 OCR 前先补齐 `det.onnx` + `rec.onnx` + `keys.txt` 再改 `method`。
  此约束由 `--check [14]`（真实 bundle `post_bundle` 必须成功）与 `[5]`（空目录检查）守护。
- 本阶段（#3 / #9）**未启动游戏、未点击真实窗口**，仅安装、编码与静态自测（含真实帧校准，均来自已有 artifacts 素材）。

## 9. 如何启动（任务 #11）

### 为什么需要启动脚本
`maafw` 装在 WorkBuddy 内置 Python（`C:\Users\Anorak\.workbuddy\binaries\python\versions\3.13.12\python.exe`，
Python 3.13.14）里，而用户终端是 **conda base**（`D:\anaconda\python.exe`，Python 3.13.5）——两者不是同一个解释器。
直接 `(base) PS> python .\agent\main.py` 会报 `ModuleNotFoundError: No module named 'maa'`。
其余系统 Python（C:\Python314 3.14、D:\Python\*、AppData Python313）经逐个验证也都没有 `maa`。

### 两种用法
- **① 实跑**：右键 `agent\run.bat`（或 `run.ps1`）→ **「以管理员身份运行」**（由用户手动完成；脚本不自行提权、不弹 UAC）。
  见 §1 硬性前置：游戏 ELEVATED，未提权时连 `SendInput`/`Seize` 真实鼠标都无效。
- **② 静态自测**：在管理员终端里执行 `agent\run.bat --check`（或 `.\run.ps1 --check`），
  应打印 18 项自测、`静态自测全部通过`、退出码 0；不启动游戏、不点击真实窗口。

脚本会**自动选对解释器**：先探测当前 PATH 上能 `import maa` 的 python，再回退到上面记录的 WorkBuddy 内置解释器绝对路径，
最后把**所有参数透传**给 `agent\main.py` 并保留退出码。两个脚本都用 `chcp 65001`（UTF-8），
中文提示放在 UTF-8 文件 `msg_install.txt` / `msg_admin.txt` 里经 `type`/`Get-Content` 显示
（批处理主体刻意保持纯 ASCII：GBK 中文在 `IF` 块内 echo 会因汉字低字节撞上 cmd 元字符而解析出错）。

### 缺 maafw 时
脚本打印（**必须带官方源**，本机 pip 全局指向清华镜像、无此包）：
```
python -m pip install maafw -i https://pypi.org/simple
```

### ★推荐长期做法★
WorkBuddy 内置解释器路径属会话内置/临时路径，**可能随会话变化**，不宜长期依赖。建议把 maafw 装进用户的 conda base：
```
conda activate base
python -m pip install maafw -i https://pypi.org/simple
python -c "from maa.library import Library; print(Library.version())"
```
装好后，脚本的「先探测 PATH」逻辑会优先命中 conda base 的 python，之后即使内置路径失效也能正常工作；
也可直接在 `(base)` 终端用 `python .\agent\main.py --check`。脚本对两种环境都兼容。

> 说明：脚本仅做权限**提示**，绝不自行提权或触发 UAC；不含任何令牌/账号值；不改 `main.py` 逻辑；不引入新依赖。
> `SCSP_NO_PAUSE=1` 可跳过结尾 `pause`（供自动化/自测使用；正常双击仍保留暂停以便查看输出）。

## 10. 窗口「被放大」根因与修复（任务 #14，175% DPI）

### 根因（已由 #13 实测 + #14 代码复核坐实）
游戏窗口启动时是 **2268×1328 @ (-130,-102)**（与基线一致，仅左上越界到屏幕外）。但 `controller.screencap=Foreground`
（= `DXGI_DesktopDup_Window | ScreenDC`，见 `define.py`）的两个截图单元
`DesktopDupWindowScreencap.cpp:18,23`、`ScreenDCScreencap.cpp:16,21` **每次截图都调用**
`ensure_window_on_screen()`（`Screencap/HwndUtils.hpp:60-197`）与 `ensure_foreground_with_cooldown()`。
`ensure_window_on_screen` 里**像素空间混用**：
- 框架 `connect()` 设了 `SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)`（`Win32ControlUnitMgr.cpp:53`），
  故 `GetWindowRect` / `ClientToScreen` / `GetMonitorInfo(rcWork)` 返回**物理像素**；
- 但 `GetClientRect` 返回**窗口自身 96-DPI 逻辑尺寸**（不随调用者 DPI 缩放）。
- 于是 `frame_right = window_rect.right(物理) - client_origin.x(物理) - client_rect.right(逻辑)` 被虚高；
  `new_window_w = client_w(逻辑) + frame_left + frame_right(虚高)` 逐轮变大，`SetWindowPos` 再放大一档 → **正反馈**。
- 本机 175% 缩放（`AppliedDPI=168`，实测 `GetDpiForSystem()=168`）+ 游戏进程 DPI-unaware（manifest 无声明）放大该效应。
- #13 实测：48 次调用，`new_window_w` 2268→2592、`h` 1328→1594，最终 **2589×1585**（+14.2%/+19.4%），
  原始截图帧峰值 3132×1780，`vision/` 出现 16 种宽度（1261~1660）→ 截图几何漂移，连带影响基于 1230×720 的模板识别。

### 修复（本任务已实施，仅改 `agent/`）
- `main.py` 新增：`init_dpi_awareness()`（本进程设为 PerMonitorV2，实测返回 `PerMonitorV2`）、
  纯函数 `compute_prealign_rect()`、`get_monitor_work_rect()` / `get_window_rect_px()` / `set_window_pos()`、
  `prealign_window()`。
- **连接前预调整**：`build_session()` 在创建 `Win32Controller` **之前**调用 `prealign_window(win.hwnd, cfg)`，
  用 DPI-aware `SetWindowPos` 把窗口**一次性移进主屏工作区**（默认 `SWP_NOSIZE` 仅移动、不改尺寸），
  使 `ensure_window_on_screen` 判 `need_change=False` 直接返回，**切断正反馈**。
  - 本地实测：主屏 `rcWork=(0,0,2560,1516)`（物理）；基线 `(-130,-102,2268,1328)` → 预调整 `(0,0,2268,1328)`
    （尺寸不变、完全在工作区内）；按框架判断逻辑模拟 **`need_change=False`** ✓。
- 配置 `config/settings.json` 的 `window.prealign`（`enabled=true` / `desired_size=null`）：
  `desired_size=null` = 保留当前尺寸仅按工作区裁剪（**绝不放大**），需强制恢复原尺寸时填 `[2268,1328]`。
- `run()` 开头调用 `init_dpi_awareness()` 并记日志；`--check [19]` 新增纯函数单测（7 例 + 基线尺寸不变式 + 接线守护）。

### 175% DPI 注意点
- 预调整**只在连接前做一次**；不得「每次截图后补偿」（会与框架每次 `screencap()` 的 `ensure_window_on_screen` 拉锯抖动）。
- 默认用 `SWP_NOSIZE` 仅移动，规避「DPI-aware 调用者对 DPI-unaware 目标窗口 SetWindowPos 的尺寸换算歧义」；
  只有当前尺寸超出工作区时才 resize（恢复路径）。
- 若 a 仍触发修正，回退方案 b：把 `controller.screencap` 换成**不调用 `ensure_window_on_screen`** 的方式
  （`GDI`/`DXGI_DesktopDup`/`FramePool`；`Foreground` 的 8|32 两个都会调；`PrintWindow` 已知全黑不可用），
  但换方式须实跑验证截图非黑、模板可命中。

### 仍需实跑验证（#5 / 后续）
- 连接后框架日志里 `ensure_window_on_screen` 的 `new_window_w/h` 是否**不再递增**（应停在 2268×1328 或仅 1~2 次同值移动）；
- `Client rect out of bounds` 是否消失、原始帧是否稳定在 ~2240×1260、截图目标是否稳定 **1280×720**；
- 模板已由 #18 用**修复后会话**（02:10，raw 稳定 2240×1260）的真实 1280×720 帧重裁，见 §3 / §11。

## 11. #18：窗口置前 / 模板几何 / 成功判定（三个根因与修复）

### 11.1 根因 A：点击未生效 = 窗口始终不是前台
本次会话 `02:10:23~02:11:36` 每次截图都打 `Failed to ensure foreground window before screencap`
（`source/MaaWin32ControlUnit/Screencap/DesktopDupWindowScreencap.cpp:19`），`SetForegroundWindow` 一直被前台锁拒绝；
`Seize` 的 `touch_down/touch_up` 每次都走 `ensure_foreground()`
（`SeizeInput.cpp:69,136` → `Base/ForegroundUtils.h:11-35`），同样失败 → 点击坐标算得对、但鼠标事件没落到游戏上。
**修复**：`force_foreground(hwnd)` 用 `AttachThreadInput(前台线程, 本线程, TRUE)` + `BringWindowToTop` +
`SetForegroundWindow` + `SetFocus`（用完 detach），**并校验 `GetForegroundWindow()==hwnd` 记日志**；
`build_session()` 连接前调用一次、`run()` 每轮 pipeline 前再调一次。
`--check [20]` 断言 `AttachThreadInput`/`GetForegroundWindow` 存在、且置前调用在创建 `Win32Controller` 之前。

### 11.2 根因 B：假成功 = 裸 inverse + 模板/阈值临界
- `InGameSuccess` 只用「按钮未匹配」判成功，而帧几何/模板外观都在临界 → 真标题帧被判「按钮消失」→ 假 `OK_DEGRADED`。
- 几何问题已在 §3.1 更正（1510 是调试图拼接的模板缩略图，真实 target 只有 1280×720；模板本身按 1280 校准）。
- 真正让「按钮消失」不可信的是**半透明按钮透出轮播立绘**，同几何下分数在 0.65~1.0 摆动。
**修复**：`run()` 不再以「按钮不可见」为成功条件，必须
① 点击后**观察到加载态**（`sink.loading_seen_after_click`）或 ② 正向主界面模板命中；
否则 `NOT_CONFIRMED`。`--check [20]` 源码断言「`confirmed = bool(sink.loading_seen_after_click) or bool(marker_tpl)`」
且 `run()` 内**不得再出现** `confirm_in_game_stable(`（旧函数以按钮不可见为成功条件）。

### 11.3 根因 C：白屏加载阈值把亮标题帧吃掉
旧 `is_loading_frame` 规则「`mean>220` 即白屏加载」把 `02.11.05.315`（mean 239.7/std 23.1、按钮清晰可见）
判成加载态 → `[JumpBack]HandleLoadingScreen` 抢占节点 → **亮场期间永远不点按钮**。
**修复**：白屏加 `std < white_std_max(12)`；黑屏维持 `mean<45`（真黑屏帧 std=16，加约束会漏判）。
`--check [17]` 用真实帧断言「亮标题帧/白色警告页/白色公告页 → 非加载态；纯白/纯黑/纯灰/真黑屏过渡 → 加载态」。

### 11.4 输入方式对比（一行切换，`config/settings.json` 的 `controller.mouse` / `keyboard`）

| 值 | 枚举 | 需要前台？ | 本期实测 | 适用 |
| --- | --- | --- | --- | --- |
| `Seize` | 1 | **是** | 上次会话因窗口非前台 62 次失败；已加 `force_foreground` 修复，**待 #5 实跑确认** | 默认；真鼠标最接近人手 |
| `PostMessageWithCursorPos` | 64 | 否 | 未跑（本任务离线） | 提权后若 `Seize` 仍不生效的首选备选 |
| `PostMessage` | 4 | 否 | 未跑（Unity 是否响应未验证） | 次选 |
| `SendMessage` / `SendMessageWithCursorPos` | 2 / 32 | 否 | 未跑 | 兜底 |

> 前提不变：**宿主未提权时消息式输入会被 UIPI 静默丢弃**（§1）。截图方式同理可一行切换。

### 11.5 改动清单

| 文件 | 改动 |
| --- | --- |
| `agent/main.py` | 新增 `force_foreground()`（AttachThreadInput 置前+校验）、`wait_target_geometry()`（点击前几何守卫）、`ClickCenterAction`（点框中心）、`NodeEventSink.clicked/loading_seen_after_click`、`confirm_not_loading_stable()`、`FAIL_NOT_CONFIRMED`；`is_loading_frame()` 加 `white_std_max`；`build_session()` 改 `set_screenshot_target_long_side(1280)` 并在连接前后置前；`run()` 改为「观察到加载态/正向模板」才判成功、点击上限 2、几何未收敛禁用点击；`_load_real_frame_bgr()` 支持 `debug/` 下 PNG/JPG；`--check` 修 [5]/[7]/[16]/[17]/[18]/[10]，新增 [20] |
| `agent/config/settings.json` | `controller.screenshot_target_long_side=1280` + 几何守卫参数 + 输入方式取值表；`template.threshold=0.5`、`crop=[468,576,793,599]`、8 模板；`calibration` 全部改用真实 1280 帧并记录「不可分结论作废」；`loading_frame.white_std_max=12`；`flow.max_clicks=2` |
| `agent/resource/pipeline/login.json` | 阈值 0.5、`HandleLoadingScreen` 增加 `white_std_max`、点击节点改 `Custom/ClickCenter`（去掉 `target`） |
| `agent/resource/image/*.png` | 8 张 325×23 模板全部重裁（v1 换掉被控制台污染的源帧） |
| `agent/tools/make_template.py` | 新增 `decode_jpeg()` / `decode_image()`（Windows GDI+，无 Pillow/OpenCV 依赖），供 `--check` 直接读 `debug/vision/*.jpg` |
| `agent/README.md` | §3 重写、§5/§6/§7/§10 更正、新增 §11 |

### 11.6 自测输出

`python agent/main.py --check` → **退出码 0，全部通过**（[1]~[14]、[16]~[21]；[15] 为历史跳号）。
关键结果：8 模板均 325×23 且 crop ⊂ roi；真实帧 10/10 可见帧 `button_visible=True`（含亮标题帧）、
6/6 加载态+纯色 `False`；ClickCenter 实测点击 `(630,587)` 落在框 `(468,576,325,23)` 内且为框中心；
阈值 0.5 ∈ (0.282, 0.676)；[21] 用虚拟时钟验证「加载必超时、退出必失败、稳定满 7s 才成功、亮帧不误判加载」。

### 11.7 仍需 #5 实跑验证（不编造结论）
- `force_foreground` 是否真的拿到前台（看日志 `force_foreground: 目标 hwnd=... 置前成功`），
  `Failed to ensure foreground window before screencap` 是否消失；
- 点击后是否真的进入加载（`loading_seen_after_click=True`）；若从未观察到加载但确实进了游戏，
  以 `in_game_marker_template` 正向模板为准（★#22 已配置 3 个，见 §12.7），绝不放宽成裸 inverse；
- 截图目标是否稳定 1280×720（`几何守卫` 日志）；`Seize` 若仍点不动，按 §11.4 切 64/4 并记录效果；
- ★#22 已完成真实主界面模板接入与离线 NCC 校准★，但**仍需管理员终端实跑**确认端到端
  `OK_MARKER`/退出码 0；若实跑主界面布局与素材不同，按 `in_game_probe_*` 留图补裁并重新校准阈值。

## 12. #21/#22/#23/#25：进了游戏却判失败 → 三态判定 + 取证留图 + 加载页模板 + 主界面正向模板 + 重连直判 + 收口

### 12.1 真实证据链（10:25~10:29 的 4 次实跑）

`debug/agent.log` 显示 4 轮全部 `NOT_CONFIRMED`，但**每轮都成功点击**（`ClickCenterAction (630,587) 成功`），
且 `force_foreground` 全部「置前成功」、`几何守卫 1280x720 一致` —— 说明 #18 的置前/几何/点击修复**已经生效**。
失败点在判定：

- `HandleLoadingScreen` **一次都没命中**（`LoadingScreenReco` 18 次 `ret=false`）；
- 点击后 2~6s `InGameSuccess` 的裸 inverse 就命中，但 `debug/screencap/` 里
  `10:26:11`、`10:26:41`、`10:27:37`、`10:28:18` 四张全是**标题 LOGO 画面**（无 TOUCH），
  `10:29:05`/`10:29:09` 是点击后「TOUCH TO START」放大淡出的过渡帧；
- 时间线（`HandleLoadingScreen` 干净帧逐帧判定）：`10.29.02`/`10.29.03` 按钮仍 `Y`、`10.29.04` 消失、
  `10.29.06` 又 `Y`、`10.29.09` 消失 —— **单帧「按钮不见」毫无判定价值**。

### 12.2 为什么 `loading_seen_after_click` 永远 false（用户素材给出答案）

用户提供的真实加载页 `artifacts/20-loading-user.png`（实际 **2246×1254**）是
**淡蓝渐变几何背景 + 右下角青蓝色 `Now Loading...`**（归一化后 mean≈237，std≈25）：
既不是纯黑(`mean<45`)、也不是纯白(`mean>220 且 std<12`)、也不是低方差(`std<8`)，
**旧的统计判据覆盖不到**，所以 18 次全部 miss。

新增模板判据（与统计判据**并用**，任一命中即加载态）：

- 素材归一化：原图按宽高分别缩放到 **1280×720** → `artifacts/20-loading-1280.png`（原图不动）；
- 模板：`resource/image/loading_now.png`，在归一化图上**实测** crop `(895,657,1150,696)` → **255×39**；
- 匹配：**半分辨率灰度 NCC**（numpy 滑窗，无 OpenCV），roi `[860,630,340,90]`，单次 ~30ms；
- 实测：加载页 **0.872**；标题帧 0.270 / 0.259 / 0.176（亮标题）；真黑屏 0.167；白色警告页 0.000
  → **阈值 0.6**（正侧余量 +0.27、负侧 −0.33）。

### 12.3 三态判定（`post_click_verify`）

| verdict | 触发条件 | 结果码 | 退出码 |
| --- | --- | --- | --- |
| `MARKER` | 任一主界面模板在各自 roi/threshold 下命中、且当前非加载态（#22） | `OK_MARKER` | 0 |
| `LEFT_TITLE` | 按钮连续 `in_game_absent_sec`(20s) 不可见 + 当前非加载 + 进程存活 | `OK_DEGRADED_UNVERIFIED` | **3** |
| `STILL_TITLE` | 窗口内按钮仍反复可见（含轮播淡入淡出） | `NOT_CONFIRMED` | 2 |
| `TIMEOUT` | 窗口结束仍凑不满连续缺席 | `NOT_CONFIRMED` | 2 |
| `SESSION_LOST` | 点击后进程/窗口消失（实测客户端会重启） | 重连，不计判定失败 | — |

- 采样 1Hz，窗口 30s；到期若**仍在加载页**则每次 +30s，最多 90s（加载页可长达数十秒）。
- 点击后前 3s（`post_click_grace_sec`）忽略按钮可见性，避开残留帧。
- 若 pipeline 已观察到加载页（`loading_seen_after_click=True`），缺席门槛降到 5s。
- **裸 inverse / `confirm_in_game_stable` 均不参与成功判定**（`--check [20]` 源码守护）；
  ★#22★ 加载态帧被显式排除在正向判定之外（`post_click_verify` 的 `not loading and marker_hit_fn(...)`），
  不得把加载态误判为成功。
- 终端文案三态分明：`结果: 成功（已确认进入主界面）` / `结果: 疑似成功（已离开标题并进入加载/游戏，但未确认主界面）`
  / `结果: 失败（未离开标题画面）`；**退出码 3 不是失败，是「待确认」**，并会打印 probe 图路径与补模板提示。

### 12.4 取证留图（下次一定能拿到主界面素材）

`save_probe_frame()` 把**原始 1280×720 截图**（`post_screencap()` 结果，非 vision 调试图）写到
`debug/screencap/in_game_probe_<YYYY.MM.DD-HH.MM.SS.mmm>_<tag>.png`，tag ∈
`verdict` / `marker_hit` / `unverified` / `still_title` / `timeout` / `fail_no_flag`。
**只新增文件**，绝不覆盖/删除 `debug/` 下已有帧（红线）。

### 12.5 自测（`--check` 退出码 0）

- `[21]` 三态单测 5 例：仍标题→`STILL_TITLE`；连续 20s 不见→`LEFT_TITLE`（用时 23.0s）；
  模板命中→`MARKER`（0.0s）；进程消失→`SESSION_LOST`；全程加载→`TIMEOUT`（30s）。
- `[22]` 三态互斥 + `UNVERIFIED` 必须 `return False` + 失败路径留图 + marker 为空不得判 `OK_MARKER`
  + 留图实测（1280×720 PNG、命名正确、只新增）。
- `[23]` 真实加载页 `tpl=0.872 ≥ 0.6` 且 `reason=loading-template`；标题帧/判定帧全 `False`；
  真黑屏与纯色仍由统计判据命中。
- ★#22★ `[24]` 真实 MaaFW bundle 端到端：主界面帧命中 / 标题帧与加载页帧均不命中；
  3 模板各自 roi/threshold 生效；单字符串兼容；marker=`null`/空数组/文件缺失一律禁用正向判定。
- ★#22★ `artifacts/22-guard.py` 离线单测：marker=`null`/空数组/文件缺失时 `post_click_verify`
  喂主界面帧也只给 `LEFT_TITLE`（疑似），绝不 `MARKER`；marker 有效时第一帧即 `MARKER`。
- ★#22★ 可复现脚本：`artifacts/22-ncc.py`（NCC 表）、`22-analyze.py`（差分扫描）、
  `22-crops.py`（候选区三帧对比）。
- ★#23★ `[25]` 把**完整** `build_pipeline_override(cfg)`（与 `run()` 同源）喂给真实
  `tasker.post_task`：7 项覆盖、节点图合法、未抛异常，静态帧下立即 `post_stop` 不等待 pipeline
  结果；补上「含 override 的完整节点图从未被离线真跑」的盲区（P1-1）。
- ★#23★ `[7]/[22]/[24]` 同步改造：断言三数组配置合法、`make_marker_hit_fn` 可用、
  override **不含** `InGameMarker_*` 死节点；直判路径同样受 marker 失效守护；空阈值数组
  回退默认 0.7 不崩溃（P2）。
- ★#23★ `artifacts/24-verify.py` 端到端单测：模拟 SESSION_LOST 重连后
  `sink.is_success=False` → 直判仍命中 → `OK_MARKER`/退出码 0；标题帧/加载页/纯色/marker 失效
  一律不误报；三态 0/3/2 可达。日志 `artifacts/24-check.log` / `24-verify.log`。
- ★#25★ `[24]` 新增 4 组畸形阈值配置（`[0.1]*3` / `[0.5]*3` / `[0.1,0.2]` /
  `[0.1,0.2,0.3,0.4,0.5]`）一律 `build_marker_params=None` → `make_marker_hit_fn=None`
  → 标题帧直判 `False`（阈值下限 0.55 + 长度一致性硬守护，绝不假成功）；当前校准配置仍返回
  3 模板参数。`[22]` 断言「点击前守护已删除」。`artifacts/26-verify.py` 端到端单测覆盖
  `(click_count=0, restarts=0)` + 主界面帧 → `OK_MARKER`/exit 0，以及代表性负样本零误命中。
  日志 `artifacts/26-check.log` / `26-verify.log`。

### 12.6 实跑验证状态与仍需确认（★#25 版本核对★）

**已闭环**：2026-11-07 11:51:08~11:51:36 用户在管理员终端真实实跑成功，
`debug/agent.log:1681-1683` 记录 `点击后判定结论: MARKER | 第 5 次采样命中正向主界面模板（5.9s）`
→ `成功【OK_MARKER】…` → `结果: 成功（已确认进入主界面） [OK_MARKER] 退出码 0`；
开局 `成功判定模式: 【正向模式 OK_MARKER】`（`agent.log:1558`）、几何守卫 1280×720 一致、
`force_foreground` 已是前台、`ClickCenter` 生效。这是本项目**首次端到端闭环**（详见
`artifacts/05-verify.md` 阶段二、`artifacts/26-finalize.md`）。

- ★#25 版本核对（如实）★ 该次实跑 `maafw.log` 会话头为 `11:51:08.392 MAA Process Start`，
  而含 #23 修复的 `agent/main.py` 文件 mtime = **12:05:43**、#23 的 `24-check.log` 生成于
  12:10；且日志走的是 `post_click_verify` 第 5 次采样 MARKER，**全文无「主界面模板直判命中」**。
  → **成功验证的是 #22 版（settings.json 三模板 + post_click_verify 正向判定）；当前含 #23
  修复（两处直判 / 删死节点 / P2）的最终代码尚未经真实实跑**，仅经 `--check` + 离线单测 +
  `review-hy4` 独立复核。本次实跑也**未触发 SESSION_LOST 重连**，#23 直判路径的真实场景仍未覆盖。
- 点击后客户端重启 → `SESSION_LOST` 重连：#23 已修 `sink.is_success` 门槛问题，#25 又删除
  「点击前不直判」守护使直判每轮都执行；但**重连落点仍未经实跑验证**，需后续实跑确认
  直判在重连轮被调用并输出 0。
- 加载页模板只有用户这一张（跨场景/不同加载页未验证）→ 若实跑出现加载页漏判，按 `in_game_probe_*` 重裁或改多模板数组；
- 主界面 3 类常驻 UI 只覆盖当前账号当前版本（`Ver. 2.19.1`）；#25 已用 11:51:36 过曝主界面帧
  补入第 4 张**真正独立**正样本并复测 NCC（见 `artifacts/26-finalize.md`），但跨版本/跨账号/
  跨活动版式泛化仍未验证。若后续实跑主界面布局与素材不同，按 `in_game_probe_*` 留图补裁、
  重跑 `22-ncc.py` 重新校准；**严禁下调 `in_game_marker_threshold` 迁就误判**（阈值下限 0.55，
  标题帧 lb 负样本上界 0.507）。

### 12.7 ★#22/#23/#25★ 主界面正向模板接入（疑似 3 → 确认 0）

#### 素材与逐张判定（一律 1280×720 口径）

`settings.json` 的 `controller.screenshot_target_long_side=1280`，运行时帧 1280×720。
最新 3 张 `in_game_probe_*.png` 里**只有 1 张是主界面**，故额外引入 10:38 帧与用户手动截图，
共得 **3 张主界面正样本 + 多张标题/污染负样本**：

| 帧 | 尺寸 | 判定 | 说明 |
| --- | --- | --- | --- |
| `in_game_probe_2026.11.07-10.48.45.318_verdict.png` | 1280×720 | 标题 | LOGO + `TOUCH TO START` + 立绘轮播 |
| `in_game_probe_2026.11.07-10.49.34.705_verdict.png` | 1280×720 | 标题 | LOGO + 右下加载转圈，过渡帧 |
| `in_game_probe_2026.11.07-10.50.00.294_unverified.png` | 1280×720 | **主界面** | LB/体力/货币、右上四图标、底栏、3rd Anniversary；**模板母图** |
| `in_game_probe_2026.11.07-10.38.30.934_unverified.png` | 1280×720 | **主界面** | 同上 + 台词对话框、货币 16,405 |
| `artifacts/屏幕截图 2026-11-07 103935.png` → `22-manual-1280.png` | 2250×1256 → 1280×720 | **主界面** | 同账号同布局；按宽高分别归一化（比例差 0.7%），用于交叉比对 |
| `10.37.37` / `10.38.05` probe、10:37~10:49 的 `*_InGameSuccess.png` | 1280×720 | 标题 | LOGO 期；其中 10.48.43.676 被管理员 PowerShell 控制台遮挡（污染帧） |

三张主界面帧在 UI 区**逐像素一致**（两两 d12=0.00，脚本 `22-analyze.py`）；手动归一化帧的差异
（d13≈10~30）来自 2250×1256→1280×720 重采样与 0.7% 拉伸失真，非内容变化。差分图
`22-diff-stable.png` 显示：顶部整条（LB/体力条边框/加号/钻石/右上四图标）与底部导航栏为常驻白区；
**对话框、左侧活动横幅、3rd Anniversary 横幅、左下玩家条、中央角色立绘、`20/20`/`16,440`/
`あと25秒` 数值与倒计时均为实时变化内容，一律不作模板**。

#### 三类常驻区域与精确坐标（1280×720）

| 类 | 模板文件 | crop (x0,y0,x1,y1) | 尺寸 | 匹配 roi [x,y,w,h] | 阈值 |
| --- | --- | --- | --- | --- | --- |
| a 右下角导航栏（Live 舞台按钮） | `home_nav_bottom_right.png` | (1080,590,1278,720) | 198×130 | [1040,570,240,150] | 0.55 |
| b 左上角 LB 字样（钻石/数值会变，故只取恒定 logo） | `home_lb_diamond.png` | (10,5,90,60) | 80×55 | [0,0,140,100] | 0.75 |
| c 右上角四图标（相机/圆形/全屏/九宫格） | `home_top_right_icons.png` | (950,5,1278,72) | 328×67 | [910,0,370,110] | 0.60 |

三个模板均从**干净的 1280×720 运行时帧** `10.50.00.294_unverified.png` 裁出
（`tools/make_template.py --already-720 --crop ...`），不用归一化手动帧（避免重采样失真）。
b 类如实取舍：`LB` 字样与紫色钻石图标虽然都恒定，但二者之间夹着**会变的体力/货币数值**，
无法连成一片裁成单一矩形；`LB` 字样已能独立代表该类恒定特征，故不硬造第 4 个模板。

#### 阈值依据（半分辨率灰度 NCC，脚本 `artifacts/22-ncc.py` 可复现）

算法与 `match_loading_template()` 完全一致：灰度半分辨率、去均值、roi 内滑窗取最大归一化
相关系数（等价于 TM_CCOEFF_NORMED 灰度版）；roi 按 1280×720 给出、函数内除以 2。
正样本 = 3 张主界面帧；负样本 = 10 张标题/污染帧 + 真实加载页 + 合成纯黑/白/灰。

| 模板 | 正样本 h1(10:38) / h2(母图) / h3(手动) | 负样本最大 | 负样本均值 | 取阈值 | 正/负余量 |
| --- | --- | --- | --- | --- | --- |
| nav_bottom_right | 1.000 / 1.000 / 0.723 | 0.339（标题 10.49.31） | 0.189 | **0.55** | +0.17 / −0.21 |
| lb_diamond | 0.964 / 0.964 / 0.955 | 0.559（标题 10.38.03） | 0.423 | **0.75** | +0.21 / −0.19 |
| top_right_icons | 0.952 / 0.952 / 0.962 | 0.261（标题 10.38.03） | 0.150 | **0.60** | +0.35 / −0.34 |

`lb_diamond` 负样本偏高（标题帧左上角是汉堡菜单 ☰，低分辨率下与 LB 字形有约 0.52~0.56 的
结构相似），故单独取 0.75；三模板负样本上界差异大，**不沿用未校准的单一 0.7**，改用
`in_game_marker_threshold` / `in_game_marker_roi` 两个与模板等长的数组，每个模板各自阈值与
roi、各自留出 ±0.17 以上余量。h3 对 nav 仅 0.723 是归一化失真所致；实跑为原生 1280×720，
正样本实际为 1.000。

#### 多模板判定逻辑与硬守护

- `build_marker_params(cfg)`：字符串或数组 → 规范化为每模板 `(file, roi, threshold)`；
  `null` / 空数组 / 全空串 / **任一模板文件缺失** → 返回 `None`。
- `make_marker_hit_fn(tasker, cfg)`：`None` 时直接返回 `None`；否则因 `JTemplateMatch.roi` 是单个
  `JTarget`，**逐模板各一次** `post_recognition(TemplateMatch, JTemplateMatch(template=[f],
  threshold=[thr], roi=roi, method=5), frame)`，任一 `hit` 即 `True`。
- `post_click_verify()`：`if not loading and marker_hit_fn is not None and marker_hit_fn(frame)`
  → `MARKER` → `OK_MARKER`/exit 0。**加载态帧显式排除**；实测 3 张主界面帧
  `detect_loading_frame=False`、加载页 `=True(tpl=0.872)`，守护安全。
- `build_pipeline_override()`：**`InGameSuccess` 恒保持 `inverse=true` + 按钮模板**，只作
  「离开标题」前置信号。若切正向，`OK_DEGRADED_UNVERIFIED`/exit 3 将不可达。
  ★#23 P1-1★ 曾生成的 `InGameMarker_<i>` 节点（`next=[]` 且无任何 `next`/`on_error` 指入 →
  孤立死节点、永不执行）已删除：权威正向判定完全在 Python 侧，不需要 pipeline 冗余节点；
  同时消除了「含这些节点的完整 override 从未喂给 `post_task` 真跑」的验证盲区
  （改由 `--check [25]` 覆盖完整 override 注入）。
- **marker 为空/失效时的降级**：`make_marker_hit_fn=None` → 正向判定整条关闭，`post_click_verify`
  最多给 `LEFT_TITLE`（`OK_DEGRADED_UNVERIFIED`/exit 3，疑似成功，**不是失败**）或
  `TIMEOUT`/`NOT_CONFIRMED`(exit 2)；**绝不判 `OK_MARKER`、绝不回退到裸 inverse**
  （模板不匹配 ≠ 成功）。`artifacts/22-guard.py` 对 `null`/`[]`/文件缺失三种情况端到端验证。
- 三态文案与退出码 **0 / 2 / 3 完全不变**；加载页模板、ClickCenter、force_foreground、
  prealign_window 等既有逻辑未改。

#### ★#23/#25★ SESSION_LOST 重连后的「直判」路径（P1-2 修复 + #25 收口）

- **问题**：点击后客户端会重启 → `post_click_verify` 返回 `SESSION_LOST` → `run()` 丢弃会话重连。
  重连后若游戏**直接落在主界面**（按钮不可见），`TitleScreenDetectedClick` 本轮永不命中 →
  `sink.is_success()`=False → 原门控 `continue` → `post_click_verify`（原来唯一能判 `OK_MARKER`
  的入口）永不调用 → 「明明进了游戏却判失败」。#22 把正向判定挂在了「标题→点击→按钮消失」
  信号链之后，重连正好打破这条链。
- **修复**：`run()` 每轮在 ① **pipeline 之前**（含重连后的新一轮开头）与 ② **门控之前**，
  先用 `ctrl.post_screencap().wait().get()` 取一帧，调用 `direct_marker_confirm(frame, cfg,
  marker_hit_fn)`；命中即留 `probe` 图（tag=`marker_hit`）并 `return True, OK_MARKER`(exit 0)。
  `marker_hit_fn` 提前构造到门控之前，供直判与 `post_click_verify` 共用。
- **硬守护**（与 `post_click_verify` 一致）：`marker_hit_fn=None`（marker=null/空数组/文件缺失）
  → 直判禁用；空帧 → 禁用；**加载态帧 → 禁用**（`detect_loading_frame` 复合判据）；标题帧
  实测 3 模板零命中。绝不回退裸 inverse；三态 0/3/2 仍全部可达，`InGameSuccess` 的 inverse 结构未动。
- ★#25★ **删除**原「首次点击前且未重连（`click_count<1 and restarts<1`）不直判」守护：
  该守护在二次运行 / `--skip-launch` / 残留窗口使游戏**已在主界面**时会挡住两处直判，
  而主界面无 TOUCH TO START 可点，最终空转到 `FAIL_MAX_RESTARTS`(exit 2)。复核用真实
  MaaFW 判定器遍历 21 张负样本（标题/污染/加载/纯色）零误命中，模板本身即双保险，
  故直判改为**每轮都执行**。对应单测 `artifacts/26-verify.py` A 段覆盖 `(0,0)` 场景。
- **离线证据**：`artifacts/24-verify.py` A 段模拟 `sink.is_success=False` 的重连场景 → 直判命中
  → 退出码 0；B 段验证标题帧/加载页/纯色/marker 失效均不误报；`--check [25]` 验证完整
  override 注入 `post_task` 成功。

## 13. #39 MV 探索性实跑采集模式（方案 B：只留图不改判定）

> **这不是 MV 自动化。** 观看 MV 六步流程（勘察见 `.workbuddy/artifacts/30-mv-survey.md`）的第
> 2~6 步界面素材为零，无法裁模板、无法确定坐标/是否弹确认框。本模式让用户在管理员终端
> 手动走完六步时，脚本**旁路只读旁观**录下每个界面的真实 1280×720 帧，供下一阶段裁模板
> 与语义确认。脚本不替用户点任何一步、不做任何识别/判定、不改阈值、不改三态、不改退出码。

### 13.1 开关与「默认关闭零回归」铁律

`config/settings.json` 的 `flow` 段新增两项（其余既有键逐字未动）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `flow.mv_explore` | `false` | **默认关闭**。关闭时 `_finish_ok()` 直接 `return True, OK_MARKER`，不截图、不留图、不写任何日志、不改变时序 → 与 #33 基线（两轮实跑 `OK_MARKER`/exit 0）逐字零回归。 |
| `flow.mv_explore_cfg` | 见下表 | 采集参数，仅在开启时读取；关闭时不读不用。 |

| `mv_explore_cfg` 键 | 取值 | 依据 |
| --- | --- | --- |
| `sample_interval_sec` | `0.9` | 派单建议 0.8~1.0s；兼顾控制页可能自动隐藏与采样开销 |
| `max_duration_sec` | `180` | 可配；用采集自己的单调时钟，不受登录 `overall_timeout_sec=300` 影响 |
| `frame_diff_threshold` | `12.0` | 1280×720 按步长 8 降为 160×90 灰度后相邻帧平均绝对差（0~255），滤掉立绘轮播/数值微动、保留整页切换 |
| `max_frames` | `200` | ★#46 上调★ 硬闸，防磁盘爆（单张 1280×720 PNG 约 0.3~2MB）。`32-review.md` V2 定量：MV 视频段每帧都变时 `60` 张仅 54s 即 `stop_reason=max_frames` 提前截断、采不到第 5/6 步；`200` 可覆盖 180s 时长预算 |
| `only_changed_frames` | `true` | 显著变化帧才留；首帧无条件留作差分基准 |
| `expected_width/height`、`geometry_tolerance` | `1280/720/±4` | 识别「截到终端/尺寸不符」的无效帧 |
| `black_mean_max` | `12.0` | 全黑/极暗帧判为无效 |
| `bad_frame_warn_after` | `3` | 连续 3 帧无效才 WARN 一次（不刷屏） |
| `diff_downsample_step` | `8` | 差分降采样步长 |

`recognition.in_game_marker_*` 三数组、`[0.55,0.75,0.6]`、`<0.55` 下限守护、
`controller`、`launch`、三态相关配置**一字未动**；`resource/pipeline/login.json` 10 节点**未改**。
三处 `return True, OK_MARKER` 全部改为 `return _finish_ok(...)`，但 `_finish_ok` 默认分支仍
直接返回 `OK_MARKER`，故 `[22]` 既有断言与成功日志行均不变。

**★#46 命令行开关（首选）★**：`--mv-explore`（`agent\run.bat --mv-explore`，`run.bat` 末行 `%*` 透传全部参数，
已实测）。它在 `main()` 里**仅内存覆盖** `cfg["flow"]["mv_explore"] = True`，**绝不写盘**；落盘默认值恒为
`false`，因此没有「手改配置改错行 / 忘记改回 false / CRLF 编辑器导致整文件 diff」三种风险，git 工作区
始终保持可比对。`--check`（含 `--check --mv-explore`）仍走原始配置，`[26]` 断言不受影响。
**手改 `settings.json` 的方式不再推荐。**

**★#66 采集上限命令行覆盖（大流程必备）★**：单次走路的自动育成可能 5~10 分钟，落盘默认
`max_duration_sec=180` / `max_frames=200` 不够时，用命令行按需放大，**不要手改 `settings.json`**：

| 开关 | 覆盖项 | 取值约束 |
| --- | --- | --- |
| `--explore-max-duration <SEC>` | `flow.mv_explore_cfg.max_duration_sec` | 大于 0 的有限秒数（支持小数，如 `600` / `2.5`） |
| `--explore-max-frames <N>` | `flow.mv_explore_cfg.max_frames` | 整数且 `>= 1` |

例：`agent\run.bat --mv-explore --explore-max-duration 900 --explore-max-frames 800`。
两个开关与 `--mv-explore` 同款机制：在 `main()` 里**仅内存覆盖**、**绝不写盘**（覆盖调用在
`if args.check` 之后，`--check` 路径不受影响），不带开关时行为逐字不变。非法值（`0`/负数/
`NaN`/`inf`/非数字，以及 `frames < 1`）→ `argparse` 打印用法并 **exit 2**，绝不带着非法上限继续跑。
注意留图额度与磁盘：单张 1280×720 PNG 约 0.3~2MB，`800` 张约 0.24~1.6GB。
验收：`--check [31]` + `.workbuddy/artifacts/50-explore-verify.py`（真驱动 `main()`，A~G 段）。

### 13.2 管理员终端实跑命令与操作清单（★#46 两步，命令行开关优先★）

> 完整清单以 `.workbuddy/artifacts/33-fix.md` 为准；本节为摘要。**先做第 1 次默认关闭实跑**
> （验证零回归），再做第 2 次采集实跑；顺序不能反（#33 两轮实跑都在 #39 改动之前，
> 静态证明不替代真实实跑；采集实跑只覆盖开启分支，不覆盖默认关闭分支）。

**第 1 次｜默认关闭，验证零回归（必须，且放在采集之前）**

1. 右键「Windows 终端 / PowerShell」→ **以管理员身份运行**；`cd /d D:\DMM\scsp_MAA`。
2. 执行 `agent\run.bat`（配置保持 `mv_explore=false`、**不带任何开关**）。
3. 确认终局 `结果: 成功（已确认进入主界面） [OK_MARKER] 退出码 0`、日志与 #33 两轮一致 → 回传终端输出。

**第 2 次｜采集**

4. 执行 `agent\run.bat --mv-explore`（等价于 `python agent\main.py --mv-explore`）。
   **不要手改 `settings.json`**。
5. 等终端打印「【MV 探索性实跑采集模式】已启动」与六步提示 → **切到游戏窗口、把管理员终端最小化**、
   保持游戏在前台。手动依次完成六步：① 点右下角 Live → ② 点「观看 MV」 → ③ 点「开始」 →
   ④ 点屏幕唤起控制页 → ⑤ 点右上角最右边的快进键退出 MV → ⑥ 点左下角返回键回到主页面。
6. ★**MV 播放段请尽快完成第 4/5/6 步，不要等 MV 播完**★（视频段每帧都变，留图额度消耗最快；
   第 4/5/6 步界面正是本次采集唯一目的）。采集上限已放宽到 200 张，但回传前请按 0001..N 顺序
   目视剔除重复帧与终端/桌面帧。
7. 走完六步切回终端按 **Ctrl+C**（或等自动结束：180s / 200 张 / 游戏退出）；确认仍 `OK_MARKER`/exit 0。
8. 回传 `agent\debug\screencap\` 下本次 `in_game_probe_*_mv_explore_*.png` **全部**（按 0001..N 顺序）
   + 收尾统计（**含 `stop_reason`**）。★若 `stop_reason=max_frames`，说明额度耗尽、可能没走到第 5/6 步
   → 素材未采全，需重跑★。并附 4 项语义确认（见 `33-fix.md`）。

### 13.3 留图命名、位置与 Foreground 截图风险

- 命名：`in_game_probe_<时间戳>_mv_explore_0001.png`（序号 4 位递增），落在
  `agent/debug/screencap/`，**只新增不覆盖**，便于按序对照六步。
- 收尾统计**必打印 `stop_reason`**（`max_duration` / `max_frames` / `ctrl_c` / `process_gone` / `error`）；
  `max_frames` 与 `error` 额外 WARN「素材未采全、需重跑」。留图清单对每张附灰度 `mean/std`，
  便于按序识别并剔除明显是终端/桌面的帧（不引入终端内容启发式，避免误杀游戏内纯色 UI）。
- `controller.screencap="Foreground"`（DXGI_DesktopDup_Window|ScreenDC）依赖游戏窗口前台。
  用户手动点游戏时窗口自然前台；但**管理员终端被切到前台**时可能截到终端/黑帧。处理：
  观察循环内**不调用 `force_foreground(hwnd)`**（会抢焦点、干扰手动点击）；对空帧 / 全黑 /
  尺寸非 1280×720±4 的帧直接跳过、不存图、不更新差分基准，连续 `bad_frame_warn_after`
  帧才 WARN 一次并提示保持游戏在前台。**不擅自改 `controller.screencap` 全局值**
  （会牵动登录链路与 `--check [3]` 断言）。代价：终端抢前台的那几秒可能丢素材，
  用户按 13.2 第 5 条把终端最小化、保持游戏在前台即可覆盖。
- 结束条件：Ctrl+C（优雅收尾、不吞掉后继续跑）、总时长上限、最大留图数、或
  `monitor.alive()` 为假（游戏自退时立刻收尾并如实提示）。任何情况均返回统计并仍
  `OK_MARKER`/exit 0。

### 13.4 验收（全部离线）

`python agent\main.py --check` → EXIT=0，`[14]` 真实 bundle post_bundle 成功、无 OCR 报错、
`[22]/[24]/[25]/[26]` 全过（`[26]` 含 #46 的 `max_frames>=200` 与 `--mv-explore` 开关断言）；
`.workbuddy/artifacts/31-verify.py` 用合成帧序列驱动 12 组断言（含 #46 新增：
`--mv-explore` 仅内存覆盖不落盘、`max_frames=200` 视频帧序列跑到 `stop_reason=max_duration`）全过。
#45 复核见 `.workbuddy/artifacts/32-review.md`，#46 修复与最终两步实跑清单见
`.workbuddy/artifacts/33-fix.md`；初版实跑清单（`31-mv-explore.md §9`）
**已被 32-review.md / 33-fix.md 取代**。

## 14. #48 实跑结果结构化输出（L2，主 agent 直接读文件判读）

每次**真实实跑**（`agent\run.bat`，不带 `--check`）结束后，脚本在 `agent/debug/` 下自动落两份机器可读结果，
无需用户手动贴日志：

| 文件 | 语义 | 写入时机 |
| --- | --- | --- |
| `agent/debug/last_result.json` | **覆盖写**，"最新一次"实跑的完整结果 | `main()` 拿到 `report_result()` 退出码后、进程退出前 |
| `agent/debug/results.jsonl` | **只追加**，每次一行精简历史 | 同上，逐次追加 |

两份都在已被 git 忽略的 `agent/debug/`，不入库、不影响 diff。

### 14.1 `last_result.json` 字段

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 固定 `1` |
| `timestamp` | ISO 本地时间（秒精度），用于判断结果新鲜度 |
| `duration_sec` | 本次 `run()` 耗时（秒） |
| `exit_code` | 0 / 2 / 3（或异常码） |
| `verdict` | `OK_MARKER` / `OK_DEGRADED_UNVERIFIED` / `NOT_CONFIRMED` / `FAIL_*` |
| `terminal_text` | 终端那句「结果: …」原文（取自最近日志环形缓存，便于核对文案） |
| `mode` | `login` 或 `mv_explore`（读**生效值**；`--mv-explore` 仅内存覆盖也算 `mv_explore`） |
| `login_ok` | 布尔：**登录是否成功**，与「MV 采集是否完整」刻意分离 |
| `config_snapshot` | `mv_explore` / `max_frames` / `max_duration_sec` / `sample_interval_sec` / `in_game_marker_threshold` 只读快照 |
| `key_log_lines` | 最近 30 条日志行数组 |
| 仅 `mode=mv_explore` 时：`stop_reason` | `ctrl_c` / `max_duration` / `max_frames` / `process_gone` / `error` |
| 仅采集：`frames_taken` / `frames_saved` / `frames_skipped_invalid` | 取帧数 / 留图数 / 跳过无效帧数 |
| 仅采集：`probe_files` | 按 0001..N 顺序的 `{name, mean, std}` 数组（沿用 #46 的灰度 mean/std） |
| 仅采集：`collection_warn` | 若 `stop_reason=max_frames` / `error` / `saved=0`，写明「素材未采全、需重跑」 |

关闭采集（`mode=login`）时**不含任何采集字段**。`results.jsonl` 每行精简为
`timestamp / exit_code / verdict / mode / stop_reason / frames_saved`。

### 14.2 主 agent（或用户）如何读

```bash
# 读最新一次完整结果（推荐）
python -c "import json; d=json.load(open('agent/debug/last_result.json',encoding='utf-8')); print(d['verdict'], d['exit_code'], d['mode'], d.get('stop_reason'), d.get('collection_warn'))"

# 直接看完整 JSON（记事本 / VS Code 亦可打开）
cat agent/debug/last_result.json

# 看多轮历史（每次一行）
tail -n 5 agent/debug/results.jsonl
```

### 14.3 判读要点

- `login_ok=true` + `verdict=OK_MARKER` + `exit_code=0` → 登录成功；`login_ok` 与采集完整性**互不干扰**
  （采集 `stop_reason=max_frames` 时登录仍可 `exit 0`）。
- ★**`stop_reason=max_frames`（或 `error` / `frames_saved=0`）→ 素材未采全、需重跑**★，
  结果文件里 `collection_warn` 会直接写明；按 `33-fix.md §5` 的两步清单重做采集。
- `interpreter_crash: true` → `run.bat` 的崩溃兜底：python 以非 `0/2/3` 退出码结束时写的最小结果，
  表示"进程异常结束、没有正常结果"，主 agent 应排查而非当作成功。
- **`--check` 路径不写结果文件**（静态自测不是实跑）；也不删除既有的 `last_result.json`，
  主 agent 请以 `timestamp` 判断是否是本次实跑产生的新鲜结果。

### 14.4 L0 聚合验证入口（可选但推荐）

`python agent\tools\verify_all.py` 一条命令跑完
`main.py --check` + `31-verify.py` + `git diff --check` + `git status --short`，
打印 PASS/FAIL 列表与总退出码（0=全过）。**只读，不改任何源文件**；
用带 maafw 的解释器执行。主 agent 每次改完代码直接跑这一条即可。

### 14.5 不变量

写结果是**纯旁路**：任何写文件异常都被 `main()` 吞掉并 WARN，**绝不改变退出码、三态文案、
登录/判定/采集行为**；`--check [1]~[26]` 既有断言未动，只追加 `[27]`（真调用
`write_result_files` 验证字段、覆盖写/追加、崩溃兜底）。详见 `.workbuddy/artifacts/35-result-json.md`。

## 15. #49 MV 自动观看流程（mv_enabled，登录成功后自动跑，默认关闭）

登录确认 `OK_MARKER` 后、在同一次会话内自动执行「观看 MV」七步流程并回到主界面。
**默认关闭**（`flow.mv_enabled=false`）：关闭时 `_finish_ok` 双关闭分支直接返回，
行为与当前逐字一致（零回归）。实跑用命令行开关：`agent\run.bat --mv-enabled`
（仅内存覆盖，不落盘；与 `--mv-explore` 同款机制，手改配置不再推荐）。
**`--mv-enabled` 与 `--mv-explore` 不要同时开**：采集是「用户手动走」模式，与脚本自动点击
互斥，同时开时采集优先、自动流程跳过并 WARN。

### 15.1 七步语义（按 17.27~17.28 真实采集素材目视修正，比最初六步多一次返回）

| 步 | id | 动作 | 模板（roi / 阈值） | 点击后正向确认 |
| --- | --- | --- | --- | --- |
| ① | `1_click_live` | 主界面点右下 Live | `home_nav_bottom_right.png`（[1040,570,240,150] / 0.55，复用主界面判定模板） | `mv_watch_button.png` 出现（进入乐曲选择页） |
| ② | `2_click_watch` | 乐曲选择页点右下「观看MV」 | `mv_watch_button.png`（[820,630,260,90] / 0.75） | `mv_start_button.png` 出现（进入队伍确认页） |
| ③ | `3_click_start` | 队伍确认页点右下「开始」 | `mv_start_button.png`（[1030,630,250,90] / 0.75） | `gone`（开始按钮消失=已离开）+ `post_wait_sec=6.0`（MV 载入） |
| ④ | `4_tap_screen` | 点屏幕中心 (640,360) 唤起控制条 | coord 点击（先做 1280×720±4 几何守卫，绝不按错误坐标乱点） | `mv_ff_button.png` 出现（控制条唤起） |
| ⑤ | `5_click_ff` | 点顶部最右 ⏭ 快进键退出 MV | `mv_ff_button.png`（[1170,0,110,110] / 0.75） | `mv_watch_button.png` 出现（落回乐曲选择页） |
| ⑥ | `6_click_back` | 乐曲选择页点左下 ◀ 返回键 | `mv_back_button.png`（[0,630,110,90] / 0.80） | **主界面**（marker 三模板正向确认 + 留图 `mv_return_home`） |

每步：`force_foreground` → 截帧 → 模板判定 → **box_center 精确点击**（不用 `target:true` 的
rand_point）→ 轮询正向确认（`step_timeout_sec=15`）→ 超时按 `step_retry=2` 重试 → 失败留图
`mv_fail_<step>` 并 WARN。整体预算 `overall_timeout_sec=240`（重试期间同样受约束）；
`monitor.alive()` 为假立即中止。任何失败都**不重启游戏、不改阈值、不进登录兜底**。

### 15.2 模板与阈值依据（`36-ncc.py`，半分辨率灰度 NCC，与 `match_loading_template` 同口径）

| 模板 | 来源帧 | 正样本 min | 真负样本 max | 阈值 | 余量 |
| --- | --- | --- | --- | --- | --- |
| `mv_watch_button.png`（195×53） | 0003 乐曲选择 | 0.927（0010=0.932 交叉） | 0.362 | **0.75** | +0.18 / −0.39 |
| `mv_start_button.png`（195×53） | 0005 队伍确认 | 0.961 | 0.314 | **0.75** | +0.21 / −0.44 |
| `mv_ff_button.png`（62×62，⏭） | 0008 控制条 | 1.000 | 0.264 | **0.75** | +0.25 / −0.49 |
| `mv_back_button.png`（52×52，◀） | 0003 乐曲选择 | 0.995（0010=1.000 交叉） | 0.508 | **0.80** | +0.20 / −0.29 |

- ★⏭ vs ↺ 专项验证★：放宽 roi=[1050,0,230,110]（覆盖相邻 ↺ 循环键）下最高分 1.000 仍落在
  ⏭ 区域；↺（x≈1102~1167）在正常 roi=[1170,0,110,110] 之外，零误命中。
- 语义注意：0004/0005（队伍确认系页面）左下同样存在 ◀，back 模板命中它们是**正确行为**而非误报；
  真负样本（页面上无该按钮的帧）max=0.508（主界面帧）。
- ★严禁下调阈值★：后续补素材/重校准只可维持或上调；模板一律裁自常驻 UI（按钮/箭头），
  严禁裁进度条/歌词/立绘等动态内容。

### 15.3 结果码与未知项（如实声明）

- 流程成功 → 仍 `OK_MARKER` / exit 0；业务失败（步骤重试耗尽 / 整体超时 / 进程退出）→ 新增
  `FAIL_MV_FLOW` → 走 `report_result` else 分支 → **exit 2**（#34 §5.3 选项 B；三态 0/3/2 与
  文案一字未改）。终端文案为「结果: 失败（MV_FLOW_FAILED） 退出码 2」，日志 WARN 明确
  「登录已成功；按选项 B 上报」。意外异常一律吞掉 + WARN + 留图 `mv_fail_exception`，仍
  `OK_MARKER`/exit 0（不把登录成功降级）。
- ★未知项★：「乐曲选择 → 主界面」最后一段零素材（采集结束早），该段**是否有确认弹窗未知**。
  第 ⑥ 步确认超时/意外弹窗一律 WARN + 留图 `mv_fail_6_click_back`，**绝不脑补点击**；
  首次实跑若在此失败，把 `agent/debug/screencap/` 的 `mv_fail_*.png` 回传即可补齐素材。
- 验收：`--check [28]`（配置/模板/默认关闭/confirm 链/接入/三态/失败路径真调用）+
  `36-verify.py`（16 项断言：真实 bundle 七步走通、confirm 超时、home 不确认、进程退出、
  整体超时、FAIL_MV_FLOW、`_finish_ok` 三分支重建闭包驱动）全过；详见
  `.workbuddy/artifacts/36-mv-flow.md`。

### 15.4 ★#51★ 弹窗处理（放弃中断Live确认，dialog_check=true）

#50 首次真实实跑（19:22）发现：点 Live 后可能弹出「**放弃中断Live确认**」模态框
（正文『由于其他设备存在中断中的Live，无法继续。要放弃中断中的数据吗？』+ 红字
『已消耗的LB和道具不会返还』，左「返回标题」白 / 右「**放 弃**」红）。
**用户已明确决策：自动点「放 弃」继续**；「返回标题」绝不自动点。

- 配置：`flow.mv.dialog_check=true` + `flow.mv.dialogs[]`（id=`discard_live`：
  识别用 `mv_dialog_title.png`（793×38 蓝条标题，[220,160,850,90] / 0.75），
  点击用 `mv_dialog_discard.png`（208×62 红按钮，[640,440,260,110] / 0.75）。
  NCC：discard 正 0.948 / 真负 max 0.337；title 正 1.000 / 真负 max 0.408
  （`38-ncc.py`；弹窗背景是模糊主界面，已验证在无弹窗帧上零误命中）。
- 行为：确认等待循环（第 ①③④⑤⑥ 步全部）每次采样先过 `_check_dialog` 钩子 ——
  命中弹窗 → WARN + 留图 `mv_dialog_<id>` → box_center 点「放 弃」→ 继续原确认链；
  同一弹窗**最多自动点 3 次**（防快速连点，超限只 WARN 交由超时/重试）；点后弹窗仍在/
  确认仍超时 → 走既有失败路径（retry → `FAIL_MV_FLOW`），不无限循环；点击按钮未命中时
  不脑补点击。
- login_ok 分离（★#51 修复★）：#50 实跑暴露 `login_ok=bool(ok)` 在 `FAIL_MV_FLOW`
  时误为 False（登录 19:22:19 已 `OK_MARKER` 成功）。已修为
  `login_ok = ok or (reason == FAIL_MV_FLOW)` —— 登录事实不被 MV 附加流程结果覆盖；
  `--check [27](e)` 与 `36-verify.py` I 段断言守护。

## 16. #54 自动打歌（跳过模式）流程（play_enabled，登录成功后自动跑，默认关闭）

登录确认 `OK_MARKER` 后自动执行「打歌（跳过模式）」八步流程并回到主界面。
**默认关闭**（`flow.play_enabled=false`）。实跑：`agent\run.bat --play-enabled`
（仅内存覆盖不落盘）。★`--mv-enabled` 与 `--play-enabled` 互斥：同时开启时 **MV 优先**、
打歌跳过并 WARN（写死于 `_finish_ok`）；与 `--mv-explore` 同开时采集优先、自动流程全跳过。

### 16.1 八步语义（20.15~20.16 真实素材目视确认）

| 步 | id | 动作 | 模板（roi / 阈值） | 确认 |
| --- | --- | --- | --- | --- |
| ① | `1_click_live` | 主界面点 Live | `home_nav_bottom_right.png`（[1040,570,240,150] / 0.55） | `play_decide_button` 出现 |
| ② | `2_click_decide` | 乐曲选择页点「決定」 | `play_decide_button.png`（[1030,630,250,90] / 0.75） | `play_skip_button` 出现 |
| ③ | `3_click_skip` | 队伍确认页点「跳过」 | `play_skip_button.png`（[1090,630,190,90] / **0.85**） | `play_dialog_title` 出现 |
| ④ | `4_dialog_start` | 弹窗点「開 始」 | `play_start_button.png`（[620,630,260,90] / 0.75）+ **precheck** | `play_result_title` 出现 |
| ⑤ | `5_click_ok` | 結果页点「OK」（**模板数组任一命中**；ok2 为常态頁第二裁切，对偶像升级变体布局页有效性待取证——见下方勘误） | `play_ok_button.png` + `play_ok_button2.png`（各 [500,640,280,80] / 0.75） | `play_skip_button` 出现（回队伍确认页） |
| ⑥ | `6_back_team` | 队伍确认页点 ◀ | `mv_back_button.png`（[0,630,110,90] / 0.75） | `play_decide_button` 出现 |
| ⑦ | `7_back_song` | 乐曲选择页点 ◀ | `mv_back_button.png`（同上 / 0.75） | home（Loading→主界面） |
| ⑧ | `8_confirm_home` | 纯确认（click=none） | — | home（幂等二次确认） |

### 16.2 模板 NCC（`40-ncc.py` / `40-back-probe.log`）

| 模板 | 正样本 min | 真负样本 max | 阈值 |
| --- | --- | --- | --- |
| `play_decide_button`（195×52） | 0.893 | 0.608 | 0.75 |
| `play_skip_button`（115×30，只裁『跳过』避开数字） | 0.973（剩余 9/10/8 次三帧全过） | 0.714 | **0.85** |
| `play_start_button`（205×52） | 1.000 | 0.443 | 0.75 |
| `play_ok_button`（200×42，常态結果页） | 0.894 | 0.385 | 0.75 |
| `play_ok_button2`（208×54，裁自 22.16.38 留图） | 0.966（对常态頁交叉 0.875） | 0.544 | 0.75 |
| `play_dialog_title`（258×30） | 1.000 | 0.479 | 0.75 |
| `play_result_title`（326×29） | 0.946 | 0.338 | 0.75 |
| `play_max_checked`（60×33，勾选态） | 0.972 | 0.599 | 0.80 |
| `mv_back_button` 复用（play 段阈值 **0.75**） | 打歌乐曲选择 0.994/0.997、队伍确认 0.786~0.838 | 打歌主界面 0.548 | mv 段维持 0.8 |

### 16.3 条件分支与互斥（★新流程形态★）

- **precheck 前置钩子**（配置驱动，通用机制在 `_run_flow_generic`）：第 ④ 步点「開 始」前先截帧
  查 `play_max_checked`（常に最大已勾选态）：命中 → 直接点開始；未命中 → 几何守卫后按
  `coord=[479,380]` 点勾选框一次 → 重新截帧验证 → 命中则点開始；**仍未命中 → WARN + 留图
  `play_max_unchecked` → ★仍继续点開始★**（未勾选不危险，只是 LB 消耗非最大；如实记录降级）。
  常に最大未勾选态零素材，此分支按上述降级设计。
- **互斥**：`mv_enabled` 与 `play_enabled` 双开 → WARN + **MV 优先**（打歌跳过）；
  结果文件 `flow` 字段亦按此优先级（schema v2）。
- **结果文件 schema v2**：新增 `flow` 字段（`none`/`mv_explore`/`mv`/`play`）与
  `config_snapshot.mv_enabled/play_enabled`；`mode` 保留旧语义向后兼容（v1 读取者不受影响）。
- **通用执行器**：`run_mv_flow` / `run_play_flow` 共用 `_run_flow_generic`（弹窗钩子 / precheck /
  `click=none` 纯确认步 / 重试 / 几何守卫全部配置驱动，无复制粘贴）。

### 16.4 边界与未知项（按未知处理，绝不脑补）

**★勘误（#61 实证）★ 偶像升级变体页**：22.16.38 失败留图经 #61 实证为 **attempt 点击前缓存帧
（常态結果页）**，非变体布局页——play_ok_button2 实为常态頁第二裁切（与 ok 同帧不同 y 区域，
box 中心差 2px），对真正升级变体布局页的有效性**未验证**（真正变体页素材当时零采集，系旧失败
留图时机存缓存帧所致，已由 P2-1 修复：失败留图改用最后采样帧）。第⑤步模板数组任一命中机制
保留；变体页之后是否还有别的页、点其 OK 后是否直达队伍确认页、升级多级时页面是否更多均未知
（confirm=skip 天然容忍中间任意页数），待取证素材到位后评估 ok2 是否重裁。
常に最大未勾选态零素材（precheck 降级设计）；跳过剩余 0 次时点跳过的行为未知（安全中止）；
LB=0 时流程会在某步失败（安全中止，**LB 道具恢复明确不做**，0021~0022 素材不作实现依据）；
結果页 SS 评级/偶像数值为动态内容严禁裁模板；弹窗「取消」、队伍确认页「自動 剩余20次」粉钮
一律不点；跨会话/曲目/难度泛化未验证。验收：`--check [29]` + `40-verify.py`（17 项断言）；
详见 `.workbuddy/artifacts/40-play-flow.md`。

## 17. #64 登录确认阶段通用弹窗清除（popup_clear，仅自动流程启用）

**实证背景**：23:35 实跑（--play-enabled）登录点击后 19 次采样 26.8s 主界面 marker 不命中 →
OK_DEGRADED_UNVERIFIED exit 3，打歌流程按设计未启动。unverified 留图实证根因 = **每日登录奖励
弹窗（デイリーログインボーナス）遮挡主界面**（游戏日 0:00 JST 跨天当日首次登录触发，GET! 图章+
奖励板全屏遮挡、无专用 × 钮）；23:42 探针普通登录 6.4s OK_MARKER（弹窗已领取，每游戏日一次）。
这是 #55 备案「跨会话/活动弹窗泛化」缺口的首次真实触发。

### 17.1 触发条件与动作（post_click_verify 采样循环内，判定三态逻辑零改动）

触发条件**全部满足**才点击：①点击后已过 `min_wait_sec`（8.0s）；②当前非加载态；③主界面
marker 未命中；④标题按钮不可见。动作：按 `interval_sec`（3.0s）连续 `coord` 点击
（默认屏幕中心 [640,360]），每次点击前 WARN + 留图 `popup_tap_<n>`（#62 最后采样帧机制=
点击前画面）；点击后继续正常采样，**marker 命中立即停止并正常确认**；`taps` 上限（4）用尽
仍未命中 → **维持现有降级结论**（OK_DEGRADED_UNVERIFIED / LEFT_TITLE，绝不因点击误判成功）。

### 17.2 门控红线与配置

| 项 | 值 |
| --- | --- |
| `flow.popup_clear.enabled` | true（总开关，但见下方门控） |
| `flow.popup_clear.taps` / `interval_sec` / `coord` / `min_wait_sec` | 4 / 3.0 / [640,360] / 8.0（全可配） |
| **生产有效门控（两条）** | `popup_clear.enabled=true` **且** `mv_enabled or play_enabled` 任一开启 |
| **第三重防御（离线路径）** | `click_fn` 未注入时额外禁用——`run()` 无条件注入，故该条只在 `--check`/离线探针（不注入 `click_fn`）生效；本轮 `--check [30]` 已改为行为级断言 |

**普通登录（无 flow 开启）→ 有效门控不满足 → 行为逐字零回归**（两轮实跑验证的链路一字未动；本文案按 #68 F3 勘正为与实现一致的「两条」表述）。
配置位于 `flow.popup_clear`（与 mv/play 平级）。

### 17.3 坐标风险评估（如实）

若误判（无弹窗却点击），主界面中心 [640,360] 落点对照 `22-manual-1280.png` = 房间场景中部
角色立绘区（非按钮/导航/图标）——点击无 UI 交互或仅触发无害对话气泡；兜底 = marker 命中即停 +
taps 上限 4 + WARN + 逐次留图。验收：`--check [30]` + 40-verify M 段（M1 弹窗 4 次内命中即停 /
M2 永不消失安全降级 / M3 普通登录门控零点击零留图）；详见 `.workbuddy/artifacts/48-popup-clear.md`。
