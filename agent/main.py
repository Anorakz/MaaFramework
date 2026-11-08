"""SongForPrism 自动登录（MaaFramework / Win32 集成）。

流程：权限自检 → 经 .lnk 启动游戏 → 20s 启动确认/2.5s 轮询动态发现主窗口 → Win32Controller 连接
     → 加载 resource/ Bundle → 运行 pipeline(识别 TOUCH TO START → 点击 → 判定)
     → ★#21★ 点击后持续采样 20~30s（仍在加载页可延到 90s），三态判定（互斥、退出码分明）：
       ① OK_MARKER              正向主界面模板命中 → 终端「结果: 成功（已确认进入主界面）」，退出码 0
       ② OK_DEGRADED_UNVERIFIED 按钮连续 20s 不见 + 非加载态 + 进程存活，但无正向标志
                                → 终端「结果: 疑似成功（已离开标题并进入加载/游戏，但未确认主界面）」，
                                  退出码 3；**这不是失败，是待确认**，并打印留图路径供补模板
       ③ NOT_CONFIRMED          点击未成功或仍停在标题 → 「结果: 失败（未离开标题画面）」，退出码 2
       裸 inverse「按钮消失」**不作**成功依据（实测标题轮播/淡出会让它反复误命中）。
       判定时刻的原图一律存 debug/screencap/in_game_probe_*.png 供下次裁主界面模板。
       详见 agent/README.md §5/§12 与 artifacts/21-ingame-verify.md。

认证由 .lnk 内嵌的 DMM 参数在启动前完成；全程不需要账号/密码/验证码，本文件也绝不
读取或记录 .lnk 内嵌的 pf_access_token 等令牌值，只保存 .lnk 的路径。

────────────────────────────────────────────────────────────────────────────
【为什么必须「管理员权限」运行】
目标游戏进程 imasscprism 是 ELEVATED(管理员)，宿主 WorkBuddy 非管理员。Windows UIPI
会静默丢弃 PostMessage/SendMessage 窗口消息，并使 PrintWindow 后台截图返回全黑，识别与
点击都会失效。因此脚本开头做权限自检：宿主非管理员时输出中文提示(ELEVATION_HINT)并以
明确非零退出码结束；不静默降级、不自行弹 UAC、不自行调用 runas / ShellExecute("runas")，
提权由用户手动完成(以管理员身份打开终端后执行 python agent/main.py)。

【两种输入 / 截图方案的切换方法】(均在 config/settings.json 的 controller 段，一行切换)
  · 默认(实测推荐): screencap="Foreground"(避开 PrintWindow 全黑) + mouse/keyboard="Seize"
    (真实鼠标，不受 UIPI 限制，对 Unity 最可靠)。
  · 提权后若不希望抢鼠标: 把 mouse/keyboard 改成 "PostMessage" / "SendMessage" /
    "PostMessageWithCursorPos" 等消息式输入；但 Unity 对消息式点击的响应未经验证，
    若点击无效请改回 "Seize"。
  · 截图备选: "ScreenDC" / "FramePool" / "GDI" / "Background"；若某个方式黑屏或失败，
    改这一行即可。实跑由任务 #5 按效果决定，勿写死。
────────────────────────────────────────────────────────────────────────────

用法：
  python main.py --check        # 静态自测：不启动游戏、不点击真实窗口、不创建 Win32 控制器
  python main.py                # 实跑(需管理员；由任务 #5 承担，本阶段不执行)
  python main.py --skip-launch  # 跳过启动，仅附加到已存在窗口(供联调)

待定开关(用户已拍板)：flow.click_to_start=true(点击 TOUCH TO START 进入游戏)。
启动只用现有 .lnk(launch_game 单一函数，无第二种启动方式)。
"""
from __future__ import annotations

import argparse
import ctypes
import inspect
import json
import os
import struct
import sys
import time
import datetime
from ctypes import wintypes
from pathlib import Path

from maa.library import Library
from maa.toolkit import Toolkit
from maa.resource import Resource
from maa.controller import (
    Win32Controller,
    MaaWin32ScreencapMethodEnum,
    MaaWin32InputMethodEnum,
)
from maa.tasker import Tasker
from maa.context import Context, ContextEventSink
from maa.event_sink import NotificationType
from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.define import LoggingLevelEnum
from maa.pipeline import JRecognitionType, JTemplateMatch

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "settings.json"
RESOURCE_DIR = BASE_DIR / "resource"
DEBUG_DIR = BASE_DIR / "debug"
IMAGE_DIR = RESOURCE_DIR / "image"
PIPELINE_DIR = RESOURCE_DIR / "pipeline"
AGENT_LOG = DEBUG_DIR / "agent.log"
ARTIFACTS_DIR = (BASE_DIR.parent / ".workbuddy" / "artifacts")

# tools/ 下的纯 Python PNG 工具（静态自测「自匹配管道自检」时按需 import）
_TOOLS_DIR = str(BASE_DIR / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

CUSTOM_RECO_NAME = "LoadingScreenReco"

# 结果码（★#21★ 三态判定，互斥且语义固定）
OK_MARKER = "OK_MARKER"              # ① 正向主界面模板命中 → 确认进入游戏
OK_DEGRADED_UNVERIFIED = "OK_DEGRADED_UNVERIFIED"  # ② 疑似成功：点击成功+按钮消失+非加载+进程存活，
                                     #    但无正向标志（未经确认，退出码 3，日志显式标注）
FAIL_NOT_CONFIRMED = "NOT_CONFIRMED"  # ③ 点击未成功或仍停在标题（真失败）
FAIL_NOT_ELEVATED = "NOT_ELEVATED"
FAIL_LAUNCH = "LAUNCH_FAILED"
FAIL_WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
FAIL_CONNECT = "CONNECT_FAILED"
FAIL_RESOURCE = "RESOURCE_LOAD_FAILED"
FAIL_PROCESS_DIED = "GAME_PROCESS_DIED"
FAIL_OVERALL_TIMEOUT = "OVERALL_TIMEOUT"
FAIL_MAX_RESTARTS = "MAX_RESTARTS_EXCEEDED"
FAIL_DETERMINISTIC = "DETERMINISTIC_FAILURE"  # 第3次同窗退出，停止重试并上报
FAIL_STABLE_TIMEOUT = "STABLE_CONFIRM_TIMEOUT"  # 点击后加载态迟迟不结束，稳定复检超时
FAIL_MV_FLOW = "MV_FLOW_FAILED"  # ★#49★ 登录已成功但 MV 自动观看流程失败（选项 B → exit 2）
FAIL_PLAY_FLOW = "PLAY_FLOW_FAILED"  # ★#54★ 登录已成功但自动打歌流程失败（选项 B → exit 2）
FAIL_NOT_CONFIRMED = "NOT_CONFIRMED"  # ★#18★ 未观察到加载态、也无正向主界面标志 → 不得判成功

ELEVATION_HINT = (
    "============================================================\n"
    " 权限不足：必须以管理员身份运行本脚本\n"
    "============================================================\n"
    "原因：目标游戏进程 imasscprism 以管理员(ELEVATED)权限运行，而当前宿主进程非管理员。\n"
    "      Windows UIPI 会静默丢弃 PostMessage/SendMessage 窗口消息，并使 PrintWindow 等\n"
    "      后台截图返回全黑，导致界面识别与点击失效，自动登录无法工作。\n"
    "解决：请【手动】以管理员身份运行，本脚本不会自行弹 UAC、不会自行调用 runas 提权：\n"
    "      1) 右键「Windows 终端 / PowerShell / CMD」→ 以管理员身份运行；\n"
    "      2) 执行： cd /d D:\\DMM\\scsp_MAA   然后   python agent\\main.py\n"
    "      3) 静态自测可在非管理员下运行： python agent\\main.py --check\n"
)


# --------------------------------------------------------------------------- #
# 日志（自有日志 debug/agent.log；框架日志 debug/maafw.log 由 init_option 生成）
# --------------------------------------------------------------------------- #
# ★#48★ 最近 30 条日志行的内存环形缓存（仅供 debug/last_result.json 的 key_log_lines
# 与 terminal_text 读取；不改 log 的输出行为 —— print + 写文件保持原样，零回归）。
_LOG_RING: list[str] = []
_LOG_RING_MAX = 30


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}][{level}] {msg}"
    _LOG_RING.append(line)
    if len(_LOG_RING) > _LOG_RING_MAX:
        del _LOG_RING[0]
    print(line, flush=True)
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        with AGENT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_settings() -> dict:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 权限自检（不自行提权）
# --------------------------------------------------------------------------- #
_advapi32 = ctypes.windll.advapi32
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TokenElevation 信息类


def is_self_elevated() -> bool:
    """宿主进程是否管理员（IsUserAnAdmin）。"""
    try:
        ctypes.windll.shell32.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_process_elevated(pid: int):
    """目标进程是否管理员；无法判断返回 None（token query）。"""
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            elev = wintypes.DWORD()
            length = wintypes.DWORD()
            ok = _advapi32.GetTokenInformation(
                tok, TOKEN_ELEVATION, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(length)
            )
            return bool(elev.value) if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(tok)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def require_elevation(cfg: dict) -> bool:
    """实跑前置：宿主非 ELEVATED → 打印中文提示 + 返回 False（由 run 以明确失败码结束）。"""
    if not cfg["privileges"]["require_elevation"]:
        return True
    if is_self_elevated():
        log("权限自检：宿主已 ELEVATED ✓")
        return True
    print(ELEVATION_HINT)
    log("权限自检失败：宿主非 ELEVATED；按规格输出提示并以非零退出码结束（不自行提权）", "ERROR")
    return False


# --------------------------------------------------------------------------- #
# 全局日志 / 调试开关
# --------------------------------------------------------------------------- #
def init_framework_logging() -> bool:
    (BASE_DIR / "config").mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    default_config = {
        "logging": True,          # debug/maafw.log 保留全量
        "save_draw": True,        # debug/vision 识别可视化
        "save_on_error": True,    # debug/on_error 失败留图
        "stdout_level": 4,        # 控制台只显示 Info 及以上，避免 TRACE 刷屏
        "draw_quality": 85,
    }
    ok = Toolkit.init_option(str(BASE_DIR), default_config)
    Tasker.set_log_dir(str(DEBUG_DIR))
    Tasker.set_save_draw(True)
    Tasker.set_save_on_error(True)
    Tasker.set_stdout_level(LoggingLevelEnum.Info)
    Tasker.set_draw_quality(85)
    return bool(ok)


def resolve_controller_methods(cfg: dict) -> tuple[int, int, int]:
    c = cfg["controller"]
    try:
        screencap = int(getattr(MaaWin32ScreencapMethodEnum, c["screencap"]))
        mouse = int(getattr(MaaWin32InputMethodEnum, c["mouse"]))
        keyboard = int(getattr(MaaWin32InputMethodEnum, c["keyboard"]))
    except AttributeError as exc:
        raise ValueError(f"controller 配置含未知方式: {exc}") from exc
    return screencap, mouse, keyboard


# --------------------------------------------------------------------------- #
# 加载态识别：黑屏 / 白屏 / 低信息量纯色（[JumpBack] 用，纯 numpy，无外部素材）
# --------------------------------------------------------------------------- #
def is_loading_frame(frame, black_mean_max=45.0, white_mean_min=220.0, std_min=8.0,
                     white_std_max=12.0) -> bool:
    """★第5项补正★ 判定画面是否处于加载/过渡态：黑屏、纯白屏、或低信息量纯色。

    ★#18 修正（有真实帧证据）★ 白屏必须再加低方差约束：旧规则「mean>220 即加载」会把
    『亮标题帧』判成加载态——实测 `02.11.05.315` 是白到发光的标题画面(mean=239.7/std=23.1，
    TOUCH TO START 清晰可见，NCC 0.653 命中)，却被判加载→ HandleLoadingScreen 抢走 →
    整个亮场期间永远不点按钮。真·白屏加载/纯色帧 std 都 <5，白色公告页 std≈22，
    故取 white_std_max=12 作为分界（真白屏 0~5 命中；亮标题 23+ / 公告页 22 不命中）。
    黑屏保持「mean<45」不加方差约束：实测真黑屏过渡帧 mean=2.8/std=16.0，加约束会漏判。
    纯函数：自定义识别与 Python 侧连续稳定复检共用，便于 --check 单元测。
    """
    if frame is None or getattr(frame, "size", 0) == 0:
        return True  # 无有效帧按加载态处理（保守，避免误报成功）
    mean = float(frame.mean())
    std = float(frame.std())
    return (mean < black_mean_max
            or (mean > white_mean_min and std < white_std_max)
            or std < std_min)


# --------------------------------------------------------------------------- #
# ★#21★ 加载页模板判定（真实素材：淡蓝渐变背景 + 右下 "Now Loading..."）
#   背景：用户实测的加载页既不是纯黑也不是纯白（mean≈237 / std≈40），旧三态判据全部 miss，
#   导致 18 次 LoadingScreenReco 全部 ret=false、loading_seen_after_click 永远不置位。
# --------------------------------------------------------------------------- #
_LOADING_TPL_CACHE: dict = {}


def _downsample_half(gray):
    import numpy as np
    h, w = gray.shape[:2]
    h2, w2 = h // 2, w // 2
    return gray[: h2 * 2, : w2 * 2].reshape(h2, 2, w2, 2).mean(axis=(1, 3)).astype(np.float32)


def load_loading_template(name: str):
    """加载并缓存「半分辨率灰度、已去均值」的加载页模板 → (模板, 范数)。文件 mtime 变更自动失效。"""
    import numpy as np
    from make_template import decode_image  # tools/
    path = IMAGE_DIR / name
    key = (str(path), int(path.stat().st_mtime))
    cached = _LOADING_TPL_CACHE.get(key)
    if cached is not None:
        return cached
    rgb = decode_image(path)[:, :, :3]
    g = _downsample_half(rgb.mean(axis=2).astype(np.float32))
    g -= g.mean()
    tpl = (g, float(np.sqrt((g * g).sum())) or 1.0)
    _LOADING_TPL_CACHE.clear()
    _LOADING_TPL_CACHE[key] = tpl
    return tpl


def match_loading_template(frame, template: str, roi=(860, 630, 340, 90)) -> float:
    """半分辨率灰度 NCC：返回 roi 内与加载页模板的最大归一化相关系数（可负，取最大）。

    只在运行时几何（1280×720±2）启用；其它尺寸返回 0（模板按运行时几何裁，缩放会失真，
    交给黑白/纯色统计判据处理）。半分辨率 + 局部 roi 使单次耗时 ~30ms，可 1Hz 调用。
    实测（#21，`--check [17]` 复现）：真实加载页 **0.872**；标题帧 ≤0.270、真黑屏 0.167、
    白色警告页 0.000 → 阈值 0.6 两侧余量 +0.27 / −0.33。
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view as swv
    if frame is None or getattr(frame, "size", 0) == 0:
        return 0.0
    arr = np.asarray(frame)
    if arr.ndim == 3:
        gray = arr[:, :, :3].mean(axis=2)
    else:
        gray = arr
    h, w = gray.shape[:2]
    if abs(w - 1280) > 2 or abs(h - 720) > 2:
        return 0.0
    tg, tden = load_loading_template(template)
    th, tw = tg.shape
    x, y, rw, rh = [int(v / 2) for v in roi]      # roi 按 1280×720 给出 → 半分辨率除以 2
    g2 = _downsample_half(gray.astype(np.float32))
    sub = g2[y:y + rh, x:x + rw]
    if sub.shape[0] < th or sub.shape[1] < tw:
        return 0.0
    flat_t = tg.reshape(-1)
    best = 0.0
    for yy in range(sub.shape[0] - th + 1):
        win = swv(sub[yy:yy + th], (th, tw))[0].reshape(-1, th * tw)
        win = win - win.mean(axis=1, keepdims=True)
        den = np.sqrt((win * win).sum(axis=1)) + 1e-6
        best = max(best, float(((win @ flat_t) / (den * tden)).max()))
    return best


def detect_loading_frame(frame, *, black_mean_max=45.0, white_mean_min=220.0, std_min=8.0,
                         white_std_max=12.0, template=None, template_threshold=0.6,
                         template_roi=(860, 630, 340, 90)) -> tuple[bool, dict]:
    """★#21★ 加载态**复合**判据：黑/纯白低方差/低方差纯色 **或** 命中 `Now Loading` 模板。

    返回 (is_loading, info)。info 含 mean/std/stats_hit/tpl_score/reason，便于日志与自测核对。
    """
    stats_hit = is_loading_frame(frame, black_mean_max, white_mean_min, std_min, white_std_max)
    info: dict = {"stats_hit": stats_hit}
    try:
        if frame is not None and getattr(frame, "size", 0):
            info["mean"] = round(float(frame.mean()), 1)
            info["std"] = round(float(frame.std()), 1)
    except Exception:  # noqa: BLE001 - 仅日志信息
        pass
    if stats_hit:
        info["reason"] = "stats(black/white/low-var)"
        return True, info
    if template:
        s = match_loading_template(frame, template, tuple(template_roi))
        info["tpl_score"] = round(s, 3)
        if s >= float(template_threshold):
            info["reason"] = "loading-template"
            return True, info
    info["reason"] = "none"
    return False, info


class LoadingScreenReco(CustomRecognition):
    """加载态命中 → [JumpBack] 回跳等待，不得进入 InGameSuccess。

    ★复合判据（#21）★ ① 黑屏 / 纯白低方差 / 低方差纯色（统计）**或** ② 命中 `Now Loading`
    模板。用户提供的真实加载页是**淡蓝渐变 + 右下角 Now Loading...**（mean≈237/std≈40），
    统计判据完全不命中（实测 18 次 ret=false），必须靠模板；两者任一命中即加载态。
    """

    def analyze(self, context: Context, argv: CustomRecognition.AnalyzeArg):
        black, white, std_min, white_std_max = 45.0, 220.0, 8.0, 12.0
        tpl, tpl_thr, tpl_roi = None, 0.6, (860, 630, 340, 90)
        try:
            param = json.loads(argv.custom_recognition_param or "{}")
            black = float(param.get("black_mean_max", black))
            white = float(param.get("white_mean_min", white))
            std_min = float(param.get("std_min", std_min))
            white_std_max = float(param.get("white_std_max", white_std_max))
            tpl = param.get("loading_template") or None
            tpl_thr = float(param.get("loading_template_threshold", tpl_thr))
            roi = param.get("loading_template_roi")
            if roi:
                tpl_roi = tuple(int(v) for v in roi)
        except (ValueError, TypeError, AttributeError):
            pass
        img = argv.image
        hit, info = detect_loading_frame(
            img, black_mean_max=black, white_mean_min=white, std_min=std_min,
            white_std_max=white_std_max, template=tpl, template_threshold=tpl_thr,
            template_roi=tpl_roi,
        )
        if not hit:
            return None
        h, w = img.shape[:2] if (img is not None and getattr(img, "size", 0)) else (0, 0)
        return CustomRecognition.AnalyzeResult(
            box=(0, 0, w, h),
            detail={
                "black_mean_max": black, "white_mean_min": white, "std_min": std_min,
                "white_std_max": white_std_max, "loading_template": tpl,
                "loading_template_threshold": tpl_thr, **info,
            },
        )


# --------------------------------------------------------------------------- #
# 事件回调：记录被识别到的界面标志，作为成功判定依据
# --------------------------------------------------------------------------- #
class ClickCenterAction(CustomAction):
    """★#18★ 点击「识别框中心」。默认 Click 的 target:true 会用 rand_point（正态分布、可能落框沿，
    见 Actuator.cpp:145-176）；本自定义动作精确点 box 中心。argv.box 为缩放截图坐标，
    post_click 期望缩放坐标、框架内部再按 raw/target 还原（ControllerAgent.cpp:1226-1238）。"""

    def run(self, context: Context, argv: CustomAction.RunArg):
        box = argv.box
        cx = int(box.x + box.w // 2)
        cy = int(box.y + box.h // 2)
        try:
            job = context.tasker.controller.post_click(cx, cy)
            job.wait()
            ok = bool(job.succeeded)
        except Exception as exc:  # noqa: BLE001
            log(f"ClickCenterAction 异常: {exc}", "WARN")
            return False
        log(f"ClickCenterAction: 点击识别框中心 ({cx},{cy}) "
            f"box=({box.x},{box.y},{box.w},{box.h}) {'成功' if ok else '失败'}",
            "INFO" if ok else "WARN")
        return ok


class NodeEventSink(ContextEventSink):
    def __init__(self) -> None:
        super().__init__()
        self.marker_node = ""
        self.success_node = ""
        self.succeeded_recognitions: set[str] = set()
        self.succeeded_actions: set[str] = set()
        self.events: list[str] = []
        # ★#18★ 成功判定不再依赖裸 inverse：记录「是否已点击」与「点击后是否观察到加载态」
        self.clicked = False
        self.loading_seen_after_click = False
        self.click_count = 0

    def set_targets(self, marker_node: str, success_node: str) -> None:
        self.marker_node = marker_node
        self.success_node = success_node

    def reset(self) -> None:
        self.succeeded_recognitions.clear()
        self.succeeded_actions.clear()
        self.events.clear()
        self.clicked = False
        self.loading_seen_after_click = False

    def on_node_recognition(self, context, noti_type, detail):
        name = str(detail.name)
        succeeded = noti_type == NotificationType.Succeeded
        self.events.append(f"reco {name} type={int(noti_type)} succeeded={succeeded}")
        if succeeded:
            self.succeeded_recognitions.add(name)
            # ★#18★ 点击后若命中 HandleLoadingScreen（黑/白/低方差加载态）→ 记录已观察到加载
            if name == "HandleLoadingScreen" and self.clicked:
                self.loading_seen_after_click = True
        log(f"  · 识别 {name}: {'命中' if succeeded else int(noti_type)}")

    def on_node_action(self, context, noti_type, detail):
        name = str(detail.name)
        succeeded = noti_type == NotificationType.Succeeded
        self.events.append(f"action {name} type={int(noti_type)} succeeded={succeeded}")
        if succeeded:
            self.succeeded_actions.add(name)
            if name == "TitleScreenDetectedClick":
                self.clicked = True
                self.click_count += 1
        log(f"  · 动作 {name}: {'成功' if succeeded else int(noti_type)}")

    def is_success(self, tasker: Tasker) -> bool:
        """成功判定靠「识别到的界面标志」，不能只判断动作已发送。"""
        marker_ok = self.marker_node in self.succeeded_recognitions
        success_ok = (
            self.success_node in self.succeeded_recognitions
            or self.success_node in self.succeeded_actions
            or tasker.get_latest_node(self.success_node) is not None
        )
        failure_seen = ("HandleFailure" in self.succeeded_recognitions
                        or "DoStop" in self.succeeded_actions)
        return marker_ok and success_ok and not failure_seen


# --------------------------------------------------------------------------- #
# 进程 / 窗口存活监控 + 窗口几何（ctypes，无第三方依赖）
# --------------------------------------------------------------------------- #
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class _RECT(ctypes.Structure):
    _fields_ = [("L", wintypes.LONG), ("T", wintypes.LONG),
                ("R", wintypes.LONG), ("B", wintypes.LONG)]


_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL


# --------------------------------------------------------------------------- #
# ★#14★ 窗口预调整所需的 Win32 结构 / 常量 / API（连接前把窗口摆进工作区，
# 切断 175% DPI 下框架 ensure_window_on_screen 的正反馈放大）
# --------------------------------------------------------------------------- #
class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", _RECT),
                ("rcWork", _RECT),
                ("dwFlags", wintypes.DWORD)]


MONITOR_DEFAULTTONEAREST = 2
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200

_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL


def _hwnd_int(hwnd) -> int:
    if isinstance(hwnd, int):
        return hwnd
    val = getattr(hwnd, "value", None)
    return int(val) if val is not None else int(hwnd)


def window_usable(hwnd) -> bool:
    """排除 0×0 隐藏的 IME 窗口：必须可见且宽高 > 0。"""
    h = _hwnd_int(hwnd)
    if not _user32.IsWindowVisible(wintypes.HWND(h)):
        return False
    r = _RECT()
    if not _user32.GetWindowRect(wintypes.HWND(h), ctypes.byref(r)):
        return False
    return (r.R - r.L) > 0 and (r.B - r.T) > 0


def init_dpi_awareness() -> str:
    """★#14★ 让本进程成为 Per-Monitor DPI aware，使 GetWindowRect/SetWindowPos 用物理像素。

    175% 缩放下若本进程 DPI-unaware，窗口坐标会被二次虚拟化，预调整的尺寸/位置会算错
    （框架 ensure_window_on_screen 正是因逻辑/物理混用 + 目标窗口 DPI-unaware 才正反馈放大）。
    失败不致命（可能已由 manifest/宿主设过），返回实际生效模式或 unchanged。
    """
    try:
        fn = getattr(_user32, "SetProcessDpiAwarenessContext", None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = wintypes.BOOL
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if fn(ctypes.c_void_p(-4)):
                return "PerMonitorV2"
    except Exception:
        pass
    try:
        fn2 = getattr(ctypes.windll.shcore, "SetProcessDpiAwareness", None)
        if fn2 is not None:
            fn2.argtypes = [ctypes.c_int]
            fn2.restype = ctypes.c_long
            if fn2(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
                return "PerMonitor"
    except Exception:
        pass
    try:
        if _user32.SetProcessDPIAware():
            return "SystemAware"
    except Exception:
        pass
    return "unchanged(already-set-or-failed)"


def compute_prealign_rect(mon_work, cur_rect, desired_size=None):
    """★纯函数★ 计算把窗口摆进工作区的目标窗口矩形 (x, y, w, h)，不触碰任何 HWND，便于单测。

    mon_work: (left, top, right, bottom) 显示器工作区（物理像素，rcWork）
    cur_rect: (x, y, w, h) 当前窗口矩形（物理像素）
    desired_size: (w, h) 期望尺寸；None = 保留当前尺寸（仅按工作区上限裁剪，绝不放大）
    规则：尺寸取 min(期望或当前, 工作区尺寸)；位置先保右/下不越界，再保左/上不越界（与框架同序）。
    """
    ml, mt, mr, mb = mon_work
    work_w = mr - ml
    work_h = mb - mt
    cx, cy, cw, ch = cur_rect
    dw, dh = desired_size if desired_size else (cw, ch)
    w = max(1, min(int(dw), max(1, work_w)))
    h = max(1, min(int(dh), max(1, work_h)))
    x, y = int(cx), int(cy)
    if x + w > mr:
        x = mr - w
    if y + h > mb:
        y = mb - h
    if x < ml:
        x = ml
    if y < mt:
        y = mt
    return (x, y, w, h)


def get_window_rect_px(hwnd):
    """读取窗口矩形 (x,y,w,h) 物理像素；失败返回 None。"""
    r = _RECT()
    if not _user32.GetWindowRect(wintypes.HWND(_hwnd_int(hwnd)), ctypes.byref(r)):
        return None
    return (r.L, r.T, r.R - r.L, r.B - r.T)


def get_monitor_work_rect(hwnd):
    """取窗口所在显示器工作区 (l,t,r,b) 物理像素；失败返回 None。"""
    hmon = _user32.MonitorFromWindow(wintypes.HWND(_hwnd_int(hwnd)), MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return (mi.rcWork.L, mi.rcWork.T, mi.rcWork.R, mi.rcWork.B)


def set_window_pos(hwnd, x, y, cx, cy, flags) -> bool:
    return bool(_user32.SetWindowPos(wintypes.HWND(_hwnd_int(hwnd)), None,
                                     int(x), int(y), int(cx), int(cy), int(flags)))


_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.SetFocus.restype = wintypes.HWND
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def force_foreground(hwnd) -> bool:
    """★#18★ 可靠置前：AttachThreadInput(前台线程, 本线程) + BringWindowToTop + SetForegroundWindow，
    然后校验 GetForegroundWindow()==hwnd 并记日志。裸 SetForegroundWindow 会被前台锁拒绝。
    不自行提权、不弹 UAC（仅窗口 Z 序/焦点操作）。"""
    h = _hwnd_int(hwnd)
    hwnd_w = wintypes.HWND(h)
    if not _user32.IsWindow(hwnd_w):
        log("force_foreground: 窗口无效", "WARN")
        return False
    fg = _user32.GetForegroundWindow()
    if fg and _hwnd_int(fg) == h:
        log(f"force_foreground: 目标 hwnd={h} 已是前台，无需切换（GetForegroundWindow 校验通过）")
        return True
    fg_tid = 0
    if fg:
        pid_tmp = wintypes.DWORD(0)
        fg_tid = int(_user32.GetWindowThreadProcessId(fg, ctypes.byref(pid_tmp)))
    cur_tid = int(_kernel32.GetCurrentThreadId())
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
        _user32.BringWindowToTop(hwnd_w)
        _user32.SetForegroundWindow(hwnd_w)
        try:
            _user32.SetFocus(hwnd_w)
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        log(f"force_foreground 异常: {exc}", "WARN")
    finally:
        if attached:
            try:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
            except Exception:
                pass
    ok = _hwnd_int(_user32.GetForegroundWindow()) == h
    log(f"force_foreground: 目标 hwnd={h} 置前{'成功' if ok else '失败'}"
        f"（当前前台 hwnd={_hwnd_int(_user32.GetForegroundWindow())}）", "INFO" if ok else "WARN")
    return ok


def prealign_window(hwnd, cfg):
    """★#14★ 连接 Win32Controller 之前，把窗口一次性摆进主屏工作区（DPI-aware 物理像素）。

    目的：让框架 ensure_window_on_screen() 判 need_change=False 直接返回，切断 175% DPI 下
    「GetClientRect(逻辑) / GetWindowRect(物理) 混用 → frame 虚高 → SetWindowPos 逐次放大」的正反馈。
    默认仅移动（SWP_NOSIZE），规避 DPI-unaware 目标窗口的尺寸换算歧义；仅当尺寸超出工作区才 resize。
    返回目标矩形 (x,y,w,h)；跳过/失败返回 None。
    """
    pa = (cfg.get("window") or {}).get("prealign") or {}
    if not pa.get("enabled", True):
        log("窗口预调整已禁用（window.prealign.enabled=false）", "WARN")
        return None
    h = _hwnd_int(hwnd)
    mon = get_monitor_work_rect(h)
    cur = get_window_rect_px(h)
    if mon is None or cur is None:
        log("窗口预调整跳过：无法获取显示器工作区或窗口矩形", "WARN")
        return None
    target = compute_prealign_rect(mon, cur, pa.get("desired_size"))
    summary = (f"原始 pos=({cur[0]},{cur[1]}) size={cur[2]}x{cur[3]} | "
               f"rcWork(l,t,r,b)={mon} | 目标 pos=({target[0]},{target[1]}) size={target[2]}x{target[3]}")
    if target == cur:
        log(f"窗口预调整: 已在工作区内，无需移动（{summary}）")
        return target
    same_size = (target[2], target[3]) == (cur[2], cur[3])
    if same_size:
        ok = set_window_pos(h, target[0], target[1], 0, 0,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        mode = "仅移动(SWP_NOSIZE)"
    else:
        ok = set_window_pos(h, target[0], target[1], target[2], target[3],
                            SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER)
        mode = "移动+缩放"
    log(f"窗口预调整[{mode}] {'成功' if ok else '失败'}：{summary}", "INFO" if ok else "WARN")
    return target


class GameProcessMonitor:
    """通过窗口句柄解析 PID，检测窗口销毁 / 进程退出（游戏静默自退），并记录权限状态。"""

    def __init__(self, hwnd) -> None:
        self.hwnd_int = _hwnd_int(hwnd)
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(wintypes.HWND(self.hwnd_int), ctypes.byref(pid))
        self.pid = int(pid.value)
        self.elevated = is_process_elevated(self.pid)  # True/False/None
        self.first_seen = time.monotonic()  # 窗口首次发现时间，用于「连续存活 ≥60s」口径
        log(f"进程监控已绑定: hwnd={self.hwnd_int} pid={self.pid} "
            f"target_elevated={self.elevated} self_elevated={is_self_elevated()}")

    def alive_duration(self) -> float:
        """窗口连续存活秒数。§5-4『连续存活 ≥60s 判本次启动可用』仅用于
        重启决策/确定性失败标注的观测口径，★不作为登录前置等待、不是长期保活★。"""
        return time.monotonic() - self.first_seen

    def alive(self) -> bool:
        if not _user32.IsWindow(wintypes.HWND(self.hwnd_int)):
            return False
        if self.pid:
            handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
            if handle:
                try:
                    code = wintypes.DWORD()
                    ok = _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    if ok:
                        return int(code.value) == STILL_ACTIVE
                finally:
                    _kernel32.CloseHandle(handle)
        return True


# --------------------------------------------------------------------------- #
# 窗口发现（可单元测试）
# --------------------------------------------------------------------------- #
def pick_window(windows, wcfg: dict, usable=None):
    """按 window_name 含 name_include 命中，排除工具窗/IME 隐藏窗，记录 class_name 兜底。

    usable: callable(hwnd_int)->bool，用于排除 0×0/不可见窗口；None 时跳过（单测用）。
    """
    include = wcfg["name_include"]
    excludes = wcfg.get("exclude_names", [])
    exclude_classes = wcfg.get("exclude_class_names", [])
    fallback_cls = wcfg.get("class_name_fallback", "")
    for w in windows:
        name = w.window_name or ""
        cls = w.class_name or ""
        if any(ex and ex in name for ex in excludes):
            continue
        if cls in exclude_classes:
            continue
        hit = include in name
        if not hit and fallback_cls and fallback_cls in cls:
            hit = True
        if not hit:
            continue
        if usable is not None and not usable(_hwnd_int(w.hwnd)):
            continue
        return w
    return None


def wait_for_window(cfg: dict, hard_deadline: float):
    lcfg = cfg["launch"]
    timeout = float(lcfg["window_poll_timeout_sec"])
    interval = float(lcfg["window_poll_interval_sec"])
    deadline = min(hard_deadline, time.monotonic() + timeout)
    wcfg = cfg["window"]
    log(f"轮询等待目标窗口（最多 {timeout:.0f}s，name 含 {wcfg['name_include']!r} "
        f"/ class {wcfg.get('class_name')!r}，排除工具窗与 IME 隐藏窗）")
    while True:
        windows = Toolkit.find_desktop_windows()
        win = pick_window(windows, wcfg, usable=window_usable if wcfg.get("require_visible_sized") else None)
        if win is not None:
            log(f"发现窗口: hwnd={_hwnd_int(win.hwnd)} "
                f"class_name={win.class_name!r} window_name={win.window_name!r}")
            return win
        if time.monotonic() >= deadline:
            names = [(w.window_name, w.class_name) for w in windows]
            raise TimeoutError(
                f"窗口轮询超时({timeout:.0f}s/整体超时)未发现目标窗口 "
                f"{wcfg['name_include']!r}；当前桌面窗口: {names}"
            )
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# ★启动命令单一函数★（只用现有 .lnk，os.startfile / ShellExecute）
# --------------------------------------------------------------------------- #
def launch_game(cfg: dict) -> str:
    """★启动命令单一决策点★ launch.mode 锁定为 lnk；绝不解析/复述 .lnk 内嵌令牌，
    绝不直接拉 imasscprism.exe，无 DMMGamePlayer/协议分支，无令牌刷新逻辑。"""
    mode = cfg["launch"].get("mode", "lnk")
    if mode != "lnk":
        raise RuntimeError(f"launch.mode 锁定为 lnk，不接受 {mode!r}；不存在第二种启动方式")
    path = cfg["launch"]["lnk_path"]
    if not Path(path).exists():
        raise FileNotFoundError(f"快捷方式不存在: {path}")
    os.startfile(path)  # ShellExecute：由 .lnk 的 Arguments 在启动前完成 DMM 认证
    return f"os.startfile(.lnk) path={path}"


# --------------------------------------------------------------------------- #
# ★点击决策集中点★（用户已拍板 click_to_start=true：点击 TOUCH TO START 进入游戏）
# --------------------------------------------------------------------------- #
def select_task(cfg: dict) -> tuple[str, str, str]:
    """返回 (入口节点, 界面标志节点, 成功标志节点)。"""
    if cfg["flow"]["click_to_start"]:
        return "LoginAndStart", "TitleScreenDetectedClick", "InGameSuccess"
    return "LoginTitleOnly", "TitleScreenDetected", "LoginSuccess"


# --------------------------------------------------------------------------- #
# ★P0-0 校准参数单一来源★ 供 pipeline 注入与 Python 侧 button_visible_fn 共用，
# 保证「识别按钮是否可见」与「点击按钮」用的是同一套已校准模板/roi/阈值/算法。
# --------------------------------------------------------------------------- #
def build_detection_params(cfg: dict) -> dict:
    """返回 TemplateMatch 的识别参数字典（pipeline v1 格式）。
    ★4 模板数组 + 局部 roi + 实测 threshold，见 config.calibration / artifacts/09-calibration.md。"""
    rec = cfg["recognition"]
    tpl = rec["template"]
    params = {
        "recognition": "TemplateMatch",
        "template": list(tpl["files"]),   # 多模板数组：跨立绘轮播稳健
        "threshold": tpl["threshold"],
        "method": tpl["method"],
    }
    if tpl.get("roi"):
        params["roi"] = list(tpl["roi"])  # 局部锚定按钮区，避开全图轮播
    return params


def build_marker_params(cfg: dict):
    """★#22★ 把 `recognition.in_game_marker_template`（字符串或数组）规范化为
    **每个模板各自的 (file, roi, threshold)**，供 make_marker_hit_fn / build_pipeline_override 共用。

    返回 dict 或 **None**。以下任一情况返回 None（硬守护）：marker 为 null / 空数组 /
    全空字符串 / 任一模板文件在 `resource/image/` 下缺失 / 阈值低于下限 / 阈值数组长度
    与模板数不一致。调用方必须据此禁用正向判定，绝不判 OK_MARKER、绝不回退到裸 inverse
    （模板不匹配 ≠ 成功）。

    `in_game_marker_threshold` / `in_game_marker_roi` 可为单值（广播到全部模板）或与模板
    等长的数组（每模板各自参数，#22 校准即用数组）。★#23 P2★ 空阈值数组回退默认 0.7 广播
    （不再触发 thrs[-1] IndexError）。★#25 P1-1★ 阈值数组长度与模板数不一致（单模板
    可插拔路径除外）或任一阈值 <0.55 → WARN + 返回 None：真实 MaaFW 判定器实测标题帧
    `home_lb_diamond` score=0.507051，阈值 ≤0.507 会把标题帧误命中 → OK_MARKER 假成功；
    当前校准 [0.55,0.75,0.6] 用 `<0.55` 严格小于判断，正好通过。
    """
    rec = cfg["recognition"]
    tpl = rec.get("in_game_marker_template")
    if not tpl:
        return None
    files = [tpl] if isinstance(tpl, str) else list(tpl)
    files = [f for f in files if isinstance(f, str) and f.strip()]
    if not files:
        return None
    missing = [f for f in files if not (IMAGE_DIR / f).is_file()]
    if missing:
        log(f"主界面模板文件缺失 {missing} → 正向标志禁用（绝不判 OK_MARKER）", "WARN")
        return None

    thr_cfg = rec.get("in_game_marker_threshold", 0.7)
    thrs = ([float(t) for t in thr_cfg] if isinstance(thr_cfg, (list, tuple))
            else [float(thr_cfg)])
    # ★#23 P2-1★ 空阈值数组回退默认 0.7 再广播，避免 thrs[-1] IndexError 让 run() 崩溃
    if not thrs:
        thrs = [0.7]
        log("主界面阈值数组为空 → 回退默认 0.7 广播", "WARN")
    # ★#25 P1-1★ 阈值长度硬校验：单值广播保留；单模板（可插拔）只取首个阈值；
    # 多模板时长度必须严格相等，不再末值补齐/截断（畸形数组会挑出低阈值 → 假成功）。
    if len(files) == 1:
        if len(thrs) > 1:
            log(f"单模板配置忽略多余阈值项 {thrs[1:]}，仅用 {thrs[0]}", "WARN")
        thrs = thrs[:1]
    elif len(thrs) == 1:
        thrs = thrs * len(files)
    elif len(thrs) != len(files):
        log(f"主界面阈值数组长度 {len(thrs)} 与模板数 {len(files)} 不一致 → "
            f"正向标志禁用（绝不判 OK_MARKER）", "WARN")
        return None
    # ★#25 P1-1★ 阈值下限：标题帧 lb 模板真实判定器负样本上界 0.507051，
    # 任何 <0.55 的阈值都可能把标题帧误命中；绝不静默接受（WARN + 禁用）。
    if any(t < 0.55 for t in thrs):
        log(f"主界面阈值 {thrs} 含 <0.55 的值 → 正向标志禁用"
            f"（标题帧负样本上界 0.507，绝不假成功）", "WARN")
        return None

    roi_cfg = rec.get("in_game_marker_roi") or []
    rois = []
    for i in range(len(files)):
        r = roi_cfg[i] if i < len(roi_cfg) else None
        rois.append(list(r) if r else None)
    return {"template": files, "roi": rois, "threshold": thrs, "method": 5}


# --------------------------------------------------------------------------- #
# 把识别方式 / 超时 / 画面稳定 / 黑屏阈值等配置注入 pipeline（merge 语义部分覆盖）
# --------------------------------------------------------------------------- #
def build_pipeline_override(cfg: dict) -> dict:
    rec = cfg["recognition"]
    flow = cfg["flow"]

    if rec["method"] == "template":
        detection = build_detection_params(cfg)
    elif rec["method"] == "ocr":
        detection = {
            "recognition": "OCR",
            "expected": rec["ocr"]["expected"],
            "threshold": rec["ocr"]["threshold"],
        }
        if rec["template"].get("roi"):
            detection["roi"] = list(rec["template"]["roi"])  # 局部锚定按钮区，避免全图 OCR 干扰
    else:
        raise ValueError(f"未知 recognition.method: {rec['method']!r}（应为 template / ocr）")

    override: dict[str, dict] = {
        "TitleScreenDetected": dict(detection),
        "TitleScreenDetectedClick": dict(detection),
        "InGameSuccess": dict(detection),
        # timeout 设在「等待下一节点」的上一节点（入口 / WaitInGame）
        "LoginTitleOnly": {"timeout": flow["title_wait_timeout_ms"], "rate_limit": flow["rate_limit_ms"]},
        "LoginAndStart": {"timeout": flow["title_wait_timeout_ms"], "rate_limit": flow["rate_limit_ms"]},
        "WaitInGame": {"timeout": flow["post_click_timeout_ms"], "rate_limit": flow["rate_limit_ms"]},
        "HandleLoadingScreen": {"custom_recognition_param": {
            "black_mean_max": rec["loading_frame"]["black_mean_max"],
            "white_mean_min": rec["loading_frame"]["white_mean_min"],
            "white_std_max": rec["loading_frame"]["white_std_max"],
            "std_min": rec["loading_frame"]["std_min"],
            # ★#21★ 加载页模板判据（淡蓝背景 + Now Loading...，统计判据覆盖不到）
            "loading_template": rec.get("loading_template", {}).get("file"),
            "loading_template_threshold": rec.get("loading_template", {}).get("threshold", 0.6),
            "loading_template_roi": rec.get("loading_template", {}).get("roi", [860, 630, 340, 90]),
        }},
    }
    # ★#18★ 点击目标：login.json 用 action=Custom / custom_action=ClickCenter 精确点识别框中心，
    # 不再用 target:true（其背后是 Actuator::rand_point 的正态随机点，可能落框沿）。
    # 也绝不注入母图绝对坐标：运行时帧 1280×720、模板母图 860×720，宽差会错位。
    # ★轮播★ 不注入全图 pre/post_wait_freezes：标题画面角色立绘持续轮播(diff 37~76)，全图差分等静止
    # 会让节点超时。画面稳定改由 Python 侧 confirm_not_loading_stable() 用加载态统计 + 进程存活实现。

    # ★#22/#23★ InGameSuccess 节点【恒保持】inverse=true + 按钮模板 —— 它只是「离开标题/
    # 按钮消失」的前置信号，绝不是成功判定。权威正向判定全在 Python 侧
    # （make_marker_hit_fn / post_click_verify / #23 新增 direct_marker_confirm）。这样
    # OK_DEGRADED_UNVERIFIED(exit 3) 三态完整保留，也绝不出现「裸 inverse 单帧命中即成功」；
    # 若切成正向 marker 识别，主界面未出现时 sink.is_success 恒为 False、exit 3 将不可达。
    # ★#23 P1-1★ 删除曾生成的 InGameMarker_0/1/2：它们 next=[] 且无任何 next/on_error 指入，
    # 合并 login.json 后是**孤立死节点、永不执行**；且「含这些节点的完整 override 喂给
    # post_task」从未离线验证过（历史「自测全过实跑翻车」盲区）。正向判定不需要 pipeline 冗余节点。
    override["InGameSuccess"]["inverse"] = True
    return override


# --------------------------------------------------------------------------- #
# 单任务监控：进程存活 + 整体 wall-clock 超时（不阻塞在 .wait()，避免死等）
# --------------------------------------------------------------------------- #
def monitor_job(job, tasker: Tasker, monitor: GameProcessMonitor,
                deadline: float, interval: float) -> str:
    while not job.done:
        if not monitor.alive():
            tasker.post_stop()
            return FAIL_PROCESS_DIED
        if time.monotonic() > deadline:
            tasker.post_stop()
            return FAIL_OVERALL_TIMEOUT
        time.sleep(interval)
    return "DONE"


def wait_target_geometry(ctrl, cfg: dict, *, timeout_sec: float = 15.0,
                         sleep_fn=None, log_fn=None) -> tuple[bool, str]:
    """★#18★ 点击前的几何守卫：等待截图目标尺寸收敛到「长边 1280 / 高 720」。

    背景（第0步实测）：日志 `Resolution changed` 只在 02:10:11（raw 2532×1447，窗口刚被
    prealign 改尺寸）与 02:10:16（raw 2240×1260 = 1280×720@175%）出现两次，之后再未变化。
    即模板几何只在启动最初几秒不吻合；点错坐标的代价远大于多等几秒，故点击前显式收敛。
    返回 (是否可用, 说明)；不可用时调用方必须禁用点击（不得盲目点）。
    """
    want_w = int(cfg["controller"].get("screenshot_target_long_side", 1280))
    want_h = int(cfg["controller"].get("geometry_expect_height", 720))
    tol = int(cfg["controller"].get("geometry_height_tolerance", 4))
    wait_sec = float(cfg["controller"].get("geometry_wait_sec", 15.0)) if timeout_sec is None else timeout_sec
    sleep_fn = sleep_fn or time.sleep
    log_fn = log_fn or log
    end = time.monotonic() + max(0.0, wait_sec)
    last = ""
    while True:
        try:
            frame = ctrl.post_screencap().wait().get()
            h, w = int(frame.shape[0]), int(frame.shape[1])
            last = f"{w}x{h}"
            if w == want_w and abs(h - want_h) <= tol:
                log_fn(f"几何守卫: 截图目标 {w}x{h} 与模板校准几何 {want_w}x{want_h} 一致")
                return True, last
        except Exception as exc:  # noqa: BLE001 - 截图失败不应直接中断
            last = f"截图异常: {exc}"
        if time.monotonic() >= end:
            log_fn(f"几何守卫: {wait_sec:.0f}s 内未收敛到 {want_w}x{want_h}±{tol}（最后 {last}），"
                   f"本轮禁用点击以免点错坐标", "WARN")
            return False, last
        sleep_fn(0.5)


def build_session(cfg: dict, res: Resource, win):
    """建立一次控制会话：连接控制器、绑定 Tasker、挂事件回调、进程监控。失败返回 None。"""
    # ★#14★ 连接前一次性把窗口摆进主屏工作区（DPI-aware），使框架 ensure_window_on_screen
    # 判 need_change=False，切断 175% DPI 下的正反馈放大。失败不阻断连接。
    try:
        prealign_window(win.hwnd, cfg)
    except Exception as exc:  # noqa: BLE001 - 预调整异常不应阻断连接
        log(f"窗口预调整异常（继续连接）: {exc}", "WARN")
    # ★#18★ 连接后立即可靠置前（上次会话窗口始终非前台导致 Seize 点击未生效）。
    try:
        force_foreground(win.hwnd)
    except Exception as exc:  # noqa: BLE001
        log(f"置前异常（继续连接）: {exc}", "WARN")
    screencap, mouse, keyboard = resolve_controller_methods(cfg)
    ctrl = Win32Controller(win.hwnd, screencap, mouse, keyboard)  # 位置参数：binding 是 hWnd
    conn_job = ctrl.post_connection()
    conn_job.wait()
    if not conn_job.succeeded:
        log("post_connection 失败", "ERROR")
        return None
    # ★#18 第0步结论★ 帧宽"波动"的真正来源不是窗口几何，而是调试图拼接：
    # TemplateMatcher::draw_result() 会把模板缩略图拼在右侧（copyMakeBorder(..., templ.cols)，
    # TemplateMatcher.cpp:143-146），旧模板宽 230 → vision 里 1280 帧显示为 1510、1260 显示为 1490；
    # 真实 image_target 只有 1260（窗口刚被 prealign 改尺寸的 5s 过渡）与 1280（其后稳定）两种。
    # 因此不需要多尺度模板：把缩放基准从「短边 720」改成「长边 1280」，横屏窗口的
    # 模板坐标系宽度恒等于 1280，任何窗口比例下按钮 x 坐标都不再漂移（16:9 → 1280×720 与旧值完全一致）。
    long_side = int(cfg["controller"].get("screenshot_target_long_side", 1280))
    ctrl.set_screenshot_target_long_side(long_side)
    log(f"控制器已连接: screencap={cfg['controller']['screencap']} "
        f"mouse={cfg['controller']['mouse']} keyboard={cfg['controller']['keyboard']} "
        f"长边={long_side}（模板按 1280×720 校准）")
    tasker = Tasker()
    tasker.bind(res, ctrl)
    if not tasker.inited:
        log("Tasker 初始化失败", "ERROR")
        return None
    sink = NodeEventSink()
    if tasker.add_context_sink(sink) is None:
        log("add_context_sink 失败，无法据界面标志判定成功", "ERROR")
        return None
    monitor = GameProcessMonitor(win.hwnd)
    return (ctrl, tasker, sink, monitor, _hwnd_int(win.hwnd))


# --------------------------------------------------------------------------- #
# ★第5项补正★ Python 侧「连续稳定 5~10s」成功复检（降级判定模式的核心）
# --------------------------------------------------------------------------- #
def make_button_visible_fn(tasker: Tasker, cfg: dict):
    """返回 callable(frame)->bool：用框架 post_recognition 正向 TemplateMatch
    复检当前帧里 TOUCH TO START 按钮是否仍可见。

    ★P0-0★ 与 TitleScreenDetectedClick 共用 build_detection_params() 的同一套
    已校准参数（4 模板数组 + roi + threshold），不再用失效的单模板。实测：
    12 张按钮可见真实帧全部 hit=True，黑/白屏加载态真实帧 + 纯色帧全部 hit=False。
    """
    p = build_detection_params(cfg)
    reco_param = JTemplateMatch(
        template=list(p["template"]),
        threshold=[p["threshold"]],
        method=p["method"],
    )
    if p.get("roi"):
        reco_param.roi = tuple(p["roi"])

    def _visible(frame) -> bool:
        try:
            import numpy as np
            job = tasker.post_recognition(JRecognitionType.TemplateMatch, reco_param,
                                          np.ascontiguousarray(frame))
            job.wait()
            detail = job.get()
            nid = detail.node_id_list[0] if (detail and getattr(detail, "node_id_list", None)) else None
            if nid is None:
                return False
            node = tasker.get_node_detail(nid)
            return bool(getattr(getattr(node, "recognition", None), "hit", False))
        except Exception as exc:  # noqa: BLE001 - 无法确定时保守处理，阻止提前成功
            log(f"按钮可见性复检异常（按保守处理）: {exc}", "WARN")
            return True

    return _visible


def confirm_in_game_stable(cfg: dict, *, screencap_fn, is_alive_fn, button_visible_fn,
                           log_fn=log, now_fn=time.monotonic, sleep_fn=time.sleep) -> tuple[bool, str]:
    """★降级判定模式核心★ 点击后必须连续 in_game_stable_sec(5~10s) 同时满足：
      ① 按钮不可见（TOUCH TO START 消失）② 画面非加载态（黑/白/低方差排除）
      ③ 目标进程仍存活
    任一不满足即重置稳定计时，避免『还在加载』被 inverse 误报成登录成功。
    纯依赖注入：--check 用合成帧序列做单元测，不启动游戏、不点击真实窗口。
    """
    flow = cfg["flow"]
    lf = cfg["recognition"]["loading_frame"]
    duration = float(flow["in_game_stable_sec"])
    interval = float(flow["stable_check_interval_sec"])
    hard_deadline = now_fn() + float(flow["stable_confirm_timeout_sec"])
    stable_start = None
    last_unmet = None
    checks = 0
    while True:
        if now_fn() > hard_deadline:
            hint = f"（最后未满足: {last_unmet}）" if last_unmet else ""
            return False, f"稳定确认超时({flow['stable_confirm_timeout_sec']}s){hint}"
        if not is_alive_fn():
            return False, "复检期间进程退出"
        frame = screencap_fn()
        checks += 1
        reason = None
        if is_loading_frame(frame, lf["black_mean_max"], lf["white_mean_min"], lf["std_min"]):
            reason = "加载态(黑/白/低方差)"
        elif button_visible_fn(frame):
            reason = "TOUCH TO START 仍可见"
        if reason:
            last_unmet = reason
            if stable_start is not None:
                log_fn(f"复检#{checks}: {reason} → 重置稳定计时", "WARN")
            stable_start = None
        else:
            if stable_start is None:
                stable_start = now_fn()
                log_fn(f"复检#{checks}: 条件满足，开始累计稳定窗口({duration:.0f}s)")
            elif now_fn() - stable_start >= duration:
                return True, (f"连续稳定 {now_fn() - stable_start:.1f}s："
                              f"按钮不可见 + 非加载态 + 进程存活（共 {checks} 次复检）")
        sleep_fn(interval)


def save_probe_frame(frame, tag: str, *, log_fn=log, out_dir=None) -> str:
    """★#21★ 取证留图：把当前**原始截图帧（1280×720，image_target 口径）**写到
    `debug/screencap/in_game_probe_<时间戳>_<tag>.png`，供下次裁剪主界面模板。

    - 存的是 controller 的 `cached_image`（post_screencap 结果），**不是** `debug/vision/` 那种
      被 TemplateMatcher 拼接了模板缩略图、带识别框线的调试图；
    - **只新增文件**：绝不覆盖/删除 `debug/` 下已有帧（红线）；
    - 不依赖 Pillow/OpenCV（tools/make_template.encode_png 纯 Python）。
    """
    try:
        import numpy as np
        from make_template import encode_png  # tools/
        if frame is None or getattr(frame, "size", 0) == 0:
            log_fn(f"取证留图({tag})跳过：空帧", "WARN")
            return ""
        arr = np.asarray(frame)
        if arr.ndim == 3 and arr.shape[2] == 3:
            arr = arr[:, :, ::-1]            # BGR -> RGB
        ts = time.strftime("%Y.%m.%d-%H.%M.%S") + f".{int(time.time() * 1000) % 1000:03d}"
        out = Path(out_dir or (DEBUG_DIR / "screencap")) / f"in_game_probe_{ts}_{tag}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        encode_png(np.ascontiguousarray(arr), out)
        log_fn(f"取证留图: {out.name}（{arr.shape[1]}x{arr.shape[0]}，tag={tag}）")
        return str(out)
    except Exception as exc:  # noqa: BLE001 - 留图失败不得影响判定
        log_fn(f"取证留图失败({tag}): {exc}", "WARN")
        return ""


def make_marker_hit_fn(tasker: Tasker, cfg: dict):
    """★#22★ 返回 callable(frame)->bool：**任一**主界面正向模板在**各自 roi/threshold** 下命中。

    spec 来自 build_marker_params()；其为 None（marker 为 null / 空数组 / 文件缺失）时返回
    **None**，调用方不得判 OK_MARKER、绝不回退裸 inverse。JTemplateMatch.roi 是单个 JTarget，
    无法在一个 recognition 里给多模板各自 roi，故每个模板单独一次 post_recognition。
    """
    spec = build_marker_params(cfg)
    if spec is None:
        return None
    from maa.pipeline import JRecognitionType, JTemplateMatch
    jobs = []
    for f, roi, thr in zip(spec["template"], spec["roi"], spec["threshold"]):
        kw = {"template": [f], "threshold": [thr], "method": spec["method"]}
        if roi:
            kw["roi"] = tuple(int(v) for v in roi)
        jobs.append((f, JTemplateMatch(**kw)))

    def _hit(frame) -> bool:
        try:
            import numpy as np
            arr = np.ascontiguousarray(frame)
            for f, reco in jobs:
                try:
                    job = tasker.post_recognition(JRecognitionType.TemplateMatch, reco, arr)
                    job.wait()
                    detail = job.get()
                    nid = detail.node_id_list[0] if (detail and getattr(detail, "node_id_list", None)) else None
                    if nid is None:
                        continue
                    node = tasker.get_node_detail(nid)
                    if bool(getattr(getattr(node, "recognition", None), "hit", False)):
                        return True
                except Exception as exc:  # noqa: BLE001 - 单模板异常按未命中，继续其余模板
                    log(f"主界面模板 {f} 匹配异常（按未命中）: {exc}", "WARN")
                    continue
        except Exception as exc:  # noqa: BLE001 - 保守：出错按未命中，避免误报 OK_MARKER
            log(f"主界面模板匹配异常（按未命中）: {exc}", "WARN")
        return False

    return _hit


def post_click_verify(cfg: dict, *, screencap_fn, is_alive_fn, button_visible_fn,
                      marker_hit_fn=None, probe_fn=None, log_fn=None,
                      loading_seen_before: bool = False,
                      now_fn=time.monotonic, sleep_fn=time.sleep,
                      click_fn=None) -> tuple[str, str]:
    """★#21★ 点击后 20~30s 持续采样，给出三态判定的「结论」，由 run() 映射成结果码。

    verdict:
      - `MARKER`      : 正向主界面模板命中 → OK_MARKER（确认进入）
      - `LEFT_TITLE`  : 按钮连续 `in_game_absent_sec`(20s) 不可见且当前非加载态 → 仅"疑似成功"
      - `STILL_TITLE` : 窗口结束时按钮仍反复可见（轮播淡入淡出会反复闪现）→ 仍在标题
      - `SESSION_LOST`: 点击后进程/窗口消失（实测点击后客户端会重启）→ 交 run() 重连
      - `TIMEOUT`     : 30s 内既无 marker，也凑不满连续不可见时长

    设计依据（10:25~10:29 真实日志）：点击后 2~6s 裸 inverse 必然命中，但 10:26:11 / 10:28:18
    的 screencap 仍是标题 LOGO 画面，`10:29:04` 消失后 `10:29:06` 按钮又出现——单帧缺失毫无意义。
    所以窗口拉到 30s，且要求「连续 20s 不见」才给疑似结论，绝不因单帧缺失判成功。
    """
    flow = cfg["flow"]
    lf = cfg["recognition"]["loading_frame"]
    lt = cfg["recognition"].get("loading_template") or {}
    log_fn = log_fn or log
    window = float(flow.get("in_game_verify_sec", 30))
    max_window = float(flow.get("in_game_verify_max_sec", 90))
    absent_need = float(flow.get("in_game_absent_sec", 20))
    # ★#21★ 若 pipeline 已经观察到加载页（loading_seen_before），说明画面确实离开过标题，
    # 缺席门槛可以缩短（加载结束本身就是强证据），否则仍要求完整的连续缺席时长。
    if loading_seen_before:
        absent_need = min(absent_need, float(flow.get("in_game_absent_after_loading_sec", 5)))
    # ★#68 F1★ 缺席门槛是否被缩短（决定 popup 点击后是否重置缺席计时，见 popup 分支说明）
    absent_shortened = loading_seen_before
    interval = float(flow.get("verify_sample_interval_sec", 1.0))
    grace = float(flow.get("post_click_grace_sec", 3.0))
    # ★#64★ popup_clear 门控：生产有效门控 = enabled 且 mv/play 任一开启（run() 无条件注入
    # click_fn）；click_fn 未注入时额外禁用，是 --check/离线路径的第三重防御。
    # 普通登录（无 flow 开启）门控不满足 → 行为逐字零回归（两轮实跑验证的链路）。
    popup_cfg = flow.get("popup_clear") or {}
    popup_enabled = (bool(popup_cfg.get("enabled", False))
                     and click_fn is not None
                     and (mv_flow_enabled(cfg) or play_flow_enabled(cfg)))
    popup_min_wait = float(popup_cfg.get("min_wait_sec", 8.0))
    popup_interval = float(popup_cfg.get("interval_sec", 3.0))
    popup_coord = popup_cfg.get("coord") or [640, 360]
    popup_taps = int(popup_cfg.get("taps", 4))
    taps_used = 0
    last_tap_at = None                      # ★#64★ 上次 popup 点击时刻（interval 间隔控制）

    start = now_fn()
    absent_since = None
    samples = 0
    loading_seen = False
    last_visible_at = None

    while True:
        if not is_alive_fn():
            return "SESSION_LOST", f"点击后目标进程消失（{now_fn() - start:.1f}s，疑似客户端重启）"
        try:
            frame = screencap_fn()
        except Exception as exc:  # noqa: BLE001 - 截图失败按会话丢失处理
            return "SESSION_LOST", f"点击后截图失败（{exc}）"
        if frame is None or getattr(frame, "size", 0) == 0:
            return "SESSION_LOST", "点击后截图返回空帧"

        samples += 1
        elapsed = now_fn() - start
        loading, linfo = detect_loading_frame(
            frame, black_mean_max=lf["black_mean_max"], white_mean_min=lf["white_mean_min"],
            std_min=lf["std_min"], white_std_max=lf["white_std_max"],
            template=lt.get("file"), template_threshold=lt.get("threshold", 0.6),
            template_roi=tuple(lt.get("roi", (860, 630, 340, 90))),
        )
        loading_seen = loading_seen or loading

        # ★#22★ 加载态帧绝不参与正向判定（不得把加载态误判为成功）
        if not loading and marker_hit_fn is not None and marker_hit_fn(frame):
            if probe_fn:
                probe_fn(frame, "marker_hit")
            return "MARKER", f"第 {samples} 次采样命中正向主界面模板（{elapsed:.1f}s）"

        vis = bool(button_visible_fn(frame))

        # ★#64★ 登录确认阶段通用弹窗清除（popup_clear）：每日登录奖励/活动公告等全屏
        # 弹窗遮挡主界面时，用户确认的关闭交互 = 连点屏幕依次关闭（无专用 × 钮）。
        # 触发条件全满足才点击：过 min_wait_sec + 非加载态 + marker 未命中 + 按钮不可见；
        # 每次点击前 WARN+留图 popup_tap_<n>（#62 最后采样帧机制=点击前画面）；
        # 点击后继续正常采样，marker 命中立即停止并正常确认；taps 用尽→维持现有降级结论。
        if (popup_enabled and taps_used < popup_taps and elapsed >= popup_min_wait
                and (last_tap_at is None or now_fn() - last_tap_at >= popup_interval)
                and not loading and not vis):
            taps_used += 1
            last_tap_at = now_fn()
            log_fn(f"  ★popup_clear：主界面疑似被弹窗遮挡（marker 未命中/按钮不可见，"
                   f"t={elapsed:.1f}s），按用户语义连点屏幕关闭（第 {taps_used}/{popup_taps} 次）",
                   "WARN")
            if probe_fn:
                try:
                    probe_fn(frame, f"popup_tap_{taps_used}")
                except Exception as exc:  # noqa: BLE001 - 留图失败不影响清除
                    log_fn(f"  popup_clear 留图异常（忽略）: {exc}", "WARN")
            try:
                click_fn(int(popup_coord[0]), int(popup_coord[1]))
            except Exception as exc:  # noqa: BLE001 - 点击失败继续采样
                log_fn(f"  popup_clear 点击异常（继续采样）: {exc}", "WARN")
            # ★#68 F1★ 点击后重置「连续不可见」计时基准：loading_seen_before=True 时
            # absent_need 被缩短到 5s，点击前累计的缺席时长会在下一次采样立即凑满，
            # 抢在 taps 用完前收口为 LEFT_TITLE（实测只点 1 次就把剩余 3 次吞掉）。
            # 仅在缩短门槛路径重置：默认 20s 门槛下若也重置，4 次点击（≈12s）后需再等
            # 20s 才够，会超出 30s 判定窗口 → LEFT_TITLE 变 TIMEOUT（exit 3→2），
            # 属三态语义变化，本修复不取。三态判定与成功判据、taps/预算语义均不变。
            if absent_shortened:
                absent_since = None
            sleep_fn(interval)
            continue

        if elapsed >= grace:
            if vis:
                last_visible_at = elapsed
                absent_since = None
            elif absent_since is None:
                absent_since = now_fn()

        log_fn(f"  判定采样#{samples} t={elapsed:.1f}s mean={float(frame.mean()):.0f} "
               f"std={float(frame.std()):.0f} loading={loading}({linfo.get('reason')}"
               f"{'' if 'tpl_score' not in linfo else f',tpl={linfo['tpl_score']}'}) "
               f"button_visible={vis}")

        if absent_since is not None and not loading and now_fn() - absent_since >= absent_need:
            if probe_fn:
                probe_fn(frame, "unverified")
            return "LEFT_TITLE", (f"按钮连续 {now_fn() - absent_since:.1f}s 不可见且非加载态"
                                  f"（{samples} 次采样，期间观察到加载态={loading_seen}），"
                                  f"但无正向主界面标志 → 仅疑似成功")

        if now_fn() - start >= window:
            # ★#21★ 窗口到期但画面仍在加载页（用户实测加载页可长达数十秒）→ 延长，不判失败
            if loading and window < max_window:
                window = min(window + float(flow.get("in_game_verify_sec", 30)), max_window)
                log_fn(f"  判定窗口到期但仍在加载态，延长到 {window:.0f}s（上限 {max_window:.0f}s）")
                sleep_fn(interval)
                continue
            if probe_fn:
                probe_fn(frame, "still_title" if last_visible_at is not None else "timeout")
            if last_visible_at is not None:
                return "STILL_TITLE", (f"{window:.0f}s 窗口内按钮仍反复可见"
                                       f"（最后一次可见 t={last_visible_at:.1f}s）")
            return "TIMEOUT", (f"{window:.0f}s 窗口结束仍未确认"
                               f"（按钮不可见时长不足 {absent_need:.0f}s，loading_seen={loading_seen}）")

        sleep_fn(interval)


def direct_marker_confirm(frame, cfg: dict, marker_hit_fn, *, log_fn=None, probe_fn=None) -> bool:
    """★#23 P1-2★ 主界面正向「直判」：非加载态帧 + 任一主界面模板命中 → True（→ OK_MARKER/0）。

    背景：点击后客户端会重启 → post_click_verify 返回 SESSION_LOST → run() 丢弃会话重连。
    重连后若游戏**直接落在主界面**（按钮不可见），`TitleScreenDetectedClick` 本轮永不命中 →
    `sink.is_success()`=False → 门控 continue → post_click_verify（原来唯一能做 OK_MARKER 的入口）
    永不调用 → 「明明进了游戏却判失败」。#22 把正向判定挂在了「标题→点击→按钮消失」信号链之后，
    重连正好打破这条链。故 run() 在每轮 pipeline 前/门控前用本函数对当前帧直判一次，绕开该门槛。

    硬守护（与 post_click_verify 一致，绝不引入假成功）：
      - marker_hit_fn 为 None（marker=null/空数组/文件缺失）→ False，绝不回退裸 inverse；
      - 空帧 → False；加载态帧 → False（不得把加载态误判成功）；判加载异常 → False；
      - 标题帧实测 3 模板零命中（NCC 表 / --check [24]）；
      - 只返回 bool，由调用方决定是否 return OK_MARKER，三态 0/3/2 仍全部可达。
    """
    if marker_hit_fn is None or frame is None or getattr(frame, "size", 0) == 0:
        return False
    log_fn = log_fn or log
    rec = cfg["recognition"]
    lf = rec["loading_frame"]
    lt = rec.get("loading_template") or {}
    try:
        loading, _ = detect_loading_frame(
            frame, black_mean_max=lf["black_mean_max"], white_mean_min=lf["white_mean_min"],
            std_min=lf["std_min"], white_std_max=lf["white_std_max"],
            template=lt.get("file"), template_threshold=lt.get("threshold", 0.6),
            template_roi=tuple(lt.get("roi", (860, 630, 340, 90))),
        )
    except Exception as exc:  # noqa: BLE001 - 保守：判加载失败不直判
        log_fn(f"主界面直判：加载态判定异常（按未命中）: {exc}", "WARN")
        return False
    if loading:
        return False
    if not marker_hit_fn(frame):
        return False
    if probe_fn:
        try:
            probe_fn(frame, "marker_hit")
        except Exception as exc:  # noqa: BLE001 - 留图失败不影响判定
            log_fn(f"主界面直判留图异常（忽略）: {exc}", "WARN")
    return True


# --------------------------------------------------------------------------- #
# ★#39★ MV 探索性实跑采集模式（方案 B）：只读旁观、逐帧差分去重留图
#   设计铁律：flow.mv_explore 默认 false，关闭时 _finish_ok 直接返回 OK_MARKER，
#   不截图、不留图、不写任何日志、不改变时序 → 对两轮实跑验证的登录链路逐字零回归。
#   开启时仅在「登录已确认成功」后于同一会话内旁路采集，绝不点击/改判定/动三态。
#   screencap_fn / is_alive_fn / save_fn / sleep_fn / now_fn 全部可注入，
#   供 .workbuddy/artifacts/31-verify.py 用合成帧序列离线驱动（无需真实控制器）。
# --------------------------------------------------------------------------- #
def mv_explore_enabled(cfg: dict) -> bool:
    """采集模式总开关：flow.mv_explore 默认 false（key 缺失亦为 false，兼容旧配置）。"""
    return bool(cfg.get("flow", {}).get("mv_explore", False))


def _mv_frame_signature(frame, *, step: int, expected_w: int, expected_h: int,
                        tol: int, black_mean_max: float):
    """返回 (small_gray, ok, why)：small_gray 为降采样灰度浮点矩阵（帧差基准）。

    ok=False 表示该帧疑似『终端被截到 / 全黑 / 尺寸不符』，必须跳过、不得当有效素材存下，
    也不得更新差分基准。仅用尺寸 + 极暗均值判据（不识别终端内容，避免引入模板/误判）。
    """
    import numpy as np
    if frame is None or getattr(frame, "size", 0) == 0:
        return None, False, "空帧"
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] != 3:
        return None, False, f"维度/通道不符 shape={arr.shape}"
    h, w = int(arr.shape[0]), int(arr.shape[1])
    if abs(w - expected_w) > tol or abs(h - expected_h) > tol:
        return None, False, f"尺寸不符 {w}x{h}（应为 {expected_w}x{expected_h}±{tol}）"
    small = arr[::step, ::step, :].astype(np.float32).mean(axis=2)
    if float(small.mean()) < black_mean_max:
        return None, False, "全黑/极暗帧"
    return small, True, ""


def mv_explore_loop(cfg: dict, *, screencap_fn, is_alive_fn, save_fn,
                    log_fn=log, sleep_fn=time.sleep, now_fn=time.monotonic) -> dict:
    """★#39★ 登录确认成功后的旁路采集：只读旁观，绝不触碰游戏。

    只用 screencap_fn() 取帧、save_fn(frame, tag) 留图、is_alive_fn() 判进程；
    绝不点击、发任务、做识别、停任务、抢窗口焦点、重启游戏、改阈值、进登录兜底。
    结束条件：Ctrl+C（优雅收尾，不吞掉后继续跑）、总时长上限、最大留图数、
    或进程消失。任何情况结束后都返回统计 dict，调用方仍返回 OK_MARKER/exit 0。
    """
    import numpy as np
    stat = {"enabled": False, "seen": 0, "saved": 0, "skipped": 0,
            "bad_frames": 0, "stop_reason": "disabled", "files": [], "frame_meta": []}
    if not mv_explore_enabled(cfg):
        return stat                       # ★关闭：零截图/零留图/零日志/零副作用★
    stat["enabled"] = True

    ec = dict(cfg.get("flow", {}).get("mv_explore_cfg") or {})
    interval = float(ec.get("sample_interval_sec", 0.9))
    max_duration = float(ec.get("max_duration_sec", 180))
    diff_thr = float(ec.get("frame_diff_threshold", 12.0))
    max_frames = int(ec.get("max_frames", 60))
    only_changed = bool(ec.get("only_changed_frames", True))
    expected_w = int(ec.get("expected_width", 1280))
    expected_h = int(ec.get("expected_height", 720))
    tol = int(ec.get("geometry_tolerance", 4))
    black_mean_max = float(ec.get("black_mean_max", 12.0))
    warn_after = max(1, int(ec.get("bad_frame_warn_after", 3)))
    step = max(1, int(ec.get("diff_downsample_step", 8)))

    log_fn("=" * 62)
    log_fn("【MV 探索性实跑采集模式】已启动（只旁观截图，不点击、不改判定、不改三态）")
    log_fn("请在游戏里手动依次完成六步：")
    log_fn("  1) 点击主界面右下角 Live")
    log_fn("  2) 点击「观看 MV」")
    log_fn("  3) 点击「开始」")
    log_fn("  4) 点击屏幕唤起控制页")
    log_fn("  5) 点右上角最右边的快进键退出 MV")
    log_fn("  6) 点左下角返回键回到主页面")
    log_fn("★期间请把【管理员终端最小化】、保持游戏窗口在前台★", "WARN")
    log_fn("  （Foreground 截图依赖游戏窗口前台；终端被切到前台时可能截到终端/黑帧，")
    log_fn("   脚本会自动跳过并 WARN，但那几秒可能丢素材；切回终端按 Ctrl+C 前采集可能已结束）")
    log_fn("★MV 播放段请尽快完成第 4/5/6 步，不要等 MV 播完★"
           "（视频段每帧都变，留图额度消耗最快）", "WARN")
    log_fn("采集上限已放宽；回传前请按 0001..N 顺序目视剔除重复帧与终端/桌面帧。")
    log_fn("走完六步后切回终端按 Ctrl+C 结束；或等待自动结束（时长/留图数上限 / 游戏退出）。")
    log_fn("★收尾若打印 stop_reason=max_frames，说明额度耗尽、素材未采全，需重跑★", "WARN")
    log_fn(f"采样 {interval}s/帧，最长 {max_duration:.0f}s，最多留 {max_frames} 张；"
           f"差分阈值 {diff_thr:g}，仅变化帧留图={only_changed}")
    log_fn("=" * 62)

    started = now_fn()
    prev_small = None
    consecutive_bad = 0
    warned_bad = False
    seq = 0
    stop_reason = "max_duration"
    try:
        while True:
            if now_fn() - started >= max_duration:
                stop_reason = "max_duration"
                break
            if stat["saved"] >= max_frames:
                stop_reason = "max_frames"
                break
            try:
                if not is_alive_fn():
                    stop_reason = "process_gone"
                    break
            except Exception as exc:  # noqa: BLE001 - 存活检查异常按失联收尾
                log_fn(f"MV 采集：进程存活检查异常（按失联收尾）: {exc}", "WARN")
                stop_reason = "process_gone"
                break

            # 只读取帧；★不抢窗口焦点★（会干扰用户手动点击）
            try:
                frame = screencap_fn()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                log_fn(f"MV 采集：取帧失败（跳过本帧）: {exc}", "WARN")
                frame = None
            stat["seen"] += 1

            small, ok, why = _mv_frame_signature(
                frame, step=step, expected_w=expected_w, expected_h=expected_h,
                tol=tol, black_mean_max=black_mean_max)
            if not ok:
                stat["skipped"] += 1
                stat["bad_frames"] += 1
                consecutive_bad += 1
                if consecutive_bad >= warn_after and not warned_bad:
                    log_fn(f"MV 采集：连续 {consecutive_bad} 帧疑似终端/全黑/尺寸不符"
                           f"（{why}），已跳过；请保持游戏窗口在前台、不要把终端切到前面", "WARN")
                    warned_bad = True
                # 无效帧不更新差分基准，避免把终端/黑帧当成「页面切换」
            else:
                consecutive_bad = 0
                warned_bad = False
                changed = (prev_small is None or not only_changed or
                           small.shape != prev_small.shape or
                           float(np.abs(small - prev_small).mean()) >= diff_thr)
                if changed:
                    tag = f"mv_explore_{seq + 1:04d}"
                    try:
                        path = save_fn(frame, tag)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log_fn(f"MV 采集：留图失败({tag})（跳过）: {exc}", "WARN")
                        path = ""
                    seq += 1
                    stat["saved"] += 1
                    if path:
                        stat["files"].append(str(path))
                        stat["frame_meta"].append(
                            (str(path), float(small.mean()), float(small.std())))
                    prev_small = small

            sleep_fn(max(0.0, min(interval, max_duration - (now_fn() - started))))
    except KeyboardInterrupt:
        stop_reason = "ctrl_c"          # ★不吞掉后继续跑：直接跳出循环进入收尾★
    except Exception as exc:  # noqa: BLE001 - 采集是旁路，异常不得影响登录，但须 WARN
        stop_reason = "error"
        log_fn(f"MV 采集循环异常（已收尾，登录结果不受影响）: {exc}", "WARN")
    finally:
        stat["stop_reason"] = stop_reason

    log_fn("=" * 62)
    log_fn(f"【MV 探索性实跑采集】结束（stop_reason={stop_reason}）")
    log_fn(f"取帧 {stat['seen']} 次，留图 {stat['saved']} 张，跳过无效帧 {stat['skipped']} 张"
           f"（疑似终端/全黑/尺寸不符 {stat['bad_frames']} 张）")
    if stop_reason == "max_frames":
        log_fn("★stop_reason=max_frames：留图额度耗尽，可能未走到第 5/6 步，"
               "素材未采全，需重跑★", "WARN")
    if stop_reason == "process_gone":
        log_fn("游戏进程已退出，采集提前收尾（登录结果不受影响）", "WARN")
    if stop_reason == "ctrl_c":
        log_fn("已收到 Ctrl+C，采集优雅收尾（登录结果不受影响）")
    if stop_reason == "error":
        log_fn("★采集循环异常结束，素材可能不完整，需重跑★", "WARN")
    log_fn(f"留图目录: {DEBUG_DIR / 'screencap'}")
    if stat["files"]:
        log_fn("本次留图清单（按序，附灰度 mean/std，便于剔除终端/桌面帧）:")
        for p, mean, std in stat["frame_meta"]:
            log_fn(f"    {p}  mean={mean:.1f} std={std:.1f}")
    else:
        log_fn("本次无留图（可能全是重复帧/无效帧，或采集被立即结束），需重跑", "WARN")
    log_fn("=" * 62)
    return stat


def mv_flow_enabled(cfg: dict) -> bool:
    """★#49★ MV 自动观看流程总开关：flow.mv_enabled 默认 false（缺 key 兼容旧配置）。"""
    return bool(cfg.get("flow", {}).get("mv_enabled", False))


def play_flow_enabled(cfg: dict) -> bool:
    """★#54★ 自动打歌流程总开关：flow.play_enabled 默认 false（缺 key 兼容旧配置）。"""
    return bool(cfg.get("flow", {}).get("play_enabled", False))


def _mv_match(tasker, frame, tpl: str, threshold: float, roi):
    """★#49★ MV 单模板判定（同 make_marker_hit_fn 的 post_recognition 机制）。

    返回 (hit, box_center, score)：box_center 为截图缩放坐标 (cx, cy)（post_click 期望
    缩放坐标，同 ClickCenterAction 的 box 中心算法）；score 为模板匹配分（框架未提供时 1.0）；
    未命中 / 异常一律 (False, None, 0.0)，绝不误报。
    """
    from maa.pipeline import JRecognitionType, JTemplateMatch
    try:
        import numpy as np
        kw = {"template": [tpl], "threshold": [float(threshold)], "method": 5}
        if roi:
            kw["roi"] = tuple(int(v) for v in roi)
        arr = np.ascontiguousarray(frame)
        job = tasker.post_recognition(JRecognitionType.TemplateMatch, JTemplateMatch(**kw), arr)
        job.wait()
        detail = job.get()
        nid = detail.node_id_list[0] if (detail and getattr(detail, "node_id_list", None)) else None
        if nid is None:
            return False, None, 0.0
        node = tasker.get_node_detail(nid)
        rec = getattr(node, "recognition", None)
        if not bool(getattr(rec, "hit", False)):
            return False, None, 0.0
        score = getattr(rec, "score", None)
        try:
            score = float(score) if score is not None else 1.0
        except (TypeError, ValueError):
            score = 1.0
        box = getattr(rec, "box", None)
        if box is None:
            return True, None, score
        return True, (int(box.x + box.w // 2), int(box.y + box.h // 2)), score
    except Exception as exc:  # noqa: BLE001 - 匹配异常按未命中
        log(f"MV 模板 {tpl} 匹配异常（按未命中）: {exc}", "WARN")
        return False, None, 0.0


def _mv_match_multi(tasker, frame, tpls, thrs, rois):
    """★#60★ 多模板任一命中（box 取最高分者），与 in_game_marker 数组机制同风格。

    tpls/thrs/rois 等长数组（缺项回退末项/0.75/None）；全部未命中返回 (False, None, 0.0, "")。
    ★#61 P2-2★ 返回第 4 元 = 实际命中模板名（供 re_click WARN 输出真实命中者）。"""
    best = (False, None, 0.0, "")
    for i, t in enumerate(tpls):
        thr = thrs[i] if i < len(thrs) else (thrs[-1] if thrs else 0.75)
        roi = rois[i] if i < len(rois) and rois[i] else (rois[-1] if rois else None)
        hit, center, score = _mv_match(tasker, frame, t, thr, roi)
        if hit and score >= best[2]:
            best = (True, center, score, t)
    return best


def run_mv_flow(cfg: dict, *, ctrl, tasker, hwnd, marker_hit_fn, monitor,
                log_fn=log, sleep_fn=time.sleep, now_fn=time.monotonic,
                save_fn=None) -> tuple[bool, str]:
    """★#49★ MV 自动观看流程（flow.mv_enabled=true 时由 _finish_ok 调用）。

    七步语义（按 17.27~17.28 真实采集素材目视修正，比最初六步多一次返回）：
      ①主界面点 Live → ②乐曲选择页点「观看MV」 → ③队伍确认页点「开始」
      → ④MV 载入播放，点屏幕唤起控制条 → ⑤点顶部最右 ⏭ 退出 → Loading
      → ⑥落回乐曲选择页，点左下 ◀ → ⑦主界面（marker 三模板正向确认）。

    业务失败（步骤重试耗尽 / 整体超时 / 进程退出）返回 (False, why) → FAIL_MV_FLOW；
    不重启游戏、不改阈值、不进登录兜底。
    ★#54★ 通用机制抽入 _run_flow_generic（与 run_play_flow 共用），本壳只读 flow.mv 配置。
    """
    return _run_flow_generic(
        cfg, ctrl=ctrl, tasker=tasker, hwnd=hwnd, marker_hit_fn=marker_hit_fn,
        monitor=monitor, flowcfg=cfg.get("flow", {}).get("mv") or {},
        label="MV 自动观看流程", short="MV", tag_prefix="mv",
        log_fn=log_fn, sleep_fn=sleep_fn, now_fn=now_fn, save_fn=save_fn)


def run_play_flow(cfg: dict, *, ctrl, tasker, hwnd, marker_hit_fn, monitor,
                  log_fn=log, sleep_fn=time.sleep, now_fn=time.monotonic,
                  save_fn=None) -> tuple[bool, str]:
    """★#54★ 自动打歌（跳过模式）流程（flow.play_enabled=true 时由 _finish_ok 调用）。

    八步语义（按 20.15~20.16 真实采集素材目视确认）：
      ①主界面点 Live → ②乐曲选择页点「決定」 → ③队伍确认页点「跳过」（避开剩余次数数字）
      → ④スキップライブ確認弹窗：precheck「常に最大已勾选」（未命中点勾选框一次后重验，
        仍未命中 WARN+留图并★降级继续★）→ 点「開 始」 → ⑤結果页点「OK」
      → ⑥队伍确认页点 ◀ → ⑦乐曲选择页点 ◀ → ⑧主界面确认（marker 三模板，留图
        play_return_home）。

    业务失败返回 (False, why) → FAIL_PLAY_FLOW；与 run_mv_flow 共用 _run_flow_generic
    通用机制（precheck/弹窗钩子/重试/几何守卫全部配置驱动）。
    """
    return _run_flow_generic(
        cfg, ctrl=ctrl, tasker=tasker, hwnd=hwnd, marker_hit_fn=marker_hit_fn,
        monitor=monitor, flowcfg=cfg.get("flow", {}).get("play") or {},
        label="自動打歌（跳过模式）流程", short="打歌", tag_prefix="play",
        log_fn=log_fn, sleep_fn=sleep_fn, now_fn=now_fn, save_fn=save_fn)


_CONFIRM_FALLBACK_WARNED: set = set()


def _warn_confirm_fallback_once(tpl: str, log_fn=log) -> None:
    """★#56★ confirm 模板无显式 roi 时一次性 WARN（模块级去重，避免刷屏）。"""
    if tpl in _CONFIRM_FALLBACK_WARNED:
        return
    _CONFIRM_FALLBACK_WARNED.add(tpl)
    log_fn(f"confirm 模板 {tpl} 无显式 roi 条目，使用全图匹配（建议在 flow 配置 "
           f"confirm_params 中显式化）", "WARN")


def _run_flow_generic(cfg: dict, *, ctrl, tasker, hwnd, marker_hit_fn, monitor,
                      flowcfg, label: str, short: str, tag_prefix: str,
                      log_fn=log, sleep_fn=time.sleep, now_fn=time.monotonic,
                      save_fn=None) -> tuple[bool, str]:
    """★#49/#54★ 通用多步流程执行器（run_mv_flow / run_play_flow 共用）。

    每步：force_foreground → 截帧 → precheck 前置钩子（可选，配置驱动）→ 模板判定 →
    box_center 点击（coord 步先做 1280×720±4 几何守卫，click=none 为纯确认步）→
    轮询正向确认（step_timeout_sec）→ 超时按 step_retry 重试 → 失败留图
    {tag_prefix}fail_<step> 并 WARN。确认等待中每次采样先过 _check_dialog 弹窗钩子。
    未知弹窗段一律 WARN+留图，绝不脑补点击。
    业务失败（步骤重试耗尽 / 整体超时 / 进程退出）返回 (False, why)；不重启游戏、
    不改阈值、不进登录兜底。sleep_fn/now_fn/save_fn/log_fn 可注入（离线单测）。
    """
    lf = log_fn
    if save_fn is None:
        save_fn = lambda f, tag: save_probe_frame(f, tag, log_fn=lf)  # noqa: E731

    mvcfg = dict(flowcfg or {})
    steps = list(mvcfg.get("steps") or [])
    overall = float(mvcfg.get("overall_timeout_sec", 240))
    step_to = float(mvcfg.get("step_timeout_sec", 15))
    retry = int(mvcfg.get("step_retry", 2))
    home_to = float(mvcfg.get("return_home_confirm_sec", 20))
    interval = 0.9
    # ★#56 B1★ 流程启动前稳定延迟（主界面淡入动画期间点击易被吞，56 实测两轮同型失败）
    start_delay = float(mvcfg.get("start_delay_sec", 3.0))
    # ★#51★ 确认等待中的弹窗检查钩子配置（放弃中断Live确认 → 自动点「放 弃」）
    dialog_check = bool(mvcfg.get("dialog_check", False))
    dialogs = list(mvcfg.get("dialogs") or [])
    dialog_click_count: dict = {}   # 同一弹窗的自动点击计数（防快速连点；超上限只 WARN 交由超时/重试）

    if not steps:
        lf(f"{label}：flow.steps 为空 → 视为未配置，按失败上报", "WARN")
        return False, "steps empty"

    # confirm 模板的 (threshold, roi) 从 steps 收集（confirm 通常是下一步页面的按钮）
    tpl_params = {}
    for s2 in steps:
        t2 = (s2.get("template") or [None])[0]
        if t2:
            tpl_params[t2] = ((s2.get("threshold") or [0.75])[0],
                              (s2.get("roi") or [None])[0])
    # ★#56 B3★ flow 级 confirm_params：为不在 steps 里的 confirm 模板补显式 threshold/roi
    for ct2, cp2 in (mvcfg.get("confirm_params") or {}).items():
        tpl_params[ct2] = ((cp2.get("threshold") or [0.75])[0],
                           (cp2.get("roi") or [None])[0])

    rec = cfg.get("recognition", {})
    lf_cfg = rec.get("loading_frame", {})
    lt = rec.get("loading_template") or {}

    def _loading(frame):
        try:
            hit, _ = detect_loading_frame(
                frame, black_mean_max=lf_cfg.get("black_mean_max", 45.0),
                white_mean_min=lf_cfg.get("white_mean_min", 220.0),
                std_min=lf_cfg.get("std_min", 8.0),
                white_std_max=lf_cfg.get("white_std_max", 12.0),
                template=lt.get("file"), template_threshold=lt.get("threshold", 0.6),
                template_roi=tuple(lt.get("roi", (860, 630, 340, 90))))
            return hit
        except Exception as exc:  # noqa: BLE001 - 判加载失败按非加载（不阻塞流程，交由正向确认把关）
            lf(f"{label}：加载态判定异常（按非加载）: {exc}", "WARN")
            return False

    def _snap():
        try:
            return ctrl.post_screencap().wait().get()
        except Exception as exc:  # noqa: BLE001
            lf(f"{label}：截图失败: {exc}", "WARN")
            return None

    def _check_dialog(f2):
        """★#51★ 确认等待中的弹窗检查钩子：命中已配置弹窗 → WARN+留图+自动点配置的按钮。

        返回 True 表示检测并处理了一个弹窗（调用方继续等待原确认链自然恢复）。
        「返回标题」类按钮绝不自动点 —— flow.mv.dialogs 配置只包含用户明确决策的点击目标
        （#50 实测 19:22:35 弹窗：用户决策=自动点「放 弃」继续）。
        点「放 弃」后弹窗仍在/确认仍超时 → 走既有失败路径（retry→FAIL_MV_FLOW），不无限循环。
        """
        if not dialog_check:
            return False
        for dlg in dialogs:
            t = (dlg.get("template") or [None])[0]
            if not t:
                continue
            thr = (dlg.get("threshold") or [0.75])[0]
            roi = (dlg.get("roi") or [None])[0]
            hit, _, _scd = _mv_match(tasker, f2, t, thr, roi)
            if not hit:
                continue
            did = str(dlg.get("id", "dialog"))
            lf(f"检测到弹窗[{did}]（{t} 命中），按用户决策自动处理", "WARN")
            save_fn(f2, f"{tag_prefix}_dialog_{did}")
            ct = (dlg.get("click_template") or [None])[0]
            if not ct:
                return True
            n = dialog_click_count.get(did, 0)
            if n >= 3:
                # 同一弹窗已自动点 3 次仍在 → 停止重复点击（防快速连点），交由确认超时/重试失败
                lf(f"弹窗[{did}]：已自动点击 {n} 次仍在 → 停止重复点击，"
                   f"交由确认超时/重试处理", "WARN")
                return True
            cthr = (dlg.get("click_threshold") or [0.75])[0]
            croi = (dlg.get("click_roi") or [None])[0]
            f3 = _snap()
            chit, center = False, None
            if f3 is not None and getattr(f3, "size", 0) != 0:
                chit, center, _scd = _mv_match(tasker, f3, ct, cthr, croi)
            if chit and center:
                dialog_click_count[did] = n + 1
                try:
                    job = ctrl.post_click(int(center[0]), int(center[1]))
                    job.wait()
                    lf(f"弹窗[{did}]：已自动点击第 {n + 1} 次 ({center[0]},{center[1]})", "OK")
                except Exception as exc:  # noqa: BLE001
                    lf(f"弹窗[{did}] 点击异常: {exc}", "WARN")
            else:
                lf(f"弹窗[{did}]：点击按钮未命中（不脑补点击，交由确认超时/重试处理）", "WARN")
            return True
        return False

    def _precheck(pc, sid, frame):
        """★#54★ 点击主按钮前的前置检查（配置驱动，如打歌「常に最大已勾选」）。

        返回 (ok, why)：True=已勾选（或点击勾选框一次后重验恢复）；False=未命中
        （调用方按配置降级继续 —— 未勾选不危险，只是 LB 消耗非最大，如实记录）。"""
        pt = (pc.get("template") or [None])[0]
        if not pt:
            return True, "no precheck template"
        pthr = (pc.get("threshold") or [0.75])[0]
        proi = (pc.get("roi") or [None])[0]
        if frame is not None and getattr(frame, "size", 0) != 0:
            hit, _, _scd = _mv_match(tasker, frame, pt, pthr, proi)
            if hit:
                return True, f"{pt} already checked"
        coord = pc.get("coord")
        if not coord:
            return False, f"{pt} unchecked and no coord"
        import numpy as np
        if frame is None:
            return False, "precheck screencap invalid"
        arr = np.asarray(frame)
        h, w = int(arr.shape[0]), int(arr.shape[1])
        if abs(w - 1280) > 4 or abs(h - 720) > 4:
            return False, f"precheck geometry {w}x{h}"
        try:
            job = ctrl.post_click(int(coord[0]), int(coord[1]))
            job.wait()
        except Exception as exc:  # noqa: BLE001
            lf(f"{label} precheck 点击异常: {exc}", "WARN")
            return False, f"precheck click error: {exc}"
        lf(f"{short} 步骤 {sid}：precheck 未命中 → 已点击勾选框 "
           f"({coord[0]},{coord[1]})，重新截帧验证")
        f3 = _snap()
        if f3 is None or getattr(f3, "size", 0) == 0:
            return False, "precheck re-verify screencap invalid"
        hit2, _, _scd = _mv_match(tasker, f3, pt, pthr, proi)
        if hit2:
            return True, f"{pt} checked after click"
        return False, f"{pt} unchecked after click"

    def _maybe_re_click(f2, tpls, thrs, rois, sid, re_click, state):
        """★#56 B2★ 确认轮询中的二次点击：页面未切换（主模板仍命中）→ 按间隔重击主按钮。

        re_click 配置 = {"interval_sec": .., "max_clicks": ..}；页面已切换（主模板不再命中）
        → 停止；达 max_clicks 上限 → 停止。每次重击 WARN 计次。"""
        if not re_click or state["count"] >= int(re_click.get("max_clicks", 2)):
            return
        if now_fn() - state["last"] < float(re_click.get("interval_sec", 5)):
            return
        hit3, c3, sc3, hit_tpl = _mv_match_multi(tasker, f2, tpls, thrs, rois)
        if not hit3 or c3 is None:
            return                     # 页面已切换 → 停止重击
        try:
            job = ctrl.post_click(int(c3[0]), int(c3[1]))
            job.wait()
            state["count"] += 1
            state["last"] = now_fn()
            lf(f"{short} 步骤 {sid}：确认窗口内页面未切换（实际命中 {hit_tpl} "
               f"score={sc3:.3f}），re_click 第 {state['count']}/"
               f"{int(re_click.get('max_clicks', 2))} 次 ({c3[0]},{c3[1]})", "WARN")
        except Exception as exc:  # noqa: BLE001
            lf(f"{label} re_click 点击异常: {exc}", "WARN")

    def _keep_last(out, f2):
        """★#68 F4★ 仅在拿到**有效帧**时更新「最后采样帧」。

        末次截图失败（None）/空帧不得覆盖上一个有效帧 —— 否则失败留图会回退到 attempt
        起始帧，正是 #62 要消灭的取证失真。
        """
        if out is not None and f2 is not None and getattr(f2, "size", 0) != 0:
            out["last_frame"] = f2

    def _confirm(confirm, tpls, thrs, rois, sid="", re_click=None, out=None):
        """点击后的正向确认。返回 (ok, why)。★#51★ 每次采样先做弹窗检查钩子；
        ★#56 B2★ re_click 配置存在时按间隔重击主按钮（页面未切换才击）。
        ★#61 P2-1★ out 为可变容器（dict）：轮询期间持续更新 out["last_frame"]=
        最后采样帧（内存引用，勿频繁写盘）；确认失败时调用方用「最后采样帧」留图
        （最接近失败时刻的画面），轮询一次都没执行过（极端）才回退 attempt 起始帧。"""
        re_state = {"count": 0, "last": now_fn()}
        if confirm == "home":
            end = now_fn() + home_to
            while now_fn() < end:
                f2 = _snap()
                _keep_last(out, f2)               # ★#61/#68★ 最后有效采样帧
                if f2 is not None and getattr(f2, "size", 0) != 0:
                    if _check_dialog(f2):
                        sleep_fn(interval)
                        continue
                    _maybe_re_click(f2, tpls, thrs, rois, sid, re_click, re_state)
                    if not _loading(f2) and marker_hit_fn and marker_hit_fn(f2):
                        save_fn(f2, f"{tag_prefix}_return_home")
                        return True, "home confirmed"
                sleep_fn(interval)
            return False, (f"home not confirmed within {home_to:.0f}s"
                           f"（「乐曲选择→主界面」段是否有确认弹窗未知，已超时）")
        if confirm == "gone":
            end = now_fn() + step_to
            while now_fn() < end:
                f2 = _snap()
                _keep_last(out, f2)               # ★#61/#68★ 最后有效采样帧
                if f2 is None or getattr(f2, "size", 0) == 0:
                    sleep_fn(interval)
                    continue
                if _check_dialog(f2):
                    sleep_fn(interval)
                    continue
                _maybe_re_click(f2, tpls, thrs, rois, sid, re_click, re_state)
                hit2, _sc2, _sc3, _tpl4 = _mv_match_multi(tasker, f2, tpls, thrs, rois)
                if not hit2:
                    return True, "target gone"
                sleep_fn(interval)
            return False, f"{tpl} still visible after {step_to:.0f}s"
        # confirm 为模板名（下一步页面的按钮出现 = 已进入下一步）
        if confirm not in tpl_params:
            _warn_confirm_fallback_once(confirm, lf)   # ★#56 B3★ 无显式 roi → 一次性 WARN
        cthr, croi = tpl_params.get(confirm, (0.75, None))
        end = now_fn() + step_to
        while now_fn() < end:
            f2 = _snap()
            _keep_last(out, f2)                   # ★#61/#68★ 最后有效采样帧
            if f2 is not None and getattr(f2, "size", 0) != 0:
                if _check_dialog(f2):
                    sleep_fn(interval)
                    continue
                _maybe_re_click(f2, tpls, thrs, rois, sid, re_click, re_state)
                hit2, _scd, _sc2 = _mv_match(tasker, f2, confirm, cthr, croi)
                if hit2:
                    return True, f"{confirm} appeared"
            sleep_fn(interval)
        return False, f"{confirm} not appeared within {step_to:.0f}s"

    if start_delay > 0:
        lf(f"{label}：流程启动前稳定等待 {start_delay:.1f}s"
           f"（主界面淡入动画期间点击易被吞，56 实测 19:47/21:14 两轮同型失败）")
        sleep_fn(start_delay)
    started = now_fn()
    lf("=" * 62)
    lf(f"【{label}】启动（登录已成功；流程失败按选项 B 上报 → exit 2）")
    for i, s3 in enumerate(steps, 1):
        lf(f"  步骤{i}: {s3.get('id')} click={s3.get('click')} "
           f"confirm={s3.get('confirm')} tpl={(s3.get('template') or ['-'])[0]}")
    lf(f"  overall={overall:.0f}s step_timeout={step_to:.0f}s retry={retry} "
       f"home_confirm={home_to:.0f}s")
    lf("=" * 62)

    try:
        for idx, st in enumerate(steps, 1):
            sid = str(st.get("id", f"step{idx}"))
            if now_fn() - started >= overall:
                lf(f"{label}整体超时（{overall:.0f}s），中止于 {sid}", "WARN")
                return False, f"overall_timeout@{sid}"
            try:
                if not monitor.alive():
                    lf(f"{label}：游戏进程已退出，中止", "WARN")
                    return False, f"process_gone@{sid}"
            except Exception as exc:  # noqa: BLE001
                lf(f"{label}：进程存活检查异常（按失联中止）: {exc}", "WARN")
                return False, f"process_check_error@{sid}"

            click_kind = str(st.get("click", "box_center"))
            tpl = (st.get("template") or [None])[0]          # 供日志头显示
            tpls = [t for t in (st.get("template") or []) if t]   # ★#60★ 数组任一命中
            thrs = [float(x) for x in (st.get("threshold") or [])] or [0.75]
            rois = list(st.get("roi") or [])
            confirm = str(st.get("confirm", ""))
            coord = st.get("coord")
            post_wait = float(st.get("post_wait_sec", 0.0))
            re_click_cfg = st.get("re_click_during_confirm")   # ★#56 B2★

            attempt = 0
            ok_step, why = False, ""
            while attempt <= retry:
                # ★#49★ 重试期间同样受整体预算约束（截图失败等内层 continue 不绕过 overall）
                if now_fn() - started >= overall:
                    lf(f"{label}整体超时（{overall:.0f}s），中止于 {sid}（重试期间）", "WARN")
                    return False, f"overall_timeout@{sid}"
                attempt += 1
                if attempt > 1:
                    lf(f"{short} 步骤 {sid} 第 {attempt}/{retry + 1} 次尝试", "WARN")
                try:
                    force_foreground(hwnd)
                except Exception as exc:  # noqa: BLE001
                    lf(f"{short} 步骤 {sid} 置前失败（继续）: {exc}", "WARN")
                frame = _snap()
                if frame is None or getattr(frame, "size", 0) == 0:
                    lf(f"{short} 步骤 {sid} 截图无效，稍后重试", "WARN")
                    ok_step, why = False, "screencap invalid"
                    sleep_fn(0.8)
                    continue

                # ★#54★ precheck 前置钩子（配置驱动，如打歌「常に最大已勾选」检查）
                pc = st.get("precheck")
                if pc:
                    pc_ok, pc_why = _precheck(pc, sid, frame)
                    if not pc_ok:
                        lf(f"{short} 步骤 {sid}：precheck 未命中（{pc_why}）→ "
                           f"按配置降级继续（如实记录，不中止流程）", "WARN")
                        save_fn(frame, str(pc.get("miss_tag") or "precheck_miss"))
                    # 无论 precheck 结果如何都继续点击主按钮（未勾选不危险，只是消耗非最大）

                if click_kind == "none":
                    # ★#54★ 纯确认步骤（不点击），如打歌第⑧步主界面二次确认
                    lf(f"{short} 步骤 {sid}：仅确认（不点击）")
                    confirm_out = {"last_frame": None}   # ★#61 P2-1★
                    ok_step, why = _confirm(confirm, tpls, thrs, rois, sid,
                                            re_click_cfg, confirm_out)
                    if ok_step:
                        break
                    # ★#61 P2-1★ 失败留图用最后采样帧（最接近失败时刻）；轮询未执行才回退起始帧
                    _lf = confirm_out.get("last_frame")
                    save_fn(_lf if _lf is not None else frame,
                            f"{tag_prefix}_fail_{sid}")
                    lf(f"{short} 步骤 {sid} 确认失败: {why}（已留图 {tag_prefix}_fail_{sid}）",
                       "WARN")
                    sleep_fn(interval)
                    continue

                if click_kind == "coord":
                    import numpy as np
                    arr = np.asarray(frame)
                    h, w = int(arr.shape[0]), int(arr.shape[1])
                    if abs(w - 1280) > 4 or abs(h - 720) > 4:
                        lf(f"{short} 步骤 {sid} 几何不符 {w}x{h}（应 1280×720±4），"
                           f"跳过坐标点击，绝不按错误坐标乱点", "WARN")
                        ok_step, why = False, f"geometry {w}x{h}"
                        sleep_fn(interval)
                        continue
                    cx, cy = int(coord[0]), int(coord[1])
                else:
                    hit, center, _score, _tpl_hit = _mv_match_multi(
                        tasker, frame, tpls, thrs, rois)
                    if not hit or center is None:
                        lf(f"{short} 步骤 {sid}：{tpl} 未命中（等待/重试）")
                        ok_step, why = False, f"{tpl} not found"
                        sleep_fn(interval)
                        continue
                    cx, cy = center

                try:
                    job = ctrl.post_click(int(cx), int(cy))
                    job.wait()
                except Exception as exc:  # noqa: BLE001
                    lf(f"{short} 步骤 {sid} 点击异常: {exc}", "WARN")
                    ok_step, why = False, f"click error: {exc}"
                    sleep_fn(interval)
                    continue
                lf(f"{short} 步骤 {sid}：点击 ({cx},{cy})（{click_kind}）")

                confirm_out = {"last_frame": None}       # ★#61 P2-1★
                ok_step, why = _confirm(confirm, tpls, thrs, rois, sid,
                                        re_click_cfg, confirm_out)
                if ok_step:
                    if post_wait > 0:
                        lf(f"{short} 步骤 {sid}：确认成功，等待 {post_wait:.1f}s（载入/过渡）")
                        sleep_fn(post_wait)
                    break
                # ★#61 P2-1★ 失败留图用最后采样帧（最接近失败时刻的画面，如確認超时时的
                # 升级变体布局页）；轮询一次都没执行过（极端）才回退 attempt 起始帧。
                # 重试每一轮的失败留图仍逐轮保留（只新增不覆盖不变）。
                _lf = confirm_out.get("last_frame")
                save_fn(_lf if _lf is not None else frame,
                        f"{tag_prefix}_fail_{sid}")
                lf(f"{short} 步骤 {sid} 确认失败: {why}（已留图 {tag_prefix}_fail_{sid}）",
                   "WARN")
                sleep_fn(interval)

            if not ok_step:
                lf(f"{short} 步骤 {sid} 重试耗尽（{retry + 1} 次），{label}失败", "WARN")
                return False, f"{sid}: {why}"
            lf(f"{short} 步骤 {sid} ✔ {why}")

        lf(f"【{label}】全部步骤完成，已回到主界面", "OK")
        return True, "all steps done"
    except Exception as exc:  # noqa: BLE001 - 意外异常：留图+WARN 后向上抛，由 _finish_ok 吞掉
        try:
            frame = _snap()
            if frame is not None and getattr(frame, "size", 0) != 0:
                save_fn(frame, f"{tag_prefix}_fail_exception")
        except Exception:  # noqa: BLE001
            pass
        lf(f"{label}异常（已留图 {tag_prefix}_fail_exception，向上抛出由收口逻辑吞掉）: "
           f"{type(exc).__name__}: {exc}", "WARN")
        raise


def run(cfg: dict, skip_launch: bool = False) -> tuple[bool, str]:
    init_framework_logging()
    # ★#14★ 本进程设为 Per-Monitor DPI aware，保证窗口预调整/读取用物理像素（175% 缩放下必需）。
    log(f"DPI 感知: {init_dpi_awareness()}")
    log(f"MaaFramework 版本: {Library.version()}")
    log(f"开关: flow.click_to_start={cfg['flow']['click_to_start']}  "
        f"controller={cfg['controller']['screencap']}/{cfg['controller']['mouse']}/"
        f"{cfg['controller']['keyboard']}  recognition={cfg['recognition']['method']!r}")
    degraded = build_marker_params(cfg) is None  # ★#22★ 空/缺文件也算降级模式
    log("成功判定模式: " + (
        "【无主界面模板】点击后持续采样 20~30s：只能给出『疑似成功(OK_DEGRADED_UNVERIFIED)』，"
        "不会伪装成确认成功"
        if degraded else "【正向模式 OK_MARKER】主界面模板命中才判确认成功"))

    # 0) 权限自检（实跑前置；--check 不走此路径）
    if not require_elevation(cfg):
        return False, FAIL_NOT_ELEVATED

    # 资源只建一次（自定义识别注册一次，重启兜底复用）
    res = Resource()
    if not res.register_custom_recognition(CUSTOM_RECO_NAME, LoadingScreenReco()):
        log("注册 LoadingScreenReco 失败", "ERROR")
        return False, FAIL_RESOURCE
    # ★#18★ 注册自定义动作 ClickCenter（精确点击识别框中心）
    if not res.register_custom_action("ClickCenter", ClickCenterAction()):
        log("注册 ClickCenter 自定义动作失败", "ERROR")
        return False, FAIL_RESOURCE
    bundle_job = res.post_bundle(str(RESOURCE_DIR))
    bundle_job.wait()
    if not bundle_job.succeeded:
        log(f"加载 Bundle 失败: {RESOURCE_DIR}", "ERROR")
        return False, FAIL_RESOURCE
    log(f"Bundle 已加载: {RESOURCE_DIR}")

    entry, marker_node, success_node = select_task(cfg)
    override = build_pipeline_override(cfg)
    log(f"任务入口={entry} 界面标志节点={marker_node} 成功标志节点={success_node}")
    log(f"pipeline_override={json.dumps(override, ensure_ascii=False)}")

    flow = cfg["flow"]
    overall_deadline = time.monotonic() + float(flow["overall_timeout_sec"])
    interval = float(flow["process_check_interval_sec"])             # 2~3s 轮询
    attempt_cap = (flow["title_wait_timeout_ms"] + flow["post_click_timeout_ms"]) / 1000.0 + 20
    max_restarts = int(flow["max_restarts"])                         # 2
    max_starts = max_restarts + 1                                    # 合计最多 3 次启动
    rate_window = float(flow["start_rate_window_sec"])               # 600s（5~10 分钟）
    max_starts_per_window = int(flow["max_starts_per_window"])       # 3
    restart_wait = float(flow["restart_wait_sec"])                   # 10~15s
    stable_alive = float(flow["stable_alive_sec"])                   # 60s 启动稳定性口径

    start_times: list[float] = []  # 每次 launch 的时间（限流用）
    start_count = 0
    restarts = 0
    session = None
    last_reason = "首次运行"
    # ★#18★ 受控点击（P1-8）：单次 run() 内最多 max_clicks(默认2) 次；达上限后禁用点击，不无限重试。
    click_count = 0
    max_clicks = int(flow.get("max_clicks", 2))
    total_attempt_limit = max_starts * 2 + 1  # 含复用会话重跑，硬上限防无限循环

    log(f"兜底参数(06-diagnosis.md §5-4): 轮询={interval}s 启动确认超时="
        f"{cfg['launch']['window_poll_timeout_sec']}s 连续存活≥{stable_alive:.0f}s判本次可用(仅观测口径,非登录前置)"
        f" 退出后等{restart_wait:.0f}s再重启 最多重启{max_restarts}次(合计{max_starts}次启动) "
        f"{int(rate_window)}s内≤{max_starts_per_window}次启动 第3次同窗退出判确定性失败并停止上报")

    for attempt in range(1, total_attempt_limit + 1):
        if time.monotonic() > overall_deadline:
            log("整体超时（overall_timeout_sec）已到，放弃", "ERROR")
            return False, FAIL_OVERALL_TIMEOUT

        # 无会话 → 需要（重新）启动一次
        if session is None:
            now = time.monotonic()
            recent = [t for t in start_times if now - t < rate_window]
            if len(recent) >= max_starts_per_window:
                log(f"限流：最近 {int(rate_window)}s 内已启动 {len(recent)} 次，达到上限 "
                    f"{max_starts_per_window}（避免触发 GameGuard 风控）；停止重试并上报确定性失败。", "ERROR")
                return False, FAIL_DETERMINISTIC
            if start_count >= max_starts:
                log(f"启动次数已达上限 {max_starts}（最多重启 {max_restarts} 次）；"
                    f"停止重试并上报确定性失败。", "ERROR")
                return False, FAIL_DETERMINISTIC
            if not skip_launch:
                try:
                    log(f"[{time.strftime('%H:%M:%S')}] 第 {start_count + 1}/{max_starts} 次启动: {launch_game(cfg)}")
                except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
                    log(f"启动失败: {exc}", "ERROR")
                    return False, FAIL_LAUNCH
                start_times.append(now)
                start_count += 1
            else:
                log("--skip-launch：跳过启动，直接查找已存在的窗口")
            try:
                win = wait_for_window(cfg, overall_deadline)
            except TimeoutError as exc:
                log(str(exc), "ERROR")
                return False, FAIL_WINDOW_NOT_FOUND
            session = build_session(cfg, res, win)
            if session is None:
                last_reason = "连接/初始化失败"
                log(f"[{time.strftime('%H:%M:%S')}] 第 {attempt} 次会话建立失败({last_reason})，计入重启兜底", "ERROR")
                restarts += 1
                continue

        ctrl, tasker, sink, monitor, hwnd = session
        # ★#23★ 主界面正向判定函数提前构造（原来在门控之后才建，重连后根本到不了那里）。
        # 供本轮「直判」与 post_click_verify 三态判定共用，避免重复构造。
        marker_hit_fn = make_marker_hit_fn(tasker, cfg)  # 内部 build_marker_params 已做空/缺文件硬守护

        def _finish_ok(when: str) -> tuple[bool, str]:
            """★#39★ 登录已确认成功后的统一收口：默认关闭时逐字零回归。

            本闭包与 run() 共享同一会话对象（ctrl / tasker / hwnd / marker_hit_fn /
            monitor / cfg）；采集模式「只读旁观」，不使用 marker_hit_fn 改判定、
            不抢窗口焦点、不点击、不发任务、不做识别。
            - mv_explore=false（默认）：直接 return True, OK_MARKER —— 不截图、不留图、
              不写任何日志、不改变时序，与 #33 基线行为逐字一致。
            - mv_explore=true：旁路跑 mv_explore_loop()，结束后仍 return True, OK_MARKER
              / 退出码 0（登录本就成功，采集是旁路，不新增结果码、不动三态、不改文案）。
              ★#49★ 采集是「用户手动走」模式，与脚本自动点击互斥：此时跳过 MV 自动流程并 WARN。
            - ★#49★ mv_enabled=true（且未开采集）：跑 run_mv_flow() 自动观看；业务失败
              → return False, FAIL_MV_FLOW（#34 §5.3 选项 B → report_result else → exit 2）；
              意外异常一律吞掉+WARN，仍 return True, OK_MARKER（不把登录成功降级）。
            """
            global _LAST_MV_STAT          # ★#48★ 采集统计透传给结果文件
            # ★#49/#54★ 三开关都关（默认）→ 直接返回，不截图/不留图/不写日志/不改变时序（逐字零回归）
            if (not mv_explore_enabled(cfg) and not mv_flow_enabled(cfg)
                    and not play_flow_enabled(cfg)):
                return True, OK_MARKER
            if not mv_explore_enabled(cfg):
                # ★#49/#54★ 自动流程分支（登录已成功；业务失败按选项 B 上报 → exit 2）
                # ★#54★ mv_enabled 与 play_enabled 同时开启 → 互斥，MV 优先（写死，见 README §16.3）
                if mv_flow_enabled(cfg):
                    if play_flow_enabled(cfg):
                        log("mv_enabled 与 play_enabled 同时开启：互斥，本次执行 MV 流程、"
                            "跳过打歌流程（如需打歌请单独开启）", "WARN")
                    try:
                        ok_mv, why_mv = run_mv_flow(
                            cfg, ctrl=ctrl, tasker=tasker, hwnd=hwnd,
                            marker_hit_fn=marker_hit_fn, monitor=monitor)
                    except Exception as exc:  # noqa: BLE001 - 意外异常吞掉，不把登录成功降级
                        log(f"MV 流程异常（已忽略，登录结果不变）: {type(exc).__name__}: {exc}",
                            "WARN")
                        return True, OK_MARKER
                    if ok_mv:
                        log("MV 自动观看流程完成（登录结果不变）", "OK")
                        return True, OK_MARKER
                    log(f"MV 自动观看流程失败: {why_mv}（登录已成功；按 #34 §5.3 选项 B 上报 "
                        f"FAIL_MV_FLOW → exit 2）", "WARN")
                    return False, FAIL_MV_FLOW
                if play_flow_enabled(cfg):
                    try:
                        ok_pl, why_pl = run_play_flow(
                            cfg, ctrl=ctrl, tasker=tasker, hwnd=hwnd,
                            marker_hit_fn=marker_hit_fn, monitor=monitor)
                    except Exception as exc:  # noqa: BLE001 - 意外异常吞掉，不把登录成功降级
                        log(f"打歌流程异常（已忽略，登录结果不变）: {type(exc).__name__}: {exc}",
                            "WARN")
                        return True, OK_MARKER
                    if ok_pl:
                        log("自動打歌（跳过模式）流程完成（登录结果不变）", "OK")
                        return True, OK_MARKER
                    log(f"自動打歌（跳过模式）流程失败: {why_pl}（登录已成功；按 #34 §5.3 选项 B "
                        f"上报 FAIL_PLAY_FLOW → exit 2）", "WARN")
                    return False, FAIL_PLAY_FLOW
                return True, OK_MARKER
            try:
                log(f"MV 探索采集：登录已确认（入口[{when}]），开始旁路只读采集"
                    f"（登录结果不变）")
                stat = mv_explore_loop(
                    cfg,
                    screencap_fn=lambda: ctrl.post_screencap().wait().get(),
                    is_alive_fn=monitor.alive,
                    save_fn=lambda f, tag: save_probe_frame(f, tag, log_fn=log),
                    log_fn=log,
                )
                _LAST_MV_STAT = stat       # ★#48★ 供 write_result_files 读取采集统计
                # ★#46 P2-1★ 采集自身异常/空采集必须显式 WARN，避免「看起来采到了其实没有」
                stop = (stat or {}).get("stop_reason", "unknown")
                if not (stat or {}).get("enabled", False) or stop == "error":
                    log(f"★MV 探索采集未正常执行（stop_reason={stop}），素材未采到，需重跑★"
                        f"（登录结果不变）", "WARN")
                elif (stat or {}).get("saved", 0) == 0:
                    log("★MV 探索采集本次 0 张留图，素材未采到，需重跑★（登录结果不变）", "WARN")
            except KeyboardInterrupt:
                log("MV 探索采集被 Ctrl+C 中断（登录结果不变）", "WARN")
            except Exception as exc:  # noqa: BLE001 - 附加采集异常绝不影响「登录已成功」
                import traceback
                log(f"★MV 探索采集未正常执行（异常），素材未采到，需重跑★: "
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                    f"（登录结果不变）", "WARN")
            if mv_flow_enabled(cfg) or play_flow_enabled(cfg):
                log("mv_enabled/play_enabled 与 mv_explore 同时开启：采集（用户手动）优先，"
                    "本次跳过自动流程", "WARN")
            return True, OK_MARKER

        def _direct_confirm(when: str) -> bool:
            """对当前帧直判一次主界面，绕开 sink.is_success 的 TitleScreenDetectedClick 门槛。

            ★#25★ 不再设「点击前/未重连不直判」守护：二次运行 / --skip-launch / 残留窗口
            使游戏已在主界面时（click_count=0 且 restarts=0），主界面无按钮可点、正常点击
            流程救不回来，会一路空转到 FAIL_MAX_RESTARTS。复核已用真实判定器遍历 21 张
            负样本（标题/污染/加载/纯色）证明模板本身零误命中，直判在每轮都执行是安全的。
            """
            try:
                frame = ctrl.post_screencap().wait().get()
            except Exception as exc:  # noqa: BLE001 - 截图失败不影响后续流程
                log(f"主界面直判[{when}]截图失败（忽略）: {exc}", "WARN")
                return False
            if direct_marker_confirm(frame, cfg, marker_hit_fn, log_fn=log,
                                     probe_fn=lambda f, tag: save_probe_frame(f, tag, log_fn=log)):
                log(f"成功【{OK_MARKER}】：主界面模板直判命中[{when}]（非加载态），已确认进入游戏", "OK")
                return True
            return False

        # ① 每轮 pipeline 之前（含 SESSION_LOST / 进程退出重连后的新一轮开头）：直达主界面立刻确认
        if _direct_confirm("pipeline 前"):
            return _finish_ok("pipeline 前直判")

        # ★#18★ 每次跑 pipeline 前强制置前（上次会话 SetForegroundWindow 一直被拒）
        try:
            force_foreground(hwnd)
        except Exception as exc:  # noqa: BLE001
            log(f"置前异常（继续）: {exc}", "WARN")
        attempt_deadline = min(overall_deadline, time.monotonic() + attempt_cap)
        log(f"第 {attempt} 次运行 pipeline（上次原因: {last_reason}）...")
        sink.reset()
        sink.set_targets(marker_node, success_node)
        # ★#18★ 达到最大点击次数后禁用点击（仅在重跑时生效，不无限点击）
        attempt_override = dict(override)
        geometry_ok = True
        if click_count < max_clicks:
            # ★#18 几何守卫★ 只有截图目标尺寸收敛到模板校准几何(1280×720)才允许点；
            # 未收敛则本轮禁用点击，避免把 1260 宽帧的识别框按 1280 坐标系点击出去。
            geometry_ok, geo = wait_target_geometry(
                ctrl, cfg,
                timeout_sec=float(cfg["controller"].get("geometry_wait_sec", 15.0)),
            )
        if click_count >= max_clicks:
            attempt_override["TitleScreenDetectedClick"] = {
                **build_detection_params(cfg), "action": "DoNothing",
            }
            log(f"  #18: 已达最大点击次数 {max_clicks}，本次重跑禁用点击动作")
        elif not geometry_ok:
            attempt_override["TitleScreenDetectedClick"] = {
                **build_detection_params(cfg), "action": "DoNothing",
            }
            log("  #18: 几何未收敛（截图目标 ≠ 1280×720），本轮禁用点击动作")
        job = tasker.post_task(entry, attempt_override)
        status = monitor_job(job, tasker, monitor, attempt_deadline, interval)
        if sink.clicked:
            click_count += 1
            log(f"  #18: 本次点击发生，累计 {click_count}/{max_clicks}")

        if status == FAIL_OVERALL_TIMEOUT:
            log("运行期触发整体超时，已 post_stop()", "ERROR")
            return False, FAIL_OVERALL_TIMEOUT

        if status == FAIL_PROCESS_DIED:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            dur = monitor.alive_duration()
            stability = ("启动未稳定(<60s，短动作窗口不足)" if dur < stable_alive
                         else "本次曾稳定存活≥60s(短动作窗口充足)，退出属数分钟级静默自退")
            last_reason = f"游戏进程中途退出@{ts} 连续存活={dur:.0f}s {stability}"
            log(f"[{ts}] 检测到目标进程退出（非模板识别失败）。{last_reason}；"
                f"保留日志与截图(debug/maafw.log、debug/on_error/、debug/screencap/)。", "ERROR")
            try:
                ctrl.post_screencap().wait()  # best effort 留证
            except Exception:
                pass
            session = None  # 丢弃旧会话，下轮重新 launch + 发现窗口 + 连接
            restarts += 1
            # ★第 3 次仍在相同时间窗退出 → 确定性失败，停止重试并上报，不无限循环★
            if start_count >= max_starts:
                log(f"第 {start_count} 次(最后一次)启动仍在相同时间窗退出 → "
                    f"判定确定性失败，停止重试并上报。", "ERROR")
                return False, FAIL_DETERMINISTIC
            # 退出后等 10~15s 再重启（等 conhost/UnityCrashHandler64 子进程退出、句柄释放）
            log(f"等待 {restart_wait:.0f}s 后重启（等子进程退出/句柄释放）...", "WARN")
            wait_end = min(overall_deadline, time.monotonic() + restart_wait)
            while time.monotonic() < wait_end:
                time.sleep(min(0.5, max(0.0, wait_end - time.monotonic())))
            continue

        job.wait()
        try:
            detail = job.get()
            log(f"任务详情: {str(detail)[:300]}")
        except Exception as exc:  # noqa: BLE001 - 取详情失败不影响成功判定
            log(f"取任务详情失败（不影响判定）: {exc}", "WARN")

        # ★#23 P1-2★ ② 门控之前再直判一次：覆盖「pipeline 跑完但 sink.is_success 门槛未满足」
        # 的主界面帧。重连后直达主界面时 TitleScreenDetectedClick 不命中、is_success=False，
        # 若没有这里，post_click_verify 永不调用、OK_MARKER 永久不可达（老毛病「进了游戏却判失败」）。
        if _direct_confirm("门控前"):
            return _finish_ok("门控前直判")

        # 前置：pipeline 走到成功标志节点 + 进程存活（★#23★ 不再是 OK_MARKER 的唯一入口）
        if not (sink.is_success(tasker) and monitor.alive()):
            last_reason = "界面标志未命中（进程仍存活，不重启游戏，仅复用会话重跑）"
            log(f"第 {attempt} 次未达成功标志（marker={marker_node} 命中="
                f"{marker_node in sink.succeeded_recognitions}）；"
                f"识别事件: {sink.events[-12:]}", "WARN")
            # ★#21★ 失败路径也留一张判定时刻原图（pipeline 没走到 InGameSuccess 时同样要取证）
            try:
                save_probe_frame(ctrl.post_screencap().wait().get(), "fail_no_flag", log_fn=log)
            except Exception as exc:  # noqa: BLE001
                log(f"失败路径留图异常（忽略）: {exc}", "WARN")
            if click_count >= max_clicks:
                return False, FAIL_NOT_CONFIRMED
            continue

        # ★#21★ 判定时刻先留一张原图（无论后续结论如何，保证下次能拿到素材）
        try:
            save_probe_frame(ctrl.post_screencap().wait().get(), "verdict", log_fn=log)
        except Exception as exc:  # noqa: BLE001
            log(f"判定时刻留图异常（忽略）: {exc}", "WARN")

        # ★#21★ 三态判定：点击后持续采样 20~30s，绝不因单帧「按钮消失」就下结论。
        # marker_hit_fn 已在每轮开头提前构造（#23，供直判与此三态判定共用）。
        verdict, why = post_click_verify(
            cfg,
            screencap_fn=lambda: ctrl.post_screencap().wait().get(),
            is_alive_fn=monitor.alive,
            button_visible_fn=make_button_visible_fn(tasker, cfg),
            marker_hit_fn=marker_hit_fn,
            probe_fn=lambda f, tag: save_probe_frame(f, tag, log_fn=log),
            log_fn=log,
            click_fn=lambda x, y: ctrl.post_click(x, y).wait(),
            loading_seen_before=bool(sink.loading_seen_after_click),
        )
        log(f"点击后判定结论: {verdict} | {why}")

        if verdict == "MARKER":
            log(f"成功【{OK_MARKER}】：{why}（正向主界面模板命中，已确认进入游戏）", "OK")
            return _finish_ok("post_click_verify MARKER")

        if verdict == "LEFT_TITLE":
            # 疑似成功：按钮连续 20s 不见 + 非加载态 + 进程存活，但**没有**正向标志 →
            # 不得当作成功返回（run 返回 False + 专用结果码，main() 用退出码 3 显式区分）。
            log(f"疑似成功（未经正向标志确认）【{OK_DEGRADED_UNVERIFIED}】：{why}。"
                f"★仅是推测：无主界面模板，无法确认真的进入了游戏；请核对取证留图 "
                f"debug/screencap/in_game_probe_*_unverified.png★", "WARN")
            return False, OK_DEGRADED_UNVERIFIED

        if verdict == "SESSION_LOST":
            last_reason = f"点击后窗口/进程消失（疑似客户端重启）: {why}"
            log(f"[{time.strftime('%H:%M:%S')}] {last_reason} → 走重启兜底（不计入判定失败）", "WARN")
            session = None
            restarts += 1
            if start_count >= max_starts:
                return False, FAIL_PROCESS_DIED
            continue

        # STILL_TITLE / TIMEOUT：仍停在标题或无法确认
        last_reason = f"点击后 {verdict}: {why}"
        log(f"[{time.strftime('%H:%M:%S')}] {last_reason}；click_count={click_count}/{max_clicks}", "ERROR")
        if click_count >= max_clicks:
            return False, FAIL_NOT_CONFIRMED
        continue

    log(f"达到最大尝试上限，仍失败（最后原因: {last_reason}）。请查看 debug/on_error/ 留图。", "ERROR")
    return False, FAIL_MAX_RESTARTS


# --------------------------------------------------------------------------- #
# 静态自测（--check）：绝不启动游戏、不点击真实窗口、不创建 Win32 控制器
# --------------------------------------------------------------------------- #
def _fake_win(name, cls, hwnd, visible=True, width=800, height=600):
    return type("FakeWin", (), {
        "hwnd": hwnd, "class_name": cls, "window_name": name,
        "visible": visible, "width": width, "height": height,
    })()


def _validate_pipeline_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_frame_path(name: str):
    """把帧名解析成绝对路径：支持 artifacts 素材名与 debug 下的相对路径（vision/xx.jpg）。"""
    p = Path(name)
    if p.is_absolute():
        return p
    for base in (ARTIFACTS_DIR, DEBUG_DIR):
        cand = base / name
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"找不到帧素材: {name}（已查 {ARTIFACTS_DIR} 与 {DEBUG_DIR}）")


def _load_real_frame_bgr(name: str):
    """读一张真实帧（PNG/JPG），按 720p 短边缩放，转 BGR 连续数组（与运行时截图同尺度）。

    ★#18★ 支持直接读 debug/vision 下的真实运行帧（JPG），校准与 --check 才能用上实跑帧。
    """
    import numpy as np
    from make_template import decode_image, area_resize_short_side  # tools/
    rgb = decode_image(_resolve_frame_path(name))[:, :, :3]
    scaled = area_resize_short_side(rgb)
    return np.ascontiguousarray(scaled[:, :, ::-1])  # BGR


def _make_static_tasker(frame_bgr, cfg: dict):
    """构造离线 Tasker：CustomController(喂一张静态帧) + 真实 resource bundle + 注册自定义识别。

    返回 (tasker, ctrl, cleanup_fn)。真实 bundle 保证用的是与实跑完全相同的 4 模板/roi/threshold，
    不再用临时假 bundle、也不再用写死 lambda 自证识别能力。
    """
    import shutil
    import tempfile
    import numpy as np
    from maa.controller import CustomController

    class StaticController(CustomController):
        def __init__(self, img):
            self.img = img
            self.clicks = []
            super().__init__()

        def connect(self): return True
        def request_uuid(self): return "offline-static-720p"
        def get_features(self): return 0
        def start_app(self, intent): return True
        def stop_app(self, intent): return True
        def screencap(self): return self.img
        def click(self, x, y):
            self.clicks.append((int(x), int(y)))
            return True
        def swipe(self, *a): return True
        def touch_down(self, *a): return True
        def touch_move(self, *a): return True
        def touch_up(self, *a): return True
        def click_key(self, *a): return True
        def input_text(self, *a): return True
        def key_down(self, *a): return True
        def key_up(self, *a): return True

    tmp = Path(tempfile.mkdtemp(prefix="maa_offline_"))
    Toolkit.init_option(str(tmp), {"logging": False, "stdout_level": 0})
    ctrl = StaticController(frame_bgr)
    ctrl.set_screenshot_use_raw_size(True)  # 避免二次缩放，坐标与裁剪一致
    cj = ctrl.post_connection(); cj.wait()
    res = Resource()
    res.register_custom_recognition(CUSTOM_RECO_NAME, LoadingScreenReco())
    rj = res.post_bundle(str(RESOURCE_DIR)); rj.wait()  # ★真实 bundle★
    if not rj.succeeded:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("真实 resource bundle post_bundle 失败（检查 model/ocr 目录）")
    tasker = Tasker()
    tasker.bind(res, ctrl)
    if not tasker.inited:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("离线 Tasker 初始化失败")
    cleanup = lambda: shutil.rmtree(tmp, ignore_errors=True)
    return tasker, ctrl, cleanup


def offline_template_match_check(cfg: dict) -> tuple[bool, str]:
    """★自匹配管道自检★ 喂 1280×720 真实判定帧（模板 v8 的裁剪母图）跑最小 pipeline。

    ⚠️ 这是自匹配：NCC=1.0 必然命中、box 必然是裁剪框，只证明真实 bundle 能加载 + 识别/点击链路
    能跑通，**完全不证明真实帧识别能力**。真实帧识别能力由 real_frame_button_visibility_check()
    用真实 make_button_visible_fn + 多张独立真实帧覆盖（--check [16]）。
    ★#18★ 母图从旧的 860 宽 artifacts 帧改为 1280 宽真实 screencap：模板几何 1280×720，
    喂 860 帧时按钮坐标不同、crop 必然落空（旧版本正是因此假失败）。
    """
    import numpy as np

    SRC = "screencap/2026.11.07-02.11.27.757_InGameSuccess.png"
    try:
        bgr = _load_real_frame_bgr(SRC)
    except FileNotFoundError:
        return False, f"缺少素材: {SRC}"
    tcfg = cfg["recognition"]["template"]
    crop = tcfg["crop"]  # [468,576,793,599]

    try:
        tasker, ctrl, cleanup = _make_static_tasker(bgr, cfg)
    except Exception as exc:
        return False, f"离线 Tasker 构造失败: {exc}"

    try:
        # 最小 pipeline（override 注入）：用与实跑同一套 8 模板数组 + roi + threshold 识别按钮并点击
        ov = {
            "OfflineEntry": {"action": "DoNothing", "next": ["OfflineFind"],
                             "timeout": 5000, "rate_limit": 10},
            "OfflineFind": {**build_detection_params(cfg), "action": "Click",
                            "target": True, "next": ["OfflineDone"]},
            "OfflineDone": {"action": "StopTask"},
        }
        job = tasker.post_task("OfflineEntry", ov).wait()

        coord = ctrl.clicks[0] if ctrl.clicks else None
        x0, y0, x1, y1 = crop
        in_box = bool(coord) and (x0 <= coord[0] <= x1 and y0 <= coord[1] <= y1)

        # 加载态合成帧不误命中（同一套识别参数）
        frame_checks = []
        for name, val in [("白屏", 250), ("黑屏", 5), ("灰屏纯色", 128)]:
            lf = np.full_like(bgr, val)
            p = build_detection_params(cfg)
            rj = tasker.post_recognition(
                JRecognitionType.TemplateMatch,
                JTemplateMatch(template=list(p["template"]),
                               threshold=[p["threshold"]], method=p["method"],
                               roi=tuple(p["roi"]) if p.get("roi") else (0, 0, 0, 0)),
                lf,
            )
            rj.wait()
            rd = rj.get()
            nid = rd.node_id_list[0] if (rd and getattr(rd, "node_id_list", None)) else None
            nd = tasker.get_node_detail(nid) if nid else None
            hit = bool(getattr(getattr(nd, "recognition", None), "hit", False))
            frame_checks.append(f"{name}:hit={hit}")
        loading_no_hit = all("hit=False" in x for x in frame_checks)

        ok = bool(job.succeeded and ctrl.clicks and in_box and loading_no_hit)
        detail = (f"真实 bundle 加载 + 识别/点击链路跑通; 点击坐标={coord} 落在按钮框 {crop} 内={in_box} "
                  f"(★自匹配必然值，不作真实帧识别能力证据★); task_succeeded={job.succeeded}; "
                  f"| 加载态合成帧不误命中[{' '.join(frame_checks)}]={'OK' if loading_no_hit else 'FAIL'}")
        Tasker.set_log_dir(str(DEBUG_DIR))
        return ok, detail
    finally:
        cleanup()


def real_frame_button_visibility_check(cfg: dict) -> tuple[bool, str]:
    """★#18 真实帧断言★ 用真实的 make_button_visible_fn（非写死 lambda）在真实 1280×720 帧上判定。

    - 「按钮可见」真实帧(1280×720，含一张白到发光的标题帧) → 期望 True，并输出 NCC/命中 box
    - 「加载/过渡」真实帧(黑屏 / 白色警告页 / 白色公告页) + 纯白/黑/灰 → 期望 False
    实测(NCC 8 模板取最大)：正样本 leave-one-out min=0.676，负样本 max=0.282，阈值 0.5 两侧有余量。
    ★该函数只证明『按钮能被定位』，不证明『按钮不可见=进入游戏』★——后者由 run() 的
    『点击后观察到加载态』守卫负责，禁止用本函数反推成功。
    """
    import numpy as np
    visible = cfg["recognition"]["calibration"]["source_frames_visible"]
    loading = cfg["recognition"]["calibration"]["source_frames_loading"]

    first = _load_real_frame_bgr(visible[0])
    try:
        tasker, ctrl, cleanup = _make_static_tasker(first, cfg)
    except Exception as exc:
        return False, f"离线 Tasker 构造失败: {exc}"
    try:
        p = build_detection_params(cfg)
        reco_param = JTemplateMatch(template=list(p["template"]),
                                    threshold=[p["threshold"]], method=p["method"],
                                    roi=tuple(p["roi"]) if p.get("roi") else (0, 0, 0, 0))
        visible_fn = make_button_visible_fn(tasker, cfg)  # ★真实函数★

        def probe(frame):
            j = tasker.post_recognition(JRecognitionType.TemplateMatch, reco_param,
                                        np.ascontiguousarray(frame)); j.wait()
            d = j.get()
            rd = tasker.get_node_detail(d.node_id_list[0]).recognition
            allr = getattr(rd, "all_results", []) or []
            ncc = max([r.score for r in allr], default=None)
            return bool(rd.hit), ncc, getattr(rd, "box", None)

        lines = []
        ok = True
        v_hit = 0
        for n in visible:
            hit, ncc, box = probe(_load_real_frame_bgr(n))
            vis = visible_fn(_load_real_frame_bgr(n))  # 真实 make_button_visible_fn
            v_hit += int(vis)
            lines.append(f"{n}:button_visible={vis},NCC={None if ncc is None else round(ncc,4)},box={box}")
            if not vis:
                ok = False
        l_hit = 0
        for n in loading:
            vis = visible_fn(_load_real_frame_bgr(n))
            l_hit += int(vis)
            lines.append(f"{n}:button_visible={vis}")
            if vis:
                ok = False
        for name, val in [("纯白", 250), ("纯黑", 5), ("纯灰", 128)]:
            vis = visible_fn(np.full_like(first, val))
            l_hit += int(vis)
            lines.append(f"{name}:button_visible={vis}")
            if vis:
                ok = False
        Tasker.set_log_dir(str(DEBUG_DIR))
        detail = (f"可见帧 button_visible=True {v_hit}/{len(visible)}; "
                  f"加载态+纯色 button_visible=False {len(loading) + 3 - l_hit}/{len(loading) + 3} "
                  f"| " + " ; ".join(lines))
        return ok, detail
    finally:
        cleanup()


def static_self_check(cfg: dict) -> bool:
    print("=" * 70)
    print("静态自测 --check（不启动游戏 / 不点击真实窗口 / 不创建 Win32 控制器）")
    print("=" * 70)
    failures: list[str] = []

    # 1) import + 版本
    try:
        print(f"[1] Library.version() = {Library.version()}")
        from maa import controller, toolkit, resource, tasker  # noqa: F401
        print("    maa.* 关键模块 import OK")
    except Exception as exc:
        failures.append(f"import 失败: {exc}")

    # 2) inspect.signature 复核 Win32Controller：第一参数必须是 hWnd（故用位置参数）
    try:
        sig = inspect.signature(Win32Controller.__init__)
        params = list(sig.parameters)
        first = params[1] if len(params) > 1 else None
        print(f"[2] Win32Controller.__init__{sig}")
        print(f"    第一个窗口参数名 = {first!r}（binding 为 hWnd，与 demo 的 hwnd= 冲突 → 用位置参数）")
        if first != "hWnd":
            failures.append(f"第一个窗口参数名不是 hWnd，而是 {first!r}")
    except Exception as exc:
        failures.append(f"inspect.signature 失败: {exc}")

    # 3) 控制器枚举解析（默认 Foreground + Seize）+ 输入方式一行可切
    try:
        s, m, k = resolve_controller_methods(cfg)
        print(f"[3] controller 枚举解析 OK: screencap={s}({cfg['controller']['screencap']}) "
              f"mouse={m}({cfg['controller']['mouse']}) keyboard={k}({cfg['controller']['keyboard']})")
        # ★#18★ 输入方式备选必须都能解析（Seize 需前台；PostMessageWithCursorPos/PostMessage 不需前台）
        alts = ["Seize", "PostMessageWithCursorPos", "PostMessage"]
        resolved = {n: int(getattr(MaaWin32InputMethodEnum, n)) for n in alts}
        assert resolved == {"Seize": 1, "PostMessageWithCursorPos": 64, "PostMessage": 4}, resolved
        print(f"    输入方式一行可切（改 settings.controller.mouse/keyboard）: {resolved}；"
              f"当前={cfg['controller']['mouse']}（Seize 需前台；若置前后仍点不动由 #5 切 64/4）")
    except Exception as exc:
        failures.append(f"枚举解析失败: {exc}")

    # 4) pipeline JSON 语法 + 节点引用完整性
    try:
        for p in sorted(PIPELINE_DIR.glob("*.json")):
            data = _validate_pipeline_json(p)
            print(f"[4] pipeline JSON 语法 OK: {p.name}（{len(data)} 个节点）")
        _validate_pipeline_json(RESOURCE_DIR / "default_pipeline.json")
        print("    default_pipeline.json 语法 OK")
        merged: dict = {}
        for p in PIPELINE_DIR.glob("*.json"):
            merged.update(_validate_pipeline_json(p))
        known = set(merged)
        missing: list[str] = []
        for node, body in merged.items():
            refs: list = []
            for key in ("next", "on_error"):
                v = body.get(key, [])
                refs += v if isinstance(v, list) else [v]
            for r in refs:
                if not isinstance(r, str):
                    continue
                clean = r.split("]", 1)[1] if r.startswith("[") else r
                if clean not in known:
                    missing.append(f"{node} -> {r}")
        if missing:
            failures.append(f"pipeline 悬空引用: {missing}")
        else:
            print(f"    next / on_error 引用完整性 OK（{len(known)} 节点，含 [JumpBack] 前缀剥离）")
    except Exception as exc:
        failures.append(f"pipeline JSON 校验失败: {exc}")

    # 5) 资源目录结构 + 8 模板 PNG（真实 1280×720 帧裁的 325×23 纯文字区）+ OCR 目录约束
    try:
        for d in [RESOURCE_DIR / "pipeline", IMAGE_DIR, BASE_DIR / "config", DEBUG_DIR]:
            assert d.is_dir(), f"缺少目录 {d}"
        tcfg = cfg["recognition"]["template"]
        tpls = tcfg["files"]
        assert len(tpls) >= 2, "P0-0 需要多模板数组（跨立绘轮播稳健）"
        c0, cy0, c1, cy1 = tcfg["crop"]
        want = (c1 - c0, cy1 - cy0)
        assert tcfg["roi"][0] <= c0 and tcfg["roi"][1] <= cy0 and \
            tcfg["roi"][0] + tcfg["roi"][2] >= c1 and tcfg["roi"][1] + tcfg["roi"][3] >= cy1, \
            f"crop {tcfg['crop']} 必须完整落在 roi {tcfg['roi']} 内（否则永远定位不到）"
        dims = []
        for fn in tpls:
            tpl = IMAGE_DIR / fn
            assert tpl.is_file(), f"缺少模板 {tpl}"
            with tpl.open("rb") as f:
                head = f.read(33)
            assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{fn} 不是合法 PNG"
            dims.append(struct.unpack(">II", head[16:24]))
        assert all(d == want for d in dims), f"模板尺寸应与 crop 一致 {want}，实际 {dims}"
        print(f"[5] 资源目录结构 OK；{len(tpls)} 张模板均 {want[0]}x{want[1]} PNG "
              f"（1280x720 真实帧 crop={tcfg['crop']} 纯文字区，且落在 roi 内）")
        # ★OCR 目录约束★：缺三件套时不得存在 model/ocr（否则 post_bundle 整体失败）
        ocr_dir = RESOURCE_DIR / "model" / "ocr"
        has_models = all((ocr_dir / n).exists() for n in ["det.onnx", "rec.onnx", "keys.txt"])
        if ocr_dir.is_dir() and not has_models:
            failures.append("model/ocr 目录存在但缺三件套 → post_bundle 会失败（应移除该空目录）")
        else:
            print(f"    OCR 目录约束 OK: model/ocr {'缺（默认 template，正确）' if not ocr_dir.is_dir() else '存在且三件套齐全'}")
        # P1-7: README.md 真实存在（reviewer 报悬空引用）
        assert (BASE_DIR / "README.md").is_file(), "agent/README.md 缺失（P1-7 悬空引用）"
        print("    P1-7: agent/README.md 真实存在 OK")
    except Exception as exc:
        failures.append(f"资源结构校验失败: {exc}")

    # 6) 窗口过滤单元检查（含排除 IME 隐藏窗 / 工具窗 / 可见非 0×0）
    try:
        wcfg = cfg["window"]
        wins = [
            _fake_win("iM@S SCSP Tools By chinosk", "ConsoleWindowClass", 11),
            _fake_win("WorkBuddy - 某文档", "Chrome_WidgetWin_1", 22),
            _fake_win("MSCTFIME UI", "MSCTFIME UI", 33, visible=False, width=0, height=0),
            _fake_win("Default IME", "Default IME", 44, visible=False, width=0, height=0),
            _fake_win("imasscprism", "UnityWndClass", 55),
            _fake_win("一个无关窗口", "", 66),
        ]
        usable = lambda h: {
            11: True, 22: True, 33: False, 44: False, 55: True, 66: True
        }.get(h, True)
        got = pick_window(wins, wcfg, usable=usable)
        assert got is not None and got.window_name == "imasscprism", "未正确选中目标窗口"
        assert pick_window([wins[0]], wcfg, usable=usable) is None, "mod 工具窗未被排除"
        assert pick_window([wins[1]], wcfg, usable=usable) is None, "WorkBuddy 窗口未被排除"
        assert pick_window([wins[2]], wcfg, usable=usable) is None, "MSCTFIME UI 未被排除"
        assert pick_window([wins[3]], wcfg, usable=usable) is None, "Default IME 未被排除"
        assert pick_window([wins[5]], wcfg, usable=usable) is None, "无关窗口被误选"
        # 目标窗口本身若 0×0/不可见也应被排除
        hidden_target = _fake_win("imasscprism", "UnityWndClass", 77, visible=False, width=0, height=0)
        assert pick_window([hidden_target], wcfg, usable=lambda h: False) is None, "隐藏目标窗未被排除"
        print(f"[6] 窗口过滤单元检查 OK: 命中 hwnd={_hwnd_int(got.hwnd)} class={got.class_name!r}；"
              f"工具窗/IME/WorkBuddy/隐藏窗均已排除")
        wcfg2 = dict(wcfg); wcfg2["class_name_fallback"] = "UnityWndClass"
        assert pick_window([_fake_win("别的名字", "UnityWndClass", 88)], wcfg2, usable=usable) is not None
        print("    class_name_fallback 兜底分支 OK")
    except Exception as exc:
        failures.append(f"窗口过滤单元检查失败: {exc}")

    # 7) 已拍板开关 / 单一决策点 / 可插拔接口 / pipeline 过渡态防线结构
    try:
        entry, marker, success = select_task(cfg)
        assert cfg["flow"]["click_to_start"] is True, "click_to_start 必须为 true"
        assert cfg["launch"]["mode"] == "lnk", "launch.mode 必须锁定 lnk"
        assert "dmmplayer" not in cfg["launch"], "launch 段不应残留 dmmplayer 配置"
        assert entry == "LoginAndStart", "默认必须走点击进入游戏的 flow"
        print("[7] 用户已拍板（逐项断言）:")
        print(f"    flow.click_to_start = True  (决策点 select_task → entry={entry})")
        print(f"    launch.mode = 'lnk' 固定，launch 段无 dmmplayer 配置（单一 launch_game）")
        print(f"    据此选择: marker={marker} success={success}")
        ov = build_pipeline_override(cfg)
        assert "TitleScreenDetectedClick" in ov and "InGameSuccess" in ov
        assert ov["InGameSuccess"]["inverse"] is True, "默认应inverse判定(未填主界面模板)"
        # ★#18★ 点击目标不再是 target:true 的随机点，而是 Custom/ClickCenter 精确点框中心
        assert "target" not in ov["TitleScreenDetectedClick"], \
            "build_pipeline_override 不得注入 target（#18 起用 ClickCenter 点框中心，避免 rand_point 落框沿）"
        # ★轮播★ override 不得注入全图 wait_freezes
        assert "pre_wait_freezes" not in ov["InGameSuccess"] and \
               "post_wait_freezes" not in ov["InGameSuccess"], "不得注入全图 wait_freezes（立绘轮播）"
        # ★第5项补正★ pipeline 过渡态防线结构确认
        pdata = _validate_pipeline_json(PIPELINE_DIR / "login.json")
        click_node = pdata["TitleScreenDetectedClick"]
        assert click_node.get("action") == "Custom" and click_node.get("custom_action") == "ClickCenter", \
            "点击须走 Custom/ClickCenter（点识别框中心）"
        assert "target" not in click_node, "ClickCenter 方案下不得再写 target（会叠加随机偏移）"
        assert "pre_wait_freezes" not in click_node, "点击节点不得用全图 wait_freezes"
        assert "pre_wait_freezes" not in pdata["InGameSuccess"], "InGameSuccess 不得用全图 wait_freezes"
        assert "post_wait_freezes" not in pdata["InGameSuccess"], "InGameSuccess 不得用全图 post_wait_freezes"
        assert pdata["WaitInGame"]["next"][0] == "[JumpBack]HandleLoadingScreen", \
            "加载态回跳必须放在 WaitInGame.next 第一位，优先于 inverse 成功判定"
        assert click_node["post_delay"] >= 1500, "点击后需 post_delay 1500ms"
        assert pdata["HandleLoadingScreen"]["custom_recognition"] == "LoadingScreenReco"
        assert set(pdata["HandleLoadingScreen"]["custom_recognition_param"]) == \
            {"black_mean_max", "white_mean_min", "white_std_max", "std_min",
             "loading_template", "loading_template_threshold", "loading_template_roi"}, \
            "加载态须覆盖 黑/纯白(低方差)/低方差 + Now Loading 模板 四类参数"
        assert pdata["HandleLoadingScreen"]["custom_recognition_param"]["white_std_max"] == \
            cfg["recognition"]["loading_frame"]["white_std_max"], "pipeline 与配置的白屏方差阈值须一致"
        assert pdata["HandleLoadingScreen"]["custom_recognition_param"]["loading_template"] == \
            cfg["recognition"]["loading_template"]["file"], "pipeline 与配置的加载页模板须一致"
        assert OK_DEGRADED_UNVERIFIED != OK_MARKER and "UNVERIFIED" in OK_DEGRADED_UNVERIFIED, \
            "疑似成功须有独立结果码（且名字里带 UNVERIFIED，避免被当成成功）"
        # ★P0-0★ 校准参数一致性：build_detection_params 与 login.json 三个 TemplateMatch
        # 节点、以及 make_button_visible_fn 必须共用同一套 4 模板数组 + roi + threshold
        dp = build_detection_params(cfg)
        assert isinstance(dp["template"], list) and len(dp["template"]) >= 2, "须为多模板数组"
        assert dp["roi"] == cfg["recognition"]["template"]["roi"], "roi 须来自校准配置"
        for node in ["TitleScreenDetectedClick", "TitleScreenDetected", "InGameSuccess"]:
            assert pdata[node]["template"] == dp["template"], f"{node} 的 template 须与 build_detection_params 一致"
            assert pdata[node]["roi"] == dp["roi"], f"{node} 的 roi 须与校准一致"
            assert pdata[node]["threshold"] == dp["threshold"], f"{node} 的 threshold 须与校准一致"
        assert ov["TitleScreenDetectedClick"]["template"] == dp["template"], "override 的 template 须与校准一致"
        print(f"    ★P0-0 校准一致性 OK: {len(dp['template'])} 模板数组 + roi={dp['roi']} + "
              f"threshold={dp['threshold']}，三个节点/override/button_visible_fn 同源 build_detection_params")
        print(f"    build_pipeline_override 生成 {len(ov)} 项覆盖；InGameSuccess inverse="
              f"{ov['InGameSuccess']['inverse']}；点击走 ClickCenter、无 target 随机偏移、无全图 wait_freezes 注入")
        print("    过渡态防线 OK: WaitInGame.next[0]=[JumpBack]HandleLoadingScreen、"
              "post_delay=1500ms、LoadingScreenReco 覆盖黑/纯白低方差/低方差、"
              f"三态结果码 {OK_MARKER}/{OK_DEGRADED_UNVERIFIED}/{FAIL_NOT_CONFIRMED} 互斥")
        # ★#23 P1-1★ 三数组配置合法 + Python 侧正向判定可用 + override 不再含孤立死节点
        rec = cfg["recognition"]
        assert isinstance(rec["in_game_marker_template"], list) and \
               len(rec["in_game_marker_template"]) == 3, "模板须为 3 元素数组（字符串或数组接口均可）"
        assert rec["in_game_marker_threshold"] == [0.55, 0.75, 0.6] and \
               len(rec["in_game_marker_roi"]) == 3, "每模板各自阈值/roi 数组须与模板等长且合法"
        spec = build_marker_params(cfg)
        assert spec is not None and spec["template"] == rec["in_game_marker_template"], \
            "build_marker_params 须与配置一致"
        assert spec["threshold"] == [0.55, 0.75, 0.6] and \
               spec["roi"] == rec["in_game_marker_roi"], "每模板各自阈值/roi 须来自校准配置"
        assert ov["InGameSuccess"]["inverse"] is True, \
            "InGameSuccess 必须保持 inverse（离开标题前置信号），切正向会让 exit 3 不可达"
        assert not any(k.startswith("InGameMarker_") for k in ov), \
            "build_pipeline_override 不得生成孤立死节点 InGameMarker_*（#23 P1-1 已删除）"
        assert make_marker_hit_fn(None, cfg) is not None, \
            "3 模板有效时 Python 侧正向判定函数必须可用（权威判定不在 pipeline 里）"
        print(f"    ★#23 主界面模板数组 OK★: 3 模板各自 roi/threshold 配置合法、"
              f"make_marker_hit_fn 可用；override 无 InGameMarker_* 死节点；"
              f"InGameSuccess 保持 inverse=true（三态完整）")
    except Exception as exc:
        failures.append(f"开关/决策点检查失败: {exc}")

    # 8) pipeline_override 序列化
    try:
        json.dumps(build_pipeline_override(cfg), ensure_ascii=False)
        print("[8] pipeline_override 可被 json.dumps 序列化 OK")
    except Exception as exc:
        failures.append(f"override 序列化失败: {exc}")

    # 9) 权限自检：报告宿主状态与「非管理员时的提示文案」
    try:
        self_elev = is_self_elevated()
        print(f"[9] 权限自检: 当前宿主 ELEVATED={self_elev}（实跑前置 require_elevation 会据此拒绝/放行）")
        print("    非管理员时输出的中文提示文案（ELEVATION_HINT）:")
        for ln in ELEVATION_HINT.strip().splitlines():
            print("      | " + ln)
        print("    不自行弹 UAC / 不自行 runas；--check 不因非管理员而失败")
    except Exception as exc:
        failures.append(f"权限自检文案检查失败: {exc}")

    # 10) ★自匹配管道自检★（喂的是模板裁剪母图；DbgController 不可用 → CustomController 静态图）
    try:
        import maa
        dbg_dll = Path(maa.__file__).parent / "bin" / "MaaDbgControlUnit.dll"
        custom_dll = Path(maa.__file__).parent / "bin" / "MaaCustomControlUnit.dll"
        print("[10] ★自匹配管道自检★（喂 debug/screencap/02.11.27.757_InGameSuccess.png = 模板 v8 的裁剪母图，真实 bundle）")
        print("     ⚠️ 这是自匹配：NCC=1.0 必然命中、box 必然是裁剪框，只证明真实 bundle 能加载 +")
        print("        识别/点击链路跑通，★不证明真实帧识别能力★。真实帧能力由 [16] 真实帧断言覆盖。")
        print(f"     DbgController 所需 MaaDbgControlUnit.dll 存在={dbg_dll.exists()} → wheel 未随附、不可用")
        print(f"     MaaCustomControlUnit.dll 存在={custom_dll.exists()} → 用 CustomController 喂静态图跑通链路")
        ok, detail = offline_template_match_check(cfg)
        print(f"     管道自检结果: {'跑通' if ok else '未跑通'} | {detail}")
        if not ok:
            failures.append(f"自匹配管道自检失败: {detail}")
    except Exception as exc:
        failures.append(f"自匹配管道自检异常: {exc}")

    # 11) 加载态识别单测（黑/白/纯色/正常标题画面）
    try:
        import numpy as np
        from make_template import decode_png as _dec
        lf = cfg["recognition"]["loading_frame"]
        b, w, s = lf["black_mean_max"], lf["white_mean_min"], lf["std_min"]
        title = _dec(ARTIFACTS_DIR / "02-login-window-720p.png")[:, :, :3]
        cases = [
            ("白屏", np.full((720, 860, 3), 250, np.uint8), True),
            ("黑屏", np.full((720, 860, 3), 5, np.uint8), True),
            ("灰屏纯色", np.full((720, 860, 3), 128, np.uint8), True),
            ("标题画面", title, False),
        ]
        for name, frame, expect in cases:
            got = is_loading_frame(frame, b, w, s)
            assert got == expect, f"{name} 期望 is_loading={expect} 得到 {got}"
            print(f"      {name}: is_loading={got} (mean={frame.mean():.0f} std={frame.std():.0f})")
        print("[11] 加载态识别单测 OK：黑/白/低方差纯色三态全命中，标题画面不误判（黑白两态都覆盖）")
    except Exception as exc:
        failures.append(f"加载态识别单测失败: {exc}")

    # 12) 连续稳定复检逻辑单测（虚拟时钟 + 合成帧序列，不启动游戏、不点真实窗口）
    try:
        import numpy as np
        rng = np.random.default_rng(0)
        game = rng.integers(0, 256, (720, 860, 3), dtype=np.uint8)  # 非加载态、有纹理
        white = np.full((720, 860, 3), 250, np.uint8)
        black = np.full((720, 860, 3), 5, np.uint8)
        nop = lambda *a, **k: None

        # 12a 加载态(白→黑)之后连续稳定 → 必须累计满 5~10s 才成功
        clock = {"t": 0.0}
        seq = [white, white, black, black] + [game] * 40
        idx = {"i": 0}
        def cap():
            f = seq[min(idx["i"], len(seq) - 1)]
            idx["i"] += 1
            return f
        ok, why = confirm_in_game_stable(
            cfg, screencap_fn=cap, is_alive_fn=lambda: True,
            button_visible_fn=lambda f: False, log_fn=nop,
            now_fn=lambda: clock["t"], sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s))
        assert ok and "连续稳定" in why, f"加载态后应能稳定通过: {ok} {why}"
        assert clock["t"] >= cfg["flow"]["in_game_stable_sec"], "未累计满连续稳定时长就判成功"
        print(f"      [12a] 白屏→黑屏→稳定: PASS（{why}）")

        # 12b 加载态持续 → 不得误报成功（裸 inverse 会在此误命中）
        c2 = {"t": 0.0}
        ok2, why2 = confirm_in_game_stable(
            cfg, screencap_fn=lambda: white, is_alive_fn=lambda: True,
            button_visible_fn=lambda f: False, log_fn=nop,
            now_fn=lambda: c2["t"], sleep_fn=lambda s: c2.__setitem__("t", c2["t"] + s))
        assert not ok2 and "超时" in why2, f"加载态持续应超时失败: {ok2} {why2}"
        print(f"      [12b] 白屏加载态持续: 正确超时失败（{why2}）")

        # 12c 按钮始终可见 → 不得判成功
        c3 = {"t": 0.0}
        ok3, why3 = confirm_in_game_stable(
            cfg, screencap_fn=lambda: game, is_alive_fn=lambda: True,
            button_visible_fn=lambda f: True, log_fn=nop,
            now_fn=lambda: c3["t"], sleep_fn=lambda s: c3.__setitem__("t", c3["t"] + s))
        assert not ok3, "按钮一直可见不应成功"
        print(f"      [12c] 按钮始终可见: 正确不判成功（{why3}）")

        # 12d 进程退出 → 立即失败
        ok4, why4 = confirm_in_game_stable(
            cfg, screencap_fn=lambda: game, is_alive_fn=lambda: False,
            button_visible_fn=lambda f: False, log_fn=nop,
            now_fn=lambda: 0.0, sleep_fn=lambda s: None)
        assert not ok4 and "进程退出" in why4
        print(f"      [12d] 进程退出: 正确失败（{why4}）")
        print("[12] 连续稳定复检逻辑单测 OK（4 例全过：加载态/按钮可见/进程退出均会重置或拒绝）")
    except Exception as exc:
        failures.append(f"连续稳定复检单测失败: {exc}")

    # 13) 兜底参数按 06-diagnosis.md §5 第 4 条校准
    try:
        f, l = cfg["flow"], cfg["launch"]
        assert 2 <= l["window_poll_interval_sec"] <= 3, "轮询间隔须 2~3s"
        assert l["window_poll_timeout_sec"] == 20, "启动确认超时须 20s（覆盖约 10s 加载）"
        assert 2 <= f["process_check_interval_sec"] <= 3, "进程轮询须 2~3s"
        assert f["stable_alive_sec"] >= 60, "连续存活≥60s 判本次启动可用"
        assert 10 <= f["restart_wait_sec"] <= 15, "退出后等 10~15s 再重启"
        assert f["max_restarts"] == 2, "最多重启 2 次（合计 3 次启动）"
        assert 300 <= f["start_rate_window_sec"] <= 600, "5~10 分钟限流窗口"
        assert f["max_starts_per_window"] == 3, "窗口内不超过 3 次启动"
        assert 5 <= f["in_game_stable_sec"] <= 10, "连续稳定 5~10s"
        assert FAIL_DETERMINISTIC and FAIL_STABLE_TIMEOUT, "确定性失败/稳定超时须有明确失败码"
        print("[13] 兜底参数(§5-4)校准 OK: "
              f"轮询={l['window_poll_interval_sec']}s/{f['process_check_interval_sec']}s "
              f"启动确认={l['window_poll_timeout_sec']}s 连续存活≥{f['stable_alive_sec']:.0f}s(仅观测口径) "
              f"退出后等{f['restart_wait_sec']:.0f}s 最多重启{f['max_restarts']}次(合计3次启动) "
              f"{int(f['start_rate_window_sec'])}s窗口≤{f['max_starts_per_window']}次 "
              f"连续稳定{f['in_game_stable_sec']:.0f}s | 明确失败码 {FAIL_DETERMINISTIC}/{FAIL_STABLE_TIMEOUT}")
        print("     注明：60s 仅作重启决策/确定性失败标注的观测口径，非登录前置、非长期保活；"
              "不写令牌刷新逻辑")
    except Exception as exc:
        failures.append(f"兜底参数校准检查失败: {exc}")

    # 14) ★P0 守护★ 真实 resource bundle 必须能 post_bundle 成功
    # （#3 遗留隐藏缺陷：空 model/ocr 目录会让整个 bundle 加载失败，实跑必然 FAIL_RESOURCE）
    try:
        import tempfile
        from maa.resource import Resource as _R
        Toolkit.init_option(str(Path(tempfile.mkdtemp())), {"logging": False, "stdout_level": 0})
        r = _R()
        r.register_custom_recognition(CUSTOM_RECO_NAME, LoadingScreenReco())
        j = r.post_bundle(str(RESOURCE_DIR)); j.wait()
        assert j.succeeded, "真实 resource bundle post_bundle 失败（检查 model/ocr 空目录）"
        Tasker.set_log_dir(str(DEBUG_DIR))  # 恢复全局日志目录
        print("[14] ★真实 bundle post_bundle 成功★（P0 守护：model/ocr 空目录缺陷已消除，实跑不会 FAIL_RESOURCE）")
    except Exception as exc:
        failures.append(f"真实 bundle 加载检查失败: {exc}")

    # 16) ★P0-0 真实帧断言★ 用真实 make_button_visible_fn + 多张独立真实帧（非写死 lambda）
    try:
        ok, detail = real_frame_button_visibility_check(cfg)
        print("[16] ★真实帧 button_visible 断言★（真实 make_button_visible_fn，非合成/写死 lambda）")
        print(f"     {detail}")
        if not ok:
            failures.append(f"真实帧 button_visible 断言失败: {detail}")
    except Exception as exc:
        failures.append(f"真实帧 button_visible 断言异常: {exc}")

    # 17) 三态加载阈值在真实帧上的验证（★#18：白屏必须带低方差，亮标题帧不得被判加载★）
    try:
        import numpy as np
        lf = cfg["recognition"]["loading_frame"]
        b, w, s, ws = (lf["black_mean_max"], lf["white_mean_min"],
                       lf["std_min"], lf["white_std_max"])
        cases = [
            # (名称, 帧, 期望 is_loading, 说明)
            ("黑屏过渡帧", _load_real_frame_bgr("vision/2026.11.07-02.10.16.166_HandleLoadingScreen_400000021.jpg"), True, "mean 2.8/std 16.0"),
            ("白色警告页", _load_real_frame_bgr("vision/2026.11.07-02.10.18.652_HandleLoadingScreen_400000031.jpg"), False, "mean 251.5/std 22.1 有文字≠纯白"),
            ("白色公告页", _load_real_frame_bgr("vision/2026.11.07-02.10.17.163_HandleLoadingScreen_400000025.jpg"), False, "mean 246.9/std 32.0 有文字≠纯白"),
            ("亮标题帧(按钮可见)", _load_real_frame_bgr("vision/2026.11.07-02.11.05.315_HandleLoadingScreen_400000124.jpg"), False, "mean 239.7/std 23.1 ★旧规则会误判加载★"),
            ("干净标题帧", _load_real_frame_bgr("screencap/2026.11.07-02.11.27.757_InGameSuccess.png"), False, "mean≈135/std≈73"),
            ("合成纯白", np.full((720, 1280, 3), 250, np.uint8), True, "250/0"),
            ("合成纯黑", np.full((720, 1280, 3), 5, np.uint8), True, "5/0"),
            ("合成纯灰", np.full((720, 1280, 3), 128, np.uint8), True, "128/0"),
        ]
        for name, frame, expect, stat in cases:
            got = is_loading_frame(frame, b, w, s, ws)
            assert got == expect, f"{name} is_loading 期望 {expect} 得到 {got} ({stat})"
            print(f"      {name:18s} is_loading={got!s:5s} (期望 {expect!s:5s}) {stat} "
                  f"[实测 mean={frame.mean():.0f}/std={frame.std():.0f}]")
        print(f"[17] 三态加载阈值真实帧验证 OK：黑屏/纯白(低方差)/纯色命中；"
              f"亮标题帧(mean 239.7/std 23.1)、白色警告页/公告页(有文字)不误判 —— "
              f"white_std_max={ws} 修掉了『亮场永不点按钮』的 P0")
    except Exception as exc:
        failures.append(f"三态阈值真实帧验证失败: {exc}")

    # 18) P1-8 受控点击：源码级确认 run() 有 click_count/max_clicks 与达上限禁用点击
    try:
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        run_src = src[: src.index("def static_self_check")]  # 只看 run()/辅助函数区，避免自证
        assert "click_count = 0" in run_src and "max_clicks = int(flow.get(\"max_clicks\", 2))" in run_src, \
            "run() 缺少受控点击计数（click_count / max_clicks）"
        assert '"action": "DoNothing"' in run_src and "P1-8" in run_src, "重跑时未禁用点击动作(P1-8)"
        assert int(cfg["flow"]["max_clicks"]) <= 2, "受控点击总次数上限必须 ≤2"
        print(f"[18] P1-8 受控点击 OK：run() 维护 click_count，上限 max_clicks="
              f"{cfg['flow']['max_clicks']}（≤2），达上限/几何未收敛时把 "
              f"TitleScreenDetectedClick.action 覆盖为 DoNothing，不无限点击")
    except Exception as exc:
        failures.append(f"P1-8 检查失败: {exc}")

    # 19) ★#14★ 连接前窗口预调整：compute_prealign_rect 纯函数单测（不启动游戏、不碰真实窗口）
    try:
        cases = [
            # (mon_work(l,t,r,b), cur_rect(x,y,w,h), desired_size, expect)
            ((0, 0, 2560, 1560), (-130, -102, 2268, 1328), None, (0, 0, 2268, 1328)),   # 基线越界→移入,尺寸不变
            ((0, 0, 2560, 1560), (0, 0, 2589, 1585), None, (0, 0, 2560, 1560)),          # 超大→cap 到工作区
            ((0, 0, 2560, 1560), (200, 100, 1600, 900), None, (200, 100, 1600, 900)),    # 已合法→不动
            ((0, 0, 2560, 1560), (-2000, -2000, 1000, 800), None, (0, 0, 1000, 800)),    # 远负→移入
            ((0, 0, 2560, 1560), (3000, 0, 800, 600), None, (1760, 0, 800, 600)),        # 远右→右对齐
            ((-823, -216, -103, 864), (-900, -300, 720, 1080), None, (-823, -216, 720, 1080)),  # 副屏负坐标
            ((0, 0, 2560, 1560), (0, 0, 1600, 900), [2268, 1328], (0, 0, 2268, 1328)),   # desired 覆盖
        ]
        for mon, cur, desired, expect in cases:
            got = compute_prealign_rect(mon, cur, desired)
            assert got == expect, f"compute_prealign_rect({mon},{cur},{desired}) 期望 {expect} 得到 {got}"
        # 关键不变式：基线窗口经预调整后尺寸与原始一致（仅移动、不放大），且左上角落到非负
        base = compute_prealign_rect((0, 0, 2560, 1560), (-130, -102, 2268, 1328), None)
        assert (base[2], base[3]) == (2268, 1328), "基线窗口预调整后尺寸必须保持不变（不放大）"
        assert base[0] >= 0 and base[1] >= 0, "预调整后左上角必须落在工作区内"
        # 源码接线守护：prealign_window 存在且确实在 build_session 里、创建 controller 之前被调用
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        assert "def prealign_window(" in src, "缺少 prealign_window"
        assert "def compute_prealign_rect(" in src, "缺少 compute_prealign_rect"
        assert "prealign_window(win.hwnd, cfg)" in src, "build_session 未调用 prealign_window"
        assert src.index("prealign_window(win.hwnd, cfg)") < src.index("Win32Controller(win.hwnd"), \
            "prealign_window 必须在创建 Win32Controller 之前调用"
        assert "def init_dpi_awareness(" in src, "缺少 init_dpi_awareness"
        print(f"[19] ★#14 窗口预调整纯函数单测 OK（{len(cases)} 例 + 基线尺寸不变式 + build_session 接线）: "
              f"基线越界仅移动不动尺寸、超大 cap、合法不动、负坐标/副屏 clamp、desired 覆盖")
    except Exception as exc:
        failures.append(f"窗口预调整纯函数单测失败: {exc}")

    # 20) ★#18★ 三处根因的守护：置前接线 / 判定不得裸 inverse / 点击点落框内 + 模板几何一致
    try:
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        run_src = src[: src.index("def static_self_check")]
        run_body = src[src.index("def run(cfg: dict"): src.index("def static_self_check")]

        # (a) ★#21 更新★ 判定必须走 post_click_verify 三态；裸 inverse 与旧稳定复检都不得回来
        assert "post_click_verify(" in run_body, "run() 必须用 post_click_verify 做点击后三态判定"
        assert "verdict == \"MARKER\"" in run_body and "OK_DEGRADED_UNVERIFIED" in run_body, \
            "run() 必须按 MARKER / OK_DEGRADED_UNVERIFIED 分支返回"
        assert "FAIL_NOT_CONFIRMED" in run_body, "未确认时必须有明确失败码 NOT_CONFIRMED"
        assert "confirm_in_game_stable(" not in run_body, \
            "run() 不得再调用 confirm_in_game_stable（其成功条件就是『按钮不可见』= 裸 inverse）"
        assert "confirm_not_loading_stable(" not in run_body, \
            "旧 confirm_not_loading_stable 已被 post_click_verify 取代（避免两套判定并存）"
        assert 'if name == "HandleLoadingScreen" and self.clicked' in src, \
            "loading_seen_after_click 必须限定『点击之后』才置位"

        # (b) 置前接线：build_session 连接前 + run() 每轮 pipeline 前
        bs = src[src.index("def build_session"): src.index("def make_button_visible_fn")]
        assert "force_foreground(win.hwnd)" in bs, "build_session 未调用 force_foreground"
        assert bs.index("force_foreground(win.hwnd)") < bs.index("Win32Controller(win.hwnd"), \
            "force_foreground 必须在创建 Win32Controller 之前调用"
        assert "AttachThreadInput" in src and "GetForegroundWindow" in src, \
            "force_foreground 必须用 AttachThreadInput 绕过前台锁并校验 GetForegroundWindow"
        assert "force_foreground(hwnd)" in run_body, "run() 每轮 pipeline 前未调用 force_foreground"

        # (c) 点击点落在识别框内（真实调用 ClickCenterAction，用假 controller 收集坐标）
        class _Job:
            succeeded = True

            def wait(self):
                return self

        class _Ctrl:
            def __init__(self):
                self.clicks = []

            def post_click(self, x, y):
                self.clicks.append((int(x), int(y)))
                return _Job()

        fake = _Ctrl()
        ctx = type("Ctx", (), {"tasker": type("T", (), {"controller": fake})()})()
        x0, y0, x1, y1 = cfg["recognition"]["template"]["crop"]
        box = type("Box", (), {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})()
        arv = type("Arg", (), {"box": box})()
        clicked = ClickCenterAction().run(ctx, arv)
        assert clicked and len(fake.clicks) == 1, "ClickCenterAction 未发出点击"
        cx, cy = fake.clicks[0]
        assert x0 <= cx <= x1 and y0 <= cy <= y1, f"点击点 ({cx},{cy}) 未落在识别框 {cfg['recognition']['template']['crop']} 内"
        assert abs(cx - (x0 + x1) // 2) <= 1 and abs(cy - (y0 + y1) // 2) <= 1, \
            f"点击点 ({cx},{cy}) 不是框中心"

        # (d) 模板几何/阈值与实测一致
        cal = cfg["recognition"]["calibration"]
        thr = float(cfg["recognition"]["template"]["threshold"])
        assert float(cal["negative_ncc_max"]) < thr < float(cal["visible_ncc_min"]), \
            f"阈值 {thr} 未落在 (负样本最高 {cal['negative_ncc_max']}, 正样本最低 {cal['visible_ncc_min']}) 之间"
        assert int(cfg["controller"]["screenshot_target_long_side"]) == 1280, "截图目标长边须为 1280（模板校准几何）"
        assert int(cfg["controller"]["geometry_expect_height"]) == 720, "期望帧高须为 720"
        print(f"[20] ★#18 三根因守护 OK★")
        print(f"    (a) 判定: 走 post_click_verify 三态（MARKER / OK_DEGRADED_UNVERIFIED / NOT_CONFIRMED）；"
              f"裸 inverse 与旧稳定复检均已移除")
        print(f"    (b) 置前: build_session 连接前 + 每轮 pipeline 前均 force_foreground("
              f"AttachThreadInput+SetForegroundWindow，校验 GetForegroundWindow)")
        print(f"    (c) 点击: ClickCenter 实测点击 ({cx},{cy})，落在框 {cfg['recognition']['template']['crop']} 内且为框中心")
        print(f"    (d) 几何/阈值: 长边={cfg['controller']['screenshot_target_long_side']}×高"
              f"{cfg['controller']['geometry_expect_height']}；threshold={thr} ∈ "
              f"({cal['negative_ncc_max']}, {cal['visible_ncc_min']})")
    except Exception as exc:
        failures.append(f"#18 三根因守护检查失败: {exc}")

    # 21) ★#21★ post_click_verify 三态判定单测（虚拟时钟 + 合成帧序列，不启动游戏）
    try:
        import numpy as np
        rng = np.random.default_rng(7)
        game = rng.integers(0, 256, (720, 1280, 3), dtype=np.uint8)  # 有纹理、非加载
        white = np.full((720, 1280, 3), 250, np.uint8)               # 纯白（加载态）
        nop = lambda *a, **k: None
        flow = cfg["flow"]
        absent_need = float(flow["in_game_absent_sec"])
        window = float(flow["in_game_verify_sec"])

        def run_verify(clock, *, frames, vis_fn, alive=True, marker=None):
            idx = {"i": 0}

            def cap():
                f = frames[min(idx["i"], len(frames) - 1)]
                idx["i"] += 1
                return f

            return post_click_verify(
                cfg, screencap_fn=cap, is_alive_fn=(lambda: alive) if callable(alive) else (lambda: alive),
                button_visible_fn=vis_fn, marker_hit_fn=marker, log_fn=nop,
                now_fn=lambda: clock["t"], sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s))

        # 21a 仍在标题（按钮反复可见）→ STILL_TITLE，绝不判成功
        c1 = {"t": 0.0}
        v1, w1 = run_verify(c1, frames=[game] * 80, vis_fn=lambda f: True)
        assert v1 == "STILL_TITLE", f"按钮一直可见应为 STILL_TITLE，实际 {v1} {w1}"
        print(f"      [21a] 按钮反复可见: {v1}（{w1}）")

        # 21b 按钮一直不见 + 非加载 → LEFT_TITLE，且必须等满 absent_sec 才下结论
        c2 = {"t": 0.0}
        v2, w2 = run_verify(c2, frames=[game] * 80, vis_fn=lambda f: False)
        assert v2 == "LEFT_TITLE", f"连续不见应为 LEFT_TITLE，实际 {v2} {w2}"
        assert c2["t"] >= absent_need, f"疑似结论必须等满 {absent_need}s，实际 {c2['t']:.1f}s"
        print(f"      [21b] 连续 {absent_need:.0f}s 不见: {v2}（用时 {c2['t']:.1f}s）")

        # 21c 正向模板命中 → MARKER（立即，不等窗口）
        c3 = {"t": 0.0}
        v3, w3 = run_verify(c3, frames=[game] * 80, vis_fn=lambda f: False, marker=lambda f: True)
        assert v3 == "MARKER", f"marker 命中应为 MARKER，实际 {v3} {w3}"
        assert c3["t"] <= 2.0, "marker 命中应立即返回，不应等窗口结束"
        print(f"      [21c] 主界面模板命中: {v3}（{w3}）")

        # 21d 点击后进程/窗口消失（实测客户端会重启）→ SESSION_LOST
        c4 = {"t": 0.0}
        v4, w4 = run_verify(c4, frames=[game], vis_fn=lambda f: False, alive=False)
        assert v4 == "SESSION_LOST", f"进程消失应为 SESSION_LOST，实际 {v4} {w4}"
        print(f"      [21d] 点击后进程消失: {v4}（{w4}）")

        # 21e 一直加载（按钮不见但画面是纯白）→ 不得给疑似成功
        c5 = {"t": 0.0}
        v5, w5 = run_verify(c5, frames=[white] * 80, vis_fn=lambda f: False)
        assert v5 == "TIMEOUT", f"持续加载应为 TIMEOUT，实际 {v5} {w5}"
        assert c5["t"] >= window, "应等满整个判定窗口"
        print(f"      [21e] 全程加载态: {v5}（{w5}）")

        print("[21] ★#21 post_click_verify 三态单测 OK★（仍标题/连续不见/模板命中/会话丢失/全程加载 五例，"
              "任何单帧「按钮消失」都不产生成功结论）")
    except Exception as exc:
        failures.append(f"#21 三态判定单测失败: {exc}")

    # 22) ★#21★ 三态互斥语义 + 取证留图 + marker 为空不得判 OK_MARKER
    try:
        import tempfile
        import numpy as np
        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        run_body = src[src.index("def run(cfg: dict"): src.index("def static_self_check")]

        # (a) 三态互斥且语义正确：UNVERIFIED 绝不 ok=True；NOT_CONFIRMED 与 OK_MARKER 互斥
        codes = {OK_MARKER, OK_DEGRADED_UNVERIFIED, FAIL_NOT_CONFIRMED}
        assert len(codes) == 3, "三种结果码必须互不相同"
        assert "return False, OK_DEGRADED_UNVERIFIED" in run_body, \
            "疑似成功必须 return False（不得当成功返回）"
        assert "return True, OK_MARKER" in run_body, "只有正向模板命中才 return True"
        assert "UNVERIFIED" in OK_DEGRADED_UNVERIFIED and "OK_" not in FAIL_NOT_CONFIRMED

        # (b) 失败路径也留图：pipeline 未达标志 + 判定结论两个路径都要存
        assert 'save_probe_frame(ctrl.post_screencap().wait().get(), "fail_no_flag"' in run_body, \
            "失败路径（未达成功标志）未留图"
        assert 'save_probe_frame(ctrl.post_screencap().wait().get(), "verdict"' in run_body, \
            "判定时刻未留图"
        assert "probe_fn=lambda f, tag: save_probe_frame(f, tag" in run_body, \
            "三态判定各结论（marker/疑似/仍标题/超时）未接取证留图"

        # (c) ★#22/#23★ marker 为 null / 空数组 / 文件缺失时绝不判 OK_MARKER
        #     默认配置现已是 3 模板数组，故用「显式置空副本」验证降级守护。
        #     ★#23★ 新增「直判」路径同样纳入守护：它绕开 sink.is_success 门槛，必须与
        #     post_click_verify 一样在 marker 失效时禁用，绝不回退裸 inverse。
        assert "marker_hit_fn = make_marker_hit_fn(tasker, cfg)" in run_body, \
            "run() 必须用 make_marker_hit_fn（其内部做硬守护）"
        assert "direct_marker_confirm(" in run_body, \
            "run() 必须调用直判函数绕开 sink.is_success 门槛（#23 P1-2）"
        assert 'if _direct_confirm("pipeline 前")' in run_body and \
               'if _direct_confirm("门控前")' in run_body, \
            "直判须在每轮 pipeline 前与门控前各一次（覆盖 SESSION_LOST 重连后的新会话）"
        # ★#25★ 点击前守护已删除：(0,0) 场景（二次运行/--skip-launch/残留窗口已在主界面）
        # 不得再被挡住，否则主界面无按钮可点会空转到 FAIL_MAX_RESTARTS。
        assert "if click_count < 1 and restarts < 1:" not in run_body, \
            "#25 已删除『点击前不直判』守护，直判须每轮都执行"
        for bad, desc in [(None, "null"), ([], "空数组"), (["__missing__.png"], "文件缺失")]:
            c = json.loads(json.dumps(cfg))
            c["recognition"]["in_game_marker_template"] = bad
            assert build_marker_params(c) is None, f"marker={desc} 时 build_marker_params 必须返回 None"
            assert make_marker_hit_fn(None, c) is None, f"marker={desc} 时不得返回命中函数"
            assert direct_marker_confirm(None, c, make_marker_hit_fn(None, c)) is False, \
                f"marker={desc} 时直判路径也必须禁用（绝不判 OK_MARKER）"
            ov_empty = build_pipeline_override(c)
            assert ov_empty["InGameSuccess"]["inverse"] is True and \
                   not any(k.startswith("InGameMarker_") for k in ov_empty), \
                   f"marker={desc} 时不得生成正向节点且 InGameSuccess 维持 inverse"
            assert ov_empty["InGameSuccess"]["template"] == build_detection_params(c)["template"], \
                f"marker={desc} 时 InGameSuccess 的 template 仍是按钮模板，不得变成主界面模板"
        # ★#23 P1-1★ marker 有效时 override 同样不得含死节点（不只是空配置）
        assert not any(k.startswith("InGameMarker_") for k in build_pipeline_override(cfg)), \
            "marker 有效时 build_pipeline_override 同样不得生成 InGameMarker_* 死节点"
        print("    marker 守护: null/空数组/文件缺失 → build_marker_params=None、"
              "make_marker_hit_fn=None、直判禁用、无正向节点、InGameSuccess 维持 inverse；"
              "直判在 pipeline 前+门控前各一次（绝不判 OK_MARKER）；"
              "#25 已删除『点击前不直判』守护（(0,0) 场景不再被挡）")

        # (d) 取证留图函数本身可用：1280×720 原图落盘、命名含 in_game_probe_、不覆盖已有文件
        tmpd = Path(tempfile.mkdtemp(prefix="probe_test_"))
        frame = np.zeros((720, 1280, 3), np.uint8)
        frame[:, :, 2] = 200  # BGR 蓝
        p1 = save_probe_frame(frame, "selftest", log_fn=lambda *a, **k: None, out_dir=tmpd)
        assert p1 and Path(p1).is_file(), "取证留图未生成文件"
        assert "in_game_probe_" in Path(p1).name and "_selftest.png" in Path(p1).name
        with open(p1, "rb") as f:
            head = f.read(33)
        assert head[:8] == b"\x89PNG\r\n\x1a\n", "留图不是 PNG"
        assert struct.unpack(">II", head[16:24]) == (1280, 720), "留图必须是原始 1280×720"
        # (e) ★#21★ 最终文案与退出码必须三态分明（用户不能把 exit 3 看成失败）——实测调用
        main_body = src[src.index("def main(argv=None)"):]
        assert "report_result(reason)" in main_body, "main() 必须走 report_result 统一三态输出"

        l1: list[str] = []
        assert report_result(OK_MARKER, log_fn=l1.append) == 0, "OK_MARKER 退出码必须为 0"
        assert any("结果: 成功（已确认进入主界面）" in x for x in l1), f"OK_MARKER 文案不对: {l1}"

        l2: list[str] = []
        assert report_result(FAIL_NOT_CONFIRMED, log_fn=l2.append) == 2, "NOT_CONFIRMED 退出码必须为 2"
        assert any("结果: 失败（未离开标题画面）" in x for x in l2), f"NOT_CONFIRMED 文案不对: {l2}"

        l3: list[str] = []
        assert report_result(OK_DEGRADED_UNVERIFIED, log_fn=l3.append) == 3, "UNVERIFIED 退出码必须为 3"
        assert any("结果: 疑似成功（已离开标题并进入加载/游戏，但未确认主界面）" in x for x in l3), \
            f"UNVERIFIED 文案不对: {l3}"
        assert any("这不是失败，是待确认" in x for x in l3), "exit 3 必须显式声明『不是失败，是待确认』"
        assert any("in_game_probe_" in x or "判定留图" in x for x in l3), "exit 3 必须打印 probe 留图路径/提示"
        assert not any("失败（" in x and "疑似" not in x for x in l3), "exit 3 的输出不得出现『失败（』字样"

        print(f"[22] ★#21 三态/留图/marker 守护 OK★: 三态互斥且 UNVERIFIED 不返回成功；"
              f"失败路径与判定结论均留图；marker 为空时不会判 OK_MARKER；"
              f"留图实测 {Path(p1).name}（1280×720 PNG，只新增不覆盖）；"
              f"实测 report_result → 0/2/3 且 exit 3 文案含『疑似成功/待确认』不含『失败（』")
    except Exception as exc:
        failures.append(f"#21 三态/留图检查失败: {exc}")

    # 23) ★#21★ 真实加载页（用户素材）复合判据 + 回归（模板命中不得把标题帧误判成加载）
    try:
        import numpy as np
        lt = cfg["recognition"]["loading_template"]
        lf = cfg["recognition"]["loading_frame"]
        tpl_path = IMAGE_DIR / lt["file"]
        assert tpl_path.is_file(), f"缺少加载页模板 {tpl_path}"
        with tpl_path.open("rb") as f:
            head = f.read(33)
        tw, th = struct.unpack(">II", head[16:24])
        assert (tw, th) == (255, 39), f"加载页模板尺寸应为 255x39，实际 {tw}x{th}"

        kw = dict(black_mean_max=lf["black_mean_max"], white_mean_min=lf["white_mean_min"],
                  std_min=lf["std_min"], white_std_max=lf["white_std_max"],
                  template=lt["file"], template_threshold=float(lt["threshold"]),
                  template_roi=tuple(lt["roi"]))

        loading_page = _load_real_frame_bgr("20-loading-1280.png")   # 用户素材归一化到 1280×720
        hit, info = detect_loading_frame(loading_page, **kw)
        score = match_loading_template(loading_page, lt["file"], tuple(lt["roi"]))
        assert hit and info.get("reason") == "loading-template", \
            f"真实加载页必须判加载态且走模板判据，实际 hit={hit} info={info}"
        assert score >= float(lt["threshold"]), f"加载页模板得分 {score:.3f} 低于阈值 {lt['threshold']}"
        print(f"      真实加载页 artifacts/20-loading-1280.png（用户素材归一化）: is_loading={hit} "
              f"reason={info['reason']} tpl_score={score:.3f} mean={info.get('mean')} std={info.get('std')}")

        # 回归：亮标题帧 / 干净标题帧 不得因模板被误判成加载（全部为真实帧文件）
        for name, stat in [("vision/2026.11.07-02.11.05.315_HandleLoadingScreen_400000124.jpg", "亮标题 mean 239.7/std 23.1"),
                           ("vision/2026.11.07-02.10.22.145_HandleLoadingScreen_400000041.jpg", "干净标题"),
                           ("screencap/2026.11.07-02.11.27.757_InGameSuccess.png", "判定帧")]:
            fr = _load_real_frame_bgr(name)
            hit, info = detect_loading_frame(fr, **kw)
            assert not hit, f"{name} 不应判加载态（{stat}），实际 info={info}"
            print(f"      {name:64s} is_loading=False tpl_score={info.get('tpl_score')} ({stat})")

        # 回归：真黑屏仍必须命中（统计判据），纯白/纯灰同理
        for name, expect in [("vision/2026.11.07-02.10.16.166_HandleLoadingScreen_400000021.jpg", True)]:
            fr = _load_real_frame_bgr(name)
            hit, info = detect_loading_frame(fr, **kw)
            assert hit, f"{name} 应判加载态（真黑屏过渡），实际 info={info}"
            print(f"      {name:64s} is_loading=True reason={info['reason']}")
        for label, val in [("合成纯白", 250), ("合成纯黑", 5), ("合成纯灰", 128)]:
            fr = np.full((720, 1280, 3), val, np.uint8)
            hit, info = detect_loading_frame(fr, **kw)
            assert hit and info["reason"].startswith("stats"), f"{label} 应由统计判据命中，实际 {info}"
            print(f"      {label:18s} is_loading=True reason={info['reason']} tpl_score={info.get('tpl_score')}")

        print(f"[23] ★#21 真实加载页判据 OK★：Now Loading 模板 {tw}x{th}，真实加载页 tpl={score:.3f} ≥ {lt['threshold']}；"
              f"标题帧/判定帧全部 False（模板未误伤），真黑屏与纯色仍由统计判据命中")
    except Exception as exc:
        failures.append(f"#21 加载页判据检查失败: {exc}")

    # 24) ★#22★ 主界面模板端到端（真实 MaaFW bundle）+ 加载态不误判 + 空/缺文件守护
    try:
        spec = build_marker_params(cfg)
        assert spec is not None and len(spec["template"]) == 3
        for f in spec["template"]:
            assert (IMAGE_DIR / f).is_file(), f"缺少主界面模板 {f}"

        home = _load_real_frame_bgr("screencap/in_game_probe_2026.11.07-10.50.00.294_unverified.png")
        title = _load_real_frame_bgr("screencap/in_game_probe_2026.11.07-10.48.45.318_verdict.png")
        loading_page = _load_real_frame_bgr("20-loading-1280.png")
        tasker, ctrl, cleanup = _make_static_tasker(home, cfg)
        try:
            hitfn = make_marker_hit_fn(tasker, cfg)
            assert hitfn is not None, "已配置 3 模板时 make_marker_hit_fn 必须可用"
            assert hitfn(home) is True, "真实主界面帧必须命中正向主界面模板（→ OK_MARKER）"
            assert hitfn(title) is False, "标题帧绝不可命中主界面模板"
            assert hitfn(loading_page) is False, "加载页帧绝不可命中主界面模板"
            # ★#23★ 直判路径（绕开 sink.is_success 门槛）同样不得误报：
            # 主界面命中，标题帧/加载页帧/marker 失效一律不命中
            assert direct_marker_confirm(home, cfg, hitfn) is True, "主界面帧直判须命中"
            assert direct_marker_confirm(title, cfg, hitfn) is False, "标题帧直判不得命中"
            assert direct_marker_confirm(loading_page, cfg, hitfn) is False, "加载页帧直判不得命中"
            assert direct_marker_confirm(home, cfg, None) is False, "marker 失效时直判须禁用"
        finally:
            cleanup()

        src = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        assert "if not loading and marker_hit_fn is not None" in src, \
            "post_click_verify 必须先排除加载态再做正向判定"

        # 单字符串（可插拔）仍兼容
        c_one = json.loads(json.dumps(cfg))
        c_one["recognition"]["in_game_marker_template"] = spec["template"][0]
        assert build_marker_params(c_one)["template"] == [spec["template"][0]], \
            "in_game_marker_template 仍须兼容单字符串"

        # ★#23 P2-1★ 空阈值数组不得触发 thrs[-1] IndexError（回退默认 0.7 广播）
        c_empty_thr = json.loads(json.dumps(cfg))
        c_empty_thr["recognition"]["in_game_marker_threshold"] = []
        spec_empty = build_marker_params(c_empty_thr)
        assert spec_empty is not None and spec_empty["threshold"] == [0.7, 0.7, 0.7] and \
               spec_empty["template"] == spec["template"], \
            "空阈值数组须回退默认 0.7 并广播到 3 模板，不得崩溃/不返回 None"

        # ★#25 P1-1★ 低阈值 / 长度不一致配置一律禁用正向判定（防标题帧假成功）。
        # 复核用真实 MaaFW 判定器实测：标题帧 home_lb_diamond score=0.507051，阈值 ≤0.507 即误命中。
        for bad_thr, desc in [([0.1] * 3, "[0.1]*3"),
                              ([0.5] * 3, "[0.5]*3"),
                              ([0.1, 0.2], "[0.1,0.2] 长度不足"),
                              ([0.1, 0.2, 0.3, 0.4, 0.5], "[0.1..0.5] 长度超出")]:
            c_bad = json.loads(json.dumps(cfg))
            c_bad["recognition"]["in_game_marker_threshold"] = bad_thr
            assert build_marker_params(c_bad) is None, \
                f"阈值 {desc} 必须被拒绝（下限 0.55 / 长度须与模板等长）"
            bad_hitfn = make_marker_hit_fn(None, c_bad)
            assert bad_hitfn is None, f"阈值 {desc} 时不得返回命中函数"
            assert direct_marker_confirm(title, c_bad, bad_hitfn) is False, \
                f"阈值 {desc} 时标题帧直判不得命中（绝不假成功）"
        assert build_marker_params(cfg) is not None and \
               len(build_marker_params(cfg)["template"]) == 3, \
            "当前校准配置 [0.55,0.75,0.6] 必须仍返回 3 模板参数"

        print(f"[24] ★#22/#23/#25 主界面模板端到端 OK★: 真实 MaaFW bundle 下 主界面帧命中 / "
              f"标题帧与加载页帧均不命中；直判路径同款守护（标题/加载页/marker 失效不误报）；"
              f"加载态不参与判定（源码守护）；3 模板各自 roi={spec['roi']} "
              f"threshold={spec['threshold']}；单字符串兼容；空阈值数组回退 0.7 不崩溃；"
              f"★#25 低阈值/长度不齐 4 组畸形配置一律禁用正向判定、标题帧直判 False★；"
              f"marker=null/空数组/文件缺失 一律禁用正向判定（绝不判 OK_MARKER）")
    except Exception as exc:
        failures.append(f"#22/#23 主界面模板检查失败: {exc}")

    # 25) ★#23 P1-1③★ 把**完整** build_pipeline_override（与 run() 同源）喂给真实 post_task。
    #     历史盲区：[10] 用自定义最小 ov、[24] 用 post_recognition、[14] 用原始 login.json 的
    #     post_bundle —— 「含 override 的完整节点图」从未被 post_task 真跑过一次。这里补上。
    try:
        home25 = _load_real_frame_bgr("screencap/in_game_probe_2026.11.07-10.50.00.294_unverified.png")
        entry25, _, _ = select_task(cfg)
        full_ov = build_pipeline_override(cfg)
        assert full_ov["InGameSuccess"]["inverse"] is True, "完整 override 不得改变三态结构"
        assert not any(k.startswith("InGameMarker_") for k in full_ov), \
            "完整 override 不得含孤立死节点"
        tasker, ctrl, cleanup = _make_static_tasker(home25, cfg)
        try:
            job = tasker.post_task(entry25, full_ov)   # 与 run() 同源的完整 override
            assert job is not None, "post_task 未返回 job"
            tasker.post_stop()   # 静态帧下 pipeline 会等超时节点，立即停止；只验证注入/节点图
            job.wait()
            assert tasker.inited, "注入完整 override 后 tasker 失效"
        finally:
            cleanup()
        print(f"[25] ★#23 完整 override 注入 post_task 成功★: {len(full_ov)} 项覆盖、节点图合法、"
              f"未抛异常；静态帧下立即 post_stop 不等待 pipeline 结果；"
              f"InGameSuccess inverse=True 与三态不受影响；无 InGameMarker_* 死节点（P1-1 盲区已补）")
    except Exception as exc:
        failures.append(f"#23 完整 override 注入检查失败: {exc}")

    # 26) ★#39★ MV 探索性实跑采集模式：默认关闭零回归 + 旁路只读 + 既有红线完好
    try:
        src26 = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        run_body26 = src26[src26.index("def run(cfg: dict"):src26.index("def static_self_check")]
        finish_body = src26[src26.index("def _finish_ok("):src26.index("def _direct_confirm(")]
        # ★#49 适配★ 采集循环切片原到 def run；现 mv_flow_enabled/_mv_match/run_mv_flow 插入其间，
        # 收窄到 mv_explore_loop 自身函数体 —— 只读旁观断言的意图不变（MV 流程合法使用置前/点击）。
        loop_body = src26[src26.index("def mv_explore_loop("):src26.index("def mv_flow_enabled(")]

        # (a) 默认关闭（含缺 key 兼容），配置齐全且取值合理
        assert mv_explore_enabled(cfg) is False, "#39 mv_explore 必须默认 false"
        assert mv_explore_enabled({"flow": {}}) is False, "#39 缺 key 必须按 false 处理"
        ec26 = cfg["flow"]["mv_explore_cfg"]
        assert 0.8 <= float(ec26["sample_interval_sec"]) <= 1.0, "采样间隔须在 0.8~1.0s"
        assert float(ec26["max_duration_sec"]) > 0 and int(ec26["max_frames"]) > 0, \
            "时长/留图数上限须为正"
        assert bool(ec26["only_changed_frames"]) is True, "须开启『变化帧才留』"

        # (b) 三处 OK_MARKER return 全部经 _finish_ok；双关闭分支在截图/日志之前直接返回
        #     ★#49 适配★ _finish_ok 现分流 mv_explore（采集）与 mv_enabled（自动观看），
        #     双关闭合并分支保持「截图前直接 return True, OK_MARKER」的零回归语义；
        #     断言锚点同步适配、意图不变，MV 分支断言在 [28] 补强。
        assert run_body26.count("return _finish_ok(") == 3, \
            "#39 三处 OK_MARKER 返回必须全部改为 return _finish_ok(...)（漏一处直判会跳过采集）"
        assert "\n                return True, OK_MARKER" in finish_body, \
            "#39/#49 双关闭分支须直接 return True, OK_MARKER（16 空格）"
        # ★#54 适配★ _finish_ok 现分流 mv_explore/mv/play，锚点改三关闭合并分支，意图不变
        idx_both26 = finish_body.index(
            "if (not mv_explore_enabled(cfg) and not mv_flow_enabled(cfg)")
        idx_default26 = finish_body.index("return True, OK_MARKER", idx_both26)
        idx_log26 = finish_body.index("log(", idx_both26)
        idx_cap26 = finish_body.index("screencap_fn=")
        assert idx_both26 < idx_default26 < idx_log26 < idx_cap26, \
            "#39/#49 双关闭分支必须在任何日志与截图之前直接返回（零截图/零留图/零日志/零时序变化）"

        # (c) 开启时的循环必须有：帧差去重、时长/留图数上限、Ctrl+C 收尾、进程消失收尾、
        #     无效帧跳过；且绝不抢焦点/点击/发任务/识别/停任务（只读旁观）
        for key26, desc26 in [
            ("frame_diff_threshold", "帧差去重阈值"),
            ("max_duration", "总时长上限"),
            ("max_frames", "最大留图数"),
            ("KeyboardInterrupt", "Ctrl+C 优雅收尾"),
            ("process_gone", "进程消失收尾"),
            ("only_changed", "变化帧才留"),
            ("_mv_frame_signature", "无效帧（终端/全黑/尺寸不符）跳过"),
        ]:
            assert key26 in loop_body, f"#39 观察循环缺少 {desc26}（{key26}）"
        assert "force_foreground" not in loop_body, \
            "#39 观察循环内不得反复抢窗口焦点（会抢用户焦点、干扰手动点击）"
        assert all(k not in loop_body for k in
                   ("post_click", "post_task", "post_recognition", "post_stop")), \
            "#39 采集只读旁观，绝不点击/发任务/识别/停止"

        # (d) 行为：关闭时 mv_explore_loop 零截图/零留图/零日志（mock 注入）
        calls26 = {"cap": 0, "save": 0, "log": 0, "alive": 0}

        def cap26():
            calls26["cap"] += 1
            return None

        def save26(f, tag):
            calls26["save"] += 1
            return ""

        def log26(*a, **k):
            calls26["log"] += 1

        def alive26():
            calls26["alive"] += 1
            return True

        st26 = mv_explore_loop(cfg, screencap_fn=cap26, is_alive_fn=alive26,
                               save_fn=save26, log_fn=log26,
                               sleep_fn=lambda s: None, now_fn=lambda: 0.0)
        assert st26["enabled"] is False and st26["stop_reason"] == "disabled", \
            "#39 关闭时 mv_explore_loop 须立即返回 disabled"
        assert all(v == 0 for v in calls26.values()), \
            f"#39 关闭时不得截图/留图/写日志/查进程（实际 {calls26}）"

        # (e) 行为：开启时最小驱动（fake clock 立即到时长上限）验证入口可用、无异常
        c26 = json.loads(json.dumps(cfg))
        c26["flow"]["mv_explore"] = True
        c26["flow"]["mv_explore_cfg"]["max_duration_sec"] = 0
        st26b = mv_explore_loop(c26, screencap_fn=cap26, is_alive_fn=alive26,
                                save_fn=save26, log_fn=log26,
                                sleep_fn=lambda s: None, now_fn=lambda: 0.0)
        assert st26b["enabled"] is True and st26b["stop_reason"] == "max_duration", \
            "#39 开启时循环须能正常进入并按时长上限收尾"

        # (f) 既有红线完好：三态 / in_game_marker 三数组 / <0.55 守护 / inverse=True / login.json
        nop26 = lambda *a, **k: None  # noqa: E731
        assert report_result(OK_MARKER, log_fn=nop26) == 0
        assert report_result(OK_DEGRADED_UNVERIFIED, log_fn=nop26) == 3
        assert report_result(FAIL_NOT_CONFIRMED, log_fn=nop26) == 2
        rec26 = cfg["recognition"]
        assert len(rec26["in_game_marker_template"]) == \
               len(rec26["in_game_marker_threshold"]) == \
               len(rec26["in_game_marker_roi"]) == 3, "#39 不得改动 in_game_marker 三数组"
        assert rec26["in_game_marker_threshold"] == [0.55, 0.75, 0.6], \
            "#39 严禁改动阈值数值"
        low26 = json.loads(json.dumps(cfg))
        low26["recognition"]["in_game_marker_threshold"] = [0.1, 0.1, 0.1]
        assert build_marker_params(low26) is None, "#39 <0.55 阈值下限守护须完好"
        assert build_pipeline_override(cfg)["InGameSuccess"]["inverse"] is True, \
            "#39 InGameSuccess 必须维持 inverse=True"
        login26 = json.loads((PIPELINE_DIR / "login.json").read_text(encoding="utf-8"))
        assert len(login26) == 10, "#39 login.json 须仍为 10 节点（本次不得改动）"
        assert "InGameSuccess" in login26 and "WaitInGame" in login26

        # (g) ★#46★ P1-1 max_frames 防回退 + P1-2 --mv-explore 命令行开关
        assert int(ec26["max_frames"]) >= 200, \
            "#46 P1-1 max_frames 须 >=200（60 会在 54~107s 提前截断、采不到第 5/6 步）"
        # 用「换行 + 完整签名」定位真实定义，避免误命中 [22] 里 src.index("def main(argv=None)") 的字符串字面量
        main_body26 = src26[src26.index("\ndef main(argv=None) -> int:") + 1:]
        assert 'add_argument("--mv-explore"' in main_body26, "#46 须有 --mv-explore 命令行开关"
        assert "args.mv_explore" in main_body26 and \
               'cfg["flow"]["mv_explore"] = True' in main_body26, \
            "#46 开关须做内存覆盖（仅 run 路径，不写盘）"
        assert not any(k in main_body26 for k in
                       ("write_text", "json.dump", "CONFIG_FILE", "open(")), \
            "#46 --mv-explore 仅内存覆盖，绝不写盘"
        disk_cfg26 = load_settings()
        assert disk_cfg26["flow"]["mv_explore"] is False, \
            "#46 落盘默认必须仍为 false（开关不落盘）"
        assert mv_explore_enabled(cfg) is False, \
            "#46 不带开关时 mv_explore 仍为 false"
        print("[26] ★#39/#46 MV 探索采集模式 OK★: 默认关闭(mv_explore=false)、三处 OK_MARKER "
              "return 全经 _finish_ok、关闭零截图/零留图/零日志/零时序；开启循环含帧差去重/"
              "时长+留图数上限/Ctrl+C+进程消失收尾/无效帧跳过、不抢焦点也无任何点击；"
              "★#46: max_frames>=200、--mv-explore 开关存在/仅内存覆盖不落盘/不带开关仍 false★；"
              "三态 0/3/2、in_game_marker 三数组与 [0.55,0.75,0.6]、<0.55 守护、"
              "InGameSuccess inverse=True、login.json 10 节点均完好")
    except Exception as exc:
        failures.append(f"#39 MV 探索采集检查失败: {exc}")

    # 27) ★#48★ 实跑结果结构化输出：真调用 write_result_files 验证内容（非纯源码匹配）
    try:
        import tempfile
        tmp27 = Path(tempfile.mkdtemp(prefix="result_json_"))

        # (a) 关闭采集：mode=login、必含字段、不含采集字段、login_ok=True
        c27 = json.loads(json.dumps(cfg))
        c27["flow"]["mv_explore"] = False
        r27a = write_result_files(c27, ok=True, reason=OK_MARKER, exit_code=0,
                                  duration_sec=1.25, out_dir=tmp27, mv_stat=None)
        for k27 in ("schema_version", "timestamp", "duration_sec", "exit_code",
                    "verdict", "terminal_text", "mode", "login_ok",
                    "config_snapshot", "key_log_lines"):
            assert k27 in r27a, f"#48 last_result 缺字段 {k27}"
        assert r27a["mode"] == "login" and r27a["exit_code"] == 0 \
               and r27a["verdict"] == OK_MARKER and r27a["login_ok"] is True
        assert all(k27 not in r27a for k27 in
                   ("stop_reason", "frames_saved", "probe_files", "collection_warn")), \
            "#48 关闭采集时不得含采集字段"
        assert r27a["config_snapshot"]["in_game_marker_threshold"] == [0.55, 0.75, 0.6]
        p27 = tmp27 / "last_result.json"
        assert p27.is_file() and json.loads(p27.read_text(encoding="utf-8"))["mode"] == "login", \
            "#48 落盘的 last_result.json 内容须与返回值一致"

        # (b) 采集模式：stop_reason / 统计 / probe_files / collection_warn；login_ok 分离
        stat27 = {
            "enabled": True, "seen": 17, "saved": 10, "skipped": 1, "bad_frames": 1,
            "stop_reason": "max_frames",
            "files": ["/x/in_game_probe_x_mv_explore_0001.png"],
            "frame_meta": [("/x/in_game_probe_x_mv_explore_0001.png", 120.0, 30.0)],
        }
        c27b = json.loads(json.dumps(cfg))
        c27b["flow"]["mv_explore"] = True
        r27b = write_result_files(c27b, ok=True, reason=OK_MARKER, exit_code=0,
                                  duration_sec=2.0, out_dir=tmp27, mv_stat=stat27)
        assert r27b["mode"] == "mv_explore" and r27b["stop_reason"] == "max_frames"
        assert r27b["frames_taken"] == 17 and r27b["frames_saved"] == 10 \
               and r27b["frames_skipped_invalid"] == 1
        assert r27b["probe_files"] and r27b["probe_files"][0]["mean"] == 120.0
        assert "需重跑" in r27b["collection_warn"]
        assert r27b["login_ok"] is True, "#48 登录成功与采集完整性必须分离"

        # (c) last_result.json 覆盖写；results.jsonl 只追加（2 行）
        disk27 = json.loads(p27.read_text(encoding="utf-8"))
        assert disk27["mode"] == "mv_explore" and disk27["stop_reason"] == "max_frames", \
            "#48 last_result.json 必须覆盖为最新一次"
        lines27 = (tmp27 / "results.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines27) == 2, f"#48 results.jsonl 须逐次追加 2 行，实际 {len(lines27)}"
        assert json.loads(lines27[0])["mode"] == "login"
        assert json.loads(lines27[1])["mode"] == "mv_explore"

        # (d) 崩溃兜底：run.bat 内存在（真调用写出验证在 35-verify.py）
        bat27 = (BASE_DIR / "run.bat").read_text(encoding="utf-8")
        assert "interpreter_crash" in bat27 and "CRASH48" in bat27, \
            "#48 run.bat 须含崩溃兜底"

        # (e) ★#51★ login_ok 与 MV 流程分离：run() 返回 (False, FAIL_MV_FLOW) 时
        #     login_ok 必须仍为 True（登录本身已 OK_MARKER 成功，不被 MV 附加流程覆盖）
        nop27 = lambda *a, **k: None  # noqa: E731
        assert report_result(FAIL_MV_FLOW, log_fn=nop27) == 2
        r27e = write_result_files(cfg, ok=False, reason=FAIL_MV_FLOW, exit_code=2,
                                  duration_sec=1.0, out_dir=tmp27, mv_stat=None)
        assert r27e["verdict"] == FAIL_MV_FLOW and r27e["login_ok"] is True, \
            "#51 login_ok 须与 MV 流程结果分离（登录已成功的事实不被 FAIL_MV_FLOW 覆盖）"
        assert r27e["exit_code"] == 2, "#51 FAIL_MV_FLOW 场景 exit_code 仍须为 2"
        print("[27] ★#48/#51 结果文件 OK★: 真调用 write_result_files —— 关闭采集 mode=login"
              "（不含采集字段）/ 采集含 stop_reason+统计+probe_files+collection_warn /"
              "login_ok 与采集完整性分离 / last_result.json 覆盖写 / results.jsonl 只追加 /"
              "run.bat 崩溃兜底存在 / ★#51: FAIL_MV_FLOW 时 login_ok 仍为 True★")
    except Exception as exc:
        failures.append(f"#48 结果文件检查失败: {exc}")

    # 28) ★#49★ MV 自动观看流程：配置/模板/默认关闭/confirm 链/接入/三态不受影响/失败路径真调用
    try:
        src28 = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        # (a) 默认关闭 + 缺 key 兼容 + 配置结构
        assert mv_flow_enabled(cfg) is False, "#49 mv_enabled 必须默认 false"
        assert mv_flow_enabled({"flow": {}}) is False, "#49 缺 key 必须按 false 处理"
        main_body49 = src28[src28.index("\ndef main(argv=None) -> int:") + 1:]
        assert 'add_argument("--mv-enabled"' in main_body49 \
               and "args.mv_enabled" in main_body49 \
               and 'cfg["flow"]["mv_enabled"] = True' in main_body49, \
            "#49 --mv-enabled 开关须存在并做内存覆盖"
        assert not any(k in main_body49 for k in
                       ("write_text", "json.dump", "CONFIG_FILE", "open(")), \
            "#49 --mv-enabled 仅内存覆盖，绝不写盘"
        mvcfg28 = cfg["flow"]["mv"]
        steps28 = mvcfg28["steps"]
        assert [s28["id"] for s28 in steps28] == [
            "1_click_live", "2_click_watch", "3_click_start",
            "4_tap_screen", "5_click_ff", "6_click_back"], "#49 步骤 id 须按七步语义完整"
        assert float(mvcfg28["overall_timeout_sec"]) > 0 \
               and float(mvcfg28["step_timeout_sec"]) > 0 \
               and int(mvcfg28["step_retry"]) >= 0 \
               and float(mvcfg28["return_home_confirm_sec"]) > 0, "#49 超时/重试参数须有效"

        # (b) 模板文件存在 + 尺寸 + 阈值下限 + confirm 链
        for t28, (w28, h28) in {"mv_watch_button.png": (195, 53),
                                "mv_start_button.png": (195, 53),
                                "mv_ff_button.png": (62, 62),
                                "mv_back_button.png": (52, 52)}.items():
            p28 = IMAGE_DIR / t28
            assert p28.is_file(), f"#49 模板缺失: {t28}"
            with open(p28, "rb") as fh28:
                head28 = fh28.read(33)
            assert struct.unpack(">II", head28[16:24]) == (w28, h28), \
                f"#49 模板 {t28} 尺寸须为 {w28}x{h28}"
        for s28 in steps28:
            for th28 in (s28.get("threshold") or []):
                assert float(th28) >= 0.55, f"#49 MV 阈值严禁 <0.55（{s28['id']}={th28}）"
            for r28 in (s28.get("roi") or []):
                assert len(r28) == 4, f"#49 roi 须为 [x,y,w,h]（{s28['id']}）"

        # (b2) ★#51★ 弹窗检查配置与模板（放弃中断Live确认 → 自动点「放 弃」）
        assert bool(mvcfg28.get("dialog_check")) is True, "#51 dialog_check 须默认开启"
        dlgs28 = mvcfg28.get("dialogs") or []
        assert dlgs28 and dlgs28[0].get("id") == "discard_live", \
            "#51 弹窗配置须存在（discard_live）"
        for t51, (w51, h51) in {"mv_dialog_discard.png": (208, 62),
                                "mv_dialog_title.png": (793, 38)}.items():
            p51 = IMAGE_DIR / t51
            assert p51.is_file(), f"#51 模板缺失: {t51}"
            with open(p51, "rb") as fh51:
                head51 = fh51.read(33)
            assert struct.unpack(">II", head51[16:24]) == (w51, h51), \
                f"#51 模板 {t51} 尺寸须为 {w51}x{h51}"
        d51 = dlgs28[0]
        assert d51["template"][0] == "mv_dialog_title.png" \
               and d51["click_template"][0] == "mv_dialog_discard.png", \
            "#51 弹窗须用标题条识别、点击目标=「放 弃」按钮（返回标题绝不自动点）"
        assert float(d51["threshold"][0]) >= 0.55 \
               and float(d51["click_threshold"][0]) >= 0.55, "#51 弹窗阈值严禁 <0.55"
        assert steps28[0]["template"][0] == "home_nav_bottom_right.png" \
               and float(steps28[0]["threshold"][0]) == 0.55, \
            "#49 第 1 步须复用主界面 Live 区域模板与阈值"
        assert steps28[3]["click"] == "coord" and steps28[3]["coord"] == [640, 360], \
            "#49 第 4 步须为屏幕中心坐标点击"
        assert [s28.get("confirm") for s28 in steps28] == [
            "mv_watch_button.png", "mv_start_button.png", "gone",
            "mv_ff_button.png", "mv_watch_button.png", "home"], "#49 confirm 链须完整"

        # (c) _finish_ok 接入 + run_mv_flow 关键结构（源码断言）
        body28 = src28[src28.index("def _finish_ok("):src28.index("def _direct_confirm(")]
        assert "run_mv_flow(" in body28 and "FAIL_MV_FLOW" in body28 \
               and "mv_flow_enabled(cfg)" in body28, "#49 _finish_ok 须接入 MV 自动观看分支"
        assert "run_play_flow(" in body28 and "FAIL_PLAY_FLOW" in body28 \
               and "play_flow_enabled(cfg)" in body28, "#54 _finish_ok 须接入打歌分支"
        assert "与 mv_explore 同时开启" in body28, "#49 采集优先互斥须 WARN"
        loop28 = src28[src28.index("def run_mv_flow("):src28.index("def run(cfg: dict")]
        assert "force_foreground(hwnd)" in loop28 and "post_click" in loop28, \
            "#49 每步须置前后点击"
        assert "{tag_prefix}_fail_" in loop28 and "{tag_prefix}_return_home" in loop28, \
            "#49/#54 失败/回主页留图 tag 须存在（tag_prefix 拼接）"
        assert "1280" in loop28 and "720" in loop28, "#49 coord 步须 1280×720±4 几何守卫"
        assert "未知" in loop28, "#49 未知弹窗段须按『未知』处理（绝不脑补点击）"
        assert "_check_dialog" in loop28 and "{tag_prefix}_dialog_" in loop28 \
               and "dialog_check" in loop28, \
            "#51 确认等待中的弹窗检查钩子（_check_dialog）须存在且接入"

        # (d) 三态语义不受影响：FAIL_MV_FLOW 走 else → exit 2；0/3/2 不变
        nop28 = lambda *a, **k: None  # noqa: E731
        assert report_result(FAIL_MV_FLOW, log_fn=nop28) == 2, "#49 FAIL_MV_FLOW 须 exit 2"
        assert report_result(OK_MARKER, log_fn=nop28) == 0 \
               and report_result(OK_DEGRADED_UNVERIFIED, log_fn=nop28) == 3 \
               and report_result(FAIL_NOT_CONFIRMED, log_fn=nop28) == 2, "#49 三态 0/3/2 不受影响"

        # (e) 行为：真实 run_mv_flow + mock 控制器（模板全不命中）→ 重试耗尽安全返回 (False, why)
        class _Job28:
            def __init__(self, v):
                self._v = v

            def wait(self):
                return self

            def get(self):
                return self._v

        class _Ctrl28:
            def post_screencap(self):
                import numpy as np
                return _Job28(np.zeros((720, 1280, 3), np.uint8))

            def post_click(self, x, y):
                return _Job28(True)

        class _Mon28:
            def alive(self):
                return True

        st28 = run_mv_flow(cfg, ctrl=_Ctrl28(), tasker=None, hwnd=None,
                           marker_hit_fn=None, monitor=_Mon28(),
                           sleep_fn=lambda s: None, now_fn=lambda: 0.0,
                           save_fn=lambda f, tag: "")
        assert st28[0] is False and "1_click_live" in st28[1], \
            f"#49 失败路径须安全返回 (False, why)，实际 {st28}"

        print("[28] ★#49/#51 MV 自动观看流程 OK★: mv_enabled 默认 false、七步 id/confirm 链完整、"
              "4 张模板存在且尺寸正确、阈值全部 ≥0.55、第 1 步复用 home_nav_bottom_right(0.55)、"
              "第 4 步 coord=[640,360]；_finish_ok 接入 MV 分支（失败→FAIL_MV_FLOW、异常吞掉、"
              "双开互斥 WARN）；每步置前+box_center 点击+几何守卫+失败留图 mv_fail_*+"
              "回主页 mv_return_home；FAIL_MV_FLOW→exit 2 且三态 0/3/2 不变；"
              "mock 控制器失败路径真调用安全返回；"
              "★#51: dialog_check=true、弹窗标题/放弃按钮模板存在且阈值≥0.55、"
              "_check_dialog 钩子接入确认循环★")
    except Exception as exc:
        failures.append(f"#49 MV 自动观看流程检查失败: {exc}")

    # 29) ★#54★ 自动打歌（跳过模式）流程：配置/模板/默认关闭/互斥/precheck/三态/失败路径真调用
    try:
        src54 = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        # (a) 默认关闭 + 缺 key 兼容 + --play-enabled 仅内存覆盖
        assert play_flow_enabled(cfg) is False, "#54 play_enabled 必须默认 false"
        assert play_flow_enabled({"flow": {}}) is False, "#54 缺 key 必须按 false 处理"
        main_body54 = src54[src54.index("\ndef main(argv=None) -> int:") + 1:]
        assert 'add_argument("--play-enabled"' in main_body54 \
               and "args.play_enabled" in main_body54 \
               and 'cfg["flow"]["play_enabled"] = True' in main_body54, \
            "#54 --play-enabled 开关须存在并做内存覆盖"
        assert not any(k in main_body54 for k in
                       ("write_text", "json.dump", "CONFIG_FILE", "open(")), \
            "#54 --play-enabled 仅内存覆盖，绝不写盘"

        # (b) 配置结构：8 步 id / 超时参数 / 7 张模板存在+尺寸 / 阈值≥0.55 / confirm 链 / precheck
        pcfg54 = cfg["flow"]["play"]
        steps54 = pcfg54["steps"]
        assert [s54["id"] for s54 in steps54] == [
            "1_click_live", "2_click_decide", "3_click_skip", "4_dialog_start",
            "5_click_ok", "6_back_team", "7_back_song", "8_confirm_home"], \
            "#54 八步 id 须完整"
        assert float(pcfg54["overall_timeout_sec"]) > 0 \
               and float(pcfg54["step_timeout_sec"]) > 0 \
               and int(pcfg54["step_retry"]) >= 0 \
               and float(pcfg54["return_home_confirm_sec"]) > 0, "#54 超时/重试参数须有效"
        for t54, (w54, h54) in {
                "play_decide_button.png": (195, 52), "play_skip_button.png": (115, 30),
                "play_start_button.png": (205, 52), "play_ok_button.png": (200, 42),
                "play_ok_button2.png": (208, 54),
                "play_dialog_title.png": (258, 30), "play_result_title.png": (326, 29),
                "play_max_checked.png": (60, 33)}.items():
            p54 = IMAGE_DIR / t54
            assert p54.is_file(), f"#54 模板缺失: {t54}"
            with open(p54, "rb") as fh54:
                head54 = fh54.read(33)
            assert struct.unpack(">II", head54[16:24]) == (w54, h54), \
                f"#54 模板 {t54} 尺寸须为 {w54}x{h54}"
        for s54 in steps54:
            for th54 in (s54.get("threshold") or []):
                assert float(th54) >= 0.55, f"#54 阈值严禁 <0.55（{s54['id']}={th54}）"
        assert steps54[0]["template"][0] == "home_nav_bottom_right.png", \
            "#54 第①步须复用主界面 Live 模板"
        assert steps54[5]["template"][0] == "mv_back_button.png" \
               and steps54[6]["template"][0] == "mv_back_button.png", \
            "#54 两次返回须复用 mv_back_button"
        assert steps54[7].get("click") == "none", "#54 第⑧步须为纯确认步（click=none）"
        # (b2) ★#60★ 第⑤步模板数组（結果页两页 OK 任一命中）
        s560 = steps54[4]
        assert s560["template"] == ["play_ok_button.png", "play_ok_button2.png"], \
            "#60 第⑤步模板须为数组（結果页两页 OK 任一命中）"
        assert len(s560["threshold"]) == 2 and len(s560["roi"]) == 2, \
            "#60 第⑤步 threshold/roi 数组须与模板一一对应"
        pc54 = steps54[3].get("precheck")
        assert pc54 and pc54["template"][0] == "play_max_checked.png" \
               and pc54["coord"] and pc54.get("miss_tag") == "play_max_unchecked", \
            "#54 第④步须带『常に最大已勾选』precheck（未命中降级继续）"
        assert [s54.get("confirm") for s54 in steps54] == [
            "play_decide_button.png", "play_skip_button.png", "play_dialog_title.png",
            "play_result_title.png", "play_skip_button.png",
            "play_decide_button.png", "home", "home"], "#54 confirm 链须完整"

        # (c) _finish_ok：play 分支接入 + 三关闭分支 + 双开互斥（mv 优先）
        finish54 = src54[src54.index("def _finish_ok("):src54.index("def _direct_confirm(")]
        assert "run_play_flow(" in finish54 and "FAIL_PLAY_FLOW" in finish54, \
            "#54 _finish_ok 须接入打歌分支"
        assert "mv_enabled 与 play_enabled 同时开启" in finish54, \
            "#54 mv/play 双开须互斥 WARN（MV 优先）"
        assert "if (not mv_explore_enabled(cfg) and not mv_flow_enabled(cfg)" in finish54 \
               and "and not play_flow_enabled(cfg)" in finish54, \
            "#54 三关闭合并分支须存在（默认零回归）"
        generic54 = src54[src54.index("def _run_flow_generic("):src54.index("def run(cfg: dict")]
        assert "precheck" in generic54 and "click_kind == \"none\"" in generic54, \
            "#54 通用执行器须支持 precheck 钩子与 click=none 纯确认步"
        # (c2) ★#56★ 三项优化断言
        assert float(cfg["flow"]["mv"]["start_delay_sec"]) > 0 \
               and float(cfg["flow"]["play"]["start_delay_sec"]) > 0, \
            "#56 start_delay_sec 须存在于 mv/play 两段（默认 3.0）"
        rc56 = steps54[4].get("re_click_during_confirm")
        assert rc56 and int(rc56["max_clicks"]) == 2 \
               and float(rc56["interval_sec"]) > 0, \
            "#56 第⑤步须启用 re_click_during_confirm（interval/max_clicks）"
        assert set(cfg["flow"]["play"]["confirm_params"].keys()) == \
               {"play_dialog_title.png", "play_result_title.png"}, \
            "#56 confirm_params 须显式覆盖 play_dialog_title/play_result_title"
        assert "start_delay" in generic54 and "_maybe_re_click" in generic54 \
               and "_warn_confirm_fallback_once" in generic54 \
               and "_CONFIRM_FALLBACK_WARNED" in src54, \
            "#56 generic 须含 start_delay/re_click/fallback WARN 实现"
        assert "_mv_match_multi" in generic54 and "_mv_match_multi(" in generic54 \
               and "tasker, frame, tpls, thrs, rois" in generic54, "#60 通用执行器须用 _mv_match_multi（数组任一命中）"

        # (d) 三态不受影响：FAIL_PLAY_FLOW → exit 2；0/3/2 不变
        nop54 = lambda *a, **k: None  # noqa: E731
        assert report_result(FAIL_PLAY_FLOW, log_fn=nop54) == 2, "#54 FAIL_PLAY_FLOW 须 exit 2"
        assert report_result(OK_MARKER, log_fn=nop54) == 0 \
               and report_result(OK_DEGRADED_UNVERIFIED, log_fn=nop54) == 3 \
               and report_result(FAIL_NOT_CONFIRMED, log_fn=nop54) == 2, "#54 三态 0/3/2 不受影响"

        # (e) schema v2：flow 字段（play 场景 flow=="play"；双开互斥 flow=="mv"）；login_ok 分离
        c54 = json.loads(json.dumps(cfg))
        c54["flow"]["play_enabled"] = True
        import tempfile, shutil
        tmp54 = Path(tempfile.mkdtemp(prefix="play_flow_"))
        r54a = write_result_files(c54, ok=False, reason=FAIL_PLAY_FLOW, exit_code=2,
                                  duration_sec=1.0, out_dir=tmp54, mv_stat=None)
        assert r54a["schema_version"] == 2 and r54a["flow"] == "play" \
               and r54a["login_ok"] is True and r54a["exit_code"] == 2, \
            "#54 schema v2 flow 字段 + login_ok 分离须成立（play 场景）"
        c54b = json.loads(json.dumps(cfg))
        c54b["flow"]["play_enabled"] = True
        c54b["flow"]["mv_enabled"] = True
        r54b = write_result_files(c54b, ok=False, reason=FAIL_MV_FLOW, exit_code=2,
                                  duration_sec=1.0, out_dir=tmp54, mv_stat=None)
        assert r54b["flow"] == "mv", "#54 双开互斥：结果文件 flow 须为 mv（MV 优先）"
        assert r54b["login_ok"] is True
        shutil.rmtree(tmp54, ignore_errors=True)

        # (f) 行为：真实 run_play_flow + mock 控制器（模板全不命中）→ 安全失败
        class _Job54:
            def __init__(self, v):
                self._v = v

            def wait(self):
                return self

            def get(self):
                return self._v

        class _Ctrl54:
            def post_screencap(self):
                import numpy as np
                return _Job54(np.zeros((720, 1280, 3), np.uint8))

            def post_click(self, x, y):
                return _Job54(True)

        class _Mon54:
            def alive(self):
                return True

        st54 = run_play_flow(cfg, ctrl=_Ctrl54(), tasker=None, hwnd=None,
                             marker_hit_fn=None, monitor=_Mon54(),
                             sleep_fn=lambda s: None, now_fn=lambda: 0.0,
                             save_fn=lambda f, tag: "")
        assert st54[0] is False and "1_click_live" in st54[1], \
            f"#54 打歌失败路径须安全返回 (False, why)，实际 {st54}"

        print("[29] ★#54 自动打歌流程 OK★: play_enabled 默认 false、八步 id/confirm 链/precheck 完整、"
              "7 张模板存在且尺寸正确、阈值全部 ≥0.55、--play-enabled 仅内存覆盖不落盘；"
              "_finish_ok play 分支接入（失败→FAIL_PLAY_FLOW、异常吞掉、双开互斥 mv 优先 WARN、"
              "三关闭零回归分支存在）；generic 支持 precheck 钩子与 click=none；"
              "schema v2 flow 字段（play 场景 flow=play、双开 flow=mv）+ login_ok 分离；"
              "FAIL_PLAY_FLOW→exit 2 且三态 0/3/2 不变；mock 失败路径真调用安全返回")
    except Exception as exc:
        failures.append(f"#54 自动打歌流程检查失败: {exc}")

    # 30) ★#64★ 登录确认阶段通用弹窗清除（popup_clear）：配置/门控/坐标界内/三态不变
    try:
        pc64 = cfg["flow"]["popup_clear"]
        assert pc64["enabled"] is True, "#64 popup_clear.enabled 须默认 true（门控兜底在代码层）"
        assert int(pc64["taps"]) == 4 and float(pc64["interval_sec"]) > 0 \
               and float(pc64["min_wait_sec"]) > 0, "#64 taps/interval/min_wait 须有效"
        cx64, cy64 = pc64["coord"]
        assert 0 <= int(cx64) < 1280 and 0 <= int(cy64) < 720, \
            "#64 popup coord 须在 1280x720 界内"
        assert "連点" in pc64["note"] or "连点" in pc64["note"], \
            "#64 note 须写明用户确认的连点关闭语义"
        src64 = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        i_pcv = src64.index("def post_click_verify")
        pcv64 = src64[i_pcv:src64.index("def direct_marker_confirm")]
        # ★#68 F3★ 门控改「行为级」断言（原为源码字面量检查，恒真、不构成证据）。
        # 澄清后的语义：生产有效门控 = enabled ∧ (mv∨play)；click_fn 未注入时额外禁用
        # （run() 无条件注入，故该条只在 --check/离线等不注入路径生效，属第三重防御）。
        import numpy as np
        _fr64 = np.random.RandomState(64).randint(60, 200, size=(720, 1280, 3)).astype(
            np.uint8)
        _clock64 = [0.0]

        def _now64():
            _clock64[0] += 1.0
            return _clock64[0]

        def _sleep64(s):
            _clock64[0] += float(s)

        def _clicks64(c64, click_fn):
            got = []
            post_click_verify(
                c64, screencap_fn=lambda: _fr64, is_alive_fn=lambda: True,
                button_visible_fn=lambda f: False, marker_hit_fn=lambda f: False,
                click_fn=(None if click_fn is None else (lambda x, y: got.append((x, y)))),
                log_fn=lambda *a, **k: None, now_fn=_now64, sleep_fn=_sleep64)
            return got

        _cfg_on64 = json.loads(json.dumps(cfg))
        _cfg_on64["flow"]["mv_enabled"] = True
        _cfg_on64["flow"]["popup_clear"]["min_wait_sec"] = 1.0
        _cfg_off64 = json.loads(json.dumps(cfg))        # mv/play 全关
        assert len(_clicks64(_cfg_on64, None)) == 0, \
            "#68 F3 click_fn 未注入时必须零点击（离线/--check 第三重防御）"
        assert len(_clicks64(_cfg_off64, "inject")) == 0, \
            "#68 F3 mv/play 全关时必须零点击（有效门控两条件之一）"
        assert len(_clicks64(_cfg_on64, "inject")) == int(pc64["taps"]), \
            "#68 F3 mv 开启 + click_fn 注入须正常连点到 taps 上限（对照组）"
        assert "popup_tap_" in pcv64 and "popup_min_wait" in pcv64 \
               and "popup_taps" in pcv64, "#64 弹窗清除子阶段须存在"
        # 普通登录零回归：popup 子阶段必须在 marker 正向判定之后（marker 命中即 return，
        # 弹窗清除绝不先于三态判定执行）
        i_marker64 = pcv64.index('return "MARKER"')
        i_popup64 = pcv64.index("popup_enabled and taps_used < popup_taps")
        assert i_marker64 < i_popup64, "#64 popup_clear 须在 marker 正向判定之后"
        run_body64 = src64[src64.index("def run(cfg: dict"):src64.index("def static_self_check")]
        assert "click_fn=lambda x, y: ctrl.post_click(x, y).wait()" in run_body64, \
            "#64 run() 调用点须注入 click_fn（普通登录不注入则门控自动关闭）"
        # 三态不受影响
        nop64 = lambda *a, **k: None  # noqa: E731
        assert report_result(OK_MARKER, log_fn=nop64) == 0 \
               and report_result(OK_DEGRADED_UNVERIFIED, log_fn=nop64) == 3 \
               and report_result(FAIL_NOT_CONFIRMED, log_fn=nop64) == 2, \
            "#64 三态 0/3/2 不受影响"
        print("[30] ★#64/#68 popup_clear OK★: 配置结构完整（taps=4/interval/coord=[640,360] 界内/"
              "min_wait=8）、门控行为级验证（mv 开+click_fn 注入→连点至 taps 上限；"
              "mv/play 全关→零点击；click_fn 未注入→零点击）、"
              "子阶段在 marker 正向判定之后（弹窗清除绝不先于三态判定）、"
              "run() 调用点无条件注入 click_fn（有效门控=enabled∧(mv∨play)）、三态 0/3/2 不受影响；"
              "★#68 F1★ 点击后重置 absent 计时基准（loading_seen_before=True 下仍能连点满 taps）；"
              "note 写明用户确认的连点关闭语义与坐标风险评估")
    except Exception as exc:
        failures.append(f"#64 popup_clear 检查失败: {exc}")

    # 31) ★#66★ 采集上限命令行覆盖：开关存在 / 仅内存不落盘 / 不带开关默认不变 / 边界校验
    try:
        src66 = (BASE_DIR / "main.py").read_text(encoding="utf-8")
        main66 = src66[src66.index("\ndef main(argv=None) -> int:") + 1:]
        assert 'add_argument("--explore-max-duration"' in main66, \
            "#66 须有 --explore-max-duration 命令行开关"
        assert 'add_argument("--explore-max-frames"' in main66, \
            "#66 须有 --explore-max-frames 命令行开关"
        # 位置：覆盖调用必须在 if args.check 之后（--check 仍用原始配置，[26]/[31] 不受影响）
        i_chk66 = main66.index("if args.check:")
        i_ovr66 = main66.index("_apply_explore_overrides(")
        assert i_chk66 < i_ovr66, \
            "#66 覆盖须放在 if args.check 之后（--check 路径不受影响）"
        # 仅内存：main() 的覆盖段绝不出现任何写盘原语
        seg66 = main66[i_ovr66:]
        assert not any(k in seg66 for k in
                       ("write_text", "json.dump", "open(", "CONFIG_FILE")), \
            "#66 覆盖仅内存，绝不写盘"
        # 不带开关：落盘默认值逐字不变
        ec_disk66 = load_settings()["flow"]["mv_explore_cfg"]
        assert float(ec_disk66["max_duration_sec"]) == 180.0 \
               and int(ec_disk66["max_frames"]) == 200, \
            "#66 settings.json 落盘默认须仍为 max_duration_sec=180 / max_frames=200"

        # 真调用纯函数：覆盖生效 + 落盘字节不变
        b_before66 = CONFIG_FILE.read_bytes()
        c66 = load_settings()
        _apply_explore_overrides(c66, max_duration=600, max_frames=1500)
        assert float(c66["flow"]["mv_explore_cfg"]["max_duration_sec"]) == 600.0 \
               and int(c66["flow"]["mv_explore_cfg"]["max_frames"]) == 1500, \
            "#66 覆盖须真实写入内存配置"
        # 单开一项不影响另一项
        c66b = load_settings()
        _apply_explore_overrides(c66b, max_frames=1200)
        assert float(c66b["flow"]["mv_explore_cfg"]["max_duration_sec"]) == \
               float(ec_disk66["max_duration_sec"]), "#66 只给 --explore-max-frames 时不得动时长"
        assert CONFIG_FILE.read_bytes() == b_before66, \
            "#66 覆盖绝不写盘（settings.json 字节须不变）"
        # 边界校验：0 / 负数 / NaN / inf 与 frames<1 必须抛 ValueError
        for bad in (0, 0.0, -1, -0.5, float("nan"), float("inf")):
            try:
                _apply_explore_overrides(load_settings(), max_duration=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"#66 max_duration={bad!r} 须被拒绝")
        for bad in (0, -1, -200):
            try:
                _apply_explore_overrides(load_settings(), max_frames=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"#66 max_frames={bad!r} 须被拒绝")
        assert "ap.error(str(exc))" in main66, \
            "#66 非法值须经 ap.error 报错（exit 2），不得带着非法上限继续跑"
        print("[31] ★#66 采集上限命令行覆盖 OK★: --explore-max-duration/--explore-max-frames "
              "存在且位于 if args.check 之后；仅内存覆盖 flow.mv_explore_cfg、"
              "settings.json 字节不变（默认 180/200 不变）；单开一项不动另一项；"
              "duration<=0/NaN/inf、frames<1 一律 ValueError → ap.error exit 2；"
              "三态 0/3/2 与 mv_explore_loop 采样逻辑未改")
    except Exception as exc:
        failures.append(f"#66 采集上限命令行覆盖检查失败: {exc}")

    print("-" * 70)
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print(f"静态自测未通过：{len(failures)} 项")
        return False
    print("静态自测全部通过（未启动游戏、未点击真实窗口、未创建 Win32 控制器）。")
    return True


def report_result(reason: str, *, log_fn=None) -> int:
    """★#21★ 三态最终输出：文案与退出码一一对应（team-lead 裁定：exit 3 不是失败，是待确认）。

    0 = 成功（已确认进入主界面） / 3 = 疑似成功（未确认） / 2 = 失败（未离开标题）。
    exit 3 时紧接着打印最新 probe 图路径，提示据此补主界面模板。
    """
    log_fn = log_fn or log
    if reason == OK_MARKER:
        log_fn(f"结果: 成功（已确认进入主界面） [{reason}] 退出码 0")
        return 0
    if reason == OK_DEGRADED_UNVERIFIED:
        log_fn(f"结果: 疑似成功（已离开标题并进入加载/游戏，但未确认主界面） [{reason}] "
               f"退出码 3 —— ★这不是失败，是待确认★")
        probes = []
        try:
            probes = sorted((DEBUG_DIR / "screencap").glob("in_game_probe_*.png"),
                            key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        except OSError:
            pass
        for p in probes:
            log_fn(f"   判定留图: {p}")
        log_fn("   提示：从上面的 in_game_probe_*.png 里挑一张确认为主界面的帧，裁模板填 "
               "config.recognition.in_game_marker_template，即可把「疑似成功」升级为「成功（已确认）」")
        return 3
    if reason == FAIL_NOT_CONFIRMED:
        log_fn(f"结果: 失败（未离开标题画面） [{reason}] 退出码 2")
        return 2
    log_fn(f"结果: 失败（{reason}） 退出码 2")
    return 2


# --------------------------------------------------------------------------- #
# ★#48★ 实跑结果结构化输出：last_result.json（覆盖写）+ results.jsonl（只追加）
#   纯旁路：在 main() 拿到 report_result 的退出码后、进程退出前写出；任何写文件异常
#   由 main() 吞掉并 WARN，绝不影响退出码与三态文案（零回归）。
#   _LAST_MV_STAT 由 run() 内的 _finish_ok 闭包在采集结束时赋值（模块级，仅新增代码使用）；
#   out_dir 可注入，供 --check [27] / 35-verify.py 用临时目录离线验证，不污染真实 debug/。
# --------------------------------------------------------------------------- #
_LAST_MV_STAT: dict | None = None


def _terminal_text_from_ring() -> str:
    """从日志环形缓存取最后一条『结果: …』原文（report_result 打印的终局文案）。"""
    for line in reversed(_LOG_RING):
        if "结果: " in line:
            return line[line.index("结果: "):]
    return ""


def write_result_files(cfg: dict, *, ok: bool, reason: str, exit_code: int,
                       duration_sec: float, out_dir=None, mv_stat=None) -> dict:
    """★#48★ 写 last_result.json（覆盖）与 results.jsonl（追加），返回写入的完整 dict。

    out_dir / mv_stat 可注入（供 --check [27] 与 35-verify.py 离线验证，不污染真实 debug/）；
    main() 实跑时不传，mv_stat 自动取模块级 _LAST_MV_STAT。
    """
    out = Path(out_dir) if out_dir else DEBUG_DIR
    out.mkdir(parents=True, exist_ok=True)

    flow = cfg.get("flow", {})
    rec = cfg.get("recognition", {})
    ecfg = flow.get("mv_explore_cfg", {}) or {}
    mode = "mv_explore" if mv_explore_enabled(cfg) else "login"
    stat = dict(mv_stat if mv_stat is not None else (_LAST_MV_STAT or {}))
    # ★#54★ schema v2：新增 flow 字段（实际执行的附加流程），mode 保留旧语义向后兼容
    #   flow: "none" / "mv_explore"（采集） / "mv"（自动观看） / "play"（自动打歌）
    #   互斥优先级与 _finish_ok 一致：采集 > mv > play
    if mv_explore_enabled(cfg):
        flow_kind = "mv_explore"
    elif play_flow_enabled(cfg) and not mv_flow_enabled(cfg):
        flow_kind = "play"
    elif mv_flow_enabled(cfg):
        flow_kind = "mv"
    else:
        flow_kind = "none"

    result = {
        "schema_version": 2,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(float(duration_sec), 3),
        "exit_code": int(exit_code),
        "verdict": reason,
        "terminal_text": _terminal_text_from_ring(),
        "mode": mode,
        "flow": flow_kind,
        # ★#51★ FAIL_MV_FLOW 时 run() 返回 (False, FAIL_MV_FLOW)，但登录本身已 OK_MARKER 成功
        #（19:22:19 实测）—— login_ok 表达「登录是否成功」，不得被 MV 附加流程结果覆盖。
        # ★#54★ FAIL_PLAY_FLOW 时登录同样已成功，login_ok 一并不被附加流程结果覆盖
        "login_ok": bool(ok) or reason in (FAIL_MV_FLOW, FAIL_PLAY_FLOW),
        "config_snapshot": {
            "mv_explore": bool(flow.get("mv_explore", False)),
            "mv_enabled": bool(flow.get("mv_enabled", False)),
            "play_enabled": bool(flow.get("play_enabled", False)),
            "max_frames": ecfg.get("max_frames"),
            "max_duration_sec": ecfg.get("max_duration_sec"),
            "sample_interval_sec": ecfg.get("sample_interval_sec"),
            "in_game_marker_threshold": rec.get("in_game_marker_threshold"),
        },
        "key_log_lines": list(_LOG_RING[-_LOG_RING_MAX:]),
    }

    if mode == "mv_explore":
        stop_reason = stat.get("stop_reason")
        saved = int(stat.get("saved", 0))
        result["stop_reason"] = stop_reason
        result["frames_taken"] = int(stat.get("seen", 0))
        result["frames_saved"] = saved
        result["frames_skipped_invalid"] = int(stat.get("skipped", 0))
        result["probe_files"] = [
            {"name": Path(p).name, "mean": round(float(m), 1), "std": round(float(s), 1)}
            for p, m, s in stat.get("frame_meta", [])
        ]
        if stop_reason in ("max_frames", "error") or saved == 0:
            result["collection_warn"] = "素材未采全、需重跑"

    (out / "last_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    history = {
        "timestamp": result["timestamp"],
        "exit_code": result["exit_code"],
        "verdict": result["verdict"],
        "mode": result["mode"],
        "stop_reason": result.get("stop_reason"),
        "frames_saved": result.get("frames_saved"),
    }
    with (out / "results.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(history, ensure_ascii=False) + "\n")
    return result


def _apply_explore_overrides(cfg: dict, *, max_duration=None, max_frames=None) -> dict:
    """★#66★ 采集模式上限的命令行覆盖（仅内存，绝不写盘）。

    max_duration：秒，须 0 < v < inf；max_frames：张，须 >= 1 的整数。
    None 表示不覆盖 → settings.json 的落盘默认值逐字生效。
    非法值抛 ValueError，由 main() 转 argparse 报错并以 exit 2 退出。
    原地修改并返回 cfg，便于单测直接断言生效值。
    """
    if max_duration is None and max_frames is None:
        return cfg
    ec = cfg.setdefault("flow", {}).setdefault("mv_explore_cfg", {})
    if max_duration is not None:
        try:
            dur = float(max_duration)
        except (TypeError, ValueError):
            raise ValueError(f"--explore-max-duration 须为数字，当前 {max_duration!r}")
        # NaN 与任何值比较均为 False、inf 会让时长上限形同不存在 → 一并拒绝
        if not (0.0 < dur < float("inf")):
            raise ValueError(
                f"--explore-max-duration 须为大于 0 的有限秒数，当前 {max_duration!r}")
        ec["max_duration_sec"] = dur
    if max_frames is not None:
        try:
            frames = int(max_frames)
        except (TypeError, ValueError):
            raise ValueError(f"--explore-max-frames 须为整数，当前 {max_frames!r}")
        if frames < 1:
            raise ValueError(f"--explore-max-frames 须 >= 1，当前 {max_frames!r}")
        ec["max_frames"] = frames
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SongForPrism 自动登录")
    ap.add_argument("--check", action="store_true", help="静态自测后退出（不启动游戏）")
    ap.add_argument("--skip-launch", action="store_true", help="跳过启动，仅附加到已存在窗口")
    ap.add_argument("--mv-explore", action="store_true",
                    help="只读旁观采集 MV 六步界面素材（仅内存覆盖 mv_explore=true，不落盘）")
    ap.add_argument("--mv-enabled", action="store_true",
                    help="登录成功后自动观看 MV（仅内存覆盖 mv_enabled=true，不落盘）")
    ap.add_argument("--play-enabled", action="store_true",
                    help="登录成功后自动打歌（跳过模式）（仅内存覆盖 play_enabled=true，不落盘）")
    ap.add_argument("--explore-max-duration", type=float, default=None, metavar="SEC",
                    help="覆盖采集模式最长时长秒数（仅内存覆盖 mv_explore_cfg.max_duration_sec，不落盘）")
    ap.add_argument("--explore-max-frames", type=int, default=None, metavar="N",
                    help="覆盖采集模式最大留图数（仅内存覆盖 mv_explore_cfg.max_frames，不落盘）")
    args = ap.parse_args(argv)

    cfg = load_settings()
    if args.check:
        return 0 if static_self_check(cfg) else 1

    # ★#46 P1-2★ 命令行开关：仅内存覆盖，绝不写盘；落盘默认值保持不变。
    # 放在 --check 之后，保证 --check（含 --check --mv-explore）仍用原始配置，[26] 断言不受影响。
    if args.mv_explore:
        cfg["flow"]["mv_explore"] = True
    # ★#49★ 同上：--mv-enabled 仅内存覆盖 flow.mv_enabled，绝不写盘。
    if args.mv_enabled:
        cfg["flow"]["mv_enabled"] = True
    # ★#54★ 同上：--play-enabled 仅内存覆盖 flow.play_enabled，绝不写盘。
    if args.play_enabled:
        cfg["flow"]["play_enabled"] = True
    # ★#66★ 同上：--explore-max-duration/--explore-max-frames 仅内存覆盖
    # flow.mv_explore_cfg.{max_duration_sec,max_frames}，绝不写盘；非法值 ap.error → exit 2。
    if args.explore_max_duration is not None or args.explore_max_frames is not None:
        try:
            _apply_explore_overrides(cfg, max_duration=args.explore_max_duration,
                                     max_frames=args.explore_max_frames)
        except ValueError as exc:
            ap.error(str(exc))          # 打印用法 + exit 2，绝不带着非法上限继续跑

    t0_48 = time.monotonic()
    _ok, reason = run(cfg, skip_launch=args.skip_launch)
    exit_code = report_result(reason)
    # ★#48★ 结构化结果落盘（纯旁路）：写失败绝不影响退出码与三态文案。
    try:
        write_result_files(cfg, ok=_ok, reason=reason, exit_code=exit_code,
                           duration_sec=time.monotonic() - t0_48)
    except Exception as exc:  # noqa: BLE001 - 结果文件是旁路，绝不影响退出码
        log(f"写结果文件失败（忽略，退出码不变）: {exc}", "WARN")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
