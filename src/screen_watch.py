"""屏幕活动监控（第一阶段：前台窗口感知 + 正反馈触发）。

目标：让小念"看到"用户此刻在玩什么游戏 / 用什么软件、用了多久，并在合适的
时机（切到新程序稳定后、连续使用达到里程碑）触发一条正反馈/鼓励，由上层调
LLM 生成话术并用语音+气泡说出来。为后续「陪玩 / 代肝」打基础。

设计要点：
- 纯 ctypes 读取前台窗口(标题)+进程名，零新依赖，实时、极低开销。
- 只决定"何时该说"（事件 + 全局限频），说什么交给上层 LLM，简单又聪明。
- 预留截图钩子(capture)：接多模态视觉模型后即可看懂"游戏内输赢/升级"等画面。
- 忽略桌面/系统 shell/小念自己的窗口，避免误触发与自我评论。
"""

import os
import time
import threading
import ctypes
from ctypes import wintypes

# ----------------------------- Win32 前台窗口读取 ----------------------------- #
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _process_exe(pid):
    if not pid:
        return ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(4096)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def get_foreground_info():
    """返回当前前台窗口 {exe, title, pid}；失败返回 None。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _process_exe(pid.value)
    return {"exe": exe, "title": title, "pid": pid.value}


# ----------------------------- 可选：屏幕截图钩子 ----------------------------- #
def capture_screenshot(save_path):
    """把当前屏幕存为图片（供将来多模态视觉分析）。成功返回路径，否则 None。
    优先用 mss，退回 PIL.ImageGrab；两者都没有则跳过（不报错）。"""
    try:
        import mss  # type: ignore
        with mss.mss() as sct:
            img = sct.grab(sct.monitors[0])
            import mss.tools  # type: ignore
            mss.tools.to_png(img.rgb, img.size, output=save_path)
        return save_path
    except Exception:
        pass
    try:
        from PIL import ImageGrab  # type: ignore
        ImageGrab.grab().save(save_path)
        return save_path
    except Exception:
        return None


# ----------------------------- 监控主体 ----------------------------- #
class ScreenWatcher:
    """轮询前台窗口，按事件+限频回调 on_event(event_dict)。

    event_dict: {
        "kind": "start" | "milestone",   # 切到新程序稳定 / 连续使用里程碑
        "app":  友好程序名(标题优先，否则去掉 .exe 的进程名),
        "exe":  进程名,
        "title": 窗口标题,
        "minutes": 已连续使用分钟数,
        "shot": 截图路径 或 None,
    }
    """

    def __init__(self, on_event, interval_sec=5, settle_sec=20, min_gap_min=10,
                 milestones_min=(30, 60, 120), capture=False, data_dir=".",
                 ignore=None, self_names=None):
        self.on_event = on_event
        self.interval = max(2, int(interval_sec))
        self.settle_sec = max(5, int(settle_sec))
        self.min_gap = max(30.0, float(min_gap_min) * 60.0)
        self.milestones = sorted(set(int(m) for m in milestones_min if int(m) > 0))
        self.capture = bool(capture)
        self.data_dir = data_dir
        self.ignore = set((ignore or []))
        self.self_names = [s for s in (self_names or []) if s]
        self._stop = threading.Event()
        self._thread = None
        # 供上层“主动关心/搭话”循环读取的实时状态
        self._cur_exe = ""
        self._cur_title = ""
        self._cur_dwell_start = time.time()
        self._last_switch_time = time.time()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # -- 内部 --
    def _friendly(self, exe, title):
        t = (title or "").strip()
        if t:
            return t if len(t) <= 40 else t[:40] + "…"
        e = (exe or "").strip()
        return e[:-4] if e.lower().endswith(".exe") else (e or "某个程序")

    def _is_ignored(self, exe, title):
        if not exe and not title:
            return True
        if exe and exe.lower() in self.ignore:
            return True
        if not title:                      # 无标题窗口(桌面/托盘等)不评论
            return True
        for s in self.self_names:          # 小念自己的窗口不评论
            if s and s in title:
                return True
        return False

    def _emit(self, kind, exe, title, minutes):
        shot = None
        if self.capture:
            try:
                d = os.path.join(self.data_dir, "screen_watch")
                os.makedirs(d, exist_ok=True)
                shot = capture_screenshot(os.path.join(d, "latest.png"))
            except Exception:
                shot = None
        event = {
            "kind": kind,
            "app": self._friendly(exe, title),
            "exe": exe,
            "title": title,
            "minutes": int(minutes),
            "shot": shot,
        }
        try:
            self.on_event(event)
        except Exception:
            pass

    def _loop(self):
        last_key = None
        dwell_start = time.time()
        settled = False
        fired = set()
        last_comment = 0.0
        while not self._stop.is_set():
            try:
                info = get_foreground_info()
            except Exception:
                info = None
            now = time.time()
            exe = (info or {}).get("exe", "") or ""
            title = (info or {}).get("title", "") or ""
            # 记录当前前台窗口（供上层主动关心/搭话循环读取）
            self._cur_exe = exe
            self._cur_title = title

            if self._is_ignored(exe, title):
                time.sleep(self.interval)
                continue

            key = exe.lower() or title
            if key != last_key:                # 切到新程序 → 重新计时
                last_key = key
                dwell_start = now
                settled = False
                fired = set()
                self._cur_dwell_start = now
                self._last_switch_time = now   # 用户“切换窗口”=一次动作

            dwell = now - dwell_start

            # 事件1：切到新程序并稳定停留 settle_sec 后（过滤快速 alt-tab）
            if not settled and dwell >= self.settle_sec:
                settled = True
                if now - last_comment >= self.min_gap:
                    last_comment = now
                    self._emit("start", exe, title, dwell // 60)

            # 事件2：连续使用达到里程碑（鼓励 / 提醒休息）
            for m in self.milestones:
                if m not in fired and dwell >= m * 60:
                    fired.add(m)
                    if now - last_comment >= self.min_gap:
                        last_comment = now
                        self._emit("milestone", exe, title, m)

            time.sleep(self.interval)

    def current_state(self):
        """返回当前前台窗口快照，供上层“主动关心/搭话”循环判断用户状态。

        返回 {exe, title, app, dwell_seconds, last_switch}：
        - dwell_seconds：当前程序已连续使用秒数；
        - last_switch：最近一次“切换窗口”（用户动作）的时间戳。
        """
        return {
            "exe": self._cur_exe,
            "title": self._cur_title,
            "app": self._friendly(self._cur_exe, self._cur_title),
            "dwell_seconds": max(0.0, time.time() - self._cur_dwell_start),
            "last_switch": self._last_switch_time,
        }


